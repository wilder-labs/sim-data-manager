"""Tests for the residency portal (sdmmodern/residency_portal.py). Run: python -m unittest tests.test_residency_portal"""
import base64, csv, datetime, io, json, os, shutil, sqlite3, tempfile, threading, unittest
from sdmmodern import residency_portal as P, residency as R
try:
    from sdmmodern.store import SCHEMA
except ImportError:      # store needs the full program; this copy of the tables is enough for the tests
    SCHEMA = '''
    CREATE TABLE races(racenum REAL PRIMARY KEY, week INTEGER, surf TEXT, restr TEXT, cond TEXT, dist REAL, size INTEGER, purse INTEGER, cprice INTEGER, age TEXT, loc_etc TEXT, raceid INTEGER);
    CREATE TABLE horses(hid INTEGER PRIMARY KEY, hname TEXT, sex TEXT);
    CREATE TABLE owners(id INTEGER PRIMARY KEY, oname TEXT, o15 TEXT, o12 TEXT, o10 TEXT, UNIQUE(oname,o15,o12,o10));
    CREATE TABLE entries(id INTEGER PRIMARY KEY, racenum REAL, week INTEGER, hid INTEGER, owner_id INTEGER, tag TEXT, fin INTEGER, ppos INTEGER, sp REAL, odds REAL, bps INTEGER, earn REAL);
    CREATE TABLE weeks(week INTEGER PRIMARY KEY, races INTEGER, entries INTEGER);
    CREATE TABLE settings(k TEXT PRIMARY KEY, v TEXT);
    '''

ANCHOR_WEEK, ANCHOR_DATE = 10295, datetime.date(2026, 9, 27)


class FakeStore:
    def __init__(self, folder):
        self.path = os.path.join(folder, 'sdm.db')
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.executescript(SCHEMA)
    def anchor(self):
        return ANCHOR_WEEK, ANCHOR_DATE


class App:
    def __init__(self, store):
        self.store, self.lock = store, threading.RLock()


def wk(d):
    return ANCHOR_WEEK + (datetime.date.fromisoformat(d) - ANCHOR_DATE).days


def csv_bytes(rows):
    b = io.StringIO(); csv.writer(b).writerows(rows); return b.getvalue().encode()


ROSTER_ROWS = [['Member', 'Master stable', 'Stable', 'Joined', 'Left'],
               ['Ann', 'Alpha', 'Alpha', '', ''], ['Ann', 'Alpha', 'Alpha Two', '', ''],
               ['Bo', 'Gamma', 'Gamma', '', ''], ['Bo', 'Gamma', 'Late Joiner', '2026-10-05', ''],
               ['Cy', 'Ghost', 'Ghost Stable', '', '']]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = FakeStore(self.tmp)
        self.app = App(self.st)
        db = self.st.db
        for i, n in enumerate(['Alpha', 'Alpha Two', 'Gamma', 'Late Joiner', 'Other'], 1):
            db.execute('INSERT INTO owners(id,oname,o15,o12,o10) VALUES(?,?,?,?,?)', (i, n.upper(), n[:15], n[:12], n[:10]))
        self.rn = 2000
        os.environ.pop('SDM_HOSTED', None); os.environ.pop('SDM_RESIDENCY_KEY', None)
        P._writes[:] = []; P._bad[:] = []
        r = R.build('r.csv', csv_bytes(ROSTER_ROWS), code='tst')
        self.roster = r
        R.save(r, R.residency_dir(self.st.path, 'tst'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop('SDM_HOSTED', None); os.environ.pop('SDM_RESIDENCY_KEY', None)

    def race(self, day, restr='', age='3-3'):
        self.rn += 1
        self.st.db.execute('INSERT INTO races(racenum,week,restr,age) VALUES(?,?,?,?)', (self.rn, wk(day), restr, age))
        self.st.db.execute('INSERT OR IGNORE INTO weeks(week,races,entries) VALUES(?,0,0)', (wk(day),))
        return self.rn

    def run(self_, *a, **k):
        return unittest.TestCase.run(self_, *a, **k)

    def enter(self, rn, day, owner, fin):
        self.st.db.execute('INSERT INTO entries(racenum,week,hid,owner_id,fin) VALUES(?,?,?,?,?)', (rn, wk(day), 1, owner, fin))

    def board(self, d0='2026-10-05', d1='2026-10-11', rule=None, **kw):
        return P.compute(self.st, self.roster, d0, d1, rule, **kw)

    def member(self, b, name):
        return next(m for m in b['members'] if m['name'] == name)


class Rules(Base):
    def test_default_and_parse(self):
        r = P.parse_rule(None)
        self.assertEqual(r['points'][:5], [10, 8, 6, 4, 3])
        r = P.parse_rule({'points': '10,8,6', 'bonus': 'stk:2,g1:5', 'bonus_on': 'win'})
        self.assertEqual(r['points'], [10, 8, 6]); self.assertEqual(r['bonus']['stk'], 2); self.assertEqual(r['bonus']['g1'], 5)

    def test_bad_rules(self):
        for bad in ({'points': []}, {'points': ['x']}, {'points': [-1]}, {'points': [1] * 11}, {'points': [1], 'bonus_on': 'x'},
                    {'points': [float('nan')]}, {'points': [99999]}, {'points': [1], 'bonus': {'stk': 'a'}}):
            with self.assertRaises(P.PortalError, msg=str(bad)):
                P.parse_rule(bad)


class Scoring(Base):
    def test_matrix_and_rollup(self):
        r1 = self.race('2026-10-06'); r2 = self.race('2026-10-07')
        self.enter(r1, '2026-10-06', 1, 1); self.enter(r1, '2026-10-06', 2, 2)   # Alpha wins, Alpha Two 2nd
        self.enter(r2, '2026-10-07', 1, 5); self.enter(r2, '2026-10-07', 3, 3)   # Alpha 5th, Gamma 3rd
        b = self.board()
        ann, bo = self.member(b, 'Ann'), self.member(b, 'Bo')
        self.assertEqual((ann['starts'], ann['wins'], ann['seconds']), (3, 1, 1))
        self.assertEqual(ann['points'], 10 + 8 + 3); self.assertEqual(ann['total'], 21)
        self.assertEqual(bo['points'], 6); self.assertEqual(bo['thirds'], 1)
        self.assertEqual([m['name'] for m in b['members']][:2], ['Ann', 'Bo'])
        self.assertEqual(b['members'][0]['rank'], 1)

    def test_unfinished_gets_nothing(self):
        r = self.race('2026-10-06'); self.enter(r, '2026-10-06', 1, None); self.enter(r, '2026-10-06', 3, 0)
        b = self.board()
        self.assertEqual(self.member(b, 'Ann')['starts'], 1); self.assertEqual(self.member(b, 'Ann')['total'], 0)

    def test_places_beyond_matrix_score_zero(self):
        r = self.race('2026-10-06'); self.enter(r, '2026-10-06', 1, 2)
        b = self.board(rule={'points': [10]})
        self.assertEqual(self.member(b, 'Ann')['total'], 0); self.assertEqual(self.member(b, 'Ann')['seconds'], 1)

    def test_stakes_bonus_on_place_vs_win(self):
        r = self.race('2026-10-06', 'G2'); self.enter(r, '2026-10-06', 1, 1); self.enter(r, '2026-10-06', 3, 2)
        rule = {'points': [10, 8], 'bonus': {'g2': 5, 'stk': 2}, 'bonus_on': 'place'}
        b = self.board(rule=rule)
        self.assertEqual(self.member(b, 'Ann')['bonus'], 5); self.assertEqual(self.member(b, 'Bo')['bonus'], 5)
        rule['bonus_on'] = 'win'
        b = self.board(rule=rule)
        self.assertEqual(self.member(b, 'Ann')['bonus'], 5); self.assertEqual(self.member(b, 'Bo')['bonus'], 0)
        self.assertEqual(self.member(b, 'Ann')['total'], 15)

    def test_plain_race_gets_no_bonus_and_stk_class(self):
        r = self.race('2026-10-06', ''); s = self.race('2026-10-07', 'Stk')
        self.enter(r, '2026-10-06', 1, 1); self.enter(s, '2026-10-07', 1, 1)
        b = self.board(rule={'points': [10], 'bonus': {'stk': 3}})
        self.assertEqual(self.member(b, 'Ann')['bonus'], 3)

    def test_filters(self):
        a = self.race('2026-10-06', 'G1', '3-3'); c = self.race('2026-10-07', '', '2-2'); d = self.race('2026-10-08', 'Stk', '2-3')
        for r, day in ((a, '2026-10-06'), (c, '2026-10-07'), (d, '2026-10-08')):
            self.enter(r, day, 1, 1)
        self.assertEqual(self.member(self.board(age='2'), 'Ann')['starts'], 1)
        self.assertEqual(self.member(self.board(age='3'), 'Ann')['starts'], 2)
        self.assertEqual(self.member(self.board(races='stakes'), 'Ann')['starts'], 2)
        self.assertEqual(self.member(self.board(races='graded'), 'Ann')['starts'], 1)

    def test_date_range_edges_inclusive(self):
        for day in ('2026-10-04', '2026-10-05', '2026-10-11', '2026-10-12'):
            self.enter(self.race(day), day, 1, 1)
        self.assertEqual(self.member(self.board(), 'Ann')['starts'], 2)

    def test_joined_date(self):
        r1 = self.race('2026-10-01'); r2 = self.race('2026-10-06')
        self.enter(r1, '2026-10-01', 4, 1); self.enter(r2, '2026-10-06', 4, 1)
        b = self.board('2026-09-28', '2026-10-11')
        self.assertEqual(self.member(b, 'Bo')['starts'], 1)

    def test_ties_share_rank(self):
        r = self.race('2026-10-06'); self.enter(r, '2026-10-06', 1, 3); self.enter(r, '2026-10-06', 3, 3)
        b = self.board()
        self.assertEqual([m['rank'] for m in b['members']][:2], [1, 1]); self.assertTrue(b['members'][0]['tied'])
        self.assertEqual(b['members'][2]['rank'], 3)

    def test_unmatched_and_quiet(self):
        r = self.race('2026-10-06'); self.enter(r, '2026-10-06', 1, 1)
        b = self.board()
        self.assertIn('Ghost Stable', b['unmatched']); self.assertIn('Alpha Two', b['no_races'])
        self.assertEqual(self.member(b, 'Cy')['total'], 0)

    def test_match_on_short_name_columns(self):
        self.st.db.execute("INSERT INTO owners(id,oname,o15,o12,o10) VALUES(99,'x','ghost stable','','')")
        r = self.race('2026-10-06'); self.enter(r, '2026-10-06', 99, 1)
        self.assertEqual(self.member(self.board(), 'Cy')['wins'], 1)

    def test_bad_dates_and_limits(self):
        for a in (('x', '2026-10-01'), ('2026-10-05', '2026-10-01'), ('2025-01-01', '2026-10-01')):
            with self.assertRaises(P.PortalError):
                self.board(*a)
        with self.assertRaises(P.PortalError): self.board(age='9')
        with self.assertRaises(P.PortalError): self.board(races='x')

    def test_sql_injection_text_is_inert(self):
        with self.assertRaises(P.PortalError):
            self.board("2026-10-05'; DROP TABLE entries;--", '2026-10-11')
        self.assertTrue(self.st.db.execute('SELECT 1 FROM entries').fetchall() is not None)


class Exports(Base):
    def test_csv_and_json(self):
        r = self.race('2026-10-06'); self.enter(r, '2026-10-06', 1, 1)
        b = self.board()
        rows = list(csv.reader(io.StringIO(P.to_csv(b, 'members'))))
        self.assertEqual(rows[0][1], 'member'); self.assertEqual(rows[1][1], 'Ann'); self.assertEqual(rows[1][-1], '10')
        rows = list(csv.reader(io.StringIO(P.to_csv(b, 'stables'))))
        self.assertEqual(len(rows) - 1, 5)
        json.dumps(b)


class Finishes(Base):
    def test_finish_counts_ignore_scoring(self):
        for i, fin in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 11, None]):
            day = '2026-10-06'; r = self.race(day); self.enter(r, day, 1, fin)
        r = self.race('2026-10-07'); self.enter(r, '2026-10-07', 2, 2)
        b = self.board(rule={'points': [1]})                       # scoring only pays 1st; counts must not care
        ann = self.member(b, 'Ann')
        self.assertEqual(ann['finishes'], [1, 2, 1, 1, 1, 1, 1, 2]); self.assertEqual(ann['nofin'], 1)
        self.assertEqual(ann['starts'], 11)
        rows = list(csv.reader(io.StringIO(P.to_csv(b, 'finishes'))))
        self.assertEqual(rows[0], ['member', 'master_stable', 'stable', 'starts', '1st', '2nd', '3rd', '4th', '5th', '6th', '7th', '8th_or_worse', 'no_finish_recorded'])
        alpha = next(r for r in rows if r[2] == 'Alpha')
        self.assertEqual(alpha[3:], ['10', '1', '1', '1', '1', '1', '1', '1', '2', '1'])
        self.assertNotIn('points', ','.join(rows[0]))
        rows = list(csv.reader(io.StringIO(P.to_csv(b, 'finishes_members'))))
        self.assertEqual(rows[0][:4], ['member', 'masters', 'stables', 'starts'])
        self.assertEqual(next(r for r in rows if r[0] == 'Ann')[3], '11')

    def test_export_route(self):
        app = App(self.st)
        for kind in ('finishes', 'finishes_members'):
            s, c, data, extra = P.route(app, 'GET', '/api/residency/export', dict(code='tst', **{'from': '2026-10-05', 'to': '2026-10-11'}, kind=kind), {})
            self.assertEqual(s, 200); self.assertIn(kind, extra['Content-Disposition'])


class Routes(Base):
    H = {'X-SDM': '1'}

    def call(self, method, path, q=None, headers=None, body=b''):
        s, ct, data, extra = P.route(self.app, method, path, q or {}, headers or {}, body)
        return s, ct, data, extra

    def up(self, headers=None, code='newres', rows=None, **kw):
        body = json.dumps(dict(code=code, filename='r.csv', data=base64.b64encode(csv_bytes(rows or ROSTER_ROWS)).decode(), **kw)).encode()
        return self.call('POST', '/api/residency/upload', headers=self.H if headers is None else headers, body=body)

    def test_page_and_status(self):
        s, ct, data, _ = self.call('GET', '/residency')
        self.assertEqual(s, 200); self.assertIn(b'Residency Leaderboards', data)
        s, ct, data, _ = self.call('GET', '/api/residency/status')
        j = json.loads(data); self.assertTrue(j['can_write']); self.assertIsNone(j['first_date'])

    def test_upload_local_then_board(self):
        s, _, data, _ = self.up()
        j = json.loads(data); self.assertEqual(s, 200); self.assertTrue(j['saved'])
        self.assertEqual(j['match']['checked'], 5); self.assertTrue(len(j['match']['unmatched']) >= 1)
        s, _, data, _ = self.call('GET', '/api/residency/roster', {'code': 'newres'})
        self.assertEqual(s, 200); self.assertEqual(json.loads(data)['roster']['counts']['members'], 3)
        s, _, data, _ = self.call('GET', '/api/residency/board', {'code': 'newres', 'from': '2026-10-05', 'to': '2026-10-11'})
        self.assertEqual(s, 200); self.assertEqual(json.loads(data)['counts']['members'], 3)

    def test_upload_needs_header(self):
        s, _, data, _ = self.up(headers={})
        self.assertEqual(s, 403)

    def test_hosted_without_key_refuses(self):
        os.environ['SDM_HOSTED'] = '1'
        self.assertFalse(P.can_write())
        s, _, data, _ = self.up()
        self.assertEqual(s, 403)
        self.assertFalse(os.path.exists(R.residency_dir(self.st.path, 'newres')))

    def test_hosted_with_key(self):
        os.environ['SDM_HOSTED'] = '1'; os.environ['SDM_RESIDENCY_KEY'] = 'sesame'
        self.assertTrue(P.can_write())
        self.assertEqual(self.up(headers=self.H)[0], 403)
        self.assertEqual(self.up(headers=dict(self.H, **{'X-Residency-Key': 'wrong'}))[0], 403)
        self.assertEqual(self.up(headers=dict(self.H, **{'X-Residency-Key': 'sesame'}))[0], 200)
        # reading needs no key
        self.assertEqual(self.call('GET', '/api/residency/roster', {'code': 'tst'})[0], 200)

    def hosted(self, admin='sesame'):
        os.environ['SDM_HOSTED'] = '1'; os.environ['SDM_RESIDENCY_KEY'] = admin

    def hdr(self, key):
        return dict(self.H, **{'X-Residency-Key': key})

    def test_admin_creates_residency_and_gets_its_key(self):
        self.hosted()
        j = json.loads(self.up(headers=self.hdr('sesame'), code='orph')[2])
        key = j['new_key']; self.assertTrue(j['saved']); self.assertTrue(key and len(key) >= 20)
        raw = open(P._key_path(self.st, 'orph')).read()
        self.assertNotIn(key, raw)                                   # only the scrambled form is stored
        j2 = json.loads(self.up(headers=self.hdr(key), code='orph')[2])   # its own key works
        self.assertTrue(j2['saved']); self.assertIsNone(j2['new_key'])
        body = json.dumps(dict(code='orph', rule=dict(points=[5]))).encode()
        self.assertEqual(self.call('POST', '/api/residency/rules', headers=self.hdr(key), body=body)[0], 200)

    def test_residency_key_is_limited_to_its_own_residency(self):
        self.hosted()
        k1 = json.loads(self.up(headers=self.hdr('sesame'), code='aaa')[2])['new_key']
        json.loads(self.up(headers=self.hdr('sesame'), code='bbb')[2])
        self.assertEqual(self.up(headers=self.hdr(k1), code='bbb')[0], 403)
        self.assertEqual(self.up(headers=self.hdr(k1), code='ccc')[0], 403)          # cannot create a new residency
        self.assertFalse(os.path.exists(R.residency_dir(self.st.path, 'ccc')))
        nk = json.dumps(dict(code='aaa')).encode()
        self.assertEqual(self.call('POST', '/api/residency/newkey', headers=self.hdr(k1), body=nk)[0], 403)
        body = json.dumps(dict(code='bbb', rule=dict(points=[5]))).encode()
        self.assertEqual(self.call('POST', '/api/residency/rules', headers=self.hdr(k1), body=body)[0], 403)

    def test_newkey_replaces_old_key(self):
        self.hosted()
        k1 = json.loads(self.up(headers=self.hdr('sesame'), code='aaa')[2])['new_key']
        s, _, data, _ = self.call('POST', '/api/residency/newkey', headers=self.hdr('sesame'), body=json.dumps(dict(code='aaa')).encode())
        k2 = json.loads(data)['new_key']; self.assertEqual(s, 200); self.assertNotEqual(k1, k2)
        self.assertEqual(self.up(headers=self.hdr(k1), code='aaa')[0], 403)
        self.assertEqual(self.up(headers=self.hdr(k2), code='aaa')[0], 200)

    def test_newkey_for_new_code_then_residency_key_can_upload(self):
        self.hosted()
        k = json.loads(self.call('POST', '/api/residency/newkey', headers=self.hdr('sesame'), body=json.dumps(dict(code='fresh')).encode())[2])['new_key']
        self.assertEqual(self.up(headers=self.hdr(k), code='fresh')[0], 200)

    def test_newkey_local_refused_and_needs_header(self):
        nk = json.dumps(dict(code='aaa')).encode()
        self.assertEqual(self.call('POST', '/api/residency/newkey', headers=self.H, body=nk)[0], 400)
        self.hosted()
        self.assertEqual(self.call('POST', '/api/residency/newkey', headers={}, body=nk)[0], 403)

    def test_wrong_keys_are_counted_and_blocked(self):
        self.hosted()
        for i in range(P.MAX_BAD_KEYS_PER_HOUR):
            self.assertEqual(self.up(headers=self.hdr('guess%d' % i), code='tst')[0], 403)
        self.assertEqual(self.up(headers=self.hdr('sesame'), code='tst')[0], 429)      # even the right key waits

    def test_empty_key_never_matches_residency_key(self):
        self.hosted()
        json.loads(self.up(headers=self.hdr('sesame'), code='aaa')[2])
        self.assertEqual(self.up(headers=self.hdr(''), code='aaa')[0], 403)

    def test_roster_view_says_if_residency_has_key(self):
        self.hosted()
        self.up(headers=self.hdr('sesame'), code='aaa')
        j = json.loads(self.call('GET', '/api/residency/roster', {'code': 'aaa'})[2])
        self.assertTrue(j['has_key']); self.assertNotIn('hash', json.dumps(j))

    def test_bad_code_and_traversal(self):
        for c in ('../evil', 'a/b', '', 'A B', '..', 'x' * 40):
            self.assertEqual(self.up(code=c)[0], 400, c)
            self.assertEqual(self.call('GET', '/api/residency/roster', {'code': c})[0], 400, c)

    def test_bad_upload_bodies(self):
        h = self.H
        self.assertEqual(self.call('POST', '/api/residency/upload', headers=h, body=b'not json')[0], 400)
        self.assertEqual(self.call('POST', '/api/residency/upload', headers=h, body=json.dumps(dict(code='abc', data='!!!')).encode())[0], 400)
        self.assertEqual(self.call('POST', '/api/residency/upload', headers=h, body=json.dumps(dict(code='abc', data='')).encode())[0], 400)
        self.assertEqual(self.call('POST', '/api/residency/upload', headers=h, body=json.dumps(dict(code='abc', data=base64.b64encode(b'\x00\x01junk').decode(), filename='x.xlsx')).encode())[0], 400)

    def test_errors_not_saved_unless_accepted(self):
        rows = ROSTER_ROWS + [['Bo', 'Gamma', 'Alpha', '', '']]   # Alpha under two members
        j = json.loads(self.up(rows=rows, code='bad1')[2])
        self.assertFalse(j['saved']); self.assertTrue(j['problems'])
        self.assertEqual(self.call('GET', '/api/residency/roster', {'code': 'bad1'})[0], 404)
        j = json.loads(self.up(rows=rows, code='bad1', accept_errors=True)[2])
        self.assertTrue(j['saved'])

    def test_rules_save_and_use(self):
        body = json.dumps(dict(code='tst', rule=dict(points=[1, 1], bonus=dict(stk=1)))).encode()
        s, _, data, _ = self.call('POST', '/api/residency/rules', headers=self.H, body=body)
        self.assertEqual(s, 200)
        self.assertEqual(P.load_rule(self.st, 'tst')['points'], [1, 1])
        bad = json.dumps(dict(code='tst', rule=dict(points=['x']))).encode()
        self.assertEqual(self.call('POST', '/api/residency/rules', headers=self.H, body=bad)[0], 400)
        nocode = json.dumps(dict(code='nope', rule=dict(points=[1]))).encode()
        self.assertEqual(self.call('POST', '/api/residency/rules', headers=self.H, body=nocode)[0], 404)

    def test_write_rate_limit(self):
        for i in range(P.MAX_WRITES_PER_HOUR):
            self.assertEqual(self.up(code='r%d' % (i % 20 + 10))[0], 200)
        self.assertEqual(self.up(code='zzz')[0], 429)

    def test_code_limit(self):
        for i in range(P.MAX_CODES):
            os.makedirs(R.residency_dir(self.st.path, 'c%02d' % i), exist_ok=True)
        s = self.up(code='onemore')[0]
        self.assertEqual(s, 400)

    def test_exports(self):
        q = {'code': 'tst', 'from': '2026-10-05', 'to': '2026-10-11'}
        for kind, ct in (('members', 'text/csv'), ('stables', 'text/csv'), ('json', 'application/json')):
            s, c, data, extra = self.call('GET', '/api/residency/export', dict(q, kind=kind))
            self.assertEqual(s, 200); self.assertTrue(c.startswith(ct)); self.assertIn('attachment', extra['Content-Disposition'])
        self.assertEqual(self.call('GET', '/api/residency/export', dict(q, kind='zip'))[0], 400)

    def test_query_rule_overrides_saved(self):
        r = self.race('2026-10-06'); self.enter(r, '2026-10-06', 1, 1)
        q = {'code': 'tst', 'from': '2026-10-05', 'to': '2026-10-11', 'pts': '7,3', 'bonus': 'stk:0,g3:0,g2:0,g1:0'}
        j = json.loads(self.call('GET', '/api/residency/board', q)[2])
        self.assertEqual(j['rule']['points'], [7, 3]); self.assertEqual(j['members'][0]['total'], 7)

    def test_unknown_path_and_server_error_hidden(self):
        self.assertEqual(self.call('GET', '/api/residency/nope')[0], 404)
        self.st.db.close()
        s, _, data, _ = self.call('GET', '/api/residency/status')
        self.assertEqual(s, 500); self.assertNotIn(self.tmp.encode(), data)


class Safety(unittest.TestCase):
    def test_no_site_or_network_in_code(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for f in ('residency_portal.py', os.path.join('static', 'residency.html')):
            t = open(os.path.join(here, 'sdmmodern', f), encoding='utf-8').read().lower()
            for bad in ('ehorseracing', 'urllib.request', 'http.client', 'socket', 'requests', 'https://', 'http://'):
                self.assertNotIn(bad, t, (f, bad))


if __name__ == '__main__':
    unittest.main()
