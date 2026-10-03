"""Tablet reply consumer. Only an exact official-UI recipient may cross the send gate."""
import hashlib
import json
import re
import subprocess
import time
import uuid

from source_reader import CONTACT_SERVICES, contact_for_send

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class ReplyError(Exception):
    """Fixed public error codes only."""


def reply_candidate(row):
    talker = row.get('talker')
    identity = row.get('replyIdentity')
    contact = identity.get('contact') if isinstance(identity, dict) else None
    alias = contact.get('alias') if isinstance(contact, dict) else None
    return (isinstance(talker, str) and bool(talker) and '@' not in talker and talker not in CONTACT_SERVICES
            and isinstance(contact, dict) and identity.get('username') == talker
            and contact.get('username') == talker and isinstance(alias, str)
            and 1 <= len(alias) <= 128 and not any(ord(char) < 32 for char in alias))


def bootstrap_key(config, decode):
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=config['pairId'].encode(),
                info=('AWR1|TABLET_REPLY_KEY_BOOTSTRAP|' + config['deviceId']).encode()
                ).derive(decode(config['kA2I']))


def command_aad(command, config):
    version = command.get('v')
    if version not in (5, 6, 7, 8) or command.get('deviceId') != config['deviceId'] or command.get('pairId') != config['pairId']:
        raise ReplyError('INVALID_REPLY')
    target_key = 'targetContactSnapshotId' if version in (7, 8) else 'targetMessageId'
    for key in ('id', target_key):
        try:
            if str(uuid.UUID(command[key])) != command[key]:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ReplyError('INVALID_REPLY') from None
    if (command.get('wechatUserId') != 0 or type(command.get('createdAt')) is not int
            or command['createdAt'] <= 0
            or command.get('targetMessageId' if version in (7, 8) else 'targetContactSnapshotId') is not None):
        raise ReplyError('INVALID_REPLY')
    kind = {5: 'TABLET_SEND', 6: 'REPLY_KEY_BOOTSTRAP', 7: 'TABLET_CONTACT_SEND',
            8: 'TABLET_CONTACT_REPLY_KEY_BOOTSTRAP'}[version]
    return f"AWR1|I2A|{version}|{kind}|{config['pairId']}|{command['id']}|{config['deviceId']}|{command[target_key]}|{command['createdAt']}|0"


def decrypt_command(command, config, decode):
    aad = command_aad(command, config)
    envelope = command.get('replyEnvelope')
    kid = 'phase2-reply-bootstrap' if command['v'] in (6, 8) else 'phase1-reply'
    if (not isinstance(envelope, dict) or set(envelope) != {'alg', 'kid', 'aad', 'iv', 'ct'}
            or envelope.get('alg') != 'A256GCM' or envelope.get('kid') != kid or envelope.get('aad') != aad):
        raise ReplyError('INVALID_REPLY')
    try:
        key = bootstrap_key(config, decode) if command['v'] in (6, 8) else decode(config['kI2A'])
        iv, ciphertext = decode(envelope['iv']), decode(envelope['ct'])
        if len(iv) != 12 or not 17 <= len(ciphertext) <= 8192:
            raise ValueError()
        value = json.loads(AESGCM(key).decrypt(iv, ciphertext, aad.encode()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except Exception:
        raise ReplyError('INVALID_REPLY') from None


def open_journal(db):
    db.execute('''CREATE TABLE IF NOT EXISTS reply_commands(
        id TEXT PRIMARY KEY, command BLOB NOT NULL, status TEXT NOT NULL,
        acked INTEGER NOT NULL DEFAULT 0)''')
    db.commit()


def executor_request(executor, value):
    if executor is None:
        return {'status': 'AUTOMATION_NOT_READY', 'clicked': False}
    try:
        result = subprocess.run([str(executor)], input=json.dumps(value, ensure_ascii=False).encode(),
                                capture_output=True, timeout=245)
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError()
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError()
        return response
    except (OSError, subprocess.TimeoutExpired, ValueError):
        # A transport error after starting the executor cannot prove the click did not happen.
        return {'status': 'SEND_UNCONFIRMED', 'clicked': None}


def executor_ready(executor):
    result = executor_request(executor, {'v': 1, 'operation': 'probe'})
    return result == {'status': 'READY', 'protocol': 2, 'recipientProof': 'wechat-profile-alias'}


def acknowledge_pending(db, config, request, json_bytes):
    for reply_id, status in db.execute("SELECT id,status FROM reply_commands WHERE acked=0 AND status NOT IN ('RECEIVED','COMMITTING')").fetchall():
        result = request(config, f'/api/v1/android/replies/{reply_id}/ack', json_bytes({'status': status}))
        if result.get('replyId') != reply_id or result.get('status') != status:
            raise ReplyError('reply_ack_not_confirmed')
        with db:
            db.execute('UPDATE reply_commands SET acked=1 WHERE id=? AND status=?', (reply_id, status))


def poll(db, config, config_path, reader_command, executor, request, read_source, save_config, decode, encode, json_bytes,
         execute=executor_request, now_ms=lambda: time.time_ns() // 1_000_000, sleep=time.sleep):
    open_journal(db)
    # A crash anywhere past this boundary is uncertain, including a successful click before an ACK.
    with db:
        db.execute("UPDATE reply_commands SET status='SEND_UNCONFIRMED' WHERE status='COMMITTING'")
    acknowledge_pending(db, config, request, json_bytes)
    response = request(config, '/api/v1/android/replies/tablet', b'', method='GET')
    command = response.get('reply')
    if command is None:
        return
    if not isinstance(command, dict):
        raise ReplyError('invalid_reply_response')
    command_aad(command, config)
    raw = json_bytes(command)
    previous = db.execute('SELECT command,status FROM reply_commands WHERE id=?', (command['id'],)).fetchone()
    if previous and previous[0] != raw:
        raise ReplyError('reply_identity_changed')
    if not previous:
        with db:
            db.execute("INSERT INTO reply_commands(id,command,status) VALUES(?,?,'RECEIVED')", (command['id'], raw))
    elif previous[1] != 'RECEIVED':
        # The server may have committed an ACK whose response was lost.
        with db:
            db.execute('UPDATE reply_commands SET acked=0 WHERE id=?', (command['id'],))
        acknowledge_pending(db, config, request, json_bytes)
        return
    try:
        status = process(db, config, config_path, command, response.get('relayPolicy', {}), reader_command,
                         executor, read_source, save_config, decode, encode, execute, now_ms, sleep,
                         lambda: request(config, '/api/v1/android/replies/tablet', b'', method='GET'))
    except ReplyError as error:
        status = str(error)
    if command['v'] in (6, 8) and status not in ('REPLY_KEY_INSTALLED', 'INVALID_REPLY'):
        status = 'FAILED'
    with db:
        db.execute('UPDATE reply_commands SET status=? WHERE id=?', (status, command['id']))
    acknowledge_pending(db, config, request, json_bytes)


def process(db, config, config_path, command, policy, reader_command, executor, read_source, save_config,
            decode, encode, execute, now_ms, sleep, refresh_policy):
    contact_command = command['v'] in (7, 8)
    stored_account = db.execute("SELECT value FROM settings WHERE name='account'").fetchone()
    if contact_command:
        from collector import contacts_binding
        state_row = db.execute("SELECT value FROM settings WHERE name='contacts_state'").fetchone()
        try:
            state = json.loads(state_row[0]) if state_row else {}
            snapshot = state.get('snapshot', {})
            valid = (stored_account and state.get('snapshotId') == command['targetContactSnapshotId']
                     and state.get('acknowledged') is True and snapshot.get('v') == 3
                     and state.get('binding') == contacts_binding(config, stored_account[0])
                     and snapshot.get('accountFingerprint') == stored_account[0])
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ReplyError('WECHAT_ACTION_CHANGED')
    else:
        target = db.execute('SELECT source_id,body,uploaded FROM messages WHERE id=?', (command['targetMessageId'],)).fetchone()
        if not target or target[2] != 1:
            raise ReplyError('INVALID_REPLY')
        message = json.loads(target[1])
        if (message.get('deviceId') != config['deviceId'] or message.get('wechatUserId') != 0
                or message.get('v') not in (6, 7, 8)):
            raise ReplyError('INVALID_REPLY')
    value = decrypt_command(command, config, decode)
    if command['v'] in (6, 8):
        if contact_command:
            if policy.get('active') is not True or abs(now_ms() - command['createdAt']) > 120_000:
                raise ReplyError('AUTOMATION_NOT_READY')
            batch = read_source(reader_command, 0, include_media=False, defer_media=True)
            if (batch.get('accountFingerprint') != stored_account[0]
                    or batch.get('contactSnapshot', {}).get('state') != 'ready'):
                raise ReplyError('WECHAT_ACTION_CHANGED')
            if abs(now_ms() - command['createdAt']) > 120_000:
                raise ReplyError('AUTOMATION_NOT_READY')
        if (set(value) != {'v', 'pairId', 'deviceId', 'replyKey'} or value.get('v') != 1
                or value.get('pairId') != config['pairId'] or value.get('deviceId') != config['deviceId']):
            raise ReplyError('INVALID_REPLY')
        try:
            key = decode(value['replyKey'])
            if len(key) != 32 or key == decode(config['kA2I']):
                raise ValueError()
            if config.get('kI2A') and decode(config['kI2A']) != key:
                raise ValueError()
        except Exception:
            raise ReplyError('INVALID_REPLY') from None
        updated = {**config, 'kI2A': encode(key)}
        save_config(config_path, updated)
        config.update(updated)
        return 'REPLY_KEY_INSTALLED'
    if ((not contact_command and (message.get('v') not in (7, 8) or message.get('replyCapable') is not True))
            or policy.get('active') is not True
            or abs(now_ms() - command['createdAt']) > 120_000):
        raise ReplyError('AUTOMATION_NOT_READY')
    if (set(value) != {'body', 'conversationId', 'accountFingerprint'} or not isinstance(value.get('body'), str)
            or not 1 <= len(value['body']) <= 1000 or not value['body'].strip()
            or not isinstance(value.get('conversationId'), str)
            or not isinstance(value.get('accountFingerprint'), str)
            or not re.fullmatch('[a-f0-9]{64}', value['accountFingerprint'])):
        raise ReplyError('INVALID_REPLY')
    if not stored_account or stored_account[0] != value['accountFingerprint']:
        raise ReplyError('WECHAT_ACTION_CHANGED')
    if contact_command:
        saved_contact = contact_for_send({'state': 'ready', 'contacts': snapshot.get('contacts')}, value['conversationId'])
        if saved_contact is None:
            raise ReplyError('WECHAT_ACTION_CHANGED')
        batch = read_source(reader_command, 0, include_media=False, defer_media=True)
        contact = contact_for_send(batch.get('contactSnapshot'), value['conversationId'], saved_contact['alias'])
        if contact is None:
            raise ReplyError('WECHAT_ACTION_CHANGED')
        recipient = {'sourceContactSnapshotId': command['targetContactSnapshotId'],
                     'alias': contact['alias'], 'conversationName': contact['name']}
    else:
        batch = read_source(reader_command, 0, [target[0]], include_media=False, defer_media=True)
        matches = [row for row in batch.get('messages', []) if row.get('msgId') == target[0]]
        if len(matches) != 1:
            raise ReplyError('WECHAT_ACTION_CHANGED')
        row = matches[0]
        source = db.execute('SELECT identity FROM sources WHERE source_id=?', (target[0],)).fetchone()
        identity = hashlib.sha256(f"{stored_account[0]}|{target[0]}|{row.get('msgSvrId')}".encode()).hexdigest()
        if (not source or source[0] != identity or row.get('talker') != value['conversationId']
                or not reply_candidate(row)):
            raise ReplyError('WECHAT_ACTION_CHANGED')
        contact = contact_for_send(batch.get('contactSnapshot'), value['conversationId'], row['replyIdentity']['contact']['alias'])
        if contact is None:
            raise ReplyError('WECHAT_ACTION_CHANGED')
        recipient = {'sourceMessageId': target[0], 'sourceServerId': str(row['msgSvrId']),
                     'alias': row['replyIdentity']['contact']['alias'], 'conversationName': row.get('conversationName', '')}
    if batch.get('accountFingerprint') != stored_account[0]:
        raise ReplyError('WECHAT_ACTION_CHANGED')
    if executor is None:
        raise ReplyError('AUTOMATION_NOT_READY')
    after_id = batch.get('sourceMaxId')
    if type(after_id) is not int or after_id < (0 if contact_command else target[0]):
        raise ReplyError('AUTOMATION_NOT_READY')
    started = now_ms()
    if abs(started - command['createdAt']) > 120_000:
        raise ReplyError('AUTOMATION_NOT_READY')
    send_request = {'v': 1, 'operation': 'send', 'replyId': command['id'],
        'accountFingerprint': stored_account[0], 'conversationId': value['conversationId'],
        **recipient,
        'sourceMaxId': after_id,
        'expiresAt': command['createdAt'] + 120_000,
        'body': value['body']}
    for _ in range(120):
        current = refresh_policy()
        if current.get('reply') != command or current.get('relayPolicy', {}).get('active') is not True:
            raise ReplyError('AUTOMATION_NOT_READY')
        if now_ms() >= send_request['expiresAt'] or abs(now_ms() - command['createdAt']) > 120_000:
            raise ReplyError('AUTOMATION_NOT_READY')
        if contact_command:
            latest = json.loads(db.execute("SELECT value FROM settings WHERE name='contacts_state'").fetchone()[0])
            if latest.get('snapshotId') != command['targetContactSnapshotId'] or latest.get('acknowledged') is not True:
                raise ReplyError('WECHAT_ACTION_CHANGED')
        with db:
            db.execute("UPDATE reply_commands SET status='COMMITTING' WHERE id=?", (command['id'],))
        result = execute(executor, send_request)
        if result != {'status': 'AUTOMATION_NOT_READY', 'clicked': False, 'reason': 'ui_executor_busy'}:
            break
        # This exact response precedes guest lock acquisition and every UI effect.
        with db:
            db.execute("UPDATE reply_commands SET status='RECEIVED' WHERE id=?", (command['id'],))
        sleep(min(1, max(0, (send_request['expiresAt'] - now_ms()) / 1000)))
    else:
        return 'AUTOMATION_NOT_READY'
    if result.get('clicked') is False and result.get('status') in ('AUTOMATION_NOT_READY', 'WECHAT_ACTION_CHANGED', 'WECHAT_WINDOW_TIMEOUT'):
        return result['status']
    if result.get('status') != 'UI_ACCEPTED' or result.get('clicked') is not True:
        return 'SEND_UNCONFIRMED'
    gate_id, clicked_at = result.get('sourceMaxId'), result.get('clickedAt')
    if (type(gate_id) is not int or gate_id < after_id or type(clicked_at) is not int
            or not started <= clicked_at <= now_ms()):
        return 'SEND_UNCONFIRMED'
    after_id = gate_id
    # Official UI acceptance is insufficient. Require one new server-assigned outgoing source row.
    seen = {}
    # ponytail: cap confirmation at 20 source pages; busy windows remain uncertain.
    # Raise the budget only if verified send traffic requires a larger scan.
    read_budget = 20
    for _ in range(8):
        upper_bound = None
        while True:
            if read_budget == 0:
                return 'SEND_UNCONFIRMED'
            read_budget -= 1
            batch = read_source(reader_command, after_id, include_media=False)
            watermark = batch.get('sourceMaxId')
            if (batch.get('accountFingerprint') != stored_account[0] or type(watermark) is not int
                    or watermark < after_id):
                return 'SEND_UNCONFIRMED'
            if upper_bound is None:
                upper_bound = watermark
            previous_id = after_id
            for outgoing in batch.get('messages', []):
                source_id = outgoing.get('msgId')
                if type(source_id) is not int or source_id <= 0 or source_id > watermark:
                    return 'SEND_UNCONFIRMED'
                if source_id <= previous_id or source_id > upper_bound:
                    continue
                after_id = max(after_id, source_id)
                if (outgoing.get('isSend') == 1 and outgoing.get('talker') == value['conversationId']
                        and outgoing.get('content') == value['body'] and outgoing.get('createTime', 0) >= clicked_at - 2000):
                    seen[source_id] = outgoing
            if len(seen) > 1:
                return 'SEND_UNCONFIRMED'
            if after_id >= upper_bound:
                break
            if after_id == previous_id:
                return 'SEND_UNCONFIRMED'
        if len(seen) == 1:
            outgoing = next(iter(seen.values()))
            # Re-read the same local row while WeChat assigns its server identity.
            confirmed = read_source(reader_command, 0, [outgoing['msgId']], include_media=False)
            records = confirmed.get('messages', [])
            if confirmed.get('accountFingerprint') != stored_account[0] or len(records) != 1:
                return 'SEND_UNCONFIRMED'
            current = records[0]
            if (current.get('msgId') == outgoing['msgId'] and current.get('isSend') == 1
                    and current.get('talker') == value['conversationId'] and current.get('content') == value['body']
                    and re.fullmatch('-?[1-9][0-9]*', str(current.get('msgSvrId', '0')))):
                return 'SENT_TO_WECHAT'
        sleep(1)
    return 'SEND_UNCONFIRMED'
