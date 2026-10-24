"""Top 25 Speeds of the Week: the lists load from local files, imports are checked, and nothing reaches the Bugler.
Run from the program folder:  python -m unittest tests.test_top25"""
import http.client, json, os, re, socket, sys, tempfile, threading, unittest, urllib.error, urllib.request
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from http.server import ThreadingHTTPServer
from sdmmodern import server as SV
from sdmmodern import top25 as T25
from sdmmodern.store import Store


def export_week(week, posted, pid, n=25):
    rows = ['st%d|HORSE %d|%.4f|Alw' % (i, i, 120 - i * .1) for i in range(n)]
    return dict(week=week, id=pid, posted=posted, title='Top 25 Speeds', overall=rows, overallYear=rows[:5],
                two=rows, twoYear=rows[:5], three=rows, threeYear=rows[:5])


class Data(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = type('S', (), {'path': os.path.join(self.tmp, 'sdm.db')})()

    def test_bundled_2026_lists_load(self):
        w = T25.load(self.store)['weeks']
        self.assertGreaterEqual(len(w), 42)
        self.assertEqual(min(w), '2026-01-04')
        self.assertEqual(len(w['2026-10-18']['overall']), 25)
        self.assertEqual(w['2026-10-18']['overall'][0].split('|')[1], 'CRYSTAL RAIN')

    def test_title_date_mistakes_are_fixed(self):
        f = {'weeks': [export_week('2025-08-02', '2026-08-05', '1'), export_week('2026-04-16', '2026-04-29', '2')]}
        w = T25.parse_import(json.dumps(f).encode())
        self.assertEqual(sorted(w), ['2026-04-26', '2026-08-02'])
        self.assertIn('title reads 08/02/2025', w['2026-08-02']['posted'])

    def test_bad_lines_are_dropped_not_guessed(self):
        f = {'2026-11-01': dict(post='9<x', overall=['a|b|120.5|Stk', 'junk', 'a|b|fast|Stk', 'a|b|c|d|e'])}
        w = T25.parse_import(json.dumps(f).encode())['2026-11-01']
        self.assertEqual(w['overall'], ['a|b|120.5|Stk'])
        self.assertEqual(w['post'], '')

    def test_rejects_non_json_empty_and_huge(self):
        for data in (b'hello', b'{"weeks": []}', b'x' * (T25.MAX_IMPORT + 1)):
            with self.assertRaises(ValueError):
                T25.parse_import(data)

    def test_added_weeks_go_next_to_the_database(self):
        r = T25.add(self.store, json.dumps({'weeks': [export_week('2026-10-25', '2026-10-28', '5')]}).encode())
        self.assertEqual((r['new'], r['replaced']), (1, 0))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, 'top25-speeds.json')))
        self.assertIn('2026-10-25', T25.load(self.store)['weeks'])


class Server(unittest.TestCase):
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
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), SV.make_handler(SV.App(self.store)))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.store.db.close()
        urllib.request.OpenerDirector.open, socket.create_connection = self._open, self._conn

    def hit(self, method, path, body=None, sdm=True):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=20)
        c.request(method, path, body=body, headers={'X-SDM': '1'} if sdm else {})
        r = c.getresponse(); b = r.read(); c.close()
        return r.status, b

    def test_page_and_lists(self):
        self.assertEqual(self.hit('GET', '/top25')[0], 200)
        st, b = self.hit('GET', '/api/top25')
        self.assertEqual(st, 200)
        self.assertGreaterEqual(len(json.loads(b)['weeks']), 42)
        self.assertEqual(self.out, [])

    def test_import_needs_the_app_header(self):
        body = json.dumps({'weeks': [export_week('2026-10-25', '2026-10-28', '5')]}).encode()
        self.assertEqual(self.hit('POST', '/api/top25/import', body, sdm=False)[0], 403)
        self.assertEqual(self.hit('POST', '/api/top25/import', b'not json')[0], 400)
        self.assertEqual(self.hit('POST', '/api/top25/import', body)[0], 200)
        self.assertEqual(self.out, [])

    def test_page_only_fetches_its_own_server(self):
        s = open(os.path.join(ROOT, 'sdmmodern', 'static', 'top25.html'), encoding='utf-8').read()
        for m in re.finditer(r'fetch\(\s*[\'"`]([^\'"`]*)', s):
            self.assertTrue(m.group(1).startswith('/'), m.group(1))
        self.assertNotIn('XMLHttpRequest', s)


if __name__ == '__main__':
    unittest.main()
