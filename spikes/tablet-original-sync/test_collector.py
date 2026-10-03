import base64
from concurrent.futures import Future
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
import unicodedata
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import collector as c


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = c.open_store(Path(self.temporary.name))
        self.key = bytes(range(32))
        self.config = {'kA2I': c.encoded(self.key), 'deviceId': c.encoded(bytes(range(16))),
                       'pairId': '00000000-0000-4000-8000-000000000001'}

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

    def contact_batch(self, names):
        contacts = []
        occurrences = {}
        for name in names:
            normalized = unicodedata.normalize('NFC', name) if isinstance(name, str) else str(name)
            token = hashlib.sha256(normalized.encode()).hexdigest()[:16]
            occurrences[token] = occurrences.get(token, 0) + 1
            identity = 'fixture_' + token + '_' + str(occurrences[token])
            contacts.append({'name': name, 'conversationId': identity, 'alias': identity})
        return {'accountFingerprint': 'a' * 64, 'messages': [],
                'contactSnapshot': {'state': 'ready', 'contacts': contacts}}

    def test_contacts_empty_message_batch_encrypts_sorted_v3_identity_snapshot(self):
        c.ingest(self.db, self.config, self.contact_batch(['张三', 'Alice', 'e\u0301']))
        state = json.loads(c.setting(self.db, 'contacts_state'))
        body = state['pending']
        self.assertEqual(set(body), {'v', 'id', 'deviceId', 'wechatUserId', 'capturedAt', 'contactsEnvelope'})
        self.assertEqual(body['v'], 3)
        self.assertEqual(body['wechatUserId'], 0)
        self.assertEqual(body['id'].split('-')[2][0], '7')
        envelope = body['contactsEnvelope']
        self.assertEqual(envelope['kid'], 'phase1-contacts')
        self.assertEqual(envelope['aad'], f"AWR1|A2I_CONTACTS|3|{body['id']}|{body['deviceId']}|{body['capturedAt']}|0")
        expected = sorted(self.contact_batch(['张三', 'Alice', 'é'])['contactSnapshot']['contacts'], key=lambda row: row['conversationId'])
        self.assertEqual(json.loads(self.plaintext(envelope)), {'v': 3, 'accountFingerprint': 'a' * 64, 'contacts': expected})
        self.assertEqual(state['snapshotId'], body['id'])
        self.assertEqual(c.status(self.db)['contacts_count'], 3)
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_contacts_retry_restart_and_unchanged_list_preserve_exact_ciphertext(self):
        batch = self.contact_batch(['Bob', 'Alice'])
        c.ingest(self.db, self.config, batch)
        original = c.setting(self.db, 'contacts_state')
        sent = []
        def failed(config, path, body):
            self.assertEqual(path, '/api/v1/android/contacts')
            sent.append(body)
            raise c.CollectorError('network_transport_error')
        with self.assertRaises(c.CollectorError):
            c.upload_contacts(self.db, self.config, failed)
        self.assertEqual(c.setting(self.db, 'contacts_state'), original)
        self.db.close()
        self.db = c.open_store(Path(self.temporary.name))
        c.ingest(self.db, self.config, self.contact_batch(['Alice', 'Bob']))
        self.assertEqual(c.setting(self.db, 'contacts_state'), original)
        def confirmed(config, path, body):
            self.assertEqual(body, sent[0])
            return {'id': json.loads(body)['id'], 'idempotent': True}
        c.upload_contacts(self.db, self.config, confirmed)
        acknowledged = c.setting(self.db, 'contacts_state')
        self.assertNotIn('pending', json.loads(acknowledged))
        self.assertTrue(json.loads(acknowledged)['acknowledged'])
        self.assertEqual(json.loads(acknowledged)['snapshotId'], json.loads(original)['pending']['id'])
        c.ingest(self.db, self.config, batch)
        self.assertEqual(c.setting(self.db, 'contacts_state'), acknowledged)
        c.upload_contacts(self.db, self.config, lambda *args: self.fail('unchanged snapshot uploaded'))

    def test_invalid_contacts_preserve_old_queue_and_do_not_block_messages(self):
        c.ingest(self.db, self.config, self.contact_batch(['Alice']))
        original = c.setting(self.db, 'contacts_state')
        cases = [([''], 'invalid_contact_name'), ([' Alice'], 'invalid_contact_name'),
                 (['x' * 513], 'invalid_contact_name'), ([None], 'invalid_contact_name'),
                 (['x'] * 10001, 'invalid_contacts_snapshot')]
        for names, reason in cases:
            with self.subTest(reason=reason):
                batch = self.contact_batch(names)
                batch['messages'] = self.batch()['messages']
                c.ingest(self.db, self.config, batch)
                self.assertEqual(c.setting(self.db, 'contacts_state'), original)
                self.assertEqual(c.setting(self.db, 'contacts_status'), reason)
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
        batch = {'accountFingerprint': 'a' * 64, 'messages': [],
                 'contactSnapshot': {'state': 'unavailable', 'reason': 'contacts_schema_unsupported'}}
        c.ingest(self.db, self.config, batch)
        self.assertEqual(c.setting(self.db, 'contacts_state'), original)
        self.assertEqual(c.setting(self.db, 'contacts_status'), 'contacts_schema_unsupported')
        batch['contactSnapshot']['reason'] = 'private-name-that-must-not-be-printed'
        c.ingest(self.db, self.config, batch)
        self.assertEqual(c.setting(self.db, 'contacts_status'), 'contacts_source_unavailable')
        batch.pop('contactSnapshot')
        c.ingest(self.db, self.config, batch)
        self.assertEqual(c.setting(self.db, 'contacts_state'), original)

    def test_contacts_identity_and_ack_validation_preserve_queue(self):
        c.ingest(self.db, self.config, self.contact_batch(['Alice']))
        original = c.setting(self.db, 'contacts_state')
        batch = self.contact_batch(['Bob'])
        batch['accountFingerprint'] = 'b' * 64
        with self.assertRaisesRegex(c.CollectorError, 'source_account_changed'):
            c.ingest(self.db, self.config, batch)
        for changed in ({'deviceId': 'other-device'}, {'pairId': 'other-pair'}, {'kA2I': c.encoded(b'x' * 32)}):
            with self.assertRaisesRegex(c.CollectorError, 'contacts_identity_changed'):
                c.upload_contacts(self.db, {**self.config, **changed}, lambda *args: self.fail('identity mismatch sent'))
        with self.assertRaisesRegex(c.CollectorError, 'contacts_ack_not_confirmed'):
            c.upload_contacts(self.db, self.config, lambda *args: {})
        self.assertEqual(c.setting(self.db, 'contacts_state'), original)

    def test_contacts_stale_ack_preserves_newer_empty_snapshot(self):
        c.ingest(self.db, self.config, self.contact_batch(['Alice']))
        old = json.loads(c.setting(self.db, 'contacts_state'))
        def ack(config, path, body):
            c.ingest(self.db, self.config, self.contact_batch([]))
            return {'id': json.loads(body)['id'], 'idempotent': False}
        c.upload_contacts(self.db, self.config, ack)
        latest = json.loads(c.setting(self.db, 'contacts_state'))
        self.assertGreater(latest['capturedAt'], old['capturedAt'])
        self.assertEqual(latest['count'], 0)
        self.assertNotEqual(latest['pending']['id'], old['pending']['id'])
        self.assertEqual(json.loads(self.plaintext(latest['pending']['contactsEnvelope'])), {'v': 3, 'accountFingerprint': 'a' * 64, 'contacts': []})

    def test_duplicate_names_keep_distinct_identities_and_empty_aliases_are_displayable(self):
        batch = self.contact_batch(['Alice', 'Alice', 'é', 'e\u0301'])
        batch['contactSnapshot']['contacts'][0]['alias'] = ''
        c.ingest(self.db, self.config, batch)
        state = json.loads(c.setting(self.db, 'contacts_state'))
        contacts = json.loads(self.plaintext(state['pending']['contactsEnvelope']))['contacts']
        self.assertEqual(len(contacts), 4)
        self.assertEqual(len({row['conversationId'] for row in contacts}), 4)
        self.assertEqual(sum(row['name'] == 'Alice' for row in contacts), 2)
        original = c.setting(self.db, 'contacts_state')
        batch['contactSnapshot']['contacts'][1]['conversationId'] = batch['contactSnapshot']['contacts'][0]['conversationId']
        c.ingest(self.db, self.config, batch)
        self.assertEqual(c.setting(self.db, 'contacts_state'), original)
        self.assertEqual(c.setting(self.db, 'contacts_status'), 'contacts_identity_ambiguous')

    def test_contacts_payload_size_is_bounded_without_clearing_old_queue(self):
        c.ingest(self.db, self.config, self.contact_batch(['Alice']))
        original = c.setting(self.db, 'contacts_state')
        c.ingest(self.db, self.config, self.contact_batch([str(index) + 'x' * 500 for index in range(2200)]))
        self.assertEqual(c.setting(self.db, 'contacts_state'), original)
        self.assertEqual(c.setting(self.db, 'contacts_status'), 'contacts_too_large')

    def test_contacts_background_worker_attempts_after_failed_asset(self):
        c.ingest(self.db, self.config, self.contact_batch(['Alice']))
        calls = []
        def assets(db, config, limit):
            calls.append(('assets', limit))
            raise c.CollectorError('fixture_asset_failed')
        with patch.object(c, 'upload_assets', side_effect=assets), patch.object(c, 'upload_contacts', side_effect=lambda *args: calls.append(('contacts',))):
            with self.assertRaisesRegex(c.CollectorError, 'fixture_asset_failed'):
                c.background_assets(Path(self.temporary.name), self.config, None)
        self.assertEqual(calls, [('assets', 1), ('contacts',)])

    def test_contacts_once_reads_fast_snapshot_without_advancing_full_media_cursor(self):
        reads = []
        def read(command, after, ids=None, **options):
            reads.append((after, options))
            if options.get('defer_media'):
                batch = self.contact_batch(['Alice'])
                batch['messages'] = self.batch()['messages']
                return batch
            return {'accountFingerprint': 'a' * 64, 'messages': []}
        with patch.object(c, 'read_source', side_effect=read), patch.object(c, 'upload_contacts') as contacts:
            c.run(self.db, self.config, ['fixture-reader'], True, None)
        self.assertEqual(reads, [(0, {'include_media': False, 'defer_media': True}), (0, {})])
        self.assertEqual(c.setting(self.db, 'cursor', '0'), '0')
        self.assertEqual(c.status(self.db)['contacts_count'], 1)
        contacts.assert_called_once_with(self.db, self.config)

    def test_contacts_event_worker_runs_with_empty_message_batch(self):
        class StopLoop(BaseException):
            pass
        class Wake:
            def __enter__(self):
                self.dirty, self.media_dirty, self.wake = threading.Event(), threading.Event(), threading.Event()
                self.dirty.set()
                return self
            def __exit__(self, *_):
                pass
            def retry(self, **flags):
                pass
        jobs = []
        class Worker:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def submit(self, operation, *args):
                jobs.append(operation)
                raise StopLoop()
        with patch.object(c, 'SourceWake', return_value=Wake()), patch.object(c, 'ThreadPoolExecutor', return_value=Worker()), patch.object(c, 'read_source', return_value=self.contact_batch(['Alice'])), patch.object(c, 'upload_contacts') as contacts:
            with self.assertRaises(StopLoop):
                c.run_events(self.db, self.config, ['fixture-reader'], None, None, None)
        self.assertEqual(jobs, [c.background_assets])
        contacts.assert_not_called()

    def record_batch(self, media):
        from xml.sax.saxutils import escape
        record = '<recordinfo><dataitem datatype="1"><datadesc>text first</datadesc></dataitem>'
        for ordinal, item in enumerate(media):
            datatype = {'image': '2', 'sticker': '37', 'audio': '3', 'video': '4', 'file': '8'}[item['kind']]
            record += '<dataitem datatype="' + datatype + '" dataid="fixture-' + str(ordinal) + '" />'
            item['recordItemIndex'] = ordinal + 1
        record += '</recordinfo>'
        batch = self.batch(media)
        batch['messages'][0].update(type=49, content='<msg><appmsg><type>19</type><recorditem>' + escape(record) + '</recorditem></appmsg></msg>')
        return batch

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

    def test_hd_image_preview_and_original_resolve_in_one_immutable_message(self):
        import image_download
        full = b'\xff\xd8\xff-original-hd-image'
        preview = b'\xff\xd8\xff-preview'
        batch = self.batch()
        batch['sourceMaxId'] = 1
        row = batch['messages'][0]
        row.update(type=3, content='<msg><img hdlength="' + str(len(full)) + '" md5="' + hashlib.md5(full).hexdigest() + '" /></msg>',
                   mediaDeferred=True)
        c.ingest(self.db, self.config, batch)
        first = self.db.execute('SELECT body FROM messages').fetchone()[0]
        message = json.loads(first)
        self.assertEqual(message['v'], 8)
        slots = message['nativeAssetSlots']
        self.assertEqual([slot['role'] for slot in slots], ['original', 'playback'])
        self.assertEqual(slots[1]['derivedFrom'], slots[0]['id'])
        content = json.loads(self.plaintext(message['contentEnvelope']))
        self.assertEqual(content['attachments'][1]['name'], 'preview.jpg')
        row.pop('mediaDeferred')
        row['media'] = [{'kind': 'image', 'state': 'pending', 'mimeType': 'application/octet-stream', 'byteLength': len(full)}]
        row['imagePreview'] = {'kind': 'image', 'state': 'ready', 'mimeType': 'image/jpeg', 'byteLength': len(preview),
                               'sha256': hashlib.sha256(preview).hexdigest(), 'dataBase64': base64.b64encode(preview).decode(),
                               'sourceRepresentation': 'native_db_thumbnail', 'originalBytesVerified': False}
        self.assertIsNotNone(image_download.image_command(batch, row, c.now_ms()))
        c.ingest(self.db, self.config, batch)
        bodies = self.db.execute('SELECT body FROM assets ORDER BY ordinal').fetchall()
        self.assertIsNone(bodies[0][0])
        self.assertEqual(self.plaintext(json.loads(bodies[1][0])['envelope']), preview)
        medium = b'\xff\xd8\xff-later-medium-image'
        row['imagePreview'].update(byteLength=len(medium), sha256=hashlib.sha256(medium).hexdigest(),
                                   dataBase64=base64.b64encode(medium).decode(), sourceRepresentation='native_db_decoded')
        c.ingest(self.db, self.config, batch)
        self.assertEqual(self.db.execute('SELECT body FROM assets ORDER BY ordinal').fetchall(), bodies)
        self.db.close()
        self.db = c.open_store(Path(self.temporary.name))
        row['media'][0].update(state='ready', mimeType='image/jpeg', dataBase64=base64.b64encode(full).decode(),
                               sha256=hashlib.sha256(full).hexdigest())
        row.pop('imagePreview')
        c.ingest(self.db, self.config, batch)
        final = self.db.execute('SELECT body FROM assets ORDER BY ordinal').fetchall()
        self.assertEqual(self.plaintext(json.loads(final[0][0])['envelope']), full)
        self.assertEqual(final[1][0], bodies[1][0])
        self.assertEqual(self.db.execute('SELECT body FROM messages').fetchone()[0], first)
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)

    def test_hd_original_does_not_wait_for_unnecessary_preview(self):
        data = b'\xff\xd8\xff-hd'
        batch = self.batch([{'kind': 'image', 'state': 'ready', 'mimeType': 'image/jpeg', 'byteLength': len(data),
                             'dataBase64': base64.b64encode(data).decode()}])
        batch['messages'][0].update(type=3, content='<msg><img hdlength="7" /></msg>')
        c.ingest(self.db, self.config, batch)
        rows = self.db.execute('SELECT body,last_error FROM assets ORDER BY ordinal').fetchall()
        self.assertIsNotNone(rows[0][0])
        self.assertIsNone(rows[1][0])
        self.assertEqual(rows[1][1], 'playback_not_applicable')

    def test_existing_v7_hd_image_ignores_later_preview_representation(self):
        data = b'\xff\xd8\xff-hd'
        batch = self.batch([{'kind': 'image', 'state': 'ready', 'mimeType': 'image/jpeg', 'byteLength': len(data),
                             'dataBase64': base64.b64encode(data).decode()}])
        batch['messages'][0].update(type=3, content='<msg><img hdlength="7" /></msg>')
        with patch('source_reader.original_image_spec', return_value={'requiresHD': False}):
            c.ingest(self.db, self.config, batch)
        first = self.db.execute('SELECT body FROM messages').fetchone()[0]
        self.assertEqual(json.loads(first)['v'], 7)
        batch['messages'][0]['imagePreview'] = {**batch['messages'][0]['media'][0], 'name': 'preview.jpg'}
        c.ingest(self.db, self.config, batch)
        self.assertEqual(self.db.execute('SELECT body FROM messages').fetchone()[0], first)
        self.assertEqual(self.db.execute('SELECT count(*) FROM assets').fetchone()[0], 1)

    def test_record_text_precedes_declared_image_and_original_hydrates_same_message(self):
        import source_reader
        from xml.sax.saxutils import escape
        data = b'\xff\xd8\xff-record-original'
        record = ('<recordinfo><dataitem datatype="1"><datadesc>visible first</datadesc></dataitem>'
                  '<dataitem datatype="2" dataid="image-id"><datafmt>jpg</datafmt><datasize>' + str(len(data)) +
                  '</datasize><fullmd5>' + hashlib.md5(data).hexdigest() + '</fullmd5></dataitem></recordinfo>')
        batch = self.batch()
        row = batch['messages'][0]
        row.update(type=49, content='<msg><appmsg><type>19</type><recorditem>' + escape(record) + '</recorditem></appmsg></msg>')
        batch['messages'] = source_reader.assemble_messages([[row], [], [], [], [], []], [], True)
        c.ingest(self.db, self.config, batch)
        original = self.db.execute('SELECT body FROM messages').fetchone()[0]
        content = json.loads(self.plaintext(json.loads(original)['contentEnvelope']))
        self.assertEqual(content['records'][0]['text'], 'visible first')
        self.assertEqual(content['attachments'][0]['recordItemIndex'], 1)
        self.assertIsNone(self.db.execute('SELECT body FROM assets').fetchone()[0])
        row['media'][0].update(state='ready', dataBase64=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest())
        c.ingest(self.db, self.config, batch)
        self.assertEqual(self.db.execute('SELECT body FROM messages').fetchone()[0], original)
        asset = json.loads(self.db.execute('SELECT body FROM assets').fetchone()[0])
        self.assertEqual(self.plaintext(asset['envelope']), data)

    def test_unknown_record_slots_resolve_independently_with_pair_bound_immutable_body(self):
        media = [{'kind': kind, 'state': 'pending', 'mimeType': 'application/octet-stream', 'byteLength': 0}
                 for kind in ('image', 'sticker')]
        batch = self.record_batch(media)
        c.ingest(self.db, self.config, batch)
        original = self.db.execute('SELECT body FROM messages').fetchone()[0]
        message = json.loads(original)
        self.assertEqual(message['v'], 8)
        self.assertNotIn('nativeAssets', message)
        self.assertEqual(len(message['nativeAssetSlots']), 2)
        self.assertTrue(all('mimeType' not in slot and 'byteLength' not in slot for slot in message['nativeAssetSlots']))
        content = json.loads(self.plaintext(message['contentEnvelope']))
        self.assertEqual(content['records'][0]['text'], 'text first')
        self.assertEqual(content['records'][2]['kind'], 'sticker')
        self.assertIn('|8|' + self.config['pairId'] + '|', message['contentEnvelope']['aad'])
        data = b'GIF89a-fixture-late-record'
        media[1].update(state='ready', byteLength=len(data), mimeType='image/gif',
                        dataBase64=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest())
        c.ingest(self.db, self.config, batch)
        assets = self.db.execute('SELECT body FROM assets ORDER BY ordinal').fetchall()
        self.assertIsNone(assets[0][0])
        resolved = json.loads(assets[1][0])
        self.assertEqual(resolved['metadata']['id'], message['nativeAssetSlots'][1]['id'])
        self.assertEqual(self.plaintext(resolved['envelope']), data)
        self.assertIn('|8|' + self.config['pairId'] + '|', resolved['envelope']['aad'])
        self.db.close()
        self.db = c.open_store(Path(self.temporary.name))
        c.ingest(self.db, self.config, batch)
        self.assertEqual(self.db.execute('SELECT body FROM messages').fetchone()[0], original)
        self.assertEqual(self.db.execute('SELECT body FROM assets ORDER BY ordinal').fetchall(), assets)
        with self.assertRaisesRegex(c.CollectorError, 'source_pair_changed'):
            c.ingest(self.db, {**self.config, 'pairId': '00000000-0000-4000-8000-000000000002'}, batch)
        batch['messages'][0]['content'] += 'changed'
        with self.assertRaisesRegex(c.CollectorError, 'source_content_changed'):
            c.ingest(self.db, self.config, batch)

    def test_deferred_record_publishes_text_without_touching_media_lane(self):
        import source_reader
        batch = self.record_batch([{'kind': 'image'}, {'kind': 'sticker'}])
        row = batch['messages'][0]
        row.update(media=[], mediaDeferred=True)
        with patch.object(source_reader, 'file_index', side_effect=AssertionError('media scan on text lane')):
            c.ingest(self.db, self.config, batch)
        message = json.loads(self.db.execute('SELECT body FROM messages').fetchone()[0])
        self.assertEqual([slot['kind'] for slot in message['nativeAssetSlots']], ['image', 'sticker'])
        self.assertEqual(json.loads(self.plaintext(message['contentEnvelope']))['records'][0]['text'], 'text first')
        self.assertEqual(self.db.execute('SELECT count(*) FROM assets WHERE body IS NULL').fetchone()[0], 2)

    def test_bad_record_asset_does_not_block_text_or_other_asset(self):
        data = b'\xff\xd8\xff-valid-original'
        media = [{'kind': 'image', 'mimeType': 'image/jpeg', 'state': 'ready', 'byteLength': len(data),
                  'dataBase64': base64.b64encode(data).decode(), 'sha256': digest}
                 for digest in ('0' * 64, hashlib.sha256(data).hexdigest())]
        c.ingest(self.db, self.config, self.record_batch(media))
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
        assets = self.db.execute('SELECT body,last_error FROM assets ORDER BY ordinal').fetchall()
        self.assertEqual(assets[0], (None, 'source_media_hash_mismatch'))
        self.assertIsNotNone(assets[1][0])
        self.assertIsNone(assets[1][1])

    def test_record_late_audio_preserves_playback_slot_identity(self):
        media = [{'kind': 'audio', 'state': 'pending', 'mimeType': 'application/octet-stream', 'byteLength': 0}]
        batch = self.record_batch(media)
        decoder = Path('/fixture/decoder')
        c.ingest(self.db, self.config, batch, decoder)
        original = self.db.execute('SELECT body FROM messages').fetchone()[0]
        slots = json.loads(original)['nativeAssetSlots']
        self.assertEqual(slots[1]['derivedFrom'], slots[0]['id'])
        data = b'#!SILK_V3-fixture'
        media[0].update(state='ready', mimeType='audio/silk', byteLength=len(data), dataBase64=base64.b64encode(data).decode())
        with patch.object(c, 'decode_silk', return_value=b'RIFFfixture-WAV'):
            c.ingest(self.db, self.config, batch, decoder)
        self.assertEqual(self.db.execute('SELECT body FROM messages').fetchone()[0], original)
        resolved = [json.loads(row[0])['metadata'] for row in self.db.execute('SELECT body FROM assets ORDER BY ordinal')]
        self.assertEqual([asset['id'] for asset in resolved], [slot['id'] for slot in slots])
        self.assertEqual(resolved[1]['mimeType'], 'audio/wav')

    def test_policy_drop_is_not_acknowledged(self):
        c.ingest(self.db, self.config, self.batch())
        with self.assertRaisesRegex(c.CollectorError, 'relay_policy_paused'):
            c.upload(self.db, self.config, lambda *args: {'dropped': True})
        self.assertEqual(self.db.execute('SELECT uploaded FROM messages').fetchone()[0], 0)

    def test_new_text_is_acknowledged_before_slow_old_media_retry(self):
        pending = self.batch([{'kind': 'image', 'mimeType': 'application/octet-stream', 'state': 'pending'}])
        c.ingest(self.db, self.config, pending)
        incoming = self.batch()
        incoming['messages'][0].update(msgId=2, msgSvrId='124')
        outgoing = self.batch()
        outgoing['messages'][0].update(msgId=3, msgSvrId='125', isSend=1)
        new = {**incoming, 'messages': incoming['messages'] + outgoing['messages']}
        reads = []
        def read(command, after, ids=None, **options):
            reads.append(ids)
            if ids is None:
                return new
            self.assertEqual(ids, [1])
            self.assertEqual(self.db.execute('SELECT sum(uploaded) FROM messages').fetchone()[0], 2)
            return pending
        def request(config, path, body):
            message = json.loads(body)
            return {'id': message['id'], 'idempotent': False}
        upload = c.upload
        with patch.object(c, 'read_source', side_effect=read), patch.object(c, 'upload', side_effect=lambda db, config: upload(db, config, send=request)), patch('reply_executor.executor_ready') as probe:
            c.run(self.db, self.config, ['fixture-reader'], True, None, reply_executor=Path('/fixture-helper'))
        self.assertEqual(reads, [None, None, [1]])
        probe.assert_not_called()
        self.assertEqual(self.db.execute('SELECT status FROM sources WHERE source_id=1').fetchone()[0], 'waiting_original_download')

    def test_unconfirmed_ack_is_not_marked_uploaded(self):
        c.ingest(self.db, self.config, self.batch())
        with self.assertRaisesRegex(c.CollectorError, 'message_ack_not_confirmed'):
            c.upload(self.db, self.config, lambda *args: {})
        self.assertEqual(self.db.execute('SELECT uploaded FROM messages').fetchone()[0], 0)

    def test_deferred_media_preserves_cursor_without_fabricating_message(self):
        batch = self.batch()
        batch['messages'][0].update(type=3, mediaDeferred=True)
        c.ingest(self.db, self.config, batch)
        self.assertEqual(c.setting(self.db, 'cursor'), '1')
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT status FROM sources').fetchone()[0], 'waiting_original_download')
        # The full reader later supplies the same source identity.
        batch['messages'][0].pop('mediaDeferred')
        batch['messages'][0].update(type=1)
        c.ingest(self.db, self.config, batch)
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)

    def test_event_text_upload_does_not_wait_for_inflight_media_read(self):
        blocked = threading.Event()
        released = threading.Event()
        uploaded = threading.Event()
        event = threading.Event()
        dirty = threading.Event()
        class StopLoop(BaseException):
            pass
        class Wake:
            def __enter__(self):
                self.dirty = dirty
                self.media_dirty = threading.Event()
                self.wake = self
                dirty.set()
                return self
            def __exit__(self, *_):
                released.set()
            def changed(self):
                dirty.set()
                event.set()
            def clear(self):
                event.clear()
            def set(self):
                event.set()
            def wait(self, timeout=None):
                if uploaded.is_set():
                    raise StopLoop()
                if not event.wait(2):
                    raise AssertionError('new text did not wake the collector')
        pending = self.batch()
        pending['messages'][0].update(type=3, mediaDeferred=True)
        incoming = self.batch()
        incoming['messages'][0].update(msgId=2, msgSvrId='124')
        incoming['messages'][0].update(talker='fixture-private', replyIdentity={'username': 'fixture-private', 'contact': {'username': 'fixture-private', 'alias': 'fixture-alias'}})
        self.config['kI2A'] = c.encoded(self.key)
        reads = []
        def read(command, after, **options):
            self.assertEqual(options, {'include_media': False, 'defer_media': True})
            reads.append(after)
            return pending if after == 0 else incoming
        def media(*args):
            self.assertEqual(args[1], [1])
            blocked.set()
            dirty.set()
            event.set()
            if not released.wait(3):
                raise AssertionError('media blocked the new text upload')
            return None, False
        def send(config, path, body):
            self.assertTrue(blocked.is_set())
            self.assertFalse(released.is_set())
            message = json.loads(body)
            self.assertTrue(message['replyCapable'])
            uploaded.set()
            released.set()
            return {'id': message['id'], 'idempotent': False}
        upload = c.upload
        original_background = c.background_read
        def background(command, ids, executor, has_key, *image_options):
            return media(command, ids, executor, has_key) if ids else original_background(command, ids, executor, has_key)
        with patch.object(c, 'SourceWake', return_value=Wake()), patch.object(c, 'read_source', side_effect=read), patch.object(c, 'background_read', side_effect=background), patch('reply_executor.executor_ready', return_value=True) as probe, patch.object(c, 'upload', side_effect=lambda db, config, **options: upload(db, config, send=send, **options)):
            with self.assertRaises(StopLoop):
                c.run_events(self.db, self.config, ['fixture-reader'], None, None, Path('/fixture-helper'))
        probe.assert_called_once()
        self.assertEqual(reads[:2], [0, 1])
        self.assertTrue(uploaded.is_set())
        self.assertEqual(self.db.execute('SELECT sum(uploaded) FROM messages').fetchone()[0], 1)

    def test_failed_asset_has_backoff_and_does_not_starve_media(self):
        data = b'fixture'
        c.ingest(self.db, self.config, self.batch([{'kind': 'file', 'mimeType': 'application/octet-stream', 'state': 'ready', 'byteLength': len(data), 'dataBase64': base64.b64encode(data).decode()}]))
        self.db.execute('UPDATE messages SET uploaded=1')
        self.db.commit()
        class StopLoop(BaseException):
            pass
        class Event(threading.Event):
            def wait(self, timeout=None):
                return False
        class Wake:
            def __enter__(self):
                self.dirty, self.wake = Event(), Event()
                self.media_dirty = Event()
                self.media_dirty.set()
                self.dirty.set()
                return self
            def __exit__(self, *_):
                pass
            def retry(self, **flags):
                pass
        jobs = []
        class Worker:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def submit(self, operation, *args):
                jobs.append(operation)
                result = Future()
                result.set_exception(c.CollectorError('fixture_asset_failed') if operation == c.background_assets else StopLoop())
                return result
        empty = {'accountFingerprint': 'a' * 64, 'sourceMaxId': 1, 'messages': []}
        with patch.object(c, 'SourceWake', return_value=Wake()), patch.object(c, 'ThreadPoolExecutor', return_value=Worker()), patch.object(c, 'read_source', return_value=empty), patch.object(c.time, 'monotonic', return_value=100):
            with self.assertRaises(StopLoop):
                c.run_events(self.db, self.config, ['fixture-reader'], None, None, None)
        self.assertEqual(jobs, [c.background_assets, c.background_read])

    def test_blocked_old_download_does_not_hold_new_ready_asset_ack(self):
        blocked, released, uploaded = threading.Event(), threading.Event(), threading.Event()
        class StopLoop(BaseException):
            pass
        class Wake:
            def __enter__(self):
                self.dirty, self.media_dirty = threading.Event(), threading.Event()
                self.dirty.set()
                self.wake = threading.Event()
                return self
            def __exit__(self, *_):
                released.set()
            def retry(self, **flags):
                pass
            def changed(self):
                self.dirty.set()
                self.wake.set()
        wake = Wake()
        data = b'new-original'
        old = self.batch([{'kind': 'image', 'mimeType': 'application/octet-stream', 'state': 'pending'}])
        old['sourceMaxId'] = 1
        old['messages'][0].update(type=3, content='<msg><img length="12" /></msg>')
        new = self.batch([{'kind': 'image', 'mimeType': 'image/jpeg', 'state': 'ready',
                           'byteLength': len(data), 'dataBase64': base64.b64encode(data).decode()}])
        new['sourceMaxId'] = 2
        new['messages'][0].update(msgId=2, msgSvrId='124', type=3)
        def read(command, after, ids=None, **options):
            batch = old if ids == [1] or (ids is None and after == 0) else new
            if ids is None:
                batch = {**batch, 'messages': [{**batch['messages'][0], 'mediaDeferred': True}]}
            return batch
        def download(*args, **options):
            self.assertEqual(args[0]['messages'][0]['msgId'], 1)
            blocked.set()
            wake.changed()
            if not released.wait(3):
                raise AssertionError('new ready asset blocked behind old download')
            return 1, [], True
        def send(config, path, body):
            if '/assets/' in path:
                self.assertTrue(blocked.is_set())
                self.assertFalse(released.is_set())
                uploaded.set()
                released.set()
                return {'id': path.rsplit('/', 1)[1], 'idempotent': False}
            return {'id': json.loads(body)['id'], 'idempotent': False}
        original_upload, original_assets = c.upload, c.upload_assets
        def report(db):
            if uploaded.is_set():
                raise StopLoop()
            return {}
        with patch.object(c, 'SourceWake', return_value=wake), patch.object(c, 'read_source', side_effect=read), \
                patch('image_download.download_pending', side_effect=download), patch.object(c, 'status', side_effect=report), \
                patch.object(c, 'upload', side_effect=lambda db, config, **options: original_upload(db, config, send=send, **options)), \
                patch.object(c, 'upload_assets', side_effect=lambda db, config, **options: original_assets(db, config, send=send, **options)):
            with self.assertRaises(StopLoop):
                c.run_events(self.db, self.config, ['fixture'], None, None, Path('/fixture'))
        self.assertTrue(uploaded.is_set())
        self.assertEqual(self.db.execute('SELECT sum(uploaded) FROM assets').fetchone()[0], 1)

    def test_successful_asset_backlog_alternates_with_media_reads(self):
        data = b'fixture'
        media = {'kind': 'file', 'mimeType': 'application/octet-stream', 'state': 'ready',
                 'byteLength': len(data), 'dataBase64': base64.b64encode(data).decode()}
        c.ingest(self.db, self.config, self.batch([media, media, media]))
        with self.db:
            self.db.execute('UPDATE messages SET uploaded=1')
        class StopLoop(BaseException):
            pass
        class Wake:
            def __enter__(self):
                self.dirty, self.media_dirty, self.wake = threading.Event(), threading.Event(), self
                self.media_dirty.set()
                return self
            def __exit__(self, *_):
                pass
            def clear(self):
                pass
            def set(self):
                pass
            def wait(self, timeout=None):
                pass
        wake, jobs = Wake(), []
        class Worker:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def submit(self, operation, *args):
                jobs.append(operation)
                result = Future()
                if len(jobs) == 5:
                    result.set_exception(StopLoop())
                elif operation == c.background_assets:
                    operation(*args)
                    wake.media_dirty.set()
                    result.set_result(None)
                else:
                    result.set_result((None, False))
                return result
        def send(config, path, body):
            return {'id': path.rsplit('/', 1)[1], 'idempotent': False}
        original = c.upload_assets
        with patch.object(c, 'SourceWake', return_value=wake), patch.object(c, 'ThreadPoolExecutor', return_value=Worker()), \
                patch.object(c, 'upload_assets', side_effect=lambda db, config, **options: original(db, config, send=send, **options)):
            with self.assertRaises(StopLoop):
                c.run_events(self.db, self.config, ['fixture'], None, None, None)
        self.assertEqual(jobs, [c.background_assets, c.background_read, c.background_assets, c.background_read, c.background_assets])
        self.assertEqual(self.db.execute('SELECT sum(uploaded) FROM assets').fetchone()[0], 2)

    def test_download_completion_reads_exact_source_and_rejects_changed_account(self):
        old = self.batch([{'kind': 'image', 'mimeType': 'application/octet-stream', 'state': 'pending'}])
        old['sourceMaxId'] = 1
        old['messages'][0].update(type=3, content='<msg><img length="12" /></msg>')
        c.ingest(self.db, self.config, old)
        changed = {**old, 'accountFingerprint': 'b' * 64}
        class StopLoop(BaseException):
            pass
        retries, reads = [], []
        class Wake:
            def __enter__(self):
                self.dirty, self.media_dirty, self.wake = threading.Event(), threading.Event(), self
                self.media_dirty.set()
                return self
            def __exit__(self, *_):
                pass
            def clear(self):
                pass
            def set(self):
                pass
            def wait(self, timeout=None):
                if retries:
                    raise StopLoop()
            def retry(self, **flags):
                retries.append(flags)
        class Worker:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def submit(self, operation, *args, **options):
                result = Future()
                if operation == c.background_read:
                    reads.append(args[1])
                    result.set_result((old if len(reads) == 1 else changed, False))
                else:
                    result.set_result((1, [], False))
                return result
        with patch.object(c, 'SourceWake', return_value=Wake()), patch.object(c, 'ThreadPoolExecutor', return_value=Worker()):
            with self.assertRaises(StopLoop):
                c.run_events(self.db, self.config, ['fixture'], None, None, Path('/fixture'))
        self.assertEqual(reads, [[1], [1]])
        self.assertEqual(retries, [{'media': True}])
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        self.assertEqual(c.setting(self.db, 'account'), 'a' * 64)

    def test_idle_collector_never_schedules_another_source_scan(self):
        class StopLoop(BaseException):
            pass
        class Wake:
            def __enter__(self):
                self.dirty, self.media_dirty = threading.Event(), threading.Event()
                self.dirty.set()
                self.wake = self
                return self
            def __exit__(self, *_):
                pass
            def clear(self):
                pass
            def wait(self, timeout=None):
                self.timeout = timeout
                raise StopLoop()
        wake = Wake()
        empty = {'accountFingerprint': 'a' * 64, 'sourceMaxId': 0, 'messages': []}
        with patch.object(c, 'SourceWake', return_value=wake), patch.object(c, 'read_source', return_value=empty) as read, patch.object(c, 'background_read', return_value=(None, False)) as media:
            with self.assertRaises(StopLoop):
                c.run_events(self.db, self.config, ['fixture-reader'], None, None, None)
        self.assertIsNone(wake.timeout)
        read.assert_called_once()
        media.assert_called_once_with(['fixture-reader'], [], None, False)

    def test_media_only_event_does_not_trigger_text_scan(self):
        wake = c.SourceWake(['fixture-reader'])
        wake.changed(source=False, media=True)
        self.assertFalse(wake.dirty.is_set())
        self.assertTrue(wake.media_dirty.is_set())
        self.assertTrue(wake.wake.is_set())
        wake.wake.clear()
        wake.changed(source=False, media=False)
        self.assertFalse(wake.wake.is_set())

    def test_one_media_event_drains_all_pending_batches_once(self):
        with self.db:
            self.db.executemany('INSERT INTO sources VALUES(?,?,?,?,?)',
                                [(value, str(value), str(value), 'waiting_original_download', 0) for value in range(1, 26)])
        class StopLoop(BaseException):
            pass
        class Wake:
            def __enter__(self):
                self.dirty, self.media_dirty = threading.Event(), threading.Event()
                self.media_dirty.set()
                self.wake = self
                return self
            def __exit__(self, *_):
                pass
            def clear(self):
                pass
            def set(self):
                pass
            def wait(self, timeout=None):
                if len(reads) == 2:
                    raise StopLoop()
        reads = []
        class Worker:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def submit(self, operation, *args):
                reads.append(args[1])
                result = Future()
                result.set_result((None, False))
                return result
        with patch.object(c, 'SourceWake', return_value=Wake()), patch.object(c, 'ThreadPoolExecutor', return_value=Worker()), patch.object(c, 'read_source') as read:
            with self.assertRaises(StopLoop):
                c.run_events(self.db, self.config, ['fixture'], None, None, None)
        self.assertEqual(reads, [list(range(25, 5, -1)), list(range(5, 0, -1))])
        read.assert_not_called()

    def test_foreground_failure_preserves_completed_background_result(self):
        class StopLoop(BaseException):
            pass
        retries = []
        class Wake:
            def __enter__(self):
                self.dirty, self.media_dirty = threading.Event(), threading.Event()
                self.media_dirty.set()
                self.wake = self
                return self
            def __exit__(self, *_):
                pass
            def clear(self):
                pass
            def set(self):
                pass
            def wait(self, timeout=None):
                if retries:
                    self.dirty.clear()
                else:
                    self.dirty.set()
            def retry(self, **flags):
                retries.append(flags)
        class Worker:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def submit(self, *args):
                result = Future()
                result.set_exception(StopLoop())
                return result
        with patch.object(c, 'SourceWake', return_value=Wake()), patch.object(c, 'ThreadPoolExecutor', return_value=Worker()), patch.object(c, 'read_source', side_effect=c.CollectorError('fixture_read_failed')):
            with self.assertRaises(StopLoop):
                c.run_events(self.db, self.config, ['fixture'], None, None, None)
        self.assertEqual(retries, [{'source': True}])

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
