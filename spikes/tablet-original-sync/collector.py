#!/usr/bin/env python3
"""Read-only tablet collector. State is private; console output is aggregate only."""
import argparse
import base64
import contextlib
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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


def request(config, path, body, signed=True):
    headers = {'Content-Type': 'application/json'}
    if signed:
        timestamp, nonce = str(now_ms()), encoded(secrets.token_bytes(16))
        canonical = f'POST\n{path}\n{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}'
        key = serialization.load_pem_private_key(config['privateKey'].encode(), password=None)
        signature = key.sign(canonical.encode(), ec.ECDSA(hashes.SHA256()))
        headers.update({'X-AWR-Device-Id': config['deviceId'], 'X-AWR-Timestamp': timestamp,
                        'X-AWR-Nonce': nonce, 'X-AWR-Signature': encoded(signature)})
    req = urllib.request.Request(config['origin'] + path, data=body, headers=headers, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=45) as response:
            data = response.read(65537)
            if len(data) > 65536:
                raise CollectorError('response_too_large')
            return json.loads(data)
    except urllib.error.HTTPError as error:
        raise CollectorError(f'http_{error.code}') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise CollectorError('network_or_response_error') from None


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
        config.pop('kI2A', None)
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
            seq INTEGER UNIQUE NOT NULL, body BLOB NOT NULL, uploaded INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL, metadata TEXT NOT NULL, body BLOB,
            uploaded INTEGER NOT NULL DEFAULT 0);
    ''')
    os.chmod(state / 'outbox.sqlite', 0o600)
    return db


def setting(db, name, default=''):
    row = db.execute('SELECT value FROM settings WHERE name=?', (name,)).fetchone()
    return row[0] if row else default


def set_setting(db, name, value):
    db.execute('INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value', (name, str(value)))


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
                                      'kind': {'1': 'text', '2': 'image', '3': 'audio', '4': 'video', '8': 'file'}.get(item.attrib.get('datatype'), 'unsupported'),
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


def asset_aad(message, asset):
    return '|'.join(str(value) for value in ('AWR1', 'A2I_ASSET', 6, message['id'], asset['id'],
        message['deviceId'], message['seq'], message['createdAt'], 0, asset['kind'], asset['mimeType'],
        asset['byteLength'], asset['role'], asset.get('derivedFrom', '')))


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
        original = available_bytes(media, media)
        try:
            wav = decode_silk(original, decoder)
        except AudioDecodeError:
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


def ingest(db, config, batch, decoder=None):
    fingerprint = batch['accountFingerprint']
    if not isinstance(fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', fingerprint):
        raise CollectorError('invalid_account_fingerprint')
    previous = setting(db, 'account')
    if previous and previous != fingerprint:
        raise CollectorError('source_account_changed')
    with db:
        set_setting(db, 'account', fingerprint)
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
        existing = db.execute('SELECT id,body FROM messages WHERE source_id=?', (source_id,)).fetchone()
        if existing:
            hydrate_assets(db, key, json.loads(existing[1]), row.get('media', []))
            continue
        try:
            created = int(row['createTime'])
            if abs(now_ms() - created) > 172_800_000:
                raise CollectorError('outside_server_time_window')
            content = native_content(row)
            metadata = []
            media_items = playback_media(row.get('media', []), decoder)
            if len(media_items) > 8:
                raise CollectorError('too_many_attachments')
            for media in media_items:
                if media.get('state') == 'pending' and media.get('mimeType') == 'application/octet-stream':
                    raise CollectorError('waiting_original_download')
                length = int(media.get('byteLength') or 0)
                if length <= 0:
                    raise CollectorError('waiting_original_size')
                if length > MAX_ASSET:
                    raise CollectorError('attachment_over_8mib')
                if media['kind'] not in ('image', 'sticker', 'audio', 'video', 'file'):
                    raise CollectorError('invalid_source_media_kind')
                asset = {'id': uuid7(), 'kind': media['kind'], 'mimeType': media['mimeType'], 'byteLength': length, 'role': media.get('role', 'original')}
                if asset['role'] == 'playback':
                    asset['derivedFrom'] = metadata[media['derivedOrdinal']]['id']
                metadata.append(asset)
                original_name = media.get('name')
                if media['kind'] == 'file':
                    source_xml = xml_root(row.get('content') or '')
                    if source_xml is not None:
                        original_name = source_xml.findtext('.//appmsg/title') or original_name
                attachment = {'assetId': asset['id'], 'name': original_name or ('original.' + {'audio': 'silk', 'video': 'mp4', 'image': 'jpg', 'sticker': 'gif'}.get(media['kind'], 'bin'))}
                if 'recordItemIndex' in media:
                    record_index = media['recordItemIndex']
                    if type(record_index) is not int or not 0 <= record_index < len(content.get('records', [])):
                        raise CollectorError('invalid_record_attachment_index')
                    attachment['recordItemIndex'] = record_index
                if media.get('sha256'):
                    attachment['sha256'] = media['sha256']
                content['attachments'].append(attachment)
            plaintext = json_bytes(content)
            if len(plaintext) > MAX_CONTENT:
                raise CollectorError('content_too_large')
            with db:
                seq = int(setting(db, 'seq', '0')) + 1
                message = {'v': 6, 'id': uuid7(), 'deviceId': config['deviceId'], 'seq': seq,
                           'createdAt': created, 'wechatUserId': 0, 'replyCapable': False,
                           'conversationSendCapable': False, 'assets': [], 'nativeAssets': metadata}
                base = f"{message['id']}|{message['deviceId']}|{seq}|{created}|0"
                message['previewEnvelope'] = seal(preview_bytes(content), key, 'phase1', 'AWR1|A2I|' + base)
                message['contentEnvelope'] = seal(plaintext, key, 'phase2-content', 'AWR1|A2I_CONTENT|6|' + base)
                db.execute('INSERT INTO messages(id,source_id,seq,body) VALUES(?,?,?,?)', (message['id'], source_id, seq, json_bytes(message)))
                for ordinal, asset in enumerate(metadata):
                    db.execute('INSERT INTO assets(id,message_id,ordinal,metadata) VALUES(?,?,?,?)', (asset['id'], message['id'], ordinal, json.dumps(asset)))
                set_setting(db, 'seq', seq)
                db.execute('UPDATE sources SET status=? WHERE source_id=?', ('queued', source_id))
            hydrate_assets(db, key, message, media_items)
        except CollectorError as error:
            with db:
                db.execute('UPDATE sources SET status=? WHERE source_id=?', (str(error), source_id))


def hydrate_assets(db, key, message, media_items):
    for asset_id, ordinal, raw in db.execute('SELECT id,ordinal,metadata FROM assets WHERE message_id=? AND body IS NULL', (message['id'],)).fetchall():
        if ordinal >= len(media_items):
            continue
        asset, media = json.loads(raw), media_items[ordinal]
        if media['kind'] != asset['kind'] or media['mimeType'] != asset['mimeType']:
            raise CollectorError('source_media_metadata_changed')
        data = available_bytes(media, asset)
        if data is not None:
            body = json_bytes({'envelope': seal(data, key, 'phase2-asset', asset_aad(message, asset))})
            with db:
                db.execute('UPDATE assets SET body=? WHERE id=? AND body IS NULL', (body, asset_id))


def upload(db, config, send=request):
    for message_id, body in db.execute('SELECT id,body FROM messages WHERE uploaded=0 ORDER BY seq').fetchall():
        result = send(config, '/api/v1/android/messages', body)
        if result.get('dropped') is True:
            raise CollectorError('relay_policy_paused')
        if result.get('id') != message_id or not isinstance(result.get('idempotent'), bool):
            raise CollectorError('message_ack_not_confirmed')
        with db:
            db.execute('UPDATE messages SET uploaded=1 WHERE id=?', (message_id,))
    for asset_id, message_id, body in db.execute('SELECT a.id,a.message_id,a.body FROM assets a JOIN messages m ON m.id=a.message_id WHERE a.uploaded=0 AND a.body IS NOT NULL AND m.uploaded=1').fetchall():
        result = send(config, f'/api/v1/android/messages/{message_id}/assets/{asset_id}', body)
        if result.get('id') != asset_id or not isinstance(result.get('idempotent'), bool):
            raise CollectorError('asset_ack_not_confirmed')
        with db:
            db.execute('UPDATE assets SET uploaded=1 WHERE id=?', (asset_id,))
    with db:
        db.execute("UPDATE sources SET status='complete' WHERE source_id IN (SELECT m.source_id FROM messages m WHERE m.uploaded=1 AND NOT EXISTS(SELECT 1 FROM assets a WHERE a.message_id=m.id AND a.uploaded=0))")


def read_source(command, after, ids=None):
    arguments = command + ['--limit', '20', '--include-media']
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
    return {'state': 'ready', 'sources': dict(db.execute('SELECT status,count(*) FROM sources GROUP BY status')),
            'messages_uploaded': db.execute('SELECT count(*) FROM messages WHERE uploaded=1').fetchone()[0],
            'assets_uploaded': db.execute('SELECT count(*) FROM assets WHERE uploaded=1').fetchone()[0]}


def run(db, config, command, once, decoder):
    if not command:
        raise CollectorError('reader_command_required')
    while True:
        try:
            ingest(db, config, read_source(command, int(setting(db, 'cursor', '0'))), decoder)
            pending = [row[0] for row in db.execute("SELECT source_id FROM sources WHERE status NOT IN ('complete','outside_server_time_window','attachment_over_8mib','content_too_large','too_many_attachments') ORDER BY retry_at,source_id LIMIT 20")]
            if pending:
                ingest(db, config, read_source(command, 0, pending), decoder)
                with db:
                    db.executemany('UPDATE sources SET retry_at=? WHERE source_id=?', [(now_ms(), value) for value in pending])
            upload(db, config)
            print(json.dumps(status(db)), flush=True)
        except CollectorError as error:
            print(json.dumps({'state': str(error)}), flush=True)
            if once:
                raise
        if once:
            return
        time.sleep(15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/share/aurora-tablet-relay')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('pair')
    sub.add_parser('status')
    runner = sub.add_parser('run')
    runner.add_argument('--once', action='store_true')
    runner.add_argument('--silk-decoder', type=Path)
    runner.add_argument('--reader-command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = args.state_dir.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or args.state_dir.is_symlink():
        raise CollectorError('state_directory_not_private')
    with (args.state_dir / 'collector.lock').open('a') as lock:
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
            run(db, config, args.reader_command, args.once, args.silk_decoder)


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
