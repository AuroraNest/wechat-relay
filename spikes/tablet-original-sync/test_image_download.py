import hashlib
import html
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import collector
import guest_reply
import image_download


class ImageDownloadTests(unittest.TestCase):
    def test_busy_ui_reports_contention_without_touching_wechat(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home, \
                patch.object(guest_reply.Path, 'home', return_value=Path(home)), \
                patch.object(guest_reply.fcntl, 'flock', side_effect=BlockingIOError), \
                patch.object(guest_reply, 'docker') as docker:
            result = guest_reply.perform({'v': 1, 'operation': 'download-record-image'})
        self.assertEqual(result['reason'], 'ui_executor_busy')
        self.assertFalse(result['clicked'])
        docker.assert_not_called()

    def setUp(self):
        self.md5 = hashlib.md5(b'original-fixture').hexdigest()
        self.row = {'msgId': 10, 'msgSvrId': '123', 'type': 3, 'talker': 'fixture@chatroom',
                    'content': f'<msg><img hdlength="16" md5="{self.md5}" /></msg>',
                    'media': [{'kind': 'image', 'state': 'pending'}]}
        self.batch = {'accountFingerprint': 'a' * 64, 'sourceMaxId': 20, 'messages': [self.row]}
        self.command = image_download.image_command(self.batch, self.row, 1000)

    def record_batch(self, data_ids=('first', 'second')):
        items = ''.join(f'<dataitem datatype="2" dataid="{data_id}"><datasize>16</datasize>'
                        f'<fullmd5>{self.md5}</fullmd5></dataitem>' for data_id in data_ids)
        xml = '<recordinfo><datalist>' + items + '</datalist></recordinfo>'
        row = {**self.row, 'type': 49,
               'content': '<msg><appmsg><type>19</type><recorditem>' + html.escape(xml) + '</recorditem></appmsg></msg>',
               'media': [{'kind': 'image', 'state': 'pending', 'recordItemIndex': index}
                         for index in range(len(data_ids))]}
        return {**self.batch, 'messages': [row]}

    def record_sticker_batch(self):
        batch = self.record_batch()
        row = batch['messages'][0]
        row['content'] = row['content'].replace('datatype=&quot;2&quot;', 'datatype=&quot;37&quot;')
        row['content'] = row['content'].replace('&lt;datasize&gt;16&lt;/datasize&gt;', '&lt;datasize&gt;0&lt;/datasize&gt;')
        for item in row['media']:
            item['kind'] = 'sticker'
        return batch

    def test_record_sticker_params_and_exact_source_with_unknown_length(self):
        batch = self.record_sticker_batch()
        commands = image_download.image_commands(batch, batch['messages'][0], 1000)
        self.assertEqual(len(commands), 2)
        command = commands[0]
        self.assertEqual(command['operation'], 'download-record-sticker')
        self.assertEqual(command['originalByteLength'], 0)
        self.assertEqual(guest_reply.validate_image_batch(command, batch), 20)
        for key, value in (('originalByteLength', 16), ('recordDataId', 'changed'), ('originalMD5', '0' * 32)):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                guest_reply.validate_image_batch({**command, key: value}, batch)
        with patch.object(guest_reply, 'docker') as docker, \
                patch.object(guest_reply, 'viewer_task', side_effect=[None, 12]):
            self.assertEqual(guest_reply.open_image_viewer(command), 12)
        args = docker.call_args.args
        self.assertIn('com.tencent.mm/' + guest_reply.RECORD_DETAIL_VIEWER, args)
        self.assertEqual(args[-2], 'params')
        self.assertEqual(json.loads(args[-1]), {'msgId': 10, 'msgTalker': 'fixture@chatroom',
                                               'msgSvrId': 123, 'showShare': False})
        self.assertNotIn('record_data_id', args)

    def test_record_sticker_unknown_size_requires_verified_positive_actual_image(self):
        batch = self.record_sticker_batch()
        command = image_download.image_commands(batch, batch['messages'][0], 1000)[0]
        ready = {'kind': 'sticker', 'state': 'available', 'recordItemIndex': 0,
                 'byteLength': 16, 'mimeType': 'image/gif', 'sha256': 'a' * 64}
        for item, success in ((ready, True), ({**ready, 'mimeType': 'application/octet-stream'}, False),
                              ({**ready, 'byteLength': 0}, False)):
            current = {**batch, 'messages': [{**batch['messages'][0], 'media': [item]}]}
            with self.subTest(item=item), patch.object(guest_reply, 'read_batch', return_value=current), \
                    patch.object(guest_reply.time, 'time_ns', side_effect=[1_000_000_000, 400_000_000_000]), \
                    patch.object(guest_reply.time, 'monotonic', return_value=0), \
                    patch.object(guest_reply.time, 'sleep'):
                self.assertEqual(guest_reply.wait_image_original(command), success)

    def test_modern_record_activity_is_never_a_native_image_button_target(self):
        activity = b' topResumedActivity=ActivityRecord{abcd u0 com.tencent.mm/.feature.appmsg.ui.RecordDetailUI t12}'
        with patch.object(guest_reply, 'docker', return_value=activity):
            self.assertEqual(guest_reply.viewer_task(), 12)
            self.assertIsNone(guest_reply.viewer_activity(image_only=True))
            with self.assertRaisesRegex(RuntimeError, 'source_viewer_changed'):
                guest_reply.native_image_snapshot(12)

    def test_record_sticker_offscreen_is_unconfirmed_and_each_item_retries_independently(self):
        import tempfile
        batch = self.record_sticker_batch()
        command = image_download.image_commands(batch, batch['messages'][0], 1000)[0]
        with tempfile.TemporaryDirectory() as home, \
                patch.object(guest_reply.Path, 'home', return_value=Path(home)), \
                patch.object(guest_reply, 'docker', return_value=b'package:com.tencent.mm versionCode:3180'), \
                patch.object(guest_reply, 'verify_image_source'), \
                patch.object(guest_reply, 'open_image_viewer', return_value=12), \
                patch.object(guest_reply, 'wait_record_sticker', return_value=False), \
                patch.object(guest_reply, 'close_image_viewer') as close, \
                patch.object(guest_reply, 'instrument') as instrument, \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000):
            response = guest_reply.perform(command)
            self.assertEqual(response['status'], 'DOWNLOAD_UNCONFIRMED')
            instrument.assert_not_called()
            close.assert_called_once_with(12)
        attempts, seen = {}, []
        def request(executor, current):
            seen.append(current['recordDataId'])
            return response
        for _ in range(2):
            self.assertEqual(image_download.download_pending(batch, Path('/fixture'), 'a' * 64, attempts,
                                                             request=request, now_ms=lambda: 1000), (10, [10], True))
        self.assertEqual(seen, ['first', 'second'])
        self.assertTrue(all(due > 1000 for _, due in attempts.values()))

    def test_record_items_have_independent_retry_and_preserve_same_message_remaining(self):
        batch = self.record_batch()
        commands = image_download.image_commands(batch, batch['messages'][0], 1000)
        self.assertEqual(len({command['replyId'] for command in commands}), 2)
        attempts, seen = {}, []
        def request(executor, command):
            seen.append(command['recordDataId'])
            return {'status': 'DOWNLOAD_UNCONFIRMED'}
        for expected in ((10, [10], True), (10, [10], True), (None, [], True)):
            self.assertEqual(image_download.download_pending(batch, Path('/fixture'), 'a' * 64, attempts,
                                                             request=request, now_ms=lambda: 1000), expected)
        self.assertEqual(seen, ['first', 'second'])
        self.assertEqual(len(attempts), 2)

    def test_record_source_rejects_changed_data_id_duplicate_id_and_wrong_account(self):
        batch = self.record_batch()
        command = image_download.image_commands(batch, batch['messages'][0], 1000)[0]
        self.assertEqual(guest_reply.validate_image_batch(command, batch), 20)
        for changed in (self.record_batch(('changed', 'second')), self.record_batch(('first', 'first')),
                        {**batch, 'accountFingerprint': 'b' * 64}):
            with self.assertRaises(RuntimeError):
                guest_reply.validate_image_batch(command, changed)
        for key, value in (('sourceServerId', '999'), ('conversationId', 'other'),
                           ('recordItemIndex', 1), ('originalMD5', '0' * 32),
                           ('originalByteLength', 17), ('sourceMaxId', 21)):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                guest_reply.validate_image_batch({**command, key: value}, batch)
        duplicate = self.record_batch(('same', 'same'))
        self.assertEqual(image_download.image_commands(duplicate, duplicate['messages'][0], 1000), [])

    def test_record_ready_asset_is_untouched_and_only_datatype_two_opens_gallery(self):
        batch = self.record_batch()
        row = batch['messages'][0]
        row['media'][0] = {'kind': 'image', 'state': 'ready', 'recordItemIndex': 0, 'assetId': 'existing'}
        self.assertEqual([command['recordDataId'] for command in image_download.image_commands(batch, row, 1000)], ['second'])
        self.assertEqual(row['media'][0]['assetId'], 'existing')
        row['content'] = row['content'].replace('datatype=&quot;2&quot;', 'datatype=&quot;8&quot;')
        self.assertEqual(image_download.image_commands(batch, row, 1000), [])

    def test_record_viewer_intent_uses_message_and_data_id_without_xml_or_page_index(self):
        batch = self.record_batch()
        command = image_download.image_commands(batch, batch['messages'][0], 1000)[1]
        with patch.object(guest_reply, 'docker') as docker, patch.object(guest_reply, 'viewer_task', side_effect=[None, 12]):
            self.assertEqual(guest_reply.open_image_viewer(command), 12)
        args = docker.call_args.args
        self.assertIn('com.tencent.mm/' + guest_reply.RECORD_IMAGE_VIEWER, args)
        self.assertIn('message_id', args)
        self.assertIn('message_talker', args)
        self.assertIn('record_data_id', args)
        self.assertIn('second', args)
        self.assertNotIn('record_xml', args)
        self.assertNotIn('recordItemIndex', args)
        with patch.object(guest_reply, 'docker'), patch.object(guest_reply, 'viewer_task', return_value=12):
            with self.assertRaisesRegex(RuntimeError, 'source_viewer_unavailable'):
                guest_reply.open_image_viewer(command)

    def test_record_wait_checks_exact_item_full_bytes_and_ui_open_is_not_completion(self):
        import tempfile
        batch = self.record_batch()
        command = image_download.image_commands(batch, batch['messages'][0], 1000)[0]
        def docker(*args, **kwargs):
            self.assertEqual(args[-1], 'com.tencent.mm')
            return b'package:com.tencent.mm versionCode:3180'
        with tempfile.TemporaryDirectory() as home, \
                patch.object(guest_reply.Path, 'home', return_value=Path(home)), \
                patch.object(guest_reply, 'docker', side_effect=docker), \
                patch.object(guest_reply, 'verify_image_source'), \
                patch.object(guest_reply, 'open_image_viewer', return_value=12), \
                patch.object(guest_reply, 'wait_image_original', return_value=False), \
                patch.object(guest_reply, 'close_image_viewer') as close, \
                patch.object(guest_reply, 'instrument') as instrument, \
                patch.object(guest_reply, 'request_native_image') as native, \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000):
            self.assertEqual(guest_reply.perform(command)['status'], 'DOWNLOAD_UNCONFIRMED')
            close.assert_called_once_with(12)
            instrument.assert_not_called()
            native.assert_not_called()
        ready = {'kind': 'image', 'state': 'available', 'recordItemIndex': 0, 'byteLength': 16, 'sha256': 'a' * 64}
        wrong = {**batch, 'messages': [{**batch['messages'][0], 'media': [
            batch['messages'][0]['media'][0], {**ready, 'recordItemIndex': 1}]}]}
        exact = {**batch, 'messages': [{**batch['messages'][0], 'media': [ready]}]}
        with patch.object(guest_reply, 'read_batch', side_effect=[wrong, exact]) as read, \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000), \
                patch.object(guest_reply.time, 'monotonic', return_value=0), patch.object(guest_reply.time, 'sleep'):
            self.assertTrue(guest_reply.wait_image_original(command))
            self.assertEqual(read.call_count, 2)
        unverified = {**batch, 'messages': [{**batch['messages'][0], 'media': [
            {**ready, 'originalBytesVerified': False}]}]}
        with patch.object(guest_reply, 'read_batch', return_value=unverified), \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000), \
                patch.object(guest_reply.time, 'monotonic', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'source_image_changed'):
                guest_reply.wait_image_original(command)

    def test_exact_image_identity_supports_group_without_alias_or_screen_index(self):
        self.assertEqual(self.command['sourceMessageId'], 10)
        self.assertEqual(self.command['sourceServerId'], '123')
        self.assertEqual(self.command['conversationId'], 'fixture@chatroom')
        self.assertEqual(self.command['originalMD5'], self.md5)
        self.assertEqual(self.command['originalByteLength'], 16)
        self.assertNotIn('body', self.command)

    def test_ready_unknown_and_oversized_originals_do_not_open_ui(self):
        for row in ({**self.row, 'media': [{'state': 'ready'}]},
                    {**self.row, 'content': '<msg><img /></msg>'},
                    {**self.row, 'content': f'<msg><img hdlength="8388609" md5="{self.md5}" /></msg>'}):
            self.assertIsNone(image_download.image_command(self.batch, row, 1000))

    def test_preview_and_hd_have_independent_retry_identity(self):
        self.assertTrue(self.command['downloadPreview'])
        row = {**self.row, 'imagePreview': {'state': 'available'}}
        original = image_download.image_command(self.batch, row, 1000)
        self.assertNotIn('downloadPreview', original)
        self.assertNotEqual(original['replyId'], self.command['replyId'])
        self.assertEqual(original['originalMD5'], self.command['originalMD5'])

    def test_preview_retry_precedes_fresh_hd_upgrade(self):
        high = {**self.row, 'msgId': 11, 'msgSvrId': '124', 'imagePreview': {'state': 'available'}}
        attempts = {self.command['replyId']: (1, 0)}
        requested = []
        image_download.download_pending({**self.batch, 'messages': [self.row, high]}, Path('/fixture'),
                                        'a' * 64, attempts, now_ms=lambda: 1000,
                                        request=lambda _, command: requested.append(command) or {'status': 'PREVIEW_AVAILABLE'})
        self.assertEqual(requested[0]['sourceMessageId'], 10)
        self.assertTrue(requested[0]['downloadPreview'])

    def test_preview_ready_never_satisfies_original_and_rejects_unverified_cache(self):
        preview = {'state': 'available', 'sourceRepresentation': 'native_db_decoded',
                   'originalBytesVerified': False, 'mimeType': 'image/jpeg', 'byteLength': 9, 'sha256': 'a' * 64}
        batch = {**self.batch, 'messages': [{**self.row, 'imagePreview': preview}]}
        for representation in ('native_db_decoded', 'native_db_thumbnail'):
            preview['sourceRepresentation'] = representation
            with patch.object(guest_reply, 'read_batch', return_value=batch):
                self.assertTrue(guest_reply.image_original_ready(self.command, allow_preview=True))
                self.assertFalse(guest_reply.image_original_ready(self.command))
        for change in ({'state': 'pending'}, {'sourceRepresentation': 'thumbnail'}, {'originalBytesVerified': True},
                       {'byteLength': 0}, {'sha256': ''}, {'mimeType': 'application/octet-stream'}):
            invalid = {**batch, 'messages': [{**self.row, 'imagePreview': {**preview, **change}}]}
            with self.subTest(change=change), patch.object(guest_reply, 'read_batch', return_value=invalid):
                self.assertFalse(guest_reply.image_original_ready(self.command, allow_preview=True))
        with patch.object(guest_reply, 'read_batch', return_value={**batch, 'accountFingerprint': 'b' * 64}):
            with self.assertRaisesRegex(RuntimeError, 'source_account_changed'):
                guest_reply.image_original_ready(self.command, allow_preview=True)

    def test_preview_phase_does_not_request_hd_and_closes_after_preview_check(self):
        import tempfile
        def docker(*args, **kwargs):
            package = args[-1]
            return ('package:' + package + ' versionCode:' + ('3' if package == guest_reply.PACKAGE else '3180')).encode()
        with tempfile.TemporaryDirectory() as home, patch.object(guest_reply.Path, 'home', return_value=Path(home)), \
                patch.object(guest_reply, 'docker', side_effect=docker), \
                patch.object(guest_reply, 'verify_image_source'), \
                patch.object(guest_reply, 'open_image_viewer', return_value=12), \
                patch.object(guest_reply, 'wait_image_original', return_value=True) as wait, \
                patch.object(guest_reply, 'wait_hd_image') as hd, \
                patch.object(guest_reply, 'request_native_image') as request, \
                patch.object(guest_reply, 'close_image_viewer') as close, \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000):
            result = guest_reply.perform(self.command)
        self.assertEqual(result, {'status': 'PREVIEW_AVAILABLE', 'clicked': False})
        wait.assert_called_once_with({**self.command, 'viewerTask': 12}, timeout=45, allow_preview=True)
        hd.assert_not_called()
        request.assert_not_called()
        close.assert_called_once_with(12)

    def test_preview_mode_rejects_invalid_flag_and_non_hd_source(self):
        for value in ('true', 1):
            with patch.object(guest_reply, 'read_batch', return_value=self.batch):
                with self.assertRaisesRegex(RuntimeError, 'source_image_changed'):
                    guest_reply.verify_image_source({**self.command, 'downloadPreview': value})
        row = {**self.row, 'content': f'<msg><img length="16" md5="{self.md5}" /></msg>'}
        with patch.object(guest_reply, 'read_batch', return_value={**self.batch, 'messages': [row]}):
            with self.assertRaisesRegex(RuntimeError, 'source_image_changed'):
                guest_reply.verify_image_source({**self.command, 'requiresHD': False})

    def test_missing_source_md5_still_requires_exact_declared_original_size(self):
        row = {**self.row, 'content': '<msg><img length="12" hdlength="16" /></msg>'}
        command = image_download.image_command(self.batch, row, 1000)
        self.assertEqual(command['originalMD5'], '')
        self.assertEqual(command['originalByteLength'], 16)
        with patch.object(guest_reply, 'read_batch', return_value={**self.batch, 'messages': [row]}):
            self.assertEqual(guest_reply.verify_image_source(command), 20)

    def test_other_account_never_reaches_executor(self):
        def forbidden(*_):
            self.fail('changed account reached UI')
        self.assertEqual(image_download.download_pending(self.batch, Path('/fixture'), 'b' * 64, {},
                                                        request=forbidden), (None, [], False))

    def test_one_request_per_batch_with_fairness_and_failed_task_backoff(self):
        second = {**self.row, 'msgId': 11, 'msgSvrId': '124'}
        batch = {**self.batch, 'messages': [self.row, second]}
        attempts, requested = {}, []
        def request(executor, command):
            requested.append(command['sourceMessageId'])
            return {'status': 'DOWNLOAD_REQUESTED'}
        self.assertEqual(image_download.download_pending(batch, Path('/fixture'), 'a' * 64, attempts,
                                                        request=request, now_ms=lambda: 1000), (11, [10], True))
        self.assertEqual(image_download.download_pending(batch, Path('/fixture'), 'a' * 64, attempts,
                                                        request=request, now_ms=lambda: 1000), (10, [11], True))
        self.assertEqual(requested, [11, 10])
        self.assertEqual(image_download.download_pending(batch, Path('/fixture'), 'a' * 64, attempts,
                                                        request=request, now_ms=lambda: 1000), (None, [], True))

    def test_source_identity_and_original_metadata_rechecked_before_download(self):
        with patch.object(guest_reply, 'read_batch', return_value=self.batch) as read:
            self.assertEqual(guest_reply.verify_image_source(self.command), 20)
            read.assert_called_once_with(10, True, 45)
        for key, value in [('accountFingerprint', 'b' * 64), ('sourceMessageId', 11),
                           ('sourceServerId', '999'), ('conversationId', 'other'),
                           ('originalMD5', '0' * 32), ('originalByteLength', 17)]:
            with self.subTest(key=key), patch.object(guest_reply, 'read_batch', return_value=self.batch):
                with self.assertRaises(RuntimeError):
                    guest_reply.verify_image_source({**self.command, key: value})
        with patch.object(guest_reply, 'read_batch', return_value=self.batch), patch.object(guest_reply, 'viewer_task', return_value=2):
            with self.assertRaisesRegex(RuntimeError, 'source_viewer_changed'):
                guest_reply.verify_image_source({**self.command, 'viewerTask': 1})

    def test_new_viewer_instance_and_cleanup_never_back_out_user_window(self):
        with patch.object(guest_reply, 'docker') as docker, patch.object(guest_reply, 'viewer_task', return_value=12):
            self.assertEqual(guest_reply.open_image_viewer(self.command), 12)
            self.assertIn('0x18000000', docker.call_args.args)
            self.assertIn(str(self.row['msgId']), docker.call_args.args)
            self.assertIn('img_gallery_enter_from_chatting_ui', docker.call_args.args)
            self.assertIn('img_gallery_msg_svr_id', docker.call_args.args)
        with patch.object(guest_reply, 'docker') as docker, patch.object(guest_reply, 'viewer_task', return_value=13):
            guest_reply.close_image_viewer(12)
            docker.assert_not_called()

    def test_download_waits_for_verified_original_before_viewer_can_close(self):
        ready = {**self.batch, 'messages': [{**self.row, 'media': [
            {'state': 'available', 'byteLength': 16, 'sha256': 'a' * 64}]}]}
        with patch.object(guest_reply, 'read_batch', side_effect=[self.batch, ready]) as read, \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000), \
                patch.object(guest_reply.time, 'monotonic', return_value=0), \
                patch.object(guest_reply.time, 'sleep'):
            self.assertTrue(guest_reply.wait_image_original(self.command))
            self.assertEqual(read.call_count, 2)
            self.assertFalse(read.call_args.args[1])

    def test_original_wait_rejects_account_change_even_if_bytes_are_ready(self):
        changed = {**self.batch, 'accountFingerprint': 'b' * 64, 'messages': [{**self.row, 'media': [
            {'state': 'available', 'byteLength': 16, 'sha256': 'a' * 64}]}]}
        with patch.object(guest_reply, 'read_batch', return_value=changed), \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000):
            with self.assertRaisesRegex(RuntimeError, 'source_account_changed'):
                guest_reply.wait_image_original(self.command)

    def test_guest_source_checks_reuse_private_reader_and_fail_closed(self):
        with patch.object(guest_reply.SOURCE, 'read', return_value=self.batch) as read:
            self.assertEqual(guest_reply.read_batch(10, True, 20), self.batch)
            self.assertEqual(guest_reply.read_batch(10, False, 5), self.batch)
        self.assertEqual(read.call_args_list[0].kwargs, {'timeout': 20, 'limit': 1})
        self.assertEqual(read.call_args_list[1].args, (0, [10], False, False))
        with patch.object(guest_reply.SOURCE, 'read', side_effect=guest_reply.ReaderError('fixture')):
            with self.assertRaisesRegex(RuntimeError, 'source_target_unavailable'):
                guest_reply.read_batch(10, True, 20)

    def test_cleanup_dismisses_dialog_then_exits_only_its_viewer(self):
        with patch.object(guest_reply, 'docker') as docker, \
                patch.object(guest_reply, 'viewer_task', side_effect=[12, 12, None]), \
                patch.object(guest_reply.time, 'sleep'):
            guest_reply.close_image_viewer(12)
            self.assertEqual(docker.call_count, 2)

    def test_hd_and_non_hd_keep_viewer_open_until_verified_wait_finishes(self):
        def docker(*args, **kwargs):
            if args[:4] == ('cmd', 'package', 'list', 'packages'):
                package = args[-1]
                return ('package:' + package + ' versionCode:' + ('3' if package == guest_reply.PACKAGE else '3180')).encode()
            if args[-2:] == ('cat', 'files/result.json'):
                return b'{"status":"IMAGE_DOWNLOAD_NOT_AVAILABLE","clicked":false}'
            return b''

        import tempfile
        for high in (True, False):
            stages = []
            with self.subTest(requiresHD=high), tempfile.TemporaryDirectory() as home, \
                    patch.object(guest_reply.Path, 'home', return_value=Path(home)), \
                    patch.object(guest_reply, 'docker', side_effect=docker), \
                    patch.object(guest_reply, 'verify_image_source') as verify, \
                    patch.object(guest_reply, 'open_image_viewer', return_value=12), \
                    patch.object(guest_reply, 'instrument') as instrument, \
                    patch.object(guest_reply, 'request_native_image', return_value=False) as native, \
                    patch.object(guest_reply, 'wait_hd_image', side_effect=lambda command: stages.append('hd_wait') or True) as hd_wait, \
                    patch.object(guest_reply, 'wait_image_original', side_effect=lambda command: stages.append('wait') or True) as wait, \
                    patch.object(guest_reply, 'close_image_viewer', side_effect=lambda task: stages.append('close')) as close, \
                    patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000):
                command = {**self.command, 'requiresHD': high, 'downloadPreview': False}
                response = guest_reply.perform(command)
                self.assertEqual(response['status'], 'IMAGE_AVAILABLE')
                self.assertEqual(wait.call_count, 0 if high else 1)
                verify.assert_called_once_with(command)
                instrument.assert_not_called()
                native.assert_not_called()
                self.assertEqual(hd_wait.call_count, 1 if high else 0)
                self.assertEqual(stages, ['hd_wait', 'close'] if high else ['wait', 'close'])
                close.assert_called_once_with(12)

    def test_hd_wait_retries_late_exact_button_then_only_waits_for_original(self):
        command = {**self.command, 'viewerTask': 12}
        with patch.object(guest_reply, 'image_original_ready', side_effect=[False, False, False, True]), \
                patch.object(guest_reply, 'request_native_image', side_effect=[False, True]) as request, \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000), \
                patch.object(guest_reply.time, 'monotonic', return_value=0), patch.object(guest_reply.time, 'sleep'):
            self.assertTrue(guest_reply.wait_hd_image(command))
        self.assertEqual(request.call_count, 2)
        request.assert_called_with(command, deadline=30)

    def test_hd_without_request_releases_viewer_before_full_download_budget(self):
        with patch.object(guest_reply, 'image_original_ready', return_value=False), \
                patch.object(guest_reply, 'request_native_image', return_value=False) as request, \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000), \
                patch.object(guest_reply.time, 'monotonic', side_effect=[0, 0, 0, 1, 31, 31]), \
                patch.object(guest_reply.time, 'sleep'):
            self.assertFalse(guest_reply.wait_hd_image(self.command))
        request.assert_called_once_with(self.command, deadline=30)

    def test_hd_wait_without_button_stops_at_shared_deadline(self):
        with patch.object(guest_reply, 'image_original_ready', return_value=False), \
                patch.object(guest_reply, 'request_native_image', return_value=False) as request, \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000), \
                patch.object(guest_reply.time, 'monotonic', side_effect=[0, 0, 0, 0, 1, 1, 1, 2]), \
                patch.object(guest_reply.time, 'sleep'):
            self.assertFalse(guest_reply.wait_hd_image({**self.command, 'viewerTask': 12}, timeout=2))
        self.assertEqual(request.call_count, 2)

    def test_hd_wait_expiry_or_changed_viewer_never_clicks(self):
        command = {**self.command, 'viewerTask': 12}
        with patch.object(guest_reply.time, 'time_ns', return_value=999_000_000_000), \
                patch.object(guest_reply, 'request_native_image') as request:
            self.assertFalse(guest_reply.wait_hd_image(command))
        request.assert_not_called()
        with patch.object(guest_reply, 'image_original_ready', return_value=False), \
                patch.object(guest_reply, 'native_image_snapshot', side_effect=RuntimeError('source_viewer_changed')), \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000), \
                patch.object(guest_reply, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'source_viewer_changed'):
                guest_reply.wait_hd_image(command)
        docker.assert_not_called()

    def test_native_hd_button_expiry_after_verification_never_taps(self):
        command = {**self.command, 'viewerTask': 12}
        target = ('button', (0, 0, 100, 100), ('root',))
        with patch.object(guest_reply, 'native_image_snapshot', return_value=target), \
                patch.object(guest_reply, 'verify_image_source'), \
                patch.object(guest_reply.time, 'time_ns', return_value=999_000_000_000), \
                patch.object(guest_reply.time, 'monotonic', return_value=0), \
                patch.object(guest_reply, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'source_image_window_changed'):
                guest_reply.request_native_image(command, deadline=150)
        docker.assert_not_called()

    def test_hd_requirement_is_bound_to_source_xml(self):
        with self.assertRaisesRegex(RuntimeError, 'source_image_changed'):
            guest_reply.validate_image_batch({**self.command, 'requiresHD': False}, self.batch)

    def test_native_record_list_requires_exact_visible_task_activity_and_bounds(self):
        fixture = '''TASK com.tencent.mm id=12 userId=0
  ACTIVITY com.tencent.mm/.feature.appmsg.ui.RecordDetailUI abcd pid=42
    View Hierarchy:
      DecorView{root V.E...... ........ 0,0-1200,1920}
        android.widget.FrameLayout{parent V.E...... ........ 20,30-1180,1900}
          com.tencent.mm.view.recyclerview.WxRecyclerView{list V.E...... ........ 30,40-1100,1800 #7f0a58af app:id/lqa}
            android.widget.FrameLayout{row V.E...... ........ 0,0-1000,200}
'''
        target = guest_reply.native_view_target(fixture, 12, 'abcd', record=True)
        self.assertEqual(target, ('list', (50, 70, 1120, 1830), ('root', 'parent'), ('row',)))
        for output, task, activity in [(fixture, 13, 'abcd'), (fixture, 12, 'ffff'),
                (fixture.replace('parent V', 'parent G'), 12, 'abcd'),
                (fixture.replace('list V.E', 'list V..'), 12, 'abcd'),
                (fixture.replace('app:id/lqa', 'app:id/lqa_other'), 12, 'abcd'),
                (fixture.replace('app:id/lqa', 'app:id/lqa/other'), 12, 'abcd'),
                (fixture.replace('#7f0a58af', '#7f0a58a0'), 12, 'abcd'),
                (fixture.replace('RecordDetailUI', 'RecordMsgImageUI'), 12, 'abcd'),
                (fixture.replace('WxRecyclerView{list', 'RecyclerView{list'), 12, 'abcd'),
                (fixture.replace('1100,1800', '1190,1800'), 12, 'abcd')]:
            with self.subTest(output=output, task=task, activity=activity):
                self.assertIsNone(guest_reply.native_view_target(output, task, activity, record=True))
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            guest_reply.native_view_target(fixture + fixture.splitlines()[-2] + '\n', 12, 'abcd', record=True)

    def test_record_sticker_scroll_rechecks_snapshot_and_source_before_swipe(self):
        batch = self.record_sticker_batch()
        command = {**image_download.image_commands(batch, batch['messages'][0], 1000)[0], 'viewerTask': 12}
        target = ('list', (20, 40, 1020, 1840), ('root',), ('row',))
        with patch.object(guest_reply, 'image_original_ready', side_effect=[False, True]), \
                patch.object(guest_reply, 'native_view_snapshot', return_value=target), \
                patch.object(guest_reply, 'verify_image_source') as verify, \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000), \
                patch.object(guest_reply.time, 'monotonic', return_value=0), patch.object(guest_reply.time, 'sleep'), \
                patch.object(guest_reply, 'docker') as docker:
            self.assertTrue(guest_reply.wait_record_sticker(command))
            verify.assert_called_once_with(command, timeout=20)
            self.assertEqual(docker.call_args.args, ('input', 'swipe', '520', '1480', '520', '400', '350'))
        for after, expires in [(None, False), (target, True)]:
            with self.subTest(expires=expires), \
                    patch.object(guest_reply, 'image_original_ready', return_value=False), \
                    patch.object(guest_reply, 'native_view_snapshot', side_effect=[target, after]), \
                    patch.object(guest_reply, 'verify_image_source'), \
                    patch.object(guest_reply.time, 'time_ns', side_effect=[1_000_000_000, 999_000_000_000 if expires else 1_000_000_000]), \
                    patch.object(guest_reply.time, 'monotonic', return_value=0), \
                    patch.object(guest_reply, 'docker') as docker:
                with self.assertRaisesRegex(RuntimeError, 'window_changed'):
                    guest_reply.wait_record_sticker(command)
                docker.assert_not_called()
        with patch.object(guest_reply, 'image_original_ready', return_value=False), \
                patch.object(guest_reply, 'native_view_snapshot', return_value=target), \
                patch.object(guest_reply, 'verify_image_source', side_effect=RuntimeError('source_account_changed')), \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000), \
                patch.object(guest_reply.time, 'monotonic', return_value=0), \
                patch.object(guest_reply, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'source_account_changed'):
                guest_reply.wait_record_sticker(command)
            docker.assert_not_called()

    def test_record_native_snapshot_retains_activity_instance_identity(self):
        target = ('list', (0, 0, 1000, 1800), ('root',), ('row',))
        result = guest_reply.subprocess.CompletedProcess([], 0, stdout='', stderr='')
        with patch.object(guest_reply, 'viewer_activity', side_effect=[(12, 'abcd'), (12, 'ffff')]), \
                patch.object(guest_reply.subprocess, 'run', return_value=result), \
                patch.object(guest_reply, 'native_view_target', return_value=target):
            before = guest_reply.native_view_snapshot(12, record=True)
            after = guest_reply.native_view_snapshot(12, record=True)
        self.assertNotEqual(before, after)

    def test_record_sticker_scroll_has_page_limit_and_stops_at_unchanged_rows(self):
        command = {**self.command, 'operation': 'download-record-sticker', 'viewerTask': 12}
        for changing, expected in [(True, 12), (False, 1)]:
            snapshots = [('list', (0, 0, 1000, 1800), ('root',), (str(index if changing else 0),))
                         for index in range(12) for _ in range(2)]
            with self.subTest(changing=changing), \
                    patch.object(guest_reply, 'image_original_ready', return_value=False), \
                    patch.object(guest_reply, 'native_view_snapshot', side_effect=snapshots), \
                    patch.object(guest_reply, 'verify_image_source'), \
                    patch.object(guest_reply, 'wait_image_original', return_value=False) as wait, \
                    patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000), \
                    patch.object(guest_reply.time, 'monotonic', return_value=0), patch.object(guest_reply.time, 'sleep'), \
                    patch.object(guest_reply, 'docker') as docker:
                self.assertFalse(guest_reply.wait_record_sticker(command))
                self.assertEqual(docker.call_count, expected)
                wait.assert_called_once_with(command, 150)

    def test_native_button_requires_exact_task_visible_ancestors_and_unique_resource(self):
        fixture = '''TASK com.tencent.mm id=12 userId=0
  ACTIVITY com.tencent.mm/.ui.chatting.gallery.ImageGalleryUI abcd pid=42
    View Hierarchy:
      DecorView{root V.E...... ........ 0,0-1200,1920}
        android.widget.FrameLayout{parent V.E...... ........ 20,30-1180,1900}
          android.widget.Button{button VFED..C.. ........ 30,40-130,100 #7f0a17b3 app:id/cnb}
'''
        self.assertEqual(guest_reply.native_image_button(fixture, 12)[1], (50, 70, 150, 130))
        self.assertEqual(guest_reply.native_image_button(fixture.split('\n', 1)[1], 12, 'abcd')[1], (50, 70, 150, 130))
        self.assertIsNone(guest_reply.native_image_button(fixture, 12, 'ffff'))
        for output, task in [(fixture, 13), (fixture.replace('parent V', 'parent G'), 12),
                             (fixture.replace('VFED..C..', 'VF.D..C..'), 12),
                             (fixture.replace('30,40-130,100', '30,40-30,40'), 12),
                             (fixture.replace('app:id/cnb', 'app:id/other'), 12)]:
            self.assertIsNone(guest_reply.native_image_button(output, task))
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            guest_reply.native_image_button(fixture + fixture.splitlines()[-1] + '\n', 12)

    def test_native_button_snapshot_change_never_taps(self):
        command = {**self.command, 'viewerTask': 12}
        before = ('button', (0, 0, 100, 100), ('root',))
        with patch.object(guest_reply, 'native_image_snapshot', side_effect=[before, None]), \
                patch.object(guest_reply, 'verify_image_source'), patch.object(guest_reply, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'window_changed'):
                guest_reply.request_native_image(command)
            docker.assert_not_called()

    def test_native_button_click_uses_bound_geometry_after_source_recheck(self):
        command = {**self.command, 'viewerTask': 12}
        button = ('button', (20, 40, 120, 80), ('root',))
        with patch.object(guest_reply, 'native_image_snapshot', return_value=button), \
                patch.object(guest_reply, 'verify_image_source') as verify, \
                patch.object(guest_reply.time, 'time_ns', return_value=1_000_000_000), \
                patch.object(guest_reply, 'docker') as docker:
            self.assertTrue(guest_reply.request_native_image(command))
            verify.assert_called_once_with(command, timeout=20)
            self.assertEqual(docker.call_args.args, ('input', 'tap', '70', '60'))

    def test_media_read_returns_pending_without_waiting_for_official_download(self):
        with patch.object(collector, 'read_source', return_value=self.batch) as read:
            with patch.object(image_download, 'download_pending') as download:
                batch, ready = collector.background_read(['fixture-reader'], [10], Path('/fixture'), False, 'a' * 64, {})
        read.assert_called_once_with(['fixture-reader'], 0, [10])
        download.assert_not_called()
        self.assertEqual(batch['messages'][0]['media'][0]['state'], 'pending')
        self.assertFalse(ready)

    def test_fresh_rows_always_precede_due_retry_even_with_legacy_preference(self):
        new = {**self.row, 'msgId': 11, 'msgSvrId': '124'}
        batch = {**self.batch, 'messages': [self.row, new]}
        attempts = {self.command['replyId']: (1, 0)}
        requested = []
        def request(executor, command):
            requested.append(command['sourceMessageId'])
            return {'status': 'DOWNLOAD_UNCONFIRMED'}
        image_download.download_pending(batch, Path('/fixture'), 'a' * 64, attempts,
                                        request=request, now_ms=lambda: 1000)
        newer = {**new, 'msgId': 12, 'msgSvrId': '125'}
        image_download.download_pending({**batch, 'messages': [self.row, newer]}, Path('/fixture'), 'a' * 64, attempts,
                                        request=request, now_ms=lambda: 1000, prefer_retry=True)
        self.assertEqual(requested, [11, 12])

    def test_retry_remains_available_when_no_fresh_rows_and_respects_due(self):
        attempts = {self.command['replyId']: (1, 2000)}
        requested = []
        def request(executor, command):
            requested.append(command['sourceMessageId'])
            return {'status': 'DOWNLOAD_UNCONFIRMED'}
        self.assertEqual(image_download.download_pending(self.batch, Path('/fixture'), 'a' * 64, attempts,
                                                        request=request, now_ms=lambda: 1000, prefer_retry=True),
                         (None, [], True))
        self.assertEqual(requested, [])
        self.assertEqual(image_download.download_pending(self.batch, Path('/fixture'), 'a' * 64, attempts,
                                                        request=request, now_ms=lambda: 2000, prefer_retry=True),
                         (10, [], True))
        self.assertEqual(requested, [10])
        self.assertEqual(attempts[self.command['replyId']], (2, 32000))

    def test_download_log_includes_timing_and_only_known_reason_codes(self):
        for reason in ('ui_executor_busy', 'message_id_secret', {'message': 'secret'}):
            with self.subTest(reason=reason), patch('builtins.print') as printed, \
                    patch.object(image_download.time, 'monotonic', side_effect=[1.0, 1.125]):
                image_download.download_pending(self.batch, Path('/fixture'), 'a' * 64, {},
                                                request=lambda *_: {'status': 'AUTOMATION_NOT_READY', 'reason': reason},
                                                now_ms=lambda: 1000)
                log = json.loads(printed.call_args.args[0])
                expected = {'imageDownload': 'AUTOMATION_NOT_READY', 'operation': 'download-image', 'durationMs': 125}
                if reason == 'ui_executor_busy':
                    expected['reason'] = reason
                self.assertEqual(log, expected)


if __name__ == '__main__':
    unittest.main()
