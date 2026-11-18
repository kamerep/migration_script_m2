"""Tests of the local web UI (m2_ui.py, m2_ui.html): the page, its assets, the Docs tab and the API around them.

    .venv/bin/python -m unittest discover -s tests -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import m2_ui  # noqa: E402

PAGE = (HERE / 'm2_ui.html').read_text(encoding='utf-8')
SCRIPT = re.search(r'<script>(.*)</script>', PAGE, re.S).group(1)
NAV = re.findall(r"\['([a-z]+)', '[^']+', '([a-z]+)'\]", re.search(r'const NAV = (.*?)const TABS', SCRIPT, re.S).group(1))


class TestPage(unittest.TestCase):
    """The page stays one offline file whose parts agree with each other."""

    def test_no_external_resources(self):
        # CSP is default-src 'self': a font, script or image from another host would silently not load
        self.assertEqual(re.findall(r'(?:src|href)\s*=\s*["\']https?://', PAGE), [])
        self.assertNotIn('@import', PAGE)
        self.assertEqual(PAGE.count('<script'), 1)

    def test_token_placeholder(self):
        self.assertEqual(PAGE.count("'__TOKEN__'"), 1)

    def test_every_icon_exists(self):
        symbols = set(re.findall(r'<symbol id="i-([a-z]+)"', PAGE))
        used = set(re.findall(r"ic\('([a-z]+)'", SCRIPT)) | set(re.findall(r'href="#i-([a-z]+)"', PAGE))
        used |= {icon for _tab, icon in NAV}
        used |= set(re.findall(r"icon: '([a-z]+)'", SCRIPT))                              # command palette
        self.assertEqual(sorted(used - symbols), [])

    def test_sections_have_panel_and_renderer(self):
        nav = [tab for tab, _icon in NAV]
        panels = re.findall(r'<section class="panel" data-tab="([a-z]+)">', PAGE)
        render = re.search(r'const RENDER = \{(.*?)\};', SCRIPT, re.S).group(1)
        self.assertEqual(sorted(nav), sorted(panels))
        for tab in nav:
            self.assertRegex(render, rf'\b{tab}:')
        self.assertIn('docs', nav)

    def test_script_parses(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as fh:
            fh.write(SCRIPT)
        result = subprocess.run([node, '--check', fh.name], capture_output=True, text=True, check=False)
        Path(fh.name).unlink()
        self.assertEqual(result.returncode, 0, result.stderr)


class TestServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.config = Path(cls.tmp.name) / 'm2_config.json'
        shutil.copy(HERE / 'm2_config.json', cls.config)
        cls.saved = (m2_ui.CONFIG, m2_ui.RUNS, m2_ui.SERVER_LOG)
        m2_ui.CONFIG, m2_ui.RUNS = cls.config, Path(cls.tmp.name) / 'runs'
        m2_ui.SERVER_LOG = Path(cls.tmp.name) / 'logs' / 'm2_ui.log'
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), m2_ui.Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        m2_ui.CONFIG, m2_ui.RUNS, m2_ui.SERVER_LOG = cls.saved
        cls.tmp.cleanup()

    def get(self, path, token=True, body=None):
        headers = {'X-Token': m2_ui.TOKEN} if token else {}
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url + path, data, headers), timeout=10) as r:
                return r.status, r.headers.get('Content-Type', ''), r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get('Content-Type', ''), exc.read()

    def test_page_carries_the_token_and_csp(self):
        status, ctype, body = self.get('/', token=False)
        self.assertEqual(status, 200)
        self.assertIn('text/html', ctype)
        self.assertIn(m2_ui.TOKEN.encode(), body)
        self.assertNotIn(b'__TOKEN__', body)
        with urllib.request.urlopen(self.url + '/', timeout=10) as r:      # strict CSP: the script by its hash
            csp, ref = r.headers['Content-Security-Policy'], r.headers['Referrer-Policy']
        script = csp.split('script-src ')[1].split(';')[0]
        self.assertNotIn('unsafe-inline', script)
        self.assertEqual(script, m2_ui.page_csp(body.decode()).split('script-src ')[1].split(';')[0])
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(ref, 'no-referrer')

    def test_health_is_public_and_api_is_not(self):
        self.assertEqual(self.get('/health', token=False)[0], 200)
        for path in ('/api/state', '/api/docs', '/api/routes', '/api/runs'):
            self.assertEqual(self.get(path, token=False)[0], 403, path)
            self.assertEqual(self.get(path)[0], 200, path)

    def test_assets(self):
        for name, ctype in (('icon.svg', 'image/svg+xml'), ('favicon.svg', 'image/svg+xml'), ('logo.svg', 'image/svg+xml'),
                            ('icon-192.png', 'image/png'), ('site.webmanifest', 'application/manifest+json')):
            status, got, body = self.get('/assets/' + name, token=False)
            self.assertEqual((status, got), (200, ctype), name)
            self.assertTrue(body)
        self.assertEqual(self.get('/favicon.ico', token=False)[:2], (200, 'image/x-icon'))

    def test_assets_serve_nothing_else(self):
        for path in ('/assets/../m2_config.json', '/assets/..%2Fm2_config.json', '/assets/nope.svg',
                     '/assets/../m2_ui.py', '/assets/'):
            self.assertIn(self.get(path, token=False)[0], (403, 404), path)

    def test_docs(self):
        docs = json.loads(self.get('/api/docs')[2])['docs']
        ids = [d['id'] for d in docs]
        self.assertEqual(ids[0], 'M2_Migration_Running_and_UI')
        for ident in ('ref-api', 'ref-mcp', 'ref-cli'):
            self.assertIn(ident, ids)
        for doc in docs:
            self.assertTrue(doc['title'] and doc['html'], doc['id'])
            self.assertNotRegex(doc['html'], r'(?i)<script|<style|<h1')
        api = next(d for d in docs if d['id'] == 'ref-api')['html']
        for _method, path in m2_ui.ROUTES:
            self.assertIn(path, api)
        self.assertIn('m2_open_decisions', next(d for d in docs if d['id'] == 'ref-mcp')['html'])
        self.assertIn('m2ctl.sh deploy', next(d for d in docs if d['id'] == 'ref-cli')['html'])

    def test_guide_pdf(self):
        self.assertEqual(self.get('/api/doc-file?name=M2_Migration_Running_and_UI.pdf')[:2], (200, 'application/pdf'))
        self.assertEqual(self.get('/api/doc-file?name=../m2_config.json')[0], 404)
        self.assertEqual(self.get('/api/doc-file?name=M2_Migration_Running_and_UI.pdf', token=False)[0], 403)

    def test_inventory_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp, 'M2 Archive inventory.csv')
            csv_path.write_text('"Path","Length","Modified"\r\n"NOVA-PH\\KP-00001-ENT\\a.pdf","1","2024-01-01 10:00"\r\n'
                                '"NOVA-PH 2\\NOVA-PH\\KP-00002-CON\\b.pdf","1","2024-01-01 10:00"\r\n"NOVA-PH 2\\NOVA-PH\\KP-00002',
                                encoding='utf-8-sig')                  # the last line is still being written
            Path(tmp, 'archive_holders.txt').write_text('# holders\nNOVA-PH\nNOVA-PH2\nNOVA-DUBAI\n', encoding='utf-8')
            r = m2_ui.api_inventory({'folder': tmp})
            self.assertEqual((r['state'], r['done'], r['total'], r['files'], r['listing']), ('writing', 1, 3, 2, 'NOVA-PH2'))
            self.assertEqual([h['state'] for h in r['holders']], ['done', 'listing', 'waiting'])
            with csv_path.open('a', encoding='utf-8') as fh:            # the line completes, the next holder starts
                fh.write('\\c.pdf","1","2024-01-01 10:00"\r\n"NOVA-DUBAI\\KP-00003-ENT\\d.pdf","1","2024-01-01 10:00"\r\n')
            r = m2_ui.api_inventory({'folder': tmp})
            self.assertEqual((r['done'], r['files'], r['listing']), (2, 4, 'NOVA-DUBAI'))
            self.assertEqual(r['holders'][1]['folders'], 1)
            old = csv_path.stat().st_mtime - 3600                        # no write for an hour: complete
            os.utime(csv_path, (old, old)); m2_ui.INVENTORY_CACHE.clear()
            r = m2_ui.api_inventory({'folder': tmp})
            self.assertEqual((r['state'], r['done']), ('complete', 3))

    def test_routes_are_described(self):
        routes = json.loads(self.get('/api/routes')[2])['routes']
        self.assertEqual(len(routes), len(m2_ui.ROUTES))
        self.assertEqual([r['path'] for r in routes if not r['doc']], [])

    def test_decide_and_undo(self):
        def config():
            return json.loads(self.config.read_text(encoding='utf-8'))
        self.get('/api/decide', body={'type': 'company', 'key': 'ZZ', 'value': 'Test Company Ltd'})
        self.get('/api/decide', body={'type': 'alias', 'key': 'Phone 2', 'kind': 'addresses', 'field': 'mobile'})
        self.assertEqual(config()['company_map']['ZZ'], 'Test Company Ltd')
        self.assertEqual(config()['extra_aliases']['addresses']['mobile'], ['Phone 2'])
        self.assertEqual(self.get('/api/decide', body={'type': 'unset', 'target': 'company', 'key': 'ZZ'})[0], 200)
        self.get('/api/decide', body={'type': 'unset', 'target': 'alias', 'key': 'Phone 2', 'kind': 'addresses',
                                      'field': 'mobile'})
        self.assertNotIn('ZZ', config()['company_map'])
        self.assertNotIn('mobile', config()['extra_aliases'].get('addresses', {}))
        self.assertEqual(self.get('/api/decide', body={'type': 'unset', 'target': 'nothing', 'key': 'ZZ'})[0], 400)

    def test_errors_are_json(self):
        status, ctype, body = self.get('/api/nope')
        self.assertEqual(status, 404)
        self.assertIn('application/json', ctype)
        self.assertIn('error', json.loads(body))


if __name__ == '__main__':
    unittest.main()
