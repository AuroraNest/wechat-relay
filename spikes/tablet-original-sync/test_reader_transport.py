"""Synthetic private-pipe fixtures; no account credentials or real content."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import reader_transport as transport
import source_reader


class ReaderTransportTests(unittest.TestCase):
    def request(self, request_id, **changes):
        args = {'afterId': 0, 'limit': 20, 'ids': [], 'includeMedia': False, 'deferMedia': True, **changes}
        return json.dumps({'requestId': request_id, 'args': args}).encode() + b'\n'

    def test_resident_server_handles_fresh_requests_and_keeps_errors_fixed(self):
        output = io.StringIO()
        snapshots = iter(['a' * 64, 'b' * 64])
        def read(args):
            return {'messages': [], 'accountFingerprint': next(snapshots), 'sourceMaxId': args.after_id}
        data = self.request(1, afterId=10) + self.request(2, afterId=20) + self.request(3, ids=[0])
        source_reader.serve(read, io.BytesIO(data), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]['batch']['accountFingerprint'], 'a' * 64)
        self.assertEqual(responses[1]['batch']['accountFingerprint'], 'b' * 64)
        self.assertEqual(responses[1]['batch']['sourceMaxId'], 20)
        self.assertEqual(responses[2], {'requestId': 3, 'error': 'invalid_ids'})

    def test_rpc_never_accepts_watch_or_arbitrary_modes_and_closes_oversized_frame(self):
        for data in (self.request(1, watchSource=True), b'x' * 16385 + self.request(2)):
            with self.subTest(dataLength=len(data)):
                output = io.StringIO()
                with patch.object(source_reader, 'read_once') as read:
                    source_reader.serve(read, io.BytesIO(data), output)
                read.assert_not_called()
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                self.assertEqual(json.loads(output.getvalue())['error'], 'invalid_reader_request')

    def fixture(self, directory, response):
        path = Path(directory) / 'fixture_reader.py'
        path.write_text('import json,sys,time\nfor raw in sys.stdin.buffer:\n value=json.loads(raw)\n ' + response + '\n')
        return transport.ReaderSession([sys.executable, '-B', str(path)])

    def test_pipe_reuses_process_and_reconnects_after_exit(self):
        response = "print(json.dumps({'requestId':value['requestId'],'batch':{'messages':[],'marker':value['args']['afterId']}}),flush=True)"
        with tempfile.TemporaryDirectory() as directory:
            session = self.fixture(directory, response)
            try:
                first = session.read(10, [], False, True)
                pid = session.process.pid
                second = session.read(20, [], False, True)
                self.assertEqual(session.process.pid, pid)
                self.assertEqual([first['marker'], second['marker']], [10, 20])
                session.process.kill()
                session.process.wait(timeout=2)
                self.assertEqual(session.read(30, [], False, True)['marker'], 30)
                self.assertNotEqual(session.process.pid, pid)
            finally:
                session.close()

    def test_pipe_rejects_unbound_response_and_timeout_then_releases_process(self):
        for response, timeout in [("print(json.dumps({'requestId':999,'batch':{}}),flush=True)", 1),
                                  ('time.sleep(10)', 0.03)]:
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as directory:
                session = self.fixture(directory, response)
                with self.assertRaisesRegex(transport.ReaderError, 'source_reader_unavailable'):
                    session.read(0, [], False, True, timeout=timeout)
                self.assertIsNone(session.process)

    def test_guest_adapter_pipe_is_quiet_and_preserves_single_target_bound(self):
        response = "print(json.dumps({'requestId':value['requestId'],'batch':{'limit':value['args']['limit']}}),flush=True)"
        with tempfile.TemporaryDirectory() as directory:
            session = self.fixture(directory, response)
            session.diagnostics = False
            try:
                output = io.StringIO()
                with patch('sys.stdout', output):
                    self.assertEqual(session.read(0, [10], False, True, limit=1), {'limit': 1})
                self.assertEqual(output.getvalue(), '')
            finally:
                session.close()


if __name__ == '__main__':
    unittest.main()
