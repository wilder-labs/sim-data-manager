"""Sire Finder and price watch. Reads the public eHorseRacing 'Find Sires' list (no login needed), keeps a daily
price snapshot of every sire it sees in the local database, and checks a watchlist of sires against limits you set."""
import re, html, time, datetime, urllib.request, urllib.parse

URL = 'http://ehorseracing.com/includes/findsires/findsires_inline.asp'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
PAGE_SIZE = 100
FILTERS = {'a': 'All', 's': 'Sire', '2': '2YO Sire', '3': '3YO Sire', 'd': 'DS', 'g': 'DDS', 'j': 'Newer'}
SORTS = {'creditcost': 'Credits', 'sire_cost': 'BP', 'horsename': 'Name', 'dds_credit_cost': 'DDS credits'}

_ROW = re.compile(r'<tr[\s>]', re.I)
_TD = re.compile(r'<td\b[^>]*>(.*?)</td>', re.S | re.I)
_SIRE = re.compile(r"loadsire\('(\d+)'\)")
_NEXT = re.compile(r'setstart\((\d+)\);\s*selsires\(\)[^>]*>\s*(?:&nbsp;)?\s*Next', re.I)


def _txt(s):
    if re.search(r'<img', s, re.I):
        return '✓'
    return html.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', s))).replace('\xa0', ' ').strip()


def _int(s):
    s = (s or '').replace(',', '').strip()
    return int(s) if s.isdigit() else None


def parse(page):
    """-> (rows, next_start or None). Row: sire_id, name, loc, loc2, cost, bp, y2, y3, ds, dds_cost, dds_bp, dds, sire_of."""
    rows = []
    for chunk in _ROW.split(page):
        m = _SIRE.search(chunk)
        if not m:
            continue
        body = chunk.split('</tr>')[0]
        body = re.sub(r'<!--.*?-->', '', body, flags=re.S)       # the site leaves commented-out cells in every row
        cells = _TD.findall(body)
        if len(cells) < 6:
            continue
        c = [_txt(x) for x in cells]
        anchors = _SIRE.findall(body)
        r = dict(sire_id=m.group(1), name=c[0], loc=c[1], loc2=c[2], cost=None, bp=None, y2=False, y3=False, ds=False,
                 dds_cost=None, dds_bp=None, dds=False, sire_of='')
        # 'Cred?' says whether the sire can be bought with credits; cost reads n/a when only BP can buy it
        r['cost'], r['bp'] = _int(c[4]), _int(c[5])
        r['y2'], r['y3'], r['ds'] = (c[6:7] == ['✓']), (c[7:8] == ['✓']), (c[8:9] == ['✓'])
        if len(c) > 12 and _int(c[11]) is not None:
            r['dds_cost'], r['dds_bp'], r['dds'] = _int(c[10]), _int(c[11]), c[12] == '✓'
        if len(anchors) > 1:
            nm = re.search(r"loadsire\('%s'\)[^>]*>(.*?)</a>" % anchors[1], body, re.S)
            r['sire_of'] = _txt(nm.group(1)) if nm else ''
        rows.append(r)
    nx = _NEXT.search(page)
    return rows, (int(nx.group(1)) if nx else None)


def fetch_page(filt='a', sort='creditcost', start=0, name='', loc='all', timeout=25):
    q = urllib.parse.urlencode(dict(a='', t1='', sort=sort, genx=1, l1=start, sireplace=loc, chosen1=filt, chosen2='b',
                                    start=0, sirematch=name, sireloc=loc))
    req = urllib.request.Request(URL + '?' + q, headers={'User-Agent': UA, 'Referer': 'http://ehorseracing.com/findsires.asp?sel=breeding'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('latin-1', 'replace')


def collect(filt='a', sort='creditcost', name='', loc='all', pages=3, fetch=fetch_page):
    """Reads up to `pages` pages of 100 sires, one after the other (the site is polite-paced). -> (rows, more)."""
    out, start, more = [], 0, False
    for i in range(max(1, min(int(pages), 60))):
        rows, nxt = parse(fetch(filt, sort, start, name, loc))
        out.extend(rows)
        if nxt is None or not rows:
            break
        start = nxt
        if i == pages - 1:
            more = True
        time.sleep(0.4)
    return out, more


# ---- storage -------------------------------------------------------------------------------------------------
def ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS sire_prices(sire_id TEXT, day TEXT, name TEXT, loc TEXT, cost INTEGER, bp INTEGER, '
               'y2 INTEGER, y3 INTEGER, ds INTEGER, dds_cost INTEGER, dds_bp INTEGER, PRIMARY KEY(sire_id, day))')
    db.execute('CREATE TABLE IF NOT EXISTS sire_watch(sire_id TEXT PRIMARY KEY, name TEXT, max_cost INTEGER, max_bp INTEGER, '
               'added TEXT, last_cost INTEGER, last_bp INTEGER, last_checked TEXT, alert_key TEXT)')


def today():
    return datetime.date.today().isoformat()


def snapshot(store, rows):
    ensure(store.db)
    d = today()
    store.db.executemany(
        'INSERT OR REPLACE INTO sire_prices(sire_id,day,name,loc,cost,bp,y2,y3,ds,dds_cost,dds_bp) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        [(r['sire_id'], d, r['name'], r['loc'], r['cost'], r['bp'], int(r['y2']), int(r['y3']), int(r['ds']), r['dds_cost'], r['dds_bp'])
         for r in rows if r['cost'] is not None or r['bp'] is not None])
    store.db.commit()


def history(store, sire_id, days=90):
    ensure(store.db)
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return [dict(r) for r in store.db.execute(
        'SELECT day,cost,bp FROM sire_prices WHERE sire_id=? AND day>=? ORDER BY day', (sire_id, since))]


def ranges(store, ids, days=30):
    """Lowest and highest price seen over the last `days` days, per sire."""
    ensure(store.db)
    if not ids:
        return {}
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    q = ','.join('?' * len(ids))
    out = {}
    for r in store.db.execute('SELECT sire_id,MIN(cost) c0,MAX(cost) c1,MIN(bp) b0,MAX(bp) b1,COUNT(*) n FROM sire_prices '
                              'WHERE day>=? AND sire_id IN (%s) GROUP BY sire_id' % q, [since] + list(ids)):
        out[r['sire_id']] = dict(cost_lo=r['c0'], cost_hi=r['c1'], bp_lo=r['b0'], bp_hi=r['b1'], days=r['n'])
    return out


def watch_list(store):
    ensure(store.db)
    rows = [dict(r) for r in store.db.execute('SELECT * FROM sire_watch ORDER BY name')]
    rg = ranges(store, [r['sire_id'] for r in rows])
    for r in rows:
        r.update(rg.get(r['sire_id'], {}))
        p = store.db.execute('SELECT y2,y3 FROM sire_prices WHERE sire_id=? ORDER BY day DESC LIMIT 1', (r['sire_id'],)).fetchone()
        r['y2'], r['y3'] = (bool(p['y2']), bool(p['y3'])) if p else (False, False)
        r['hit'] = hit(r, r['last_cost'], r['last_bp'])
    return rows


def hit(w, cost, bp):
    """Which limits the price is at or below: [] , ['cost'], ['bp'] or both."""
    h = []
    if w.get('max_cost') is not None and cost is not None and cost <= w['max_cost']:
        h.append('cost')
    if w.get('max_bp') is not None and bp is not None and bp <= w['max_bp']:
        h.append('bp')
    return h


def watch_set(store, sire_id, name, max_cost, max_bp):
    ensure(store.db)
    mc = int(max_cost) if str(max_cost or '').strip().isdigit() else None
    mb = int(max_bp) if str(max_bp or '').strip().isdigit() else None
    cur = store.db.execute('SELECT 1 FROM sire_watch WHERE sire_id=?', (sire_id,)).fetchone()
    if cur:
        store.db.execute('UPDATE sire_watch SET max_cost=?, max_bp=?, alert_key=NULL WHERE sire_id=?', (mc, mb, sire_id))
    else:
        store.db.execute('INSERT INTO sire_watch(sire_id,name,max_cost,max_bp,added) VALUES(?,?,?,?,?)', (sire_id, name, mc, mb, today()))
    store.db.commit()


def watch_delete(store, sire_id):
    ensure(store.db)
    store.db.execute('DELETE FROM sire_watch WHERE sire_id=?', (sire_id,))
    store.db.commit()


def watched_ids(store):
    ensure(store.db)
    return {r['sire_id']: dict(max_cost=r['max_cost'], max_bp=r['max_bp']) for r in store.db.execute('SELECT * FROM sire_watch')}


WATCH_PAGES = 8      # a price check reads at most this many pages of 100 sires, one after the other


def read_table(fetch=fetch_page, pages=WATCH_PAGES):
    """One read of the Find Sires table, cheapest credits first. -> (rows, more). No page is opened for a single sire."""
    return collect('a', 'creditcost', '', 'all', pages, fetch)


def found_from(rows, watch, more=False):
    """Matches the watchlist against the table rows. -> {sire_id: row, or a message when it is not in the pages read}."""
    by = {r['sire_id']: r for r in rows}
    out = {}
    for w in watch:
        r = by.get(w['sire_id'])
        out[w['sire_id']] = r if r else ('Not in the first %d sires of the list (priced out of range?)' % len(rows) if more else None)
    return out


def apply_check(store, watch, found):
    """Stores the new prices and works out which limits were newly crossed. -> list of watch rows with 'hit' and 'new'."""
    ensure(store.db)
    good = [r for r in found.values() if isinstance(r, dict)]
    snapshot(store, good)
    out = []
    for w in watch:
        r = found.get(w['sire_id'])
        if isinstance(r, dict):
            h = hit(w, r['cost'], r['bp'])
            key = '%s:%s:%s' % ('+'.join(h), r['cost'], r['bp']) if h else None
            new = bool(h) and key != w.get('alert_key')
            store.db.execute('UPDATE sire_watch SET last_cost=?, last_bp=?, last_checked=?, alert_key=? WHERE sire_id=?',
                             (r['cost'], r['bp'], time.strftime('%Y-%m-%d %H:%M'), key, w['sire_id']))
            out.append(dict(sire_id=w['sire_id'], name=w['name'], cost=r['cost'], bp=r['bp'], hit=h, new=new))
        else:
            out.append(dict(sire_id=w['sire_id'], name=w['name'], error=r if isinstance(r, str) else 'Not found in the site list', hit=[], new=False))
    store.db.commit()
    return out
