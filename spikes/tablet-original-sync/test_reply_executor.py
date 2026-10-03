import copy
import fcntl
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collector as c
import guest_reply as g
import reply_executor as r


class TabletReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.db = c.open_store(self.state)
        self.time = c.now_ms()
        self.config = {'pairId': '019d2f1a-7b4c-7d10-8c21-1c77be6a91b1',
                       'deviceId': 'tablet-device-0001', 'kA2I': c.encoded(bytes(range(32)))}
        self.reply_key = bytes(range(32, 64))
        self.row = {'msgId': 1, 'msgSvrId': '101', 'type': 1, 'createTime': self.time,
                    'isSend': 0, 'talker': 'wxid_fixture', 'content': 'incoming',
                    'conversationName': 'Fixture contact', 'media': [],
                    'replyIdentity': {'username': 'wxid_fixture',
                                      'contact': {'username': 'wxid_fixture', 'alias': 'fixture_alias'}}}
        self.batch = {'accountFingerprint': 'a' * 64, 'sourceMaxId': 1, 'messages': [self.row],
                      'contactSnapshot': {'state': 'ready', 'contacts': [
                          {'name': 'Fixture contact', 'conversationId': 'wxid_fixture', 'alias': 'fixture_alias'}]}}
        self.acks = []
        self.executions = []
        self.active = True
        self.polls = 0

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def target(self, sending=False):
        if sending:
            self.config['kI2A'] = c.encoded(self.reply_key)
        c.ingest(self.db, self.config, self.batch, reply_ready=sending)
        with self.db:
            self.db.execute('UPDATE messages SET uploaded=1')
        return json.loads(self.db.execute('SELECT body FROM messages WHERE source_id=?', (self.row['msgId'],)).fetchone()[0])

    def command(self, target, bootstrap=False):
        version = 6 if bootstrap else 5
        value = ({'v': 1, 'pairId': self.config['pairId'], 'deviceId': self.config['deviceId'], 'replyKey': c.encoded(self.reply_key)}
                 if bootstrap else {'body': '完整中文回复 👋', 'conversationId': self.row['talker'], 'accountFingerprint': 'a' * 64})
        command = {'v': version, 'id': c.uuid7(), 'pairId': self.config['pairId'], 'deviceId': self.config['deviceId'],
                   'targetMessageId': target['id'], 'createdAt': self.time, 'wechatUserId': 0}
        key = r.bootstrap_key(self.config, c.decoded) if bootstrap else self.reply_key
        command['replyEnvelope'] = c.seal(c.json_bytes(value), key, 'phase2-reply-bootstrap' if bootstrap else 'phase1-reply', r.command_aad(command, self.config))
        self.current = command
        return command

    def request(self, config, path, body, **kwargs):
        if kwargs.get('method') == 'GET':
            self.polls += 1
            return {'reply': self.current, 'relayPolicy': {'active': self.active}}
        status = json.loads(body)['status']
        self.acks.append(status)
        return {'replyId': self.current['id'], 'status': status}

    def read(self, command, after, ids=None, include_media=True, defer_media=False):
        self.assertFalse(include_media)
        if defer_media and ids is None:
            return self.batch
        if ids == [self.row['msgId']]:
            return self.batch
        outgoing = {**self.row, 'msgId': self.row['msgId'] + 1, 'msgSvrId': '102', 'isSend': 1, 'content': '完整中文回复 👋'}
        return {'accountFingerprint': 'a' * 64, 'sourceMaxId': outgoing['msgId'], 'messages': [outgoing]}

    def execute(self, executor, value):
        self.executions.append(value)
        return {'status': 'UI_ACCEPTED', 'clicked': True, 'sourceMaxId': self.batch['sourceMaxId'], 'clickedAt': self.time}

    def poll(self, execute=None, read=None):
        r.poll(self.db, self.config, self.state / 'device.json', ['private-source'], Path('/private-adapter'),
               self.request, read or self.read, c.save_config, c.decoded, c.encoded, c.json_bytes,
               execute=execute or self.execute, now_ms=lambda: self.time, sleep=lambda seconds: None)

    def test_bootstrap_retains_pair_device_outbox_and_uses_distinct_derived_key(self):
        target = self.target()
        before = self.db.execute('SELECT id,seq,body FROM messages').fetchall()
        self.command(target, bootstrap=True)
        self.poll()
        self.assertEqual(self.acks, ['REPLY_KEY_INSTALLED'])
        self.assertEqual(c.decoded(self.config['kI2A']), self.reply_key)
        self.assertNotEqual(r.bootstrap_key(self.config, c.decoded), c.decoded(self.config['kA2I']))
        self.assertEqual(self.db.execute('SELECT id,seq,body FROM messages').fetchall(), before)
        self.assertEqual(self.executions, [])
        self.assertEqual(json.loads((self.state / 'device.json').read_text())['pairId'], self.config['pairId'])

    def test_native_v8_message_bootstraps_and_replies(self):
        self.row['type'] = 49
        self.row['content'] = '<msg><appmsg><type>19</type><title>Fixture record</title><recorditem><![CDATA[<recordinfo><dataitem datatype="1"><datadesc>incoming</datadesc></dataitem></recordinfo>]]></recorditem></appmsg></msg>'
        target = self.target()
        self.assertEqual(target['v'], 8)
        self.command(target, bootstrap=True)
        self.poll()
        self.assertEqual(self.acks, ['REPLY_KEY_INSTALLED'])
        # A new native source row after bootstrap advertises reply capability.
        original = self.db.execute('SELECT id,seq,body FROM messages').fetchall()
        self.row['msgId'] = 3
        self.row['msgSvrId'] = '103'
        self.batch['sourceMaxId'] = 3
        target = self.target(sending=True)
        self.assertEqual(target['v'], 8)
        self.assertTrue(target['replyCapable'])
        self.command(target)
        self.poll()
        self.assertEqual(self.acks[-1], 'SENT_TO_WECHAT')
        self.assertEqual(self.db.execute('SELECT id,seq,body FROM messages WHERE source_id=1').fetchall(), original)

    def contact_target(self, key=True, acknowledge=True):
        if key:
            self.config['kI2A'] = c.encoded(self.reply_key)
        self.batch['messages'] = []
        self.batch['sourceMaxId'] = 0
        self.batch['contactSnapshot'] = {'state': 'ready', 'contacts': [
            {'name': 'Fixture contact', 'conversationId': 'wxid_fixture', 'alias': 'fixture_alias'},
            {'name': 'Fixture contact', 'conversationId': 'wxid_same_name', 'alias': 'other_alias'}]}
        if not acknowledge:
            self.batch['contactSnapshot']['contacts'][0]['name'] = 'Changed contact'
        c.ingest(self.db, self.config, self.batch)
        state = json.loads(c.setting(self.db, 'contacts_state'))
        snapshot = state.get('pending', {'id': state['snapshotId']})
        if acknowledge:
            c.upload_contacts(self.db, self.config, lambda config, path, raw: {'id': json.loads(raw)['id'], 'idempotent': False})
        return snapshot

    def contact_command(self, snapshot, bootstrap=False):
        command = {'v': 8 if bootstrap else 7, 'id': c.uuid7(), 'pairId': self.config['pairId'],
                   'deviceId': self.config['deviceId'], 'targetContactSnapshotId': snapshot['id'],
                   'createdAt': self.time, 'wechatUserId': 0}
        value = ({'v': 1, 'pairId': self.config['pairId'], 'deviceId': self.config['deviceId'], 'replyKey': c.encoded(self.reply_key)}
                 if bootstrap else {'body': '完整中文回复 👋', 'conversationId': 'wxid_fixture', 'accountFingerprint': 'a' * 64})
        key = r.bootstrap_key(self.config, c.decoded) if bootstrap else self.reply_key
        command['replyEnvelope'] = c.seal(c.json_bytes(value), key, 'phase2-reply-bootstrap' if bootstrap else 'phase1-reply', r.command_aad(command, self.config))
        self.current = command
        return command

    def test_contact_bootstrap_without_history_preserves_pair_and_never_enters_ui(self):
        self.contact_command(self.contact_target(key=False), bootstrap=True)
        before = self.db.execute('SELECT id,seq,body FROM messages').fetchall()
        self.poll()
        self.assertEqual(self.acks, ['REPLY_KEY_INSTALLED'])
        self.assertEqual(c.decoded(self.config['kI2A']), self.reply_key)
        self.assertEqual(self.config['deviceId'], 'tablet-device-0001')
        self.assertEqual(self.db.execute('SELECT id,seq,body FROM messages').fetchall(), before)
        self.assertFalse(self.executions)
        self.poll()
        self.assertEqual(self.acks[-1], 'REPLY_KEY_INSTALLED')

    def test_contact_bootstrap_cannot_replace_existing_key(self):
        self.contact_command(self.contact_target(key=False), bootstrap=True)
        self.config['kI2A'] = c.encoded(b'x' * 32)
        self.poll()
        self.assertEqual(self.acks, ['INVALID_REPLY'])
        self.assertEqual(c.decoded(self.config['kI2A']), b'x' * 32)

    def test_contact_send_without_history_uses_acknowledged_identity_and_real_watermark(self):
        snapshot = self.contact_target()
        self.contact_command(snapshot)
        self.poll()
        self.assertEqual(self.acks, ['SENT_TO_WECHAT'])
        self.assertEqual(self.executions[0]['sourceContactSnapshotId'], snapshot['id'])
        self.assertEqual(self.executions[0]['sourceMaxId'], 0)
        self.assertNotIn('sourceMessageId', self.executions[0])
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        self.poll()
        self.assertEqual(len(self.executions), 1)

    def test_contact_send_requires_bootstrapped_key_and_acknowledged_current_snapshot(self):
        self.contact_command(self.contact_target(key=False))
        self.poll()
        self.assertEqual(self.acks, ['INVALID_REPLY'])
        self.assertFalse(self.executions)
        self.contact_command(self.contact_target(acknowledge=False))
        self.poll()
        self.assertEqual(self.acks[-1], 'WECHAT_ACTION_CHANGED')
        self.assertFalse(self.executions)

    def test_contact_send_rejects_stale_snapshot_and_live_identity_changes(self):
        snapshot = self.contact_target()
        self.contact_command(snapshot)
        self.batch['contactSnapshot']['contacts'][0]['alias'] = 'changed_alias'
        self.poll()
        self.assertEqual(self.acks[-1], 'WECHAT_ACTION_CHANGED')
        self.assertFalse(self.executions)
        c.ingest(self.db, self.config, self.batch)
        self.contact_command(snapshot)
        self.poll()
        self.assertEqual(self.acks[-1], 'WECHAT_ACTION_CHANGED')
        self.assertFalse(self.executions)

    def test_contact_send_rejects_empty_duplicate_alias_account_switch_pause_and_expiry(self):
        for change in ('empty_alias', 'duplicate_alias', 'missing_friend', 'group', 'account', 'pause', 'expiry'):
            with self.subTest(change=change):
                self.active = True
                self.batch['accountFingerprint'] = 'a' * 64
                self.contact_command(self.contact_target())
                contacts = self.batch['contactSnapshot']['contacts']
                if change == 'empty_alias':
                    contacts[0]['alias'] = ''
                elif change == 'duplicate_alias':
                    contacts[1]['alias'] = contacts[0]['alias']
                elif change == 'missing_friend':
                    contacts.pop(0)
                elif change == 'group':
                    contacts[0]['conversationId'] = 'group@chatroom'
                elif change == 'account':
                    self.batch['accountFingerprint'] = 'b' * 64
                elif change == 'pause':
                    self.active = False
                else:
                    self.time += 120001
                self.poll()
                self.assertEqual(self.acks[-1], 'AUTOMATION_NOT_READY' if change in ('pause', 'expiry') else 'WECHAT_ACTION_CHANGED')
                self.assertFalse(self.executions)

    def test_bootstrap_replay_is_idempotent_and_never_enters_ui(self):
        self.command(self.target(), bootstrap=True)
        self.poll()
        self.poll()
        self.assertEqual(self.acks, ['REPLY_KEY_INSTALLED', 'REPLY_KEY_INSTALLED'])
        self.assertFalse(self.executions)

    def test_legacy_v6_message_bootstrap_does_not_require_a_sendable_friend(self):
        self.row['talker'] = 'fixture@chatroom'
        target = self.target()
        target['v'] = 6
        with self.db:
            self.db.execute('UPDATE messages SET body=? WHERE id=?', (c.json_bytes(target), target['id']))
        self.command(target, bootstrap=True)
        self.poll()
        self.assertEqual(self.acks, ['REPLY_KEY_INSTALLED'])
        self.assertFalse(self.executions)

    def test_contact_ui_acceptance_with_zero_server_identity_is_uncertain_and_never_repeats(self):
        self.contact_command(self.contact_target())
        def pending_server(command, after, ids=None, include_media=True, defer_media=False):
            if defer_media:
                return self.batch
            outgoing = {**self.row, 'msgId': 2, 'msgSvrId': '0', 'isSend': 1, 'content': '完整中文回复 👋'}
            return {'accountFingerprint': 'a' * 64, 'sourceMaxId': 2, 'messages': [outgoing]}
        self.poll(read=pending_server)
        self.assertEqual(self.acks, ['SEND_UNCONFIRMED'])
        self.poll(read=pending_server)
        self.assertEqual(len(self.executions), 1)

    def test_message_reply_requires_current_ordinary_friend_even_with_valid_old_identity(self):
        self.command(self.target(sending=True))
        self.batch['contactSnapshot']['contacts'] = []
        self.poll()
        self.assertEqual(self.acks, ['WECHAT_ACTION_CHANGED'])
        self.assertFalse(self.executions)

    def test_changed_pair_cannot_decrypt_bootstrap(self):
        command = self.command(self.target(), bootstrap=True)
        other = {**self.config, 'pairId': '019d2f1a-7b4c-7d10-8c21-1c77be6a91b2'}
        forged = {**command, 'pairId': other['pairId'], 'replyEnvelope': {**command['replyEnvelope']}}
        forged['replyEnvelope']['aad'] = r.command_aad(forged, other)
        with self.assertRaisesRegex(r.ReplyError, 'INVALID_REPLY'):
            r.decrypt_command(forged, other, c.decoded)

    def test_existing_different_reply_key_is_not_replaced(self):
        self.command(self.target(), bootstrap=True)
        self.config['kI2A'] = c.encoded(b'x' * 32)
        self.poll()
        self.assertEqual(self.acks, ['INVALID_REPLY'])
        self.assertEqual(c.decoded(self.config['kI2A']), b'x' * 32)

    def test_one_thousand_emoji_fit_tablet_envelope_with_identity_overhead(self):
        command = self.command(self.target(sending=True))
        value = {'body': '👋' * 1000, 'conversationId': self.row['talker'], 'accountFingerprint': 'a' * 64}
        command['replyEnvelope'] = c.seal(c.json_bytes(value), self.reply_key, 'phase1-reply', r.command_aad(command, self.config))
        self.assertGreater(len(c.decoded(command['replyEnvelope']['ct'])), 4096)
        self.assertEqual(r.decrypt_command(command, self.config, c.decoded), value)

    def test_success_requires_official_ui_and_new_outgoing_server_identity(self):
        self.command(self.target(sending=True))
        self.poll()
        self.assertEqual(self.acks, ['SENT_TO_WECHAT'])
        self.assertEqual(len(self.executions), 1)

    def test_contact_send_waits_only_for_proven_busy_then_sends_once(self):
        self.contact_command(self.contact_target())
        def busy_once(executor, value):
            if not self.executions:
                self.executions.append(value)
                return {'status': 'AUTOMATION_NOT_READY', 'clicked': False, 'reason': 'ui_executor_busy'}
            return self.execute(executor, value)
        self.poll(execute=busy_once)
        self.assertEqual(self.acks, ['SENT_TO_WECHAT'])
        self.assertEqual(len(self.executions), 2)
        self.assertEqual(self.executions[0], self.executions[1])
        self.assertGreaterEqual(self.polls, 3)
        self.poll(execute=busy_once)
        self.assertEqual(len(self.executions), 2)

    def test_busy_retry_rechecks_policy_expiry_and_current_command(self):
        for change in ('pause', 'expiry', 'command'):
            with self.subTest(change=change):
                self.active = True
                self.executions = []
                self.contact_command(self.contact_target())
                def busy(executor, value):
                    self.executions.append(value)
                    if change == 'pause':
                        self.active = False
                    elif change == 'expiry':
                        self.time += 120000
                    else:
                        self.current = {**self.current, 'createdAt': self.current['createdAt'] + 1}
                    return {'status': 'AUTOMATION_NOT_READY', 'clicked': False, 'reason': 'ui_executor_busy'}
                self.poll(execute=busy)
                self.assertEqual(self.acks[-1], 'AUTOMATION_NOT_READY')
                self.assertEqual(len(self.executions), 1)

    def test_uncertain_click_or_other_ui_failure_never_retries(self):
        results = [({'status': 'AUTOMATION_NOT_READY', 'clicked': None, 'reason': 'ui_executor_busy'}, 'SEND_UNCONFIRMED'),
                   ({'status': 'AUTOMATION_NOT_READY', 'clicked': True, 'reason': 'ui_executor_busy'}, 'SEND_UNCONFIRMED'),
                   ({'status': 'SEND_UNCONFIRMED', 'clicked': False, 'reason': 'ui_executor_busy'}, 'SEND_UNCONFIRMED'),
                   ({'status': 'AUTOMATION_NOT_READY', 'clicked': False}, 'AUTOMATION_NOT_READY'),
                   ({'status': 'WECHAT_ACTION_CHANGED', 'clicked': False}, 'WECHAT_ACTION_CHANGED')]
        for result, expected in results:
            with self.subTest(result=result):
                self.executions = []
                self.command(self.target(sending=True))
                def failure(executor, value):
                    self.executions.append(value)
                    return result
                self.poll(execute=failure)
                self.assertEqual(self.acks[-1], expected)
                self.poll(execute=failure)
                self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.executions[0]['alias'], 'fixture_alias')
        self.assertEqual(self.executions[0]['body'], '完整中文回复 👋')
        self.poll()
        self.assertEqual(len(self.executions), 1)

    def test_crash_after_commit_is_uncertain_and_never_reclicks(self):
        self.command(self.target(sending=True))
        def interrupted(executor, value):
            self.executions.append(value)
            raise RuntimeError('simulated crash')
        with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
            self.poll(execute=interrupted)
        self.poll()
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.acks[-1], 'SEND_UNCONFIRMED')

    def test_account_recipient_and_group_changes_fail_before_ui(self):
        self.command(self.target(sending=True))
        self.batch['accountFingerprint'] = 'b' * 64
        self.poll()
        self.assertEqual(self.acks, ['WECHAT_ACTION_CHANGED'])
        self.assertFalse(self.executions)
        group = copy.deepcopy(self.row)
        group['talker'] = 'fixture@chatroom'
        group['replyIdentity']['username'] = group['talker']
        group['replyIdentity']['contact']['username'] = group['talker']
        self.assertFalse(r.reply_candidate(group))

    def test_source_read_expiry_and_current_pause_prevent_send(self):
        self.command(self.target(sending=True))
        def slow_reader(command, after, ids=None, include_media=True, defer_media=False):
            self.time += 120_001
            return self.read(command, after, ids, include_media, defer_media)
        self.poll(read=slow_reader)
        self.assertEqual(self.acks, ['AUTOMATION_NOT_READY'])
        self.assertFalse(self.executions)

    def test_policy_refresh_after_source_read_prevents_send(self):
        self.command(self.target(sending=True))
        def pausing_reader(command, after, ids=None, include_media=True, defer_media=False):
            self.active = False
            return self.read(command, after, ids, include_media, defer_media)
        self.poll(read=pausing_reader)
        self.assertEqual(self.acks, ['AUTOMATION_NOT_READY'])
        self.assertFalse(self.executions)

    def test_ui_acceptance_without_source_confirmation_is_uncertain(self):
        self.command(self.target(sending=True))
        def no_sent_row(command, after, ids=None, include_media=True, defer_media=False):
            return self.batch if ids == [1] else {'accountFingerprint': 'a' * 64, 'messages': []}
        self.poll(read=no_sent_row)
        self.assertEqual(self.acks, ['SEND_UNCONFIRMED'])

    def test_existing_identical_outgoing_cannot_confirm_this_click(self):
        self.command(self.target(sending=True))
        self.batch['sourceMaxId'] = 2
        def accepted(executor, value):
            self.executions.append(value)
            return {'status': 'UI_ACCEPTED', 'clicked': True, 'sourceMaxId': 2, 'clickedAt': self.time}
        self.poll(execute=accepted)
        self.assertEqual(self.acks, ['SEND_UNCONFIRMED'])
        self.assertEqual(self.executions[0]['sourceMaxId'], 2)

    def test_missing_or_older_gate_evidence_cannot_report_success(self):
        self.command(self.target(sending=True))
        self.poll(execute=lambda executor, value: {'status': 'UI_ACCEPTED', 'clicked': True,
                                                  'sourceMaxId': 0, 'clickedAt': self.time})
        self.assertEqual(self.acks, ['SEND_UNCONFIRMED'])

    def test_missing_source_watermark_stops_before_executor(self):
        self.command(self.target(sending=True))
        del self.batch['sourceMaxId']
        self.poll()
        self.assertEqual(self.acks, ['AUTOMATION_NOT_READY'])
        self.assertFalse(self.executions)

    def test_confirmation_scans_later_pages_before_deciding_unique_outgoing(self):
        for second_match in (True, False):
            with self.subTest(second_match=second_match):
                self.command(self.target(sending=True))
                matching = {**self.row, 'msgId': 2, 'msgSvrId': '102', 'isSend': 1, 'content': '完整中文回复 👋'}
                first = [matching] + [{**self.row, 'msgId': source_id, 'msgSvrId': str(100 + source_id)} for source_id in range(3, 22)]
                second = {**matching, 'msgId': 22, 'msgSvrId': '122', 'content': matching['content'] if second_match else 'Different outgoing'}
                pages = []
                def paged(command, after, ids=None, include_media=True, defer_media=False):
                    if ids == [1]:
                        return self.batch
                    if ids == [2]:
                        self.assertEqual(pages, [1, 21])
                        return {'accountFingerprint': 'a' * 64, 'sourceMaxId': 22, 'messages': [matching]}
                    pages.append(after)
                    return {'accountFingerprint': 'a' * 64, 'sourceMaxId': 22,
                            'messages': first if after == 1 else [second]}
                self.poll(read=paged)
                self.assertEqual(pages, [1, 21])
                self.assertEqual(self.acks[-1], 'SEND_UNCONFIRMED' if second_match else 'SENT_TO_WECHAT')

    def test_confirmation_exhausted_page_budget_stays_uncertain(self):
        self.command(self.target(sending=True))
        matching = {**self.row, 'msgId': 2, 'msgSvrId': '102', 'isSend': 1, 'content': '完整中文回复 👋'}
        def unbounded(command, after, ids=None, include_media=True, defer_media=False):
            if ids == [1]:
                return self.batch
            self.assertIsNone(ids)
            row = matching if after == 1 else {**self.row, 'msgId': after + 1}
            return {'accountFingerprint': 'a' * 64, 'sourceMaxId': 10000, 'messages': [row]}
        self.poll(read=unbounded)
        self.assertEqual(self.acks, ['SEND_UNCONFIRMED'])

    def test_older_helper_without_source_gate_cannot_advertise_ready(self):
        for protocol, expected in [(1, False), (2, True)]:
            with patch.object(r, 'executor_request', return_value={'status': 'READY', 'protocol': protocol,
                                                                 'recipientProof': 'wechat-profile-alias'}):
                self.assertEqual(r.executor_ready(Path('/private-adapter')), expected)


class GuestLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / '.local/share/aurora-tablet-ui'
        self.state.mkdir(parents=True, mode=0o700)
        self.command = {'v': 1, 'operation': 'send', 'expiresAt': 121000}

    def tearDown(self):
        self.temp.cleanup()

    def test_waiting_send_blocks_new_media_and_rechecks_source_after_acquiring_lock(self):
        waits = []
        with (self.state / 'executor.lock').open('a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release(seconds):
                waits.append(seconds)
                verify.assert_not_called()
                media = g.perform({'v': 1, 'operation': 'download-image'})
                self.assertEqual(media, {'status': 'AUTOMATION_NOT_READY', 'clicked': False, 'reason': 'ui_executor_busy'})
                docker.assert_not_called()
                fcntl.flock(holder, fcntl.LOCK_UN)
            def response(*arguments, **kwargs):
                if arguments[:3] == ('cmd', 'package', 'list'):
                    return ('package:' + g.PACKAGE + ' versionCode:3').encode()
                if arguments[-1] == 'files/result.json':
                    return b'{"status":"UI_ACCEPTED","clicked":true}'
                return b''
            with patch.object(g.Path, 'home', return_value=self.root), \
                    patch.object(g.time, 'time_ns', return_value=1000 * 1000000), \
                    patch.object(g.time, 'sleep', side_effect=release), \
                    patch.object(g, 'verify_source', return_value=0) as verify, \
                    patch.object(g, 'docker', side_effect=response) as docker, \
                    patch.object(g, 'instrument') as instrument:
                self.assertEqual(g.perform(self.command), {'status': 'UI_ACCEPTED', 'clicked': True})
                self.assertEqual(len(waits), 1)
                verify.assert_called_once_with(self.command)
                instrument.assert_called_once()

    def test_busy_guest_wait_is_bounded_and_has_no_source_or_ui_effects(self):
        clock = [0.0]
        waits = []
        def advance(seconds):
            waits.append(seconds)
            clock[0] += seconds
        with (self.state / 'executor.lock').open('a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(g.Path, 'home', return_value=self.root), \
                    patch.object(g.time, 'time_ns', return_value=1000 * 1000000), \
                    patch.object(g.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(g.time, 'sleep', side_effect=advance), \
                    patch.object(g, 'verify_source') as verify, patch.object(g, 'docker') as docker:
                self.assertEqual(g.perform(self.command), {'status': 'AUTOMATION_NOT_READY', 'clicked': False, 'reason': 'ui_executor_busy'})
                self.assertAlmostEqual(sum(waits), 10, places=6)
                self.assertGreaterEqual(sum(waits), 9.9)
                verify.assert_not_called()
                docker.assert_not_called()


class GuestAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.command = {'replyId': '019d2f1a-7b4c-7d10-8c21-1c77be6a91b1', 'expiresAt': 5000}
        self.raw = c.json_bytes(self.command)
        self.gate = {'replyId': self.command['replyId'], 'commandHash': hashlib.sha256(self.raw).hexdigest(),
                     'nonce': '019d2f1a-7b4c-7d10-8c21-1c77be6a91b2'}

    def test_grant_rechecks_source_and_binds_nonce_digest_and_short_expiry(self):
        verified = []
        def verify(command, timeout):
            verified.append((command, timeout))
            return 42
        grant = g.authorize_send(self.command, self.raw, self.gate, verify=verify, now_ms=lambda: 1000)
        self.assertEqual(grant, {**self.gate, 'sourceMaxId': 42, 'expiresAt': 3000})
        self.assertEqual(verified, [(self.command, 20)])

    def test_changed_account_never_receives_grant(self):
        def changed(command, timeout):
            raise RuntimeError('source_account_changed')
        with self.assertRaisesRegex(RuntimeError, 'source_account_changed'):
            g.authorize_send(self.command, self.raw, self.gate, verify=changed, now_ms=lambda: 1000)

    def test_other_command_digest_or_nonce_is_rejected_before_source_read(self):
        def unexpected(command, timeout):
            self.fail('invalid gate reached source reader')
        for field, value in [('replyId', 'other'), ('commandHash', '0' * 64), ('nonce', 'invalid')]:
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'source_gate_invalid'):
                g.authorize_send(self.command, self.raw, {**self.gate, field: value}, verify=unexpected, now_ms=lambda: 1000)

    def test_expiry_during_source_read_is_rejected(self):
        clock = [1000]
        def slow(command, timeout):
            clock[0] = 5000
            return 42
        with self.assertRaisesRegex(RuntimeError, 'source_gate_expired'):
            g.authorize_send(self.command, self.raw, self.gate, verify=slow, now_ms=lambda: clock[0])

    def test_contact_source_grant_rechecks_current_ordinary_friend_and_watermark(self):
        command = {**self.command, 'sourceContactSnapshotId': c.uuid7(), 'conversationId': 'fixture',
                   'accountFingerprint': 'a' * 64, 'alias': 'exact_alias', 'conversationName': 'Same name',
                   'sourceMaxId': 10}
        batch = {'accountFingerprint': 'a' * 64, 'sourceMaxId': 12, 'messages': [],
                 'contactSnapshot': {'state': 'ready', 'contacts': [
                     {'conversationId': 'fixture', 'alias': 'exact_alias', 'name': 'Same name'},
                     {'conversationId': 'other', 'alias': 'other_alias', 'name': 'Same name'}]}}
        raw = c.json_bytes(command)
        gate = {**self.gate, 'commandHash': hashlib.sha256(raw).hexdigest()}
        with patch.object(g.SOURCE, 'read', return_value=batch) as read:
            grant = g.authorize_send(command, raw, gate, verify=g.verify_source, now_ms=lambda: 1000)
            self.assertEqual(grant['sourceMaxId'], 12)
            read.assert_called_once_with(0, [], False, True, timeout=20, limit=1)
        for change in ('account', 'alias', 'duplicate_alias', 'removed', 'service', 'older_watermark'):
            changed = copy.deepcopy(batch)
            if change == 'account':
                changed['accountFingerprint'] = 'b' * 64
            elif change == 'alias':
                changed['contactSnapshot']['contacts'][0]['alias'] = 'changed'
            elif change == 'duplicate_alias':
                changed['contactSnapshot']['contacts'][1]['alias'] = 'exact_alias'
            elif change == 'removed':
                changed['contactSnapshot']['contacts'].pop(0)
            elif change == 'service':
                changed['contactSnapshot']['contacts'][0]['conversationId'] = 'filehelper'
            else:
                changed['sourceMaxId'] = 9
            with self.subTest(change=change), patch.object(g.SOURCE, 'read', return_value=changed), self.assertRaises(RuntimeError):
                g.authorize_send(command, raw, gate, verify=g.verify_source, now_ms=lambda: 1000)


if __name__ == '__main__':
    unittest.main()
