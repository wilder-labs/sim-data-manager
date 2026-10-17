"""Claim finder: reads the public eHorseRacing race schedule for the claiming races of the next days and lists
every entered horse with its last race from the local database."""
import re, html, datetime, urllib.request
from concurrent.futures import ThreadPoolExecutor
from . import search as S
from .viewer import site_to_hid
from . import sire as SIRE

BASE = 'http://ehorseracing.com/'
PAGE = BASE + 'includes/raceschedules/lookentries_inline.asp'
PAGE_SIZE = 25
# every track number the schedule knows; the database's own track list is added to it
TRACKS = [15, 69, 1, 71, 30, 14, 25, 16, 26, 88, 6, 95, 18, 5, 41, 23, 31, 10, 2, 7, 20, 19, 3, 21, 47, 11, 17, 22, 8, 79, 9, 4,
          28, 12, 37, 13, 33, 62, 59, 49, 35, 57, 34, 36, 93, 65, 58, 63, 66, 52, 54, 89, 96, 55, 84, 81, 67, 53, 82, 51, 60, 91, 50, 94, 90]
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

_HEAD = re.compile(r"<div style='font-size:14px;[^>]*>([^<]*?)\s*\((\w+)\)</div><div[^>]*><B>([^<]*)</b> -- ([^<]*?)</b> \| Race ID : <b>(\d+)</b>")
_TYPE = re.compile(r"<div style='font-weight:bold;'>([^<]*?)\s*-\s*Purse : \$([\d,]+)</div>")
_TAG = re.compile(r"<b>Tag : </b>\s*\$([\d,]+)")
_TAGBP = re.compile(r"<b>Tag : </b>\s*\$[\d,]+\s*\((\d+)\s*BP\)")
_FURL = re.compile(r"font-weight:bold;'>\s*([\d.]+) Furlongs?")
_METERS = re.compile(r"~\s*(\d+) Meters")
_SURF = re.compile(r"align=right style='color:#333333; font-weight:bold;'><b>([^<]*)</b>")
_ROW = re.compile(r'<tr class="altrow\d"')
_HORSE = re.compile(r"lf\('ff','h=(\d+)'\)\">\s*<nobr>(.*?)</nobr>", re.S)
_PGM = re.compile(r'font-weight: bold; color:\s*#[0-9a-fA-F]+;">\s*([^<\s]+)\s*</div>')
_OWNER = re.compile(r'lookat=([^"&]+)"')
_GREY = re.compile(r"<div style=['\"]?color:#666666; font-weight:normal;['\"]?>(.*?)</div>", re.S)
_RESTR = re.compile(r"Restricted to ([^<]*)")


def _clean(t):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]*>', ' ', t or ''))).strip()


def age_label(restr):
    """'Restricted to 3 Year Olds and Older, Fillies and Mares Only' -> '3yo+ F&M'."""
    m = re.search(r'(\d+) Year Olds?( and Older| and Up)?', restr or '')
    if not m:
        return ''
    out = m.group(1) + 'yo' + ('+' if m.group(2) else '')
    if re.search(r'Fillies', restr):
        out += ' F&M'
    elif re.search(r'Colts|Geldings|Males', restr):
        out += ' C&G'
    return out


_JOCKEY = re.compile(r'<td align=left>\s*(.*?)\s*</td>', re.S)


def _num(s):
    return int(s.replace(',', '')) if s else None


def parse(page):
    """-> list of races: dict(raceid, track, code, date 'YYYY-MM-DD', time 'h:mm AM', type, purse, claim, dist, surf, horses=[...])."""
    heads = list(_HEAD.finditer(page))
    out = []
    for i, m in enumerate(heads):
        blk = page[m.start(): heads[i + 1].start() if i + 1 < len(heads) else len(page)]
        try:
            day = datetime.datetime.strptime(m.group(3).strip(), '%A, %B %d, %Y').date().isoformat()
        except ValueError:
            continue
        t = re.match(r'(\d+:\d+)(?::\d+)?\s*([AP]M)', m.group(4).strip())
        ty, tag, tbp = _TYPE.search(blk), _TAG.search(blk), _TAGBP.search(blk)
        f, mt, sf = _FURL.search(blk), _METERS.search(blk), _SURF.search(blk)
        dist = float(f.group(1)) if f else (round(int(mt.group(1)) / 201.168 * 2) / 2 if mt else None)
        gr, rs = _GREY.search(blk), _RESTR.search(blk)
        restr = _clean(rs.group(1)) if rs else ''
        cond = ' '.join(x for x in (_clean(gr.group(1)) if gr else '', restr and 'Restricted to ' + restr) if x)
        horses = []
        for r in _ROW.split(blk)[1:]:
            h = _HORSE.search(r)
            if not h:
                continue
            p, o = _PGM.search(r), _OWNER.search(r)
            horses.append(dict(pgm=p.group(1) if p else '', site_id=h.group(1), hname=html.unescape(h.group(2)).strip(),
                               owner=html.unescape(o.group(1)) if o else ''))
        out.append(dict(raceid=int(m.group(5)), track=html.unescape(m.group(1)).strip(), code=m.group(2), date=day,
                        time=(t.group(1) + ' ' + t.group(2)) if t else '', type=ty.group(1).strip() if ty else '',
                        purse=_num(ty.group(2)) if ty else None, claim=_num(tag.group(1)) if tag else None, claim_bp=int(tbp.group(1)) if tbp else None,
                        rage=age_label(restr), rcond=cond,
                        dist=dist, surf=sf.group(1).strip() if sf else '', horses=horses))
    return out


def fetch_page(d_from, d_to, start, tracks, timeout=40):
    q = ('tracks=%sx&fromdate=%s&todate=%s&track=&startme=%d&maxi=%d&trackpick=a&showwhat=all&racetypes=c'
         % ('x'.join(str(t) for t in tracks), d_from, d_to, start, PAGE_SIZE))
    req = urllib.request.Request(PAGE + '?' + q, headers={'User-Agent': UA, 'Referer': BASE + 'raceschedules.asp',
                                                          'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9'})
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode('latin-1', 'replace')
        except Exception as e:
            last = e
    raise last


def collect(d_from, d_to, tracks=None, fetch=fetch_page, workers=2, max_pages=60):
    """Every claiming race carded between the two dates (ISO strings), newest page batches fetched a few at a time."""
    tracks = sorted(set(tracks or []) | set(TRACKS))
    races, seen, start = [], set(), 0
    with ThreadPoolExecutor(workers) as ex:
        while start // PAGE_SIZE < max_pages:
            starts = [start + k * PAGE_SIZE for k in range(workers)]
            pages = list(ex.map(lambda s: parse(fetch(d_from, d_to, s, tracks)), starts))
            done = False
            for p in pages:
                for r in p:
                    if r['raceid'] not in seen:
                        seen.add(r['raceid'])
                        races.append(r)
                if len(p) < PAGE_SIZE:
                    done = True
            if done:
                break
            start += workers * PAGE_SIZE
    return races


def build(store, races):
    """One row per entered horse: the race it is entered in, plus its last race on record."""
    db = store.db
    aw, ad = store.anchor()
    rows, tracks = [], {}
    for r in sorted(races, key=lambda r: (r['date'], r['time'][-2:], _t24(r['time']), r['code'], r['raceid'])):
        T = tracks.setdefault(r['code'], dict(code=r['code'], track=r['track'], races=0, horses=0, lo=None, hi=None))
        T['races'] += 1
        c = r['claim']
        if c:
            T['lo'] = c if T['lo'] is None else min(T['lo'], c)
            T['hi'] = c if T['hi'] is None else max(T['hi'], c)
        for h in r['horses']:
            T['horses'] += 1
            hid = site_to_hid(h['site_id'])
            last = None
            if hid is not None:
                got = S._decorate(store, db.execute(S.SELECT + ' WHERE e.hid = ? ORDER BY e.racenum DESC LIMIT 1', (hid,)).fetchall())
                last = got[0] if got else None
            row = dict(last or {})
            cr = db.execute('SELECT COUNT(*), COALESCE(SUM(fin = 1), 0) FROM entries WHERE hid = ?', (hid,)).fetchone() if hid is not None else (0, 0)
            row.update(starts=cr[0], wins=cr[1])
            c = SIRE.cached(store, hid) if hid is not None else None
            row['sire'] = (c or {}).get('sire') or ''
            row.update(run_date=r['date'], run_time=r['time'], run_track=r['code'], claim=r['claim'], run_type=r['type'], run_age=r.get('rage', ''), run_cond=r.get('rcond', ''),
                       run_dist=r['dist'], run_surf=r['surf'], run_raceid=r['raceid'], pgm=h['pgm'], site_id=h['site_id'], claim_bp=r.get('claim_bp'),
                       entered_by=h['owner'], debut=last is None)
            if last is None:
                row.update(hname=h['hname'], hid=hid, racenum=None)
            else:
                row['days'] = (datetime.date.fromisoformat(r['date']) - datetime.date.fromisoformat(last['date'])).days
            rows.append(row)
    return rows, sorted(tracks.values(), key=lambda t: t['code'])


def _t24(t):
    m = re.match(r'(\d+):(\d+)', t or '')
    return ((int(m.group(1)) % 12) * 60 + int(m.group(2))) if m else 0


def find(store, d_from, d_to, fetch=fetch_page):
    races = collect(d_from, d_to, tracks=[n for n in store.tracks()], fetch=fetch)
    rows, tracks = build(store, races)
    return dict(kind='claims', candidates=[], rows=rows, summary=None, truncated=False,
                claims=dict(first_date=d_from, last_date=d_to, races=len(races), horses=len(rows),
                            debuts=sum(1 for r in rows if r['debut']), tracks=tracks))
