"""Guard test: the program must never read from the sim site (ehorseracing.com). Everything comes from the database.
Run from the program folder:  python -m unittest tests.test_no_site_access
Three checks: (1) no code or page mentions the site as something to call, (2) the removed routes are gone,
(3) while the server answers requests, no network connection is opened to anything except this computer."""
import ast, glob, json, os, re, socket, sys, tempfile, threading, unittest, urllib.error, urllib.request
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from http.server import ThreadingHTTPServer
from sdmmodern import server as SV
from sdmmodern.store import Store

SITE = re.compile(r'ehorseracing\.com|raceviewer\.com|\.asp\b', re.I)
REMOVED = ['/api/claim-finder', '/api/sires/search', '/api/sires/check', '/api/sires/watch', '/api/sires/history',
           '/api/stable-masters', '/api/my-stables', '/api/stable-group']


def code_strings(path):
    """Every string constant in a .py file except docstrings."""
    tree = ast.parse(open(path, encoding='utf-8').read())
    doc = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and n.body and isinstance(n.body[0], ast.Expr) \
                and isinstance(getattr(n.body[0], 'value', None), ast.Constant) and isinstance(n.body[0].value.value, str):
            doc.add(id(n.body[0].value))
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc]


class Static(unittest.TestCase):
    def test_no_python_code_names_the_site(self):
        files = glob.glob(os.path.join(ROOT, 'sdmmodern', '*.py')) + glob.glob(os.path.join(ROOT, 'hosted', '*.py')) + [os.path.join(ROOT, 'run_sdm.py')]
        bad = [(os.path.basename(f), s[:80]) for f in files for s in code_strings(f) if SITE.search(s)]
        self.assertEqual(bad, [])

    def test_page_only_fetches_its_own_server(self):
        for page in (os.path.join(ROOT, 'sdmmodern', 'static', 'index.html'), os.path.join(ROOT, 'hosted', 'm.html')):
            s = open(page, encoding='utf-8').read()
            for m in re.finditer(r'fetch\(\s*[\'"`]([^\'"`]*)', s):
                self.assertTrue(m.group(1).startswith('/'), (os.path.basename(page), m.group(1)))
            self.assertNotIn('XMLHttpRequest', s)
            for r in REMOVED:
                self.assertNotIn(r, s, (os.path.basename(page), r))

    def test_removed_modules_are_empty_stubs(self):
        for n in ('claims.py', 'sirefinder.py'):
            self.assertLess(os.path.getsize(os.path.join(ROOT, 'sdmmodern', n)), 300, n)


class Runtime(unittest.TestCase):
    def setUp(self):
        self.out = []
        self._open, self._conn = urllib.request.OpenerDirector.open, socket.create_connection
        me = self

        def open_(self_, *a, **k):
            me.out.append(('urllib', str(a[0] if a else k)[:100]))
            raise urllib.error.URLError('blocked in test')

        def conn(addr, *a, **k):
            if addr[0] not in ('127.0.0.1', 'localhost'):
                me.out.append(('socket', str(addr)))
                raise OSError('blocked in test')
            return me._conn(addr, *a, **k)
        urllib.request.OpenerDirector.open = open_
        socket.create_connection = conn
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, 'sdm.db'))
        self.app = SV.App(self.store)
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), SV.make_handler(self.app))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        urllib.request.OpenerDirector.open, socket.create_connection = self._open, self._conn

    def hit(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers={'Content-Type': 'application/json', 'X-SDM': '1'}, method='POST' if data is not None else 'GET')
        # talk to our own server with a plain socket-level opener (the patched OpenerDirector.open is for outside calls)
        import http.client
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=20)
        c.request(req.get_method(), path, body=data, headers={'Content-Type': 'application/json', 'X-SDM': '1'})
        r = c.getresponse(); b = r.read(); c.close()
        return r.status, b

    def test_removed_routes_are_gone(self):
        for p in REMOVED:
            self.assertEqual(self.hit(p)[0], 404, p)
            self.assertEqual(self.hit(p, {})[0], 404, p)

    def test_normal_use_makes_no_outside_connection(self):
        self.assertEqual(self.hit('/')[0], 200)
        for p in ('/api/info', '/api/tracks', '/api/sire?hid=2100059930'):
            self.hit(p)
        for p, b in (('/api/search', dict(kind='horse', name='x*', hid='', first_week='', last_week='')), ('/api/best-speeds', {}), ('/api/owner-rankings', {})):
            self.hit(p, b)
        self.assertEqual(self.out, [])


if __name__ == '__main__':
    unittest.main()
