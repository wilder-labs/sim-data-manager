"""Horse id mapping between the sim site's numbers and the database's numbers. No network access."""

STORE = None


def use_store(store):
    """Lets site_to_hid look horses up in the database."""
    global STORE
    STORE = store


def _known(hid):
    try:
        return STORE is not None and STORE.db.execute('SELECT 1 FROM horses WHERE hid = ?', (hid,)).fetchone() is not None
    except Exception:
        return False


def site_to_hid(s):
    """The site writes a horse id as 1 + year + 8 digits. The database keeps 10-digit ids as 'year + 8 digits' and
    9-digit ids with one 0 dropped, so a 0 in the fourth place can mean either; the database decides."""
    s = str(s).strip()
    if not (len(s) >= 6 and s.isdigit()):
        return None
    short = int(s[1:3] + s[4:])
    if len(s) == 11:
        if s[3] != '0':
            return int(s[1:])
        full = int(s[1:])
        if _known(full):
            return full
        if _known(short):
            return short
    return short


def parse(html):
    """-> dict(dist_f, surf, horses=[...]) or None when the page holds no race data."""
    p = _Inputs()
    p.feed(html)
    n = _i(p.v.get('numin'))
    horses = []
    for i in range(1, n + 1):
        a = (p.v.get('horseinfo%d' % i) or '').split('|')
        if len(a) < 20:
            continue
        horses.append(dict(
            num=_i(a[1]), post=_i(a[0]), name=a[2].strip(), odds=_f(a[3]), coat=_i(a[4]), owner=a[5].strip(),
            calls=[[_i(a[6]), _i(a[7])], [_i(a[8]), _i(a[9])], [_i(a[10]), _i(a[11])], [_i(a[12]), _i(a[13])]],
            speed=round(_f(a[18]), 2), hid=site_to_hid(a[19])))
    if not horses:
        return None
    return dict(dist_f=_f(p.v.get('distance')) / 1000.0, surf=(p.v.get('surface') or '').strip().lower(), horses=horses)


def fetch(raceid, timeout=15):
    req = urllib.request.Request(URL % int(raceid), headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('latin-1', 'replace')


def _ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS race_calls(raceid INTEGER PRIMARY KEY, fetched INTEGER, data TEXT)')


def cached(store, raceid):
    """-> dict (saved race), {'none': True} (site had nothing recently), or None (never fetched / retry)."""
    _ensure(store.db)
    r = store.db.execute('SELECT fetched, data FROM race_calls WHERE raceid=?', (raceid,)).fetchone()
    if not r:
        return None
    if r['data']:
        d = json.loads(r['data'])
        d['cached'] = True
        return d
    if time.time() - r['fetched'] < MISS_DAYS * 86400:
        return {'none': True}
    return None


def save(store, raceid, data):
    _ensure(store.db)
    store.db.execute('INSERT OR REPLACE INTO race_calls(raceid,fetched,data) VALUES(?,?,?)',
                     (raceid, int(time.time()), json.dumps(data) if data else ''))
    store.db.commit()
