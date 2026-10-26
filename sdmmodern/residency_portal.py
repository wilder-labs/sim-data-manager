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

Writing (upload, rules) is allowed on a local copy. On a hosted copy (SDM_HOSTED=1) it needs the key set in the
environment variable SDM_RESIDENCY_KEY, sent in the header X-Residency-Key. With no key set, a hosted copy refuses writes.
"""
import base64, binascii, csv, datetime, hmac, io, json, os, re, threading, time
from . import residency as RS

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
MAX_UPLOAD_JSON = 4 * 1024 * 1024          # request body: a 2 MB file becomes about 2.8 MB of base64
MAX_CODES = 25                             # residencies one copy will hold
MAX_WRITES_PER_HOUR = 30
MAX_POSITIONS = 10
CLASS_OF = {'Stk': 'stk', 'G3': 'g3', 'G2': 'g2', 'GI': 'g1', 'G1': 'g1'}
STAKES_SQL = "('Stk','GI','G1','G2','G3')"
GRADED_SQL = "('GI','G1','G2','G3')"
DEFAULT_RULE = dict(points=[10, 8, 6, 4, 3, 0, 0], bonus=dict(stk=0, g3=0, g2=0, g1=0), bonus_on='place')
_writes, _wlock = [], threading.Lock()


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
    return dict(starts=0, wins=0, seconds=0, thirds=0, points=0, bonus=0)


def _add(t, f):
    for k in ('starts', 'wins', 'seconds', 'thirds', 'points', 'bonus'):
        t[k] += f[k]


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
                    continue
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


def to_csv(board, kind):
    b = io.StringIO()
    w = csv.writer(b)
    if kind == 'stables':
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


def _check_write(headers):
    if headers.get('X-SDM') != '1':
        raise PortalError('Request refused.', 403)
    if _hosted():
        key = os.environ.get('SDM_RESIDENCY_KEY', '')
        if not key:
            raise PortalError('Uploads are switched off on this site.', 403)
        sent = headers.get('X-Residency-Key', '')
        if not hmac.compare_digest(sent.encode('utf-8', 'ignore'), key.encode('utf-8')):
            raise PortalError('That key is not right.', 403)
    now = time.time()
    with _wlock:
        _writes[:] = [t for t in _writes if now - t < 3600]
        if len(_writes) >= MAX_WRITES_PER_HOUR:
            raise PortalError('Too many changes this hour. Try again later.', 429)
        _writes.append(now)


def roster_view(roster):
    return dict(code=roster['code'], residency=roster.get('residency', ''), built=roster.get('built'), counts=roster['counts'],
                source=roster.get('source', {}).get('file', ''),
                members=[dict(name=m['name'], masters=[dict(name=x['name'], stables=len(x['stables'])) for x in m['masters']]) for m in roster['members']])


def upload(store, headers, body):
    _check_write(headers)
    try:
        a = json.loads(body or b'{}')
    except ValueError:
        raise PortalError('Bad request.')
    code = _code(a.get('code'))
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
    _check_write(headers)
    try:
        a = json.loads(body or b'{}')
    except ValueError:
        raise PortalError('Bad request.')
    code = _code(a.get('code'))
    load_roster(store, code)
    rule = parse_rule(a.get('rule'))
    _write_json(os.path.join(_dir(store, code), 'rules.json'), rule)
    return dict(saved=True, rule=rule)


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
                    return j(dict(roster=roster_view(load_roster(st, code)), rule=load_rule(st, code)))
                if path == '/api/residency/board':
                    return j(_board_from_query(st, q))
                if path == '/api/residency/export':
                    b = _board_from_query(st, q)
                    kind = q.get('kind', 'members')
                    stem = '%s-%s-to-%s' % (b['code'], b['period']['from'], b['period']['to'])
                    if kind == 'json':
                        return 200, 'application/json', json.dumps(b, indent=1).encode(), {'Content-Disposition': 'attachment; filename="%s.json"' % stem}
                    if kind not in ('members', 'stables'):
                        raise PortalError('kind must be members, stables or json.')
                    return 200, 'text/csv; charset=utf-8', ('﻿' + to_csv(b, kind)).encode('utf-8'), {'Content-Disposition': 'attachment; filename="%s-%s.csv"' % (stem, kind)}
        elif method == 'POST':
            with app.lock:
                if path == '/api/residency/upload':
                    return j(upload(st, headers, body))
                if path == '/api/residency/rules':
                    return j(save_rules(st, headers, body))
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
