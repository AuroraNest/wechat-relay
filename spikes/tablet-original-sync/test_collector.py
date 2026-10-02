import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import collector as c


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = c.open_store(Path(self.temporary.name))
        self.key = bytes(range(32))
        self.config = {'kA2I': c.encoded(self.key), 'deviceId': c.encoded(bytes(range(16)))}

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def batch(self, media=None):
        return {'accountFingerprint': 'a' * 64, 'messages': [{
            'msgId': 1, 'msgSvrId': '123', 'type': 1, 'createTime': c.now_ms(), 'isSend': 0,
            'talker': 'fixture@chatroom', 'content': 'member:\n完整消息' * 200,
            'conversationName': 'Fixture group', 'senderName': 'Fixture sender', 'media': media or []}]}

    def plaintext(self, envelope):
        return AESGCM(self.key).decrypt(c.decoded(envelope['iv']), c.decoded(envelope['ct']), envelope['aad'].encode())

    def test_full_content_and_preview_have_separate_bounds(self):
        c.ingest(self.db, self.config, self.batch())
        message = json.loads(self.db.execute('SELECT body FROM messages').fetchone()[0])
        full = json.loads(self.plaintext(message['contentEnvelope']))
        self.assertGreater(len(full['text'].encode()), 600)
        self.assertLessEqual(len(self.plaintext(message['previewEnvelope'])), 600)
        self.assertFalse(message['replyCapable'])
        self.assertFalse(message['conversationSendCapable'])

    def test_restart_preserves_exact_body_sequence_and_source_dedupe(self):
        batch = self.batch()
        c.ingest(self.db, self.config, batch)
        original = self.db.execute('SELECT seq,body FROM messages').fetchone()
        self.db.close()
        self.db = c.open_store(Path(self.temporary.name))
        c.ingest(self.db, self.config, batch)
        self.assertEqual(self.db.execute('SELECT seq,body FROM messages').fetchall(), [original])
        batch['messages'][0]['msgId'] = 2
        batch['messages'][0]['msgSvrId'] = '124'
        c.ingest(self.db, self.config, batch)
        self.assertEqual(self.db.execute('SELECT seq FROM messages ORDER BY seq').fetchall(), [(1,), (2,)])

    def test_pending_asset_hydration_and_identical_retry(self):
        data = b'#!SILK_V3 fixture bytes'
        media = {'kind': 'audio', 'mimeType': 'audio/silk', 'byteLength': len(data), 'state': 'pending'}
        batch = self.batch([media])
        c.ingest(self.db, self.config, batch)
        original = self.db.execute('SELECT body FROM messages').fetchone()[0]
        self.assertIsNone(self.db.execute('SELECT body FROM assets').fetchone()[0])
        media.update({'dataBase64': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest(), 'state': 'available'})
        c.ingest(self.db, self.config, batch)
        first = self.db.execute('SELECT body FROM assets').fetchone()[0]
        self.assertEqual(self.plaintext(json.loads(first)['envelope']), data)
        c.ingest(self.db, self.config, batch)
        self.assertEqual(self.db.execute('SELECT body FROM assets').fetchone()[0], first)
        self.assertEqual(self.db.execute('SELECT body FROM messages').fetchone()[0], original)

    def test_policy_drop_is_not_acknowledged(self):
        c.ingest(self.db, self.config, self.batch())
        with self.assertRaisesRegex(c.CollectorError, 'relay_policy_paused'):
            c.upload(self.db, self.config, lambda *args: {'dropped': True})
        self.assertEqual(self.db.execute('SELECT uploaded FROM messages').fetchone()[0], 0)

    def test_unconfirmed_ack_is_not_marked_uploaded(self):
        c.ingest(self.db, self.config, self.batch())
        with self.assertRaisesRegex(c.CollectorError, 'message_ack_not_confirmed'):
            c.upload(self.db, self.config, lambda *args: {})
        self.assertEqual(self.db.execute('SELECT uploaded FROM messages').fetchone()[0], 0)

    def test_outgoing_server_id_assignment_does_not_duplicate_message(self):
        batch = self.batch()
        batch['messages'][0].update(msgSvrId='0', isSend=1)
        c.ingest(self.db, self.config, batch)
        original = self.db.execute('SELECT body FROM messages').fetchone()[0]
        batch['messages'][0]['msgSvrId'] = '456'
        c.ingest(self.db, self.config, batch)
        self.assertEqual(self.db.execute('SELECT body FROM messages').fetchall(), [(original,)])
        content = json.loads(self.plaintext(json.loads(original)['contentEnvelope']))
        self.assertTrue(content['isOutgoing'])
        self.assertEqual(content['senderName'], '我')

    def test_playback_preserves_original_as_separate_asset(self):
        original = b'\x02#!SILK_V3 fixture'
        wav = b'RIFF fixture WAVE'
        media = {'kind': 'audio', 'mimeType': 'audio/silk', 'state': 'ready',
                 'byteLength': len(original), 'dataBase64': base64.b64encode(original).decode()}
        with patch.object(c, 'decode_silk', return_value=wav):
            c.ingest(self.db, self.config, self.batch([media]), Path('/fixture/decoder'))
        message = json.loads(self.db.execute('SELECT body FROM messages').fetchone()[0])
        assets = message['nativeAssets']
        self.assertEqual([a['role'] for a in assets], ['original', 'playback'])
        self.assertEqual(assets[1]['derivedFrom'], assets[0]['id'])
        plaintexts = [self.plaintext(json.loads(row[0])['envelope']) for row in self.db.execute('SELECT body FROM assets ORDER BY ordinal')]
        self.assertEqual(plaintexts, [original, wav])
        content = json.loads(self.plaintext(message['contentEnvelope']))
        self.assertEqual({a['assetId'] for a in content['attachments']}, {a['id'] for a in assets})

    def test_ambiguous_account_and_source_identity_fail_closed(self):
        batch = self.batch()
        c.ingest(self.db, self.config, batch)
        batch['accountFingerprint'] = 'b' * 64
        with self.assertRaisesRegex(c.CollectorError, 'source_account_changed'):
            c.ingest(self.db, self.config, batch)
        batch['accountFingerprint'] = 'a' * 64
        batch['messages'][0]['msgSvrId'] = 'changed'
        with self.assertRaisesRegex(c.CollectorError, 'source_identity_changed'):
            c.ingest(self.db, self.config, batch)

    def test_record_playback_stays_with_original_record_item(self):
        media = {'kind': 'audio', 'mimeType': 'audio/silk', 'state': 'ready',
                 'byteLength': 3, 'dataBase64': base64.b64encode(b'raw').decode(),
                 'recordItemIndex': 2}
        with patch.object(c, 'decode_silk', return_value=b'RIFF fixture WAVE'):
            items = c.playback_media([media], Path('/fixture/decoder'))
        self.assertEqual(items[1]['recordItemIndex'], 2)
        self.assertEqual(items[1]['derivedOrdinal'], 0)

    def test_unknown_size_and_oversize_never_fabricate_attachment(self):
        media = {'kind': 'file', 'mimeType': 'application/octet-stream', 'byteLength': 0}
        batch = self.batch([media])
        c.ingest(self.db, self.config, batch)
        self.assertEqual(self.db.execute('SELECT status FROM sources').fetchone()[0], 'waiting_original_size')
        media['byteLength'] = c.MAX_ASSET + 1
        c.ingest(self.db, self.config, batch)
        self.assertEqual(self.db.execute('SELECT status FROM sources').fetchone()[0], 'attachment_over_8mib')
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_record_preserves_nested_text_and_original_xml(self):
        row = self.batch()['messages'][0]
        row['type'] = 49
        row['content'] = '<msg><appmsg><type>19</type><title>聊天记录</title><recorditem><![CDATA[<recordinfo><datalist><dataitem datatype="1"><sourcename>A</sourcename><datadesc>完整原文</datadesc></dataitem></datalist></recordinfo>]]></recorditem></appmsg></msg>'
        content = c.native_content(row)
        self.assertEqual(content['kind'], 'record')
        self.assertEqual(content['records'][0]['text'], '完整原文')
        self.assertEqual(content['rawXML'], row['content'])

    def test_pairing_rejects_expiry_and_insecure_origin(self):
        value = {'v': 1, 'origin': 'https://relay.example', 'pairId': c.uuid7(), 'pairSecret': 'fixture',
                 'expiresAt': c.now_ms() + 10000, 'kA2I': c.encoded(self.key), 'kI2A': c.encoded(self.key)}
        self.assertEqual(c.pairing_payload('AWR1:' + c.encoded(c.json_bytes(value)))['origin'], value['origin'])
        for replacement in ({'origin': 'http://relay.example'}, {'expiresAt': 1}, {'origin': 'https://user:pass@relay.example'}):
            invalid = dict(value, **replacement)
            with self.assertRaises(c.CollectorError):
                c.pairing_payload('AWR1:' + c.encoded(c.json_bytes(invalid)))


if __name__ == '__main__':
    unittest.main()
