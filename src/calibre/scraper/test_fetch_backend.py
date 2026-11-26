#!/usr/bin/env python
# License: GPLv3 Copyright: 2024, Kovid Goyal <kovid at kovidgoyal.net>

import http.server
import json
import os
import unittest
from threading import Event, Thread

from calibre.constants import iswindows

is_ci = os.environ.get('CI', '').lower() == 'true'
skip = ''
is_sanitized = 'libasan' in os.environ.get('LD_PRELOAD', '')
if is_sanitized:
    skip = 'Skipping Scraper tests as ASAN is enabled'
elif 'SKIP_QT_BUILD_TEST' in os.environ:
    skip = 'Skipping Scraper tests as it causes crashes in macOS VM'


class Handler(http.server.BaseHTTPRequestHandler):

    def __init__(self, *a, **kw):
        # Use the standard BaseHTTPRequestHandler signature and
        # obtain the test object from the server instance to avoid
        # non-standard constructor behavior.
        super().__init__(*a, **kw)
        self.test_obj = getattr(self.server, 'test_obj', None)

    def do_POST(self):
        if getattr(self.test_obj, 'dont_send_response', False):
            return
        self.do_response()

    def do_GET(self):
        if getattr(self.test_obj, 'dont_send_response', False):
            return
        if self.path == '/favicon.ico':
            self.send_response(http.HTTPStatus.NOT_FOUND)
            self.end_headers()
            return
        if self.path == '/redirect':
            self.send_response(http.HTTPStatus.FOUND)
            self.send_header('Location', '/redirected')
            self.end_headers()
            return
        self.do_response()

    def do_response(self):
        h = {}
        for k, v in self.headers.items():
            h.setdefault(k, []).append(v)
        if self.test_obj is not None:
            self.test_obj.request_count += 1
            request_count = self.test_obj.request_count
        else:
            request_count = 0
        ans = {
            'path': self.path,
            'headers': h,
            'request_count': request_count,
            'method': self.command,
        }
        if 'Content-Length' in self.headers:
            try:
                length = int(self.headers['Content-Length'])
            except (ValueError, TypeError):
                length = 0
            if length:
                ans['data'] = self.rfile.read(length).decode()
        data = json.dumps(ans).encode()
        self.send_response(http.HTTPStatus.OK)
        self.send_header('Content-type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Set-Cookie', 'sc=1')
        self.end_headers()
        if getattr(self.test_obj, 'dont_send_body', False):
            # Intentionally do not write the body to simulate a client timeout;
            # do not call non-standard methods like flush_headers and avoid
            # raising on broken pipes to keep the server stable.
            return
        else:
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                # Client closed connection; ignore to avoid crashing server
                pass

    def log_request(self, code='-', size='-'):
        pass


@unittest.skipIf(skip, skip)
class TestFetchBackend(unittest.TestCase):

    ae = unittest.TestCase.assertEqual

    def setUp(self):
        self.server_started = Event()
        self.server_thread = Thread(target=self.run_server, daemon=True)
        self.server_thread.start()
        if not self.server_started.wait(15):
            raise Exception('Test server failed to start')
        self.request_count = 0
        self.dont_send_response = self.dont_send_body = False

    def tearDown(self):
        self.server.shutdown()
        self.server_thread.join(5)

    def test_recipe_browser_qt(self):
        # Import Browser lazily to avoid importing GUI/Qt bindings at module import time
        from .qt import Browser as Browser
        self.do_recipe_browser_test(Browser)

    @unittest.skipIf(iswindows and is_ci, 'WebEngine browser test hangs on windows CI')
    def test_recipe_browser_webengine(self):
        # Import WebEngineBrowser lazily to avoid importing GUI/Qt bindings at module import time
        from .qt import WebEngineBrowser as WebEngineBrowser
        self.do_recipe_browser_test(WebEngineBrowser)

    def do_recipe_browser_test(self, browser_class):
        from urllib.error import URLError
        from urllib.request import Request

        br = browser_class(user_agent='test-ua', headers=(('th', '1'),), start_worker=True)

        def u(path=''):
            return f'http://localhost:{self.port}{path}'

        def get(path='', headers=None, timeout=None, data=None):
            url = u(path)
            if headers:
                req = Request(url, headers=headers)
            else:
                req = url
            with br.open(req, data=data, timeout=timeout) as res:
                raw = res.read()
                ans = json.loads(raw)
                ans['final_url'] = res.geturl()
            return ans

        def test_with_timeout(no_response=True):
            self.dont_send_body = True
            if no_response:
                self.dont_send_response = True
            try:
                get(timeout=0.02)
            except URLError as e:
                self.assertTrue(e.worth_retry)
            else:
                raise AssertionError('Expected timeout not raised')
            self.dont_send_body = False
            self.dont_send_response = False

        def header(name, *expected):
            name = name.lower()
            ans = []
            for k, v in r['headers'].items():
                if k.lower() == name:
                    ans.extend(v)
            self.ae(expected, tuple(ans))

        def has_header(name):
            self.assertIn(name.lower(), [h.lower() for h in r['headers']])

        try:
            r = get()
            self.ae(r['method'], 'GET')
            self.ae(r['request_count'], 1)
            header('th', '1')
            header('User-Agent', 'test-ua')
            has_header('accept-encoding')
            r = get()
            self.ae(r['request_count'], 2)
            header('Cookie', 'sc=1')
            test_with_timeout(True)
            test_with_timeout(False)
            r = get('/redirect')
            self.ae(r['path'], '/redirected')
            header('th', '1')
            self.assertTrue(r['final_url'].endswith('/redirected'))
            header('User-Agent', 'test-ua')
            r = get(headers={'th': '2', 'tc': '1'})
            header('Th', '1, 2')
            header('Tc', '1')
            br.set_simple_cookie('cook', 'ie')
            br.set_user_agent('man in black')
            r = get()
            header('User-Agent', 'man in black')
            header('Cookie', 'sc=1; cook=ie')
            r = get(data=b'1234')
            self.ae(r['method'], 'POST')
            self.ae(r['data'], '1234')
            header('Content-Type', 'application/x-www-form-urlencoded')
        finally:
            br.shutdown()

    def run_server(self):
        from http.server import ThreadingHTTPServer

        # Use the Handler class directly and attach the test object to the server
        # so handlers can access it via self.server.test_obj. This keeps handler
        # construction standard and avoids factory-related edge cases.
        with ThreadingHTTPServer(('', 0), Handler) as httpd:
            httpd.test_obj = self
            self.server = httpd
            self.port = httpd.server_address[1]
            self.server_started.set()
            httpd.serve_forever()


def find_tests():
    return unittest.defaultTestLoader.loadTestsFromTestCase(TestFetchBackend)
