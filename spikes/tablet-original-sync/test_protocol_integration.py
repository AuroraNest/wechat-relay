"""Opt-in cross-language contract test against a disposable Node/MySQL server."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import wave

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import collector as c
import reply_executor as replies


ORIGIN = os.environ.get('AWR_INTEGRATION_ORIGIN', '').rstrip('/')
TOKEN = os.environ.get('AWR_INTEGRATION_TEST_TOKEN') or os.environ.get('TEST_TOKEN', '')


@unittest.skipUnless(ORIGIN and TOKEN, 'disposable integration server is not configured')
class ProtocolIntegrationTests(unittest.TestCase):
    def api(self, method, path, session=None, pair=None, value=None, expected=200):
        headers = {'Origin': ORIGIN, 'Content-Type': 'application/json'}
        if method == 'POST':
            headers['X-AWR-Test-Token'] = TOKEN
        if session:
            headers['X-AWR-Ack-Token'] = session
        if pair:
            headers['X-AWR-Pair-Id'] = pair
        req = urllib.request.Request(ORIGIN + path, headers=headers, method=method,
                                     data=c.json_bytes(value) if value is not None else None)
        try:
            response = urllib.request.build_opener(c.NoRedirect).open(req, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            self.assertEqual(response.status, expected, 'unexpected authenticated HTTP status')
            return json.loads(response.read(16 * 1024 * 1024))

    def paired_device(self):
        session = self.api('POST', '/api/v1/ios/sessions', value={}, expected=201)['ackToken']
        pairing = self.api('POST', '/api/v1/pairings', session=session, value={}, expected=201)
        key = ec.generate_private_key(ec.SECP256R1())
        config = {'origin': ORIGIN, 'pairId': pairing['pairId'],
                  'deviceId': c.encoded(secrets.token_bytes(16)),
                  'kA2I': c.encoded(secrets.token_bytes(32)),
                  'privateKey': key.private_bytes(serialization.Encoding.PEM,
                      serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}
        result = c.request(config, '/api/v1/android/pair', c.json_bytes({
            'pairId': pairing['pairId'], 'pairSecret': pairing['pairSecret'],
            'deviceId': config['deviceId'],
            'publicKey': c.encoded(key.public_key().public_bytes(serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo))}), signed=False)
        self.assertIs(result.get('paired'), True)
        return session, config

    def decrypt(self, config, envelope):
        return AESGCM(c.decoded(config['kA2I'])).decrypt(
            c.decoded(envelope['iv']), c.decoded(envelope['ct']), envelope['aad'].encode())

    def test_contacts_without_new_messages_use_existing_encrypted_snapshot_api(self):
        session, config = self.paired_device()
        foreign_session, foreign = self.paired_device()
        batch = {'accountFingerprint': 'a' * 64, 'messages': [], 'contactSnapshot': {
            'state': 'ready', 'contacts': [
                {'name': 'Private fixture B', 'conversationId': 'fixture_b', 'alias': 'alias_b'},
                {'name': 'Private fixture A', 'conversationId': 'fixture_a', 'alias': 'alias_a'}]}}
        with tempfile.TemporaryDirectory(prefix='awr-contacts-integration-') as directory:
            db = c.open_store(Path(directory))
            self.addCleanup(db.close)
            c.ingest(db, config, batch)
            queued = json.loads(c.setting(db, 'contacts_state'))['pending']
            body = c.json_bytes(queued)
            self.assertNotIn(b'Private fixture', body)
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
            c.upload_contacts(db, config)
            self.assertNotIn('pending', json.loads(c.setting(db, 'contacts_state')))
            snapshots = self.api('GET', '/api/v1/ios/contacts', session, config['pairId'])['snapshots']
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]['v'], 3)
            self.assertEqual(snapshots[0]['id'], queued['id'])
            self.assertEqual(json.loads(self.decrypt(config, snapshots[0]['contactsEnvelope'])), {
                'v': 3, 'accountFingerprint': 'a' * 64, 'contacts': [
                    {'name': 'Private fixture A', 'conversationId': 'fixture_a', 'alias': 'alias_a'},
                    {'name': 'Private fixture B', 'conversationId': 'fixture_b', 'alias': 'alias_b'}]})
            self.assertTrue(c.request(config, '/api/v1/android/contacts', body)['idempotent'])
            c.ingest(db, config, batch)
            self.assertNotIn('pending', json.loads(c.setting(db, 'contacts_state')))
            self.assertEqual(self.api('GET', '/api/v1/ios/contacts', foreign_session, foreign['pairId'])['snapshots'], [])
            batch['contactSnapshot']['contacts'] = []
            c.ingest(db, config, batch)
            c.upload_contacts(db, config)
            snapshots = self.api('GET', '/api/v1/ios/contacts', session, config['pairId'])['snapshots']
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(json.loads(self.decrypt(config, snapshots[0]['contactsEnvelope']))['contacts'], [])

    def test_contact_bootstrap_and_send_cross_language_without_history(self):
        session, config = self.paired_device()
        reply_key = bytes(range(32))
        # Source and UI are synthetic; only the encrypted Node transport is live.
        source = {'accountFingerprint': 'c' * 64, 'sourceMaxId': 0, 'messages': [],
                  'contactSnapshot': {'state': 'ready', 'contacts': [
                      {'name': 'Same name', 'conversationId': 'fixture_a', 'alias': 'alias_a'},
                      {'name': 'Same name', 'conversationId': 'fixture_b', 'alias': 'alias_b'}]}}
        executions = []
        outgoing = []
        with tempfile.TemporaryDirectory(prefix='awr-contact-replies-integration-') as directory:
            state = Path(directory)
            db = c.open_store(state)
            self.addCleanup(db.close)
            c.ingest(db, config, source)
            snapshot_id = json.loads(c.setting(db, 'contacts_state'))['snapshotId']
            c.upload_contacts(db, config)
            def enqueue(version, plaintext):
                command = {'v': version, 'id': c.uuid7(), 'pairId': config['pairId'], 'deviceId': config['deviceId'],
                           'targetContactSnapshotId': snapshot_id, 'createdAt': c.now_ms(), 'wechatUserId': 0}
                key = replies.bootstrap_key(config, c.decoded) if version == 8 else reply_key
                command['replyEnvelope'] = c.seal(c.json_bytes(plaintext), key,
                    'phase2-reply-bootstrap' if version == 8 else 'phase1-reply', replies.command_aad(command, config))
                result = self.api('POST', '/api/v1/replies', session, config['pairId'],
                                  {key: value for key, value in command.items() if key != 'pairId'}, expected=201)
                self.assertEqual(result['replyId'], command['id'])
                return command
            def read(command, after, ids=None, include_media=True, defer_media=False):
                self.assertFalse(include_media)
                if defer_media:
                    return source
                return {**source, 'sourceMaxId': 1 if outgoing else 0,
                        'messages': outgoing if ids == [1] or after < 1 else []}
            def execute(executor, command):
                executions.append(command)
                clicked_at = c.now_ms()
                outgoing.append({'msgId': 1, 'msgSvrId': '101', 'isSend': 1, 'createTime': clicked_at,
                                 'talker': command['conversationId'], 'content': command['body']})
                return {'status': 'UI_ACCEPTED', 'clicked': True, 'sourceMaxId': 0, 'clickedAt': clicked_at}
            def consume():
                replies.poll(db, config, state / 'device.json', ['synthetic-reader'], Path('/synthetic-ui'),
                             c.request, read, c.save_config, c.decoded, c.encoded, c.json_bytes,
                             execute=execute, sleep=lambda seconds: None)
            bootstrap = enqueue(8, {'v': 1, 'pairId': config['pairId'], 'deviceId': config['deviceId'],
                                    'replyKey': c.encoded(reply_key)})
            consume()
            self.assertEqual(c.decoded(config['kI2A']), reply_key)
            self.assertFalse(executions)
            self.assertEqual(self.api('GET', '/api/v1/replies/' + bootstrap['id'], session, config['pairId'])['status'], 'REPLY_KEY_INSTALLED')
            send = enqueue(7, {'body': 'Synthetic contact send 👋', 'conversationId': 'fixture_a',
                               'accountFingerprint': source['accountFingerprint']})
            consume()
            self.assertEqual(self.api('GET', '/api/v1/replies/' + send['id'], session, config['pairId'])['status'], 'SENT_TO_WECHAT')
            self.assertEqual(len(executions), 1)
            self.assertEqual(executions[0]['sourceContactSnapshotId'], snapshot_id)
            self.assertNotIn('sourceMessageId', executions[0])
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_unknown_record_slots_arrive_before_independent_originals(self):
        from xml.sax.saxutils import escape
        session, config = self.paired_device()
        foreign_session, foreign = self.paired_device()
        record = '<recordinfo><dataitem datatype="1"><datadesc>text before media</datadesc></dataitem><dataitem datatype="2" dataid="photo"/><dataitem datatype="37" dataid="emoji"/></recordinfo>'
        media = [{'kind': kind, 'mimeType': 'application/octet-stream', 'byteLength': 0,
                  'state': 'pending', 'recordItemIndex': index}
                 for index, kind in enumerate(('image', 'sticker'), 1)]
        batch = {'accountFingerprint': 'a' * 64, 'messages': [{
            'msgId': 1, 'msgSvrId': '123456789012345689', 'type': 49, 'createTime': c.now_ms(),
            'isSend': 0, 'talker': 'private-fixture-room@chatroom',
            'content': '<msg><appmsg><type>19</type><recorditem>' + escape(record) + '</recorditem></appmsg></msg>',
            'media': media}]}
        with tempfile.TemporaryDirectory(prefix='awr-v8-integration-') as directory:
            db = c.open_store(Path(directory))
            self.addCleanup(db.close)
            c.ingest(db, config, batch)
            original = db.execute('SELECT body FROM messages').fetchone()[0]
            message = json.loads(original)
            c.upload(db, config)
            content_path = f"/api/v1/ios/messages/{message['id']}/content"
            content = self.api('GET', content_path, session, config['pairId'])
            self.assertEqual(json.loads(self.decrypt(config, content['contentEnvelope']))['records'][0]['text'], 'text before media')
            self.assertEqual(content['nativeAssetSlots'], message['nativeAssetSlots'])
            self.assertEqual(content['nativeAssets'], [])
            first, second = message['nativeAssetSlots']
            for slot in (first, second):
                self.api('GET', '/api/v1/ios/assets/' + slot['id'], session, config['pairId'], expected=409)
            data = b'GIF89a-integration-exact-record-original'
            media[1].update(state='ready', mimeType='image/gif', byteLength=len(data),
                            dataBase64=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest())
            c.ingest(db, config, batch)
            c.upload(db, config)
            after = self.api('GET', content_path, session, config['pairId'])
            self.assertEqual(after['contentEnvelope'], content['contentEnvelope'])
            self.assertEqual(after['nativeAssetSlots'], content['nativeAssetSlots'])
            self.assertEqual([asset['id'] for asset in after['nativeAssets']], [second['id']])
            self.api('GET', '/api/v1/ios/assets/' + first['id'], session, config['pairId'], expected=409)
            resolved = self.api('GET', '/api/v1/ios/assets/' + second['id'], session, config['pairId'])
            self.assertEqual(self.decrypt(config, resolved['envelope']), data)
            self.assertIn('|8|' + config['pairId'] + '|', resolved['envelope']['aad'])
            self.api('GET', content_path, foreign_session, foreign['pairId'], expected=404)
            self.api('GET', '/api/v1/ios/assets/' + second['id'], foreign_session, foreign['pairId'], expected=404)
            body = db.execute('SELECT body FROM assets WHERE id=?', (second['id'],)).fetchone()[0]
            upload_path = f"/api/v1/android/messages/{message['id']}/assets/{second['id']}"
            self.assertTrue(c.request(config, upload_path, body)['idempotent'])
            changed = json.loads(body)
            changed['metadata']['mimeType'] = 'image/png'
            changed['envelope'] = c.seal(data, c.decoded(config['kA2I']), 'phase2-asset',
                                         c.asset_aad(message, changed['metadata'], config['pairId']))
            with self.assertRaisesRegex(c.CollectorError, '^http_409$'):
                c.request(config, upload_path, c.json_bytes(changed))
            with self.assertRaisesRegex(c.CollectorError, '^http_404$'):
                c.request(foreign, upload_path, body)
            self.assertEqual(db.execute('SELECT body FROM messages').fetchone()[0], original)
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)

    def test_hd_preview_arrives_before_original_in_the_same_message(self):
        session, config = self.paired_device()
        full, preview = b'\xff\xd8\xff-full-HD-fixture', b'\xff\xd8\xff-preview'
        row = {'msgId': 1, 'msgSvrId': '123456789012345688', 'type': 3, 'createTime': c.now_ms(),
               'isSend': 0, 'talker': 'fixture@chatroom', 'mediaDeferred': True,
               'content': '<msg><img hdlength="' + str(len(full)) + '" md5="' + hashlib.md5(full).hexdigest() + '" /></msg>'}
        batch = {'accountFingerprint': 'a' * 64, 'messages': [row]}
        with tempfile.TemporaryDirectory(prefix='awr-preview-integration-') as directory:
            db = c.open_store(Path(directory))
            self.addCleanup(db.close)
            c.ingest(db, config, batch)
            c.upload(db, config)
            message = json.loads(db.execute('SELECT body FROM messages').fetchone()[0])
            original, derived = message['nativeAssetSlots']
            path = '/api/v1/ios/messages/' + message['id'] + '/content'
            before = self.api('GET', path, session, config['pairId'])
            self.assertEqual(before['nativeAssets'], [])
            row.pop('mediaDeferred')
            row['media'] = [{'kind': 'image', 'state': 'pending', 'mimeType': 'application/octet-stream', 'byteLength': len(full)}]
            row['imagePreview'] = {'kind': 'image', 'state': 'ready', 'mimeType': 'image/jpeg', 'byteLength': len(preview),
                                   'dataBase64': base64.b64encode(preview).decode(), 'sha256': hashlib.sha256(preview).hexdigest()}
            c.ingest(db, config, batch)
            c.upload(db, config)
            first = self.api('GET', path, session, config['pairId'])
            self.assertEqual([asset['id'] for asset in first['nativeAssets']], [derived['id']])
            self.api('GET', '/api/v1/ios/assets/' + original['id'], session, config['pairId'], expected=409)
            asset = self.api('GET', '/api/v1/ios/assets/' + derived['id'], session, config['pairId'])
            self.assertEqual(self.decrypt(config, asset['envelope']), preview)
            self.assertEqual(asset['role'], 'playback')
            row['media'][0].update(state='ready', mimeType='image/jpeg', dataBase64=base64.b64encode(full).decode(),
                                   sha256=hashlib.sha256(full).hexdigest())
            row.pop('imagePreview')
            c.ingest(db, config, batch)
            c.upload(db, config)
            after = self.api('GET', path, session, config['pairId'])
            self.assertEqual(len(after['nativeAssets']), 2)
            self.assertEqual(after['contentEnvelope'], before['contentEnvelope'])
            self.assertEqual(after['nativeAssetSlots'], before['nativeAssetSlots'])
            original_asset = self.api('GET', '/api/v1/ios/assets/' + original['id'], session, config['pairId'])
            self.assertEqual(self.decrypt(config, original_asset['envelope']), full)
            listing = self.api('GET', '/api/v1/messages', session, config['pairId'])
            self.assertEqual(len(listing['messages']), 1)

    def test_python_collector_to_mysql_and_authenticated_native_download(self):
        session, config = self.paired_device()
        other_session, other_config = self.paired_device()
        original_audio = b'\x02#!SILK_V3 integration original bytes\x00\xff\x10'
        file_bytes = b'%PDF-1.4 integration private original file\x00\xff\x01'
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(b'\x00\x00' * 240)
        playback_bytes = wav_buffer.getvalue()
        full_text = '完整跨语言文本, 保留全部字符. ' * 100
        raw_xml = '<msg><appmsg><type>6</type><title>private-fixture-file.pdf</title><des>完整文件说明</des></appmsg></msg>'
        audio = {'kind': 'audio', 'mimeType': 'audio/silk', 'byteLength': len(original_audio),
                 'name': 'private-fixture-audio.silk', 'state': 'ready',
                 'sha256': hashlib.sha256(original_audio).hexdigest(),
                 'dataBase64': base64.b64encode(original_audio).decode()}
        delayed_file = {'kind': 'file', 'mimeType': 'application/pdf', 'byteLength': len(file_bytes),
                        'state': 'pending', 'sha256': hashlib.sha256(file_bytes).hexdigest()}
        base = {'createTime': c.now_ms(), 'isSend': 0, 'talker': 'private-fixture-room@chatroom',
                'conversationName': 'Private fixture room', 'senderName': 'Private fixture sender'}
        batch = {'accountFingerprint': hashlib.sha256(b'private fixture account').hexdigest(),
                 'messages': [dict(base, msgId=1, msgSvrId='123456789012345678', type=1,
                                   content='private-fixture-member:\n' + full_text, media=[]),
                              dict(base, msgId=2, msgSvrId='123456789012345679', type=34,
                                   content='private audio source marker', media=[audio]),
                              dict(base, msgId=3, msgSvrId='123456789012345680', type=49,
                                   content=raw_xml, media=[delayed_file])]}
        with tempfile.TemporaryDirectory(prefix='awr-python-integration-') as directory:
            db = c.open_store(Path(directory))
            self.addCleanup(db.close)
            # The decoder fixture isolates transport; this does not claim SILK codec verification.
            with patch.object(c, 'decode_silk', return_value=playback_bytes):
                c.ingest(db, config, batch, decoder=Path('/fixture/decoder'))
            bodies = db.execute('SELECT source_id,body FROM messages ORDER BY seq').fetchall()
            self.assertEqual(len(bodies), 3)
            messages = {source_id: json.loads(body) for source_id, body in bodies}
            c.upload(db, config)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages WHERE uploaded=1').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM assets WHERE uploaded=1').fetchone()[0], 2)

            listing = self.api('GET', '/api/v1/messages', session, config['pairId'])
            self.assertEqual(len(listing['messages']), 3)
            for listed in listing['messages']:
                self.assertIs(listed['hasNativeContent'], True)
                self.assertNotIn('contentEnvelope', listed)
                self.assertIs(listed['replyCapable'], False)
                self.assertIs(listed['conversationSendCapable'], False)
                self.assertLessEqual(len(self.decrypt(config, listed['previewEnvelope'])), 600)

            contents = {}
            for source_id, message in messages.items():
                result = self.api('GET', f"/api/v1/ios/messages/{message['id']}/content", session, config['pairId'])
                contents[source_id] = json.loads(self.decrypt(config, result['contentEnvelope']))
                self.assertTrue(result['contentEnvelope'] == message['contentEnvelope'], 'content ciphertext changed')
            self.assertTrue(contents[1]['text'] == full_text, 'full text differs from original fixture')
            self.assertTrue(contents[1]['senderId'] == 'private-fixture-member', 'group sender was lost')
            self.assertTrue(contents[3]['rawXML'] == raw_xml, 'original XML was lost')
            self.assertTrue(contents[3]['attachments'][0]['name'] == 'private-fixture-file.pdf', 'original filename was lost')

            assets = messages[2]['nativeAssets']
            self.assertTrue([asset['role'] for asset in assets] == ['original', 'playback'])
            self.assertTrue(assets[1]['derivedFrom'] == assets[0]['id'])
            self.assertTrue({asset['id'] for asset in assets} == {item['assetId'] for item in contents[2]['attachments']})
            for asset, expected_bytes in zip(assets, [original_audio, playback_bytes]):
                result = self.api('GET', f"/api/v1/ios/assets/{asset['id']}", session, config['pairId'])
                self.assertTrue(self.decrypt(config, result['envelope']) == expected_bytes, 'downloaded asset bytes differ')
                self.assertEqual(result['byteLength'], len(expected_bytes))
                attachment = next(item for item in contents[2]['attachments'] if item['assetId'] == asset['id'])
                self.assertTrue(attachment['sha256'] == hashlib.sha256(expected_bytes).hexdigest(), 'attachment digest differs')

            file_asset = messages[3]['nativeAssets'][0]
            pending = self.api('GET', f"/api/v1/ios/assets/{file_asset['id']}", session, config['pairId'], expected=409)
            self.assertTrue(pending == {'error': 'ASSET_PENDING'})
            delayed_file.update(state='ready', dataBase64=base64.b64encode(file_bytes).decode())
            c.ingest(db, config, batch)
            c.upload(db, config)
            downloaded = self.api('GET', f"/api/v1/ios/assets/{file_asset['id']}", session, config['pairId'])
            self.assertTrue(self.decrypt(config, downloaded['envelope']) == file_bytes, 'late file bytes differ')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sources WHERE status='complete'").fetchone()[0], 3)

            # Reingestion and response-loss retry retain the original sealed request bytes.
            c.ingest(db, config, batch)
            self.assertTrue(db.execute('SELECT source_id,body FROM messages ORDER BY seq').fetchall() == bodies, 'duplicate source resealed a message')
            for source_id, body in bodies:
                acknowledgement = c.request(config, '/api/v1/android/messages', body)
                self.assertTrue(acknowledgement == {'id': messages[source_id]['id'], 'idempotent': True})
            asset_bodies = db.execute('SELECT id,message_id,body FROM assets ORDER BY message_id,ordinal').fetchall()
            for asset_id, message_id, body in asset_bodies:
                acknowledgement = c.request(config, f'/api/v1/android/messages/{message_id}/assets/{asset_id}', body)
                self.assertTrue(acknowledgement == {'id': asset_id, 'idempotent': True})

            first_id = messages[1]['id']
            self.api('GET', f'/api/v1/ios/messages/{first_id}/content', other_session, other_config['pairId'], expected=404)
            self.api('GET', f"/api/v1/ios/assets/{file_asset['id']}", other_session, other_config['pairId'], expected=404)
            self.api('GET', '/api/v1/messages', session, other_config['pairId'], expected=400)
            asset_id, message_id, asset_body = asset_bodies[0]
            with self.assertRaisesRegex(c.CollectorError, '^http_404$'):
                c.request(other_config, f'/api/v1/android/messages/{message_id}/assets/{asset_id}', asset_body)

            private_markers = [full_text.encode(), raw_xml.encode(), b'private-fixture-room@chatroom',
                               b'private-fixture-file.pdf', b'private-fixture-audio.silk',
                               batch['accountFingerprint'].encode(), config['privateKey'].encode(),
                               original_audio, file_bytes]
            transports = [body for _, body in bodies] + [body for _, _, body in asset_bodies] + [c.json_bytes(listing)]
            for transport in transports:
                self.assertTrue(all(marker not in transport for marker in private_markers), 'plaintext source data leaked into transport')
            event_log = os.environ.get('AWR_INTEGRATION_EVENT_LOG')
            if event_log:
                events = Path(event_log).read_bytes()
                self.assertTrue(all(marker not in events for marker in private_markers), 'plaintext source data leaked into server events')
            db.close()


if __name__ == '__main__':
    unittest.main(verbosity=0)
