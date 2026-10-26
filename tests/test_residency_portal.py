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


class Polls(unittest.TestCase):
    """Poll tests use their own small roster: five members with a handful of stables each."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = FakeStore(self.tmp)
        self.app = App(self.st)
        os.environ.pop('SDM_HOSTED', None); os.environ.pop('SDM_RESIDENCY_KEY', None); P._writes[:] = []; P._bad[:] = []
        rows = [['Member', 'Master stable', 'Stable']]
        self.names = ['Ann', 'Bo', 'Cy', 'Di', 'Ed<script>x</script>']
        for i, m in enumerate(self.names):
            rows.append([m, 's%d' % i, 's%d' % i])
        self.roster = R.build('r.csv', csv_bytes(rows), code='pl')
        R.save(self.roster, R.residency_dir(self.st.path, 'pl'))
        for i in range(5):
            self.st.db.execute('INSERT INTO owners(id,oname,o15,o12,o10) VALUES(?,?,?,?,?)', ('%d' % (i + 1), 's%d' % i, 's%d' % i, 's%d' % i, 's%d' % i))
        self.rn = 5000

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_(self, day, owner, fin, restr='Clm', earn=1000):
        self.rn += 1
        self.st.db.execute('INSERT INTO races(racenum,week,restr,age) VALUES(?,?,?,?)', (self.rn, wk(day), restr, 'F-3'))
        self.st.db.execute('INSERT OR IGNORE INTO weeks(week,races,entries) VALUES(?,0,0)', (wk(day),))
        self.st.db.execute('INSERT INTO entries(racenum,week,hid,owner_id,fin,earn) VALUES(?,?,?,?,?,?)', (self.rn, wk(day), 1, owner, fin, earn))

    def fill(self, owner, day, fins, restr='Clm'):
        for f in fins:
            self.run_(day, owner, f, restr)

    def test_claiming_split_codes(self):
        for c in ('Clm', 'MC', '25', 'C20'):
            self.assertTrue(P.is_claiming(c), c)
        for c in ('', 'Stk', 'G1', 'Alw', None, 'Opt'):
            self.assertFalse(P.is_claiming(c), c)

    def test_only_claiming_races_count_in_claiming_poll(self):
        self.fill(1, '2026-10-06', [1] * 6, 'Clm'); self.fill(1, '2026-10-06', [1] * 20, 'Alw')
        d = P.polls(self.st, self.roster, '2026-10-05', '2026-10-18')
        r = d['claiming']['rows'][0]
        self.assertEqual((r['name'], r['starts'], r['wins']), ('Ann', 6, 6))
        self.assertEqual(next(x for x in d['overall']['rows'] if x['name'] == 'Ann')['starts'], 26)

    def test_small_sample_does_not_beat_steady_record(self):
        self.fill(1, '2026-10-06', [1, 1, 1, 1, 1])                      # 5 for 5
        self.fill(2, '2026-10-06', [1, 1, 1, 1, 1, 2, 2, 3, 3, 3, 4, 5, 6, 7, 1, 1, 2, 2, 3, 3])   # 20 starts, steady
        d = P.polls(self.st, self.roster, '2026-10-05', '2026-10-18')
        names = [r['name'] for r in d['claiming']['rows']]
        self.assertEqual(names[0], 'Bo')                                   # steady member leads
        self.assertIn('Ann', names)                                        # but the small barn is in the poll

    def test_minimum_starts_scales_with_range(self):
        self.fill(1, '2026-10-06', [1, 1, 2])                              # 3 starts
        wide = P.polls(self.st, self.roster, '2026-09-21', '2026-10-18')
        short = P.polls(self.st, self.roster, '2026-10-12', '2026-10-18')
        self.assertEqual(wide['claiming']['rows'], [])                      # 4 weeks needs 5 starts
        self.assertEqual(short['claiming']['min_starts'], 3)
        self.assertEqual(wide['claiming']['min_starts'], 5)

    def test_arrows_compare_with_previous_range(self):
        self.fill(1, '2026-10-06', [1, 1, 1, 1, 1, 2]); self.fill(2, '2026-10-06', [5, 6, 7, 8, 5, 5])
        self.fill(1, '2026-09-30', [5, 6, 7, 8, 5, 5]); self.fill(2, '2026-09-30', [1, 1, 1, 1, 1, 2])     # reversed in the range before
        self.fill(3, '2026-10-07', [2, 2, 3, 3, 4, 5])                                                      # new this range
        d = P.polls(self.st, self.roster, '2026-10-05', '2026-10-11')
        by = {r['name']: r for r in d['claiming']['rows']}
        self.assertEqual(d['previous'], {'from': '2026-09-28', 'to': '2026-10-04'})
        self.assertEqual(by['Ann']['rank'], 1); self.assertEqual(by['Ann']['previous_rank'], 2); self.assertEqual(by['Ann']['move'], 1)
        self.assertEqual(by['Bo']['move'], -2)               # was first before, now behind the new member too
        self.assertIsNone(by['Cy']['move']); self.assertIsNone(by['Cy']['previous_rank'])

    def test_stakes_quality_lifts_overall_not_claiming(self):
        base = [1, 2, 3, 4, 5, 6, 7, 8, 5]
        self.fill(1, '2026-10-06', base); self.fill(2, '2026-10-06', base)
        self.fill(2, '2026-10-07', [1, 1], 'G2')
        d = P.polls(self.st, self.roster, '2026-10-05', '2026-10-18')
        o = {r['name']: r for r in d['overall']['rows']}
        self.assertGreater(o['Bo']['parts']['qual'], o['Ann']['parts']['qual'])
        self.assertNotIn('qual', d['claiming']['rows'][0]['parts'])

    def test_parts_and_rating_in_range(self):
        self.fill(1, '2026-10-06', [1, 2, 3, 4, 5, 6, 1, 2, 3, 8])
        d = P.polls(self.st, self.roster, '2026-10-05', '2026-10-18')
        for mode in ('claiming', 'overall'):
            for r in d[mode]['rows']:
                self.assertTrue(0 <= r['rating'] <= 100)
                self.assertTrue(all(0 <= v <= 100 for v in r['parts'].values()))
                self.assertEqual(sum(d['weights'][mode].values()), 100)

    def test_ties_break_on_wins_then_name(self):
        for o in (1, 2):
            self.fill(o, '2026-10-06', [1, 2, 3, 4, 5])
        rows = P.polls(self.st, self.roster, '2026-10-05', '2026-10-18')['claiming']['rows']
        self.assertEqual([r['name'] for r in rows][:2], ['Ann', 'Bo'])

    def test_bad_inputs(self):
        for a in (('x', '2026-10-01'), ('2026-10-05', '2026-10-01'), ('2025-01-01', '2026-10-01')):
            with self.assertRaises(P.PortalError):
                P.polls(self.st, self.roster, *a)
        with self.assertRaises(P.PortalError):
            P.polls(self.st, self.roster, '2026-10-05', '2026-10-06', 'x')

    def test_csv(self):
        self.fill(1, '2026-10-06', [1, 2, 3, 4, 5, 6])
        d = P.polls(self.st, self.roster, '2026-10-05', '2026-10-18')
        rows = list(csv.reader(io.StringIO(P.polls_csv(d, 'claiming'))))
        self.assertEqual(rows[0][3], 'member'); self.assertEqual(rows[1][3], 'Ann'); self.assertEqual(rows[1][-1], str(d['claiming']['rows'][0]['rating']))

    def test_embed_is_script_free_and_escaped(self):
        for o in range(1, 6):
            self.fill(o, '2026-10-06', [1, 2, 3, 4, 5, 6])
        for board in ('claiming', 'overall', 'standings'):
            for skin in P.SKINS:
                h = P.embed_html(self.st, self.roster, '2026-10-05', '2026-10-18', board, skin)
                self.assertNotIn('<script', h.lower()); self.assertNotIn('onclick', h.lower())
                self.assertIn('Ed&lt;script&gt;', h)
                self.assertTrue(h.count('<style>') == 1 and '</div>' in h)
        with self.assertRaises(P.PortalError):
            P.embed_html(self.st, self.roster, '2026-10-05', '2026-10-18', 'claiming', 'evil')
        with self.assertRaises(P.PortalError):
            P.embed_html(self.st, self.roster, '2026-10-05', '2026-10-18', 'nope', 'plain')

    def test_embed_scoped_css(self):
        self.fill(1, '2026-10-06', [1, 2, 3, 4, 5, 6])
        h = P.embed_html(self.st, self.roster, '2026-10-05', '2026-10-18', 'claiming', 'divi')
        css = h[h.index('<style>') + 7:h.index('</style>')]
        import re as _re
        for sel in _re.findall(r'(?:^|\})([^{}@]+)\{', css):
            for part in sel.split(','):
                self.assertTrue(part.strip().startswith('.rzb-'), part)     # nothing styles the host page

    def test_routes(self):
        self.fill(1, '2026-10-06', [1, 2, 3, 4, 5, 6])
        q = {'code': 'pl', 'from': '2026-10-05', 'to': '2026-10-18'}
        s, ct, data, _ = P.route(self.app, 'GET', '/api/residency/polls', q, {})
        self.assertEqual(s, 200); self.assertEqual(json.loads(data)['claiming']['rows'][0]['name'], 'Ann')
        s, ct, data, extra = P.route(self.app, 'GET', '/api/residency/embed', dict(q, board='claiming', skin='btb'), {})
        self.assertEqual(s, 200); self.assertIn('attachment', extra['Content-Disposition']); self.assertIn("default-src 'none'", extra['Content-Security-Policy'])
        s, ct, data, extra = P.route(self.app, 'GET', '/api/residency/embed', dict(q, board='standings', skin='plain', view='1'), {})
        self.assertEqual(s, 200); self.assertNotIn('Content-Disposition', extra)
        self.assertEqual(P.route(self.app, 'GET', '/api/residency/embed', dict(q, skin='x'), {})[0], 400)
        for kind in ('poll_claiming', 'poll_overall'):
            s, ct, data, extra = P.route(self.app, 'GET', '/api/residency/export', dict(q, kind=kind), {})
            self.assertEqual(s, 200); self.assertTrue(ct.startswith('text/csv'))


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
        # reading a private residency needs its key
        self.assertEqual(self.call('GET', '/api/residency/roster', {'code': 'tst'})[0], 403)
        self.assertEqual(self.call('GET', '/api/residency/roster', {'code': 'tst'}, headers={'X-Residency-Key': 'sesame'})[0], 200)

    def hosted(self, admin='sesame'):
        os.environ['SDM_HOSTED'] = '1'; os.environ['SDM_RESIDENCY_KEY'] = admin

    def hdr(self, key):
        return dict(self.H, **{'X-Residency-Key': key})

    def test_wrong_keys_are_counted_and_blocked(self):
        self.hosted()
        for i in range(P.MAX_BAD_KEYS_PER_HOUR):
            self.assertEqual(self.up(headers=self.hdr('guess%d' % i), code='tst')[0], 403)
        self.assertEqual(self.up(headers=self.hdr('sesame'), code='tst')[0], 429)      # even the right key waits

    def test_empty_key_never_matches_residency_key(self):
        self.hosted()
        json.loads(self.up(headers=self.hdr('sesame'), code='aaa')[2])
        self.assertEqual(self.up(headers=self.hdr(''), code='aaa')[0], 403)

    def pub(self, key, code='tst', **kw):
        body = json.dumps(dict(code=code, **kw)).encode()
        return self.call('POST', '/api/residency/public', headers=self.hdr(key), body=body)

    def live(self, **q):
        q.setdefault('code', 'tst')
        return self.call('GET', '/api/residency/live', q)

    def test_live_is_off_until_admin_switches_it_on(self):
        self.hosted()
        self.race('2026-10-01', 'Clm'); self.race('2026-10-14', 'Clm'); self.st.db.commit()
        self.assertEqual(self.live()[0], 404)                       # not public yet
        self.assertEqual(self.live(code='nope')[0], 404)            # same answer for a code that does not exist
        self.assertEqual(self.pub('sesame', enabled=True)[0], 200)
        s, ct, data, hdr = self.live()
        self.assertEqual(s, 200); self.assertIn('text/html', ct)
        self.assertIn(b'<table', self.live(board='standings')[2])
        self.assertIn('frame-ancestors *', hdr['Content-Security-Policy'])
        self.assertNotIn(b'<script', data.lower())
        self.assertEqual(self.pub('sesame', enabled=False)[0], 200)
        self.assertEqual(self.live()[0], 404)

    def test_private_residency_reads_need_a_key_public_ones_do_not(self):
        self.hosted()
        for path in ('roster', 'board', 'polls', 'embed', 'export'):
            q = {'code': 'tst', 'from': '2026-10-05', 'to': '2026-10-11', 'board': 'standings'}
            self.assertEqual(self.call('GET', '/api/residency/' + path, q)[0], 403, path)
            self.assertEqual(self.call('GET', '/api/residency/' + path, q, headers={'X-Residency-Key': 'bad'})[0], 403, path)
        self.pub('sesame', enabled=True)
        self.assertEqual(self.call('GET', '/api/residency/board', {'code': 'tst', 'from': '2026-10-05', 'to': '2026-10-11'})[0], 200)

    def test_live_ranges_count_back_from_newest_day(self):
        self.race('2026-01-02', 'Clm'); self.race('2026-10-14', 'Clm'); self.st.db.commit()          # newest day is a Wednesday
        self.assertEqual(P.live_range(self.st, 'this_week'), ('2026-10-12', '2026-10-14'))
        self.assertEqual(P.live_range(self.st, 'latest'), ('2026-10-05', '2026-10-11'))
        self.assertEqual(P.live_range(self.st, 'last_week'), ('2026-10-05', '2026-10-11'))
        f, t = P.live_range(self.st, 'last_4'); self.assertEqual(t, '2026-10-11'); self.assertEqual((datetime.date.fromisoformat(t) - datetime.date.fromisoformat(f)).days, 27)
        self.assertEqual(P.live_range(self.st, 'season'), ('2026-01-02', '2026-10-14'))
        self.race('2026-10-18', 'Clm'); self.st.db.commit()          # a Sunday: that week is finished
        self.assertEqual(P.live_range(self.st, 'latest'), ('2026-10-12', '2026-10-18'))

    def test_live_rejects_odd_values(self):
        self.hosted(); self.race('2026-10-01', 'Clm'); self.race('2026-10-14', 'Clm'); self.st.db.commit(); self.pub('sesame', enabled=True)
        for q in ({'board': 'x'}, {'skin': 'evil'}, {'range': '../..'}, {'code': '../x'}, {'code': ''}):
            self.assertEqual(self.live(**q)[0], 404, q)
        self.assertEqual(self.live(skin='btb')[0], 200)


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
