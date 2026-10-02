"""Synthetic fixtures only; no account, credentials, or real message data."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

import source_reader as reader


class SourceReaderTests(unittest.TestCase):
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

    def test_targeted_rescan_ignores_after_cursor_and_joins_only_selected(self):
        sections = self.execute_queries(after=99, ids=[2])
        self.assertEqual([row['msgId'] for row in sections[0]], [2])
        self.assertEqual(sections[2][0]['TotalLen'], 5543)
        for args in [(-1, 20, []), (0, 101, []), (0, 20, [0]), (0, 20, list(range(1, 22)))]:
            with self.assertRaises(RuntimeError):
                reader.selection(*args)

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
        message = {'msgId': 10, 'type': 3, 'content': '<msg><img length="0" tplength="' + str(len(medium)) + '" tphdlength="' + str(len(original)) + '" md5="' + md5 + '" /></msg>'}
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


if __name__ == '__main__':
    unittest.main()
