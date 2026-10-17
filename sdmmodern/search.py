"""Searches and summary statistics over the local database."""
from .store import week_to_date, split_racenum

SELECT = """SELECT e.id, e.racenum, e.week, h.hname, e.hid, e.tag, h.sex, e.fin, e.ppos, e.sp, e.odds,
 o.oname, o.o15 AS oname15, o.o12 AS oname12, o.o10 AS oname10, e.bps, e.earn,
 r.surf, r.restr, r.cond, r.dist, r.size, r.purse, r.cprice, r.age, r.loc_etc, r.raceid
 FROM entries e JOIN horses h ON h.hid = e.hid LEFT JOIN owners o ON o.id = e.owner_id LEFT JOIN races r ON r.racenum = e.racenum"""


def _like(term):
    t = term.strip().replace('%', '').replace('_', r'\_')
    return t.replace('*', '%')


def _name_clause(col, term, params, exact=True):
    t = term.strip()
    if '*' in t:
        params.append(_like(t))
        return f"{col} LIKE ? ESCAPE '\\'"
    if exact:
        params.append(t.lower())
        return f"LOWER({col}) = ?"
    params.append('%' + _like(t) + '%')
    return f"{col} LIKE ? ESCAPE '\\'"


def _owner_clause(term, params, exact=True):
    parts = []
    names = [t.strip() for t in term.split(',') if t.strip()] or [term]   # several trainers may be separated by commas
    plain = [t for t in names if exact and '*' not in t]               # exact names go into IN lists: any number of them stays cheap
    other = [t for t in names if not (exact and '*' not in t)]
    for c in ('oname', 'o15', 'o12', 'o10'):
        for i in range(0, len(plain), 300):
            chunk = plain[i:i + 300]
            parts.append('LOWER(%s) IN (%s)' % (c, ','.join('?' * len(chunk))))
            params.extend(t.lower() for t in chunk)
    for t in other:
        for c in ('oname', 'o15', 'o12', 'o10'):
            parts.append(_name_clause(c, t, params, exact))
    return 'e.owner_id IN (SELECT id FROM owners WHERE ' + ' OR '.join(parts) + ')'


def _decorate(store, rows):
    tracks = store.tracks()
    aw, ad = store.anchor()
    out = []
    for r in rows:
        d = dict(r)
        tr, rc = split_racenum(d['racenum'])
        d['track'] = tracks.get(tr, str(tr))
        d['race_no'] = rc
        d['date'] = week_to_date(d['week'], aw, ad).isoformat()
        d['two_yo'] = bool(d.get('age')) and d['age'][2:3] == '2' and not (d['age'] or '').endswith('+')
        out.append(d)
    return out


def race_filters(two_yo=False, three_yo=False, graded=False):
    """WHERE clauses on the races table: 2-year-old and/or 3-year-old races (exact ages, not '3 and up'),
    and graded stakes (GI, G2, G3) only."""
    w, ages = [], []
    if two_yo:
        ages.append("r.age LIKE '_-2'")
    if three_yo:
        ages.append("r.age LIKE '_-3'")
    if ages:
        w.append('(' + ' OR '.join(ages) + ')')
    if graded:
        w.append("r.restr IN ('GI', 'G1', 'G2', 'G3')")
    return w


def search(store, kind, name='', hid=None, first_week=None, last_week=None, two_yo=False,
           current_owner_only=False, min_speed=0, limit=15000, exact=True, race=None, hname_exact=None,
           three_yo=False, graded=False):
    p, w = [], []
    if kind == 'horse':
        if hid not in (None, ''):
            w.append('e.hid = ?'); p.append(int(hid))
        if name.strip():
            w.append('e.hid IN (SELECT hid FROM horses WHERE %s)' % _name_clause('hname', name, p, exact))
        if not w:
            raise ValueError('Enter a horse name or ID.')
    elif kind == 'owner':
        if not name.strip():
            raise ValueError('Enter a trainer name.')
        w.append(_owner_clause(name, p, exact))
    elif kind == 'race':
        try:
            rn = round(float(race if race is not None else name), 6)
        except (TypeError, ValueError):
            raise ValueError('Enter a race number such as 10295.9614')
        w.append('e.racenum = ?'); p.append(rn)
    else:
        raise ValueError('Unknown search type')
    if first_week not in (None, ''):
        w.append('e.racenum >= ?'); p.append(int(first_week))
    if last_week not in (None, ''):
        w.append('e.racenum < ?'); p.append(int(last_week) + 1)
    w += race_filters(two_yo, three_yo, graded)
    sql = SELECT + ' WHERE ' + ' AND '.join(w) + ' ORDER BY e.racenum DESC, e.fin ASC LIMIT ?'
    rows = _decorate(store, store.db.execute(sql, p + [int(limit)]).fetchall())

    # a horse-name search can match several horses: ask the user which one
    if kind == 'horse':
        seen = {}
        for r in rows:
            k = r['hid']
            c = seen.setdefault(k, dict(hid=k, hname=r['hname'], tag=r['tag'], sex=r['sex'], starts=0, last_week=r['week'], owner=r['oname']))
            c['starts'] += 1
        if len(seen) > 1:
            return dict(kind=kind, candidates=sorted(seen.values(), key=lambda c: -c['last_week']), rows=[], summary=None)
        if current_owner_only and rows:
            cur = rows[0]['oname']
            rows = [r for r in rows if r['oname'] == cur]
    res = dict(kind=kind, candidates=[], rows=rows, summary=None, truncated=len(rows) >= limit)
    latest = store.db.execute('SELECT MAX(week) FROM entries').fetchone()[0] or 0
    if kind == 'horse' and rows:
        res['summary'] = summarize(rows, latest, min_speed)
        trainers = {(r['oname'] or '').lower() for r in rows}
        if len(trainers) > 1:
            cur = rows[0]['oname']
            sub = [r for r in rows if (r['oname'] or '').lower() == (cur or '').lower()]
            ct = summarize(sub, latest, min_speed)
            ct['trainer'] = cur
            res['summary']['current_trainer'] = ct
    elif kind == 'owner' and rows:
        res['stable'] = stable_table(rows, latest, min_speed)
        res['summary'] = summarize(rows, latest, min_speed, label='All horses of this trainer' if ',' not in name else 'All horses of these trainers')
    elif kind == 'race' and rows:
        res['race'] = dict(rows[0], runners=len(rows))
        res['field'] = race_field(store, rows)
    return res


LAYOFFS = (('Under 15 days', 0, 14), ('15 to 21 days', 15, 21), ('22 to 29 days', 22, 29), ('30 days or more', 30, 10 ** 9))
CLASSES = ('Claiming', 'Allowance', 'Starter allowance', 'Stakes', 'Grade stakes')


def race_class(restr):
    """Group the restriction codes: claiming = MC, Clm, C-codes and the numeric claiming levels;
    allowance = MS (maiden special weight), Alw and the A-codes; Str = starter allowance;
    Stk = stakes; G3/G2/GI = graded stakes."""
    r = (restr or '').strip()
    if r in ('GI', 'G1', 'G2', 'G3'):
        return 'Grade stakes'
    if r == 'Stk':
        return 'Stakes'
    if r == 'Str':
        return 'Starter allowance'
    if r in ('MS', 'Alw') or (r[:1] == 'A' and len(r) == 3):
        return 'Allowance'
    if r in ('MC', 'Clm') or r.isdigit() or (r[:1] == 'C' and len(r) == 3):
        return 'Claiming'
    return None


def _speeds(rows, min_speed):
    return [r['sp'] for r in rows if r['sp'] is not None and r['sp'] > 0 and r['sp'] >= min_speed]


def summarize(rows, latest_week, min_speed=0, label=None):
    n = len(rows)
    pos = {i: sum(1 for r in rows if r['fin'] == i) for i in (1, 2, 3, 4, 5)}
    sp = _speeds(rows, min_speed)
    wins = [r for r in rows if r['fin'] == 1]
    s = dict(label=label, starts=n, w=pos[1], p=pos[2], s=pos[3], fourth=pos[4], fifth=pos[5],
             win_pct=round(100 * pos[1] / n, 1) if n else 0,
             itm_pct=round(100 * (pos[1] + pos[2] + pos[3]) / n, 1) if n else 0,
             avg_speed=round(sum(sp) / len(sp), 2) if sp else None,
             max_speed=round(max(sp), 2) if sp else None,
             speed_races=len(sp), excluded=n - len(sp),
             win_purse=sum(r['purse'] or 0 for r in wins),
             bp_total=sum(r['bps'] or 0 for r in rows),
             earn=round(sum(r.get('earn') or 0 for r in rows)),
             earn_per_start=round(sum(r.get('earn') or 0 for r in rows) / n) if n else 0,
             best_earn=round(max((r.get('earn') or 0 for r in rows), default=0)))
    best = max((r for r in rows if r['sp'] and r['sp'] >= min_speed), key=lambda r: r['sp'], default=None)
    if best:
        s['max_speed_race'] = dict(date=best['date'], track=best['track'], race_no=best['race_no'], dist=best['dist'], surf=best['surf'])
    for t in (90, 100, 110):
        c = sum(1 for x in sp if x >= t)
        s[f'n{t}'] = c
        s[f'p{t}'] = round(100 * c / len(sp), 1) if sp else 0
    # wins in the last 3/6/9/12 months, measured from the newest week in the database
    s['recent_wins'] = {}
    for label_, days in (('3 months', 91), ('6 months', 182), ('9 months', 273), ('12 months', 364)):
        s['recent_wins'][label_] = sum(1 for r in wins if r['week'] > latest_week - days)
    # speed by surface and distance (furlongs: under 8 = sprint)
    tbl = {}
    for r in rows:
        if r['sp'] is None or r['sp'] <= 0 or r['sp'] < min_speed or r['dist'] is None:
            continue
        key = ({'D': 'Dirt', 'T': 'Turf', 'P': 'Poly'}.get(r['surf'], r['surf'] or '?'), 'Sprint' if r['dist'] < 8 else ('Long' if r['dist'] >= 12 else 'Route'))
        tbl.setdefault(key, []).append((r['sp'], r['hname'], r['hid']))
    s['by_type'] = []
    for k, v in sorted(tbl.items(), key=lambda kv: (kv[0][0], ('Sprint', 'Route', 'Long').index(kv[0][1]))):
        top = max(v, key=lambda t: t[0])
        s['by_type'].append(dict(surface=k[0], dist=k[1], n=len(v), avg=round(sum(t[0] for t in v) / len(v), 2), max=round(top[0], 2), hname=top[1], hid=top[2]))
    # starts, win % and top speed by class of race
    by = {c: [] for c in CLASSES}
    for r in rows:
        c = race_class(r.get('restr'))
        if c:
            by[c].append(r)
    s['by_class'] = []
    for c in CLASSES:
        rs = by[c]
        spd = [r['sp'] for r in rs if r['sp'] and r['sp'] > 0 and r['sp'] >= min_speed]
        wn = sum(1 for r in rs if r['fin'] == 1)
        s['by_class'].append(dict(cls=c, starts=len(rs), wins=wn, earn=round(sum(r.get('earn') or 0 for r in rs)), win_pct=round(100 * wn / len(rs), 1) if rs else None,
                                  top_speed=round(max(spd), 2) if spd else None,
                                  avg_speed=round(sum(spd) / len(spd), 2) if spd else None))
    # win % by days since the horse's previous race (first-time starters have no layoff and are left out)
    prev, lay = {}, {k: [0, 0, 0] for k, _, _ in LAYOFFS}
    for r in sorted(rows, key=lambda r: r['racenum']):
        pw = prev.get(r['hid'])
        prev[r['hid']] = r['week']
        if pw is None:
            continue
        gap = r['week'] - pw
        for k, lo, hi in LAYOFFS:
            if lo <= gap <= hi:
                lay[k][0] += 1; lay[k][1] += r['fin'] == 1; lay[k][2] += r['fin'] is not None and 1 <= r['fin'] <= 3
    s['by_layoff'] = [dict(label=k, starts=v[0], wins=v[1], win_pct=round(100.0 * v[1] / v[0], 1) if v[0] else None,
                           itm_pct=round(100.0 * v[2] / v[0], 1) if v[0] else None) for k, v in lay.items()]
    # last five speeds, newest first
    s['recent_speeds'] = [round(r['sp'], 1) for r in sorted(rows, key=lambda r: -r['racenum']) if r['sp'] and r['sp'] > 0][:5]
    latest = max(rows, key=lambda r: r['racenum'])
    s['last_race'] = dict(date=latest['date'], track=latest['track'], race_no=latest['race_no'], owner=latest['oname'], fin=latest['fin'])
    s['horse'] = dict(hname=latest['hname'], hid=latest['hid'], tag=latest['tag'], sex=latest['sex'])
    return s


def stable_table(rows, latest_week, min_speed=0):
    by = {}
    for r in rows:
        by.setdefault(r['hid'], []).append(r)
    out = []
    for hid, rs in by.items():
        s = summarize(rs, latest_week, min_speed)
        out.append(dict(hid=hid, hname=rs[0]['hname'], tag=rs[0]['tag'], sex=rs[0]['sex'], starts=s['starts'], w=s['w'], p=s['p'], s=s['s'],
                        avg_speed=s['avg_speed'], max_speed=s['max_speed'], last_date=s['last_race']['date'], earn=s['earn'],
                        last_racenum=max(r['racenum'] for r in rs)))
    return sorted(out, key=lambda x: (-(x['earn'] or 0), -x['last_racenum']))   # highest earnings first


def week_to_date_info(store, week=None, date=None):
    import datetime
    aw, ad = store.anchor()
    if week not in (None, ''):
        return dict(week=int(week), date=week_to_date(int(week), aw, ad).isoformat())
    d = datetime.date.fromisoformat(date)
    return dict(week=aw + (d - ad).days, date=d.isoformat())


# ---- owner changes: each horse's last race under the old owner and its first race under the new one
def _oc_signature(store):
    r = store.db.execute('SELECT COUNT(*), COALESCE(MAX(id),0) FROM entries').fetchone()
    return '%d:%d' % (r[0], r[1])


def _oc_ensure(store):
    """Build (or rebuild after new data arrives) the table of owner changes. One pass over all runners."""
    db = store.db
    db.execute('CREATE TABLE IF NOT EXISTS owner_changes(id INTEGER PRIMARY KEY, prev_id INTEGER)')
    sig = _oc_signature(store)
    have = db.execute("SELECT v FROM settings WHERE k='oc_sig'").fetchone()
    if have and have[0] == sig:
        return False
    db.execute('DELETE FROM owner_changes')
    db.execute("""INSERT INTO owner_changes(id, prev_id)
        SELECT id, prev_id FROM (SELECT id, owner_id, LAG(owner_id) OVER w AS po, LAG(id) OVER w AS prev_id
                                 FROM entries WINDOW w AS (PARTITION BY hid ORDER BY racenum))
        WHERE po IS NOT NULL AND owner_id IS NOT NULL AND po <> owner_id""")
    db.execute("INSERT OR REPLACE INTO settings VALUES('oc_sig', ?)", (sig,))
    db.commit()
    return True


def owner_changes(store, first_week=None, last_week=None, two_yo=False, min_speed=0, limit=4000, no_cpu=True, three_yo=False, graded=False, min_new=10, min_prior=3):
    rebuilt = _oc_ensure(store)
    # limited to this calendar year: the first race under the new trainer must fall between January 1 and the newest race
    latest = store.db.execute('SELECT MAX(week) FROM entries').fetchone()[0] or 0
    aw, ad = store.anchor()
    newest = week_to_date(latest, aw, ad)
    year_start = latest - (newest - newest.replace(month=1, day=1)).days
    first_week = max(int(first_week), year_start) if first_week not in (None, '') else year_start
    w, p = [], []
    if first_week not in (None, ''):
        w.append('e.racenum >= ?'); p.append(int(first_week))
    if last_week not in (None, ''):
        w.append('e.racenum < ?'); p.append(int(last_week) + 1)
    join = ''
    rf = race_filters(two_yo, three_yo, graded)
    if rf:
        join = ' JOIN races r ON r.racenum = e.racenum'
        w += rf
    join += ' JOIN entries pe ON pe.id = c.prev_id'
    if no_cpu:   # skip moves to or from the computer-run stables (names starting with *)
        join += ' JOIN owners o1 ON o1.id = e.owner_id JOIN owners o2 ON o2.id = pe.owner_id'
        w.append("o1.oname NOT LIKE '*%' AND o2.oname NOT LIKE '*%'")
    # established horses only: at least `min_prior` starts up to and including the last race for the old trainer
    est = '(SELECT COUNT(*) FROM entries z WHERE z.hid = e.hid AND z.racenum <= pe.racenum) >= ?'
    w.append(est); p.append(int(min_prior))
    sql = ('SELECT c.id, c.prev_id FROM owner_changes c JOIN entries e ON e.id = c.id' + join +
           (' WHERE ' + ' AND '.join(w) if w else '') + ' ORDER BY e.racenum DESC LIMIT ?')
    pairs = store.db.execute(sql, p + [int(limit)]).fetchall()
    ids = [x for pr in pairs for x in pr]
    got = {}
    for i in range(0, len(ids), 800):
        chunk = ids[i:i + 800]
        q = SELECT + ' WHERE e.id IN (%s)' % ','.join('?' * len(chunk))
        for r in _decorate(store, store.db.execute(q, chunk).fetchall()):
            got[r['id']] = r
    rows, stats = [], dict(pairs=0, sp_pairs=0, sp_before=0.0, sp_after=0.0, win_before=0, win_after=0, gap_days=0)
    for n, (aid, bid) in enumerate(pairs, 1):
        a, b = got.get(aid), got.get(bid)
        if not a or not b:
            continue
        a, b = dict(a), dict(b)      # a horse that changed twice appears in two pairs: give each pair its own rows
        b['chg'], a['chg'] = 'Before', 'After'
        b['pair'] = a['pair'] = n
        b['from_owner'] = b['oname']; a['from_owner'] = b['oname']
        rows += [b, a]
        stats['pairs'] += 1
        stats['win_before'] += b['fin'] == 1
        stats['win_after'] += a['fin'] == 1
        stats['gap_days'] += max(0, a['week'] - b['week'])
        if (b['sp'] or 0) > 0 and (a['sp'] or 0) > 0 and b['sp'] >= min_speed and a['sp'] >= min_speed:
            stats['sp_pairs'] += 1; stats['sp_before'] += b['sp']; stats['sp_after'] += a['sp']
    # Statistics cover every change in the date range, not just the rows shown.
    ww, pp = [], []
    if first_week not in (None, ''):
        ww.append('e.racenum >= ?'); pp.append(int(first_week))
    if last_week not in (None, ''):
        ww.append('e.racenum < ?'); pp.append(int(last_week) + 1)
    ww.append('e.racenum >= 1000 AND pe.racenum >= 1000')     # ignore damaged rows
    ww.append(est); pp.append(int(min_prior))
    if no_cpu:
        ww.append("o1.oname NOT LIKE '*%' AND o2.oname NOT LIKE '*%'")
    joins = (' FROM owner_changes c JOIN entries e ON e.id = c.id JOIN entries pe ON pe.id = c.prev_id'
             ' JOIN owners o1 ON o1.id = e.owner_id JOIN owners o2 ON o2.id = pe.owner_id')
    where = ' WHERE ' + ' AND '.join(ww)
    base = joins + where
    db = store.db
    # one pass over the pairs in range, aggregated here (the equivalent GROUP BY query is far slower in SQLite)
    pr = db.execute('SELECT o1.oname, e.fin, e.sp, pe.sp, pe.fin, e.week, pe.week, e.hid, e.racenum, e.earn, pe.earn, rb.restr' + joins + ' LEFT JOIN races rb ON rb.racenum = pe.racenum' + where, pp).fetchall()
    ok = lambda v: v is not None and 0 < v < 200
    n = len(pr)
    wb = sum(1 for x in pr if x[4] == 1)
    wa = sum(1 for x in pr if x[1] == 1)
    ita = sum(1 for x in pr if x[1] is not None and x[1] <= 3)
    both = [x for x in pr if ok(x[2]) and ok(x[3])]
    s = dict(pairs=n, shown=len(rows) // 2, rebuilt=rebuilt,
             win_before=round(100.0 * wb / n, 1) if n else 0,
             win_after=round(100.0 * wa / n, 1) if n else 0,
             itm_after=round(100.0 * ita / n, 1) if n else 0,
             avg_gap_days=round(sum(x[5] - x[6] for x in pr) / n, 1) if n else 0,
             sp_pairs=len(both),
             earn_before=round(sum(x[10] or 0 for x in pr) / n) if n else 0,
             earn_after=round(sum(x[9] or 0 for x in pr) / n) if n else 0,
             avg_before=round(sum(x[3] for x in both) / len(both), 2) if both else None,
             avg_after=round(sum(x[2] for x in both) / len(both), 2) if both else None)
    by = {}
    for x in pr:
        by.setdefault(x[0], []).append(x)
    trainers = []
    for name, xs in by.items():
        if len(xs) < int(min_new):
            continue
        k = len(xs)
        wins = sum(1 for x in xs if x[1] == 1)
        itm = sum(1 for x in xs if x[1] is not None and x[1] <= 3)
        gains = [x[2] - x[3] for x in xs if ok(x[2]) and ok(x[3])]
        after = [x[2] for x in xs if ok(x[2])]
        trainers.append(dict(oname=name, horses=k, wins=wins, itm=itm, win_pct=round(100.0 * wins / k, 1), itm_pct=round(100.0 * itm / k, 1),
                             gain=round(sum(gains) / len(gains), 2) if gains else None,
                             earn=round(sum(x[9] or 0 for x in xs)), earn_avg=round(sum(x[9] or 0 for x in xs) / k),
                             avg_after=round(sum(after) / len(after), 2) if after else None,
                             top_after=round(max(after), 2) if after else None))
    # horses that were running in a claiming race just before the change: the new trainer most likely claimed them
    cl_by = {}
    for x in pr:
        if race_class(x[11]) == 'Claiming':
            cl_by.setdefault(x[0], []).append(x)
    def cblock(xs, **lab):
        k = len(xs)
        w = sum(1 for x in xs if x[1] == 1)
        i = sum(1 for x in xs if x[1] is not None and 1 <= x[1] <= 3)
        e = sum(x[9] or 0 for x in xs)
        return dict(lab, horses=k, wins=w, win_pct=round(100.0 * w / k, 1) if k else None, itm_pct=round(100.0 * i / k, 1) if k else None,
                    earn=round(e), per_start=round(e / k) if k else None)
    claim_all = [x for xs in cl_by.values() for x in xs]
    claims = dict(min=5, overall=cblock(claim_all), rows=[cblock(v, oname=k) for k, v in cl_by.items() if len(v) >= 5])
    top = sorted(both, key=lambda x: -(x[2] - x[3]))[:10]
    jumps = []
    aw, ad = store.anchor()
    for x in top:
        hn = db.execute('SELECT hname FROM horses WHERE hid = ?', (x[7],)).fetchone()
        jumps.append(dict(hname=hn[0] if hn else '', hid=x[7], to_t=x[0], sp_before=x[3], sp_after=x[2], fin_after=x[1], racenum=x[8],
                          week=x[5], date=week_to_date(x[5], aw, ad).isoformat(), gain=round(x[2] - x[3], 2)))
    s['year'] = newest.year
    return dict(kind='changes', candidates=[], rows=rows, summary=None, changes=s, truncated=len(pairs) >= limit,
                trainers=dict(min_new=int(min_new), min_prior=int(min_prior), rows=trainers, jumps=jumps, claims=claims))


def best_speeds(store, days=30, two_yo=False, min_speed=0, limit=2000, three_yo=False, graded=False):
    """Each horse's fastest race in the newest `days` racing days of the database, fastest first."""
    latest = store.db.execute('SELECT MAX(week) FROM entries').fetchone()[0] or 0
    first = latest - days + 1
    w = ['e.racenum >= ?', 'e.racenum < ?', 'e.sp > 0', 'e.sp >= ?']
    p = [first, latest + 1, float(min_speed or 0)]
    w += race_filters(two_yo, three_yo, graded)
    sql = SELECT + ' WHERE ' + ' AND '.join(w)
    rows = _decorate(store, store.db.execute(sql, p).fetchall())
    best, runs = {}, {}
    for r in rows:
        runs[r['hid']] = runs.get(r['hid'], 0) + 1
        if r['hid'] not in best or r['sp'] > best[r['hid']]['sp']:
            best[r['hid']] = r
    top = sorted(best.values(), key=lambda r: -r['sp'])
    total = len(top)
    cats = speed_categories(rows)
    top = top[:limit]
    for i, r in enumerate(top, 1):
        r['rank'] = i
        r['runs'] = runs[r['hid']]
    aw, ad = store.anchor()
    return dict(kind='speeds', candidates=[], rows=top, summary=None, truncated=total > limit,
                speeds=dict(cats=cats, horses=total, days=days, first_date=week_to_date(first, aw, ad).isoformat(),
                            last_date=week_to_date(latest, aw, ad).isoformat(),
                            top=top[0]['sp'] if top else None, shown=len(top)))


def first_time_starters(store, limit=3000, min_trainer=10):
    """Every horse whose first race ever was on or after January 1 of the newest race's year, one row per horse
    (its debut), fastest speed figure first. Statistics and leaders cover all of them, not just the rows listed."""
    latest = store.db.execute('SELECT MAX(week) FROM entries').fetchone()[0] or 0
    aw, ad = store.anchor()
    last = week_to_date(latest, aw, ad)
    jan1 = last.replace(month=1, day=1)
    first = latest - (last - jan1).days
    sql = (SELECT + ' WHERE e.racenum >= ? AND e.racenum < ? AND NOT EXISTS '
           '(SELECT 1 FROM entries x WHERE x.hid = e.hid AND x.racenum < e.racenum) ORDER BY e.racenum DESC')
    rows = _decorate(store, store.db.execute(sql, (first, latest + 1)).fetchall())
    seen, debuts = set(), []
    for r in rows:                      # a horse that ran twice on its first day appears once
        if r['hid'] in seen:
            continue
        seen.add(r['hid'])
        r['_age'] = horse_age(r['hid'], r['date'])
        debuts.append(r)
    n = len(debuts)
    spd = lambda rs: [r['sp'] for r in rs if r['sp'] and r['sp'] > 0]

    def block(rs, **lab):
        sp = spd(rs)
        w = sum(1 for r in rs if r['fin'] == 1)
        itm = sum(1 for r in rs if r['fin'] and 1 <= r['fin'] <= 3)
        return dict(lab, debuts=len(rs), wins=w, win_pct=round(100.0 * w / len(rs), 1) if rs else None,
                    itm_pct=round(100.0 * itm / len(rs), 1) if rs else None,
                    avg=round(sum(sp) / len(sp), 2) if sp else None, top=round(max(sp), 2) if sp else None,
                    earn=round(sum(r.get('earn') or 0 for r in rs)))

    def group(keyf, order=None):
        d = {}
        for r in debuts:
            k = keyf(r)
            if k is not None:
                d.setdefault(k, []).append(r)
        keys = [k for k in order if k in d] if order else sorted(d, key=lambda k: -len(d[k]))
        return [block(d[k], label=k) for k in keys]

    surf = {'D': 'Dirt', 'T': 'Turf', 'P': 'Poly'}
    dtype = lambda r: None if r['dist'] is None else ('Sprint' if r['dist'] < 8 else ('Long' if r['dist'] >= 12 else 'Route'))
    stats = dict(
        total=block(debuts, label='All first-time starters'),
        by_age=group(lambda r: None if r['_age'] is None else ('2yo' if r['_age'] <= 2 else '3yo' if r['_age'] == 3 else '4yo and up'), ['2yo', '3yo', '4yo and up']),
        by_sex=group(lambda r: {'M': 'Colts and horses', 'G': 'Geldings', 'F': 'Fillies and mares'}.get(r['sex']), ['Colts and horses', 'Geldings', 'Fillies and mares']),
        by_surface=group(lambda r: surf.get(r['surf']), ['Dirt', 'Turf', 'Poly']),
        by_dist=group(dtype, ['Sprint', 'Route', 'Long']),
        by_class=group(lambda r: race_class(r['restr']), list(CLASSES)),
        by_track=group(lambda r: r['track'])[:10],
        cats=speed_categories(debuts))
    # trainers (computer-run stables left out)
    tr = {}
    for r in debuts:
        if r.get('oname') and not r['oname'].startswith('*'):
            tr.setdefault(r['oname'], []).append(r)
    trainers = [block(v, oname=k) for k, v in tr.items()]
    big = [t for t in trainers if t['debuts'] >= min_trainer]
    lead = lambda lst, key, extra=(): sorted(lst, key=lambda t: tuple(-(t[k] or 0) for k in (key,) + tuple(extra)))[:10]
    leaders = dict(most_debuts=lead(trainers, 'debuts', ('wins',)), most_wins=lead(trainers, 'wins', ('debuts',)),
                   best_earn=lead([dict(t, per=t['earn'] / t['debuts']) for t in big], 'per', ('debuts',)), best_speed=lead(big, 'avg', ('debuts',)),
                   most_earned=lead(trainers, 'earn'), min_trainer=int(min_trainer))
    ranked = sorted(debuts, key=lambda r: (r['sp'] is None or r['sp'] <= 0, -(r['sp'] or 0)))
    total = len(ranked)
    ranked = ranked[:limit]
    for i, r in enumerate(ranked, 1):
        r['rank'] = i
        r.pop('_age', None)
    return dict(kind='debuts', candidates=[], rows=ranked, summary=None, truncated=total > limit,
                debuts=dict(count=total, shown=len(ranked), year=last.year, first_date=jan1.isoformat(), last_date=last.isoformat(),
                            stats=stats, leaders=leaders))


def current_horses(store, name, days=180, exact=True):
    """Horses whose most recent start ever was for the named trainer(s) (several separated by commas) and who ran
    within the last `days` racing days of the database. One row per horse, most recently run first."""
    if not name.strip():
        raise ValueError('Enter a trainer name.')
    latest = store.db.execute('SELECT MAX(week) FROM entries').fetchone()[0] or 0
    aw, ad = store.anchor()
    p = []
    cl = _owner_clause(name, p, exact)
    inner = cl[len('e.owner_id IN ('):-1]      # the owner lookup, reused below to know which trainer names were searched
    names = {r[0] for r in store.db.execute('SELECT DISTINCT oname FROM owners WHERE id IN (' + inner + ')', p).fetchall()}
    sql = ('SELECT e.hid FROM entries e WHERE ' + cl + ' AND e.week > ? AND e.racenum = '
           '(SELECT MAX(x.racenum) FROM entries x WHERE x.hid = e.hid)')
    hids = [r[0] for r in store.db.execute(sql, p + [latest - int(days)]).fetchall()]
    rows = []
    for i in range(0, len(hids), 800):
        chunk = hids[i:i + 800]
        q = SELECT + ' WHERE e.hid IN (%s)' % ','.join('?' * len(chunk))
        rows += _decorate(store, store.db.execute(q, chunk).fetchall())
    last_date = week_to_date(latest, aw, ad)
    year_start = latest - (last_date - last_date.replace(month=1, day=1)).days   # all figures are for this calendar year only
    by_all = {}
    for r in rows:
        by_all.setdefault(r['hid'], []).append(r)
    rows = [r for r in rows if r['week'] >= year_start and r['oname'] in names]   # this year, for the searched trainer only
    by = {}
    for r in rows:
        by.setdefault(r['hid'], []).append(r)
    out = []
    for hid, ra in by_all.items():
        ra.sort(key=lambda r: r['racenum'])
        lastr = ra[-1]
        rs = by.get(hid, [])
        sp = [r['sp'] for r in rs if r['sp'] and r['sp'] > 0]
        w = sum(1 for r in rs if r['fin'] == 1)
        pl = sum(1 for r in rs if r['fin'] == 2)
        sh = sum(1 for r in rs if r['fin'] == 3)
        n = len(rs)
        out.append(dict(hid=hid, hname=lastr['hname'], sex=lastr['sex'], age=horse_age(hid, last_date.isoformat()), oname=lastr['oname'],
                        last_date=lastr['date'], last_track=lastr['track'], days_since=latest - lastr['week'], starts=n, w=w, p=pl, s=sh,
                        win_pct=round(100.0 * w / n, 1) if n else 0, itm_pct=round(100.0 * (w + pl + sh) / n, 1) if n else 0,
                        earn=round(sum(r.get('earn') or 0 for r in rs)),
                        avg_speed=round(sum(sp) / len(sp), 1) if sp else None, top_speed=round(max(sp), 1) if sp else None,
                        last_speed=round(lastr['sp'], 1) if lastr['sp'] and lastr['sp'] > 0 else None,
                        bps=sum(r['bps'] or 0 for r in rs), last_racenum=lastr['racenum']))
    # several trainers searched: group by stable, in the order the names were typed, then most recently run first
    order = [t.strip().lower() for t in name.split(',') if t.strip()]
    if len(order) > 1:
        pos = lambda o: order.index(o.lower()) if o.lower() in order else len(order)
        out.sort(key=lambda x: (pos(x['oname']), x['oname'].lower(), -x['last_racenum']))
    else:
        out.sort(key=lambda x: -x['last_racenum'])
    for i, x in enumerate(out, 1):
        x['rank'] = i
    tot = dict(horses=len(out), starts=sum(x['starts'] for x in out), wins=sum(x['w'] for x in out), earn=sum(x['earn'] for x in out),
               bps=sum(x['bps'] for x in out))
    tot['win_pct'] = round(100.0 * tot['wins'] / tot['starts'], 1) if tot['starts'] else 0
    spd = [x['avg_speed'] for x in out if x['avg_speed']]
    tot['avg_speed'] = round(sum(spd) / len(spd), 1) if spd else None
    ages = {}
    for x in out:
        k = '2yo' if x['age'] is not None and x['age'] <= 2 else ('3yo' if x['age'] == 3 else '4yo and up')
        ages[k] = ages.get(k, 0) + 1
    tot['by_age'] = [dict(label=k, horses=ages[k]) for k in ('2yo', '3yo', '4yo and up') if k in ages]
    tot['top_earners'] = [dict(hid=x['hid'], hname=x['hname'], earn=x['earn']) for x in sorted(out, key=lambda x: -x['earn'])[:5]]
    tot['fastest'] = [dict(hid=x['hid'], hname=x['hname'], top=x['top_speed']) for x in sorted((x for x in out if x['top_speed']), key=lambda x: -x['top_speed'])[:5]]
    if rows:
        tot['summary'] = summarize(rows, latest, label='All current horses')
        tot['cats'] = speed_categories(rows)
    else:
        tot['summary'] = None
    sexn = {}
    for x in out:
        k = {'M': 'Colts and horses', 'G': 'Geldings', 'F': 'Fillies and mares'}.get(x['sex'], 'Other')
        sexn[k] = sexn.get(k, 0) + 1
    tot['by_sex'] = [dict(label=k, horses=v) for k, v in sexn.items()]
    tot['ran_30'] = sum(1 for x in out if x['days_since'] <= 30)
    tot['ran_60'] = sum(1 for x in out if x['days_since'] <= 60)
    tot['avg_days'] = round(sum(x['days_since'] for x in out) / len(out)) if out else None
    top5 = lambda lst, f: [dict(hid=x['hid'], hname=x['hname'], v=f(x)) for x in lst[:5]]
    tot['most_wins'] = top5(sorted(out, key=lambda x: (-x['w'], -x['starts'])), lambda x: '%d from %d' % (x['w'], x['starts']))
    tot['best_pct'] = top5(sorted((x for x in out if x['starts'] >= 5), key=lambda x: (-x['win_pct'], -x['starts'])), lambda x: '%.1f%% (%d)' % (x['win_pct'], x['starts']))
    tot['most_bps'] = top5(sorted(out, key=lambda x: -x['bps']), lambda x: x['bps'])
    tot['best_avg'] = top5(sorted((x for x in out if x['avg_speed'] and x['starts'] >= 3), key=lambda x: -x['avg_speed']), lambda x: x['avg_speed'])
    tot['per_start'] = round(tot['earn'] / tot['starts']) if tot['starts'] else 0
    return dict(kind='current', candidates=[], rows=out, summary=None, truncated=False,
                current=dict(name=name, days=int(days), year=last_date.year, year_first=week_to_date(year_start, aw, ad).isoformat(), first_date=week_to_date(latest - int(days) + 1, aw, ad).isoformat(),
                             last_date=last_date.isoformat(), totals=tot))


def horse_age(hid, race_date):
    """A horse's age on a race date. The first two digits of the horse ID are its crop (27 = foaled for
    2026 as a 2-year-old); everyone gets a year older on January 1."""
    h = str(int(hid))
    if len(h) < 9:
        return None
    return int(race_date[:4]) - 1997 - int(h[:2])


def speed_categories(rows):
    """Fastest figure in each group: age and sex, then distance type on each surface."""
    out = []

    def pick(label, test):
        best = None
        for r in rows:
            if test(r) and (best is None or r['sp'] > best['sp']):
                best = r
        out.append(dict(label=label, hid=best['hid'] if best else None, hname=best['hname'] if best else None,
                        sp=round(best['sp'], 2) if best else None, date=best['date'] if best else None))

    for r in rows:
        r['_age'] = horse_age(r['hid'], r['date'])
    out.append(dict(group='Age and sex'))
    for label, ages in (('2yo', (2,)), ('3yo', (3,)), ('4yo and up', None)):
        for sx, sexes in (('male', ('M', 'G')), ('female', ('F',))):
            pick('%s %s' % (label, sx),
                 lambda r, ages=ages, sexes=sexes: r['sex'] in sexes and r['_age'] is not None and
                 ((r['_age'] in ages) if ages else r['_age'] >= 4))
    for label, ages in (('2yo', (2,)), ('3yo', (3,)), ('4yo and up', None)):
        out.append(dict(group='%s by surface' % label))
        for surf, name in (('D', 'Dirt'), ('T', 'Turf'), ('P', 'Poly')):
            pick('%s %s' % (label, name.lower()),
                 lambda r, ages=ages, surf=surf: r['surf'] == surf and r['_age'] is not None and
                 ((r['_age'] in ages) if ages else r['_age'] >= 4))
    for surf, name in (('D', 'Dirt'), ('T', 'Turf'), ('P', 'Poly')):
        out.append(dict(group=name))
        for label, lo, hi in (('Sprint', 0, 8), ('Route', 8, 12), ('Long', 12, 99)):
            pick('%s %s' % (name, label.lower()),
                 lambda r, surf=surf, lo=lo, hi=hi: r['surf'] == surf and r['dist'] is not None and lo <= r['dist'] < hi)
    for r in rows:
        r.pop('_age', None)
    return out


def graded_races(store, first_week=None, last_week=None, two_yo=False, three_yo=False, limit=5000, graded=True, restrs=None):
    """One row per race in the date range, with the winner. Graded stakes only (GI, G2, G3) when graded is set;
    otherwise every race that meets the age options."""
    if restrs:
        w, p = ['r.restr IN (%s)' % ','.join('?' * len(restrs))], list(restrs)
    else:
        w, p = (["r.restr IN ('GI', 'G1', 'G2', 'G3')"] if graded else ['1=1']), []
    if first_week not in (None, ''):
        w.append('r.racenum >= ?'); p.append(int(first_week))
    if last_week not in (None, ''):
        w.append('r.racenum < ?'); p.append(int(last_week) + 1)
    w += race_filters(two_yo, three_yo, False)
    sql = """SELECT r.racenum, r.week, r.surf, r.restr, r.cond, r.dist, r.size, r.purse, r.age, r.raceid,
                    wn.hid, h.hname, wn.sp, o.oname
             FROM races r
             LEFT JOIN entries wn ON wn.id = (SELECT MIN(x.id) FROM entries x WHERE x.racenum = r.racenum AND x.fin = 1)
             LEFT JOIN horses h ON h.hid = wn.hid LEFT JOIN owners o ON o.id = wn.owner_id
             WHERE """ + ' AND '.join(w) + ' ORDER BY r.racenum DESC LIMIT ?'
    rows = _decorate(store, store.db.execute(sql, p + [int(limit)]).fetchall())
    by = {}
    for r in rows:
        key = r['restr'] if (graded or restrs) else (race_class(r['restr']) or 'Other')
        g = by.setdefault(key, dict(grade=key, races=0, purse=0))
        g['races'] += 1
        g['purse'] += r['purse'] or 0
    order = ('GI', 'G1', 'G2', 'G3', 'Stk') if (graded or restrs) else CLASSES + ('Other',)
    return dict(kind='graded', candidates=[], rows=rows, summary=None, truncated=len(rows) >= limit,
                graded=dict(races=len(rows), only_graded=bool(graded or restrs), by_grade=[by[k] for k in order if k in by]))


def stakes_this_year(store, limit=20000):
    """Every stakes race (Stk, GI, G2, G3) from January 1 of the newest race's year to the newest race."""
    latest = store.db.execute('SELECT MAX(week) FROM entries').fetchone()[0] or 0
    aw, ad = store.anchor()
    last = week_to_date(latest, aw, ad)
    jan1 = last.replace(month=1, day=1)
    first = latest - (last - jan1).days
    res = graded_races(store, first_week=first, last_week=latest, limit=limit,
                       restrs=('Stk', 'GI', 'G1', 'G2', 'G3'))
    res['graded'].update(year=last.year, first_date=jan1.isoformat(), last_date=last.isoformat())
    res['stats'] = stakes_stats(res['rows'])
    q = """SELECT {sel} FROM entries e JOIN races r ON r.racenum = e.racenum {j}
           WHERE e.racenum >= ? AND e.racenum < ? AND r.restr IN ('Stk','GI','G1','G2','G3') AND e.fin BETWEEN 1 AND 5 {w}
           GROUP BY {g} ORDER BY earn DESC LIMIT 10"""
    ea = 'CAST(ROUND(SUM(COALESCE(e.earn,0))) AS INTEGER) AS earn'
    res['stats']['earn_owners'] = [dict(x) for x in store.db.execute(q.format(
        sel='o.oname AS owner, SUM(e.fin = 1) AS wins, ' + ea, j='JOIN owners o ON o.id = e.owner_id', w="AND o.oname NOT LIKE '*%'", g='e.owner_id'), (first, latest + 1)).fetchall()]
    res['stats']['earn_horses'] = [dict(x) for x in store.db.execute(q.format(
        sel='h.hname AS hname, h.hid AS hid, SUM(e.fin = 1) AS wins, ' + ea, j='JOIN horses h ON h.hid = e.hid', w='', g='e.hid'), (first, latest + 1)).fetchall()]
    res['stats']['total_paid'] = store.db.execute(
        "SELECT CAST(ROUND(SUM(COALESCE(e.earn,0))) AS INTEGER) FROM entries e JOIN races r ON r.racenum = e.racenum WHERE e.racenum >= ? AND e.racenum < ? AND r.restr IN ('Stk','GI','G1','G2','G3')",
        (first, latest + 1)).fetchone()[0] or 0
    return res


def _age_group(code):
    c = (code or '').strip()
    sex = ' (fillies and mares)' if c[:1] == 'F' else ''
    a = c[2:]
    if a == '2':
        g = '2yo'
    elif a == '3':
        g = '3yo'
    elif a == '3+':
        g = '3 and up'
    elif a.endswith('+') and a[:-1].isdigit():
        g = '4 and up'
    else:
        g = 'Any age'
    return g + sex


def _dist_type(d):
    if d is None:
        return None
    return 'Sprint' if d < 8 else ('Long' if d >= 12 else 'Route')


def stakes_stats(rows):
    """Winning-speed and field statistics over a list of stakes races (one row per race, winner's figures)."""
    def agg(keyfn, order=None, label=None):
        g = {}
        for r in rows:
            k = keyfn(r)
            if k is not None:
                g.setdefault(k, []).append(r)
        out = []
        for k, rs in g.items():
            sp = [r['sp'] for r in rs if r['sp'] and r['sp'] > 0]
            out.append(dict(label=(label(k) if label else k), races=len(rs),
                            avg=round(sum(sp) / len(sp), 2) if sp else None, top=round(max(sp), 2) if sp else None,
                            field=round(sum(r['size'] or 0 for r in rs) / len(rs), 1),
                            purse=sum(r['purse'] or 0 for r in rs), _k=k))
        if order:
            out.sort(key=lambda x: order.index(x['_k']) if x['_k'] in order else 99)
        else:
            out.sort(key=lambda x: -x['races'])
        for x in out:
            x.pop('_k')
        return out

    surf = {'D': 'Dirt', 'T': 'Turf', 'P': 'Poly'}
    cls = lambda r: {'GI': 'G1', 'G1': 'G1', 'G2': 'G2', 'G3': 'G3', 'Stk': 'Stakes'}.get(r['restr'])
    sp_all = [r['sp'] for r in rows if r['sp'] and r['sp'] > 0]
    owners = {}
    for r in rows:
        if r.get('oname'):
            o = owners.setdefault(r['oname'], dict(owner=r['oname'], wins=0, purse=0, g=0))
            o['wins'] += 1
            o['purse'] += r['purse'] or 0
            o['g'] += r['restr'] in ('GI', 'G1', 'G2', 'G3')
    top = sorted((r for r in rows if r['sp'] and r['sp'] > 0), key=lambda r: -r['sp'])[:10]
    return dict(
        total=dict(races=len(rows), purse=sum(r['purse'] or 0 for r in rows),
                   avg=round(sum(sp_all) / len(sp_all), 2) if sp_all else None,
                   top=round(max(sp_all), 2) if sp_all else None),
        by_class=agg(cls, ['G1', 'G2', 'G3', 'Stakes']),
        by_age=agg(lambda r: _age_group(r['age']), [g + x for x in ('', ' (fillies and mares)') for g in ('2yo', '3yo', '3 and up', '4 and up', 'Any age')]),
        by_dist=agg(lambda r: _dist_type(r['dist']), ['Sprint', 'Route', 'Long']),
        by_surface=agg(lambda r: surf.get(r['surf'])),
        by_surf_dist=agg(lambda r: (surf.get(r['surf']), _dist_type(r['dist'])) if r['surf'] in surf and r['dist'] is not None else None,
                         label=lambda k: '%s %s' % (k[0], k[1].lower())),
        by_track=agg(lambda r: r['track'])[:10],
        owners=sorted(owners.values(), key=lambda o: (-o['wins'], -o['g']))[:10],
        fastest=[dict(hid=r['hid'], hname=r['hname'], sp=round(r['sp'], 2), cls={'GI': 'G1'}.get(r['restr'], r['restr']),
                      track=r['track'], race_no=r['race_no'], date=r['date'], dist=r['dist'], surf=r['surf'], racenum=r['racenum']) for r in top])


def owner_rankings(store, min_starts=25):
    """Every trainer with at least `min_starts` runners from January 1 of the newest race's year, with their record.
    """
    latest = store.db.execute('SELECT MAX(week) FROM entries').fetchone()[0] or 0
    aw, ad = store.anchor()
    last = week_to_date(latest, aw, ad)
    jan1 = last.replace(month=1, day=1)
    first = latest - (last - jan1).days
    sql = """SELECT o.oname AS oname, COUNT(*) AS starts, SUM(e.fin = 1) AS wins, SUM(e.fin = 2) AS seconds, SUM(e.fin = 3) AS thirds,
                    SUM(e.fin = 1 AND r.restr IN ('Stk', 'GI', 'G1', 'G2', 'G3')) AS stakes_wins,
                    SUM(e.fin = 1 AND r.restr IN ('GI', 'G1', 'G2', 'G3')) AS graded_wins,
                    SUM(COALESCE(e.bps, 0)) AS bps, SUM(COALESCE(e.earn, 0)) AS earn,
                    AVG(CASE WHEN e.sp > 0 THEN e.sp END) AS avg_sp, MAX(CASE WHEN e.sp > 0 THEN e.sp END) AS top_sp,
                    COUNT(DISTINCT e.hid) AS horses
             FROM entries e JOIN races r ON r.racenum = e.racenum JOIN owners o ON o.id = e.owner_id
             WHERE e.racenum >= ? AND e.racenum < ? AND o.oname NOT LIKE '*%'
             GROUP BY e.owner_id HAVING COUNT(*) >= ? ORDER BY wins DESC, 1.0 * SUM(e.fin = 1) / COUNT(*) DESC"""
    rows = []
    for i, r in enumerate(store.db.execute(sql, (first, latest + 1, int(min_starts))).fetchall(), 1):
        d = dict(r)
        d['rank'] = i
        d['earn'] = round(d['earn'] or 0)
        d['win_pct'] = round(100.0 * d['wins'] / d['starts'], 1)
        d['itm_pct'] = round(100.0 * (d['wins'] + d['seconds'] + d['thirds']) / d['starts'], 1)
        d['avg_sp'] = round(d['avg_sp'], 2) if d['avg_sp'] else None
        d['top_sp'] = round(d['top_sp'], 2) if d['top_sp'] else None
        rows.append(d)
    # Stakes leaders are counted over every trainer. Most stakes winners have far fewer than 100 runners,
    # so the minimum-runner list would leave nearly all of them out.
    lead_sql = """SELECT o.oname AS oname, COUNT(*) AS starts,
                    SUM(e.fin = 1 AND r.restr IN ('Stk', 'GI', 'G1', 'G2', 'G3')) AS stakes_wins,
                    SUM(e.fin = 1 AND r.restr IN ('GI', 'G1', 'G2', 'G3')) AS graded_wins
                 FROM entries e JOIN races r ON r.racenum = e.racenum JOIN owners o ON o.id = e.owner_id
                 WHERE e.racenum >= ? AND e.racenum < ? AND o.oname NOT LIKE '*%' AND r.restr IN ('Stk', 'GI', 'G1', 'G2', 'G3')
                 GROUP BY e.owner_id"""
    allst = [dict(x) for x in store.db.execute(lead_sql, (first, latest + 1)).fetchall()]
    top_h = [dict(x) for x in store.db.execute(
        """SELECT h.hname AS hname, h.hid AS hid, o.oname AS oname, COUNT(*) AS starts, SUM(e.fin = 1) AS wins, CAST(ROUND(SUM(COALESCE(e.earn, 0))) AS INTEGER) AS earn
           FROM entries e JOIN horses h ON h.hid = e.hid JOIN owners o ON o.id = e.owner_id
           WHERE e.racenum >= ? AND e.racenum < ? AND o.oname NOT LIKE '*%'
           GROUP BY e.hid ORDER BY earn DESC LIMIT 10""", (first, latest + 1)).fetchall()]
    leaders = dict(
        top_horses=top_h,
        stakes_wins=sorted((x for x in allst if x['stakes_wins']), key=lambda x: (-x['stakes_wins'], -x['graded_wins']))[:10],
        graded_wins=sorted((x for x in allst if x['graded_wins']), key=lambda x: (-x['graded_wins'], -x['stakes_wins']))[:10])
    return dict(kind='owners', candidates=[], rows=rows, summary=None, truncated=False, leaders=leaders,
                owners=dict(count=len(rows), year=last.year, first_date=jan1.isoformat(), last_date=last.isoformat(), min_starts=int(min_starts)))


def race_field(store, rows):
    """For one race: each runner with its previous and next start (speed, finish, days, trainer), plus a summary
    of who won last time, who won next time, the winner's trainer and horses that changed trainer."""
    db = store.db
    aw, ad = store.anchor()
    rn, wk = rows[0]['racenum'], rows[0]['week']
    q = ('SELECT e.racenum, e.week, e.fin, e.sp, COALESCE(o.oname, "") FROM entries e LEFT JOIN owners o ON o.id = e.owner_id '
         'WHERE e.hid = ? AND e.racenum %s ? ORDER BY e.racenum %s LIMIT 1')
    out = []
    for r in sorted(rows, key=lambda r: (r['fin'] or 99, r['ppos'] or 99)):
        pv = db.execute(q % ('<', 'DESC'), (r['hid'], rn)).fetchone()
        nx = db.execute(q % ('>', 'ASC'), (r['hid'], rn)).fetchone()
        d = dict(hid=r['hid'], hname=r['hname'], fin=r['fin'], ppos=r['ppos'], odds=r['odds'], sp=r['sp'], trainer=r['oname'], earn=r['earn'])
        if pv:
            d.update(prev_sp=pv[3], prev_fin=pv[2], prev_days=wk - pv[1], prev_trainer=pv[4])
            d['changed'] = bool(pv[4]) and pv[4] != r['oname']
        else:
            d['debut'] = True
        if nx:
            d.update(next_sp=nx[3], next_fin=nx[2], next_days=nx[1] - wk, next_date=week_to_date(nx[1], aw, ad).isoformat(), next_trainer=nx[4])
        out.append(d)
    won = [x for x in out if x['fin'] == 1]
    cpu = lambda n: (n or '').startswith('*')
    return dict(
        horses=out,
        winner=won[0] if won else None,
        won_prev=[x['hname'] for x in out if x.get('prev_fin') == 1],
        had_prev=sum(1 for x in out if 'prev_fin' in x),
        won_next=[x['hname'] for x in out if x.get('next_fin') == 1],
        ran_next=sum(1 for x in out if 'next_fin' in x),
        debuts=sum(1 for x in out if x.get('debut')),
        changes=[dict(hname=x['hname'], fin=x['fin'], frm=x['prev_trainer'], to=x['trainer'], prev_sp=x.get('prev_sp'), sp=x['sp'])
                 for x in out if x.get('changed') and not cpu(x['trainer']) and not cpu(x['prev_trainer'])],
    )


def race_form(store, racenum):
    """Everything the racing-form view needs for one race: the race itself and, for every runner, its whole
    record through the latest data (past performance lines, newest first) and career splits."""
    db = store.db
    rn = round(float(racenum), 6)
    rows = db.execute(SELECT + ' WHERE e.racenum = ? ORDER BY e.ppos ASC, e.fin ASC', (rn,)).fetchall()
    if not rows:
        raise ValueError('That race is not in your database.')
    rows = _decorate(store, rows)
    race = dict(rows[0])
    aw, ad = store.anchor()
    latest = db.execute('SELECT MAX(week) FROM entries').fetchone()[0] or 0
    this_track = race['track']
    this_dist = race['dist'] or 0
    horses = []
    for r in rows:
        hist = _decorate(store, db.execute(SELECT + ' WHERE e.hid = ? ORDER BY e.racenum DESC', (r['hid'],)).fetchall())
        lines, prev_week = [], None
        for h in hist:                                  # newest first: gap to the next older start
            lines.append(dict(racenum=h['racenum'], date=h['date'], week=h['week'], track=h['track'], race_no=h['race_no'],
                              surf=h['surf'], cond=h['cond'], dist=h['dist'], restr=h['restr'], cprice=h['cprice'],
                              purse=h['purse'], age=h['age'], size=h['size'], ppos=h['ppos'], fin=h['fin'], odds=h['odds'],
                              sp=h['sp'], earn=h['earn'], oname=h['oname'], this=(h['racenum'] == rn)))
        for i, l in enumerate(lines):
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            l['gap'] = (l['week'] - nxt['week']) if nxt else None

        def rec(xs):
            sp = [x['sp'] for x in xs if x['sp'] and x['sp'] > 0]
            return dict(starts=len(xs), w=sum(1 for x in xs if x['fin'] == 1), p=sum(1 for x in xs if x['fin'] == 2),
                        s=sum(1 for x in xs if x['fin'] == 3), earn=sum((x['earn'] or 0) for x in xs),
                        top=max(sp) if sp else None)
        years = {}
        for l in lines:
            years.setdefault(l['date'][:4], []).append(l)
        splits = [
            ('Fast dirt', [l for l in lines if l['surf'] == 'D' and l['cond'] == 'F']),
            ('Off dirt', [l for l in lines if l['surf'] == 'D' and l['cond'] != 'F']),
            ('Synth', [l for l in lines if l['surf'] == 'P']),
            ('Turf', [l for l in lines if l['surf'] == 'T']),
            ('This dist', [l for l in lines if l['dist'] and abs(l['dist'] - this_dist) <= 1.0]),
            ('This track', [l for l in lines if l['track'] == this_track]),
        ]
        horses.append(dict(
            hid=r['hid'], hname=r['hname'], sex=r['sex'], tag=r['tag'], ppos=r['ppos'], odds=r['odds'], fin=r['fin'],
            sp=r['sp'], trainer=r['oname'], age=horse_age(r['hid'], race['date']),
            age_now=horse_age(r['hid'], week_to_date(latest, aw, ad).isoformat()),
            life=rec(lines),
            years=[dict(year=y, **rec(years[y])) for y in sorted(years, reverse=True)[:3]],
            splits=[dict(label=k, **rec(v)) for k, v in splits],
            lines=lines))
    race['runners'] = len(rows)
    for k in ('hname', 'hid', 'tag', 'sex', 'fin', 'ppos', 'sp', 'odds', 'oname', 'oname15', 'oname12', 'oname10', 'bps', 'earn', 'id'):
        race.pop(k, None)
    return dict(race=race, horses=horses, latest=week_to_date(latest, aw, ad).isoformat())
