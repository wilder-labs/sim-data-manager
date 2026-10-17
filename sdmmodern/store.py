"""Local SQLite store for Sim Data Manager, plus import of weekly Access (.mdb) data files."""
import csv, io, math, os, re, sqlite3, zipfile, tempfile, datetime
from .jet3 import MDB

MAX_INT = 2 ** 62
# Share of the race purse earned by each finishing position; 6th and lower earn nothing.
PURSE_SHARES = {1: 0.60, 2: 0.25, 3: 0.10, 4: 0.03, 5: 0.02}


def earnings(purse, fin):
    if not purse or fin not in PURSE_SHARES:
        return 0.0
    return round(purse * PURSE_SHARES[fin], 2)

ANCHOR_WEEK = 10295                      # sim day number ...
ANCHOR_DATE = datetime.date(2026, 9, 27)  # ... that fell on this calendar date (Sun Sep 27 2026)

SCHEMA = """
CREATE TABLE IF NOT EXISTS races(
  racenum REAL PRIMARY KEY, week INTEGER, surf TEXT, restr TEXT, cond TEXT, dist REAL,
  size INTEGER, purse INTEGER, cprice INTEGER, age TEXT, loc_etc TEXT, raceid INTEGER);
CREATE TABLE IF NOT EXISTS horses(hid INTEGER PRIMARY KEY, hname TEXT, sex TEXT);
CREATE TABLE IF NOT EXISTS owners(id INTEGER PRIMARY KEY, oname TEXT, o15 TEXT, o12 TEXT, o10 TEXT, UNIQUE(oname,o15,o12,o10));
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY, racenum REAL, week INTEGER, hid INTEGER, owner_id INTEGER, tag TEXT,
  fin INTEGER, ppos INTEGER, sp REAL, odds REAL, bps INTEGER, earn REAL);
CREATE TABLE IF NOT EXISTS packs(week INTEGER PRIMARY KEY, done_at TEXT);
CREATE TABLE IF NOT EXISTS weeks(week INTEGER PRIMARY KEY, races INTEGER, entries INTEGER);
CREATE TABLE IF NOT EXISTS tracks(num INTEGER PRIMARY KEY, code TEXT);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_e_hid ON entries(hid);
CREATE INDEX IF NOT EXISTS ix_e_race ON entries(racenum);
CREATE INDEX IF NOT EXISTS ix_e_owner ON entries(owner_id);
CREATE INDEX IF NOT EXISTS ix_h_name ON horses(hname COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_o_name ON owners(oname);
CREATE INDEX IF NOT EXISTS ix_r_week ON races(week);
"""



def app_dir():
    """The program folder (the one holding run_sdm.py)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def legacy_dir():
    return os.path.join(os.path.expanduser('~'), 'SimDataManager')


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, '.sdm_write_test')
        open(probe, 'w').close()
        os.remove(probe)
        return True
    except OSError:
        return False


def default_dir():
    """Keep the database in the program folder, so everything lives together.
    Falls back to the user folder only if the program folder cannot be written to."""
    d = os.environ.get('SDM_HOME')
    if d:
        os.makedirs(d, exist_ok=True)
        return d
    if _writable(app_dir()):
        return app_dir()
    d = legacy_dir()
    os.makedirs(d, exist_ok=True)
    return d


def week_to_date(week, anchor_week=ANCHOR_WEEK, anchor_date=ANCHOR_DATE):
    return anchor_date + datetime.timedelta(days=int(week) - anchor_week)


def date_to_week(d, anchor_week=ANCHOR_WEEK, anchor_date=ANCHOR_DATE):
    return anchor_week + (d - anchor_date).days


def split_racenum(rn):
    """10295.9614 -> (track 96, race 14).  10245.03001 -> (3, 1)."""
    frac = repr(round(float(rn), 6)).split('.')[1] if '.' in repr(float(rn)) else '0'
    frac = frac.rstrip('0') if len(frac) > 5 else frac
    if len(frac) <= 4:
        frac = frac.ljust(4, '0')
        return int(frac[:2]), int(frac[2:])
    return int(frac[:2]), int(frac[2:5].ljust(3, '0'))


class NoDatabase(ValueError):
    """A pack was readable but holds no Access 97 database. .listing says what it does hold."""
    def __init__(self, listing, note=''):
        self.listing = listing
        what = ', '.join('%s (%s)' % (n, _fmt_size(sz)) for n, sz in listing[:6]) or 'nothing'
        super().__init__('No usable database in this pack. %sIt contains: %s.' % (note + ' ' if note else '', what))


def _fmt_size(n):
    return '%d B' % n if n < 1024 else ('%d KB' % (n // 1024) if n < 1048576 else '%.1f MB' % (n / 1048576))


JET97 = b'\x00\x01\x00\x00Standard Jet DB'
ACCESS_NEW = (b'\x00\x01\x00\x00Standard ACE DB', )


def _members(data):
    """Return {name: bytes} for a .7z or .zip, or None if data is neither."""
    if data[:6] == b'7z\xbc\xaf\x27\x1c':
        from .sevenzip import extract
        return extract(data)
    if data[:2] == b'PK':
        z = zipfile.ZipFile(io.BytesIO(data))
        return {n: z.read(n) for n in z.namelist() if not n.endswith('/')}
    return None


class Store:
    def __init__(self, path=None):
        if not path:
            d = default_dir()
            path = os.path.join(d, 'sdm.db')
            old = os.path.join(d, 'sss.db')            # database from an earlier version: take it over
            if not os.path.exists(path) and os.path.exists(old):
                for suf in ('', '-wal', '-shm'):
                    if os.path.exists(old + suf):
                        os.replace(old + suf, path + suf)
        self.path = path
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        cols = [r[1] for r in self.db.execute('PRAGMA table_info(entries)')]
        if 'hname' in cols:                      # database made by the first release: upgrade it
            self._migrate_v1()
        self.db.executescript(SCHEMA)
        if 'earn' not in [r[1] for r in self.db.execute('PRAGMA table_info(entries)')]:
            self.db.execute('ALTER TABLE entries ADD COLUMN earn REAL')
        self.db.executescript(INDEXES)
        self.fill_earnings()
        # earlier builds could let a few damaged rows through with a race number of 0; they are never real races
        if self.db.execute('SELECT 1 FROM entries WHERE racenum < 1000 LIMIT 1').fetchone():
            self.db.execute('DELETE FROM entries WHERE racenum < 1000')
            self.db.execute('DELETE FROM races WHERE racenum < 1000')
            self.db.execute('DELETE FROM weeks WHERE week < 1000')
        if not self.db.execute('SELECT 1 FROM tracks LIMIT 1').fetchone():
            self.load_track_text(open(os.path.join(os.path.dirname(__file__), 'statecodes_default.txt'), encoding='latin-1').read())
        self.db.commit()

    def fill_earnings(self, force=False):
        """Work out each runner's earnings from the race purse. Done once for an existing database, then kept up to date on import."""
        if not force and self.get_setting('earn_v') == '1':
            return
        sh = PURSE_SHARES
        case = 'CASE e.fin ' + ' '.join('WHEN %d THEN %r' % (k, v) for k, v in sh.items()) + ' ELSE 0 END'
        self.db.execute('UPDATE entries AS e SET earn = COALESCE((SELECT r.purse FROM races r WHERE r.racenum = e.racenum), 0) * (%s)' % case)
        self.set_setting('earn_v', '1')
        self.db.commit()

    def _migrate_v1(self):
        c = self.db
        c.execute('ALTER TABLE entries RENAME TO entries_v1')
        c.executescript(SCHEMA)
        c.execute('INSERT OR REPLACE INTO horses SELECT hid, hname, sex FROM entries_v1 ORDER BY id')
        c.execute('INSERT OR IGNORE INTO owners(oname,o15,o12,o10) SELECT DISTINCT oname,oname15,oname12,oname10 FROM entries_v1')
        c.execute("""INSERT INTO entries(racenum,week,hid,owner_id,tag,fin,ppos,sp,odds,bps)
                     SELECT v.racenum,v.week,v.hid,o.id,v.tag,v.fin,v.ppos,v.sp,v.odds,v.bps FROM entries_v1 v
                     JOIN owners o ON o.oname IS v.oname AND o.o15 IS v.oname15 AND o.o12 IS v.oname12 AND o.o10 IS v.oname10""")
        c.execute('DROP TABLE entries_v1')
        c.execute('INSERT OR REPLACE INTO weeks SELECT week, COUNT(DISTINCT racenum), COUNT(*) FROM entries GROUP BY week')
        c.commit()
        c.execute('VACUUM')

    # ---- tracks
    def load_track_text(self, text):
        rows = [(int(a), b) for a, b in csv.reader(io.StringIO(text)) if a.strip().isdigit()]
        self.db.executemany('INSERT OR REPLACE INTO tracks VALUES(?,?)', rows)
        self.db.commit()

    def tracks(self):
        return {r['num']: r['code'] for r in self.db.execute('SELECT * FROM tracks ORDER BY num')}

    def set_track(self, num, code):
        self.db.execute('INSERT OR REPLACE INTO tracks VALUES(?,?)', (int(num), code.strip().upper()))
        self.db.commit()

    # ---- settings / date anchor
    def anchor(self):
        r = {x['k']: x['v'] for x in self.db.execute('SELECT * FROM settings')}
        if 'anchor_week' in r and 'anchor_date' in r:
            return int(r['anchor_week']), datetime.date.fromisoformat(r['anchor_date'])
        return ANCHOR_WEEK, ANCHOR_DATE

    def set_anchor(self, week, date):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES('anchor_week',?)", (str(int(week)),))
        self.db.execute("INSERT OR REPLACE INTO settings VALUES('anchor_date',?)", (date.isoformat(),))
        self.db.commit()

    def get_setting(self, k, default=None):
        r = self.db.execute('SELECT v FROM settings WHERE k=?', (k,)).fetchone()
        return r[0] if r else default

    def set_setting(self, k, v):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (k, str(v)))
        self.db.commit()

    # ---- info
    def info(self):
        q = self.db.execute
        n_e, n_r = q('SELECT COALESCE(SUM(entries),0), COALESCE(SUM(races),0) FROM weeks').fetchone()
        n_h = q('SELECT COUNT(*) FROM horses').fetchone()[0]
        lo, hi = q('SELECT MIN(week), MAX(week) FROM weeks').fetchone()
        aw, ad = self.anchor()
        return dict(entries=n_e, races=n_r, horses=n_h, first_week=lo, last_week=hi,
                    first_date=week_to_date(lo, aw, ad).isoformat() if lo else None,
                    last_date=week_to_date(hi, aw, ad).isoformat() if hi else None,
                    weeks=[dict(week=r[0], races=r[1], entries=r[2], date=week_to_date(r[0], aw, ad).isoformat())
                           for r in q('SELECT week, races, entries FROM weeks ORDER BY week')],
                    anchor_week=aw, anchor_date=ad.isoformat(), db_mb=round(os.path.getsize(self.path) / 1048576, 1), db_path=os.path.abspath(self.path))

    def weeks_present(self):
        return {r[0] for r in self.db.execute('SELECT week FROM weeks')}

    # ---- import
    @staticmethod
    def _find_database(data, depth=0):
        if data[:19] == JET97:
            if data[0x14] != 0:
                raise ValueError('This database is in the Access 2000 or newer format, which this version cannot read yet.')
            return data
        if data[:19] in ACCESS_NEW or data[:15] == b'\x00\x01\x00\x00Standard A':
            raise ValueError('This database is in the newer .accdb format, which this version cannot read yet.')
        files = _members(data)
        if files is None:
            raise ValueError('That file is not an Access database, a .7z or a .zip.')
        listing = sorted(((n, len(b)) for n, b in files.items()), key=lambda t: -t[1])
        # prefer a real database (by signature), then anything archive-like inside (depth 1 only)
        dbs = [b for n, b in sorted(files.items(), key=lambda t: -len(t[1])) if b[:19] == JET97 and len(b) > 0x14 and b[0x14] == 0]
        if dbs:
            return dbs[0]
        if any(b[:19] == JET97 and len(b) > 0x14 and b[0x14] != 0 for b in files.values()):
            raise NoDatabase(listing, 'It holds an Access 2000 or newer database, which this version cannot read yet.')
        if any(b[:15] == b'\x00\x01\x00\x00Standard A' for b in files.values()):
            raise NoDatabase(listing, 'It holds a newer-format Access database (.accdb), which this version cannot read yet.')
        if depth < 1:
            for n, b in sorted(files.items(), key=lambda t: -len(t[1])):
                if b[:6] == b'7z\xbc\xaf\x27\x1c' or b[:2] == b'PK':
                    return Store._find_database(b, depth + 1)
        raise NoDatabase(listing)

    @staticmethod
    def _open_mdb_bytes(data, filename=''):
        """Accept a raw .mdb, or a .7z / .zip containing one. The database is recognised by its contents,
        not its file name, so a renamed or nested file still works."""
        data = Store._find_database(data)
        fd, tmp = tempfile.mkstemp(suffix='.mdb')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            return MDB(tmp), tmp
        except Exception:
            os.unlink(tmp)
            raise

    def preview_mdb(self, data):
        m, tmp = self._open_mdb_bytes(data)
        try:
            h, r = self._pick_tables(m)
            weeks = sorted({int(x['racenum']) for x in m.rows(h) if not self._bad(x.get('racenum'))})
            have = self.weeks_present()
            return dict(weeks=weeks, overlap=[w for w in weeks if w in have], tables=[h, r])
        finally:
            os.unlink(tmp)

    @staticmethod
    def _pick_tables(m):
        t = m.tables()
        for h, r in (('D_PRE_tblhorsedata', 'D_tblraces'), ('tblhorsedata', 'tblraces')):
            if h in t and r in t:
                return h, r
        raise ValueError('This database does not look like a sim weekly data file (tables: %s).' % ', '.join(t))

    def import_mdb(self, data, replace=True):
        m, tmp = self._open_mdb_bytes(data)
        try:
            h, r = self._pick_tables(m)
            races = [x for x in m.rows(r)]
            ents = [x for x in m.rows(h)]
        finally:
            os.unlink(tmp)
        return self._import_rows(races, ents, replace)

    @staticmethod
    def _bad(v):
        return v is None or (isinstance(v, float) and v != v)

    def _import_rows(self, races, ents, replace=True):
        n_races, n_ents = len(races), len(ents)
        hid_of = lambda x: x.get('hid_short') if x.get('hid_short') is not None else x.get('hid')
        # Damaged data never fails a whole pack: unusable numbers become empty, and rows whose key
        # fields (race number, horse id) are unusable are skipped and counted.
        for x in races + ents:
            for k, v in list(x.items()):
                if isinstance(v, bool) or v is None:
                    continue
                if isinstance(v, float) and (not math.isfinite(v) or (abs(v) > 1e15 and k not in ('racenum', 'hid', 'hid_short'))):
                    x[k] = None
                elif isinstance(v, int) and abs(v) > MAX_INT:
                    x[k] = None
        sane_race = lambda v: isinstance(v, (int, float)) and math.isfinite(v) and 1000 <= v < 1e6
        sane_hid = lambda v: isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v < MAX_INT
        races = [x for x in races if sane_race(x.get('racenum'))]
        ents = [x for x in ents if sane_race(x.get('racenum')) and sane_hid(hid_of(x))]
        skipped = (n_races - len(races)) + (n_ents - len(ents))
        weeks = sorted({int(x['racenum']) for x in ents} | {int(x['racenum']) for x in races})
        if not weeks:
            return dict(imported=True, weeks=[], races=0, entries=0, replaced=[], skipped_rows=skipped)
        have = self.weeks_present()
        overlap = [w for w in weeks if w in have]
        if overlap and not replace:
            return dict(imported=False, overlap=overlap)
        cur = self.db.cursor()
        marks = ','.join('?' * len(weeks))
        for w in weeks:                       # race numbers start with the week, so ranges can use the index
            cur.execute('DELETE FROM entries WHERE racenum >= ? AND racenum < ?', (w, w + 1))
            cur.execute('DELETE FROM races WHERE racenum >= ? AND racenum < ?', (w, w + 1))
        cur.execute(f'DELETE FROM weeks WHERE week IN ({marks})', weeks)
        cur.executemany('INSERT OR REPLACE INTO races VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        [(round(x['racenum'], 6), int(x['racenum']), x.get('surf'), x.get('restr'), x.get('cond'), x.get('dist'),
                          x.get('size'), x.get('purse'), x.get('cprice'), x.get('age'), x.get('loc_etc'), x.get('raceid')) for x in races])
        owners = {(o['oname'], o['o15'], o['o12'], o['o10']): o['id']
                  for o in cur.execute('SELECT id,oname,o15,o12,o10 FROM owners')}
        rows, horses = [], {}
        purse_of = {round(x['racenum'], 6): x.get('purse') for x in races}
        for x in ents:
            hid = int(hid_of(x))
            on = x.get('oname')
            key = (on, x.get('oname_15') or on, x.get('oname_12') or on, x.get('oname_10') or on)
            oid = owners.get(key)
            if oid is None:
                oid = cur.execute('INSERT INTO owners(oname,o15,o12,o10) VALUES(?,?,?,?)', key).lastrowid
                owners[key] = oid
            horses[hid] = (hid, x.get('hname'), x.get('sex'))
            rows.append((round(x['racenum'], 6), int(x['racenum']), hid, oid, x.get('tag'),
                         x.get('fin'), x.get('ppos'), x.get('sp'), x.get('odds'), x.get('BPs'),
                         earnings(purse_of.get(round(x['racenum'], 6)), x.get('fin'))))
        cur.executemany('INSERT OR REPLACE INTO horses VALUES(?,?,?)', horses.values())
        cur.executemany('INSERT INTO entries(racenum,week,hid,owner_id,tag,fin,ppos,sp,odds,bps,earn) VALUES(?,?,?,?,?,?,?,?,?,?,?)', rows)
        per_week = {}
        for r in rows:
            d = per_week.setdefault(r[1], [set(), 0]); d[0].add(r[0]); d[1] += 1
        cur.executemany('INSERT INTO weeks VALUES(?,?,?)', [(w, len(v[0]), v[1]) for w, v in per_week.items()])
        self.db.commit()
        return dict(imported=True, weeks=weeks, races=len(races), entries=len(rows), replaced=overlap, skipped_rows=skipped)

    def remove_weeks(self, first, last):
        c = self.db.cursor()
        a = c.execute('DELETE FROM entries WHERE racenum >= ? AND racenum < ?', (first, last + 1)).rowcount
        b = c.execute('DELETE FROM races WHERE racenum >= ? AND racenum < ?', (first, last + 1)).rowcount
        c.execute('DELETE FROM weeks WHERE week BETWEEN ? AND ?', (first, last))
        self.db.commit()
        return dict(entries=a, races=b)

    def compact(self):
        self.db.execute('VACUUM')
