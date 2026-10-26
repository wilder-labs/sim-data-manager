"""Residency portal: upload a roster, pick a date range and a scoring matrix, get a leaderboard and export files.

Everything comes from the local database and the roster file. Nothing here contacts the sim site or any other server.

Routes (wired in server.py):
  GET  /residency                    the page
  GET  /api/residency/status         can this copy accept uploads, and the date range of the data
  GET  /api/residency/roster?code=   a saved roster (names only) and its saved scoring rules
  GET  /api/residency/board?...      the leaderboard as JSON
  GET  /api/residency/export?...     the same as a file: kind=members|stables (CSV) or kind=json
  POST /api/residency/upload         build and save a roster from an uploaded file
  POST /api/residency/rules          save the scoring rules for a residency

Writing (upload, rules, public) is allowed on a local copy. On a hosted copy (SDM_HOSTED=1) only the admin key in the environment
variable SDM_RESIDENCY_KEY can write or open a private residency, sent in the header X-Residency-Key. With no admin key set, a hosted
copy refuses writes.
"""
import base64, binascii, csv, datetime, hashlib, hmac, html, io, json, math, os, re, secrets, threading, time
from . import residency as RS

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
MAX_UPLOAD_JSON = 4 * 1024 * 1024          # request body: a 2 MB file becomes about 2.8 MB of base64
MAX_CODES = 25                             # residencies one copy will hold
MAX_WRITES_PER_HOUR = 30
MAX_BAD_KEYS_PER_HOUR = 10
KDF_ROUNDS = 100000
MAX_POSITIONS = 10
CLASS_OF = {'Stk': 'stk', 'G3': 'g3', 'G2': 'g2', 'GI': 'g1', 'G1': 'g1'}
STAKES_SQL = "('Stk','GI','G1','G2','G3')"
GRADED_SQL = "('GI','G1','G2','G3')"
DEFAULT_RULE = dict(points=[10, 8, 6, 4, 3, 0, 0], bonus=dict(stk=0, g3=0, g2=0, g1=0), bonus_on='place')
_writes, _bad, _wlock = [], [], threading.Lock()


class PortalError(Exception):
    def __init__(self, msg, code=400):
        Exception.__init__(self, msg)
        self.code = code


# ---------------------------------------------------------------- rules and dates
def _num(v, lo=0, hi=1000):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise PortalError('Scoring values must be numbers from %d to %d.' % (lo, hi))
    if not (lo <= x <= hi) or x != x:
        raise PortalError('Scoring values must be numbers from %d to %d.' % (lo, hi))
    return int(x) if x == int(x) else round(x, 2)


def parse_rule(d):
    """A scoring rule from user input: points for 1st, 2nd, ... and bonus points for stakes races."""
    d = d if isinstance(d, dict) else {}
    pts = d.get('points', DEFAULT_RULE['points'])
    if isinstance(pts, str):
        pts = [x for x in pts.split(',') if x.strip() != '']
    if not isinstance(pts, list) or not (1 <= len(pts) <= MAX_POSITIONS):
        raise PortalError('Give points for 1st place and up to %d places.' % MAX_POSITIONS)
    pts = [_num(x) for x in pts]
    b = d.get('bonus', {})
    if isinstance(b, str):                          # 'stk:2,g3:3'
        b = dict(x.split(':', 1) for x in b.split(',') if ':' in x)
    b = b if isinstance(b, dict) else {}
    bonus = {k: _num(b.get(k, 0)) for k in ('stk', 'g3', 'g2', 'g1')}
    on = d.get('bonus_on', 'place')
    if on not in ('place', 'win'):
        raise PortalError('Bonus applies to "place" or "win".')
    return dict(points=pts, bonus=bonus, bonus_on=on)


def _date(s, name):
    try:
        return datetime.date.fromisoformat(str(s))
    except ValueError:
        raise PortalError('%s must be a date like 2026-10-12.' % name)


def monday_of(d):
    return d - datetime.timedelta(days=d.weekday())


def week_of(store, d):
    aw, ad = store.anchor()
    return aw + (d - ad).days


def date_of(store, week):
    aw, ad = store.anchor()
    return ad + datetime.timedelta(days=int(week) - aw)


def data_range(store):
    lo, hi = store.db.execute('SELECT MIN(week), MAX(week) FROM weeks').fetchone()
    if not lo:
        return None, None
    return date_of(store, lo).isoformat(), date_of(store, hi).isoformat()


# ---------------------------------------------------------------- the leaderboard
def _zero():
    # finishes: how many 1st, 2nd ... 7th, then 8th or worse; nofin: started but no finish place recorded
    return dict(starts=0, wins=0, seconds=0, thirds=0, points=0, bonus=0, finishes=[0] * 8, nofin=0)


def _add(t, f):
    for k in ('starts', 'wins', 'seconds', 'thirds', 'points', 'bonus', 'nofin'):
        t[k] += f[k]
    for i in range(8):
        t['finishes'][i] += f['finishes'][i]


def compute(store, roster, d_from, d_to, rule=None, age='all', races='all'):
    """Leaderboard for a roster over a date range. Rows are rolled up stable -> master stable -> member.
    Entries are counted for a stable only on days it belonged to a member (the Joined and Left dates)."""
    rule = parse_rule(rule or DEFAULT_RULE)
    d0, d1 = _date(d_from, 'From'), _date(d_to, 'To')
    if d1 < d0:
        raise PortalError('The To date is before the From date.')
    if (d1 - d0).days > 400:
        raise PortalError('Pick a range of 400 days or less.')
    if age not in ('all', '2', '3'):
        raise PortalError('Age must be all, 2 or 3.')
    if races not in ('all', 'stakes', 'graded'):
        raise PortalError('Races must be all, stakes or graded.')
    w0, w1 = week_of(store, d0), week_of(store, d1)
    db = store.db
    stables = [dict(s, member_key=s.get('member_key') or RS.norm_key(s['member']), master_key=s.get('master_key') or RS.norm_key(s['master']))
               for s in roster.get('stables', [])]
    keys = {s['key'] for s in stables}
    ids_by_key = {}
    for row in db.execute('SELECT id, oname, o15, o12, o10 FROM owners'):
        for v in row[1:]:
            k = RS.norm_key(v or '')
            if k in keys:
                ids_by_key.setdefault(k, set()).add(row[0])
    key_of_id = {}
    for k, ids in ids_by_key.items():
        for i in ids:
            key_of_id.setdefault(i, set()).add(k)
    where = ['e.week BETWEEN ? AND ?']
    if age == '2':
        where.append("r.age LIKE '_-2'")
    elif age == '3':
        where.append("r.age LIKE '_-3'")
    if races == 'stakes':
        where.append('r.restr IN ' + STAKES_SQL)
    elif races == 'graded':
        where.append('r.restr IN ' + GRADED_SQL)
    ids = sorted(key_of_id)
    # one record per roster line; a stable listed for two members at different times has two lines
    lines = {}
    for s in stables:
        lines.setdefault(s['key'], []).append(s)
    acc = {id(s): _zero() for s in stables}
    pts = rule['points']
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        sql = ('SELECT e.owner_id, e.week, e.fin, r.restr FROM entries e JOIN races r ON r.racenum = e.racenum WHERE e.owner_id IN (%s) AND %s'
               % (','.join('?' * len(part)), ' AND '.join(where)))
        for oid, wk, fin, restr in db.execute(sql, part + [w0, w1]):
            day = date_of(store, wk).isoformat()
            for k in key_of_id[oid]:
                line = next((s for s in lines[k] if (not s['joined'] or s['joined'] <= day) and (not s['left'] or day <= s['left'])), None)
                if line is None:
                    continue
                a = acc[id(line)]
                a['starts'] += 1
                if not fin or fin < 1:
                    a['nofin'] += 1
                    continue
                a['finishes'][min(fin, 8) - 1] += 1
                if fin == 1: a['wins'] += 1
                elif fin == 2: a['seconds'] += 1
                elif fin == 3: a['thirds'] += 1
                if fin <= len(pts):
                    a['points'] += pts[fin - 1]
                    cl = CLASS_OF.get(restr)
                    if cl and (rule['bonus_on'] == 'place' or fin == 1):
                        a['bonus'] += rule['bonus'][cl]
    members = {}
    for s in stables:
        m = members.setdefault(s['member_key'], dict(name=s['member'], masters={}, **_zero()))
        ms = m['masters'].setdefault(s['master_key'], dict(name=s['master'], stables=[], **_zero()))
        a = acc[id(s)]
        ms['stables'].append(dict(name=s['name'], matched=s['key'] in ids_by_key, **a))
        _add(ms, a)
        _add(m, a)
    out = []
    for m in members.values():
        mm = [dict(name=x['name'], **{k: x[k] for k in _zero()}, total=x['points'] + x['bonus'],
                   stables=sorted([dict(st, total=st['points'] + st['bonus']) for st in x['stables']],
                                  key=lambda t: (-t['total'], -t['wins'], t['name'])))
              for x in m['masters'].values()]
        mm.sort(key=lambda t: (-t['total'], -t['wins'], t['name']))
        out.append(dict(name=m['name'], **{k: m[k] for k in _zero()}, total=m['points'] + m['bonus'], masters=mm))
    out.sort(key=lambda t: (-t['total'], -t['wins'], t['name'].lower()))
    prev, pos = None, 0
    for i, m in enumerate(out):
        if m['total'] != prev:
            pos, prev = i + 1, m['total']
        m['rank'] = pos
    for m in out:
        m['tied'] = sum(1 for x in out if x['rank'] == m['rank']) > 1
    first, last = data_range(store)
    unmatched = sorted({s['name'] for s in stables if s['key'] not in ids_by_key})
    quiet = sorted({s['name'] for s in stables if s['key'] in ids_by_key and not acc[id(s)]['starts']})
    return dict(format=1, code=roster.get('code'), residency=roster.get('residency', ''),
                period=dict(**{'from': d0.isoformat(), 'to': d1.isoformat()}, days=(d1 - d0).days + 1),
                filters=dict(age=age, races=races), rule=rule,
                generated_at=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                data=dict(first_date=first, last_date=last),
                counts=dict(members=len(out), stables=len(stables), unmatched=len(unmatched), no_races=len(quiet)),
                unmatched=unmatched, no_races=quiet, members=out)


FIN_HEAD = ['1st', '2nd', '3rd', '4th', '5th', '6th', '7th', '8th_or_worse']


def to_csv(board, kind):
    b = io.StringIO()
    w = csv.writer(b)
    if kind in ('finishes', 'finishes_members'):          # plain finish counts, no scoring
        by_stable = kind == 'finishes'
        w.writerow(['member'] + (['master_stable', 'stable'] if by_stable else ['masters', 'stables']) + ['starts'] + FIN_HEAD + ['no_finish_recorded'])
        for m in sorted(board['members'], key=lambda t: t['name'].lower()):
            if by_stable:
                for x in m['masters']:
                    for s in x['stables']:
                        w.writerow([m['name'], x['name'], s['name'], s['starts']] + s['finishes'] + [s['nofin']])
            else:
                w.writerow([m['name'], len(m['masters']), sum(len(x['stables']) for x in m['masters']), m['starts']] + m['finishes'] + [m['nofin']])
    elif kind == 'stables':
        w.writerow(['rank', 'member', 'master_stable', 'stable', 'starts', 'wins', 'seconds', 'thirds', 'place_points', 'bonus_points', 'total'])
        for m in board['members']:
            for x in m['masters']:
                for s in x['stables']:
                    w.writerow([m['rank'], m['name'], x['name'], s['name'], s['starts'], s['wins'], s['seconds'], s['thirds'], s['points'], s['bonus'], s['total']])
    else:
        w.writerow(['rank', 'member', 'masters', 'stables', 'starts', 'wins', 'seconds', 'thirds', 'place_points', 'bonus_points', 'total'])
        for m in board['members']:
            w.writerow([m['rank'], m['name'], len(m['masters']), sum(len(x['stables']) for x in m['masters']), m['starts'], m['wins'], m['seconds'], m['thirds'], m['points'], m['bonus'], m['total']])
    return b.getvalue()



# ---------------------------------------------------------------- polls: one rating formula for a date range
CLAIM_W = dict(win=30, itm=25, fin=30, vol=10, form=5)
ALL_W = dict(win=20, itm=15, fin=20, qual=30, vol=10, form=5)
POLL_CFG = dict(claiming=dict(top=5, also=5, min=5, vcap=25, w=CLAIM_W), overall=dict(top=10, also=5, min=8, vcap=60, w=ALL_W))
PART_LABELS = dict(win='Wins', itm='In the money', fin='Finish quality', qual='Race quality', vol='Races run', form='Recent form')
SHRINK_K = 8


def is_claiming(restr):
    r = (restr or '').strip()
    return r in ('MC', 'Clm') or r.isdigit() or (r[:1] == 'C' and len(r) == 3)


def _clip(x):
    return max(0.0, min(1.0, x))


def _tally(store, roster, d_from, d_to, age='all'):
    """Per member: starts, finish counts (1st..7th, 8th or worse), earnings, claiming-only counts, stakes counts.
    Entries count for a stable only on days it belonged to the member (Joined and Left dates)."""
    d0, d1 = _date(d_from, 'From'), _date(d_to, 'To')
    w0, w1 = week_of(store, d0), week_of(store, d1)
    db = store.db
    stables = [dict(s, member_key=s.get('member_key') or RS.norm_key(s['member'])) for s in roster.get('stables', [])]
    keys = {s['key'] for s in stables}
    key_of_id = {}
    for row in db.execute('SELECT id, oname, o15, o12, o10 FROM owners'):
        for v in row[1:]:
            k = RS.norm_key(v or '')
            if k in keys:
                key_of_id.setdefault(row[0], set()).add(k)
    lines = {}
    for s in stables:
        lines.setdefault(s['key'], []).append(s)
    out = {}
    for s in stables:
        out.setdefault(s['member_key'], dict(name=s['member'], stables=set(), starts=0, fin=[0] * 8, nofin=0, earn=0.0,
                                             cs=0, cfin=[0] * 8, sw=0, gw=0, spl=0))['stables'].add(s['key'])
    where = ['e.week BETWEEN ? AND ?']
    if age == '2':
        where.append("r.age LIKE '_-2'")
    elif age == '3':
        where.append("r.age LIKE '_-3'")
    ids = sorted(key_of_id)
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        sql = ('SELECT e.owner_id, e.week, e.fin, r.restr, e.earn FROM entries e JOIN races r ON r.racenum = e.racenum WHERE e.owner_id IN (%s) AND %s'
               % (','.join('?' * len(part)), ' AND '.join(where)))
        for oid, wk, fin, restr, earn in db.execute(sql, part + [w0, w1]):
            day = date_of(store, wk).isoformat()
            seen = set()
            for k in key_of_id[oid]:
                line = next((x for x in lines[k] if (not x['joined'] or x['joined'] <= day) and (not x['left'] or day <= x['left'])), None)
                if line is None or line['member_key'] in seen:
                    continue
                seen.add(line['member_key'])
                a = out[line['member_key']]
                a['starts'] += 1
                a['earn'] += earn or 0
                claim = is_claiming(restr)
                if claim:
                    a['cs'] += 1
                if not fin or fin < 1:
                    a['nofin'] += 1
                    continue
                idx = min(fin, 8) - 1
                a['fin'][idx] += 1
                if claim:
                    a['cfin'][idx] += 1
                if restr in ('Stk', 'GI', 'G1', 'G2', 'G3'):
                    if fin == 1:
                        a['sw'] += 1
                        if restr != 'Stk':
                            a['gw'] += 1
                    elif fin <= 3:
                        a['spl'] += 1
    return out


def _rate(rows, mode, days):
    """Adds score (0-100) and parts (0-100 each) to every row. Each row: n, w, p, h, sw, gw, sp, ds, dw, dp, dh."""
    cfg = POLL_CFG[mode]
    vcap = max(6, cfg['vcap'] * min(1, days / 28.0))
    total = sum(r['n'] for r in rows) or 1
    aw = sum(r['w'] for r in rows) / total
    ai = sum(r['w'] + r['p'] + r['h'] for r in rows) / total
    af = sum(3 * r['w'] + 2 * r['p'] + r['h'] for r in rows) / total
    for r in rows:
        n = r['n']
        sh = lambda x, pr: (x + SHRINK_K * pr) / (n + SHRINK_K)
        c = dict(win=_clip(sh(r['w'], aw) / 0.32), itm=_clip(sh(r['w'] + r['p'] + r['h'], ai) / 0.70),
                 fin=_clip(sh(3 * r['w'] + 2 * r['p'] + r['h'], af) / 1.5),
                 vol=min(1.0, math.log(1 + n) / math.log(1 + vcap)),
                 form=_clip(((3 * r['dw'] + 2 * r['dp'] + r['dh']) + 2 * 0.7) / (r['ds'] + 2) / 1.5),
                 qual=_clip((r['sw'] + 2 * r['gw'] + 0.4 * r['sp']) / 6.0))
        r['parts'] = {k: int(round(100 * c[k])) for k in cfg['w']}
        r['score'] = round(100 * sum(cfg['w'][k] / 100.0 * c[k] for k in cfg['w']), 1)
    return rows


def _rows(tally, mode, recent=None):
    out = []
    for k, a in tally.items():
        c = mode == 'claiming'
        n = a['cs'] if c else a['starts']
        f = a['cfin'] if c else a['fin']
        row = dict(key=k, name=a['name'], stables=len(a['stables']), n=n, w=f[0], p=f[1], h=f[2], sw=a['sw'], gw=a['gw'], sp=a['spl'],
                   earn=int(round(a['earn'])), ds=0, dw=0, dp=0, dh=0)
        if recent and k in recent:
            b = recent[k]
            rf = b['cfin'] if c else b['fin']
            row.update(ds=b['cs'] if c else b['starts'], dw=rf[0], dp=rf[1], dh=rf[2])
        out.append(row)
    return out


def _order(rows):
    rows.sort(key=lambda r: (-r['score'], -r['w'], r['name'].lower()))


def polls(store, roster, d_from, d_to, age='all'):
    """The claiming poll and the overall poll for a date range. Arrows compare with the poll for the range just before,
    of the same length. Recent form is the last quarter of the range."""
    d0, d1 = _date(d_from, 'From'), _date(d_to, 'To')
    if d1 < d0:
        raise PortalError('The To date is before the From date.')
    days = (d1 - d0).days + 1
    if days > 400:
        raise PortalError('Pick a range of 400 days or less.')
    if age not in ('all', '2', '3'):
        raise PortalError('Age must be all, 2 or 3.')
    p1 = d0 - datetime.timedelta(days=1)
    p0 = p1 - datetime.timedelta(days=days - 1)
    q = max(1, int(round(days / 4.0)))
    r0 = d1 - datetime.timedelta(days=q - 1)
    cur = _tally(store, roster, d0.isoformat(), d1.isoformat(), age)
    prev = _tally(store, roster, p0.isoformat(), p1.isoformat(), age)
    rec = _tally(store, roster, r0.isoformat(), d1.isoformat(), age)
    res = dict(format=1, code=roster.get('code'), residency=roster.get('residency', ''),
               period=dict(**{'from': d0.isoformat(), 'to': d1.isoformat()}, days=days),
               previous=dict(**{'from': p0.isoformat(), 'to': p1.isoformat()}), recent=dict(**{'from': r0.isoformat(), 'to': d1.isoformat()}),
               filters=dict(age=age), generated_at=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
               labels=PART_LABELS, weights=dict(claiming=CLAIM_W, overall=ALL_W))
    for mode, cfg in POLL_CFG.items():
        mn = max(3, int(round(cfg['min'] * min(1, days / 28.0))))
        now = _rate([r for r in _rows(cur, mode, rec) if r['n'] >= mn], mode, days)
        _order(now)
        before = _rate([r for r in _rows(prev, mode) if r['n'] >= mn], mode, days)
        _order(before)
        prank = {r['key']: i + 1 for i, r in enumerate(before)}
        rows = []
        for i, r in enumerate(now):
            pv = prank.get(r['key'])
            rows.append(dict(rank=i + 1, previous_rank=pv, move=(pv - (i + 1)) if pv else None, name=r['name'], stables=r['stables'],
                             starts=r['n'], wins=r['w'], seconds=r['p'], thirds=r['h'],
                             win_pct=round(100.0 * r['w'] / r['n'], 1) if r['n'] else 0.0,
                             itm_pct=round(100.0 * (r['w'] + r['p'] + r['h']) / r['n'], 1) if r['n'] else 0.0,
                             earnings=r['earn'], rating=r['score'], parts=r['parts']))
        res[mode] = dict(top=cfg['top'], also=cfg['also'], min_starts=mn, field=len(rows), rows=rows)
    return res


def polls_csv(data, mode):
    b = io.StringIO()
    w = csv.writer(b)
    w.writerow(['rank', 'previous_rank', 'move', 'member', 'stables', 'starts', 'wins', 'seconds', 'thirds', 'win_pct', 'itm_pct', 'earnings', 'rating'])
    for r in data[mode]['rows']:
        w.writerow([r['rank'], r['previous_rank'] or '', '' if r['move'] is None else r['move'], r['name'], r['stables'], r['starts'],
                    r['wins'], r['seconds'], r['thirds'], r['win_pct'], r['itm_pct'], r['earnings'], r['rating']])
    return b.getvalue()



# ---------------------------------------------------------------- embeddable snapshots (plain HTML + CSS, no scripts)
SKINS = ('plain',)
_X = html.escape


def _skin_css(skin, sid):
    r = '.rzb-' + sid
    base = {
        'plain': '--bg:#ffffff;--bg2:#f2f6f4;--ink:#10211b;--mute:#566a62;--line:#d3dcd8;--led:#14573f;--up:#1a7f4b;--dn:#c8362b;--w:#c8362b;--p:#8d9aa1;--s:#2a5bd7;--font:"Barlow Condensed","Arial Narrow",Arial,sans-serif;--body:system-ui,-apple-system,"Segoe UI",sans-serif;--disp-w:700;',
    }[skin]
    dark = {'plain': '--bg:#111e19;--bg2:#16271f;--ink:#e9f0eb;--mute:#8ea69b;--line:#233830;--led:#ffb020;--up:#4cc38a;--dn:#f0675a;--w:#f0675a;--p:#c3cfd4;--s:#6b98ff;'}.get(skin)
    css = (r + '{' + base + 'background:var(--bg);color:var(--ink);font:15px/1.45 var(--body);border:1px solid var(--line);border-radius:6px;overflow:hidden;max-width:960px;margin:0 auto;box-sizing:border-box}'
           + r + ' *{box-sizing:border-box}'
           + r + ' .h{padding:14px 16px;border-bottom:1px solid var(--line);background:var(--bg2)}'
           + r + ' .t{font:var(--disp-w) 28px/1.05 var(--font);text-transform:uppercase;letter-spacing:.04em}'
           + r + ' .s{color:var(--mute);font-size:13px;margin-top:3px}'
           + r + ' table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}'
           + r + ' th{font:600 12px var(--font);letter-spacing:.1em;text-transform:uppercase;color:var(--mute);text-align:right;padding:9px 10px;border-bottom:2px solid var(--line);white-space:nowrap}'
           + r + ' td{padding:10px;text-align:right;border-bottom:1px solid var(--line);font:600 17px var(--font);white-space:nowrap}'
           + r + ' th:first-child,' + r + ' td:first-child,' + r + ' .l{text-align:left}'
           + r + ' .rk{font:var(--disp-w) 24px/1 var(--font);color:var(--led);display:inline-block;min-width:30px}'
           + r + ' .nm{font:var(--disp-w) 19px var(--font);text-transform:uppercase;letter-spacing:.03em;white-space:normal}'
           + r + ' .w{color:var(--w)}' + r + ' .p{color:var(--p)}' + r + ' .sh{color:var(--s)}'
           + r + ' .wrap{overflow-x:auto}'
           + r + ' details{border-bottom:1px solid var(--line)}' + r + ' summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:64px minmax(0,1fr) auto;gap:4px 14px;align-items:center;padding:12px 16px}'
           + r + ' summary::-webkit-details-marker{display:none}' + r + ' summary:hover{background:var(--bg2)}'
           + r + ' .n{font:var(--disp-w) 46px/.9 var(--font);color:var(--led);text-align:center}'
           + r + ' .mv{display:block;font:700 13px var(--font);text-align:center;letter-spacing:.05em}' + r + ' .up{color:var(--up)}' + r + ' .dn{color:var(--dn)}' + r + ' .eq,' + r + ' .nw{color:var(--mute)}'
           + r + ' .who{font:var(--disp-w) 25px/1.05 var(--font);text-transform:uppercase;letter-spacing:.03em}'
           + r + ' .rec{color:var(--mute);font-size:13px;margin-top:2px}'
           + r + ' .rt{text-align:right;font:var(--disp-w) 32px/1 var(--font)}' + r + ' .rt small{display:block;font:600 11px var(--font);letter-spacing:.14em;color:var(--mute)}'
           + r + ' .parts{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px 16px;padding:2px 16px 14px 92px}'
           + r + ' .parts div{font:600 11px var(--font);letter-spacing:.1em;text-transform:uppercase;color:var(--mute);display:flex;justify-content:space-between}'
           + r + ' .m{height:6px;background:var(--line);border-radius:2px;overflow:hidden;grid-column:1/-1;margin-top:-4px}' + r + ' .m i{display:block;height:100%;background:var(--led)}'
           + r + ' .pt{display:flex;flex-direction:column;gap:3px}'
           + r + ' .v{padding:10px 16px 4px;font:600 12px var(--font);letter-spacing:.14em;text-transform:uppercase;color:var(--mute)}'
           + r + ' .f{padding:10px 16px;font-size:12px;color:var(--mute);border-top:1px solid var(--line)}'
           + '@media (max-width:640px){' + r + ' summary{grid-template-columns:48px minmax(0,1fr)}' + r + ' .rt{grid-column:2;text-align:left}' + r + ' .n{font-size:34px}' + r + ' .parts{padding-left:16px}}')
    if dark:
        css += '@media (prefers-color-scheme:dark){' + r + '{' + dark + '}}'
    return css


def _mv(r):
    m = r['move']
    if m is None:
        return '<span class="mv nw">NEW</span>'
    return '<span class="mv up">&#9650; %d</span>' % m if m > 0 else ('<span class="mv dn">&#9660; %d</span>' % -m if m < 0 else '<span class="mv eq">&#8211;</span>')


def _poll_html(data, mode, title):
    blk = data[mode]
    rows = blk['rows']
    labels = data['labels']
    w = data['weights'][mode]
    out = []
    for r in rows[:blk['top']]:
        parts = ''.join('<div><span>%s</span><span>%d%%</span></div><div class="m"><i style="width:%d%%"></i></div>' % (_X(labels[k]), w[k], r['parts'][k]) for k in w)
        out.append('<details><summary><div><div class="n">%d</div>%s</div><div><div class="who">%s</div><div class="rec">%d stable%s &middot; %d-%d-%d from %d starts &middot; %.1f%% wins &middot; %.1f%% ITM</div></div>'
                   '<div class="rt">%.1f<small>RATING</small></div></summary><div class="parts">%s</div></details>'
                   % (r['rank'], _mv(r), _X(r['name']), r['stables'], '' if r['stables'] == 1 else 's', r['wins'], r['seconds'], r['thirds'], r['starts'], r['win_pct'], r['itm_pct'], r['rating'], parts))
    if not rows:
        out.append('<div class="f">No member has enough starts in these dates yet.</div>')
    also = rows[blk['top']:blk['top'] + blk['also']]
    if also:
        out.append('<div class="v">Also receiving votes</div><div class="wrap"><table><thead><tr><th>Member</th><th>W-P-S</th><th>Starts</th><th>Rating</th></tr></thead><tbody>'
                   + ''.join('<tr><td><span class="rk">%d</span> <span class="nm">%s</span></td><td>%d-%d-%d</td><td>%d</td><td>%.1f</td></tr>' % (r['rank'], _X(r['name']), r['wins'], r['seconds'], r['thirds'], r['starts'], r['rating']) for r in also)
                   + '</tbody></table></div>')
    return out


def embed_html(store, roster, d_from, d_to, board='claiming', skin='plain', rule=None, age='all'):
    """A snapshot of one board as a block of HTML with its own scoped CSS and no scripts. Paste it into a Code module,
    a WordPress Custom HTML block, or any page that allows HTML. It shows the numbers as of the build."""
    if skin not in SKINS:
        raise PortalError('skin must be one of: ' + ', '.join(SKINS))
    if board not in ('claiming', 'overall', 'standings'):
        raise PortalError('board must be claiming, overall or standings.')
    sid = 'r' + hashlib.sha1(('%s|%s|%s|%s|%s' % (roster.get('code'), board, skin, d_from, d_to)).encode()).hexdigest()[:6]
    res = roster.get('residency') or (roster.get('code') or '').upper()
    if board == 'standings':
        b = compute(store, roster, d_from, d_to, rule, age)
        tl = _tally(store, roster, d_from, d_to, age)
        earn = {m['name']: int(round(tl[RS.norm_key(m['name'])]['earn'])) for m in b['members'] if RS.norm_key(m['name']) in tl}
        title, period = 'Standings', b['period']
        body = ['<div class="wrap"><table><thead><tr><th>Member</th><th>Starts</th><th class="w">Win</th><th class="p">Place</th><th class="sh">Show</th><th>Win %</th><th>ITM %</th><th>Earnings</th><th>Pts</th></tr></thead><tbody>']
        for m in b['members']:
            n = m['starts']
            body.append('<tr><td><span class="rk">%d</span> <span class="nm">%s</span></td><td>%d</td><td class="w">%d</td><td class="p">%d</td><td class="sh">%d</td><td>%.1f%%</td><td>%.1f%%</td><td>$%s</td><td style="color:var(--led)">%s</td></tr>'
                        % (m['rank'], _X(m['name']), n, m['wins'], m['seconds'], m['thirds'], 100.0 * m['wins'] / n if n else 0, 100.0 * (m['wins'] + m['seconds'] + m['thirds']) / n if n else 0,
                           format(earn.get(m['name'], 0), ','), m['total']))
        body.append('</tbody></table></div>')
        foot = 'Points: %s for 1st through %s.' % (b['rule']['points'][0], ', '.join(str(x) for x in b['rule']['points'][1:]) or 'none')
        built = b['generated_at']
    else:
        d = polls(store, roster, d_from, d_to, age)
        title = 'Claiming Trainer Poll' if board == 'claiming' else 'Top 10 Poll'
        period, built = d['period'], d['generated_at']
        body = _poll_html(d, board, title)
        w = d['weights'][board]
        foot = 'Rating out of 100: ' + ', '.join('%s %d' % (d['labels'][k].lower(), v) for k, v in w.items()) + '. Arrows compare with the previous %d days.' % period['days']
    return ('<style>%s</style><div class="rzb-%s" role="region" aria-label="%s"><div class="h"><div class="t">%s</div><div class="s">%s &middot; %s to %s</div></div>%s<div class="f">%s &middot; Built %s</div></div>'
            % (_skin_css(skin, sid), sid, _X(title + ' ' + res), _X(title), _X(res), period['from'], period['to'], ''.join(body), _X(foot), built.replace('T', ' ').replace('Z', ' UTC')))


# ---------------------------------------------------------------- files on disk
def _code(c):
    c = (c or '').strip().lower()
    if not RS.CODE_RE.match(c):
        raise PortalError('The residency code must be 2 to 24 letters, numbers or dashes.')
    return c


def _dir(store, code):
    return RS.residency_dir(store.path, code)


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


ALL_CODE = 'all'


def list_codes(store):
    """Every saved roster: code, name, member and stable counts, and whether it is public. The virtual 'all' folder is skipped."""
    root = os.path.dirname(_dir(store, 'xx'))
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for n in names:
        if n == ALL_CODE or not RS.CODE_RE.match(n):
            continue
        r = RS.load(os.path.join(root, n))
        if r:
            c = r.get('counts') or {}
            out.append(dict(code=n, name=r.get('residency') or n.upper(), members=c.get('members', len(r.get('members', []))),
                            stables=c.get('stables', len(r.get('stables', []))), public=bool(get_public(store, n))))
    out.sort(key=lambda x: x['name'].lower())
    return out


def merged_roster(store):
    """All members of all saved rosters as one roster. A member in two rosters becomes one member; stables are de-duplicated."""
    rosters = [RS.load(_dir(store, c['code'])) for c in list_codes(store)]
    rosters = [r for r in rosters if r]
    if not rosters:
        raise PortalError('No rosters are saved yet.', 404)
    members, flat, seen = {}, [], set()
    for r in rosters:
        for m in r.get('members', []):
            mm = members.setdefault(m['key'], dict(name=m['name'], key=m['key'], masters={}))
            for x in m.get('masters', []):
                xm = mm['masters'].setdefault(x['key'], dict(name=x['name'], key=x['key'], stables=[], _k=set()))
                for st in x.get('stables', []):
                    k = (st['key'], st.get('joined'), st.get('left'))
                    if k not in xm['_k']:
                        xm['_k'].add(k)
                        xm['stables'].append(st)
        for st in r.get('stables', []):
            k = (st['key'], st.get('member_key'), st.get('joined'), st.get('left'))
            if k not in seen:
                seen.add(k)
                flat.append(st)
    out = [dict(name=m['name'], key=m['key'], masters=[dict(name=x['name'], key=x['key'], stables=x['stables']) for x in sorted(m['masters'].values(), key=lambda x: x['key'])])
           for m in sorted(members.values(), key=lambda m: m['key'])]
    counts = dict(members=len(out), masters=sum(len(m['masters']) for m in out), stables=len(flat), errors=0, warnings=0)
    return dict(format=rosters[0].get('format'), code=ALL_CODE, residency='All residencies', contact='', ok=True, counts=counts,
                problems=dict(errors=[], warnings=[]), members=out, stables=flat, source=None)


def roster_mtime(store, code):
    codes = [c['code'] for c in list_codes(store)] if code == ALL_CODE else [code]
    best = 0
    for c in codes:
        try:
            best = max(best, os.path.getmtime(os.path.join(_dir(store, c), 'roster.json')))
        except OSError:
            pass
    return best


def load_roster(store, code):
    if _code(code) == ALL_CODE:
        return merged_roster(store)
    r = RS.load(_dir(store, _code(code)))
    if not r:
        raise PortalError('No roster is saved for "%s". Upload one first.' % code, 404)
    return r


def load_rule(store, code):
    d = _read_json(os.path.join(_dir(store, _code(code)), 'rules.json'))
    try:
        return parse_rule(d) if d else dict(DEFAULT_RULE)
    except PortalError:
        return dict(DEFAULT_RULE)


def _hosted():
    return os.environ.get('SDM_HOSTED') == '1'


def can_write():
    return (not _hosted()) or bool(os.environ.get('SDM_RESIDENCY_KEY'))


def _admin_ok(headers):
    """Hosted copy: true when the request carries the site admin key (SDM_RESIDENCY_KEY). Wrong keys are counted and blocked."""
    akey = os.environ.get('SDM_RESIDENCY_KEY', '')
    if not akey:
        raise PortalError('Changes are switched off on this site.', 403)
    sent = headers.get('X-Residency-Key', '')
    now = time.time()
    with _wlock:
        _bad[:] = [t for t in _bad if now - t < 3600]
        if len(_bad) >= MAX_BAD_KEYS_PER_HOUR:
            raise PortalError('Too many wrong keys. Try again later.', 429)
    if sent and hmac.compare_digest(sent.encode('utf-8', 'ignore'), akey.encode('utf-8')):
        return True
    with _wlock:
        _bad.append(now)
    raise PortalError('That key is not right.', 403)


def _check_write(store, headers, code, admin_only=False):
    """Who may change a residency. Local copy: anyone at the keyboard. Hosted copy: only the admin key."""
    if headers.get('X-SDM') != '1':
        raise PortalError('Request refused.', 403)
    admin = False
    if _hosted():
        admin = _admin_ok(headers)
    now = time.time()
    with _wlock:
        _writes[:] = [t for t in _writes if now - t < 3600]
        if len(_writes) >= MAX_WRITES_PER_HOUR:
            raise PortalError('Too many changes this hour. Try again later.', 429)
        _writes.append(now)
    return admin

def _check_read(store, headers, code):
    """Hosted site: a residency's roster and boards are private until the admin makes it public. Only the admin key opens them."""
    if not _hosted() or get_public(store, code):
        return
    if not headers.get('X-Residency-Key'):
        raise PortalError('This residency is private. Enter the admin key.', 403)
    _admin_ok(headers)

def roster_view(roster):
    return dict(code=roster['code'], residency=roster.get('residency', ''), built=roster.get('built'), counts=roster['counts'],
                source=(roster.get('source') or {}).get('file', ''),
                members=[dict(name=m['name'], masters=[dict(name=x['name'], stables=len(x['stables'])) for x in m['masters']]) for m in roster['members']])


def _body(body):
    try:
        a = json.loads(body or b'{}')
    except ValueError:
        raise PortalError('Bad request.')
    if not isinstance(a, dict):
        raise PortalError('Bad request.')
    return a


def upload(store, headers, body):
    if headers.get('X-SDM') != '1':
        raise PortalError('Request refused.', 403)
    a = _body(body)
    code = _code(a.get('code'))
    if code == ALL_CODE:
        raise PortalError('"all" is a reserved name for every roster together. Pick another code.')
    admin = _check_write(store, headers, code)
    try:
        data = base64.b64decode(a.get('data') or '', validate=True)
    except (binascii.Error, ValueError):
        raise PortalError('The file could not be read.')
    if not data:
        raise PortalError('Choose a roster file first.')
    d = RS.residency_dir(store.path, code)
    root = os.path.dirname(d)
    if not os.path.isdir(d) and os.path.isdir(root) and len([x for x in os.listdir(root) if os.path.isdir(os.path.join(root, x))]) >= MAX_CODES:
        raise PortalError('This site already holds %d residencies.' % MAX_CODES)
    try:
        r = RS.build(str(a.get('filename') or 'roster.xlsx')[:120], data, code, (a.get('name') or '')[:80] or None)
    except RS.RosterError as e:
        raise PortalError(str(e))
    saved = False
    if r['ok'] or a.get('accept_errors'):
        RS.save(r, d)
        saved = True
    rep = RS.match_conn(r, store.db)
    return dict(saved=saved, ok=r['ok'], roster=roster_view(r), problems=r['problems']['errors'] + [dict(w, level='note') for w in r['problems']['warnings']], match=dict(
        checked=rep['checked'], matched=rep['matched'], unmatched=[dict(stable=x['stable'], member=x['member'], close=x['close']) for x in rep['stables'] if not x['matched']],
        no_races=[x['stable'] for x in rep['stables'] if x['matched'] and not x['races']]))


def save_rules(store, headers, body):
    if headers.get('X-SDM') != '1':
        raise PortalError('Request refused.', 403)
    a = _body(body)
    code = _code(a.get('code'))
    _check_write(store, headers, code)
    load_roster(store, code)
    rule = parse_rule(a.get('rule'))
    _write_json(os.path.join(_dir(store, code), 'rules.json'), rule)
    return dict(saved=True, rule=rule)


# ---------------------------------------------------------------- the weekly picture card (one link, real numbers)
WEEK_COLORS = ('#c8362b', '#2a5bd7', '#1f8a5b', '#d98a00', '#7b4fc4', '#0e8fa0', '#c2417d', '#6b7d2a', '#a35a2a', '#4a6fa5', '#8a8f98', '#b8860b')
FONTS_LINK = ('<link rel="preconnect" href="' + 'https://fonts.gstatic.com" crossorigin>'
              '<link rel="stylesheet" href="' + 'https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600&family=Barlow+Condensed:wght@500;600;700&display=swap">')
WEEK_CSS = (
    ':root{--disp:"Barlow Condensed","Arial Narrow",Arial,sans-serif;--body:"Barlow",system-ui,-apple-system,"Segoe UI",sans-serif}'
    'html,body{margin:0;background:#0a1310}body{padding:14px;font-family:var(--body)}'
    '.wk{max-width:540px;margin:0 auto}'
    '.card{width:100%;aspect-ratio:4/5;background:linear-gradient(165deg,#0d2a21,#08150f 70%);color:#eef5ef;border-radius:6px;padding:7% 7% 6%;display:flex;flex-direction:column;position:relative;overflow:hidden;container-type:inline-size;border:1px solid #1d3a30;box-sizing:border-box}'
    '.card .k{font:600 3cqw var(--disp);letter-spacing:.2em;text-transform:uppercase;color:#ffb020}'
    '.card h2{font:700 12cqw/.9 var(--disp);text-transform:uppercase;letter-spacing:.05em;margin:2cqw 0 3cqw;color:#fff}'
    '.pod{display:grid;grid-template-columns:1fr 1.15fr 1fr;gap:2.5cqw;align-items:end;margin-top:auto}'
    '.pod>div{border-radius:4px 4px 0 0;padding:3cqw 2cqw;text-align:center;background:#143528;border-top:1.2cqw solid var(--c)}'
    '.pod .n{font:700 9cqw/1 var(--disp);color:var(--c)}.pod .t{font:600 4.6cqw var(--disp);text-transform:uppercase;letter-spacing:.03em;margin-top:1cqw;overflow-wrap:anywhere}'
    '.pod .s{font:500 3.2cqw var(--body);color:#9fb7ab}.pod>.p1{padding-block:6cqw 4cqw}'
    '.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:2.5cqw;margin:4cqw 0 3cqw}'
    '.stats>div{border-top:1px solid #2b4a3d;padding-top:2cqw}.stats b{display:block;font:700 7cqw/1 var(--disp);color:#ffb020;overflow-wrap:anywhere}'
    '.stats span{font:400 3cqw var(--body);color:#9fb7ab}'
    '.card .ft{font:500 2.8cqw var(--body);color:#7e9a8c;display:flex;justify-content:space-between;gap:2cqw}'
    '.tick{margin-top:18px;overflow:hidden;white-space:nowrap;background:#08150f;color:#eef5ef;border-radius:4px;border:1px solid #1d3a30;font:600 17px var(--disp);letter-spacing:.06em;text-transform:uppercase}'
    '.tick div{display:inline-block;padding:10px 0;animation:tk 38s linear infinite}.tick span{margin:0 22px}.tick em{font-style:normal;color:#ffb020;margin-right:6px}'
    '@keyframes tk{from{transform:translateX(0)}to{transform:translateX(-50%)}}'
    '@media (prefers-reduced-motion:reduce){.tick div{animation:none}.tick{overflow-x:auto}}'
    '.empty{font:500 4cqw var(--body);color:#9fb7ab;margin:auto 0}')


def _md(d):
    return d.strftime('%b ') + str(d.day)


def week_page(store, roster, d_from, d_to, rule=None):
    """The 'Week in the books' card as a full page: podium of the top three members, the biggest jump, best in-the-money rate,
    stakes wins, and a season ticker. Real numbers from the database. No scripts."""
    f, t = _date(d_from, 'From'), _date(d_to, 'To')
    span = (t - f).days + 1
    b = compute(store, roster, d_from, d_to, rule, 'all')
    prev = compute(store, roster, (f - datetime.timedelta(days=span)).isoformat(), (f - datetime.timedelta(days=1)).isoformat(), rule, 'all')
    pv = {m['name']: m['total'] for m in prev['members']}
    order = [m['name'] for m in roster.get('members', [])]
    color = lambda n: WEEK_COLORS[order.index(n) % len(WEEK_COLORS)] if n in order else WEEK_COLORS[0]
    ms = [m for m in b['members'] if m['starts']]
    top = ms[:3]
    res = roster.get('residency') or (roster.get('code') or '').upper()
    week = span == 7 and f.weekday() == 0
    when = ('Week of ' + _md(f)) if week else (_md(f) + ' to ' + _md(t))
    if len(top) >= 3:
        slots = [(top[1], 2), (top[0], 1), (top[2], 3)]
    else:
        slots = [(m, i + 1) for i, m in enumerate(top)]
    pod = ''.join('<div%s style="--c:%s"><div class="n">%d</div><div class="t">%s</div><div class="s">%s pts &middot; %dW %dP %dS</div></div>'
                  % (' class="p1"' if (r == 1 and len(top) >= 3) else '', color(m['name']), r, _X(m['name']), _X(('%g' % m['total'])), m['wins'], m['seconds'], m['thirds'])
                  for m, r in slots)
    jump = max(ms, key=lambda m: m['total'] - pv.get(m['name'], 0), default=None)
    jump_txt = _X(jump['name']) if jump and (jump['total'] - pv.get(jump['name'], 0)) > 0 else '&ndash;'
    elig = [m for m in ms if m['starts'] >= 3]
    best = max(elig, key=lambda m: (m['wins'] + m['seconds'] + m['thirds']) / float(m['starts']), default=None)
    itm = '%.1f%%' % (100.0 * (best['wins'] + best['seconds'] + best['thirds']) / best['starts']) if best else '&ndash;'
    tl = _tally(store, roster, d_from, d_to, 'all')
    sw = sum(a['sw'] for a in tl.values())
    year = compute(store, roster, datetime.date(t.year, 1, 1).isoformat(), d_to, rule, 'all')
    tick = ''.join('<span><em>%d</em>%s %s</span>' % (i + 1, _X(m['name']), _X('%g' % m['total'])) for i, m in enumerate([m for m in year['members'] if m['starts']][:20]))
    body = ('<div class="wk"><div class="card" role="img" aria-label="Weekly recap card for %s">'
            '<div class="k">%s &middot; %s</div><h2>Week in<br>the books</h2>%s'
            '<div class="stats"><div><b>%s</b><span>biggest jump</span></div><div><b>%s</b><span>best ITM%s</span></div><div><b>%d</b><span>stakes wins</span></div></div>'
            '<div class="ft"><span>%s</span><span>Data through %s</span></div></div>%s</div>'
            % (_X(res), _X(res.upper()), _X(when),
               ('<div class="pod">' + pod + '</div>') if top else '<div class="empty">No results in these dates yet.</div>',
               jump_txt, itm, (', ' + _X(best['name'])) if best else '', sw,
               'Points are for the week' if week else 'Points are for these dates', _md(t),
               ('<div class="tick" aria-label="Season standings ticker"><div>' + tick + tick + '</div></div>') if tick else ''))
    return body


def week_document(store, roster, d_from, d_to, rule=None):
    body = week_page(store, roster, d_from, d_to, rule)
    res = roster.get('residency') or (roster.get('code') or '').upper()
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex"><title>%s week in the books</title>%s<style>%s</style></head><body>%s</body></html>'
            % (_X(res), FONTS_LINK, WEEK_CSS, body)).encode('utf-8')


# ---------------------------------------------------------------- the Tote: the main standings board, one design, several looks
LOOKS = ('auto', 'dark', 'light', 'paper')
TOTE_SORTS = ('pts', 'w', 'itm', 'earn')
LIVE_SORTS = TOTE_SORTS + ('win',)
TOTE_SORT_NAMES = {'pts': 'Points', 'w': 'Wins', 'itm': 'ITM %', 'earn': 'Earnings'}
_LIGHT = '--bg:#e9eeec;--panel:#fff;--panel2:#f4f7f6;--ink:#10211b;--mute:#566a62;--line:#d3dcd8;--led:#14573f;--accent:#14573f;--accent-ink:#fff;--w:#c8362b;--pl:#8d9aa1;--sh:#2a5bd7;'
_DARK = '--bg:#0a1310;--panel:#111e19;--panel2:#16271f;--ink:#e9f0eb;--mute:#8ea69b;--line:#233830;--led:#ffb020;--accent:#ffb020;--accent-ink:#1a1200;--w:#f0675a;--pl:#c3cfd4;--sh:#6b98ff;'
_PAPER = '--bg:#f1eee4;--panel:#fbfaf6;--panel2:#f1eee4;--ink:#1c1b17;--mute:#5d5a4e;--line:#cfc9b6;--led:#7a1f1f;--accent:#7a1f1f;--accent-ink:#fff;--w:#a3262a;--pl:#6f6a58;--sh:#1f4e8c;--disp:Georgia,"Times New Roman",serif;--body:Georgia,"Times New Roman",serif;'
TOTE_CSS = (
    ':root{' + _LIGHT + '--disp:"Barlow Condensed","Arial Narrow",Arial,sans-serif;--body:"Barlow",system-ui,-apple-system,"Segoe UI",sans-serif}'
    '@media (prefers-color-scheme:dark){html[data-look="auto"]{' + _DARK + 'color-scheme:dark}}'
    'html[data-look="dark"]{' + _DARK + 'color-scheme:dark}html[data-look="light"]{' + _LIGHT + '}html[data-look="paper"]{' + _PAPER + '}'
    '*{box-sizing:border-box}html,body{margin:0}body{background:var(--bg);color:var(--ink);font:15px/1.5 var(--body);padding:12px}'
    '.board{max-width:1100px;margin:0 auto;background:var(--panel);border:1px solid var(--line);border-radius:6px;overflow:hidden}'
    '.bh{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--line);background:var(--panel2)}'
    '.bh h1{font:700 30px/1 var(--disp);text-transform:uppercase;letter-spacing:.05em;margin:0}.bh .sub{color:var(--mute);font-size:13px}'
    '.seg{display:flex;flex-wrap:wrap;gap:4px}.seg a{font:600 13px var(--disp);letter-spacing:.08em;text-transform:uppercase;background:var(--panel);color:var(--mute);border:1px solid var(--line);border-radius:3px;padding:5px 10px;text-decoration:none}'
    '.seg a[aria-current=true]{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}'
    '.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:760px;font-variant-numeric:tabular-nums}'
    'th{font:600 13px var(--disp);letter-spacing:.1em;text-transform:uppercase;color:var(--mute);text-align:right;padding:10px;border-bottom:2px solid var(--line);white-space:nowrap}'
    'td{padding:11px 10px;text-align:right;border-bottom:1px solid var(--line);font:500 19px var(--disp);letter-spacing:.02em;white-space:nowrap}'
    'th:first-child,td:first-child{text-align:left;padding-left:16px}td+td,th+th{border-left:1px solid var(--line)}'
    'tr.row{cursor:pointer}tr.row:hover td{background:var(--panel2)}.wcol{color:var(--w)}.pcol{color:var(--pl)}.scol{color:var(--sh)}'
    'th.wcol,th.pcol,th.scol{box-shadow:inset 0 -4px 0 currentColor}'
    '.rank{font:700 34px/1 var(--disp);color:var(--led);width:34px;display:inline-block}'
    '.who{display:inline-flex;align-items:center;gap:10px;font:600 22px var(--disp);letter-spacing:.03em;text-transform:uppercase}'
    '.silk{width:14px;height:22px;border-radius:2px 2px 7px 7px;display:inline-block;flex:none}.mut{color:var(--mute);font:400 13px var(--body);letter-spacing:0;text-transform:none}'
    '.pts{color:var(--led);font-size:26px;font-weight:700}'
    '.stack{display:flex;height:9px;border-radius:2px;overflow:hidden;background:var(--line);min-width:90px}.stack i{display:block;height:100%}'
    'tr.exp td{background:var(--panel2);text-align:left;font:400 14px var(--body);white-space:normal;padding:14px 16px 16px;border-left:0}'
    '.exp .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}'
    '.exp .k{font:600 12px var(--disp);letter-spacing:.1em;text-transform:uppercase;color:var(--mute)}.exp .v{font:600 22px var(--disp)}'
    '.none{padding:30px 16px;color:var(--mute)}'
    'a:focus-visible,tr.row:focus-visible{outline:2px solid var(--accent);outline-offset:2px}')
TOTE_JS = ("document.addEventListener('click',function(e){var r=e.target.closest('tr.row');if(!r)return;var x=r.nextElementSibling;if(x&&x.classList.contains('exp')){x.hidden=!x.hidden;r.setAttribute('aria-expanded',String(!x.hidden))}});"
           "document.addEventListener('keydown',function(e){if(e.key!=='Enter'&&e.key!==' ')return;var r=e.target.closest&&e.target.closest('tr.row');if(!r)return;e.preventDefault();r.click()});")


def _spark(vals, color):
    w, h = 96, 28
    mx = max(max(vals), 1)
    n = len(vals)
    pts = [(i * (w - 6) / float(max(n - 1, 1)) + 3, h - 3 - (h - 8) * v / float(mx)) for i, v in enumerate(vals)]
    d = ' '.join('%s%.1f %.1f' % ('L' if i else 'M', x, y) for i, (x, y) in enumerate(pts))
    lx, ly = pts[-1]
    return ('<svg width="%d" height="%d" viewBox="0 0 %d %d" role="img" aria-label="points per week, last 8 weeks"><path d="%s" fill="none" stroke="%s" stroke-width="2" stroke-linejoin="round"/>'
            '<circle cx="%.1f" cy="%.1f" r="3" fill="%s"/></svg>' % (w, h, w, h, d, color, lx, ly, color))


def _pctf(a, b):
    return 100.0 * a / b if b else 0.0


def tote_inner(store, roster, d_from, d_to, rule=None, sort='pts', base=''):
    """The parts of the Tote: (sub line, sort links, table html). Used by the stand-alone page and by the tabbed site."""
    f, t = _date(d_from, 'From'), _date(d_to, 'To')
    b = compute(store, roster, d_from, d_to, rule, 'all')
    tl = _tally(store, roster, d_from, d_to, 'all')
    order = [m['name'] for m in roster.get('members', [])]
    stables = {m['name']: sum(len(x['stables']) for x in m['masters']) for m in roster.get('members', [])}
    weeks = []                                                  # eight 7-day blocks ending on the last day, oldest first
    for k in range(7, -1, -1):
        e = t - datetime.timedelta(days=7 * k)
        r = compute(store, roster, (e - datetime.timedelta(days=6)).isoformat(), e.isoformat(), rule, 'all')
        weeks.append({m['name']: m['total'] for m in r['members']})
    rows = []
    for m in b['members']:
        a = tl.get(RS.norm_key(m['name']), {})
        n = m['starts']
        itm = _pctf(m['wins'] + m['seconds'] + m['thirds'], n)
        rows.append(dict(name=m['name'], n=n, w=m['wins'], p=m['seconds'], s=m['thirds'], pts=m['total'], itm=itm, earn=int(round(a.get('earn', 0) or 0)),
                         color=WEEK_COLORS[order.index(m['name']) % len(WEEK_COLORS)] if m['name'] in order else WEEK_COLORS[0],
                         weekly=[wk.get(m['name'], 0) for wk in weeks], stables=stables.get(m['name'], 0)))
    key = {'pts': lambda r: (r['pts'], r['w']), 'w': lambda r: (r['w'], r['pts']), 'itm': lambda r: (r['itm'], r['n']), 'earn': lambda r: (r['earn'], r['pts'])}[sort]
    rows.sort(key=lambda r: (tuple(-x for x in key(r)), r['name'].lower()))
    body = []
    for i, r in enumerate(rows):
        body.append('<tr class="row" tabindex="0" aria-expanded="false"><td><span class="rank">%d</span><span class="who"><span class="silk" style="background:%s"></span>%s</span> <span class="mut">%d stable%s</span></td>'
                    '<td>%d</td><td class="wcol">%d</td><td class="pcol">%d</td><td class="scol">%d</td><td>%.1f%%</td><td>%.1f%%</td><td>$%s</td><td>%s</td><td class="pts">%s</td></tr>'
                    % (i + 1, r['color'], _X(r['name']), r['stables'], '' if r['stables'] == 1 else 's', r['n'], r['w'], r['p'], r['s'], _pctf(r['w'], r['n']), r['itm'],
                       format(r['earn'], ','), _spark(r['weekly'], r['color']), _X('%g' % r['pts'])))
        body.append('<tr class="exp" hidden><td colspan="10"><div class="cols">'
                    '<div><div class="k">Win / Place / Show</div><div class="stack" role="img" aria-label="%d wins, %d seconds, %d thirds"><i style="width:%.1f%%;background:var(--w)"></i><i style="width:%.1f%%;background:var(--pl)"></i><i style="width:%.1f%%;background:var(--sh)"></i></div>'
                    '<div class="mut" style="margin-top:4px">%d wins, %d seconds, %d thirds from %d starts</div></div>'
                    '<div><div class="k">Place %%</div><div class="v">%.1f%%</div></div><div><div class="k">Show %%</div><div class="v">%.1f%%</div></div>'
                    '<div><div class="k">Earnings per start</div><div class="v">$%s</div></div><div><div class="k">Best week</div><div class="v">%s pts</div></div></div></td></tr>'
                    % (r['w'], r['p'], r['s'], _pctf(r['w'], r['n']), _pctf(r['p'], r['n']), _pctf(r['s'], r['n']), r['w'], r['p'], r['s'], r['n'],
                       _pctf(r['p'], r['n']), _pctf(r['s'], r['n']), format(int(round(r['earn'] / float(r['n']))) if r['n'] else 0, ','), _X('%g' % max(r['weekly']))))
    segs = ''.join('<a href="%ssort=%s" %s>%s</a>' % (base, k, 'aria-current="true"' if k == sort else '', TOTE_SORT_NAMES[k]) for k in TOTE_SORTS)
    sub = '%s to %s, %d &middot; all races &middot; sorted by %s' % (_md(f), _md(t), t.year, TOTE_SORT_NAMES[sort].lower())
    table = ('<div class="scroll"><table><thead><tr><th>Member</th><th>Starts</th><th class="wcol">Win</th><th class="pcol">Place</th><th class="scol">Show</th>'
             '<th>Win %</th><th>ITM %</th><th>Earnings</th><th>8 weeks</th><th>Pts</th></tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>') if rows else '<div class="none">No results in these dates yet.</div>'
    return sub, segs, table


def tote_document(store, roster, d_from, d_to, rule=None, look='auto', sort='pts', base=''):
    """The Tote as a full page. Click a row for more. Sort and look come from the link. One small script only opens and closes rows."""
    sub, segs, table = tote_inner(store, roster, d_from, d_to, rule, sort, base)
    res = roster.get('residency') or (roster.get('code') or '').upper()
    return ('<!doctype html><html lang="en" data-look="%s"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            '<title>%s standings</title>%s<style>%s</style></head><body><div class="board"><div class="bh"><div><h1>%s standings</h1><div class="sub">%s</div></div>'
            '<div class="seg" role="group" aria-label="Sort by">%s</div></div>%s</div><script>%s</script></body></html>'
            % (look, _X(res), FONTS_LINK, TOTE_CSS, _X(res), sub, segs, table, TOTE_JS)).encode('utf-8')


# ---------------------------------------------------------------- public boards (live, read-only, switched on by the admin)
PUBLIC_RANGES = ('latest', 'this_week', 'last_week', 'last_4', 'season')
PUBLIC_BOARDS = ('tote', 'poll', 'top10', 'stakes', 'crown', 'cards', 'week', 'claiming', 'overall', 'standings')
SITE_BOARDS = ('tote', 'poll', 'top10', 'stakes', 'crown', 'cards', 'week')
SITE_GRADES = ('All', 'G1', 'G2', 'G3', 'Stk')
LIVE_TTL = 300
_live_cache = {}
_live_lock = threading.Lock()


def _public_path(store, code):
    return os.path.join(_dir(store, code), 'public.json')


def get_public(store, code):
    """The saved public settings for a residency, or None when it is not public."""
    d = _read_json(_public_path(store, code))
    if isinstance(d, dict) and d.get('enabled') is True:
        return dict(enabled=True, range=d.get('range') if d.get('range') in PUBLIC_RANGES else 'latest',
                    skin=d.get('skin') if d.get('skin') in SKINS else 'plain')
    return None


def set_public(store, headers, body):
    """Admin only on the hosted site. Turns the public live boards on or off for one residency."""
    if headers.get('X-SDM') != '1':
        raise PortalError('Request refused.', 403)
    a = _body(body)
    code = _code(a.get('code'))
    _check_write(store, headers, code, admin_only=True)
    load_roster(store, code)
    on = a.get('enabled') is True
    rng = a.get('range') if a.get('range') in PUBLIC_RANGES else 'latest'
    skin = a.get('skin') if a.get('skin') in SKINS else 'plain'
    _write_json(_public_path(store, code), dict(enabled=on, range=rng, skin=skin))
    with _live_lock:
        _live_cache.clear()
    return dict(code=code, public=get_public(store, code))


def live_range(store, rng):
    """Dates for a public board, counted back from the newest day in the database. Weeks run Monday to Sunday."""
    lo, hi = data_range(store)
    if not hi:
        raise PortalError('No race data is loaded yet.', 404)
    last = datetime.date.fromisoformat(hi)
    dow = last.weekday()                       # Monday = 0
    monday = last - datetime.timedelta(days=dow)
    last_sun = last if dow == 6 else monday - datetime.timedelta(days=1)
    if rng == 'this_week':
        f, t = monday, last
    elif rng == 'last_week':
        t = last_sun; f = t - datetime.timedelta(days=6)
    elif rng == 'last_4':
        t = last_sun; f = t - datetime.timedelta(days=27)
    elif rng == 'season':
        f, t = datetime.date(last.year, 1, 1), last
    else:                                      # latest: the newest finished week
        t = last_sun; f = t - datetime.timedelta(days=6)
    if lo and f.isoformat() < lo:
        f = datetime.date.fromisoformat(lo)
    return f.isoformat(), t.isoformat()


MAX_LIVE_DAYS = 400


def live_custom(store, f, t):
    """Dates a visitor picked. Both must be real dates; they are kept inside the days the database has and at most 400 days apart."""
    lo, hi = data_range(store)
    if not hi:
        raise PortalError('No race data is loaded yet.', 404)
    try:
        a = datetime.date.fromisoformat(f) if f else datetime.date.fromisoformat(lo or hi)
        b = datetime.date.fromisoformat(t) if t else datetime.date.fromisoformat(hi)
    except ValueError:
        raise PortalError('Not found.', 404)
    if len(f) not in (0, 10) or len(t) not in (0, 10):
        raise PortalError('Not found.', 404)
    a = max(a, datetime.date.fromisoformat(lo)) if lo else a
    b = min(b, datetime.date.fromisoformat(hi))
    if b < a or (b - a).days + 1 > MAX_LIVE_DAYS:
        raise PortalError('Not found.', 404)
    return a.isoformat(), b.isoformat()


def live_page(store, q):
    """A public board page. 404 unless the admin switched this residency on. Cached for a few minutes."""
    try:
        code = _code(q.get('code'))
    except PortalError:
        raise PortalError('Not found.', 404)
    pub = get_public(store, code)
    if not pub:
        raise PortalError('Not found.', 404)
    board = q.get('board', 'tote')
    look = q.get('look') or 'auto'
    sort = q.get('sort') or ('itm' if board == 'crown' else 'pts')
    grade = q.get('grade') or 'All'
    bare = q.get('bare') == '1'
    minimum = None
    if q.get('min') not in (None, ''):
        try:
            minimum = int(q['min'])
        except (TypeError, ValueError):
            raise PortalError('Not found.', 404)
        if not 0 <= minimum <= 60:
            raise PortalError('Not found.', 404)
    if look not in LOOKS or sort not in LIVE_SORTS or grade not in SITE_GRADES or (q.get('bare') not in (None, '', '1', '0')):
        raise PortalError('Not found.', 404)
    if board == 'crown' and sort == 'earn' or board in ('tote',) and sort == 'win':
        raise PortalError('Not found.', 404)
    skin = q.get('skin') or pub['skin']
    rng = q.get('range') or pub['range']
    if board not in PUBLIC_BOARDS or skin not in SKINS or rng not in PUBLIC_RANGES:
        raise PortalError('Not found.', 404)
    cf, ct = q.get('from') or '', q.get('to') or ''
    if cf or ct:
        d_from, d_to = live_custom(store, cf, ct)
        rng = 'custom'
    else:
        d_from, d_to = live_range(store, rng)
    dd = _dir(store, code)
    mt = lambda n: os.path.getmtime(os.path.join(dd, n)) if os.path.isfile(os.path.join(dd, n)) else 0
    key = (code, board, skin, look, sort, grade, minimum, bare, d_from, d_to, roster_mtime(store, code), mt('rules.json'))
    now = time.time()
    with _live_lock:
        hit = _live_cache.get(key)
        if hit and now - hit[0] < LIVE_TTL:
            return hit[1]
    roster = load_roster(store, code)
    if board in SITE_BOARDS:
        from . import residency_site as SITE
        page = SITE.site_document(store, roster, d_from, d_to, load_rule(store, code), code, board, look, rng, sort, grade, minimum, bare)
        with _live_lock:
            if len(_live_cache) > 200:
                _live_cache.clear()
            _live_cache[key] = (now, page)
        return page
    page = embed_html(store, roster, d_from, d_to, board, skin, load_rule(store, code), 'all')
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex"><title>%s</title><style>html,body{margin:0;background:transparent}body{padding:2px}</style></head><body>%s</body></html>'
            % (_X((roster.get('residency') or code.upper()) + ' leaderboard'), page)).encode('utf-8')
    with _live_lock:
        if len(_live_cache) > 200:
            _live_cache.clear()
        _live_cache[key] = (now, page)
    return page


# ---------------------------------------------------------------- routes
def _page():
    with open(os.path.join(STATIC, 'residency.html'), 'rb') as f:
        return f.read()


def _board_from_query(store, q):
    code = _code(q.get('code'))
    roster = load_roster(store, code)
    rule = parse_rule(dict(points=q.get('pts', ','.join(str(x) for x in DEFAULT_RULE['points'])),
                           bonus=q.get('bonus', ''), bonus_on=q.get('bonus_on', 'place'))) if 'pts' in q else load_rule(store, code)
    return compute(store, roster, q.get('from'), q.get('to'), rule, q.get('age', 'all'), q.get('races', 'all'))


def route(app, method, path, q, headers, body=b''):
    """Returns (status, content type, bytes, extra headers). Raises nothing: errors come back as JSON."""
    def j(obj, code=200):
        return code, 'application/json', json.dumps(obj).encode(), {}
    try:
        st = app.store
        if method == 'GET':
            if path == '/residency':
                return 200, 'text/html; charset=utf-8', _page(), {}
            if path == '/api/residency/live':
                with app.lock:
                    page = live_page(st, q)
                script = "; script-src 'unsafe-inline'" if (q.get('board') or 'tote') == 'tote' else ''
                return 200, 'text/html; charset=utf-8', page, {
                    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com" + script + "; frame-ancestors *",
                    'Cache-Control': 'public, max-age=300'}
            with app.lock:
                if path in ('/api/residency/roster', '/api/residency/board', '/api/residency/polls', '/api/residency/embed', '/api/residency/export'):
                    _check_read(st, headers, _code(q.get('code')))
                if path == '/api/residency/status':
                    lo, hi = data_range(st)
                    return j(dict(can_write=can_write(), needs_key=_hosted(), first_date=lo, last_date=hi))
                if path == '/api/residency/list':
                    if _hosted():
                        if not headers.get('X-Residency-Key'):
                            raise PortalError('Enter the admin key to see the residencies.', 403)
                        _admin_ok(headers)
                    lst = list_codes(st)
                    return j(dict(residencies=lst, all=dict(members=len(merged_roster(st)['members']) if lst else 0, stables=sum(x['stables'] for x in lst))))
                if path == '/api/residency/roster':
                    code = _code(q.get('code'))
                    return j(dict(roster=roster_view(load_roster(st, code)), rule=load_rule(st, code), public=get_public(st, code)))
                if path == '/api/residency/board':
                    return j(_board_from_query(st, q))
                if path == '/api/residency/polls':
                    code = _code(q.get('code'))
                    return j(polls(st, load_roster(st, code), q.get('from'), q.get('to'), q.get('age', 'all')))
                if path == '/api/residency/embed':
                    code = _code(q.get('code'))
                    roster = load_roster(st, code)
                    rule = parse_rule(dict(points=q.get('pts', ''), bonus=q.get('bonus', ''), bonus_on=q.get('bonus_on', 'place'))) if q.get('pts') else load_rule(st, code)
                    _date(q.get('from'), 'From'); _date(q.get('to'), 'To')
                    b = q.get('board', 'claiming'); sk = q.get('skin', 'plain')
                    page = embed_html(st, roster, q.get('from'), q.get('to'), b, sk, rule, q.get('age', 'all'))
                    name = '%s-%s-%s-to-%s-%s.html' % (code, b, q['from'], q['to'], sk)
                    hdr = {'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'"}
                    if not q.get('view'):
                        hdr['Content-Disposition'] = 'attachment; filename="%s"' % name
                    return 200, 'text/html; charset=utf-8', page.encode('utf-8'), hdr
                if path == '/api/residency/export':
                    b = _board_from_query(st, q)
                    kind = q.get('kind', 'members')
                    stem = '%s-%s-to-%s' % (b['code'], b['period']['from'], b['period']['to'])
                    if kind == 'json':
                        return 200, 'application/json', json.dumps(b, indent=1).encode(), {'Content-Disposition': 'attachment; filename="%s.json"' % stem}
                    if kind in ('poll_claiming', 'poll_overall'):
                        d = polls(st, load_roster(st, b['code']), q.get('from'), q.get('to'), q.get('age', 'all'))
                        mode = kind[5:]
                        return 200, 'text/csv; charset=utf-8', ('\ufeff' + polls_csv(d, mode)).encode('utf-8'), {'Content-Disposition': 'attachment; filename="%s-%s.csv"' % (stem, kind)}
                    if kind not in ('members', 'stables', 'finishes', 'finishes_members'):
                        raise PortalError('kind must be members, stables, finishes, finishes_members or json.')
                    return 200, 'text/csv; charset=utf-8', ('﻿' + to_csv(b, kind)).encode('utf-8'), {'Content-Disposition': 'attachment; filename="%s-%s.csv"' % (stem, kind)}
        elif method == 'POST':
            with app.lock:
                if path == '/api/residency/upload':
                    return j(upload(st, headers, body))
                if path == '/api/residency/rules':
                    return j(save_rules(st, headers, body))
                if path == '/api/residency/public':
                    return j(set_public(st, headers, body))
        return j({'error': 'not found'}, 404)
    except PortalError as e:
        return j({'error': str(e)}, e.code)
    except Exception as e:
        return j({'error': 'Something went wrong: %s' % type(e).__name__}, 500)


def serve(handler, app, method, path, q, body=b''):
    """Glue for the HTTP handler in server.py."""
    code, ctype, data, extra = route(app, method, path, q, handler.headers, body)
    handler.send_response(code)
    handler.send_header('Content-Type', ctype)
    handler.send_header('Content-Length', str(len(data)))
    if 'Cache-Control' not in extra:
        handler.send_header('Cache-Control', 'no-store')
    for k, v in extra.items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)
