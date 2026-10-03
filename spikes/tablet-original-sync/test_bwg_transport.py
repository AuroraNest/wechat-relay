import contextlib
import copy
import io
import os
import socket
import ssl
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import collector as c


class BWGTransportTests(unittest.TestCase):
    def test_destination_guard_rejects_other_origins_before_open(self):
        origins = ['http://relay.auroramaple.com', 'https://other.example',
                   'https://relay.auroramaple.com:444', 'https://user@relay.auroramaple.com',
                   'https://relay.auroramaple.com.evil.example', 'https://relay.auroramaple.com:bad']
        with patch.dict(os.environ, {'AWR_RELAY_HTTPS_SOCKET': '/fixture/relay.sock'}), \
                patch.object(c.urllib.request, 'build_opener') as opener:
            for origin in origins:
                with self.subTest(origin=origin), self.assertRaisesRegex(c.CollectorError, 'https_socket_destination_rejected'):
                    c.request({'origin': origin}, '/fixture', b'{}', signed=False)
            opener.assert_not_called()
        c.require_relay_destination('https://relay.auroramaple.com:443/fixture')

    def test_unix_connection_preserves_verified_tls_hostname_and_timeout(self):
        connection = c.UnixHTTPSConnection('relay.auroramaple.com:443', '/fixture/relay.sock', timeout=123)
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        raw, secured = MagicMock(), MagicMock()
        with patch.object(c.socket, 'socket', return_value=raw) as factory, \
                patch.object(connection._context, 'wrap_socket', return_value=secured) as wrap:
            connection.connect()
            factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
            raw.settimeout.assert_called_once_with(123)
            raw.connect.assert_called_once_with('/fixture/relay.sock')
            wrap.assert_called_once_with(raw, server_hostname='relay.auroramaple.com')
            self.assertIs(connection.sock, secured)
            connection.close()
            secured.close.assert_called_once()

    def test_socket_closed_on_connect_and_tls_failures(self):
        for phase in ('connect', 'tls'):
            with self.subTest(phase=phase):
                connection = c.UnixHTTPSConnection('relay.auroramaple.com', '/fixture/relay.sock', timeout=45)
                raw = MagicMock()
                if phase == 'connect':
                    raw.connect.side_effect = OSError('fixture')
                with patch.object(c.socket, 'socket', return_value=raw), \
                        patch.object(connection._context, 'wrap_socket', side_effect=ssl.SSLError('fixture')):
                    with self.assertRaises(OSError):
                        connection.connect()
                raw.close.assert_called_once()
                self.assertIsNone(connection.sock)

    def test_configured_request_disables_proxies_and_preserves_url_config_timeout(self):
        config = {'origin': 'https://relay.auroramaple.com'}
        original = copy.deepcopy(config)
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
        with patch.dict(os.environ, {'AWR_RELAY_HTTPS_SOCKET': '/fixture/relay.sock',
                                    'https_proxy': 'http://untrusted.example:8080'}), \
                patch.object(c.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(c.request(config, '/fixture?x=1', b'{}', signed=False), {'ok': True})
        proxy, handler, redirect = build.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(handler, c.UnixHTTPSHandler)
        self.assertEqual(handler.socket_path, '/fixture/relay.sock')
        self.assertIs(redirect, c.NoRedirect)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, config['origin'] + '/fixture?x=1')
        self.assertEqual(opener.open.call_args.kwargs, {'timeout': 45})
        self.assertEqual(config, original)

    def test_absent_socket_preserves_default_opener(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{}'
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(c.urllib.request, 'build_opener', return_value=opener) as build:
            c.request({'origin': 'https://other.example'}, '/fixture', b'', signed=False)
        build.assert_called_once_with(c.NoRedirect)

    def test_real_opener_routes_via_unix_with_proxy_environment(self):
        raw, secured = MagicMock(), MagicMock()
        secured.makefile.return_value = io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
        with patch.dict(os.environ, {'AWR_RELAY_HTTPS_SOCKET': '/fixture/relay.sock',
                                    'https_proxy': 'http://untrusted.example:8080', 'no_proxy': ''}), \
                patch.object(c.socket, 'socket', return_value=raw), \
                patch.object(ssl.SSLContext, 'wrap_socket', return_value=secured) as wrap:
            self.assertEqual(c.request({'origin': 'https://relay.auroramaple.com'}, '/fixture', b'{}', signed=False), {})
        raw.connect.assert_called_once_with('/fixture/relay.sock')
        raw.settimeout.assert_called_once_with(45)
        wrap.assert_called_once_with(raw, server_hostname='relay.auroramaple.com')
        request_headers = secured.sendall.call_args_list[0].args[0]
        self.assertIn(b'Host: relay.auroramaple.com\r\n', request_headers)
        secured.close.assert_called_once()

    def test_configured_failure_remains_collector_error_without_fallback(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError(OSError('fixture'))
        with patch.dict(os.environ, {'AWR_RELAY_HTTPS_SOCKET': ''}), \
                patch.object(c.urllib.request, 'build_opener', return_value=opener) as build, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(c.CollectorError, 'network_transport_error'):
                c.request({'origin': 'https://relay.auroramaple.com'}, '/fixture', b'', signed=False)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(opener.open.call_count, 1)
        self.assertIsInstance(build.call_args.args[1], c.UnixHTTPSHandler)

    def test_handler_uses_unix_connection_and_rejects_redirects(self):
        handler = c.UnixHTTPSHandler('/fixture/relay.sock')
        req = urllib.request.Request('https://relay.auroramaple.com/fixture')
        with patch.object(handler, 'do_open', return_value='fixture') as do_open:
            self.assertEqual(handler.https_open(req), 'fixture')
            connection = do_open.call_args.args[0]('relay.auroramaple.com', timeout=99)
        self.assertIsInstance(connection, c.UnixHTTPSConnection)
        self.assertEqual(connection.timeout, 99)
        self.assertEqual(connection.socket_path, '/fixture/relay.sock')
        with self.assertRaisesRegex(c.CollectorError, 'redirect_rejected'):
            c.NoRedirect().redirect_request(req, None, 302, '', {}, 'https://other.example')


if __name__ == '__main__':
    unittest.main()
