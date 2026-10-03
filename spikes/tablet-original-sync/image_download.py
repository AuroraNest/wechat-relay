"""Bounded photo and record-sticker requests through official WeChat viewers."""
import json
import subprocess
import time
import uuid

from source_reader import MAX_FILE_BYTES, app_message_media, original_image_spec


def image_command(batch, row, now):
    image = original_image_spec(row)
    if (row.get('type') != 3 or not image
            or not 0 < image['byteLength'] <= MAX_FILE_BYTES
            or not row.get('talker') or str(row.get('msgSvrId', '0')) == '0'
            or type(batch.get('sourceMaxId')) is not int):
        return None
    media = row.get('media', [])
    if not media or any(item.get('state') in ('ready', 'available') for item in media):
        return None
    identity = f"{batch['accountFingerprint']}|{row['msgId']}|{row['msgSvrId']}"
    preview = image['requiresHD'] and row.get('imagePreview', {}).get('state') not in ('ready', 'available')
    if preview:
        identity += '|preview'
    return {'v': 1, 'operation': 'download-image', 'replyId': str(uuid.uuid5(uuid.NAMESPACE_OID, identity)),
            'accountFingerprint': batch['accountFingerprint'], 'sourceMessageId': row['msgId'],
            'sourceServerId': str(row['msgSvrId']), 'conversationId': row['talker'],
            'originalMD5': image['md5'], 'originalByteLength': image['byteLength'], 'requiresHD': image['requiresHD'],
            'sourceMaxId': batch['sourceMaxId'], 'expiresAt': now + 360_000,
            **({'downloadPreview': True} if preview else {})}


def record_image_commands(batch, row, now):
    _, specs = app_message_media(row)
    if (not specs or not row.get('talker') or str(row.get('msgSvrId', '0')) == '0'
            or type(batch.get('sourceMaxId')) is not int):
        return []
    commands = []
    for spec in specs:
        data_id = spec.get('dataId', '')
        sticker = spec.get('recordDataType') == '37' and spec.get('kind') == 'sticker'
        image = spec.get('recordDataType') == '2' and spec.get('kind') == 'image'
        if (not (sticker or image) or not data_id or not spec.get('md5')
                or not (0 <= spec['byteLength'] <= MAX_FILE_BYTES if sticker else 0 < spec['byteLength'] <= MAX_FILE_BYTES)
                or sum(item.get('dataId') == data_id for item in specs) != 1):
            continue
        media = [item for item in row.get('media', []) if item.get('recordItemIndex') == spec['recordItemIndex']]
        if len(media) != 1 or media[0].get('state') in ('ready', 'available'):
            continue
        identity = f"{batch['accountFingerprint']}|{row['msgId']}|{row['msgSvrId']}|record|{spec['recordDataType']}|{data_id}|{spec['md5']}|{spec['byteLength']}"
        commands.append({'v': 1, 'operation': 'download-record-sticker' if sticker else 'download-record-image',
                         'replyId': str(uuid.uuid5(uuid.NAMESPACE_OID, identity)),
                         'accountFingerprint': batch['accountFingerprint'], 'sourceMessageId': row['msgId'],
                         'sourceServerId': str(row['msgSvrId']), 'conversationId': row['talker'],
                         'recordDataId': data_id, 'recordItemIndex': spec['recordItemIndex'],
                         'originalMD5': spec['md5'], 'originalByteLength': spec['byteLength'],
                         'sourceMaxId': batch['sourceMaxId'], 'expiresAt': now + 360_000})
    return commands


def image_commands(batch, row, now):
    command = image_command(batch, row, now)
    return ([command] if command else []) + record_image_commands(batch, row, now)


def request_image(executor, command):
    try:
        result = subprocess.run([str(executor)], input=json.dumps(command, ensure_ascii=False).encode(),
                                capture_output=True, timeout=420)
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError()
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError()
        return response
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {'status': 'DOWNLOAD_UNCONFIRMED'}


def download_pending(batch, executor, account, attempts, request=request_image, now_ms=lambda: time.time_ns() // 1_000_000, prefer_retry=False):
    if executor is None or not account or batch.get('accountFingerprint') != account:
        return None, [], False
    now = now_ms()
    pending = []
    retry = False
    outstanding = []
    for row in batch['messages']:
        for command in image_commands(batch, row, now):
            key = command['replyId']
            count, due = attempts.get(key, (0, 0))
            if due == float('inf'):
                continue
            outstanding.append((row['msgId'], key))
            if due > now:
                retry = True
                continue
            pending.append((row['msgId'], key, count, command))
    if not pending:
        return None, [], retry
    # Ordinary content precedes quality upgrades, including preview retries.
    pending.sort(key=lambda item: (item[3].get('requiresHD', False) and not item[3].get('downloadPreview', False),
                                   bool(item[2]), -item[0]))
    source_id, key, count, command = pending[0]
    started = time.monotonic()
    response = request(executor, command)
    statuses = {'IMAGE_AVAILABLE', 'PREVIEW_AVAILABLE', 'DOWNLOAD_REQUESTED', 'DOWNLOAD_UNCONFIRMED', 'IMAGE_DOWNLOAD_NOT_AVAILABLE',
                'IMAGE_ORIGINAL_EXPIRED', 'AUTOMATION_NOT_READY'}
    observed = response.get('status')
    log = {'imageDownload': observed if observed in statuses else 'DOWNLOAD_UNCONFIRMED',
           'operation': command['operation'], 'durationMs': max(0, round((time.monotonic() - started) * 1000))}
    reason = response.get('reason')
    if isinstance(reason, str) and reason in {'ui_executor_busy', 'adapter_command_failed', 'adapter_timeout', 'source_target_unavailable',
                  'source_target_invalid', 'source_account_changed', 'source_recipient_changed',
                  'source_watermark_changed', 'source_image_changed', 'source_viewer_changed',
                  'source_viewer_unavailable', 'source_image_button_ambiguous', 'source_image_window_unavailable',
                  'source_image_window_changed', 'source_gate_invalid', 'source_gate_expired',
                  'source_download_wait_timeout'}:
        log['reason'] = reason
    print(json.dumps(log), flush=True)
    terminal = response.get('status') == 'IMAGE_ORIGINAL_EXPIRED'
    # ponytail: Download is idempotent, so retry state may reset on collector restart.
    # Persist it only if repeated unavailable originals cause measurable UI churn.
    attempts[key] = (count + 1, float('inf') if terminal else now_ms() + min(300_000, 15_000 * 2 ** min(count, 5)))
    remaining = list(dict.fromkeys(item[0] for item in outstanding if item[1] != key))
    return source_id, remaining, retry or not terminal
