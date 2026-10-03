"""Bounded private WeChat reader. Stdout is plaintext for an internal SSH pipe.

Deploy this file on the guest, then run as probe. Never send its JSON to logs.
The root child reads only private read-only bind views and never changes SQLite
journals, checkpoints, source files, or application/container lifecycle.
"""
import argparse
import base64
from contextlib import ExitStack, contextmanager
import ctypes
import errno
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import posixpath
import re
import select
import shlex
import stat
import struct
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

CONTAINER = 'wechat-arm64-android'
APP_ROOT = '/data/user/0/com.tencent.mm'
SQLCIPHER = '/home/probe/sqlcipher-probe/root/usr/bin/sqlcipher'
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024
WATCH_HEARTBEAT_SECONDS = 15
WATCH_DEBOUNCE_SECONDS = 0.15
# WeChat 8.0.78 ContactCountView excludes these built-in services from friends.
CONTACT_SERVICES = (
    'qqmail', 'fmessage', 'tmessage', 'qmessage', 'qqsync', 'floatbottle', 'lbsapp',
    'shakeapp', 'medianote', 'qqfriend', 'newsapp', 'blogapp', 'facebookapp',
    'topstoryapp', 'masssendapp', 'feedsapp', 'voipapp', 'cardpackage', 'voicevoipapp',
    'voiceinputapp', 'officialaccounts', 'linkedinplugin', 'notifymessage',
    'appbrandcustomerservicemsg', 'appbrand_notify_message', 'opencustomerservicemsg',
    'conversationboxservice', 'service_officialaccounts', 'BrandEcsTemplateMsg@fakeuser',
    'schedule_message', 'weixin', 'helper_entry', 'filehelper',
)


class SourceWatcher:
    # Directory events keep the watch alive across DB/WAL file replacement.
    EVENT = struct.Struct('iIII')
    INVALIDATED = 0x400 | 0x800 | 0x2000 | 0x8000
    OVERFLOW = 0x4000
    IS_DIRECTORY = 0x40000000
    MAX_DEPTH = 8
    MAX_ENTRIES = 20000
    # IN_MODIFY, ATTRIB, CLOSE_WRITE, MOVED_FROM/TO, CREATE, DELETE,
    # DELETE_SELF, MOVE_SELF, ONLYDIR and DONT_FOLLOW. Unmount/ignored are kernel events.
    MASK = 0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800 | 0x1000000 | 0x2000000
    NAMES = {b'EnMicroMsg.db', b'EnMicroMsg.db-wal'}

    def __init__(self, directory, media_roots=None):
        require(sys.platform.startswith('linux'), 'source_watch_unavailable')
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        self.descriptor = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        require(self.descriptor >= 0, 'source_watch_unavailable')
        self.libc = libc
        self.recursive = media_roots is not None
        self.watches = {}
        self.entries = 0
        try:
            if self.recursive:
                for root in [directory, *media_roots]:
                    self.add_tree(root)
                self.source_watch = next(watch for watch, (root, parts) in self.watches.items() if root == directory and not parts)
            else:
                self.source_watch = libc.inotify_add_watch(self.descriptor, os.fsencode(directory), self.MASK)
                require(self.source_watch >= 0, 'source_watch_unavailable')
        except Exception:
            self.close()
            raise

    def close(self):
        os.close(self.descriptor)

    def register_tree(self, descriptor, root, parts):
        # A pinned directory fd avoids following mutable media path components.
        watch = self.libc.inotify_add_watch(self.descriptor, ('/proc/self/fd/' + str(descriptor) + '/.').encode(), self.MASK)
        require(watch >= 0, 'source_watch_unavailable')
        if watch in self.watches:
            return
        self.watches[watch] = (root, parts)
        require(len(self.watches) <= self.MAX_ENTRIES, 'media_index_bound_exceeded')
        with os.scandir(descriptor) as entries:
            for entry in entries:
                self.entries += 1
                require(self.entries <= self.MAX_ENTRIES, 'media_index_bound_exceeded')
                if len(parts) < self.MAX_DEPTH and entry.is_dir(follow_symlinks=False):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    try:
                        self.register_tree(child, root, parts + (entry.name,))
                    finally:
                        os.close(child)

    def add_tree(self, root, parts=()):
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            self.register_tree(descriptor, root, parts)
        finally:
            os.close(descriptor)

    @classmethod
    def events(cls, data):
        offset = 0
        while offset < len(data):
            require(len(data) - offset >= cls.EVENT.size, 'source_watch_invalid')
            watch, mask, _, length = cls.EVENT.unpack_from(data, offset)
            offset += cls.EVENT.size
            require(length <= len(data) - offset, 'source_watch_invalid')
            name = data[offset:offset + length].split(b'\0', 1)[0]
            offset += length
            require(not mask & cls.INVALIDATED, 'source_watch_invalidated')
            yield watch, mask, name

    @classmethod
    def changed(cls, data):
        changed = False
        for _, mask, name in cls.events(data):
            changed = changed or bool(mask & cls.OVERFLOW) or name in cls.NAMES
        return changed

    def flags(self, data):
        if not getattr(self, 'recursive', False):
            changed = self.changed(data)
            return changed, changed
        source, media = False, False
        for watch, mask, name in self.events(data):
            # Overflow can hide a new directory. Rebuild all recursive watches
            # through the host restart instead of retaining an incomplete tree.
            require(not mask & self.OVERFLOW, 'source_watch_overflow')
            require(watch in self.watches, 'source_watch_invalidated')
            if not name or name == b'EnMicroMsg.db-shm':
                continue
            if watch == self.source_watch and name in self.NAMES:
                source, media = True, True
                continue
            if mask & self.IS_DIRECTORY:
                require(not mask & (0x40 | 0x200), 'source_watch_invalidated')
                if mask & (0x80 | 0x100):
                    root, parts = self.watches[watch]
                    if len(parts) < self.MAX_DEPTH:
                        self.add_tree(root, parts + (os.fsdecode(name),))
                    # Files can arrive before a new subtree's watches exist.
                    media = True
            else:
                media = True
        return source, media

    def wait(self, timeout):
        return any(self.wait_flags(timeout))

    def wait_flags(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0, deadline - time.monotonic())
            readable, _, _ = select.select([self.descriptor], [], [], remaining)
            if not readable:
                return False, False
            try:
                data = os.read(self.descriptor, 64 * 1024)
            except BlockingIOError:
                continue
            require(bool(data), 'source_watch_closed')
            flags = self.flags(data)
            if any(flags):
                return flags
            if time.monotonic() >= deadline:
                return False, False


def watch_source(view, media_roots=()):
    watcher = SourceWatcher(view, media_roots=media_roots)
    try:
        print('{"sourceChanged":true,"mediaChanged":true}', flush=True)
        while True:
            source, media = watcher.wait_flags(WATCH_HEARTBEAT_SECONDS)
            if source or media:
                deadline = time.monotonic() + WATCH_DEBOUNCE_SECONDS
                while time.monotonic() < deadline:
                    next_source, next_media = watcher.wait_flags(max(0, deadline - time.monotonic()))
                    source, media = source or next_source, media or next_media
            # Idle output proves the watch is alive and never requests a scan.
            print(json.dumps({'sourceChanged': source, 'mediaChanged': media}, separators=(',', ':')), flush=True)
    finally:
        watcher.close()


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


def queries(after_id, limit, ids, contact_alias=False, defer_media=False, image_columns=(), emoji_cache_key=False, contact_snapshot=False):
    chosen = selection(after_id, limit, ids)
    names = "COALESCE(NULLIF(c.conRemark,''),NULLIF(c.nickname,''),CASE WHEN m.talker LIKE '%@chatroom' THEN '群聊' ELSE m.talker END,'')"
    alias = "COALESCE(c.alias,'')" if contact_alias else "''"
    contacts = "SELECT username,MAX(conRemark) AS conRemark,MAX(nickname) AS nickname,COUNT(*) AS contactCount" + (",MAX(alias) AS alias" if contact_alias else '') + ' FROM rcontact GROUP BY username'
    sender = "CASE WHEN m.isSend=1 THEN '' WHEN m.talker LIKE '%@chatroom' AND instr(m.content, ':'||char(10))>0 THEN substr(m.content,1,instr(m.content,':'||char(10))-1) ELSE m.talker END"
    image_match = ('({alias}.msglocalid=m.msgId OR ({alias}.msgSvrId<>0 AND {alias}.msgSvrId=m.msgSvrId))'
                   ' AND (COALESCE({alias}.msglocalid,0)=0 OR {alias}.msglocalid=m.msgId)'
                   ' AND (COALESCE({alias}.msgSvrId,0)=0 OR {alias}.msgSvrId=m.msgSvrId)')
    image_join = 'ImgInfo2 i JOIN message m ON ' + image_match.format(alias='i')
    image_fields = ''
    if {'id', 'reserved1', 'compressType'} <= set(image_columns):
        # HD download rows can have neither local nor server message IDs.
        image_join = ('message m JOIN ImgInfo2 b ON ' + image_match.format(alias='b')
                      + ' JOIN ImgInfo2 i ON (i.id=b.id OR (b.reserved1>0 AND i.id=b.reserved1))'
                      + ' AND (COALESCE(i.msglocalid,0)=0 OR i.msglocalid=m.msgId)'
                      + ' AND (COALESCE(i.msgSvrId,0)=0 OR i.msgSvrId=m.msgSvrId)')
        image_fields = ',i.id AS imageId,b.reserved1 AS hdImageId,i.compressType'
        if 'msgTalker' in image_columns:
            image_fields += ',b.msgTalker AS sourceTalker'
        else:
            # This talker follows the consistent message-ID join, rather than
            # serving as independent native ImgInfo2 talker evidence.
            image_fields += ',m.talker AS sourceTalker'
    if 'thumbImgPath' in image_columns:
        image_fields += ',i.thumbImgPath'
    statements = [
        f"WITH contacts AS ({contacts}) SELECT m.msgId,COALESCE(CAST(m.msgSvrId AS TEXT),'0') AS msgSvrId,m.type,m.createTime,m.isSend,COALESCE(m.talker,'') AS talker,COALESCE(m.content,'') AS content,COALESCE(m.imgPath,'') AS imgPath,{names} AS conversationName,COALESCE(NULLIF(s.conRemark,''),NULLIF(s.nickname,''),{sender},'') AS senderName,{alias} AS recipientAlias,COALESCE(c.contactCount,0) AS recipientContactCount FROM message m LEFT JOIN contacts c ON c.username=m.talker LEFT JOIN contacts s ON s.username=({sender}) WHERE m.msgId IN ({chosen}) ORDER BY m.msgId",
        f"SELECT DISTINCT m.msgId,i.bigImgPath,i.origImgMD5,i.totalLen,i.offset,i.iscomplete{image_fields} FROM {image_join} WHERE m.msgId IN ({chosen})",
        f"SELECT MsgLocalId AS msgId,FileName,TotalLen,FileNowSize,NetOffset FROM voiceinfo WHERE MsgLocalId IN ({chosen})",
        f"SELECT msglocalid AS msgId,filename,video_path,totallen,filenowsize,netoffset FROM videoinfo2 WHERE msglocalid IN ({chosen})",
        f"SELECT msgInfoId AS msgId,fileFullPath,totalLen,offset FROM appattach WHERE msgInfoId IN ({chosen})",
        f"SELECT m.msgId,c.dataId,c.path,c.totalLen,c.offset FROM RecordCDNInfo c JOIN RecordMessageInfo r ON r.localId=c.recordLocalId JOIN message m ON (m.msgId=r.msgId OR m.msgId=r.oriMsgId) WHERE m.msgId IN ({chosen}) AND c.isThumb=0",
        f"SELECT DISTINCT m.msgId,e.md5,e.size FROM EmojiInfo e JOIN message m ON (lower(e.md5)=lower(m.imgPath) OR instr(lower(m.content),lower(e.md5))>0) WHERE length(e.md5)=32 AND m.type=47 AND m.msgId IN ({chosen})",
        "SELECT COALESCE(MAX(msgId),0) AS sourceMaxId FROM message",
    ]
    if emoji_cache_key and not defer_media:
        statements.append('SELECT md5 FROM EmojiInfo WHERE catalog=153 LIMIT 2')
    if contact_snapshot and defer_media:
        # This is the native ordinary-friends predicate, independent of the
        # message cursor. OpenIM contacts belong to a separate WeChat address book.
        excluded = ','.join("'" + name + "'" for name in CONTACT_SERVICES)
        statements.append('SELECT NULL WHERE 0')
        statements.append("SELECT username,COALESCE(NULLIF(conRemark,''),NULLIF(nickname,''),username) AS name," + ("COALESCE(alias,'')" if contact_alias else "''") + " AS alias FROM rcontact"
                          " WHERE (type&1)!=0 AND (type&32)=0 AND (type&8)=0 AND (verifyFlag&8)=0 AND deleteFlag=0"
                          " AND username NOT LIKE '%@%' AND username NOT IN (" + excluded + ")"
                          " AND username NOT IN (SELECT value FROM userinfo WHERE id=2) ORDER BY username LIMIT 10001")
        statements.append('SELECT value AS username FROM userinfo WHERE id=2 LIMIT 2')
    # Empty SELECTs emit no JSON in the SQLite shell. Label each result explicitly.
    return '\n'.join(".print '" + json.dumps({'section': index}) + "'\n" + (sql + ';' if not defer_media or index in (0, 7, 9, 10) else '') for index, sql in enumerate(statements))


def decode_sections(output):
    decoder, sections, current = json.JSONDecoder(), [[] for _ in range(8)], None
    output = output.strip()
    while output:
        value, consumed = decoder.raw_decode(output)
        output = output[consumed:].lstrip()
        if isinstance(value, dict):
            require(set(value) == {'section'} and type(value['section']) is int and 0 <= value['section'] <= 10, 'source_format_invalid')
            current = value['section']
            while current >= len(sections):
                sections.append([])
        else:
            require(current is not None and isinstance(value, list), 'source_format_invalid')
            sections[current] = value
    return sections


def query_store(view, key, after_id, limit, ids, defer_media=False):
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
    prefix = ".bail on\n.timeout 5000\n.output /dev/null\nPRAGMA key='" + key + "';\nPRAGMA cipher_compatibility=1;\nPRAGMA query_only=ON;\n.output stdout\n.mode json\nBEGIN;\n"
    # Older stores can lack alias. Detect it through a separate short,
    # read-only schema transaction instead of breaking message collection.
    schema_sql = '.print \'{"section":0}\'\nPRAGMA table_info(rcontact);\n'
    if defer_media:
        schema_sql += '.print \'{"section":3}\'\nPRAGMA table_info(userinfo);\n'
    if not defer_media:
        schema_sql += '.print \'{"section":1}\'\nPRAGMA table_info(ImgInfo2);\n'
        schema_sql += '.print \'{"section":2}\'\nPRAGMA table_info(EmojiInfo);\n'
    schema = subprocess.run([SQLCIPHER, '-readonly', view + '/EnMicroMsg.db'], input=prefix + schema_sql + 'COMMIT;\n', text=True, capture_output=True, timeout=30)
    require(schema.returncode == 0 and len(schema.stdout.encode()) <= 64 * 1024, 'source_schema_failed')
    schemas = decode_sections(schema.stdout)
    columns, image_columns, emoji_columns = schemas[:3]
    require(all(isinstance(column, dict) and isinstance(column.get('name'), str) for column in columns + image_columns + emoji_columns), 'source_schema_invalid')
    contact_alias = any(column['name'].lower() == 'alias' for column in columns)
    has_emoji_cache_key = {'md5', 'catalog'} <= {column['name'].lower() for column in emoji_columns}
    contact_snapshot = (defer_media and {'username', 'conremark', 'nickname', 'type', 'verifyflag', 'deleteflag'} <= {column['name'].lower() for column in columns}
                        and {'id', 'value'} <= {column.get('name', '').lower() for column in schemas[3] if isinstance(column, dict) and isinstance(column.get('name'), str)})
    sql = prefix + queries(after_id, limit, ids, contact_alias=contact_alias, defer_media=defer_media, image_columns=[column['name'] for column in image_columns], emoji_cache_key=has_emoji_cache_key, contact_snapshot=contact_snapshot) + '\nCOMMIT;\n'
    result = subprocess.run([SQLCIPHER, '-readonly', view + '/EnMicroMsg.db'], input=sql, text=True, capture_output=True, timeout=30)
    require(result.returncode == 0, 'source_transaction_failed')
    require(len(result.stdout.encode()) <= 16 * 1024 * 1024, 'source_payload_too_large')
    return decode_sections(result.stdout)


def contact_snapshot(sections):
    if len(sections) < 11:
        return {'state': 'unavailable', 'reason': 'contacts_schema_unsupported'}
    own = sections[10]
    if (len(own) != 1 or not isinstance(own[0], dict)
            or not isinstance(own[0].get('username'), str) or not own[0]['username']):
        return {'state': 'unavailable', 'reason': 'contacts_self_unavailable'}
    rows = sections[9]
    if len(rows) > 10000:
        return {'state': 'unavailable', 'reason': 'contacts_too_many'}
    identities, contacts = set(), []
    for row in rows:
        username = row.get('username') if isinstance(row, dict) else None
        name = row.get('name') if isinstance(row, dict) else None
        if (not isinstance(username, str) or not username or username == own[0]['username']
                or username in identities or '@' in username or username in CONTACT_SERVICES):
            return {'state': 'unavailable', 'reason': 'contacts_identity_ambiguous'}
        if not isinstance(name, str) or not name or name != name.strip() or len(name.encode()) > 512:
            return {'state': 'unavailable', 'reason': 'contacts_name_invalid'}
        alias = row.get('alias', '')
        if (not isinstance(alias, str) or len(alias) > 128 or alias != alias.strip()
                or any(ord(char) < 32 for char in alias)):
            return {'state': 'unavailable', 'reason': 'contacts_identity_ambiguous'}
        identities.add(username)
        contacts.append({'name': name, 'conversationId': username, 'alias': alias})
    return {'state': 'ready', 'contacts': sorted(contacts, key=lambda contact: contact['conversationId'])}


def contact_for_send(snapshot, conversation_id, alias=None):
    if not isinstance(snapshot, dict) or snapshot.get('state') != 'ready':
        return None
    contacts = snapshot.get('contacts')
    if not isinstance(contacts, list) or len(contacts) > 10000:
        return None
    identities = set()
    for contact in contacts:
        if not isinstance(contact, dict):
            return None
        identity = contact.get('conversationId')
        name = contact.get('name')
        if (not isinstance(identity, str) or not identity or '@' in identity
                or identity in CONTACT_SERVICES or identity in identities
                or not isinstance(name, str) or not name or name != name.strip() or len(name.encode()) > 512):
            return None
        identities.add(identity)
    matches = [contact for contact in contacts if contact['conversationId'] == conversation_id]
    if len(matches) != 1:
        return None
    contact = matches[0]
    actual_alias = contact.get('alias')
    if (not isinstance(actual_alias, str) or not 1 <= len(actual_alias) <= 128
            or actual_alias != actual_alias.strip() or any(ord(char) < 32 for char in actual_alias)
            or (alias is not None and actual_alias != alias)
            or sum(candidate.get('alias') == actual_alias for candidate in contacts) != 1):
        return None
    return contact


def media_specs(sections):
    specs = {}
    for index, rows in enumerate(sections[1:5], 1):
        for row in rows:
            if index == 1:
                # WeChat 8.0.78 origImgMD5 hashes "path-size", not file bytes.
                value = {'kind': 'image', 'path': row['bigImgPath'] or '', 'byteLength': row['totalLen'] or 0, 'cacheKey': (row['origImgMD5'] or '').lower(), 'complete': bool(row['iscomplete']) and (row['offset'] or 0) >= (row['totalLen'] or 0)}
                value.update(downloadSize=row['totalLen'] or 0, downloadOffset=row['offset'] or 0,
                             compressType=row.get('compressType'), imageId=row.get('imageId'), hdImageId=row.get('hdImageId'), sourceTalker=row.get('sourceTalker'), thumbnailPath=row.get('thumbImgPath'))
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


def emoji_cache_key(rows):
    if (len(rows) != 1 or not isinstance(rows[0], dict)
            or not isinstance(rows[0].get('md5'), str)
            or not re.fullmatch('[a-fA-F0-9]{32}', rows[0]['md5'])):
        return None
    return rows[0]['md5'].encode('ascii')[:16]


def decode_emoji_cache(data, key):
    prefix_size = min(len(data), 1024)
    if not isinstance(key, bytes) or len(key) != 16 or not prefix_size or prefix_size % 16:
        return None
    for name in ('libcrypto.so.3', 'libcrypto.so.1.1'):
        try:
            library = ctypes.CDLL(name)
            library.AES_set_decrypt_key.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            library.AES_set_decrypt_key.restype = ctypes.c_int
            library.AES_ecb_encrypt.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
            library.AES_ecb_encrypt.restype = None
            # OpenSSL 1.1/3 AES_KEY uses at most 244 bytes; retain native alignment.
            context = (ctypes.c_uint64 * 32)()
            key_buffer = ctypes.create_string_buffer(key)
            if library.AES_set_decrypt_key(key_buffer, 128, context) != 0:
                return None
            decoded = bytearray()
            for offset in range(0, prefix_size, 16):
                source = ctypes.create_string_buffer(data[offset:offset + 16])
                output = ctypes.create_string_buffer(16)
                library.AES_ecb_encrypt(source, output, context, 0)
                decoded.extend(output.raw)
            return bytes(decoded) + data[prefix_size:]
        except (OSError, AttributeError):
            continue
    return None


def resolve_media(spec, views, index, include, budget, extra_candidates=(), emoji_cache_key=None):
    size = max(0, int(spec['byteLength']))
    declared_mime = spec.get('declaredMimeType', '')
    result = {'path': spec['path'], 'byteLength': size, 'kind': spec['kind'], 'mimeType': declared_mime or 'application/octet-stream', 'state': 'pending'}
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
                native_decoded = (spec.get('nativeFullImage') is True and spec['kind'] == 'image'
                                  and before.st_size == spec.get('downloadSize')
                                  and hashlib.md5((canonical + '-' + str(before.st_size)).encode()).hexdigest() == spec.get('cacheKey')
                                  and not posixpath.basename(canonical).startswith(('th_', 'th2_'))
                                  and not canonical.startswith(('SERVERID://', 'THUMBNAIL_DIRPATH://')))
                if before.st_size == 0 or (size and before.st_size != size and not native_decoded):
                    continue
                data = stream.read(MAX_FILE_BYTES + 1)
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(data) != before.st_size:
                    continue
            exact_bytes = ((not size or len(data) == size) and (not spec.get('md5') or hashlib.md5(data).hexdigest().lower() == spec['md5'].lower()))
            if (not exact_bytes and spec['kind'] == 'sticker' and spec.get('md5')
                    and emoji_cache_key is not None):
                decoded = decode_emoji_cache(data, emoji_cache_key)
                if (decoded is not None and (not size or len(decoded) == size)
                        and hashlib.md5(decoded).hexdigest().lower() == spec['md5'].lower()):
                    data, exact_bytes = decoded, True
            if not exact_bytes and not native_decoded:
                result['reason'] = 'cache_checksum_mismatch'
                continue
            mime = sniff(data, spec['kind'], canonical)
            if spec.get('nativeThumbnail') and not (mime == 'image/jpeg' and data.endswith(b'\xff\xd9')):
                continue
            if declared_mime and mime != declared_mime:
                result['reason'] = 'declared_format_mismatch'
                continue
            if native_decoded and not exact_bytes and mime != 'image/jpeg':
                result['reason'] = 'unsupported_original_format'
                continue
            if not mime or (spec['kind'] != 'file' and not mime.startswith({'image': 'image/', 'sticker': 'image/', 'audio': 'audio/', 'video': 'video/'}[spec['kind']])):
                result['reason'] = 'unsupported_original_format'
                continue
            # Download counters can lag even after the exact declared original
            # bytes exist. A known length plus matching format is the boundary.
            if not size and not spec.get('md5') and not spec.get('complete', False):
                continue
            found.append((canonical, data, mime, native_decoded and not exact_bytes))
        except (OSError, RuntimeError):
            continue
    hashes = {hashlib.sha256(data).hexdigest() for _, data, _, _ in found}
    if len(hashes) != 1:
        return result
    canonical, data, mime, decoded = found[0]
    result.pop('reason', None)
    result.update(path=canonical, name=posixpath.basename(canonical), byteLength=len(data), mimeType=mime, sha256=next(iter(hashes)), state='available')
    if decoded:
        # This is WeChat's completed highest-quality JPEG cache, not the CDN bytes.
        result.update(name='wechat-decoded.jpg', sourceRepresentation='native_db_decoded',
                      sourceByteLength=size, originalBytesVerified=False)
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
    high = declared_size(element.get('tphdlength') if element.get('tphdurl') else element.get('hdlength'))
    size = high or declared_size(element.get('tplength') if element.get('tpurl') else element.get('length'))
    md5 = element.get('md5') or ''
    return {'byteLength': size, 'md5': md5.lower() if re.fullmatch(r'[a-fA-F0-9]{32}', md5) else '', 'requiresHD': bool(high)}


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
        if datatype not in ('2', '3', '4', '8', '37') and not size and not original_reference:
            continue
        extension = (item.findtext('datafmt') or '').lower().lstrip('.')
        mime = mimetypes.guess_type('attachment.' + extension)[0] or ''
        kind = {'2': 'image', '3': 'audio', '4': 'video', '8': 'file', '37': 'sticker'}.get(datatype, 'file')
        for prefix in ('image', 'audio', 'video'):
            if mime.startswith(prefix + '/'):
                kind = prefix
        if extension and mime and not mime.startswith(('image/', 'audio/', 'video/')):
            kind = 'file'
        # CDN/source-device paths identify missing originals; they are never
        # fetched or treated as paths on this tablet without a local mapping.
        md5 = item.findtext('fullmd5') or ''
        if datatype == '37':
            # Emoji references describe different representations. Only the
            # primary checksum binds an original; extern/thumbnail hashes do not.
            emoji = item.find('emojiitem')
            kind = 'sticker'
            if emoji is not None:
                md5 = emoji.findtext('md5') or emoji.get('md5') or md5
                size = size or declared_size(emoji.findtext('len') or emoji.get('len'))
        spec = {'path': '', 'byteLength': size, 'kind': kind, 'complete': False, 'recordItemIndex': index, 'recordDataType': datatype, 'dataId': item.attrib.get('dataid', ''), 'md5': md5.lower() if re.fullmatch(r'[a-fA-F0-9]{32}', md5) else ''}
        # Only an exact-byte record image contract can precede the file. Native
        # decoded photos and unknown emoji metadata must wait for actual bytes.
        declared_mime = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                         'gif': 'image/gif', 'webp': 'image/webp'}.get(extension)
        if datatype == '2' and spec['md5'] and size > 0 and declared_mime:
            spec['declaredMimeType'] = declared_mime
        attachments.append(spec)
    return None, attachments


def checksum_candidates(spec, index):
    # Received XML supplies checksums and sizes. Exact-byte matching
    # avoids relying on the lifetime of download bookkeeping rows.
    if not 0 <= spec['byteLength'] <= MAX_FILE_BYTES:
        return []
    native, named, sized = [], [], []
    for name, entries in index.items():
        for item in entries:
            try:
                size = os.stat(item[1], follow_symlinks=False).st_size
                if (spec.get('nativeFullImage') is True and size == spec.get('downloadSize')
                        and hashlib.md5((item[0] + '-' + str(size)).encode()).hexdigest() == spec.get('cacheKey')):
                    native.append(item)
                if spec.get('md5') and spec['md5'] in name.lower():
                    named.append(item)
                elif spec.get('md5') and spec['byteLength'] and size == spec['byteLength']:
                    # Keep the ambiguity bound without letting unrelated
                    # equal-sized caches hide an explicitly named original.
                    if len(sized) <= 8:
                        sized.append(item)
            except OSError:
                continue
    if len(native) + len(named) + len(sized) <= 8:
        return list(dict.fromkeys(native + named + sized))
    # ponytail: Use a durable media index when more than eight opaque,
    # equal-sized cache files require checksum lookup.
    return list(dict.fromkeys(native + named)) if len(native) + len(named) <= 8 else []


def image_thumbnail(message, specs, views, index, include, budget):
    value = message.get('imgPath', '')
    prefix = 'THUMBNAIL_DIRPATH://'
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    name = value[len(prefix):]
    # Both native rows must name the exact same unique file. Never guess a
    # thumbnail name from an original checksum, timestamp or file size.
    if (not name or name in ('.', '..') or '/' in name or '\x00' in name
            or len(index.get(name, [])) != 1 or not message.get('talker')
            or not any(spec.get('thumbnailPath') == value and spec.get('sourceTalker') == message['talker'] for spec in specs)):
        return None
    preview = resolve_media({'path': name, 'kind': 'image', 'byteLength': 0,
                             'complete': True, 'declaredMimeType': 'image/jpeg', 'nativeThumbnail': True},
                            views, index, include, budget)
    if preview['state'] not in ('ready', 'available', 'batch_limit'):
        return None
    return {**preview, 'name': 'preview.jpg', 'sourceRepresentation': 'native_db_thumbnail',
            'originalBytesVerified': False}


def assemble_messages(sections, views, include_media, defer_media=False):
    specs = {} if defer_media else media_specs(sections)
    xml_media = {} if defer_media else {message['msgId']: app_message_media(message) for message in sections[0]}
    index = file_index(views) if not defer_media and (specs or any(record for _, record in xml_media.values()) or any(message['type'] in (3, 47) for message in sections[0])) else {}
    record_rows = sections[5] if len(sections) > 5 else []
    emoji_rows = sections[6] if len(sections) > 6 else []
    cache_key = emoji_cache_key(sections[8]) if not defer_media and len(sections) > 8 else None
    budget = [MAX_BATCH_BYTES]
    for message in sections[0]:
        alias = message.pop('recipientAlias', '')
        contact_count = message.pop('recipientContactCount', 0)
        username = message.get('talker') or ''
        if username:
            identity = {'username': username}
            if not username.endswith('@chatroom') and contact_count == 1:
                identity['contact'] = {'username': username, 'alias': alias}
            message['replyIdentity'] = identity
        if defer_media:
            message['media'] = []
            if message['type'] != 1:
                message['mediaDeferred'] = True
            continue
        file_size, record_attachments = xml_media[message['msgId']]
        message_specs = specs.get(message['msgId'], [])
        preview_specs = []
        if message['type'] == 3:
            image = original_image_spec(message)
            if image:
                if not message_specs:
                    message_specs.append({'path': '', 'kind': 'image', 'complete': False, 'byteLength': 0})
                for spec in message_specs:
                    if spec['kind'] == 'image':
                        if image['byteLength']:
                            spec['byteLength'] = image['byteLength']
                        spec['md5'] = image['md5']
                        high = media_element(message.get('content'), 'img')
                        high_size = declared_size(high.get('tphdlength') if high.get('tphdurl') else high.get('hdlength'))
                        quality = 1 if high_size else 0
                        spec['nativeFullImage'] = (spec.get('complete') is True
                                                  and spec.get('sourceTalker') in (None, '', message.get('talker'))
                                                  and 0 < spec.get('downloadSize', 0) == spec.get('downloadOffset')
                                                  and spec.get('compressType') == quality
                                                  and (not high_size or ((spec.get('hdImageId') or 0) > 0 and spec.get('imageId') == spec['hdImageId'])))
                        if (image['requiresHD'] and spec.get('complete') is True
                                and message.get('talker') and spec.get('sourceTalker') == message['talker']
                                and 0 < spec.get('downloadSize', 0) == spec.get('downloadOffset')
                                and spec.get('compressType') == 0 and (spec.get('imageId') or 0) > 0
                                and (spec.get('hdImageId') or 0) > 0 and spec['imageId'] != spec['hdImageId']
                                and spec['path'] and not spec['path'].startswith(('SERVERID://', 'THUMBNAIL_DIRPATH://'))
                                and not posixpath.basename(spec['path']).startswith(('th_', 'th2_'))):
                            # A completed medium cache is a separate preview. Its
                            # quality must never relax the original's HD checks.
                            preview_specs.append({**spec, 'nativeFullImage': True})
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
        message['media'] = [resolve_media(spec, views, index, include_media, budget, checksum_candidates(spec, index) if spec['kind'] in ('image', 'sticker') else (), emoji_cache_key=cache_key) for spec in message_specs]
        if message['type'] == 3:
            available = [item for item in message['media'] if item['state'] in ('ready', 'available')]
            # ImgInfo2 quality rows still describe one original attachment,
            # including when all of its source representations remain pending.
            message['media'] = (available or message['media'])[:1]
            if image and image['requiresHD'] and not available:
                message['imagePreview'] = {'kind': 'image', 'name': 'preview.jpg', 'state': 'pending'}
                previews = []
                for spec in preview_specs[:8]:
                    preview = resolve_media(spec, views, index, False, budget, checksum_candidates(spec, index))
                    if (preview['state'] == 'available' and preview.get('sourceRepresentation') == 'native_db_decoded'
                            and preview.get('originalBytesVerified') is False and preview.get('mimeType') == 'image/jpeg'):
                        previews.append((spec, preview))
                if len(preview_specs) <= 8 and len({preview['sha256'] for _, preview in previews}) == 1:
                    spec, checked = previews[0]
                    preview = resolve_media(spec, views, index, include_media, budget, checksum_candidates(spec, index))
                    if (preview.get('sha256') == checked['sha256'] and preview.get('sourceRepresentation') == 'native_db_decoded'
                            and preview.get('originalBytesVerified') is False):
                        message['imagePreview'] = {**preview, 'name': 'preview.jpg'}
                if message['imagePreview']['state'] == 'pending':
                    thumbnail = image_thumbnail(message, message_specs, views, index, include_media, budget)
                    if thumbnail is not None:
                        message['imagePreview'] = thumbnail
        if record_attachments is not None:
            resolved = []
            for spec in record_attachments:
                local = [row for row in record_rows if row['msgId'] == message['msgId'] and spec['dataId'] and row['dataId'] == spec['dataId'] and row['path']]
                if len(local) == 1:
                    spec['path'] = local[0]['path']
                    if not spec['byteLength']:
                        spec['byteLength'] = max(0, local[0]['totalLen'] or 0)
                media = resolve_media(spec, views, index, include_media, budget, checksum_candidates(spec, index), emoji_cache_key=cache_key)
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


def account_identifier(preferences):
    identifiers = set()
    for value in preferences:
        for item in ET.fromstring(value):
            if item.attrib.get('name') in ('_auth_uin', 'default_uin', 'last_login_uin'):
                identifier = item.attrib.get('value', item.text or '')
                if identifier.lstrip('-').isdigit() and int(identifier) != 0:
                    identifiers.add(identifier)
    require(len(identifiers) == 1, 'ambiguous_account_identifier')
    return next(iter(identifiers))


def native_account(view):
    preferences = []
    for name in ('auth_info_key_prefs.xml', 'system_config_prefs.xml'):
        with os.fdopen(open_media(view + '/shared_prefs/' + name, view), 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_size <= 256 * 1024
                    and os.fstatvfs(stream.fileno()).f_flag & os.ST_RDONLY, 'account_preferences_invalid')
            data = stream.read(256 * 1024 + 1)
            after = os.fstat(stream.fileno())
            require(len(data) == before.st_size and (before.st_ino, before.st_size, before.st_mtime_ns)
                    == (after.st_ino, after.st_size, after.st_mtime_ns), 'account_preferences_changed')
            preferences.append(data)
    paths = []
    directory = os.open(view + '/MicroMsg', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries, 1):
                require(count <= 20000, 'account_directory_bound_exceeded')
                if not entry.is_dir(follow_symlinks=False):
                    continue
                relative = 'MicroMsg/' + entry.name + '/EnMicroMsg.db'
                try:
                    descriptor = open_media(view + '/' + relative, view)
                except FileNotFoundError:
                    continue
                try:
                    require(stat.S_ISREG(os.fstat(descriptor).st_mode), 'source_path_invalid')
                    paths.append(posixpath.dirname(relative))
                finally:
                    os.close(descriptor)
    finally:
        os.close(directory)
    require(len(paths) == 1, 'ambiguous_account')
    return paths[0], account_identifier(preferences)


def private_child(request):
    require(type(request.get('watchSource', False)) is bool and type(request.get('deferMedia', False)) is bool, 'invalid_reader_mode')
    require(not request.get('watchSource', False) or 1 <= len(request['roots']) <= 3, 'invalid_watch_roots')
    require(os.geteuid() == 0 and os.readlink('/proc/self/ns/mnt') != request['namespace'], 'namespace_not_isolated')
    guest_mounts = mount_rows('/proc/self/mountinfo')
    require(not any(any(value.startswith(('shared:', 'master:')) for value in row[6:row.index('-')]) for row in guest_mounts), 'namespace_not_private')
    container_mounts = mount_rows('/proc/' + request['pid'] + '/mountinfo')
    with tempfile.TemporaryDirectory(prefix='tablet-source-ro-') as temporary, ExitStack() as stack:
        views = []
        key, fingerprint = request.get('key'), request.get('accountFingerprint')
        roots = list(request['roots'])
        app_view, account = None, None
        if 'appRoot' in request:
            # Cache locations only. Fresh read-only metadata binds every query to
            # the current account, including account switches in the same PID.
            source = guest_backing(request['appRoot'], request['pid'], container_mounts, guest_mounts)
            app_view = stack.enter_context(readonly_view(source, temporary, 'app'))
            account = native_account(app_view)
            roots.insert(0, posixpath.join(request['appRoot'], account[0]))
            key = hashlib.md5(('1234567890ABCDEF' + account[1]).encode()).hexdigest()[:7]
            fingerprint = hashlib.sha256(('wechat-tablet-account-v1:' + account[1]).encode()).hexdigest()
        for canonical in roots:
            source = guest_backing(canonical, request['pid'], container_mounts, guest_mounts)
            view = stack.enter_context(readonly_view(source, temporary, len(views)))
            views.append((canonical, view))
        if request.get('watchSource', False):
            watch_source(views[0][1], media_roots=[view for _, view in views[1:]])
            return None
        sections = query_store(views[0][1], key, request['afterId'], request['limit'], request['ids'], defer_media=request.get('deferMedia', False))
        require(len(sections[7]) == 1 and type(sections[7][0].get('sourceMaxId')) is int
                and sections[7][0]['sourceMaxId'] >= 0, 'source_watermark_invalid')
        messages = assemble_messages(sections, views, request['includeMedia'], defer_media=request.get('deferMedia', False))
        require(app_view is None or native_account(app_view) == account, 'account_changed_during_read')
        result = {'accountFingerprint': fingerprint, 'sourceMaxId': sections[7][0]['sourceMaxId'], 'messages': messages}
        if request.get('deferMedia', False):
            result['contactSnapshot'] = contact_snapshot(sections)
    return result


def process_start(pid):
    try:
        fields = Path('/proc/' + pid + '/stat').read_text().rpartition(') ')[2].split()
        return fields[19] if len(fields) > 19 and fields[19].isdigit() else None
    except OSError:
        return None


def build_request(args, locations=None):
    pid = None
    if locations is not None:
        pid = locations.get('pid')
        # PID plus Linux start time detects replacement without starting Docker
        # for every query. Source metadata is still read fresh in the child.
        if pid and process_start(pid) == locations['processStart']:
            return {'pid': pid, 'appRoot': locations['appRoot'],
                    'roots': [] if args.defer_media else locations['mediaRoots'],
                    'namespace': os.readlink('/proc/self/ns/mnt'), 'afterId': args.after_id,
                    'limit': args.limit, 'ids': args.ids, 'includeMedia': args.include_media,
                    'deferMedia': args.defer_media, 'watchSource': False}
        locations.clear()
        result = subprocess.run(['sudo', '-n', 'docker', 'inspect', '--format', '{{.State.Pid}}', CONTAINER], capture_output=True, text=True, timeout=20)
        pid = result.stdout.strip()
        require(result.returncode == 0 and pid.isdigit() and int(pid) > 0, 'container_unavailable')
    # The pinned WeChat layout stores databases directly under account folders.
    # Avoid walking image caches and starting one Android exec for every check.
    script = 'set -e\nroot=' + shlex.quote(APP_ROOT) + r'''
find "$root/MicroMsg" -maxdepth 2 -type f -name EnMicroMsg.db -exec readlink -f {} \;
printf '\000'
cat "$root/shared_prefs/auth_info_key_prefs.xml"
printf '\000'
cat "$root/shared_prefs/system_config_prefs.xml"
printf '\000'
'''
    skip_media_roots = locations is None and args.defer_media and not args.watch_source
    if skip_media_roots:
        script += "printf '\\000\\000'\n"
    else:
        script += r'''for path in /data/media/0/Android/data/com.tencent.mm/MicroMsg /data/media/0/tencent/MicroMsg; do
  if [ "$(stat -c '%F' "$path" 2>/dev/null || true)" = directory ]; then readlink -f "$path" || true; fi
  printf '\000'
done
'''
    snapshot = docker_read('sh', '-c', script)
    require(len(snapshot) <= 256 * 1024, 'source_snapshot_too_large')
    fields = snapshot.split(b'\0')
    require(len(fields) == 6 and not fields[-1], 'source_snapshot_invalid')
    paths = fields[0].decode().splitlines()
    require(len(paths) == 1, 'ambiguous_account')
    identifier = account_identifier(fields[1:3])
    canonical = paths[0]
    require(canonical.startswith('/data/') and '..' not in canonical.split('/'), 'source_path_invalid')
    roots = [posixpath.dirname(canonical)]
    for raw in fields[3:5]:
        real = raw.decode().strip()
        if real:
            require(real.startswith('/data/') and '..' not in real.split('/'), 'source_path_invalid')
            roots.append(real)
    if pid is None:
        result = subprocess.run(['sudo', '-n', 'docker', 'inspect', '--format', '{{.State.Pid}}', CONTAINER], capture_output=True, text=True, timeout=20)
        pid = result.stdout.strip()
        require(result.returncode == 0 and pid.isdigit() and int(pid) > 0, 'container_unavailable')
    request = {'pid': pid, 'roots': roots, 'namespace': os.readlink('/proc/self/ns/mnt'), 'key': hashlib.md5(('1234567890ABCDEF' + identifier).encode()).hexdigest()[:7], 'accountFingerprint': hashlib.sha256(('wechat-tablet-account-v1:' + identifier).encode()).hexdigest(), 'afterId': args.after_id, 'limit': args.limit, 'ids': args.ids, 'includeMedia': args.include_media, 'deferMedia': args.defer_media, 'watchSource': args.watch_source}
    if locations is not None:
        require(re.fullmatch(r'/data/(?:data|user/[0-9]+)/com\.tencent\.mm/MicroMsg/[^/]+/EnMicroMsg\.db', canonical), 'source_path_invalid')
        started = process_start(pid)
        require(started is not None, 'container_unavailable')
        locations.update(pid=pid, processStart=started, appRoot=canonical.rsplit('/MicroMsg/', 1)[0], mediaRoots=roots[1:])
        request.update(appRoot=locations['appRoot'], roots=[] if args.defer_media else roots[1:])
    return request


def read_once(args, locations=None):
    request = build_request(args, locations)
    command = ['sudo', '-n', 'unshare', '--mount', '--propagation', 'private', '--', 'python3', str(Path(__file__).resolve()), '--private-child']
    if args.watch_source:
        with subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            process.stdin.write(json.dumps(request))
            process.stdin.close()
            require(process.wait() == 0, 'source_watch_failed')
        return None
    result = subprocess.run(command, input=json.dumps(request), text=True, capture_output=True, timeout=120)
    if result.returncode:
        try:
            code = json.loads(result.stderr)['error']
        except (ValueError, KeyError, TypeError):
            code = 'source_reader_failed'
        raise RuntimeError(code if isinstance(code, str) and re.fullmatch('[a-z_]+', code) else 'source_reader_failed')
    return json.loads(result.stdout)


def location_file():
    state = Path.home() / '.local/share/aurora-tablet-ui'
    if state.is_symlink() or not state.is_dir() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        return None
    return state / 'source-locations.json'


def load_locations():
    path = location_file()
    if path is None:
        return {}
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
                    and info.st_size <= 4096, 'location_hint_invalid')
            value = json.loads(stream.read(4097))
        require(isinstance(value, dict) and set(value) == {'pid', 'processStart', 'appRoot', 'mediaRoots'}, 'location_hint_invalid')
        require(all(isinstance(value[name], str) and value[name].isdigit() for name in ('pid', 'processStart'))
                and re.fullmatch(r'/data/(?:data|user/[0-9]+)/com\.tencent\.mm', value['appRoot'])
                and isinstance(value['mediaRoots'], list) and len(value['mediaRoots']) <= 2
                and all(isinstance(root, str) and root.startswith('/data/') and '..' not in root.split('/')
                        for root in value['mediaRoots']), 'location_hint_invalid')
        return value
    except (OSError, RuntimeError, ValueError, TypeError):
        return {}


def save_locations(locations):
    # Persist public location hints only so UI checks and reconnects avoid cold
    # Android discovery. Fresh native account checks remain mandatory per query.
    path = location_file()
    if path is None:
        return
    try:
        if not locations:
            path.unlink(missing_ok=True)
            return
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='source-locations-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(locations).encode())
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        pass


def serve(read=None, input_stream=None, output_stream=None):
    """Private SSH pipe only: each request rebuilds and validates the source."""
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout
    locations = {} if read else load_locations()
    while raw := input_stream.readline(16385):
        request_id = None
        try:
            require(len(raw) <= 16384 and raw.endswith(b'\n'), 'invalid_reader_request')
            value = json.loads(raw)
            require(isinstance(value, dict) and set(value) == {'requestId', 'args'}, 'invalid_reader_request')
            request_id = value['requestId']
            require(type(request_id) is int and request_id > 0, 'invalid_reader_request')
            values = value['args']
            require(isinstance(values, dict) and set(values) == {'afterId', 'limit', 'ids', 'includeMedia', 'deferMedia'}, 'invalid_reader_request')
            require(isinstance(values['ids'], list) and type(values['includeMedia']) is bool
                    and type(values['deferMedia']) is bool, 'invalid_reader_request')
            selection(values['afterId'], values['limit'], values['ids'])
            args = argparse.Namespace(after_id=values['afterId'], limit=values['limit'], ids=values['ids'],
                                      include_media=values['includeMedia'], defer_media=values['deferMedia'], watch_source=False)
            previous = dict(locations)
            response = {'requestId': request_id, 'batch': read(args) if read else read_once(args, locations)}
            if read is None and locations != previous:
                save_locations(locations)
        except Exception as error:
            locations.clear()
            if read is None:
                save_locations(locations)
            code = str(error) if isinstance(error, RuntimeError) and re.fullmatch('[a-z_]+', str(error)) else 'source_reader_unavailable'
            response = {'requestId': request_id, 'error': code}
        output_stream.write(json.dumps(response, ensure_ascii=False, separators=(',', ':')) + '\n')
        output_stream.flush()
        if len(raw) > 16384 or not raw.endswith(b'\n'):
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--after-id', type=int, default=0)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--ids', default='')
    parser.add_argument('--include-media', action='store_true')
    parser.add_argument('--defer-media', action='store_true')
    parser.add_argument('--watch-source', action='store_true')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--private-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    require(not args.serve or not (args.private_child or args.watch_source), 'invalid_reader_mode')
    if args.serve:
        serve()
        return
    if args.private_child:
        request = json.load(sys.stdin)
        result = private_child(request)
        if request.get('watchSource', False):
            return
    else:
        require(not args.ids or re.fullmatch(r'[1-9][0-9]*(,[1-9][0-9]*)*', args.ids), 'invalid_ids')
        args.ids = [int(value) for value in args.ids.split(',')] if args.ids else []
        selection(args.after_id, args.limit, args.ids)
        result = read_once(args)
        if args.watch_source:
            return
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error': str(error) if isinstance(error, RuntimeError) else 'source_reader_unavailable'}), file=sys.stderr)
        sys.exit(1)
