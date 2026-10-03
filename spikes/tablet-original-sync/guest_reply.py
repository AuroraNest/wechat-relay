#!/usr/bin/env python3
"""Private guest adapter for official WeChat UI. Never print source or command payloads."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

import source_reader
from reader_transport import ReaderError, ReaderSession

PACKAGE = 'com.aurora.tabletrelay'
COMPONENT = PACKAGE + '/' + PACKAGE + '.ReplyInstrumentation'
IMAGE_VIEWER = 'com.tencent.mm.ui.chatting.gallery.ImageGalleryUI'
RECORD_IMAGE_VIEWER = 'com.tencent.mm.plugin.record.ui.RecordMsgImageUI'
RECORD_DETAIL_VIEWER = 'com.tencent.mm.feature.appmsg.ui.RecordDetailUI'
RECORD_DOWNLOADS = ('download-record-image', 'download-record-sticker')
IMAGE_DOWNLOADS = ('download-image', *RECORD_DOWNLOADS)
SOURCE = ReaderSession([sys.executable, str(Path(source_reader.__file__).resolve())], diagnostics=False)


def docker(*arguments, data=None, timeout=30):
    result = subprocess.run(['sudo', '-n', 'docker', 'exec', '-i', source_reader.CONTAINER, *arguments],
                            input=data, capture_output=True, timeout=timeout)
    if result.returncode or len(result.stdout) > 16384:
        raise RuntimeError('adapter_command_failed')
    return result.stdout


def read_batch(source_id, defer_media, timeout):
    try:
        batch = SOURCE.read(0, [source_id], False, defer_media, timeout=timeout, limit=1)
        if len(json.dumps(batch).encode()) > 2 * 1024 * 1024:
            raise ReaderError('source_target_unavailable')
        return batch
    except ReaderError:
        raise RuntimeError('source_target_unavailable') from None


def verify_source(command, timeout=45):
    if command.get('sourceContactSnapshotId') is not None:
        try:
            snapshot_id = command['sourceContactSnapshotId']
            if str(uuid.UUID(snapshot_id)) != snapshot_id or command.get('sourceMessageId') is not None:
                raise ValueError()
            batch = SOURCE.read(0, [], False, True, timeout=timeout, limit=1)
        except (ValueError, TypeError, AttributeError, ReaderError):
            raise RuntimeError('source_target_invalid') from None
        if batch.get('accountFingerprint') != command.get('accountFingerprint'):
            raise RuntimeError('source_account_changed')
        contact = source_reader.contact_for_send(batch.get('contactSnapshot'), command.get('conversationId'), command.get('alias'))
        if contact is None or contact.get('name') != command.get('conversationName'):
            raise RuntimeError('source_recipient_changed')
        watermark = batch.get('sourceMaxId')
        if type(watermark) is not int or type(command.get('sourceMaxId')) is not int or watermark < command['sourceMaxId']:
            raise RuntimeError('source_watermark_changed')
        return watermark
    source_id = command.get('sourceMessageId')
    if type(source_id) is not int or source_id <= 0:
        raise RuntimeError('source_target_invalid')
    batch = read_batch(source_id, True, timeout)
    records = batch.get('messages', [])
    if len(records) != 1 or batch.get('accountFingerprint') != command.get('accountFingerprint'):
        raise RuntimeError('source_account_changed')
    row = records[0]
    contact = row.get('replyIdentity', {}).get('contact', {})
    if (row.get('msgId') != source_id or str(row.get('msgSvrId')) != str(command.get('sourceServerId'))
            or row.get('talker') != command.get('conversationId')
            or str(row.get('talker', '')).endswith('@chatroom')
            or contact.get('username') != command.get('conversationId') or not contact.get('alias')
            or contact.get('alias') != command.get('alias')):
        raise RuntimeError('source_recipient_changed')
    if source_reader.contact_for_send(batch.get('contactSnapshot'), command.get('conversationId'), command.get('alias')) is None:
        raise RuntimeError('source_recipient_changed')
    watermark = batch.get('sourceMaxId')
    if type(watermark) is not int or type(command.get('sourceMaxId')) is not int or watermark < command['sourceMaxId']:
        raise RuntimeError('source_watermark_changed')
    return watermark


def verify_image_source(command, timeout=45):
    source_id = command.get('sourceMessageId')
    if type(source_id) is not int or source_id <= 0:
        raise RuntimeError('source_target_invalid')
    return validate_image_batch(command, read_batch(source_id, True, timeout))


def validate_image_batch(command, batch):
    source_id = command.get('sourceMessageId')
    rows = batch.get('messages', [])
    if len(rows) != 1 or batch.get('accountFingerprint') != command.get('accountFingerprint'):
        raise RuntimeError('source_account_changed')
    row = rows[0]
    record = command.get('operation') in RECORD_DOWNLOADS
    sticker = command.get('operation') == 'download-record-sticker'
    if record:
        _, specs = source_reader.app_message_media(row)
        matches = [spec for spec in specs or [] if spec.get('dataId') == command.get('recordDataId')]
        image = matches[0] if len(matches) == 1 else None
        valid_image = (image is not None and image.get('recordDataType') == ('37' if sticker else '2')
                       and image.get('kind') == ('sticker' if sticker else 'image') and bool(image.get('dataId'))
                       and type(command.get('recordItemIndex')) is int
                       and image['recordItemIndex'] == command['recordItemIndex']
                       and bool(image.get('md5')))
    else:
        image = source_reader.original_image_spec(row)
        valid_image = (row.get('type') == 3 and image is not None
                       and type(command.get('requiresHD')) is bool
                       and image['requiresHD'] == command['requiresHD']
                       and type(command.get('downloadPreview', False)) is bool
                       and (not command.get('downloadPreview') or image['requiresHD']))
    if (row.get('msgId') != source_id or not valid_image
            or str(row.get('msgSvrId')) != str(command.get('sourceServerId'))
            or not row.get('talker') or row.get('talker') != command.get('conversationId')
            or not image or not (0 <= image['byteLength'] <= source_reader.MAX_FILE_BYTES if sticker
                                  else 0 < image['byteLength'] <= source_reader.MAX_FILE_BYTES)
            or image['md5'] != command.get('originalMD5')
            or image['byteLength'] != command.get('originalByteLength')):
        raise RuntimeError('source_image_changed')
    watermark = batch.get('sourceMaxId')
    if type(watermark) is not int or type(command.get('sourceMaxId')) is not int or watermark < command['sourceMaxId']:
        raise RuntimeError('source_watermark_changed')
    if command.get('viewerTask') is not None and viewer_task() != command['viewerTask']:
        raise RuntimeError('source_viewer_changed')
    return watermark


def image_original_ready(command, timeout=120, allow_preview=False):
    batch = read_batch(command['sourceMessageId'], False, timeout)
    validate_image_batch(command, batch)
    media = batch['messages'][0].get('media', [])
    sticker = command.get('operation') == 'download-record-sticker'
    if command.get('operation') in RECORD_DOWNLOADS:
        media = [item for item in media if item.get('recordItemIndex') == command['recordItemIndex']
                 and item.get('originalBytesVerified') is not False]
        if len(media) != 1:
            raise RuntimeError('source_image_changed')
    if any(item.get('state') in ('available', 'ready')
           and (not sticker or (item.get('kind') == 'sticker' and item.get('mimeType', '').startswith('image/')
                                and type(item.get('byteLength')) is int
                                and 0 < item['byteLength'] <= source_reader.MAX_FILE_BYTES))
           and ((sticker and command['originalByteLength'] == 0
                 and type(item.get('byteLength')) is int and 0 < item['byteLength'] <= source_reader.MAX_FILE_BYTES)
                or item.get('byteLength') == command['originalByteLength']
                or (item.get('sourceRepresentation') == 'native_db_decoded'
                    and item.get('sourceByteLength') == command['originalByteLength']
                    and item.get('originalBytesVerified') is False))
           and re.fullmatch('[a-f0-9]{64}', item.get('sha256', '')) for item in media):
        return True
    if allow_preview:
        preview = batch['messages'][0].get('imagePreview', {})
        # Only the exact source reader's completed ordinary JPEG may end the
        # preview phase. It never satisfies the original attachment contract.
        return (preview.get('state') in ('ready', 'available')
                and preview.get('sourceRepresentation') in ('native_db_decoded', 'native_db_thumbnail')
                and preview.get('originalBytesVerified') is False
                and preview.get('mimeType') == 'image/jpeg'
                and type(preview.get('byteLength')) is int
                and 0 < preview['byteLength'] <= source_reader.MAX_FILE_BYTES
                and bool(re.fullmatch('[a-f0-9]{64}', preview.get('sha256', ''))))
    return False

def wait_image_original(command, timeout=150, allow_preview=False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if time.time_ns() // 1_000_000 >= command['expiresAt']:
            return False
        remaining = max(1, min(120, deadline - time.monotonic()))
        ready = (image_original_ready(command, remaining, allow_preview=True) if allow_preview
                 else image_original_ready(command, remaining))
        if ready:
            return True
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    return False


def viewer_activity(image_only=False, timeout=10):
    output = docker('sh', '-c', "dumpsys activity activities | grep 'topResumedActivity='", timeout=timeout).decode()
    activities = r'ui\.chatting\.gallery\.ImageGalleryUI'
    if not image_only:
        activities += r'|plugin\.record\.ui\.RecordMsgImageUI|feature\.appmsg\.ui\.RecordDetailUI'
    match = re.search(r'ActivityRecord\{([a-f0-9]+) .*?com\.tencent\.mm/(?:com\.tencent\.mm)?\.(?:' + activities + r') t([0-9]+)\b', output)
    return (int(match[2]), match[1]) if match else None


def viewer_task():
    activity = viewer_activity()
    return activity[0] if activity else None


def open_image_viewer(command):
    # A fresh task avoids singleTop reusing an earlier image's Intent.
    record = command.get('operation') in RECORD_DOWNLOADS
    previous = viewer_task() if record else None
    if command.get('operation') == 'download-record-sticker':
        server_id = command['sourceServerId']
        if not re.fullmatch('[1-9][0-9]{0,18}', server_id) or int(server_id) > 2 ** 63 - 1:
            raise RuntimeError('source_target_invalid')
        params = {'msgId': command['sourceMessageId'], 'msgTalker': command['conversationId'],
                  'msgSvrId': int(server_id), 'showShare': False}
        docker('am', 'start', '-W', '-n', 'com.tencent.mm/' + RECORD_DETAIL_VIEWER, '-f', '0x18000000',
               '--es', 'params', json.dumps(params, separators=(',', ':')), timeout=25)
    elif record:
        docker('am', 'start', '-W', '-n', 'com.tencent.mm/' + RECORD_IMAGE_VIEWER, '-f', '0x18000000',
               '--el', 'message_id', str(command['sourceMessageId']),
               '--es', 'message_talker', command['conversationId'],
               '--es', 'record_data_id', command['recordDataId'], timeout=25)
    else:
        docker('am', 'start', '-W', '-n', 'com.tencent.mm/' + IMAGE_VIEWER, '-f', '0x18000000',
               '--el', 'img_gallery_msg_id', str(command['sourceMessageId']),
               '--el', 'img_gallery_msg_svr_id', command['sourceServerId'],
               '--ez', 'img_gallery_enter_from_chatting_ui', 'true',
               '--es', 'img_gallery_talker', command['conversationId'], timeout=25)
    task = viewer_task()
    if task is None or record and task == previous:
        raise RuntimeError('source_viewer_unavailable')
    return task


def close_image_viewer(task):
    # Never send BACK to a window opened by another action or by the user.
    for _ in range(3):
        if viewer_task() != task:
            return
        docker('input', 'keyevent', 'KEYCODE_BACK', timeout=10)
        time.sleep(0.3)


def native_view_target(output, task, activity_id=None, record=False):
    """Locate an exact official target in the verified viewer activity."""
    current_task, gallery = None, False
    ancestors, buttons = [], []
    activity_name = r'feature\.appmsg\.ui\.RecordDetailUI' if record else r'ui\.chatting\.gallery\.ImageGalleryUI'
    target_class = 'com.tencent.mm.view.recyclerview.WxRecyclerView' if record else 'android.widget.Button'
    target_id, resource = ('lqa', '7f0a58af') if record else ('cnb', '7f0a17b3')
    view = re.compile(r'^(\s*)([\w.$]+)\{(\S+) ([VIG]\S*) \S+ (-?\d+),(-?\d+)-(-?\d+),(-?\d+)(?: |\})')
    for line in output.splitlines():
        header = re.match(r'TASK .*\bid=(\d+)\b', line.strip())
        if header:
            current_task, gallery, ancestors = int(header[1]), False, []
        if line.strip().startswith('ACTIVITY '):
            current = re.match(r'ACTIVITY com\.tencent\.mm/(?:com\.tencent\.mm|)\.' + activity_name + r' ([a-f0-9]+)\b', line.strip())
            gallery = current is not None and (current_task == task if activity_id is None
                        else current[1] == activity_id and current_task in (None, task))
            ancestors = []
        match = view.match(line) if gallery else None
        if match is None:
            continue
        indent, class_name, identity, flags, left, top, right, bottom = match.groups()
        depth = len(indent)
        while ancestors and ancestors[-1][0] >= depth:
            ancestors.pop()
        left, top, right, bottom = map(int, (left, top, right, bottom))
        visible = flags[0] == 'V' and right > left and bottom > top and all(parent[1] for parent in ancestors)
        origin_x = left + (ancestors[-1][2] if ancestors else 0)
        origin_y = top + (ancestors[-1][3] if ancestors else 0)
        bounds = (origin_x, origin_y, origin_x + right - left, origin_y + bottom - top)
        if ancestors:
            parent_bounds = ancestors[-1][4]
            visible = visible and all((bounds[0] >= parent_bounds[0], bounds[1] >= parent_bounds[1], bounds[2] <= parent_bounds[2], bounds[3] <= parent_bounds[3]))
        if (visible and class_name == target_class and re.search(r'\bapp:id/' + target_id + r'(?:\s|\})', line)
                and re.search(r'#' + resource + r'(?:\s|\})', line) and len(flags) >= 7 and flags[2] == 'E'
                and (record or flags[6] == 'C')):
            buttons.append((identity, bounds, tuple(parent[5] for parent in ancestors)))
        if record and buttons and visible and any(parent[5] == buttons[-1][0] for parent in ancestors):
            if len(buttons[-1]) == 3:
                buttons[-1] += ((),)
            buttons[-1] = (*buttons[-1][:3], buttons[-1][3] + (identity,))
        ancestors.append((depth, visible, origin_x, origin_y, bounds, identity))
    if len(buttons) > 1:
        raise RuntimeError('source_image_button_ambiguous')
    if not buttons:
        return None
    target = buttons[0]
    return (*target[:3], target[3] if len(target) == 4 else ()) if record else target


def native_image_button(output, task, activity_id=None):
    return native_view_target(output, task, activity_id)


def native_view_snapshot(task, record=False, deadline=None):
    remaining = 30 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        return None
    activity = viewer_activity(image_only=not record, timeout=min(10, remaining))
    if activity is None or activity[0] != task:
        raise RuntimeError('source_viewer_changed')
    remaining = 30 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        return None
    result = subprocess.run(['sudo', '-n', 'docker', 'exec', source_reader.CONTAINER,
                             'dumpsys', 'activity', 'top'], capture_output=True, text=True, timeout=min(30, remaining))
    if result.returncode or len(result.stdout.encode()) > 4 * 1024 * 1024:
        raise RuntimeError('source_image_window_unavailable')
    target = native_view_target(result.stdout, task, activity[1], record)
    if target is not None:
        # Retain the activity instance as well as the task across both snapshots.
        return (*target[:2], (activity[1], *target[2]), *target[3:])
    return target


def native_image_snapshot(task, deadline=None):
    return native_view_snapshot(task, deadline=deadline)


def wait_record_sticker(command, timeout=150):
    deadline = time.monotonic() + timeout
    previous_children = None
    for _ in range(12):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or time.time_ns() // 1_000_000 >= command['expiresAt']:
            return False
        if image_original_ready(command, min(20, remaining)):
            return True
        before = native_view_snapshot(command['viewerTask'], record=True)
        if before is None:
            break
        _, bounds, _, children = before
        # ponytail: recycled row identities may stop early; add official position
        # evidence only if a verified missing sticker requires deeper scrolling.
        if children == previous_children:
            break
        left, top, right, bottom = bounds
        if right - left < 100 or bottom - top < 100 or not children:
            break
        verify_image_source(command, timeout=min(20, max(1, deadline - time.monotonic())))
        after = native_view_snapshot(command['viewerTask'], record=True)
        if before != after or time.time_ns() // 1_000_000 >= command['expiresAt'] or time.monotonic() >= deadline:
            raise RuntimeError('source_image_window_changed')
        x = (left + right) // 2
        docker('input', 'swipe', str(x), str(top + (bottom - top) * 4 // 5),
               str(x), str(top + (bottom - top) // 5), '350', timeout=min(10, deadline - time.monotonic()))
        previous_children = children
        time.sleep(min(0.3, max(0, deadline - time.monotonic())))
    return wait_image_original(command, max(0, deadline - time.monotonic()))


def request_native_image(command, deadline=None):
    # Native View Hierarchy retains official view IDs when accessibility is empty.
    # Never tap a label, guessed screen position, hidden button or another task.
    before = native_image_snapshot(command['viewerTask']) if deadline is None else native_image_snapshot(command['viewerTask'], deadline=deadline)
    if before is None:
        return False
    remaining = 20 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        return False
    verify_image_source(command, timeout=min(20, remaining))
    after = native_image_snapshot(command['viewerTask']) if deadline is None else native_image_snapshot(command['viewerTask'], deadline=deadline)
    if (before != after or time.time_ns() // 1_000_000 >= command['expiresAt']
            or deadline is not None and time.monotonic() >= deadline):
        raise RuntimeError('source_image_window_changed')
    _, bounds, _ = before
    remaining = 10 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        return False
    docker('input', 'tap', str((bounds[0] + bounds[2]) // 2), str((bounds[1] + bounds[3]) // 2),
           timeout=min(10, remaining))
    return True


def wait_hd_image(command, timeout=150):
    started = time.monotonic()
    deadline = started + timeout
    request_deadline = min(deadline, started + 30)
    requested = False
    while time.monotonic() < deadline:
        if time.time_ns() // 1_000_000 >= command['expiresAt']:
            return False
        remaining = (deadline if requested else request_deadline) - time.monotonic()
        if remaining <= 0:
            return False
        if image_original_ready(command, min(20, remaining)):
            return True
        if not requested:
            requested = request_native_image(command, deadline=request_deadline)
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    return False


def authorize_send(command, raw, gate, verify=verify_source, now_ms=lambda: time.time_ns() // 1_000_000):
    if (not isinstance(gate, dict) or set(gate) != {'replyId', 'commandHash', 'nonce'}
            or gate.get('replyId') != command.get('replyId')
            or gate.get('commandHash') != hashlib.sha256(raw).hexdigest()):
        raise RuntimeError('source_gate_invalid')
    try:
        if str(uuid.UUID(gate['nonce'])) != gate['nonce']:
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError('source_gate_invalid') from None
    if type(command.get('expiresAt')) is not int or now_ms() >= command['expiresAt']:
        raise RuntimeError('source_gate_expired')
    watermark = verify(command, timeout=20)
    expires = min(command['expiresAt'], now_ms() + 2000)
    if now_ms() >= expires:
        raise RuntimeError('source_gate_expired')
    return {**gate, 'sourceMaxId': watermark, 'expiresAt': expires}


def instrument(command, raw):
    # Docker exec does not inherit Android init's ART environment like an adb shell.
    runtime = '''export ANDROID_ROOT=/system ANDROID_DATA=/data ANDROID_ART_ROOT=/apex/com.android.art ANDROID_I18N_ROOT=/apex/com.android.i18n ANDROID_TZDATA_ROOT=/apex/com.android.tzdata
while read -r action name value; do
  case "$action:$name" in export:BOOTCLASSPATH|export:DEX2OATBOOTCLASSPATH) export "$name=$value";; esac
done < /data/system/environ/classpath
test -n "$BOOTCLASSPATH" && test -n "$DEX2OATBOOTCLASSPATH" || exit 1
exec am instrument -w -r ''' + COMPONENT
    process = subprocess.Popen(['sudo', '-n', 'docker', 'exec', '-i', source_reader.CONTAINER,
                                'sh', '-c', runtime],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    deadline = time.monotonic() + 85
    authorized = False
    try:
        while process.poll() is None:
            if time.monotonic() >= deadline:
                raise RuntimeError('adapter_timeout')
            if command['operation'] in ('send', 'download-image') and not authorized:
                gate_bytes = docker('run-as', PACKAGE, 'sh', '-c',
                                    'if [ -f files/source-check.json ]; then cat files/source-check.json; fi', timeout=5)
                if gate_bytes:
                    gate = json.loads(gate_bytes)
                    try:
                        verify = verify_image_source if command['operation'] == 'download-image' else verify_source
                        grant = authorize_send(command, raw, gate, verify=verify)
                    except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError):
                        # A denied grant lets Java reject and clear only its unchanged draft before exiting.
                        grant = {'replyId': command['replyId'], 'commandHash': hashlib.sha256(raw).hexdigest(),
                                 'nonce': gate.get('nonce', '') if isinstance(gate, dict) else '',
                                 'sourceMaxId': -1, 'expiresAt': 0}
                    docker('run-as', PACKAGE, 'sh', '-c',
                           'umask 077; cat > files/authorization.new; mv files/authorization.new files/authorization.json',
                           data=json.dumps(grant, separators=(',', ':')).encode(), timeout=5)
                    authorized = True
            time.sleep(0.2)
        stdout, _ = process.communicate(timeout=5)
        if process.returncode or len(stdout) > 16384:
            raise RuntimeError('adapter_command_failed')
    except BaseException:
        # Killing docker exec alone does not stop an instrumentation already running in Android.
        try:
            docker('am', 'force-stop', PACKAGE, timeout=5)
        except (OSError, subprocess.TimeoutExpired, RuntimeError):
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.stdout.close()
            process.stderr.close()
        raise


def perform(command):
    if not isinstance(command, dict) or command.get('v') != 1 or command.get('operation') not in ('probe', 'inspect', 'send', *IMAGE_DOWNLOADS):
        return {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
    state = Path.home() / '.local/share/aurora-tablet-ui'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.is_symlink() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        return {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
    sending = command['operation'] == 'send'
    prioritized = sending or command['operation'] in IMAGE_DOWNLOADS
    if sending and (type(command.get('expiresAt')) is not int or time.time_ns() // 1_000_000 >= command['expiresAt']):
        return {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
    lock_deadline = time.monotonic() + 10
    with (state / 'send-priority.lock').open('a') as priority, (state / 'executor.lock').open('a') as lock:
        while True:
            try:
                if prioritized:
                    # New media yields to a waiting send; an existing operation keeps its lock.
                    fcntl.flock(priority, (fcntl.LOCK_EX if sending else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if sending and time.time_ns() // 1_000_000 >= command['expiresAt']:
                    return {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
                if not sending or time.monotonic() >= lock_deadline:
                    return {'status': 'AUTOMATION_NOT_READY', 'clicked': False, 'reason': 'ui_executor_busy'}
                time.sleep(min(0.1, max(0, lock_deadline - time.monotonic())))
        if prioritized:
            fcntl.flock(priority, fcntl.LOCK_UN)
        record = command['operation'] in RECORD_DOWNLOADS
        if not record:
            installed = docker('cmd', 'package', 'list', 'packages', '--show-versioncode', PACKAGE).decode().strip()
            if installed != 'package:' + PACKAGE + ' versionCode:3':
                return {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
        if command['operation'] in IMAGE_DOWNLOADS:
            wechat = docker('cmd', 'package', 'list', 'packages', '--show-versioncode', 'com.tencent.mm').decode().strip()
            if wechat != 'package:com.tencent.mm versionCode:3180':
                return {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
        image_task = None
        if command['operation'] in ('send', *IMAGE_DOWNLOADS):
            if type(command.get('expiresAt')) is not int or time.time_ns() // 1_000_000 >= command['expiresAt']:
                return {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
            if command['operation'] in IMAGE_DOWNLOADS:
                verify_image_source(command)
            else:
                verify_source(command)
        raw = json.dumps(command, ensure_ascii=False, separators=(',', ':')).encode()
        if len(raw) > 16384:
            return {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
        started = False
        try:
            if record:
                # Official viewers request images or visible stickers on open.
                # Only source-verified bytes establish download completion.
                if time.time_ns() // 1_000_000 >= command['expiresAt']:
                    return {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
                started = True
                image_task = open_image_viewer(command)
                command = {**command, 'viewerTask': image_task}
                ready = wait_record_sticker(command) if command['operation'] == 'download-record-sticker' else wait_image_original(command)
                return {'status': 'IMAGE_AVAILABLE' if ready else 'DOWNLOAD_UNCONFIRMED',
                        'clicked': False}
            if command['operation'] == 'download-image':
                if time.time_ns() // 1_000_000 >= command['expiresAt']:
                    return {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
                image_task = open_image_viewer(command)
                command = {**command, 'viewerTask': image_task}
                if command.get('downloadPreview'):
                    started = True
                    return {'status': 'PREVIEW_AVAILABLE' if wait_image_original(command, timeout=45, allow_preview=True) else 'DOWNLOAD_UNCONFIRMED',
                            'clicked': False}
                if not command['requiresHD']:
                    # The official viewer requests this original on open.
                    started = True
                    return {'status': 'IMAGE_AVAILABLE' if wait_image_original(command) else 'DOWNLOAD_UNCONFIRMED',
                            'clicked': False}
                # The exact native button may appear only after the gallery loads.
                started = True
                return {'status': 'IMAGE_AVAILABLE' if wait_hd_image(command) else 'DOWNLOAD_UNCONFIRMED',
                        'clicked': None}
            # run-as and instrumentation use an isolated app directory, not WeChat's source files.
            docker('run-as', PACKAGE, 'sh', '-c', 'umask 077; mkdir -p files; rm -f files/result.json files/source-check.json files/source-check.new files/authorization.json files/authorization.new; cat > files/command.json', data=raw)
            started = True
            instrument(command, raw)
            response = json.loads(docker('run-as', PACKAGE, 'cat', 'files/result.json'))
            if not isinstance(response, dict):
                raise ValueError()
            return response
        except Exception as failure:
            uncertain = 'DOWNLOAD_UNCONFIRMED' if command['operation'] in IMAGE_DOWNLOADS else 'SEND_UNCONFIRMED'
            result = {'status': uncertain if started and command['operation'] in ('send', *IMAGE_DOWNLOADS) else 'AUTOMATION_NOT_READY', 'clicked': None if started else False}
            if command['operation'] in IMAGE_DOWNLOADS:
                if isinstance(failure, RuntimeError) and re.fullmatch('[a-z_]+', str(failure)):
                    result['reason'] = str(failure)
                elif isinstance(failure, subprocess.TimeoutExpired):
                    result['reason'] = 'source_download_wait_timeout'
            return result
        finally:
            if image_task is not None:
                try:
                    close_image_viewer(image_task)
                except (OSError, subprocess.TimeoutExpired, RuntimeError):
                    pass
            if not record:
                try:
                    docker('run-as', PACKAGE, 'rm', '-f', 'files/command.json', 'files/result.json',
                           'files/source-check.json', 'files/source-check.new', 'files/authorization.json', 'files/authorization.new', timeout=5)
                except (OSError, subprocess.TimeoutExpired, RuntimeError):
                    pass


def main():
    os.umask(0o077)
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError()
        result = perform(json.loads(raw))
    except Exception:
        result = {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
    finally:
        SOURCE.close()
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
