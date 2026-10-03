"""Bounded private stdio requests to a resident guest reader, one session per lane."""
import atexit
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import threading
import time

MAX_RESPONSE = 64 * 1024 * 1024


class ReaderError(Exception):
    pass


class ReaderSession:
    def __init__(self, command, diagnostics=True):
        self.command = command
        self.diagnostics = diagnostics
        self.process = None
        self.request_id = 0

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        process.stdout.close()

    def read(self, after, ids, include_media, defer_media, timeout=150, limit=20):
        started = time.monotonic()
        try:
            if self.process is None or self.process.poll() is not None:
                self.close()
                self.process = subprocess.Popen(self.command + ['--serve'], stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
            self.request_id += 1
            value = {'requestId': self.request_id, 'args': {'afterId': after, 'limit': limit, 'ids': ids or [],
                                                         'includeMedia': include_media, 'deferMedia': defer_media}}
            raw = json.dumps(value, separators=(',', ':')).encode() + b'\n'
            if len(raw) > 16384:
                raise ReaderError('invalid_reader_request')
            self.process.stdin.write(raw)
            self.process.stdin.flush()
            data = bytearray()
            deadline = started + timeout
            while not data.endswith(b'\n'):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                    raise ReaderError('source_reader_timeout')
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk or len(data) + len(chunk) > MAX_RESPONSE:
                    raise ReaderError('source_reader_unavailable')
                data.extend(chunk)
            response = json.loads(data)
            if (not isinstance(response, dict) or type(response.get('requestId')) is not int or response.get('requestId') != self.request_id
                    or set(response) != {'requestId', 'batch'} or not isinstance(response['batch'], dict)):
                raise ReaderError('source_reader_unavailable')
            batch = response['batch']
            if self.diagnostics:
                print(json.dumps({'event': 'source_read', 'mode': 'text' if defer_media else 'media',
                                  'durationMs': round((time.monotonic() - started) * 1000)}), flush=True)
            return batch
        except (OSError, ValueError, ReaderError):
            self.close()
            raise ReaderError('source_reader_unavailable') from None


_sessions = {}
_sessions_lock = threading.Lock()


def read(command, after, ids=None, include_media=True, defer_media=False):
    key = (tuple(command), threading.get_ident())
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None:
            session = _sessions[key] = ReaderSession(command)
    return session.read(after, ids, include_media, defer_media)


def supported(command):
    return bool(command) and Path(command[-1]).name == 'source_reader.py'


@atexit.register
def close_sessions():
    with _sessions_lock:
        for session in _sessions.values():
            session.close()
        _sessions.clear()
