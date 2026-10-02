"""Bounded private WeChat reader. Stdout is plaintext for an internal SSH pipe.

Deploy this file on the guest, then run as probe. Never send its JSON to logs.
The root child reads only private read-only bind views and never changes SQLite
journals, checkpoints, source files, or application/container lifecycle.
"""
import argparse
import base64
from contextlib import ExitStack, contextmanager
import errno
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

CONTAINER = 'wechat-arm64-android'
APP_ROOT = '/data/user/0/com.tencent.mm'
SQLCIPHER = '/home/probe/sqlcipher-probe/root/usr/bin/sqlcipher'
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def docker_read(*arguments, optional=False):
    result = subprocess.run(['sudo', '-n', 'docker', 'exec', CONTAINER, *arguments], capture_output=True, timeout=30)
    if result.returncode:
        require(optional, 'source_read_failed')
        return b''
    return result.stdout


def under(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def mount_rows(path):
    rows = []
    for line in Path(path).read_text().splitlines():
        row = line.split()
        for index in (3, 4):
            row[index] = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), row[index])
        rows.append(row)
    return rows


def guest_backing(canonical, pid, container_mounts, guest_mounts):
    origin = max((row for row in container_mounts if under(canonical, row[4])), key=lambda row: len(row[4]))
    filesystem_path = posixpath.normpath(posixpath.join(origin[3], posixpath.relpath(canonical, origin[4])))
    reference = os.stat('/proc/' + pid + '/root' + canonical)
    matches = set()
    for row in guest_mounts:
        if row[2] == origin[2] and under(filesystem_path, row[3]):
            candidate = posixpath.normpath(posixpath.join(row[4], posixpath.relpath(filesystem_path, row[3])))
            try:
                info = os.stat(candidate)
                if (info.st_dev, info.st_ino) == (reference.st_dev, reference.st_ino):
                    matches.add(candidate)
            except FileNotFoundError:
                pass
    require(len(matches) == 1, 'backing_inode_unavailable')
    return matches.pop()


@contextmanager
def readonly_view(source, base, index):
    view = str(Path(base) / str(index))
    os.mkdir(view, 0o700)
    mounted = False
    try:
        result = subprocess.run(['mount', '--bind', source, view], capture_output=True, timeout=10)
        require(result.returncode == 0, 'private_bind_failed')
        mounted = True
        result = subprocess.run(['mount', '-o', 'remount,bind,ro,noatime', view], capture_output=True, timeout=10)
        require(result.returncode == 0 and os.statvfs(view).f_flag & os.ST_RDONLY, 'readonly_constraint_failed')
        yield view
    finally:
        if mounted:
            result = subprocess.run(['umount', view], capture_output=True, timeout=10)
            require(result.returncode == 0, 'private_mount_cleanup_failed')


def selection(after_id, limit, ids):
    require(type(after_id) is int and after_id >= 0, 'invalid_after_id')
    require(type(limit) is int and 1 <= limit <= 100, 'invalid_limit')
    require(len(ids) <= 20 and all(type(value) is int and value > 0 for value in ids), 'invalid_ids')
    where = 'msgId IN (' + ','.join(str(value) for value in ids) + ')' if ids else 'msgId > ' + str(after_id)
    return 'SELECT msgId FROM message WHERE ' + where + ' ORDER BY msgId LIMIT ' + str(limit)


def queries(after_id, limit, ids):
    chosen = selection(after_id, limit, ids)
    names = "COALESCE(NULLIF(c.conRemark,''),NULLIF(c.nickname,''),m.talker,'')"
    sender = "CASE WHEN m.isSend=1 THEN '' WHEN m.talker LIKE '%@chatroom' AND instr(m.content, ':'||char(10))>0 THEN substr(m.content,1,instr(m.content,':'||char(10))-1) ELSE m.talker END"
    statements = [
        f"SELECT m.msgId,COALESCE(CAST(m.msgSvrId AS TEXT),'0') AS msgSvrId,m.type,m.createTime,m.isSend,COALESCE(m.talker,'') AS talker,COALESCE(m.content,'') AS content,COALESCE(m.imgPath,'') AS imgPath,{names} AS conversationName,COALESCE(NULLIF(s.conRemark,''),NULLIF(s.nickname,''),{sender},'') AS senderName FROM message m LEFT JOIN rcontact c ON c.username=m.talker LEFT JOIN rcontact s ON s.username=({sender}) WHERE m.msgId IN ({chosen}) ORDER BY m.msgId",
        f"SELECT m.msgId,i.bigImgPath,i.origImgMD5,i.totalLen,i.offset,i.iscomplete FROM ImgInfo2 i JOIN message m ON (i.msglocalid=m.msgId OR (i.msgSvrId<>0 AND i.msgSvrId=m.msgSvrId)) WHERE m.msgId IN ({chosen})",
        f"SELECT MsgLocalId AS msgId,FileName,TotalLen,FileNowSize,NetOffset FROM voiceinfo WHERE MsgLocalId IN ({chosen})",
        f"SELECT msglocalid AS msgId,filename,video_path,totallen,filenowsize,netoffset FROM videoinfo2 WHERE msglocalid IN ({chosen})",
        f"SELECT msgInfoId AS msgId,fileFullPath,totalLen,offset FROM appattach WHERE msgInfoId IN ({chosen})",
        f"SELECT m.msgId,c.dataId,c.path,c.totalLen,c.offset FROM RecordCDNInfo c JOIN RecordMessageInfo r ON r.localId=c.recordLocalId JOIN message m ON (m.msgId=r.msgId OR m.msgId=r.oriMsgId) WHERE m.msgId IN ({chosen}) AND c.isThumb=0",
        f"SELECT DISTINCT m.msgId,e.md5,e.size FROM EmojiInfo e JOIN message m ON (lower(e.md5)=lower(m.imgPath) OR instr(lower(m.content),lower(e.md5))>0) WHERE length(e.md5)=32 AND m.type=47 AND m.msgId IN ({chosen})",
    ]
    # Empty SELECTs emit no JSON in the SQLite shell. Label each result explicitly.
    return '\n'.join(".print '" + json.dumps({'section': index}) + "'\n" + sql + ';' for index, sql in enumerate(statements))


def decode_sections(output):
    decoder, sections, current = json.JSONDecoder(), [[] for _ in range(7)], None
    output = output.strip()
    while output:
        value, consumed = decoder.raw_decode(output)
        output = output[consumed:].lstrip()
        if isinstance(value, dict):
            require(set(value) == {'section'} and type(value['section']) is int and 0 <= value['section'] < 7, 'source_format_invalid')
            current = value['section']
        else:
            require(current is not None and isinstance(value, list), 'source_format_invalid')
            sections[current] = value
    return sections


def query_store(view, key, after_id, limit, ids):
    for suffix in ('', '-wal', '-shm'):
        path = view + '/EnMicroMsg.db' + suffix
        if os.path.exists(path):
            try:
                descriptor = os.open(path, os.O_RDWR)
            except OSError as error:
                require(error.errno == errno.EROFS, 'readonly_constraint_failed')
            else:
                os.close(descriptor)
                raise RuntimeError('readonly_constraint_failed')
    sql = ".bail on\n.timeout 5000\n.output /dev/null\nPRAGMA key='" + key + "';\nPRAGMA cipher_compatibility=1;\nPRAGMA query_only=ON;\n.output stdout\n.mode json\nBEGIN;\n" + queries(after_id, limit, ids) + '\nCOMMIT;\n'
    result = subprocess.run([SQLCIPHER, '-readonly', view + '/EnMicroMsg.db'], input=sql, text=True, capture_output=True, timeout=30)
    require(result.returncode == 0, 'source_transaction_failed')
    require(len(result.stdout.encode()) <= 16 * 1024 * 1024, 'source_payload_too_large')
    return decode_sections(result.stdout)


def media_specs(sections):
    specs = {}
    for index, rows in enumerate(sections[1:5], 1):
        for row in rows:
            if index == 1:
                value = {'kind': 'image', 'path': row['bigImgPath'] or '', 'byteLength': row['totalLen'] or 0, 'md5': row['origImgMD5'] or '', 'complete': bool(row['iscomplete']) and (row['offset'] or 0) >= (row['totalLen'] or 0)}
            elif index == 2:
                value = {'kind': 'audio', 'path': row['FileName'] or '', 'byteLength': row['TotalLen'] or 0, 'complete': (row['FileNowSize'] or 0) >= (row['TotalLen'] or 0)}
            elif index == 3:
                value = {'kind': 'video', 'path': row['video_path'] or row['filename'] or '', 'alternatePath': row['filename'] or '', 'byteLength': row['totallen'] or 0, 'complete': (row['filenowsize'] or 0) >= (row['totallen'] or 0)}
            else:
                value = {'kind': 'file', 'path': row['fileFullPath'] or '', 'byteLength': row['totalLen'] or 0, 'complete': (row['offset'] or 0) >= (row['totalLen'] or 0)}
            value['complete'] = value['complete'] and value['byteLength'] > 0
            specs.setdefault(row['msgId'], []).append(value)
    return specs


def sniff(data, kind, path):
    if b'#!SILK_V3' in data[:16]:
        return 'audio/silk'
    if data.startswith(b'#!AMR'):
        return 'audio/amr'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data[4:8] == b'ftyp':
        return 'video/mp4'
    if kind == 'file':
        return mimetypes.guess_type(path)[0] or 'application/octet-stream'
    return ''


def file_index(views):
    files = {}
    count = 0
    for canonical, view in views:
        for directory, children, names in os.walk(view, followlinks=False):
            relative = os.path.relpath(directory, view)
            children[:] = [name for name in children if not os.path.islink(os.path.join(directory, name))]
            if len(Path(relative).parts) >= 8:
                children[:] = []
            for name in names:
                count += 1
                require(count <= 20000, 'media_index_bound_exceeded')
                path = os.path.join(directory, name)
                if not os.path.islink(path):
                    files.setdefault(name, []).append((posixpath.join(canonical, os.path.relpath(path, view)), path, view))
    return files


def candidates_for(spec, views, index):
    value = spec['path']
    if not value or '\x00' in value or '..' in value.split('/'):
        return []
    if value.startswith(('/sdcard/', '/storage/emulated/0/')):
        value = '/data/media/0/' + value.split('/', 2 if value.startswith('/sdcard/') else 4)[-1]
    if value.startswith('/'):
        return [(value, os.path.join(view, posixpath.relpath(value, canonical)), view) for canonical, view in views if under(value, canonical)]
    name = posixpath.basename(value)
    names = [name]
    if spec['kind'] == 'audio':
        names += ['msg_' + name + '.amr', name + '.amr', name + '.silk']
    if spec['kind'] == 'video':
        names += [name + '.mp4']
    return [item for name in names for item in index.get(name, [])]


def open_media(path, view):
    components = Path(path).relative_to(view).parts
    require(bool(components) and '..' not in components, 'media_path_rejected')
    directory = os.open(view, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in components[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def resolve_media(spec, views, index, include, budget, extra_candidates=()):
    size = max(0, int(spec['byteLength']))
    result = {'path': spec['path'], 'byteLength': size, 'kind': spec['kind'], 'mimeType': 'application/octet-stream', 'state': 'pending'}
    if size > MAX_FILE_BYTES:
        result['state'] = 'unsupported_size'
        return result
    found = []
    candidates = candidates_for(spec, views, index)
    if spec.get('alternatePath'):
        # Android may retain an absolute video_path outside this mapped view;
        # the database filename still identifies the downloaded MP4 in our index.
        candidates.extend(candidates_for({'path': spec['alternatePath'], 'kind': spec['kind']}, views, index))
    candidates = list(dict.fromkeys(candidates + list(extra_candidates)))
    if len(candidates) > 8:
        return result
    for canonical, path, view in candidates:
        try:
            # dir_fd plus O_NOFOLLOW protects every component against link races.
            descriptor = open_media(path, view)
            with os.fdopen(descriptor, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or not os.fstatvfs(stream.fileno()).f_flag & os.ST_RDONLY:
                    continue
                if before.st_size > MAX_FILE_BYTES:
                    result.update(byteLength=before.st_size, state='unsupported_size')
                    continue
                if before.st_size == 0 or (size and before.st_size != size):
                    continue
                data = stream.read(MAX_FILE_BYTES + 1)
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(data) != before.st_size:
                    continue
            if spec.get('md5') and hashlib.md5(data).hexdigest().lower() != spec['md5'].lower():
                result['reason'] = 'cache_checksum_mismatch'
                continue
            mime = sniff(data, spec['kind'], canonical)
            if not mime or (spec['kind'] != 'file' and not mime.startswith({'image': 'image/', 'sticker': 'image/', 'audio': 'audio/', 'video': 'video/'}[spec['kind']])):
                result['reason'] = 'unsupported_original_format'
                continue
            # Download counters can lag even after the exact declared original
            # bytes exist. A known length plus matching format is the boundary.
            if not size and not spec.get('md5') and not spec.get('complete', False):
                continue
            found.append((canonical, data, mime))
        except (OSError, RuntimeError):
            continue
    hashes = {hashlib.sha256(data).hexdigest() for _, data, _ in found}
    if len(hashes) != 1:
        return result
    canonical, data, mime = found[0]
    result.pop('reason', None)
    result.update(path=canonical, name=posixpath.basename(canonical), byteLength=len(data), mimeType=mime, sha256=next(iter(hashes)), state='available')
    if include:
        if len(data) <= budget[0]:
            result['dataBase64'] = base64.b64encode(data).decode('ascii')
            result['state'] = 'ready'
            budget[0] -= len(data)
        else:
            result['state'] = 'batch_limit'
    return result


def declared_size(text):
    value = (text or '').strip()
    return int(value) if re.fullmatch(r'[0-9]{1,18}', value) else 0


def media_element(content, tag):
    # A received emoji can have a valid element inside a non-XML prefix/tail.
    # Parse only the bounded element rather than treating arbitrary text as XML.
    match = re.search(r'<' + tag + r'\b[^>]*(?:/>|>.*?</' + tag + '>)', content or '', re.S)
    if match:
        try:
            return ET.fromstring(match.group())
        except ET.ParseError:
            pass
    return None


def original_image_spec(message):
    element = media_element(message.get('content'), 'img')
    if element is None:
        return None
    high = max(declared_size(element.get('hdlength')), declared_size(element.get('tphdlength')))
    size = high or max(declared_size(element.get('length')), declared_size(element.get('tplength')))
    md5 = element.get('md5') or ''
    return {'byteLength': size, 'md5': md5.lower() if re.fullmatch(r'[a-fA-F0-9]{32}', md5) else ''}


def original_sticker_spec(message, emoji_rows):
    element = media_element(message.get('content'), 'emoji')
    values = element.attrib if element is not None else {}
    md5 = next((value.lower() for value in (values.get('androidmd5', ''), values.get('md5', ''), message.get('imgPath', '')) if re.fullmatch(r'[a-fA-F0-9]{32}', value)), '')
    matches = [row for row in emoji_rows if row['msgId'] == message['msgId'] and md5 and row['md5'].lower() == md5]
    size = declared_size(values.get('androidlen')) or declared_size(values.get('len'))
    if not size and len(matches) == 1:
        size = max(0, matches[0]['size'] or 0)
    if not md5:
        return None
    return {'path': md5, 'byteLength': size, 'md5': md5, 'kind': 'sticker', 'complete': False}


def app_message_media(message):
    if message['type'] & 0xFFFF != 49:
        return None, None
    content = message.get('content') or ''
    start = re.search(r'<msg(?:\s|>)', content)
    if start is None:
        return None, None
    try:
        root = ET.fromstring(content[start.start():])
    except ET.ParseError:
        return None, None
    app = root.find('appmsg')
    if app is None:
        return None, None
    subtype = app.findtext('type')
    if subtype == '6':
        return declared_size(app.findtext('appattach/totallen')), None
    if subtype != '19':
        return None, None
    record = app.find('recorditem')
    if record is None:
        return None, []
    if record.text and record.text.strip():
        try:
            record = ET.fromstring(record.text)
        except ET.ParseError:
            return None, []
    attachments = []
    for index, item in enumerate(record.findall('.//dataitem')):
        datatype = item.attrib.get('datatype', '')
        size = declared_size(item.findtext('datasize'))
        original_reference = any(item.findtext(tag) for tag in ('cdndataurl', 'sourcedatapath', 'datasourcepath', 'filefullpath'))
        if datatype not in ('2', '3', '4', '8') and not size and not original_reference:
            continue
        extension = (item.findtext('datafmt') or '').lower().lstrip('.')
        mime = mimetypes.guess_type('attachment.' + extension)[0] or ''
        kind = {'2': 'image', '3': 'audio', '4': 'video', '8': 'file'}.get(datatype, 'file')
        for prefix in ('image', 'audio', 'video'):
            if mime.startswith(prefix + '/'):
                kind = prefix
        if extension and mime and not mime.startswith(('image/', 'audio/', 'video/')):
            kind = 'file'
        # CDN/source-device paths identify missing originals; they are never
        # fetched or treated as paths on this tablet without a local mapping.
        md5 = item.findtext('fullmd5') or ''
        attachments.append({'path': '', 'byteLength': size, 'kind': kind, 'complete': False, 'recordItemIndex': index, 'dataId': item.attrib.get('dataid', ''), 'md5': md5.lower() if re.fullmatch(r'[a-fA-F0-9]{32}', md5) else ''})
    return None, attachments


def checksum_candidates(spec, index):
    # Received XML supplies checksums and sizes. Exact-byte matching
    # avoids relying on the lifetime of download bookkeeping rows.
    if not spec.get('md5') or not 0 <= spec['byteLength'] <= MAX_FILE_BYTES:
        return []
    candidates = []
    for name, entries in index.items():
        for item in entries:
            try:
                if (spec['byteLength'] and os.stat(item[1], follow_symlinks=False).st_size == spec['byteLength']) or spec['md5'] in name.lower():
                    candidates.append(item)
                    if len(candidates) > 8:
                        # ponytail: Use a durable media index when more than
                        # eight equal-sized cache files make this scan ambiguous.
                        return []
            except OSError:
                continue
    return candidates


def assemble_messages(sections, views, include_media):
    specs = media_specs(sections)
    xml_media = {message['msgId']: app_message_media(message) for message in sections[0]}
    index = file_index(views) if specs or any(record for _, record in xml_media.values()) or any(message['type'] in (3, 47) for message in sections[0]) else {}
    record_rows = sections[5] if len(sections) > 5 else []
    emoji_rows = sections[6] if len(sections) > 6 else []
    budget = [MAX_BATCH_BYTES]
    for message in sections[0]:
        file_size, record_attachments = xml_media[message['msgId']]
        message_specs = specs.get(message['msgId'], [])
        if message['type'] == 3:
            image = original_image_spec(message)
            if image:
                if not message_specs:
                    message_specs.append({'path': '', 'kind': 'image', 'complete': False, 'byteLength': 0})
                for spec in message_specs:
                    if spec['kind'] == 'image':
                        if image['byteLength']:
                            spec['byteLength'] = image['byteLength']
                        if not spec.get('md5'):
                            spec['md5'] = image['md5']
        if message['type'] == 47:
            sticker = original_sticker_spec(message, emoji_rows)
            if sticker:
                message_specs.append(sticker)
        if file_size is not None:
            file_specs = [spec for spec in message_specs if spec['kind'] == 'file']
            for spec in file_specs:
                if not spec['byteLength']:
                    spec['byteLength'] = file_size
            if not file_specs:
                message_specs.append({'path': '', 'byteLength': file_size, 'kind': 'file', 'complete': False})
        message['media'] = [resolve_media(spec, views, index, include_media, budget, checksum_candidates(spec, index) if spec['kind'] in ('image', 'sticker') else ()) for spec in message_specs]
        if message['type'] == 3:
            available = [item for item in message['media'] if item['state'] in ('ready', 'available')]
            if available:
                # ImgInfo2 can hold multiple quality rows for the same message;
                # only one verified original belongs to this photo.
                message['media'] = available[:1]
        if record_attachments is not None:
            resolved = []
            for spec in record_attachments:
                local = [row for row in record_rows if row['msgId'] == message['msgId'] and spec['dataId'] and row['dataId'] == spec['dataId'] and row['path']]
                if len(local) == 1:
                    spec['path'] = local[0]['path']
                    if not spec['byteLength']:
                        spec['byteLength'] = max(0, local[0]['totalLen'] or 0)
                media = resolve_media(spec, views, index, include_media, budget, checksum_candidates(spec, index))
                media['recordItemIndex'] = spec['recordItemIndex']
                resolved.append(media)
            message['media'].extend(resolved)
            message['recordMediaCount'] = len(resolved)
            states = {item['state'] for item in resolved}
            message['recordMediaState'] = next(iter(states)) if len(states) == 1 else 'pending' if states else 'none'
        kind = {3: 'image', 34: 'audio', 43: 'video', 47: 'sticker'}.get(message['type'])
        if kind and not message['media']:
            message['media'] = [{'path': '', 'byteLength': 0, 'kind': kind, 'mimeType': 'application/octet-stream', 'state': 'missing_metadata'}]
    return sections[0]


def private_child(request):
    require(os.geteuid() == 0 and os.readlink('/proc/self/ns/mnt') != request['namespace'], 'namespace_not_isolated')
    guest_mounts = mount_rows('/proc/self/mountinfo')
    require(not any(any(value.startswith(('shared:', 'master:')) for value in row[6:row.index('-')]) for row in guest_mounts), 'namespace_not_private')
    container_mounts = mount_rows('/proc/' + request['pid'] + '/mountinfo')
    with tempfile.TemporaryDirectory(prefix='tablet-source-ro-') as temporary, ExitStack() as stack:
        views = []
        for canonical in request['roots']:
            source = guest_backing(canonical, request['pid'], container_mounts, guest_mounts)
            view = stack.enter_context(readonly_view(source, temporary, len(views)))
            views.append((canonical, view))
        sections = query_store(views[0][1], request['key'], request['afterId'], request['limit'], request['ids'])
        messages = assemble_messages(sections, views, request['includeMedia'])
        result = {'accountFingerprint': request['accountFingerprint'], 'messages': messages}
    return result


def build_request(args):
    paths = docker_read('find', APP_ROOT, '-maxdepth', '5', '-type', 'f', '-name', 'EnMicroMsg.db').decode().splitlines()
    require(len(paths) == 1, 'ambiguous_account')
    identifiers = set()
    for name in ('auth_info_key_prefs.xml', 'system_config_prefs.xml'):
        for item in ET.fromstring(docker_read('cat', APP_ROOT + '/shared_prefs/' + name)):
            if item.attrib.get('name') in ('_auth_uin', 'default_uin', 'last_login_uin'):
                value = item.attrib.get('value', item.text or '')
                if value.lstrip('-').isdigit() and int(value) != 0:
                    identifiers.add(value)
    require(len(identifiers) == 1, 'ambiguous_account_identifier')
    identifier = next(iter(identifiers))
    canonical = docker_read('readlink', '-f', paths[0]).decode().strip()
    require(canonical.startswith('/data/') and '..' not in canonical.split('/'), 'source_path_invalid')
    roots = [posixpath.dirname(canonical)]
    for path in ('/data/media/0/Android/data/com.tencent.mm/MicroMsg', '/data/media/0/tencent/MicroMsg'):
        real = docker_read('readlink', '-f', path, optional=True).decode().strip()
        info = docker_read('stat', '-c', '%F', path, optional=True).decode().strip()
        if real and info == 'directory':
            roots.append(real)
    result = subprocess.run(['sudo', '-n', 'docker', 'inspect', '--format', '{{.State.Pid}}', CONTAINER], capture_output=True, text=True, timeout=20)
    pid = result.stdout.strip()
    require(result.returncode == 0 and pid.isdigit() and int(pid) > 0, 'container_unavailable')
    return {'pid': pid, 'roots': roots, 'namespace': os.readlink('/proc/self/ns/mnt'), 'key': hashlib.md5(('1234567890ABCDEF' + identifier).encode()).hexdigest()[:7], 'accountFingerprint': hashlib.sha256(('wechat-tablet-account-v1:' + identifier).encode()).hexdigest(), 'afterId': args.after_id, 'limit': args.limit, 'ids': args.ids, 'includeMedia': args.include_media}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--after-id', type=int, default=0)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--ids', default='')
    parser.add_argument('--include-media', action='store_true')
    parser.add_argument('--private-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.private_child:
        result = private_child(json.load(sys.stdin))
    else:
        require(not args.ids or re.fullmatch(r'[1-9][0-9]*(,[1-9][0-9]*)*', args.ids), 'invalid_ids')
        args.ids = [int(value) for value in args.ids.split(',')] if args.ids else []
        selection(args.after_id, args.limit, args.ids)
        request = build_request(args)
        result = subprocess.run(['sudo', '-n', 'unshare', '--mount', '--propagation', 'private', '--', 'python3', str(Path(__file__).resolve()), '--private-child'], input=json.dumps(request), text=True, capture_output=True, timeout=120)
        if result.returncode:
            try:
                code = json.loads(result.stderr)['error']
            except (ValueError, KeyError, TypeError):
                code = 'source_reader_failed'
            raise RuntimeError(code if isinstance(code, str) and re.fullmatch('[a-z_]+', code) else 'source_reader_failed')
        result = json.loads(result.stdout)
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error': str(error) if isinstance(error, RuntimeError) else 'source_reader_unavailable'}), file=sys.stderr)
        sys.exit(1)
