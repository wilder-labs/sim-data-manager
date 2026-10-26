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
  POST /api/residency/newkey         admin only (hosted): make or replace a residency's own key

Writing (upload, rules) is allowed on a local copy. On a hosted copy (SDM_HOSTED=1) the admin key in the environment
variable SDM_RESIDENCY_KEY does everything. It makes a key for each residency (newkey, or the first upload); that key can then
upload and save rules for its own residency only. Keys are sent in the header X-Residency-Key and stored only scrambled
(key.json beside the roster). With no admin key set, a hosted copy refuses writes.
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
SKINS = ('plain', 'divi', 'btb')
_X = html.escape


def _skin_css(skin, sid):
    r = '.rzb-' + sid
    base = {
        'plain': '--bg:#ffffff;--bg2:#f2f6f4;--ink:#10211b;--mute:#566a62;--line:#d3dcd8;--led:#14573f;--up:#1a7f4b;--dn:#c8362b;--w:#c8362b;--p:#8d9aa1;--s:#2a5bd7;--font:"Barlow Condensed","Arial Narrow",Arial,sans-serif;--body:system-ui,-apple-system,"Segoe UI",sans-serif;--disp-w:700;',
        'divi': '--bg:var(--gcid-body-color,#fff);--bg2:rgba(127,127,127,.08);--ink:inherit;--mute:rgba(127,127,127,.95);--line:rgba(127,127,127,.28);--led:var(--gcid-primary-color,#14573f);--up:#1a7f4b;--dn:#c8362b;--w:#c8362b;--p:#8d9aa1;--s:#2a5bd7;--font:inherit;--body:inherit;--disp-w:700;',
        'btb': '--bg:#fbfaf6;--bg2:#f1eee4;--ink:#1c1b17;--mute:#5d5a4e;--line:#cfc9b6;--led:#7a1f1f;--up:#256b3a;--dn:#a3262a;--w:#a3262a;--p:#6f6a58;--s:#1f4e8c;--font:Georgia,"Times New Roman",serif;--body:Georgia,"Times New Roman",serif;--disp-w:700;',
    }[skin]
    dark = {'plain': '--bg:#111e19;--bg2:#16271f;--ink:#e9f0eb;--mute:#8ea69b;--line:#233830;--led:#ffb020;--up:#4cc38a;--dn:#f0675a;--w:#f0675a;--p:#c3cfd4;--s:#6b98ff;',
            'btb': '--bg:#1c1b17;--bg2:#26241d;--ink:#f1eee4;--mute:#b3ad98;--line:#44402f;--led:#e2a64a;--up:#5fbf7a;--dn:#e8706f;--w:#e8706f;--p:#b9b39c;--s:#7aa7e6;'}.get(skin)
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
    """A snapshot of one board as a block of HTML with its own scoped CSS and no scripts. Paste it into a Divi 5 Code module,
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


def load_roster(store, code):
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


def _key_path(store, code):
    return os.path.join(_dir(store, code), 'key.json')


def _hash_key(key, salt):
    return hashlib.pbkdf2_hmac('sha256', key.encode('utf-8', 'ignore'), salt, KDF_ROUNDS).hex()


def _set_key(store, code):
    """Makes a new key for a residency, keeps only its scrambled form, and returns the key itself (shown once)."""
    key = secrets.token_urlsafe(18)
    salt = secrets.token_bytes(16)
    _write_json(_key_path(store, code), dict(salt=salt.hex(), hash=_hash_key(key, salt)))
    return key


def has_key(store, code):
    return os.path.isfile(_key_path(store, code))


def _key_ok(store, code, sent):
    d = _read_json(_key_path(store, code))
    try:
        salt = bytes.fromhex(d['salt'])
        return hmac.compare_digest(_hash_key(sent, salt).encode(), str(d['hash']).encode())
    except (TypeError, KeyError, ValueError):
        return False


def _check_write(store, headers, code, admin_only=False):
    """Who may change this residency. Local copy: anyone at the keyboard. Hosted copy: the admin key
    (SDM_RESIDENCY_KEY), or the residency's own key unless admin_only. Wrong keys are counted and blocked."""
    if headers.get('X-SDM') != '1':
        raise PortalError('Request refused.', 403)
    admin = False
    if _hosted():
        akey = os.environ.get('SDM_RESIDENCY_KEY', '')
        if not akey:
            raise PortalError('Uploads are switched off on this site.', 403)
        sent = headers.get('X-Residency-Key', '')
        now = time.time()
        with _wlock:
            _bad[:] = [t for t in _bad if now - t < 3600]
            if len(_bad) >= MAX_BAD_KEYS_PER_HOUR:
                raise PortalError('Too many wrong keys. Try again later.', 429)
        admin = hmac.compare_digest(sent.encode('utf-8', 'ignore'), akey.encode('utf-8'))
        if not admin:
            ok = (not admin_only) and has_key(store, code) and bool(sent) and _key_ok(store, code, sent)
            if not ok:
                with _wlock:
                    _bad.append(now)
                raise PortalError('That key is not right.', 403)
    now = time.time()
    with _wlock:
        _writes[:] = [t for t in _writes if now - t < 3600]
        if len(_writes) >= MAX_WRITES_PER_HOUR:
            raise PortalError('Too many changes this hour. Try again later.', 429)
        _writes.append(now)
    return admin


def roster_view(roster):
    return dict(code=roster['code'], residency=roster.get('residency', ''), built=roster.get('built'), counts=roster['counts'],
                source=roster.get('source', {}).get('file', ''),
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
    admin = _check_write(store, headers, code)
    if _hosted() and not has_key(store, code) and not admin:
        raise PortalError('This residency does not exist yet. The site admin creates it.', 403)
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
    saved, new_key = False, None
    if r['ok'] or a.get('accept_errors'):
        RS.save(r, d)
        saved = True
        if _hosted() and not has_key(store, code):
            new_key = _set_key(store, code)
    rep = RS.match_conn(r, store.db)
    return dict(saved=saved, new_key=new_key, ok=r['ok'], roster=roster_view(r), problems=r['problems']['errors'] + [dict(w, level='note') for w in r['problems']['warnings']], match=dict(
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


def new_key(store, headers, body):
    """Admin only: make a key for a residency (creating it if needed) or replace its key. The key is shown once."""
    if headers.get('X-SDM') != '1':
        raise PortalError('Request refused.', 403)
    a = _body(body)
    code = _code(a.get('code'))
    if not _hosted():
        raise PortalError('Keys are only used on the hosted site.')
    _check_write(store, headers, code, admin_only=True)
    d = _dir(store, code)
    root = os.path.dirname(d)
    if not os.path.isdir(d) and os.path.isdir(root) and len([x for x in os.listdir(root) if os.path.isdir(os.path.join(root, x))]) >= MAX_CODES:
        raise PortalError('This site already holds %d residencies.' % MAX_CODES)
    return dict(code=code, new_key=_set_key(store, code))


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
            with app.lock:
                if path == '/api/residency/status':
                    lo, hi = data_range(st)
                    return j(dict(can_write=can_write(), needs_key=_hosted(), first_date=lo, last_date=hi))
                if path == '/api/residency/roster':
                    code = _code(q.get('code'))
                    return j(dict(roster=roster_view(load_roster(st, code)), rule=load_rule(st, code), has_key=has_key(st, code)))
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
                if path == '/api/residency/newkey':
                    return j(new_key(st, headers, body))
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
    handler.send_header('Cache-Control', 'no-store')
    for k, v in extra.items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)
