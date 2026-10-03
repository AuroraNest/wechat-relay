#!/usr/bin/env python3
"""Read-only tablet collector. State is private; console output is aggregate only."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
import fcntl
import getpass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import unicodedata
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from audio_codec import AudioDecodeError, decode_silk

MAX_ASSET = 8 * 1024 * 1024
MAX_CONTENT = 1024 * 1024


class CollectorError(Exception):
    """Only fixed, non-sensitive error codes may be exposed to operators."""


def encoded(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def decoded(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise CollectorError('invalid_encoding')
    try:
        return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except ValueError:
        raise CollectorError('invalid_encoding') from None


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def now_ms():
    return time.time_ns() // 1_000_000


def uuid7():
    value = (now_ms() << 80) | (7 << 76) | (secrets.randbits(12) << 64)
    value |= (2 << 62) | secrets.randbits(62)
    return str(uuid.UUID(int=value))


def seal(data, key, kid, aad):
    iv = secrets.token_bytes(12)
    return {'alg': 'A256GCM', 'kid': kid, 'iv': encoded(iv), 'aad': aad,
            'ct': encoded(AESGCM(key).encrypt(iv, data, aad.encode()))}


def pairing_payload(code):
    if not code.startswith('AWR1:') or len(code) > 8192:
        raise CollectorError('invalid_pairing_code')
    try:
        value = json.loads(decoded(code[5:]))
        origin = urllib.parse.urlsplit(value['origin'])
        if (value['v'] != 1 or origin.scheme != 'https' or not origin.hostname
                or origin.username or origin.password or origin.query or origin.fragment
                or origin.path not in ('', '/') or origin.port not in (None, 443)
                or not isinstance(value['expiresAt'], int) or value['expiresAt'] <= now_ms()
                or len(decoded(value['kA2I'])) != 32 or len(decoded(value['kI2A'])) != 32
                or not isinstance(value['pairSecret'], str) or not value['pairSecret']):
            raise ValueError()
        value['pairId'] = str(uuid.UUID(value['pairId']))
        value['origin'] = urllib.parse.urlunsplit(('https', origin.netloc, '', '', ''))
        return value
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CollectorError('invalid_pairing_code') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CollectorError('redirect_rejected')


def require_relay_destination(url):
    try:
        destination = urllib.parse.urlsplit(url)
        if (destination.scheme != 'https' or destination.hostname != 'relay.auroramaple.com'
                or destination.port not in (None, 443) or destination.username is not None
                or destination.password is not None or destination.fragment):
            raise ValueError()
    except ValueError:
        raise CollectorError('https_socket_destination_rejected') from None


class UnixHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, socket_path, **kwargs):
        super().__init__(host, **kwargs)
        self.socket_path = socket_path

    def connect(self):
        if self.host != 'relay.auroramaple.com' or self.port != 443 or self._tunnel_host:
            raise CollectorError('https_socket_destination_rejected')
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.timeout)
            connection.connect(self.socket_path)
            # Keep the original hostname for certificate verification and TLS SNI.
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except BaseException:
            connection.close()
            raise


class UnixHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, socket_path):
        super().__init__()
        self.socket_path = socket_path

    def https_open(self, req):
        require_relay_destination(req.full_url)
        def connection(host, **kwargs):
            return UnixHTTPSConnection(host, self.socket_path, **kwargs)
        return self.do_open(connection, req, context=self._context)


def request(config, path, body, signed=True, method='POST'):
    if method not in ('GET', 'POST') or (method == 'GET' and body):
        raise CollectorError('invalid_request_method')
    socket_path = os.environ.get('AWR_RELAY_HTTPS_SOCKET')
    url = config['origin'] + path
    if socket_path is not None:
        require_relay_destination(url)
    headers = {'Content-Type': 'application/json'}
    if signed:
        timestamp, nonce = str(now_ms()), encoded(secrets.token_bytes(16))
        canonical = f'{method}\n{path}\n{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}'
        key = serialization.load_pem_private_key(config['privateKey'].encode(), password=None)
        signature = key.sign(canonical.encode(), ec.ECDSA(hashes.SHA256()))
        headers.update({'X-AWR-Device-Id': config['deviceId'], 'X-AWR-Timestamp': timestamp,
                        'X-AWR-Nonce': nonce, 'X-AWR-Signature': encoded(signature)})
    req = urllib.request.Request(url, data=body if method == 'POST' else None, headers=headers, method=method)
    started = time.monotonic()
    try:
        # Bound socket waits below the signature window; expired requests retain queued ciphertext.
        timeout = min(270, 60 + (len(body) + 32767) // 32768) if len(body) > 1_048_576 else 45
        handlers = (urllib.request.ProxyHandler({}), UnixHTTPSHandler(socket_path), NoRedirect) if socket_path is not None else (NoRedirect,)
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as response:
            data = response.read(65537)
            if len(data) > 65536:
                raise CollectorError('response_too_large')
            result = json.loads(data)
            if path == '/api/v1/android/messages' or re.fullmatch(r'/api/v1/android/messages/[^/]+/assets/[^/]+', path):
                print(json.dumps({'event': 'request_completed', 'kind': 'messages' if path.endswith('/messages') else 'asset',
                                  'bodyBytes': len(body), 'durationMs': round((time.monotonic() - started) * 1000)}), flush=True)
            return result
    except urllib.error.HTTPError as error:
        raise CollectorError(f'http_{error.code}') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        kind = 'network_timeout' if isinstance(reason, (TimeoutError, socket.timeout)) else 'invalid_response_json' if isinstance(reason, ValueError) else 'network_transport_error'
        print(json.dumps({'event': 'request_failed', 'kind': kind,
                          'errorClass': type(reason).__name__ if isinstance(reason, BaseException) else 'Unknown',
                          'bodyBytes': len(body), 'durationMs': round((time.monotonic() - started) * 1000)}), file=sys.stderr, flush=True)
        raise CollectorError(kind) from None


def save_config(path, value):
    temporary = path.with_suffix('.new')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def pair(state):
    path = state / 'device.json'
    if path.exists():
        config = json.loads(path.read_text())
        if config.get('paired'):
            print(json.dumps({'state': 'already_paired'}))
            return
    else:
        # getpass refuses an echoed fallback: pairing keys never enter history or chat.
        if not sys.stdin.isatty():
            raise CollectorError('pairing_requires_private_terminal')
        config = pairing_payload(getpass.getpass('粘贴 iPhone 平板来源的配对码(输入隐藏): '))
        key = ec.generate_private_key(ec.SECP256R1())
        config.update({'deviceId': encoded(secrets.token_bytes(16)), 'paired': False,
                       'privateKey': key.private_bytes(serialization.Encoding.PEM,
                           serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()})
        save_config(path, config)
    key = serialization.load_pem_private_key(config['privateKey'].encode(), password=None)
    body = {'pairId': config['pairId'], 'pairSecret': config['pairSecret'], 'deviceId': config['deviceId'],
            'publicKey': encoded(key.public_key().public_bytes(serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo))}
    result = request(config, '/api/v1/android/pair', json_bytes(body), signed=False)
    if result.get('paired') is not True:
        raise CollectorError('pairing_not_confirmed')
    config['paired'] = True
    config.pop('pairSecret', None)
    save_config(path, config)
    print(json.dumps({'state': 'paired'}))


def open_store(state):
    db = sqlite3.connect(state / 'outbox.sqlite')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(source_id INTEGER PRIMARY KEY, identity TEXT UNIQUE, server_id TEXT NOT NULL,
            status TEXT NOT NULL, retry_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, source_id INTEGER UNIQUE NOT NULL,
            seq INTEGER UNIQUE NOT NULL, body BLOB NOT NULL, uploaded INTEGER NOT NULL DEFAULT 0,
            source_hash TEXT);
        CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL, metadata TEXT NOT NULL, body BLOB,
            uploaded INTEGER NOT NULL DEFAULT 0, last_error TEXT);
    ''')
    if 'source_hash' not in {row[1] for row in db.execute('PRAGMA table_info(messages)')}:
        db.execute('ALTER TABLE messages ADD COLUMN source_hash TEXT')
    if 'last_error' not in {row[1] for row in db.execute('PRAGMA table_info(assets)')}:
        db.execute('ALTER TABLE assets ADD COLUMN last_error TEXT')
    os.chmod(state / 'outbox.sqlite', 0o600)
    return db


def setting(db, name, default=''):
    row = db.execute('SELECT value FROM settings WHERE name=?', (name,)).fetchone()
    return row[0] if row else default


def set_setting(db, name, value):
    db.execute('INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value', (name, str(value)))


def contacts_binding(config, fingerprint):
    return hashlib.sha256(json_bytes([config.get('pairId'), config['deviceId'], config['kA2I'], fingerprint])).hexdigest()


def queue_contacts(db, config, snapshot, fingerprint):
    if snapshot is None:
        return
    try:
        if not isinstance(snapshot, dict) or snapshot.get('state') != 'ready':
            reason = snapshot.get('reason') if isinstance(snapshot, dict) else None
            allowed = {'contacts_schema_unsupported', 'contacts_self_unavailable', 'contacts_too_many',
                       'contacts_identity_ambiguous', 'contacts_name_invalid', 'contacts_name_ambiguous'}
            raise CollectorError(reason if isinstance(reason, str) and reason in allowed else 'contacts_source_unavailable')
        contacts = snapshot.get('contacts')
        if not isinstance(contacts, list) or len(contacts) > 10_000:
            raise CollectorError('invalid_contacts_snapshot')
        from source_reader import CONTACT_SERVICES
        normalized, identities = [], set()
        for contact in contacts:
            if not isinstance(contact, dict) or set(contact) != {'name', 'conversationId', 'alias'}:
                raise CollectorError('invalid_contact_name')
            name = contact['name']
            if not isinstance(name, str) or not name or name != name.strip() or len(name.encode()) > 512:
                raise CollectorError('invalid_contact_name')
            identity, alias = contact['conversationId'], contact['alias']
            if (not isinstance(identity, str) or not identity or len(identity.encode()) > 512
                    or identity != identity.strip() or any(ord(char) < 32 for char in identity)
                    or '@' in identity or identity in CONTACT_SERVICES or identity in identities
                    or not isinstance(alias, str) or len(alias) > 128 or alias != alias.strip()
                    or any(ord(char) < 32 for char in alias)):
                raise CollectorError('contacts_identity_ambiguous')
            identities.add(identity)
            normalized.append({'name': unicodedata.normalize('NFC', name), 'conversationId': identity, 'alias': alias})
        if not isinstance(fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', fingerprint):
            raise CollectorError('contacts_identity_ambiguous')
        normalized.sort(key=lambda contact: contact['conversationId'])
        plaintext = json_bytes({'v': 3, 'accountFingerprint': fingerprint, 'contacts': normalized})
        if len(plaintext) > MAX_CONTENT:
            raise CollectorError('contacts_too_large')
        binding = contacts_binding(config, fingerprint)
        digest = hashlib.sha256(plaintext).hexdigest()
        with db:
            previous = json.loads(setting(db, 'contacts_state', '{}'))
            if previous and previous['binding'] != binding:
                raise CollectorError('contacts_identity_changed')
            if previous.get('digest') != digest:
                captured = max(now_ms(), previous.get('capturedAt', 0) + 1)
                body = {'v': 3, 'id': uuid7(), 'deviceId': config['deviceId'], 'wechatUserId': 0, 'capturedAt': captured}
                aad = f"AWR1|A2I_CONTACTS|3|{body['id']}|{body['deviceId']}|{captured}|0"
                body['contactsEnvelope'] = seal(plaintext, decoded(config['kA2I']), 'phase1-contacts', aad)
                set_setting(db, 'contacts_state', json_bytes({'binding': binding, 'digest': digest,
                    'capturedAt': captured, 'count': len(normalized), 'pending': body,
                    'snapshotId': body['id'], 'snapshot': {'v': 3, 'accountFingerprint': fingerprint,
                    'contacts': normalized}}).decode())
            set_setting(db, 'contacts_status', 'ready')
    except (CollectorError, UnicodeError) as error:
        with db:
            set_setting(db, 'contacts_status', str(error) if isinstance(error, CollectorError) else 'invalid_contact_name')


def upload_contacts(db, config, send=request):
    raw = setting(db, 'contacts_state')
    if not raw:
        return
    state = json.loads(raw)
    if state['binding'] != contacts_binding(config, setting(db, 'account')):
        raise CollectorError('contacts_identity_changed')
    body = state.get('pending')
    if body is None:
        return
    result = send(config, '/api/v1/android/contacts', json_bytes(body))
    if result.get('id') != body['id'] or not isinstance(result.get('idempotent'), bool):
        raise CollectorError('contacts_ack_not_confirmed')
    state.pop('pending')
    state['acknowledged'] = True
    with db:
        # A newer full snapshot may replace the single queue during this request.
        db.execute('UPDATE settings SET value=? WHERE name=? AND value=?',
                   (json_bytes(state).decode(), 'contacts_state', raw))


def xml_root(content):
    position = content.find('<msg')
    if position < 0:
        return None
    try:
        return ET.fromstring(content[position:])
    except ET.ParseError:
        return None


def native_content(row):
    text = row.get('content') or ''
    if len(text.encode()) > MAX_CONTENT:
        raise CollectorError('content_too_large')
    message_type = int(row['type']) & 0xffff
    kind = {1: 'text', 3: 'image', 34: 'audio', 43: 'video', 47: 'sticker'}.get(message_type, 'unsupported')
    result = {'v': 1, 'conversationId': row['talker'], 'conversationName': row.get('conversationName') or row['talker'],
              'senderId': row.get('senderId') or '', 'senderName': row.get('senderName') or row.get('conversationName') or row['talker'],
              'kind': kind, 'text': text, 'attachments': []}
    result['isOutgoing'] = row.get('isSend') == 1
    if result['isOutgoing']:
        result['senderName'] = '我'
    root = xml_root(text)
    if root is not None:
        result['rawXML'] = text
        subtype = root.findtext('.//appmsg/type')
        result['kind'] = {'6': 'file', '19': 'record', '57': 'reference', '5': 'link'}.get(subtype, kind)
        result['text'] = '\n'.join(v for v in (root.findtext('.//appmsg/title'), root.findtext('.//appmsg/des')) if v)
        record = root.findtext('.//recorditem')
        if record:
            try:
                items = ET.fromstring(record).findall('.//dataitem')
                result['records'] = [{'senderName': item.findtext('.//sourcename') or '',
                                      'kind': {'1': 'text', '2': 'image', '3': 'audio', '4': 'video', '8': 'file', '37': 'sticker'}.get(item.attrib.get('datatype'), 'unsupported'),
                                      'text': item.findtext('datadesc') or item.findtext('datatitle') or '',
                                      'rawXML': ET.tostring(item, encoding='unicode')} for item in items]
            except ET.ParseError:
                pass
    elif message_type == 1 and row['talker'].endswith('@chatroom') and ':\n' in text:
        sender, result['text'] = text.split(':\n', 1)
        result['senderId'] = sender
    return result


def preview_bytes(content):
    value = {'sender': content['conversationName'][:80], 'body': content['text'] or '[' + content['kind'] + ']'}
    while len(json_bytes(value)) > 600:
        value['body'] = value['body'][:max(0, len(value['body']) - 32)]
        if not value['body']:
            value['sender'] = value['sender'][:-8]
    return json_bytes(value)


def asset_aad(message, asset, pair_id=None):
    prefix = ('AWR1', 'A2I_ASSET', message['v'])
    if message['v'] == 8:
        prefix += (canonical_pair_id(pair_id),)
    return '|'.join(str(value) for value in prefix + (message['id'], asset['id'],
        message['deviceId'], message['seq'], message['createdAt'], 0, asset['kind'], asset['mimeType'],
        asset['byteLength'], asset['role'], asset.get('derivedFrom', '')))


def canonical_pair_id(value):
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    raise CollectorError('invalid_pair_id')


def record_source_hash(row):
    return hashlib.sha256(json_bytes([row['type'], row['talker'], row.get('content') or ''])).hexdigest()


def available_bytes(media, metadata):
    value = media.get('dataBase64')
    if not value:
        return None
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError:
        raise CollectorError('invalid_source_media') from None
    if len(data) != metadata['byteLength']:
        raise CollectorError('source_media_length_changed')
    if media.get('sha256') and hashlib.sha256(data).hexdigest() != media['sha256']:
        raise CollectorError('source_media_hash_mismatch')
    return data


def playback_media(media_items, decoder):
    result = list(media_items)
    if decoder is None:
        return result
    for ordinal, media in enumerate(media_items):
        if media.get('mimeType') != 'audio/silk' or not media.get('dataBase64'):
            continue
        try:
            original = available_bytes(media, media)
            wav = decode_silk(original, decoder)
        except (AudioDecodeError, CollectorError):
            continue
        # ponytail: the v6 whole-file limit also applies to playback copies;
        # use chunked storage before accepting longer derived audio.
        if len(wav) <= MAX_ASSET and len(result) < 8:
            result.append({'kind': 'audio', 'mimeType': 'audio/wav', 'byteLength': len(wav),
                           'state': 'ready', 'name': 'playback.wav', 'role': 'playback',
                           'derivedOrdinal': ordinal, 'sha256': hashlib.sha256(wav).hexdigest(),
                           'dataBase64': base64.b64encode(wav).decode()})
            if 'recordItemIndex' in media:
                result[-1]['recordItemIndex'] = media['recordItemIndex']
    return result


def record_slot_media(media_items, decoder):
    result = list(media_items)
    if decoder is None:
        return result
    resolved = playback_media(media_items, decoder)[len(media_items):]
    by_source = {item['derivedOrdinal']: item for item in resolved}
    for ordinal, media in enumerate(media_items):
        if media['kind'] != 'audio' or len(result) >= 8:
            continue
        # Reserve optional playback identity before decoding, so a late SILK
        # original cannot reorder or replace the immutable attachment slots.
        playback = by_source.get(ordinal, {'kind': 'audio', 'role': 'playback',
                                          'derivedOrdinal': ordinal, 'state': 'pending', 'name': 'playback.wav'})
        if media.get('state') in ('ready', 'available') and media.get('mimeType') != 'audio/silk':
            playback = {**playback, 'state': 'unsupported', 'reason': 'playback_not_applicable'}
        if 'recordItemIndex' in media:
            playback['recordItemIndex'] = media['recordItemIndex']
        result.append(playback)
    return result


def native_slot_media(row, decoder):
    if row['type'] != 3:
        return record_slot_media(row.get('media', []), decoder)
    originals = row.get('media', [])
    if len(originals) != 1 or originals[0].get('kind') != 'image':
        raise CollectorError('source_image_metadata_changed')
    original = originals[0]
    preview = row.get('imagePreview') or {'kind': 'image', 'state': 'pending'}
    if (original.get('state') in ('ready', 'available')
            and preview.get('state') not in ('ready', 'available')):
        preview = {**preview, 'state': 'unsupported', 'reason': 'playback_not_applicable'}
    # The existing derived playback role also carries display-ready image copies.
    # Both identities are reserved before either representation is available.
    return [original, {**preview, 'role': 'playback', 'derivedOrdinal': 0, 'name': 'preview.jpg'}]


def ingest(db, config, batch, decoder=None, reply_ready=False):
    fingerprint = batch['accountFingerprint']
    if not isinstance(fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', fingerprint):
        raise CollectorError('invalid_account_fingerprint')
    previous = setting(db, 'account')
    if previous and previous != fingerprint:
        raise CollectorError('source_account_changed')
    with db:
        set_setting(db, 'account', fingerprint)
    queue_contacts(db, config, batch.get('contactSnapshot'), fingerprint)
    key = decoded(config['kA2I'])
    for row in batch['messages']:
        source_id = int(row['msgId'])
        identity = hashlib.sha256(f"{fingerprint}|{source_id}|{row['msgSvrId']}".encode()).hexdigest()
        with db:
            old = db.execute('SELECT identity,server_id FROM sources WHERE source_id=?', (source_id,)).fetchone()
            if old and old[0] != identity:
                if old[1] == '0' and str(row['msgSvrId']) != '0':
                    db.execute('UPDATE sources SET identity=?,server_id=? WHERE source_id=?', (identity, str(row['msgSvrId']), source_id))
                else:
                    raise CollectorError('source_identity_changed')
            db.execute('INSERT OR IGNORE INTO sources(source_id,identity,server_id,status) VALUES(?,?,?,?)', (source_id, identity, str(row['msgSvrId']), 'pending'))
            set_setting(db, 'cursor', max(source_id, int(setting(db, 'cursor', '0'))))
        from source_reader import app_message_media, original_image_spec
        image_spec = original_image_spec(row) if row['type'] == 3 else None
        image_slots = bool(image_spec and image_spec['requiresHD'])
        if row.get('mediaDeferred') is True:
            _, record_specs = app_message_media(row)
            if image_slots:
                row = {**row, 'media': [{'kind': 'image', 'byteLength': image_spec['byteLength'],
                                        'mimeType': 'application/octet-stream', 'state': 'pending'}]}
            elif record_specs is None:
                with db:
                    db.execute("UPDATE sources SET status='waiting_original_download' WHERE source_id=? AND status='pending'", (source_id,))
                continue
            else:
                # Parsing record XML needs no media scan. Publish text and stable
                # slots on the fast lane while the media lane reads files.
                row = {**row, 'media': [{'kind': spec['kind'], 'byteLength': spec['byteLength'],
                                        'mimeType': 'application/octet-stream', 'state': 'pending',
                                        'recordItemIndex': spec['recordItemIndex']} for spec in record_specs]}
        existing = db.execute('SELECT id,body,source_hash FROM messages WHERE source_id=?', (source_id,)).fetchone()
        if existing:
            message = json.loads(existing[1])
            media_items = row.get('media', [])
            if message['v'] == 8:
                if existing[2] != record_source_hash(row):
                    raise CollectorError('source_content_changed')
                has_playback = any(slot['role'] == 'playback' for slot in message['nativeAssetSlots'])
                media_items = native_slot_media(row, decoder if has_playback else None)
            hydrate_assets(db, key, message, media_items, config.get('pairId'))
            continue
        try:
            created = int(row['createTime'])
            if abs(now_ms() - created) > 172_800_000:
                raise CollectorError('outside_server_time_window')
            content = native_content(row)
            content['accountFingerprint'] = fingerprint
            if isinstance(row.get('replyIdentity'), dict):
                content['replyIdentity'] = row['replyIdentity']
            version = 8 if content['kind'] == 'record' or image_slots else 7
            if version == 8:
                canonical_pair_id(config.get('pairId'))
            metadata = []
            media_items = native_slot_media(row, decoder) if version == 8 else playback_media(row.get('media', []), decoder)
            if len(media_items) > 8:
                raise CollectorError('too_many_attachments')
            for media in media_items:
                if version != 8 and media.get('state') == 'pending' and media.get('mimeType') == 'application/octet-stream':
                    raise CollectorError('waiting_original_download')
                length = int(media.get('byteLength') or 0)
                if version != 8 and length <= 0:
                    raise CollectorError('waiting_original_size')
                if version != 8 and length > MAX_ASSET:
                    raise CollectorError('attachment_over_8mib')
                if media['kind'] not in ('image', 'sticker', 'audio', 'video', 'file'):
                    raise CollectorError('invalid_source_media_kind')
                asset = {'id': uuid7(), 'kind': media['kind'], 'role': media.get('role', 'original')}
                if version != 8:
                    asset.update(mimeType=media['mimeType'], byteLength=length)
                if asset['role'] == 'playback':
                    asset['derivedFrom'] = metadata[media['derivedOrdinal']]['id']
                metadata.append(asset)
                original_name = media.get('name')
                if media['kind'] == 'file':
                    source_xml = xml_root(row.get('content') or '')
                    if source_xml is not None:
                        original_name = source_xml.findtext('.//appmsg/title') or original_name
                attachment = {'assetId': asset['id'], 'name': original_name or ('original' if version == 8 else 'original.' + {'audio': 'silk', 'video': 'mp4', 'image': 'jpg', 'sticker': 'gif'}.get(media['kind'], 'bin'))}
                if 'recordItemIndex' in media:
                    record_index = media['recordItemIndex']
                    if type(record_index) is not int or not 0 <= record_index < len(content.get('records', [])):
                        raise CollectorError('invalid_record_attachment_index')
                    attachment['recordItemIndex'] = record_index
                if media.get('sha256'):
                    attachment['sha256'] = media['sha256']
                if media.get('sourceRepresentation') == 'native_db_decoded':
                    attachment.update(sourceRepresentation='native_db_decoded', sourceByteLength=media['sourceByteLength'], originalBytesVerified=False)
                content['attachments'].append(attachment)
            plaintext = json_bytes(content)
            if len(plaintext) > MAX_CONTENT:
                raise CollectorError('content_too_large')
            with db:
                seq = int(setting(db, 'seq', '0')) + 1
                from reply_executor import reply_candidate
                can_reply = reply_ready and bool(config.get('kI2A')) and reply_candidate(row)
                message = {'v': version, 'id': uuid7(), 'deviceId': config['deviceId'], 'seq': seq,
                           'createdAt': created, 'wechatUserId': 0, 'replyCapable': can_reply,
                           'conversationSendCapable': False, 'assets': [],
                           'nativeAssetSlots' if version == 8 else 'nativeAssets': metadata}
                base = f"{message['id']}|{message['deviceId']}|{seq}|{created}|0"
                message['previewEnvelope'] = seal(preview_bytes(content), key, 'phase1', 'AWR1|A2I|' + base)
                content_base = (config['pairId'] + '|' if version == 8 else '') + base
                message['contentEnvelope'] = seal(plaintext, key, 'phase2-content', f'AWR1|A2I_CONTENT|{version}|' + content_base)
                db.execute('INSERT INTO messages(id,source_id,seq,body,source_hash) VALUES(?,?,?,?,?)',
                           (message['id'], source_id, seq, json_bytes(message), record_source_hash(row) if version == 8 else None))
                for ordinal, asset in enumerate(metadata):
                    db.execute('INSERT INTO assets(id,message_id,ordinal,metadata) VALUES(?,?,?,?)', (asset['id'], message['id'], ordinal, json.dumps(asset)))
                set_setting(db, 'seq', seq)
                db.execute('UPDATE sources SET status=? WHERE source_id=?', ('queued', source_id))
            hydrate_assets(db, key, message, media_items, config.get('pairId'))
        except CollectorError as error:
            with db:
                db.execute('UPDATE sources SET status=? WHERE source_id=?', (str(error), source_id))


def hydrate_assets(db, key, message, media_items, pair_id=None):
    if message['v'] == 8:
        pair_id = canonical_pair_id(pair_id)
        expected = f"AWR1|A2I_CONTENT|8|{pair_id}|{message['id']}|{message['deviceId']}|{message['seq']}|{message['createdAt']}|0"
        if message['contentEnvelope']['aad'] != expected:
            raise CollectorError('source_pair_changed')
    for asset_id, ordinal, raw in db.execute('SELECT id,ordinal,metadata FROM assets WHERE message_id=? AND body IS NULL', (message['id'],)).fetchall():
        if ordinal >= len(media_items):
            continue
        asset, media = json.loads(raw), media_items[ordinal]
        if message['v'] == 8:
            try:
                if (media['kind'] != asset['kind'] or media.get('role', 'original') != asset['role']
                        or (asset['role'] == 'playback' and
                            message['nativeAssetSlots'][media['derivedOrdinal']]['id'] != asset['derivedFrom'])):
                    raise CollectorError('source_media_metadata_changed')
                if media.get('reason') == 'playback_not_applicable' and asset['role'] == 'playback':
                    raise CollectorError('playback_not_applicable')
                if int(media.get('byteLength') or 0) > MAX_ASSET:
                    raise CollectorError('attachment_over_8mib')
                data = available_bytes(media, media)
                if data is None:
                    continue
                if not 0 < len(data) <= MAX_ASSET:
                    raise CollectorError('attachment_over_8mib')
                mime = media.get('mimeType', '')
                if (not re.fullmatch(r'[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+', mime)
                        or len(mime) > 128 or media.get('state') not in ('ready', 'available')):
                    raise CollectorError('invalid_source_media_type')
                resolved = {**asset, 'mimeType': mime, 'byteLength': len(data)}
                body = json_bytes({'metadata': resolved, 'envelope': seal(data, key, 'phase2-asset', asset_aad(message, resolved, pair_id))})
                with db:
                    db.execute('UPDATE assets SET metadata=?,body=?,last_error=NULL WHERE id=? AND body IS NULL',
                               (json.dumps(resolved), body, asset_id))
            except CollectorError as error:
                with db:
                    db.execute('UPDATE assets SET last_error=? WHERE id=?', (str(error), asset_id))
            continue
        if media['kind'] != asset['kind'] or media['mimeType'] != asset['mimeType']:
            raise CollectorError('source_media_metadata_changed')
        data = available_bytes(media, asset)
        if data is not None:
            body = json_bytes({'envelope': seal(data, key, 'phase2-asset', asset_aad(message, asset))})
            with db:
                db.execute('UPDATE assets SET body=? WHERE id=? AND body IS NULL', (body, asset_id))


def upload(db, config, send=request, include_assets=True):
    for message_id, body in db.execute('SELECT id,body FROM messages WHERE uploaded=0 ORDER BY seq').fetchall():
        result = send(config, '/api/v1/android/messages', body)
        if result.get('dropped') is True:
            raise CollectorError('relay_policy_paused')
        if result.get('id') != message_id or not isinstance(result.get('idempotent'), bool):
            raise CollectorError('message_ack_not_confirmed')
        with db:
            db.execute('UPDATE messages SET uploaded=1 WHERE id=?', (message_id,))
    if include_assets:
        upload_assets(db, config, send)
    finish_uploaded(db)


def upload_assets(db, config, send=request, limit=None):
    rows = db.execute('SELECT a.id,a.message_id,a.body FROM assets a JOIN messages m ON m.id=a.message_id WHERE a.uploaded=0 AND a.body IS NOT NULL AND m.uploaded=1 LIMIT ?',
                      (-1 if limit is None else limit,)).fetchall()
    for asset_id, message_id, body in rows:
        result = send(config, f'/api/v1/android/messages/{message_id}/assets/{asset_id}', body)
        if result.get('id') != asset_id or not isinstance(result.get('idempotent'), bool):
            raise CollectorError('asset_ack_not_confirmed')
        with db:
            db.execute('UPDATE assets SET uploaded=1 WHERE id=?', (asset_id,))
    finish_uploaded(db)


def finish_uploaded(db):
    with db:
        db.execute("UPDATE sources SET status='complete' WHERE source_id IN (SELECT m.source_id FROM messages m WHERE m.uploaded=1 AND NOT EXISTS(SELECT 1 FROM assets a WHERE a.message_id=m.id AND a.uploaded=0 AND COALESCE(a.last_error,'')<>'playback_not_applicable'))")


def read_source(command, after, ids=None, include_media=True, defer_media=False):
    from reader_transport import ReaderError, read, supported
    if supported(command):
        try:
            batch = read(command, after, ids, include_media, defer_media)
        except ReaderError:
            raise CollectorError('source_reader_unavailable') from None
        if not isinstance(batch.get('messages'), list) or len(batch['messages']) > 20:
            raise CollectorError('invalid_source_result')
        return batch
    arguments = command + ['--limit', '20']
    if include_media:
        arguments.append('--include-media')
    if defer_media:
        arguments.append('--defer-media')
    arguments += ['--ids', ','.join(str(value) for value in ids)] if ids else ['--after-id', str(after)]
    # stdout contains private source material and is only consumed in memory.
    result = subprocess.run(arguments, capture_output=True, timeout=150)
    if result.returncode or len(result.stdout) > 64 * 1024 * 1024:
        raise CollectorError('source_reader_unavailable')
    try:
        batch = json.loads(result.stdout)
        if not isinstance(batch.get('messages'), list) or len(batch['messages']) > 20:
            raise ValueError()
        return batch
    except (ValueError, AttributeError):
        raise CollectorError('invalid_source_result') from None


def status(db):
    contacts = json.loads(setting(db, 'contacts_state', '{}'))
    return {'state': 'ready', 'sources': dict(db.execute('SELECT status,count(*) FROM sources GROUP BY status')),
            'messages_uploaded': db.execute('SELECT count(*) FROM messages WHERE uploaded=1').fetchone()[0],
            'assets_uploaded': db.execute('SELECT count(*) FROM assets WHERE uploaded=1').fetchone()[0],
            'contacts_status': setting(db, 'contacts_status', 'not_read'),
            'contacts_count': contacts.get('count', 0), 'contacts_pending': 'pending' in contacts}


class SourceWake:
    """Private guest events only wake reads; normal reads still verify source identity."""

    def __init__(self, command):
        self.command = command
        self.dirty = threading.Event()
        self.media_dirty = threading.Event()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.retry_timers = {}
        self.process = None
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def changed(self, source=True, media=True):
        if source:
            self.dirty.set()
        if media:
            self.media_dirty.set()
        if source or media:
            self.wake.set()

    def retry(self, source=False, media=False):
        # This retries failed work only; idle time never initiates a source read.
        key = (source, media)
        previous = self.retry_timers.get(key)
        if previous is not None:
            previous.cancel()
        def ready():
            if not self.stopped.is_set():
                if source or media:
                    self.changed(source=source, media=media)
                else:
                    self.wake.set()
        timer = threading.Timer(15, ready)
        timer.daemon = True
        self.retry_timers[key] = timer
        timer.start()

    def __enter__(self):
        self.changed()
        self.thread.start()
        return self

    def watch(self):
        while not self.stopped.is_set():
            try:
                process = subprocess.Popen(self.command + ['--watch-source'], stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, start_new_session=True)
                self.process = process
                with process.stdout:
                    while not self.stopped.is_set():
                        line = process.stdout.readline(256)
                        if not line:
                            break
                        event = json.loads(line)
                        if event == {'sourceChanged': True}:
                            self.changed()
                            continue
                        if (not isinstance(event, dict) or set(event) != {'sourceChanged', 'mediaChanged'}
                                or any(type(value) is not bool for value in event.values())):
                            break
                        self.changed(source=event['sourceChanged'], media=event['mediaChanged'])
            except (OSError, ValueError):
                pass
            finally:
                self.stop_process()
            self.changed()
            self.stopped.wait(5)

    def stop_process(self):
        process = self.process
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            self.process = None

    def __exit__(self, *_):
        self.stopped.set()
        for timer in self.retry_timers.values():
            timer.cancel()
        self.stop_process()
        self.thread.join(timeout=3)


def background_read(command, ids, executor, has_reply_key, image_account=None, image_attempts=None):
    from reply_executor import ReplyError, executor_ready
    batch = read_source(command, 0, ids) if ids else None
    try:
        ready = has_reply_key and executor is not None and executor_ready(executor)
    except ReplyError:
        ready = False
    return batch, ready


def background_assets(state_dir, config, config_path):
    if config_path is not None:
        reload_config(config_path, config)
    db = open_store(state_dir)
    try:
        try:
            upload_assets(db, config, limit=1)
        finally:
            # A rejected asset must not starve an independent full contact snapshot.
            upload_contacts(db, config)
    finally:
        db.close()


def run_events(db, config, command, decoder, config_path, reply_executor):
    state_dir = Path(db.execute('PRAGMA database_list').fetchone()[2]).parent
    pending_job = None
    job_kind = None
    pending_ids = []
    urgent_ids = []
    next_assets = 0
    image_attempts = {}
    image_rows = {}
    download_job = None
    download_batch = None
    download_refreshed = set()
    prefer_retry = False
    last_job_kind = 'read'
    has_reply_key = bool(config.get('kI2A'))
    _, reply_ready = background_read(command, [], reply_executor, has_reply_key)
    with SourceWake(command) as events, ThreadPoolExecutor(max_workers=1) as worker, ThreadPoolExecutor(max_workers=1) as downloader:
        while True:
            events.wake.clear()
            completing_job = False
            try:
                if config_path is not None:
                    reload_config(config_path, config)
                if bool(config.get('kI2A')) and not has_reply_key:
                    _, reply_ready = background_read(command, [], reply_executor, True)
                    has_reply_key = True
                if events.dirty.is_set():
                    # Clear before reading so changes during the read are retained.
                    events.dirty.clear()
                    batch = read_source(command, int(setting(db, 'cursor', '0')), include_media=False, defer_media=True)
                    # Each send revalidates the helper; the last readiness result only advertises capability.
                    ingest(db, config, batch, reply_ready=reply_ready)
                    for row in batch['messages']:
                        if row.get('mediaDeferred') is True and row['msgId'] not in urgent_ids:
                            urgent_ids.insert(0, row['msgId'])
                            events.media_dirty.set()
                    upload(db, config, include_assets=False)
                    if batch['messages'] and batch.get('sourceMaxId', 0) > int(setting(db, 'cursor', '0')):
                        events.changed()
                if pending_job is not None and pending_job.done():
                    completing_job = True
                    result = pending_job.result()
                    if job_kind == 'read':
                        batch, reply_ready = result
                        if batch is not None:
                            ingest(db, config, batch, decoder, reply_ready)
                            # Only validated source reads supply bytes. UI results merely
                            # wake an exact source read on the separate download lane.
                            for row in batch['messages']:
                                if download_job is not None:
                                    download_refreshed.add(row['msgId'])
                                image_rows.pop(row['msgId'], None)
                                from image_download import image_commands
                                if image_commands(batch, row, now_ms()):
                                    image_rows[row['msgId']] = (batch, row)
                            with db:
                                db.executemany('UPDATE sources SET retry_at=? WHERE source_id=?', [(now_ms(), value) for value in pending_ids])
                            upload(db, config, include_assets=False)
                    pending_job = None
                    completing_job = False
                if download_job is not None and download_job.done():
                    completing_job = 'download'
                    source_id, remaining, retry = download_job.result()
                    if source_id is not None:
                        urgent_ids = list(dict.fromkeys([source_id] + urgent_ids))
                        prefer_retry = not prefer_retry
                    for row in download_batch['messages']:
                        current = db.execute('SELECT status FROM sources WHERE source_id=?', (row['msgId'],)).fetchone()
                        if (row['msgId'] in remaining and row['msgId'] not in download_refreshed
                                and current and current[0] not in ('complete', 'outside_server_time_window', 'attachment_over_8mib', 'content_too_large', 'too_many_attachments')):
                            image_rows.setdefault(row['msgId'], (download_batch, row))
                    if retry:
                        events.retry(media=True)
                    download_job = None
                    completing_job = False
                if download_job is None and image_rows:
                    from image_download import download_pending
                    candidates = list(image_rows.values())
                    download_batch = {**candidates[0][0], 'sourceMaxId': max(batch['sourceMaxId'] for batch, _ in candidates),
                                      'messages': sorted((row for _, row in candidates), key=lambda row: row['msgId'], reverse=True)}
                    image_rows.clear()
                    download_refreshed.clear()
                    # image_attempts has one writer, including backoff updates.
                    download_job = downloader.submit(download_pending, download_batch, reply_executor,
                                                     setting(db, 'account'), image_attempts, prefer_retry=prefer_retry)
                    download_job.add_done_callback(lambda _: events.wake.set())
                if pending_job is None:
                    available = db.execute('SELECT 1 FROM assets a JOIN messages m ON m.id=a.message_id WHERE a.uploaded=0 AND a.body IS NOT NULL AND m.uploaded=1 LIMIT 1').fetchone()
                    available = available or 'pending' in json.loads(setting(db, 'contacts_state', '{}'))
                    media_waiting = events.media_dirty.is_set() or urgent_ids
                    if available and time.monotonic() >= next_assets and (not media_waiting or last_job_kind != 'assets'):
                        job_kind = 'assets'
                        pending_job = worker.submit(background_assets, state_dir, dict(config), config_path)
                    elif media_waiting:
                        # Consume before dispatch so file changes during the read remain pending.
                        if events.media_dirty.is_set():
                            events.media_dirty.clear()
                            pending = [row[0] for row in db.execute("SELECT source_id FROM sources WHERE status NOT IN ('complete','outside_server_time_window','attachment_over_8mib','content_too_large','too_many_attachments') ORDER BY source_id DESC")]
                            # One pass per event, including older originals beyond the first batch.
                            urgent_ids = list(dict.fromkeys(sorted(urgent_ids, reverse=True) + pending))
                        pending_ids, urgent_ids = urgent_ids[:20], urgent_ids[20:]
                        job_kind = 'read'
                        pending_job = worker.submit(background_read, command, pending_ids, reply_executor, bool(config.get('kI2A')))
                    if pending_job is not None:
                        last_job_kind = job_kind
                        pending_job.add_done_callback(lambda _: events.wake.set())
                print(json.dumps(status(db)), flush=True)
            except (CollectorError, subprocess.TimeoutExpired, OSError, ValueError, sqlite3.Error) as error:
                if completing_job:
                    if completing_job == 'download':
                        download_job = None
                        events.retry(media=True)
                    elif job_kind == 'assets':
                        next_assets = time.monotonic() + 15
                        events.retry()
                    else:
                        events.retry(media=True)
                    if completing_job != 'download':
                        pending_job = None
                else:
                    events.retry(source=True)
                print(json.dumps({'state': str(error) if isinstance(error, CollectorError) else 'sync_retry_required'}), flush=True)
                # Failed background jobs get their own backoff without holding up new source events.
            if not events.dirty.is_set() and not (urgent_ids and pending_job is None):
                events.wake.wait()


def run(db, config, command, once, decoder, config_path=None, reply_executor=None):
    if not command:
        raise CollectorError('reader_command_required')
    if not once:
        return run_events(db, config, command, decoder, config_path, reply_executor)
    from reply_executor import executor_ready
    if config_path is not None:
        reload_config(config_path, config)
    ready = bool(config.get('kI2A')) and executor_ready(reply_executor) if reply_executor is not None else False
    # Contacts are supplied only on the fast read. Keep the cursor for the full media read.
    after = int(setting(db, 'cursor', '0'))
    fast = read_source(command, after, include_media=False, defer_media=True)
    ingest(db, config, {**fast, 'messages': []}, reply_ready=ready)
    ingest(db, config, read_source(command, after), decoder, ready)
    upload(db, config)
    pending = [row[0] for row in db.execute("SELECT source_id FROM sources WHERE status NOT IN ('complete','outside_server_time_window','attachment_over_8mib','content_too_large','too_many_attachments') ORDER BY retry_at,source_id LIMIT 20")]
    if pending:
        ingest(db, config, read_source(command, 0, pending), decoder, ready)
        with db:
            db.executemany('UPDATE sources SET retry_at=? WHERE source_id=?', [(now_ms(), value) for value in pending])
    upload(db, config)
    upload_contacts(db, config)
    print(json.dumps(status(db)), flush=True)


def reload_config(path, config):
    current = json.loads(path.read_text())
    if any(current.get(key) != config.get(key) for key in ('pairId', 'deviceId', 'origin', 'privateKey', 'kA2I', 'paired')):
        raise CollectorError('pairing_identity_changed')
    if config.get('kI2A') and current.get('kI2A') != config['kI2A']:
        raise CollectorError('reply_key_changed')
    if current.get('kI2A') and len(decoded(current['kI2A'])) != 32:
        raise CollectorError('invalid_reply_key')
    config.update(current)


def run_replies(db, config, config_path, command, executor, once):
    from reply_executor import ReplyError, poll
    if not command:
        raise CollectorError('reader_command_required')
    while True:
        try:
            reload_config(config_path, config)
            poll(db, config, config_path, command, executor, request, read_source, save_config,
                 decoded, encoded, json_bytes)
        except (CollectorError, ReplyError) as error:
            print(json.dumps({'state': str(error)}), flush=True)
            if once:
                raise CollectorError(str(error)) from None
            time.sleep(5)
        if once:
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/share/aurora-tablet-relay')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('pair')
    sub.add_parser('status')
    runner = sub.add_parser('run')
    runner.add_argument('--once', action='store_true')
    runner.add_argument('--silk-decoder', type=Path)
    runner.add_argument('--reply-executor', type=Path, help='Private stdin/stdout official-UI adapter; absent keeps sending disabled')
    runner.add_argument('--reader-command', nargs=argparse.REMAINDER)
    replies = sub.add_parser('replies')
    replies.add_argument('--once', action='store_true')
    replies.add_argument('--reply-executor', type=Path)
    replies.add_argument('--reader-command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = args.state_dir.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or args.state_dir.is_symlink():
        raise CollectorError('state_directory_not_private')
    with (args.state_dir / ('replies.lock' if args.action == 'replies' else 'collector.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CollectorError('collector_already_running') from None
        if args.action == 'pair':
            pair(args.state_dir)
            return
        with contextlib.closing(open_store(args.state_dir)) as db:
            if args.action == 'status':
                print(json.dumps(status(db)))
                return
            config_path = args.state_dir / 'device.json'
            if not config_path.exists():
                raise CollectorError('pairing_required')
            config = json.loads(config_path.read_text())
            if args.action == 'replies':
                run_replies(db, config, config_path, args.reader_command, args.reply_executor, args.once)
                return
            run(db, config, args.reader_command, args.once, args.silk_decoder, config_path, args.reply_executor)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except CollectorError as error:
        print(json.dumps({'state': str(error)}), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print(json.dumps({'state': 'collector_failed_no_private_details_logged'}), file=sys.stderr)
        sys.exit(1)
