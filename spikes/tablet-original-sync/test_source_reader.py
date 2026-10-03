"""Synthetic fixtures only; no account, credentials, or real message data."""
import hashlib
import ctypes
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

import source_reader as reader


class SourceReaderTests(unittest.TestCase):
    def emoji_crypto(self):
        for name in ('libcrypto.so.3', 'libcrypto.so.1.1', '/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib'):
            try:
                return ctypes.CDLL(name)
            except OSError:
                continue
        self.skipTest('local libcrypto unavailable; guest Linux check remains required')

    def test_emoji_cache_key_is_unique_hex_ascii_and_never_synthesized(self):
        fixture = '0123456789abcdef' * 2
        self.assertEqual(reader.emoji_cache_key([{'md5': fixture}]), fixture[:16].encode())
        for rows in ([], [{'md5': fixture}] * 2, [{'md5': ''}], [{'md5': 'z' * 32}],
                     [{'md5': None}], [{'other': fixture}], [{'md5': fixture[:-1]}]):
            self.assertIsNone(reader.emoji_cache_key(rows))

    def test_emoji_key_query_is_bounded_full_media_only_and_optional_schema(self):
        sql = reader.queries(0, 20, [3], emoji_cache_key=True)
        self.assertIn('SELECT md5 FROM EmojiInfo WHERE catalog=153 LIMIT 2;', sql)
        with self.database() as db:
            db.execute('ALTER TABLE EmojiInfo ADD COLUMN catalog INTEGER')
            db.executemany('INSERT INTO EmojiInfo(md5,catalog) VALUES(?,153)', [('0' * 32,), ('1' * 32,), ('2' * 32,)])
            sections = [[dict(row) for row in db.execute(line)] for line in sql.splitlines() if not line.startswith('.print')]
        self.assertEqual(len(sections[8]), 2)
        self.assertNotIn('catalog=153', reader.queries(0, 20, [], emoji_cache_key=True, defer_media=True))
        schema = SimpleNamespace(returncode=0, stdout='{"section":2}\n[{"name":"md5"},{"name":"catalog"}]')
        result = SimpleNamespace(returncode=0, stdout='{"section":8}\n[{"md5":"' + '0' * 32 + '"}]')
        with patch.object(reader.os.path, 'exists', return_value=False), \
                patch.object(reader.subprocess, 'run', side_effect=[schema, result]) as run:
            private = reader.query_store('/fixture', 'synthetic-key', 0, 20, [])
        self.assertEqual(private[8], [{'md5': '0' * 32}])
        self.assertIn('PRAGMA table_info(EmojiInfo)', run.call_args_list[0].kwargs['input'])
        self.assertIn('catalog=153 LIMIT 2', run.call_args_list[1].kwargs['input'])
        self.assertIn('PRAGMA query_only=ON', run.call_args_list[1].kwargs['input'])

    def test_emoji_cache_libcrypto_exact_prefix_suffix_and_block_boundaries(self):
        library = self.emoji_crypto()
        key = b'0123456789abcdef'
        block = b'GIF89a' + bytes(10)
        # Independent OpenSSL enc AES-128-ECB fixture, no padding.
        encrypted = bytes.fromhex('6dd625d93261fe2b5eecbb835ebe3726')
        suffix = b'unencrypted-suffix!'
        with patch.object(reader.ctypes, 'CDLL', return_value=library):
            self.assertEqual(reader.decode_emoji_cache(encrypted * 64 + suffix, key), block * 64 + suffix)
            self.assertEqual(reader.decode_emoji_cache(encrypted, key), block)
            for malformed in (b'', encrypted[:-1], encrypted + b'!'):
                self.assertIsNone(reader.decode_emoji_cache(malformed, key))
            self.assertIsNone(reader.decode_emoji_cache(encrypted, key[:-1]))
        with patch.object(reader.ctypes, 'CDLL', side_effect=OSError('unavailable')):
            self.assertIsNone(reader.decode_emoji_cache(encrypted, key))

    def test_encrypted_record_emoji_requires_primary_checksum_and_does_not_leak_key(self):
        library = self.emoji_crypto()
        stored_key = '0123456789abcdef' * 2
        key = stored_key[:16].encode()
        plain = (b'GIF89a' + bytes(10)) * 64 + b'unencrypted-suffix!'
        encrypted = bytes.fromhex('6dd625d93261fe2b5eecbb835ebe3726') * 64 + b'unencrypted-suffix!'
        digest = hashlib.md5(plain).hexdigest()
        record = '<recordinfo><dataitem datatype="37" dataid="fixture"><emojiitem md5="' + digest + '" /></dataitem></recordinfo>'
        row = {'msgId': 9, 'type': 49, 'content': '<msg><appmsg><type>19</type><recorditem>' + escape(record) + '</recorditem></appmsg></msg>'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, digest)
            path.write_bytes(encrypted)
            sections = [[row], [], [], [], [], [], [], [], [{'md5': stored_key}]]
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)), \
                    patch.object(reader.ctypes, 'CDLL', return_value=library):
                result = reader.assemble_messages(sections, [('/allowed', directory)], True)[0]
                self.assertEqual(result['media'][0]['state'], 'ready')
                self.assertEqual(result['media'][0]['sha256'], hashlib.sha256(plain).hexdigest())
                self.assertEqual(result['media'][0]['mimeType'], 'image/gif')
                self.assertNotIn(stored_key, json.dumps(result))
                self.assertNotIn(key.decode(), json.dumps(result))
                self.assertEqual(path.read_bytes(), encrypted)
                for key_rows in ([], [{'md5': stored_key}] * 2, [{'md5': 'z' * 32}]):
                    private_sections = [[dict(row)], [], [], [], [], [], [], [], key_rows]
                    rejected = reader.assemble_messages(private_sections, [('/allowed', directory)], True)[0]
                    self.assertEqual(rejected['media'][0]['state'], 'pending')
                spec = {'path': digest, 'kind': 'sticker', 'byteLength': len(plain), 'md5': digest}
                index = reader.file_index([('/allowed', directory)])
                for bad_key in (None, b'fedcba9876543210', b'invalid'):
                    rejected = reader.resolve_media(spec, [('/allowed', directory)], index, True, [10000], emoji_cache_key=bad_key)
                    self.assertEqual(rejected['state'], 'pending')
                for overrides in ({'md5': '0' * 32}, {'byteLength': len(plain) + 1}, {'kind': 'image'}):
                    rejected = reader.resolve_media(spec | overrides, [('/allowed', directory)], index, True, [10000], emoji_cache_key=key)
                    self.assertEqual(rejected['state'], 'pending')
                path.write_bytes(plain)
                with patch.object(reader, 'decode_emoji_cache', side_effect=AssertionError('plain cache decoded')):
                    available = reader.resolve_media(spec, [('/allowed', directory)], index, True, [10000], emoji_cache_key=key)
                self.assertEqual(available['state'], 'ready')
            self.assertEqual(path.read_bytes(), plain)

    def test_decoded_emoji_unsupported_format_remains_pending(self):
        data = b'encrypted-data!!'
        decoded = b'wxgf-not-image!!'
        spec = {'path': 'fixture', 'kind': 'sticker', 'byteLength': len(decoded), 'md5': hashlib.md5(decoded).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'fixture').write_bytes(data)
            views = [('/allowed', directory)]
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)), \
                    patch.object(reader, 'decode_emoji_cache', return_value=decoded):
                result = reader.resolve_media(spec, views, reader.file_index(views), True, [1000], emoji_cache_key=b'0123456789abcdef')
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['reason'], 'unsupported_original_format')

    def contact_sections(self, db, contact_alias=False):
        output = []
        for line in reader.queries(99, 1, [], defer_media=True, contact_snapshot=True, contact_alias=contact_alias).splitlines():
            if line.startswith('.print '):
                output.append(line[len(".print '"):-1])
            elif line:
                output.append(json.dumps([dict(row) for row in db.execute(line)]))
        return reader.decode_sections('\n'.join(output))

    def test_contacts_native_predicate_is_independent_of_message_cursor(self):
        with self.database() as db:
            db.execute('DELETE FROM rcontact')
            for column in ('type', 'verifyFlag', 'deleteFlag'):
                db.execute('ALTER TABLE rcontact ADD COLUMN ' + column + ' INTEGER DEFAULT 0')
            db.execute('CREATE TABLE userinfo(id INTEGER,value TEXT)')
            db.execute("INSERT INTO userinfo VALUES(2,'self')")
            rows = [('friend', 'Remark', 'Nickname', 3, 0, 0),
                    ('pinned', '', 'Pinned', 2051, 0, 0),
                    ('username-fallback', '', '', 1, 0, 0),
                    ('self', 'Self', '', 3, 0, 0),
                    ('nonfriend', 'Stranger', '', 4, 0, 0),
                    ('service-bit', 'System', '', 33, 0, 0),
                    ('excluded-bit', 'Excluded', '', 9, 0, 0),
                    ('verified', 'Official', '', 3, 8, 0),
                    ('deleted', 'Deleted', '', 3, 0, 1),
                    ('room@chatroom', 'Group', '', 3, 0, 0),
                    ('external@openim', 'OpenIM', '', 3, 0, 0)]
            rows += [(username, 'Service', '', 3, 0, 0) for username in reader.CONTACT_SERVICES]
            db.executemany('INSERT INTO rcontact VALUES(?,?,?,?,?,?)', rows)
            sections = self.contact_sections(db)
        self.assertEqual(sections[0], [])
        self.assertEqual(reader.contact_snapshot(sections), {'state': 'ready', 'contacts': [
            {'name': 'Remark', 'conversationId': 'friend', 'alias': ''},
            {'name': 'Pinned', 'conversationId': 'pinned', 'alias': ''},
            {'name': 'username-fallback', 'conversationId': 'username-fallback', 'alias': ''}]})

    def test_contact_native_alias_survives_duplicate_display_names(self):
        with self.database() as db:
            db.execute('DELETE FROM rcontact')
            for column in ('type', 'verifyFlag', 'deleteFlag'):
                db.execute('ALTER TABLE rcontact ADD COLUMN ' + column + ' INTEGER DEFAULT 0')
            db.execute('ALTER TABLE rcontact ADD COLUMN alias TEXT')
            db.execute('CREATE TABLE userinfo(id INTEGER,value TEXT)')
            db.execute("INSERT INTO userinfo VALUES(2,'self')")
            db.executemany('INSERT INTO rcontact VALUES(?,?,?,?,?,?,?)', [
                ('friend_a', 'Same name', '', 1, 0, 0, 'alias_a'),
                ('friend_b', 'Same name', '', 1, 0, 0, 'alias_b')])
            snapshot = reader.contact_snapshot(self.contact_sections(db, contact_alias=True))
        self.assertEqual(snapshot, {'state': 'ready', 'contacts': [
            {'name': 'Same name', 'conversationId': 'friend_a', 'alias': 'alias_a'},
            {'name': 'Same name', 'conversationId': 'friend_b', 'alias': 'alias_b'}]})

    def test_contacts_incomplete_or_ambiguous_snapshot_never_becomes_empty_success(self):
        def sections(rows, own=None):
            return [[] for _ in range(9)] + [rows, [{'username': 'self'}] if own is None else own]
        valid = {'username': 'fixture', 'name': 'Friend'}
        self.assertEqual(reader.contact_snapshot(sections([])), {'state': 'ready', 'contacts': []})
        cases = [([[] for _ in range(8)], 'contacts_schema_unsupported'),
                 (sections([valid], []), 'contacts_self_unavailable'),
                 (sections([valid] * 10001), 'contacts_too_many'),
                 (sections([valid, valid]), 'contacts_identity_ambiguous'),
                 (sections([{'username': 'self', 'name': 'Self'}]), 'contacts_identity_ambiguous'),
                 (sections([{'username': 'fixture', 'name': ' Leading'}]), 'contacts_name_invalid'),
                 (sections([{'username': 'fixture', 'name': '中' * 171}]), 'contacts_name_invalid')]
        for value, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(reader.contact_snapshot(value), {'state': 'unavailable', 'reason': reason})

        duplicate_names = reader.contact_snapshot(sections([valid, {'username': 'other', 'name': 'Friend', 'alias': 'other_alias'}]))
        self.assertEqual(duplicate_names['state'], 'ready')
        self.assertEqual(len(duplicate_names['contacts']), 2)
        self.assertIsNone(reader.contact_for_send(duplicate_names, 'fixture'))
        self.assertEqual(reader.contact_for_send(duplicate_names, 'other')['alias'], 'other_alias')
        duplicate_names['contacts'][0]['alias'] = 'other_alias'
        self.assertIsNone(reader.contact_for_send(duplicate_names, 'other'))

    def test_contacts_schema_detection_only_enables_complete_fast_read_schema(self):
        required = ('username', 'conRemark', 'nickname', 'type', 'verifyFlag', 'deleteFlag')
        for columns in (required, required[:-1]):
            schema = SimpleNamespace(returncode=0, stdout='{"section":0}\n' + json.dumps([{'name': name} for name in columns])
                                     + '\n{"section":3}\n[{"name":"id"},{"name":"value"}]')
            result = SimpleNamespace(returncode=0, stdout='{"section":0}\n[]')
            with patch.object(reader.os.path, 'exists', return_value=False), \
                    patch.object(reader.subprocess, 'run', side_effect=[schema, result]) as run:
                reader.query_store('/fixture', 'synthetic-key', 99, 1, [], defer_media=True)
            self.assertIn('PRAGMA table_info(userinfo)', run.call_args_list[0].kwargs['input'])
            self.assertEqual('ORDER BY username LIMIT 10001' in run.call_args_list[1].kwargs['input'], columns == required)

    def database(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript('''
CREATE TABLE message(msgId INTEGER PRIMARY KEY,msgSvrId INTEGER,type INTEGER,createTime INTEGER,isSend INTEGER,talker TEXT,content TEXT,imgPath TEXT);
CREATE TABLE rcontact(username TEXT,conRemark TEXT,nickname TEXT);
CREATE TABLE ImgInfo2(msglocalid INTEGER,msgSvrId INTEGER,bigImgPath TEXT,origImgMD5 TEXT,totalLen INTEGER,offset INTEGER,iscomplete INTEGER);
CREATE TABLE voiceinfo(MsgLocalId INTEGER,FileName TEXT,TotalLen INTEGER,FileNowSize INTEGER,NetOffset INTEGER);
CREATE TABLE videoinfo2(msglocalid INTEGER,filename TEXT,video_path TEXT,totallen INTEGER,filenowsize INTEGER,netoffset INTEGER);
CREATE TABLE appattach(msgInfoId INTEGER,fileFullPath TEXT,totalLen INTEGER,offset INTEGER);
CREATE TABLE RecordMessageInfo(localId INTEGER,msgId INTEGER,oriMsgId INTEGER);
CREATE TABLE RecordCDNInfo(recordLocalId INTEGER,dataId TEXT,path TEXT,totalLen INTEGER,offset INTEGER,isThumb INTEGER);
CREATE TABLE EmojiInfo(md5 TEXT,size INTEGER);
INSERT INTO message VALUES(1,9007199254740993,1,100,0,'room@chatroom','sender:'||char(10)||'fixture text','');
INSERT INTO message VALUES(2,9007199254740994,34,101,0,'sender','fixture voice','voice-fixture');
INSERT INTO message VALUES(3,9007199254740995,1,102,1,'sender','outgoing','');
INSERT INTO rcontact VALUES('room@chatroom','','Fixture room');
INSERT INTO rcontact VALUES('sender','Fixture sender','Other name');
INSERT INTO voiceinfo VALUES(2,'voice-fixture',5543,5543,5543);
INSERT INTO ImgInfo2 VALUES(0,9007199254740993,'original.jpg','',100,100,1);
''')
        return db

    def execute_queries(self, after=0, limit=20, ids=None):
        with self.database() as db:
            return [[dict(row) for row in db.execute(line)] for line in reader.queries(after, limit, ids or []).splitlines() if not line.startswith('.print')]

    def test_bounded_scan_and_names_preserve_server_id_precision(self):
        sections = self.execute_queries(limit=1)
        self.assertEqual(len(sections[0]), 1)
        self.assertEqual(sections[0][0]['msgSvrId'], '9007199254740993')
        self.assertEqual(sections[0][0]['conversationName'], 'Fixture room')
        self.assertEqual(sections[0][0]['senderName'], 'Fixture sender')
        self.assertEqual(sections[2], [])
        self.assertEqual(sections[1][0]['msgId'], 1)

    def test_deferred_media_preserves_text_identity_without_media_work(self):
        expected = reader.assemble_messages(self.execute_queries(ids=[3]), [], False)[0]
        sections = self.execute_queries()
        sections[0].append({'msgId': 4, 'type': 10000, 'talker': 'fixture-system', 'content': 'fixture notice'})
        with patch.object(reader, 'file_index', side_effect=AssertionError('media index invoked')), patch.object(reader, 'resolve_media', side_effect=AssertionError('media resolved')), patch.object(reader, 'app_message_media', side_effect=AssertionError('media XML parsed')):
            messages = reader.assemble_messages(sections, [('/allowed', '/fixture')], True, defer_media=True)
        self.assertEqual(messages[2], expected)
        self.assertEqual([message['msgId'] for message in messages], [1, 2, 3, 4])
        self.assertEqual(messages[0]['replyIdentity'], {'username': 'room@chatroom'})
        self.assertTrue(all(message['media'] == [] for message in messages))
        self.assertTrue(all(message.get('mediaDeferred') is True for message in messages if message['type'] != 1))
        self.assertTrue(all('mediaDeferred' not in message for message in messages if message['type'] == 1))
        self.assertEqual(sections[7], [{'sourceMaxId': 3}])

    def test_deferred_queries_keep_message_and_watermark_without_media_tables(self):
        sql = reader.queries(0, 20, [], defer_media=True)
        self.assertEqual(sql.count('.print '), 8)
        for table in ('ImgInfo2', 'voiceinfo', 'videoinfo2', 'appattach', 'RecordCDNInfo', 'EmojiInfo'):
            self.assertNotIn(table, sql)
        with self.database() as db:
            statements = [line for line in sql.splitlines() if line and not line.startswith('.print')]
            rows = [[dict(row) for row in db.execute(statement)] for statement in statements]
        self.assertEqual([row['msgId'] for row in rows[0]], [1, 2, 3])
        self.assertEqual(rows[1], [{'sourceMaxId': 3}])

    def test_only_deferred_reads_skip_media_roots_and_keep_fingerprint(self):
        def docker(*arguments, optional=False):
            fields = [b'/data/user/0/com.tencent.mm/MicroMsg/fixture/EnMicroMsg.db\n',
                      b'<map><int name="default_uin" value="42" /></map>', b'<map/>', b'', b'', b'']
            if 'for path in ' in arguments[-1]:
                fields[3:5] = [b'/data/media/0/Android/data/com.tencent.mm/MicroMsg\n', b'/data/media/0/tencent/MicroMsg\n']
            return b'\0'.join(fields)

        requests = []
        for defer, watch in ((False, False), (True, False), (False, True), (True, True)):
            args = SimpleNamespace(after_id=7, limit=20, ids=[], include_media=False, defer_media=defer, watch_source=watch)
            with patch.object(reader, 'docker_read', side_effect=docker) as reads, patch.object(reader.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='123')), patch.object(reader.os, 'readlink', return_value='fixture-namespace'):
                request = reader.build_request(args)
            requests.append(request)
            self.assertEqual(len(request['roots']), 1 if defer and not watch else 3)
            self.assertEqual(reads.call_count, 1)
            self.assertIn('-maxdepth 2', reads.call_args.args[-1])
            self.assertIn('"$root/MicroMsg"', reads.call_args.args[-1])
        self.assertEqual(len({request['accountFingerprint'] for request in requests}), 1)
        self.assertTrue(requests[1]['deferMedia'])
        self.assertTrue(requests[2]['watchSource'])

    def test_snapshot_rejects_multiple_accounts_inconsistent_identifiers_and_bad_paths(self):
        fields = [b'/data/user/0/com.tencent.mm/MicroMsg/fixture/EnMicroMsg.db\n',
                  b'<map><int name="default_uin" value="42" /></map>', b'<map/>', b'', b'', b'']
        args = SimpleNamespace(after_id=0, limit=1, ids=[], include_media=False, defer_media=True, watch_source=False)
        for index, value, error in [(0, fields[0] + fields[0], 'ambiguous_account'),
                                    (2, b'<map><int name="last_login_uin" value="43" /></map>', 'ambiguous_account_identifier'),
                                    (0, b'/tmp/EnMicroMsg.db', 'source_path_invalid'),
                                    (3, b'/data/../private', 'source_path_invalid')]:
            changed = list(fields)
            changed[index] = value
            with self.subTest(index=index), patch.object(reader, 'docker_read', return_value=b'\0'.join(changed)):
                with self.assertRaisesRegex(RuntimeError, error):
                    reader.build_request(args)
        with patch.object(reader, 'docker_read', return_value=b'bad snapshot'):
            with self.assertRaisesRegex(RuntimeError, 'source_snapshot_invalid'):
                reader.build_request(args)

    def test_resident_cache_keeps_only_locations_and_refreshes_on_pid_change(self):
        fields = [b'/data/user/0/com.tencent.mm/MicroMsg/fixture/EnMicroMsg.db\n',
                  b'<map><int name="default_uin" value="42" /></map>', b'<map/>', b'/data/media/fixture', b'', b'']
        args = SimpleNamespace(after_id=0, limit=1, ids=[], include_media=False, defer_media=True, watch_source=False)
        locations = {}
        with patch.object(reader, 'docker_read', return_value=b'\0'.join(fields)) as docker, patch.object(reader.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0, stdout=pid) for pid in ('123', '456')]) as inspect, patch.object(reader, 'process_start', side_effect=['111', '111', '222', '222']), patch.object(reader.os, 'readlink', return_value='namespace'):
            cold = reader.build_request(args, locations)
            warm = reader.build_request(args, locations)
            refreshed = reader.build_request(args, locations)
        self.assertEqual(docker.call_count, 2)
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(set(locations), {'pid', 'processStart', 'appRoot', 'mediaRoots'})
        self.assertEqual(cold['roots'], [])
        self.assertNotIn('accountFingerprint', warm)
        self.assertNotIn('key', warm)
        self.assertEqual(refreshed['pid'], '456')

    def test_native_account_reads_current_preferences_and_rejects_symlinks_or_ambiguity(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            root = Path(temporary)
            prefs = root / 'shared_prefs'
            prefs.mkdir()
            auth = prefs / 'auth_info_key_prefs.xml'
            auth.write_text('<map><int name="default_uin" value="42" /></map>')
            (prefs / 'system_config_prefs.xml').write_text('<map/>')
            account = root / 'MicroMsg/fixture'
            account.mkdir(parents=True)
            database = account / 'EnMicroMsg.db'
            database.write_bytes(b'fixture')
            self.assertEqual(reader.native_account(temporary), ('MicroMsg/fixture', '42'))
            auth.write_text('<map><int name="default_uin" value="43" /></map>')
            self.assertEqual(reader.native_account(temporary)[1], '43')
            other = root / 'MicroMsg/other'
            other.mkdir()
            (other / 'EnMicroMsg.db').write_bytes(b'fixture')
            with self.assertRaisesRegex(RuntimeError, 'ambiguous_account'):
                reader.native_account(temporary)
            (other / 'EnMicroMsg.db').unlink()
            database.unlink()
            database.symlink_to(auth)
            with self.assertRaises(OSError):
                reader.native_account(temporary)
            database.unlink()
            database.write_bytes(b'fixture')
            auth.unlink()
            auth.symlink_to(prefs / 'system_config_prefs.xml')
            with self.assertRaises(OSError):
                reader.native_account(temporary)

    def test_private_location_hint_survives_reconnect_without_caching_identity(self):
        value = {'pid': '123', 'processStart': '456', 'appRoot': '/data/user/0/com.tencent.mm', 'mediaRoots': []}
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / '.local/share/aurora-tablet-ui'
            state.mkdir(parents=True, mode=0o700)
            with patch.object(reader.Path, 'home', return_value=Path(temporary)):
                reader.save_locations(value)
                path = state / 'source-locations.json'
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(reader.load_locations(), value)
                for changed in ({**value, 'key': 'secret'}, {**value, 'appRoot': '/data/../private'},
                                {**value, 'mediaRoots': ['/data/../private']}):
                    path.write_text(json.dumps(changed))
                    self.assertEqual(reader.load_locations(), {})
                path.unlink()
                other = state / 'other'
                other.write_text(json.dumps(value))
                path.symlink_to(other)
                self.assertEqual(reader.load_locations(), {})
                reader.save_locations({})
                self.assertFalse(path.is_symlink())
                self.assertTrue(other.exists())

    def test_private_native_account_is_fresh_and_checked_after_transaction(self):
        request = {'namespace': 'parent', 'pid': '123', 'roots': [], 'appRoot': '/data/user/0/com.tencent.mm',
                   'afterId': 0, 'limit': 1, 'ids': [], 'includeMedia': False, 'deferMedia': True}
        view = Mock()
        view.__enter__ = Mock(return_value='/private-view')
        view.__exit__ = Mock(return_value=False)
        sections = [[], [], [], [], [], [], [], [{'sourceMaxId': 0}]]
        with patch.object(reader.os, 'geteuid', return_value=0), patch.object(reader.os, 'readlink', return_value='child'), patch.object(reader, 'mount_rows', return_value=[]), patch.object(reader, 'guest_backing', return_value='/backing'), patch.object(reader, 'readonly_view', return_value=view), patch.object(reader, 'native_account', side_effect=[('MicroMsg/fixture', '42')] * 2 + [('MicroMsg/new', '43')] * 2 + [('MicroMsg/new', '43'), ('MicroMsg/other', '44')]), patch.object(reader, 'query_store', return_value=sections) as query:
            first = reader.private_child(request)
            second = reader.private_child(request)
            self.assertNotEqual(first['accountFingerprint'], second['accountFingerprint'])
            self.assertNotEqual(query.call_args_list[0].args[1], query.call_args_list[1].args[1])
            with self.assertRaisesRegex(RuntimeError, 'account_changed_during_read'):
                reader.private_child(request)

    def test_watch_private_child_mounts_once_without_store_transaction(self):
        request = {'namespace': 'parent', 'pid': '123', 'roots': ['/data/fixture'], 'watchSource': True}
        view = Mock()
        view.__enter__ = Mock(return_value='/private-view')
        view.__exit__ = Mock(return_value=False)
        with patch.object(reader.os, 'geteuid', return_value=0), patch.object(reader.os, 'readlink', return_value='child'), patch.object(reader, 'mount_rows', return_value=[]), patch.object(reader, 'guest_backing', return_value='/backing'), patch.object(reader, 'readonly_view', return_value=view) as mount, patch.object(reader, 'watch_source') as watch, patch.object(reader, 'query_store', side_effect=AssertionError('store queried')):
            self.assertIsNone(reader.private_child(request))
        self.assertEqual(mount.call_count, 1)
        watch.assert_called_once_with('/private-view', media_roots=[])

    def test_watch_private_child_mounts_media_roots_readonly_without_store_transaction(self):
        request = {'namespace': 'parent', 'pid': '123', 'roots': ['/data/fixture', '/data/media-a', '/data/media-b'], 'watchSource': True}
        view = Mock()
        view.__enter__ = Mock(return_value='/private-view')
        view.__exit__ = Mock(return_value=False)
        with patch.object(reader.os, 'geteuid', return_value=0), patch.object(reader.os, 'readlink', return_value='child'), patch.object(reader, 'mount_rows', return_value=[]), patch.object(reader, 'guest_backing', return_value='/backing'), patch.object(reader, 'readonly_view', return_value=view) as mount, patch.object(reader, 'watch_source') as watch, patch.object(reader, 'query_store', side_effect=AssertionError('store queried')):
            self.assertIsNone(reader.private_child(request))
        self.assertEqual(mount.call_count, 3)
        watch.assert_called_once_with('/private-view', media_roots=['/private-view', '/private-view'])

    def test_watch_events_filter_names_overflow_and_invalidation(self):
        def event(mask, name=b''):
            name = name + b'\0' if name else b''
            return struct.pack('iIII', 1, mask, 0, len(name)) + name

        for name in (b'EnMicroMsg.db', b'EnMicroMsg.db-wal'):
            self.assertTrue(reader.SourceWatcher.changed(event(0x2, name)))
        self.assertFalse(reader.SourceWatcher.changed(event(0x2, b'EnMicroMsg.db-shm') + event(0x2, b'unrelated')))
        self.assertTrue(reader.SourceWatcher.changed(event(reader.SourceWatcher.OVERFLOW)))
        for mask in (0x400, 0x800, 0x2000, 0x8000):
            with self.assertRaisesRegex(RuntimeError, 'source_watch_invalidated'):
                reader.SourceWatcher.changed(event(mask))
        with self.assertRaisesRegex(RuntimeError, 'source_watch_invalid'):
            reader.SourceWatcher.changed(b'invalid')

    def test_watch_initial_and_idle_output_are_fixed_flushed_json(self):
        watcher = Mock()
        watcher.wait_flags.side_effect = [(False, False), RuntimeError('source_watch_invalidated')]
        output = io.StringIO()
        with patch.object(reader, 'SourceWatcher', return_value=watcher), patch.object(reader.sys, 'stdout', output), patch.object(reader, 'query_store', side_effect=AssertionError('idle watch queried store')):
            with self.assertRaisesRegex(RuntimeError, 'source_watch_invalidated'):
                reader.watch_source('/private-fixture')
        self.assertEqual(output.getvalue(), '{"sourceChanged":true,"mediaChanged":true}\n{"sourceChanged":false,"mediaChanged":false}\n')
        self.assertEqual(watcher.wait_flags.call_args_list[0].args, (15,))
        watcher.close.assert_called_once_with()

    def test_recursive_watch_flags_distinguish_media_db_and_directory_races(self):
        watcher = reader.SourceWatcher.__new__(reader.SourceWatcher)
        watcher.recursive = True
        watcher.source_watch = 1
        watcher.watches = {1: ('/db-root', ()), 2: ('/media-root', ('cache',))}
        watcher.add_tree = Mock()

        def event(watch, mask, name=b''):
            name = name + b'\0' if name else b''
            return struct.pack('iIII', watch, mask, 0, len(name)) + name

        for name in (b'EnMicroMsg.db', b'EnMicroMsg.db-wal'):
            self.assertEqual(watcher.flags(event(1, 0x2, name)), (True, True))
        self.assertEqual(watcher.flags(event(1, 0x2, b'EnMicroMsg.db-shm')), (False, False))
        for name in (b'image.jpg', b'voice.amr', b'clip.mp4', b'file.pdf', b'record-original'):
            for mask in (0x2, 0x8, 0x80, 0x100):
                self.assertEqual(watcher.flags(event(2, mask, name)), (False, True))
        self.assertEqual(watcher.flags(event(2, 0x100 | reader.SourceWatcher.IS_DIRECTORY, b'new')), (False, True))
        watcher.add_tree.assert_called_once_with('/media-root', ('cache', 'new'))
        with self.assertRaisesRegex(RuntimeError, 'source_watch_overflow'):
            watcher.flags(event(-1, reader.SourceWatcher.OVERFLOW))
        for mask in (0x40, 0x200):
            with self.assertRaisesRegex(RuntimeError, 'source_watch_invalidated'):
                watcher.flags(event(2, mask | reader.SourceWatcher.IS_DIRECTORY, b'removed'))

    def test_recursive_registration_failure_closes_owned_descriptor(self):
        libc = Mock()
        libc.inotify_init1.return_value = 123
        with patch.object(reader.sys, 'platform', 'linux'), patch.object(reader.ctypes, 'CDLL', return_value=libc), patch.object(reader.SourceWatcher, 'add_tree', side_effect=RuntimeError('source_watch_unavailable')), patch.object(reader.os, 'close') as close:
            with self.assertRaisesRegex(RuntimeError, 'source_watch_unavailable'):
                reader.SourceWatcher('/fixture', media_roots=[])
        close.assert_called_once_with(123)

    def test_recursive_overflow_closes_watch_and_restarts_with_initial_flags(self):
        watcher = reader.SourceWatcher.__new__(reader.SourceWatcher)
        watcher.recursive = True
        overflow = struct.pack('iIII', -1, reader.SourceWatcher.OVERFLOW, 0, 0)
        failed, rebuilt = Mock(), Mock()
        failed.wait_flags.side_effect = lambda timeout: watcher.flags(overflow)
        rebuilt.wait_flags.side_effect = RuntimeError('source_watch_invalidated')
        output = io.StringIO()
        with patch.object(reader, 'SourceWatcher', side_effect=[failed, rebuilt]) as construct, patch.object(reader.sys, 'stdout', output):
            with self.assertRaisesRegex(RuntimeError, 'source_watch_overflow'):
                reader.watch_source('/fixture', ['/media'])
            with self.assertRaisesRegex(RuntimeError, 'source_watch_invalidated'):
                reader.watch_source('/fixture', ['/media'])
        failed.close.assert_called_once_with()
        rebuilt.close.assert_called_once_with()
        self.assertEqual(construct.call_count, 2)
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [{'sourceChanged': True, 'mediaChanged': True}] * 2)

    def test_watch_debounce_merges_db_and_media_flags(self):
        for following, expected in [((False, False), {'sourceChanged': False, 'mediaChanged': True}), ((True, True), {'sourceChanged': True, 'mediaChanged': True})]:
            watcher = Mock()
            watcher.wait_flags.side_effect = [(False, True), following, RuntimeError('source_watch_invalidated')]
            output = io.StringIO()
            with patch.object(reader, 'SourceWatcher', return_value=watcher), patch.object(reader.sys, 'stdout', output), patch.object(reader.time, 'monotonic', side_effect=[0, 0, 0, 0.2]):
                with self.assertRaisesRegex(RuntimeError, 'source_watch_invalidated'):
                    reader.watch_source('/private-fixture', ['/media-fixture'])
            self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [{'sourceChanged': True, 'mediaChanged': True}, expected])
            watcher.close.assert_called_once_with()

    def test_watch_wait_handles_overflow_eof_and_unrelated_timeout(self):
        watcher = reader.SourceWatcher.__new__(reader.SourceWatcher)
        watcher.descriptor = 123
        unrelated = b'EnMicroMsg.db-shm\0'
        event = struct.pack('iIII', 1, 0x2, 0, len(unrelated)) + unrelated
        with patch.object(reader.select, 'select', side_effect=[([123], [], []), ([], [], [])]), patch.object(reader.os, 'read', return_value=event):
            self.assertFalse(watcher.wait(15))
        with patch.object(reader.select, 'select', return_value=([123], [], [])), patch.object(reader.os, 'read', return_value=struct.pack('iIII', -1, reader.SourceWatcher.OVERFLOW, 0, 0)):
            self.assertTrue(watcher.wait(15))
        with patch.object(reader.select, 'select', return_value=([123], [], [])), patch.object(reader.os, 'read', return_value=b''):
            with self.assertRaisesRegex(RuntimeError, 'source_watch_closed'):
                watcher.wait(15)

    def test_targeted_rescan_ignores_after_cursor_and_joins_only_selected(self):
        sections = self.execute_queries(after=99, ids=[2])
        self.assertEqual([row['msgId'] for row in sections[0]], [2])
        self.assertEqual(sections[2][0]['TotalLen'], 5543)
        self.assertEqual(sections[7], [{'sourceMaxId': 3}])
        for args in [(-1, 20, []), (0, 101, []), (0, 20, [0]), (0, 20, list(range(1, 22)))]:
            with self.assertRaises(RuntimeError):
                reader.selection(*args)

    def test_contact_alias_projection_is_optional_and_group_has_no_alias(self):
        with self.database() as db:
            db.execute('ALTER TABLE rcontact ADD COLUMN alias TEXT')
            db.execute("UPDATE rcontact SET alias='fixture-unique-alias' WHERE username='sender'")
            sections = [[dict(row) for row in db.execute(line)] for line in reader.queries(0, 20, [], contact_alias=True).splitlines() if not line.startswith('.print')]
        messages = reader.assemble_messages(sections, [], False)
        self.assertEqual(messages[0]['replyIdentity'], {'username': 'room@chatroom'})
        self.assertEqual(messages[1]['replyIdentity'], {'username': 'sender', 'contact': {'username': 'sender', 'alias': 'fixture-unique-alias'}})
        self.assertNotIn('recipientAlias', messages[1])
        without_alias = reader.assemble_messages(self.execute_queries(ids=[3]), [], False)[0]
        self.assertEqual(without_alias['replyIdentity'], {'username': 'sender', 'contact': {'username': 'sender', 'alias': ''}})

    def test_absent_or_ambiguous_contact_cannot_supply_reply_identity(self):
        for statement in ["DELETE FROM rcontact WHERE username='sender'", "INSERT INTO rcontact VALUES('sender','Duplicate fixture','Duplicate fixture')"]:
            with self.database() as db:
                db.execute(statement)
                sections = [[dict(row) for row in db.execute(line)] for line in reader.queries(0, 20, [2]).splitlines() if not line.startswith('.print')]
            messages = reader.assemble_messages(sections, [], False)
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]['replyIdentity'], {'username': 'sender'})
            self.assertNotIn('recipientContactCount', messages[0])

    def test_unnamed_group_uses_label_without_replacing_stable_id(self):
        with self.database() as db:
            db.execute("UPDATE rcontact SET conRemark='',nickname='' WHERE username='room@chatroom'")
            sections = [[dict(row) for row in db.execute(line)] for line in reader.queries(0, 20, [1]).splitlines() if not line.startswith('.print')]
        message = reader.assemble_messages(sections, [], False)[0]
        self.assertEqual(message['conversationName'], '群聊')
        self.assertEqual(message['talker'], 'room@chatroom')
        self.assertEqual(message['replyIdentity'], {'username': 'room@chatroom'})

    def test_schema_probe_preserves_readonly_transactions_with_optional_alias(self):
        for columns, has_alias in [([{'name': 'username'}, {'name': 'alias'}], True), ([{'name': 'username'}], False)]:
            schema = SimpleNamespace(returncode=0, stdout='{"section":0}\n' + json.dumps(columns) + '\n{"section":1}\n[]')
            messages = SimpleNamespace(returncode=0, stdout=json.dumps({'section': 0}) + '\n[]')
            with patch.object(reader.os.path, 'exists', return_value=False), patch.object(reader.subprocess, 'run', side_effect=[schema, messages]) as run:
                self.assertEqual(reader.query_store('/fixture', 'synthetic-key', 0, 20, []), [[], [], [], [], [], [], [], []])
            for call in run.call_args_list:
                self.assertIn('-readonly', call.args[0])
                self.assertIn('PRAGMA query_only=ON;', call.kwargs['input'])
                self.assertIn('BEGIN;', call.kwargs['input'])
                self.assertTrue(call.kwargs['input'].endswith('COMMIT;\n'))
            self.assertIn('PRAGMA table_info(rcontact);', run.call_args_list[0].kwargs['input'])
            self.assertEqual("COALESCE(c.alias,'') AS recipientAlias" in run.call_args_list[1].kwargs['input'], has_alias)

    def test_empty_shell_results_keep_section_alignment(self):
        output = '\n'.join([json.dumps({'section': 0}), '[{"msgId":2}]', json.dumps({'section': 1}), json.dumps({'section': 2}), '[{"msgId":2}]', json.dumps({'section': 3}), json.dumps({'section': 4})])
        sections = reader.decode_sections(output)
        self.assertEqual(sections[1], [])
        self.assertEqual(sections[2], [{'msgId': 2}])

    def test_media_path_scope_and_no_thumbnail_substitution(self):
        views = [('/data/media/0/Android/data/com.tencent.mm/MicroMsg', '/fixture')]
        spec = {'path': '../../secret', 'kind': 'file'}
        self.assertEqual(reader.candidates_for(spec, views, {}), [])
        spec['path'] = '/etc/passwd'
        self.assertEqual(reader.candidates_for(spec, views, {}), [])
        spec['path'] = '/sdcard/Android/data/com.tencent.mm/MicroMsg/a.pdf'
        self.assertEqual(reader.candidates_for(spec, views, {})[0][1], '/fixture/a.pdf')
        spec.update(path='original.jpg', kind='image')
        self.assertEqual(reader.candidates_for(spec, views, {'thumbnail.jpg': [('x', 'y', 'z')]}), [])

    def test_original_silk_selected_by_size_and_magic(self):
        data = b'\x02#!SILK_V3' + b'x' * (5543 - 10)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'msg_voice-fixture.amr').write_bytes(data)
            (root / 'voice-fixture').write_bytes(b'not audio' + b'x' * (5543 - 9))
            views = [('/allowed', directory)]
            spec = reader.media_specs(self.execute_queries(ids=[2]))[2][0]
            spec['complete'] = False
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                result = reader.resolve_media(spec, views, reader.file_index(views), True, [32 * 1024 * 1024])
            self.assertEqual(result['state'], 'ready')
            self.assertEqual(result['byteLength'], 5543)
            self.assertEqual(result['mimeType'], 'audio/silk')
            self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())

    def test_nonreadonly_media_descriptor_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'original.pdf').write_bytes(b'%PDF-fixture')
            views = [('/allowed', directory)]
            spec = {'kind': 'file', 'path': 'original.pdf', 'byteLength': 12, 'complete': True}
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=0)):
                result = reader.resolve_media(spec, views, reader.file_index(views), True, [100])
            self.assertEqual(result['state'], 'pending')
            self.assertNotIn('dataBase64', result)

    def test_pending_and_oversize_media_are_explicit(self):
        spec = {'kind': 'video', 'path': 'missing', 'byteLength': 100, 'complete': False}
        self.assertEqual(reader.resolve_media(spec, [], {}, True, [100])['state'], 'pending')
        spec['byteLength'] = reader.MAX_FILE_BYTES + 1
        self.assertEqual(reader.resolve_media(spec, [], {}, True, [100])['state'], 'unsupported_size')

    def test_parent_symlink_cannot_escape_media_view(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'view').mkdir()
            (root / 'outside').mkdir()
            (root / 'outside' / 'fixture').write_bytes(b'fixture')
            (root / 'view' / 'link').symlink_to(root / 'outside', target_is_directory=True)
            with self.assertRaises(OSError):
                reader.open_media(str(root / 'view' / 'link' / 'fixture'), str(root / 'view'))

    def test_reader_transaction_keeps_snapshot_during_fixture_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'fixture.db')
            writer = sqlite3.connect(path)
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('CREATE TABLE message(msgId INTEGER PRIMARY KEY)')
            writer.execute('INSERT INTO message VALUES(1)')
            writer.commit()
            connection = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
            try:
                connection.execute('BEGIN')
                query = reader.selection(0, 20, [])
                self.assertEqual(connection.execute(query).fetchall(), [(1,)])
                writer.execute('INSERT INTO message VALUES(2)')
                writer.commit()
                self.assertEqual(connection.execute(query).fetchall(), [(1,)])
                connection.execute('COMMIT')
                self.assertEqual(connection.execute(query).fetchall(), [(1,), (2,)])
            finally:
                connection.close()
                writer.close()

    def test_file_xml_without_appattach_stays_pending(self):
        for size, state in [('123', 'pending'), ('invalid', 'pending'), (str(reader.MAX_FILE_BYTES + 1), 'unsupported_size')]:
            message = {'msgId': 8, 'type': 1090519089, 'content': 'sender:\n<msg><appmsg><type>6</type><appattach><totallen>' + size + '</totallen></appattach></appmsg></msg>'}
            result = reader.assemble_messages([[message], [], [], [], []], [], True)[0]
            self.assertEqual(len(result['media']), 1)
            self.assertEqual(result['media'][0]['kind'], 'file')
            self.assertEqual(result['media'][0]['state'], state)
            self.assertEqual(result['media'][0]['byteLength'], int(size) if size.isdigit() else 0)
            self.assertNotIn('dataBase64', result['media'][0])

    def test_later_appattach_row_replaces_unknown_path_and_becomes_ready(self):
        data = b'%PDF-fixture'
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'fixture.pdf').write_bytes(data)
            with self.database() as db:
                db.execute("UPDATE message SET type=49,content='<msg><appmsg><type>6</type><appattach><totallen>12</totallen></appattach></appmsg></msg>' WHERE msgId=3")
                db.execute("INSERT INTO appattach VALUES(3,'/allowed/fixture.pdf',0,0)")
                sections = [[dict(row) for row in db.execute(line)] for line in reader.queries(0, 20, [3]).splitlines() if not line.startswith('.print')]
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                result = reader.assemble_messages(sections, [('/allowed', directory)], True)[0]
            self.assertEqual(len(result['media']), 1)
            self.assertEqual(result['media'][0]['state'], 'ready')
            self.assertEqual(result['media'][0]['byteLength'], len(data))

    def test_record_xml_reports_original_attachments_not_link_thumbnails(self):
        record = '<recordinfo><datalist><dataitem datatype="1"><datadesc>text</datadesc></dataitem><dataitem datatype="2"><datasize>222</datasize><datafmt>jpg</datafmt></dataitem><dataitem datatype="8"><datasize>333</datasize><datafmt>pdf</datafmt></dataitem><dataitem datatype="5"><cdnthumburl>fixture-thumbnail</cdnthumburl></dataitem></datalist></recordinfo>'
        message = {'msgId': 9, 'type': 49, 'content': '<msg><appmsg><type>19</type><recorditem>' + escape(record) + '</recorditem></appmsg></msg>'}
        result = reader.assemble_messages([[message], [], [], [], []], [], True)[0]
        self.assertEqual(result['recordMediaCount'], 2)
        self.assertEqual(result['recordMediaState'], 'pending')
        self.assertEqual([item['kind'] for item in result['media']], ['image', 'file'])
        self.assertEqual([item['recordItemIndex'] for item in result['media']], [1, 2])
        self.assertTrue(all(item['state'] == 'pending' for item in result['media']))

    def test_record_original_uses_non_thumbnail_download_index(self):
        data = b'\xff\xd8\xff-original-fixture'
        record = '<recordinfo><datalist><dataitem datatype="2" dataid="fixture-data"><datasize>' + str(len(data)) + '</datasize><datafmt>jpg</datafmt></dataitem></datalist></recordinfo>'
        xml = '<msg><appmsg><type>19</type><recorditem>' + escape(record) + '</recorditem></appmsg></msg>'
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'original.bin').write_bytes(data)
            with self.database() as db:
                db.execute('UPDATE message SET type=49,content=? WHERE msgId=3', (xml,))
                db.execute('INSERT INTO RecordMessageInfo VALUES(7,3,-1)')
                db.execute("INSERT INTO RecordCDNInfo VALUES(7,'fixture-data','/allowed/thumb.jpg',10,10,1)")
                db.execute("INSERT INTO RecordCDNInfo VALUES(7,'fixture-data','/allowed/original.bin',?,?,0)", (len(data), len(data)))
                sections = [[dict(row) for row in db.execute(line)] for line in reader.queries(0, 20, [3]).splitlines() if not line.startswith('.print')]
            self.assertEqual(len(sections[5]), 1)
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                result = reader.assemble_messages(sections, [('/allowed', directory)], True)[0]
            self.assertEqual(result['recordMediaState'], 'ready')
            self.assertEqual(result['media'][0]['mimeType'], 'image/jpeg')
            self.assertEqual(result['media'][0]['sha256'], hashlib.sha256(data).hexdigest())

    def test_record_fullmd5_matches_original_without_download_index(self):
        data = b'\xff\xd8\xff-original-fixture'
        for expected, state in [(hashlib.md5(data).hexdigest(), 'ready'), ('0' * 32, 'pending')]:
            record = '<recordinfo><datalist><dataitem datatype="2" dataid="unrelated-id"><datasize>' + str(len(data)) + '</datasize><fullmd5>' + expected + '</fullmd5><datafmt>jpg</datafmt></dataitem></datalist></recordinfo>'
            message = {'msgId': 9, 'type': 49, 'content': '<msg><appmsg><type>19</type><recorditem>' + escape(record) + '</recorditem></appmsg></msg>'}
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, 'opaque-cache-file').write_bytes(data)
                Path(directory, 'thumbnail.jpg').write_bytes(b'\xff\xd8\xff-other-image-data')
                with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                    result = reader.assemble_messages([[message], [], [], [], [], []], [('/allowed', directory)], True)[0]
            self.assertEqual(result['recordMediaState'], state)
            self.assertEqual(result['media'][0]['state'], state)
            self.assertEqual('dataBase64' in result['media'][0], state == 'ready')
            self.assertNotIn('md5', result['media'][0])
            self.assertNotIn('dataId', result['media'][0])

    def test_record_emoji_uses_nested_primary_checksum_and_detects_actual_format(self):
        data = b'GIF89a-fixture-record-emoji'
        digest = hashlib.md5(data).hexdigest()
        for attribute in (False, True):
            emoji = ('<emojiitem md5="' + digest + '" />' if attribute else
                     '<emojiitem><md5>' + digest + '</md5><externmd5>' + '0' * 32 +
                     '</externmd5><cdnurlstring>private-reference</cdnurlstring></emojiitem>')
            record = '<recordinfo><dataitem datatype="37" dataid="emoji-id">' + emoji + '</dataitem></recordinfo>'
            row = {'msgId': 9, 'type': 49, 'content': '<msg><appmsg><type>19</type><recorditem>' + escape(record) + '</recorditem></appmsg></msg>'}
            with tempfile.TemporaryDirectory() as directory:
                cached = Path(directory, digest)
                cached.write_bytes(data)
                with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                    result = reader.assemble_messages([[row], [], [], [], [], []], [('/allowed', directory)], True)[0]
                self.assertEqual(result['media'][0]['kind'], 'sticker')
                self.assertEqual(result['media'][0]['mimeType'], 'image/gif')
                self.assertEqual(result['media'][0]['state'], 'ready')
                self.assertEqual(result['media'][0]['recordItemIndex'], 0)
                self.assertNotIn('private-reference', json.dumps(result['media']))
                cached.write_bytes(b'GIF89a-wrong-thumbnail')
                with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                    rejected = reader.assemble_messages([[row], [], [], [], [], []], [('/allowed', directory)], True)[0]
                self.assertEqual(rejected['media'][0]['state'], 'pending')

    def test_record_emoji_external_reference_alone_does_not_become_original(self):
        record = '<recordinfo><dataitem datatype="37"><emojiitem><externmd5>' + 'a' * 32 + '</externmd5></emojiitem></dataitem></recordinfo>'
        row = {'type': 49, 'content': '<msg><appmsg><type>19</type><recorditem>' + escape(record) + '</recorditem></appmsg></msg>'}
        _, specs = reader.app_message_media(row)
        self.assertEqual(specs[0]['kind'], 'sticker')
        self.assertEqual(specs[0]['md5'], '')

    def test_record_declared_format_never_changes_after_pending_metadata(self):
        data = b'GIF89a-fixture-format-mismatch'
        digest = hashlib.md5(data).hexdigest()
        record = '<recordinfo><dataitem datatype="2" dataid="image-id"><datafmt>jpg</datafmt><datasize>' + str(len(data)) + '</datasize><fullmd5>' + digest + '</fullmd5></dataitem></recordinfo>'
        row = {'msgId': 9, 'type': 49, 'content': '<msg><appmsg><type>19</type><recorditem>' + escape(record) + '</recorditem></appmsg></msg>'}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, digest).write_bytes(data)
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                media = reader.assemble_messages([[row], [], [], [], [], []], [('/allowed', directory)], True)[0]['media'][0]
        self.assertEqual(media['state'], 'pending')
        self.assertEqual(media['mimeType'], 'image/jpeg')
        self.assertEqual(media['reason'], 'declared_format_mismatch')

    def test_video_database_filename_recovers_unmapped_absolute_path(self):
        data = b'\x00\x00\x00\x18ftypfixture-video'
        row = {'msgId': 7, 'filename': 'fixture-clip', 'video_path': '/unmapped/fixture-clip.mp4', 'totallen': len(data), 'filenowsize': len(data), 'netoffset': len(data)}
        spec = reader.media_specs([[], [], [], [row], [], []])[7][0]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'fixture-clip.mp4').write_bytes(data)
            Path(directory, 'fixture-clip.jpg').write_bytes(b'\xff\xd8\xff-thumbnail')
            views = [('/allowed', directory)]
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                result = reader.resolve_media(spec, views, reader.file_index(views), True, [1024])
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['path'], '/allowed/fixture-clip.mp4')
        self.assertEqual(result['mimeType'], 'video/mp4')
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())

    def test_tp_image_uses_full_size_checksum_not_medium_or_thumbnail(self):
        original = b'\xff\xd8\xff-original-image-fixture'
        medium = b'\xff\xd8\xff-medium'
        md5 = hashlib.md5(original).hexdigest()
        message = {'msgId': 10, 'type': 3, 'content': '<msg><img length="0" tpurl="fixture" tphdurl="fixture-hd" tplength="' + str(len(medium)) + '" tphdlength="' + str(len(original)) + '" md5="' + md5 + '" /></msg>'}
        row = {'msgId': 10, 'bigImgPath': 'SERVERID://fixture', 'origImgMD5': '', 'totalLen': 0, 'offset': 0, 'iscomplete': 1}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'medium.jpg').write_bytes(medium)
            views = [('/allowed', directory)]
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                pending = reader.assemble_messages([[dict(message)], [dict(row)], [], [], []], views, True)[0]
                self.assertEqual(pending['media'][0]['state'], 'pending')
                Path(directory, 'opaque-original').write_bytes(original)
                ready = reader.assemble_messages([[dict(message)], [dict(row)], [], [], []], views, True)[0]
            self.assertEqual(ready['media'][0]['state'], 'ready')
            self.assertEqual(ready['media'][0]['sha256'], hashlib.sha256(original).hexdigest())
            self.assertEqual(ready['media'][0]['byteLength'], len(original))

    def test_image_length_selects_native_download_resource(self):
        for fields, size in [('length="100" tplength="500"', 100),
                             ('length="100" tpurl="fixture" tplength="50"', 50),
                             ('length="100" hdlength="200" tphdlength="900"', 200),
                             ('length="100" hdlength="200" tphdurl="fixture" tphdlength="80"', 80)]:
            with self.subTest(fields=fields):
                self.assertEqual(reader.original_image_spec({'content': '<img ' + fields + '/>'})['byteLength'], size)

    def test_image_database_path_key_is_not_a_content_checksum(self):
        data = b'\xff\xd8\xff-original-fixture'
        message = {'msgId': 11, 'type': 3, 'content': '<img length="' + str(len(data)) + '" md5="' + hashlib.md5(data).hexdigest() + '"/>'}
        row = {'msgId': 11, 'bigImgPath': '/allowed/image.jpg', 'origImgMD5': hashlib.md5(('/allowed/image.jpg-' + str(len(data))).encode()).hexdigest(), 'totalLen': len(data), 'offset': len(data), 'iscomplete': 1}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'image.jpg').write_bytes(data)
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                media = reader.assemble_messages([[message], [row], [], [], []], [('/allowed', directory)], True)[0]['media'][0]
        self.assertEqual(media['state'], 'ready')
        self.assertEqual(media['sha256'], hashlib.sha256(data).hexdigest())

    def test_native_decoded_photo_requires_full_quality_completion_and_exact_path_key(self):
        data = b'\xff\xd8\xff-official-decoded-cache-fixture'
        message = {'msgId': 12, 'type': 3, 'talker': 'fixture', 'content': '<img length="17" hdlength="30" md5="' + '0' * 32 + '"/>'}
        row = {'msgId': 12, 'bigImgPath': '/allowed/full.jpg', 'origImgMD5': hashlib.md5(('/allowed/full.jpg-' + str(len(data))).encode()).hexdigest(), 'totalLen': len(data), 'offset': len(data), 'iscomplete': 1, 'compressType': 1, 'imageId': 9, 'hdImageId': 9, 'sourceTalker': 'fixture'}
        cases = [({}, 'ready'), ({'offset': 29}, 'pending'), ({'totalLen': 29}, 'pending'),
                 ({'iscomplete': 0}, 'pending'), ({'compressType': 0}, 'pending'),
                 ({'imageId': 8}, 'pending'), ({'origImgMD5': '1' * 32}, 'pending'),
                 ({'sourceTalker': 'other'}, 'pending')]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'full.jpg').write_bytes(data)
            for overrides, state in cases:
                with self.subTest(overrides=overrides), patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                    media = reader.assemble_messages([[dict(message)], [row | overrides], [], [], []], [('/allowed', directory)], True)[0]['media'][0]
                self.assertEqual(media['state'], state)
                if state == 'ready':
                    self.assertEqual(media['sourceRepresentation'], 'native_db_decoded')
                    self.assertFalse(media['originalBytesVerified'])
                    self.assertEqual(media['sourceByteLength'], 30)
                    self.assertEqual(media['byteLength'], len(data))
                    self.assertEqual(media['sha256'], hashlib.sha256(data).hexdigest())

    def test_hd_row_is_selected_through_single_hop_database_reference(self):
        with self.database() as db:
            for name, kind in [('id', 'INTEGER'), ('reserved1', 'INTEGER'), ('compressType', 'INTEGER')]:
                db.execute('ALTER TABLE ImgInfo2 ADD COLUMN ' + name + ' ' + kind)
            db.execute('UPDATE ImgInfo2 SET id=7,reserved1=8,compressType=0')
            db.execute("INSERT INTO ImgInfo2 VALUES(0,0,'full.jpg','',30,30,1,8,0,1)")
            statements = reader.queries(0, 20, [1], image_columns=['id', 'reserved1', 'compressType']).splitlines()
            rows = [[dict(row) for row in db.execute(line)] for line in statements if not line.startswith('.print')][1]
        self.assertEqual({row['imageId'] for row in rows}, {7, 8})
        self.assertTrue(all(row['msgId'] == 1 and row['hdImageId'] == 8 for row in rows))

    def test_image_join_rejects_conflicting_local_server_ids_and_keeps_unidentified_hd_child(self):
        columns = ['id', 'reserved1', 'compressType']
        with self.database() as db:
            for name in columns:
                db.execute('ALTER TABLE ImgInfo2 ADD COLUMN ' + name + ' INTEGER')
            db.execute('UPDATE ImgInfo2 SET id=7,reserved1=8,compressType=0')
            db.execute("INSERT INTO ImgInfo2(msglocalid,msgSvrId,bigImgPath,totalLen,offset,iscomplete,id,reserved1,compressType) VALUES(NULL,NULL,'hd.jpg',0,0,0,8,0,1)")
            for local, server, valid in ((1, 9007199254740993, True), (0, 9007199254740993, True),
                                         (1, 0, True), (None, 9007199254740993, True), (1, None, True),
                                         (1, 9007199254740994, False), (2, 9007199254740993, False),
                                         (0, 0, False)):
                db.execute('UPDATE ImgInfo2 SET msglocalid=?,msgSvrId=? WHERE id=7', (local, server))
                for image_columns in (columns, []):
                    sql = reader.queries(0, 20, [1], image_columns=image_columns)
                    rows = [[dict(row) for row in db.execute(line)] for line in sql.splitlines() if not line.startswith('.print')][1]
                    with self.subTest(local=local, server=server, columns=image_columns):
                        self.assertEqual(len(rows), (2 if image_columns else 1) if valid else 0)
                        if valid and image_columns:
                            self.assertEqual({row['imageId'] for row in rows}, {7, 8})
                            self.assertTrue(all(row['sourceTalker'] == 'room@chatroom' for row in rows))
            db.execute('UPDATE ImgInfo2 SET msglocalid=1,msgSvrId=9007199254740993 WHERE id=7')
            db.execute('UPDATE ImgInfo2 SET msglocalid=2,msgSvrId=9007199254740994 WHERE id=8')
            sql = reader.queries(0, 20, [1], image_columns=columns)
            rows = [[dict(row) for row in db.execute(line)] for line in sql.splitlines() if not line.startswith('.print')][1]
            self.assertEqual({row['imageId'] for row in rows}, {7})

    def test_talkerless_schema_preview_uses_consistent_message_join_but_native_talker_is_preserved(self):
        data, message, low, _ = self.preview_fixture()
        columns = ['id', 'reserved1', 'compressType']
        with self.database() as db, tempfile.TemporaryDirectory() as directory:
            for name in columns:
                db.execute('ALTER TABLE ImgInfo2 ADD COLUMN ' + name + ' INTEGER')
            db.execute('UPDATE message SET type=3,talker=?,content=? WHERE msgId=1', ('fixture', message['content']))
            db.execute('UPDATE ImgInfo2 SET msglocalid=1,bigImgPath=?,origImgMD5=?,totalLen=?,offset=?,id=7,reserved1=8,compressType=0',
                       (low['bigImgPath'], low['origImgMD5'], len(data), len(data)))
            db.execute("INSERT INTO ImgInfo2(msglocalid,msgSvrId,bigImgPath,totalLen,offset,iscomplete,id,reserved1,compressType) VALUES(0,0,'hd.jpg',0,0,0,8,0,1)")
            Path(directory, 'medium.jpg').write_bytes(data)
            for native in (False, True):
                if native:
                    db.execute('ALTER TABLE ImgInfo2 ADD COLUMN msgTalker TEXT')
                    db.execute("UPDATE ImgInfo2 SET msgTalker='other' WHERE id=7")
                sql = reader.queries(0, 20, [1], image_columns=columns + (['msgTalker'] if native else []))
                sections = [[dict(row) for row in db.execute(line)] for line in sql.splitlines() if not line.startswith('.print')]
                self.assertTrue(all(row['sourceTalker'] == ('other' if native else 'fixture') for row in sections[1]))
                with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                    result = reader.assemble_messages(sections, [('/allowed', directory)], True)[0]
                self.assertEqual(result['media'][0]['state'], 'pending')
                self.assertEqual(result['imagePreview']['state'], 'pending' if native else 'ready')

    def test_native_path_size_key_recovers_opaque_cache_without_guessing_filename(self):
        data = b'\xff\xd8\xff-official-decoded-fixture'
        message = {'msgId': 12, 'type': 3, 'content': '<img length="17" />'}
        row = {'msgId': 12, 'bigImgPath': 'SERVERID://fixture', 'origImgMD5': hashlib.md5(('/allowed/opaque-' + str(len(data))).encode()).hexdigest(), 'totalLen': len(data), 'offset': len(data), 'iscomplete': 1, 'compressType': 0, 'imageId': 9, 'hdImageId': 0}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'opaque').write_bytes(data)
            Path(directory, 'unrelated').write_bytes(data)
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                media = reader.assemble_messages([[message], [row], [], [], []], [('/allowed', directory)], True)[0]['media'][0]
        self.assertEqual(media['state'], 'ready')
        self.assertEqual(media['path'], '/allowed/opaque')
        self.assertEqual(media['sourceRepresentation'], 'native_db_decoded')

    def test_unwrapped_emoji_xml_and_database_index_read_original_gif(self):
        data = b'GIF89a-fixture-sticker'
        md5 = hashlib.md5(data).hexdigest()
        xml = 'sender-prefix:<msg><emoji md5="' + md5 + '" androidmd5="' + md5 + '" /></msg>non-XML-tail'
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, md5).write_bytes(data)
            with self.database() as db:
                db.execute('UPDATE message SET type=47,imgPath=?,content=? WHERE msgId=3', (md5, xml))
                db.execute('INSERT INTO EmojiInfo VALUES(?,?)', (md5, len(data)))
                sections = [[dict(row) for row in db.execute(line)] for line in reader.queries(0, 20, [3]).splitlines() if not line.startswith('.print')]
            self.assertEqual(len(sections[6]), 1)
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                result = reader.assemble_messages(sections, [('/allowed', directory)], True)[0]
            self.assertEqual(result['media'][0]['kind'], 'sticker')
            self.assertEqual(result['media'][0]['state'], 'ready')
            self.assertEqual(result['media'][0]['mimeType'], 'image/gif')
            self.assertEqual(result['media'][0]['byteLength'], len(data))

    def test_sticker_named_cache_with_wrong_checksum_is_not_original(self):
        data = b'GIF89a-fixture-sticker'
        md5 = hashlib.md5(data).hexdigest()
        message = {'msgId': 10, 'type': 47, 'imgPath': md5, 'content': '<msg><emoji md5="' + md5 + '" len="' + str(len(data)) + '" /></msg>'}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, md5).write_bytes(b'x' * len(data))
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                result = reader.assemble_messages([[message], [], [], [], [], [], []], [('/allowed', directory)], True)[0]
        self.assertEqual(result['media'][0]['state'], 'pending')
        self.assertEqual(result['media'][0]['reason'], 'cache_checksum_mismatch')
        self.assertNotIn('dataBase64', result['media'][0])

    def test_named_original_survives_equal_size_cache_ambiguity(self):
        data = b'\xff\xd8\xff-original-image'
        md5 = hashlib.md5(data).hexdigest()
        message = {'msgId': 12, 'type': 3, 'content': '<msg><img length="' + str(len(data)) + '" md5="' + md5 + '" /></msg>'}
        with tempfile.TemporaryDirectory() as directory:
            for number in range(9):
                Path(directory, 'unrelated-' + str(number)).write_bytes(b'x' * len(data))
            Path(directory, md5 + '.jpg').write_bytes(data)
            views = [('/allowed', directory)]
            candidates = reader.checksum_candidates(reader.original_image_spec(message), reader.file_index(views))
            self.assertEqual(len(candidates), 1)
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                result = reader.assemble_messages([[message], [], [], [], []], views, True)[0]
        self.assertEqual(result['media'][0]['state'], 'ready')
        self.assertEqual(result['media'][0]['sha256'], hashlib.sha256(data).hexdigest())

    def test_opaque_equal_size_cache_scan_stays_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            for number in range(9):
                Path(directory, 'opaque-' + str(number)).write_bytes(b'x' * 20)
            views = [('/allowed', directory)]
            spec = {'byteLength': 20, 'md5': '0' * 32}
            self.assertEqual(reader.checksum_candidates(spec, reader.file_index(views)), [])

    def test_same_photo_quality_rows_do_not_duplicate_original_asset(self):
        data = b'\xff\xd8\xff-original-image'
        md5 = hashlib.md5(data).hexdigest()
        message = {'msgId': 11, 'type': 3, 'content': '<msg><img length="' + str(len(data)) + '" md5="' + md5 + '" /></msg>'}
        row = {'msgId': 11, 'bigImgPath': '', 'origImgMD5': '', 'totalLen': 0, 'offset': 0, 'iscomplete': 1}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, md5).write_bytes(data)
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                result = reader.assemble_messages([[message], [dict(row), dict(row)], [], [], []], [('/allowed', directory)], True)[0]
        self.assertEqual(len(result['media']), 1)
        self.assertEqual(result['media'][0]['state'], 'ready')

    def preview_fixture(self):
        data = b'\xff\xd8\xff-completed-medium-preview'
        message = {'msgId': 12, 'type': 3, 'talker': 'fixture',
                   'content': '<img length="17" hdlength="179" md5="' + '0' * 32 + '"/>'}
        low = {'msgId': 12, 'bigImgPath': '/allowed/medium.jpg',
               'origImgMD5': hashlib.md5(('/allowed/medium.jpg-' + str(len(data))).encode()).hexdigest(),
               'totalLen': len(data), 'offset': len(data), 'iscomplete': 1,
               'compressType': 0, 'imageId': 7, 'hdImageId': 8, 'sourceTalker': 'fixture'}
        high = {**low, 'bigImgPath': '/allowed/hd.jpg', 'origImgMD5': '',
                'totalLen': 0, 'offset': 0, 'iscomplete': 0, 'compressType': 1, 'imageId': 8}
        return data, message, low, high

    def test_hd_pending_uses_separate_verified_medium_preview_and_one_original(self):
        data, message, low, high = self.preview_fixture()
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory, 'medium.jpg')
            cached.write_bytes(data)
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                for include in (True, False):
                    result = reader.assemble_messages([[dict(message)], [dict(low), dict(high)], [], [], []],
                                                      [('/allowed', directory)], include)[0]
                    self.assertEqual(len(result['media']), 1)
                    self.assertEqual(result['media'][0]['state'], 'pending')
                    preview = result['imagePreview']
                    self.assertEqual(preview['state'], 'ready' if include else 'available')
                    self.assertEqual(preview['sha256'], hashlib.sha256(data).hexdigest())
                    self.assertEqual(preview['name'], 'preview.jpg')
                    self.assertEqual(preview['byteLength'], len(data))
                    self.assertEqual(preview['sourceRepresentation'], 'native_db_decoded')
                    self.assertFalse(preview['originalBytesVerified'])
                    self.assertEqual('dataBase64' in preview, include)
                with patch.object(reader, 'MAX_BATCH_BYTES', len(data) - 1):
                    bounded = reader.assemble_messages([[dict(message)], [dict(low), dict(high)], [], [], []],
                                                       [('/allowed', directory)], True)[0]
                self.assertEqual(bounded['imagePreview']['state'], 'batch_limit')
                self.assertNotIn('dataBase64', bounded['imagePreview'])
            self.assertEqual(cached.read_bytes(), data)

    def test_hd_thumbnail_precedes_download_without_resolving_original(self):
        _, message, low, high = self.preview_fixture()
        data = b'\xff\xd8\xff-local-thumbnail\xff\xd9'
        path = 'THUMBNAIL_DIRPATH://th_exact.jpg'
        message['imgPath'] = path
        low.update(thumbImgPath=path, iscomplete=0)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'th_exact.jpg').write_bytes(data)
            for include in (True, False):
                with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                    result = reader.assemble_messages([[dict(message)], [dict(low), dict(high)], [], [], []], [('/allowed', directory)], include)[0]
                self.assertEqual(result['media'][0]['state'], 'pending')
                preview = result['imagePreview']
                self.assertEqual(preview['state'], 'ready' if include else 'available')
                self.assertEqual(preview['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual(preview['sourceRepresentation'], 'native_db_thumbnail')
                self.assertFalse(preview['originalBytesVerified'])
                self.assertEqual('dataBase64' in preview, include)
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)), patch.object(reader, 'MAX_BATCH_BYTES', 1):
                result = reader.assemble_messages([[dict(message)], [dict(low), dict(high)], [], [], []], [('/allowed', directory)], True)[0]
            self.assertEqual(result['imagePreview']['state'], 'batch_limit')
            self.assertNotIn('dataBase64', result['imagePreview'])

    def test_thumbnail_rejects_wrong_binding_ambiguous_symlink_partial_and_writable(self):
        _, message, low, high = self.preview_fixture()
        path = 'THUMBNAIL_DIRPATH://th_exact.jpg'
        message['imgPath'] = path
        low.update(thumbImgPath=path, iscomplete=0)
        with tempfile.TemporaryDirectory() as directory:
            thumbnail = Path(directory, 'th_exact.jpg')
            thumbnail.write_bytes(b'\xff\xd8\xff-thumbnail\xff\xd9')
            cases = [({'imgPath': 'THUMBNAIL_DIRPATH://../th_exact.jpg'}, {}, os.ST_RDONLY),
                     ({}, {'thumbImgPath': 'THUMBNAIL_DIRPATH://other.jpg'}, os.ST_RDONLY),
                     ({}, {'sourceTalker': 'other'}, os.ST_RDONLY), ({}, {}, 0)]
            for message_changes, row_changes, flags in cases:
                with self.subTest(message_changes=message_changes, row_changes=row_changes, flags=flags), patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=flags)):
                    result = reader.assemble_messages([[message | message_changes], [low | row_changes, dict(high)], [], [], []], [('/allowed', directory)], True)[0]
                self.assertEqual(result['imagePreview']['state'], 'pending')
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                duplicate = Path(directory, 'nested')
                duplicate.mkdir()
                (duplicate / thumbnail.name).write_bytes(thumbnail.read_bytes())
                result = reader.assemble_messages([[dict(message)], [dict(low), dict(high)], [], [], []], [('/allowed', directory)], True)[0]
                self.assertEqual(result['imagePreview']['state'], 'pending')
                (duplicate / thumbnail.name).unlink()
                thumbnail.write_bytes(b'\xff\xd8\xff-incomplete-thumbnail')
                result = reader.assemble_messages([[dict(message)], [dict(low), dict(high)], [], [], []], [('/allowed', directory)], True)[0]
                self.assertEqual(result['imagePreview']['state'], 'pending')
                thumbnail.unlink()
                target = Path(directory, 'target.jpg')
                target.write_bytes(b'\xff\xd8\xff-thumbnail\xff\xd9')
                thumbnail.symlink_to(target)
                result = reader.assemble_messages([[dict(message)], [dict(low), dict(high)], [], [], []], [('/allowed', directory)], True)[0]
                self.assertEqual(result['imagePreview']['state'], 'pending')

    def test_thumbnail_query_is_optional_for_older_image_schemas(self):
        columns = ['id', 'reserved1', 'compressType', 'thumbImgPath']
        self.assertIn('i.thumbImgPath', reader.queries(0, 1, [12], image_columns=columns))
        self.assertNotIn('i.thumbImgPath', reader.queries(0, 1, [12], image_columns=columns[:-1]))

    def test_medium_preview_rejects_wrong_source_partial_cache_thumbnail_and_placeholder(self):
        data, message, low, high = self.preview_fixture()
        cases = [{'msgId': 99}, {'sourceTalker': 'other'}, {'sourceTalker': None}, {'origImgMD5': '1' * 32},
                 {'iscomplete': 0}, {'offset': len(data) - 1}, {'totalLen': len(data) + 1},
                 {'imageId': 8}, {'hdImageId': 0}, {'compressType': 1},
                 {'bigImgPath': '/allowed/th_medium.jpg'}, {'bigImgPath': 'SERVERID://fixture'}]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'medium.jpg').write_bytes(data)
            Path(directory, 'th_medium.jpg').write_bytes(data)
            for overrides in cases:
                with self.subTest(overrides=overrides), \
                        patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                    result = reader.assemble_messages([[dict(message)], [low | overrides, dict(high)], [], [], []],
                                                      [('/allowed', directory)], True)[0]
                self.assertEqual(result['media'][0]['state'], 'pending')
                self.assertEqual(result['imagePreview']['state'], 'pending')
                self.assertNotIn('dataBase64', result['imagePreview'])

    def test_medium_preview_rejects_ambiguous_bytes_but_deduplicates_identical_cache(self):
        data, message, low, high = self.preview_fixture()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'medium.jpg').write_bytes(data)
            other = Path(directory, 'other.jpg')
            for same in (True, False):
                second_data = data if same else b'\xff\xd8\xff-other-medium-preview'
                other.write_bytes(second_data)
                second = {**low, 'bigImgPath': '/allowed/other.jpg', 'imageId': 9,
                          'totalLen': len(second_data), 'offset': len(second_data),
                          'origImgMD5': hashlib.md5(('/allowed/other.jpg-' + str(len(second_data))).encode()).hexdigest()}
                with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                    result = reader.assemble_messages([[dict(message)], [dict(low), second, dict(high)], [], [], []],
                                                      [('/allowed', directory)], True)[0]
                self.assertEqual(result['imagePreview']['state'], 'ready' if same else 'pending')
                self.assertEqual(len(result['media']), 1)

    def test_completed_original_precedes_medium_preview_and_non_hd_stays_unchanged(self):
        data, message, low, high = self.preview_fixture()
        original = b'\xff\xd8\xff-completed-full-quality-original'
        high.update(totalLen=len(original), offset=len(original), iscomplete=1,
                    origImgMD5=hashlib.md5(('/allowed/hd.jpg-' + str(len(original))).encode()).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'medium.jpg').write_bytes(data)
            Path(directory, 'hd.jpg').write_bytes(original)
            with patch.object(reader.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
                result = reader.assemble_messages([[dict(message)], [dict(low), dict(high)], [], [], []],
                                                  [('/allowed', directory)], True)[0]
                self.assertEqual(result['media'][0]['sha256'], hashlib.sha256(original).hexdigest())
                self.assertEqual(len(result['media']), 1)
                self.assertNotIn('imagePreview', result)
                non_hd = {**message, 'content': '<img length="17" md5="' + '0' * 32 + '"/>'}
                unchanged = reader.assemble_messages([[non_hd], [dict(low)], [], [], []], [('/allowed', directory)], True)[0]
                self.assertEqual(unchanged['media'][0]['state'], 'ready')
                self.assertNotIn('imagePreview', unchanged)


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux inotify is unavailable on this host')
class LinuxSourceWatcherTests(unittest.TestCase):
    def test_recursive_media_create_close_rename_and_new_subdirectory(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, 'db')
            media = Path(directory, 'media')
            db.mkdir()
            (media / 'existing').mkdir(parents=True)
            watcher = reader.SourceWatcher(str(db), media_roots=[str(media)])
            try:
                self.assertEqual(watcher.wait_flags(0.02), (False, False))
                Path(db, 'EnMicroMsg.db-shm').write_bytes(b'fixture')
                self.assertEqual(watcher.wait_flags(0.02), (False, False))
                for name in ('image.jpg', 'voice.amr', 'clip.mp4', 'file.pdf', 'record-original'):
                    path = media / 'existing' / name
                    path.write_bytes(b'fixture')
                    self.assertEqual(watcher.wait_flags(1), (False, True))
                    with path.open('ab') as stream:
                        stream.write(b'closed')
                    self.assertEqual(watcher.wait_flags(1), (False, True))
                    path.rename(path.with_name('renamed-' + name))
                    self.assertEqual(watcher.wait_flags(1), (False, True))
                new = media / 'new' / 'nested'
                new.mkdir(parents=True)
                path = new / 'before-registration.jpg'
                path.write_bytes(b'fixture')
                self.assertEqual(watcher.wait_flags(1), (False, True))
                path.write_bytes(b'after-registration')
                self.assertEqual(watcher.wait_flags(1), (False, True))
                local = db / 'image2'
                local.mkdir()
                Path(local, 'image.jpg').write_bytes(b'fixture')
                self.assertEqual(watcher.wait_flags(1), (False, True))
                Path(local, 'image.jpg').write_bytes(b'after-registration')
                self.assertEqual(watcher.wait_flags(1), (False, True))
                Path(db, 'EnMicroMsg.db-wal').write_bytes(b'fixture')
                self.assertEqual(watcher.wait_flags(1), (True, True))
            finally:
                watcher.close()

    def test_recursive_watch_does_not_follow_symlinks_or_exceed_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, 'db')
            media = Path(directory, 'media')
            outside = Path(directory, 'outside')
            db.mkdir()
            media.mkdir()
            outside.mkdir()
            (media / 'link').symlink_to(outside, target_is_directory=True)
            at_limit = media.joinpath(*['nested'] * reader.SourceWatcher.MAX_DEPTH)
            at_limit.mkdir(parents=True)
            deeper = at_limit / 'deeper'
            deeper.mkdir()
            watcher = reader.SourceWatcher(str(db), media_roots=[str(media)])
            try:
                Path(outside, 'outside.jpg').write_bytes(b'fixture')
                Path(deeper, 'too-deep.jpg').write_bytes(b'fixture')
                self.assertEqual(watcher.wait_flags(0.02), (False, False))
                Path(at_limit, 'at-limit.jpg').write_bytes(b'fixture')
                self.assertEqual(watcher.wait_flags(1), (False, True))
            finally:
                watcher.close()

    def test_recursive_entry_bound_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, 'db')
            media = Path(directory, 'media')
            db.mkdir()
            media.mkdir()
            for number in range(3):
                Path(media, str(number)).write_bytes(b'fixture')
            with patch.object(reader.SourceWatcher, 'MAX_ENTRIES', 2):
                with self.assertRaisesRegex(RuntimeError, 'media_index_bound_exceeded'):
                    reader.SourceWatcher(str(db), media_roots=[str(media)])

    def test_recursive_directory_rename_restarts_watch(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, 'db')
            media = Path(directory, 'media')
            db.mkdir()
            leaf = media / 'leaf'
            leaf.mkdir(parents=True)
            watcher = reader.SourceWatcher(str(db), media_roots=[str(media)])
            try:
                leaf.rename(media / 'renamed')
                with self.assertRaisesRegex(RuntimeError, 'source_watch_invalidated'):
                    watcher.wait_flags(1)
            finally:
                watcher.close()

    def test_db_wal_writes_and_replacement_keep_directory_watch(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory, 'EnMicroMsg.db')
            database.write_bytes(b'fixture')
            watcher = reader.SourceWatcher(directory)
            try:
                self.assertFalse(watcher.wait(0.02))
                database.write_bytes(b'changed')
                self.assertTrue(watcher.wait(1))
                Path(directory, 'EnMicroMsg.db-wal').write_bytes(b'wal')
                self.assertTrue(watcher.wait(1))
                replacement = Path(directory, 'replacement')
                replacement.write_bytes(b'replacement')
                replacement.replace(database)
                self.assertTrue(watcher.wait(1))
                database.write_bytes(b'after replacement')
                self.assertTrue(watcher.wait(1))
            finally:
                watcher.close()

    def test_unrelated_and_shm_events_do_not_wake_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = reader.SourceWatcher(directory)
            try:
                Path(directory, 'EnMicroMsg.db-shm').write_bytes(b'fixture')
                Path(directory, 'unrelated').write_bytes(b'fixture')
                self.assertFalse(watcher.wait(0.02))
            finally:
                watcher.close()

    def test_directory_self_invalidation_ends_watch(self):
        with tempfile.TemporaryDirectory() as directory:
            watched = Path(directory, 'watched')
            watched.mkdir()
            watcher = reader.SourceWatcher(str(watched))
            try:
                watched.rename(Path(directory, 'moved'))
                with self.assertRaisesRegex(RuntimeError, 'source_watch_invalidated'):
                    watcher.wait(1)
            finally:
                watcher.close()


if __name__ == '__main__':
    unittest.main()
