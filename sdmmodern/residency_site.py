"""The public leaderboard site: one link, tabs across the top, real numbers from the database.

Tabs: The Tote, Claiming Poll, Top 10 Poll, Stakes Room, Claiming Crown, Member Cards, Week in the Books.
Every tab is its own page with plain links (no script except the Tote's row-open). Look, dates and sort come from the link.
Nothing here contacts any other server. Called from residency_portal.live_page.
"""
import datetime, math
from . import residency as RS
from . import residency_portal as P

_X = P._X
TABS = (('tote', 'The Tote'), ('poll', 'Claiming Poll'), ('top10', 'Top 10 Poll'), ('stakes', 'Stakes Room'),
        ('crown', 'Claiming Crown'), ('cards', 'Member Cards'), ('week', 'Week in the Books'))
SITE_BOARDS = tuple(k for k, _ in TABS)
GRADES = ('All', 'G1', 'G2', 'G3', 'Stk')
CROWN_SORTS = ('itm', 'win', 'w')
CROWN_NAMES = {'itm': 'ITM %', 'win': 'Win %', 'w': 'Wins'}
RANGE_NAMES = (('latest', 'Newest week'), ('this_week', 'This week'), ('last_4', 'Last 4 weeks'), ('season', 'Season'))
LOOK_NAMES = (('auto', 'Auto'), ('dark', 'Dark'), ('light', 'Light'), ('paper', 'Paper'))
TIER_GOLD, TIER_SILVER = 70, 60
FEED_MAX = 100

SITE_CSS = (
    ':root{--good:#1a7f4b;--warn:#9a6200}'
    '@media (prefers-color-scheme:dark){html[data-look="auto"]{--good:#4cc38a;--warn:#f0b24b}}'
    'html[data-look="dark"]{--good:#4cc38a;--warn:#f0b24b}html[data-look="paper"]{--good:#256b3a;--warn:#8a5a00}'
    'body{padding:0 0 40px}.site{max-width:1180px;margin:0 auto;padding:0 max(16px,env(safe-area-inset-right,0px)) 0 max(16px,env(safe-area-inset-left,0px))}'
    'header.top{background:var(--panel);border-bottom:1px solid var(--line);padding-top:env(safe-area-inset-top,0px)}'
    'header.top .bar{max-width:1180px;margin:0 auto;display:flex;flex-wrap:wrap;gap:6px 18px;align-items:flex-end;justify-content:space-between;padding:10px max(16px,env(safe-area-inset-right,0px)) 0 max(16px,env(safe-area-inset-left,0px))}'
    '.brand{font:700 26px/1 var(--disp);letter-spacing:.04em;text-transform:uppercase;padding-bottom:10px}'
    '.brand small{display:block;font:500 12px var(--body);letter-spacing:.14em;color:var(--mute);margin-top:3px;text-transform:uppercase}'
    'nav.tabs{display:flex;flex-wrap:wrap;gap:2px}nav.tabs a{font:600 15px var(--disp);letter-spacing:.08em;text-transform:uppercase;color:var(--mute);text-decoration:none;padding:9px 12px;border-bottom:3px solid transparent}'
    'nav.tabs a:hover{color:var(--ink)}nav.tabs a[aria-current=page]{color:var(--ink);border-color:var(--accent)}'
    '.opts{display:flex;flex-wrap:wrap;gap:8px 22px;align-items:center;padding:12px 0}.opts .seg a{padding:4px 9px}'
    '.opts label{font:600 12px var(--disp);letter-spacing:.12em;text-transform:uppercase;color:var(--mute);margin-right:6px}'
    '.opts>div{display:flex;align-items:center;flex-wrap:wrap}'
    'h2.bt{font:700 30px/1 var(--disp);text-transform:uppercase;letter-spacing:.05em;margin:0}'
    '.poll details.pd{border-bottom:1px solid var(--line);background:var(--panel)}'
    '.poll details.pd>summary{display:grid;grid-template-columns:78px minmax(0,1fr) 120px;gap:6px 16px;align-items:center;padding:14px 16px;cursor:pointer;list-style:none}'
    '.poll details.pd>summary::-webkit-details-marker{display:none}.poll details.pd>summary:hover{background:var(--panel2)}'
    '.pn{font:700 64px/.9 var(--disp);color:var(--led);text-align:center}'
    '.mv{display:block;font:700 15px var(--disp);letter-spacing:.06em;text-align:center;margin-top:2px}'
    '.up{color:var(--good)}.dn{color:var(--w)}.eq,.nw{color:var(--mute)}.nw{color:var(--sh)}'
    '.pnm{font:700 30px/1.05 var(--disp);text-transform:uppercase;letter-spacing:.03em}.rec{color:var(--mute);font-size:13px;margin-top:3px}'
    '.prt{text-align:right}.prt b{display:block;font:700 40px/1 var(--disp);font-variant-numeric:tabular-nums}.prt span{font:600 11px var(--disp);letter-spacing:.14em;text-transform:uppercase;color:var(--mute)}'
    '.badge{display:inline-block;font:700 11px var(--disp);letter-spacing:.12em;text-transform:uppercase;padding:2px 7px;border-radius:3px;margin-left:8px;vertical-align:4px;background:var(--accent);color:var(--accent-ink)}'
    '.comp{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px 16px;padding:2px 16px 16px 94px}'
    '.comp .k{display:flex;justify-content:space-between;font:600 12px var(--disp);letter-spacing:.1em;text-transform:uppercase;color:var(--mute)}'
    '.meter{height:7px;background:var(--line);border-radius:2px;margin-top:4px;overflow:hidden}.meter i{display:block;height:100%;background:var(--led)}'
    '.votes{padding:12px 16px 4px;font:600 13px var(--disp);letter-spacing:.14em;text-transform:uppercase;color:var(--mute)}'
    'table.also td{font-size:17px}'
    '.call{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;padding:14px 16px;border-bottom:1px solid var(--line);background:var(--panel2)}'
    '.call>div{border:1px solid var(--line);border-radius:5px;padding:10px 12px;background:var(--panel)}.call b{display:block;font:600 12px var(--disp);letter-spacing:.14em;text-transform:uppercase;color:var(--mute)}'
    '.call span{font:700 22px var(--disp);text-transform:uppercase}.call small{display:block;color:var(--mute);font-size:12px}'
    'details.algo{padding:12px 16px 16px;border-top:1px solid var(--line);font-size:14px;color:var(--mute)}'
    'details.algo summary{cursor:pointer;font:600 15px var(--disp);letter-spacing:.1em;text-transform:uppercase;color:var(--ink)}'
    'details.algo table{min-width:0;margin:10px 0}details.algo td,details.algo th{font:400 14px var(--body);text-align:left;white-space:normal;letter-spacing:0;padding:6px 8px}'
    '.ladder{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;padding:14px 16px;border-bottom:1px solid var(--line)}'
    '.lad{border:1px solid var(--line);border-radius:5px;padding:10px 12px;background:var(--panel2)}'
    '.lad .nm{display:flex;align-items:center;gap:8px;font:600 18px var(--disp);text-transform:uppercase;letter-spacing:.04em}'
    '.pips{display:flex;gap:5px;margin-top:8px;flex-wrap:wrap}.pip{font:700 15px var(--disp);letter-spacing:.04em;padding:2px 8px;border-radius:3px;border:1px solid var(--line);background:var(--panel)}'
    '.pip b{color:var(--led);margin-left:5px}.g1{border-left:4px solid var(--w)}.g2{border-left:4px solid var(--warn)}.g3{border-left:4px solid var(--sh)}.gs{border-left:4px solid var(--mute)}'
    '.win{display:grid;grid-template-columns:64px minmax(0,1fr) auto;gap:6px 14px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}'
    '.win .gr{font:700 22px var(--disp);text-align:center;padding:6px 0;border-radius:3px;color:#fff}'
    '.gr.G1{background:#c8362b}.gr.G2{background:#9a6200}.gr.G3{background:#2a5bd7}.gr.Stk{background:#566a62}'
    '.win .h{font:600 22px var(--disp);text-transform:uppercase;letter-spacing:.03em}.win .m{color:var(--mute);font-size:13px}'
    '.win .p{font:600 22px var(--disp);text-align:right;font-variant-numeric:tabular-nums}'
    '.ctl{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}'
    '.ctl label{font:600 13px var(--disp);letter-spacing:.1em;text-transform:uppercase;color:var(--mute)}'
    'svg text{font-family:var(--disp);fill:var(--mute)}'
    '.tcg{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:16px;padding:16px}'
    '.tc{border-radius:10px;background:linear-gradient(170deg,var(--panel2),var(--panel));border:2px solid var(--c);padding:14px}'
    '.tc .top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}'
    '.tc .no{font:700 54px/.9 var(--disp);color:var(--c)}.tc .tnm{font:700 24px/1 var(--disp);text-transform:uppercase;letter-spacing:.03em;overflow-wrap:anywhere}'
    '.tc .tier{font:700 11px var(--disp);letter-spacing:.16em;text-transform:uppercase;color:var(--mute)}'
    '.tc .trt{font:700 34px/1 var(--disp);color:var(--led);text-align:right}.tc .trt small{display:block;font:600 11px var(--disp);letter-spacing:.14em;color:var(--mute)}'
    '.tc svg{display:block;margin:6px auto 2px;max-width:100%}'
    '.tc .st{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;border-top:1px solid var(--line);padding-top:8px;text-align:center}'
    '.tc .st b{display:block;font:700 22px/1 var(--disp)}.tc .st span{font:600 10.5px var(--disp);letter-spacing:.12em;text-transform:uppercase;color:var(--mute)}'
    '.foot{color:var(--mute);font-size:12px;padding:14px 0}'
    '@media (max-width:640px){.poll details.pd>summary{grid-template-columns:56px minmax(0,1fr)}.prt{grid-column:2;text-align:left;display:flex;gap:10px;align-items:baseline}'
    '.prt b{font-size:30px}.pn{font-size:46px}.pnm{font-size:24px}.comp{padding-left:16px}.win{grid-template-columns:52px minmax(0,1fr)}.win .p{grid-column:2;text-align:left}}')

# the week card brings its own dark card styling; drop its page-level rules so the shell controls the page
_WEEK_CARD_CSS = P.WEEK_CSS.replace('html,body{margin:0;background:#0a1310}body{padding:14px;font-family:var(--body)}', '')


def _num(v, d=1):
    return ('%.' + str(d) + 'f') % v


def _res(roster):
    return roster.get('residency') or (roster.get('code') or '').upper()


def _order(roster):
    return [m['name'] for m in roster.get('members', [])]


def _color(roster, name):
    o = _order(roster)
    return P.WEEK_COLORS[o.index(name) % len(P.WEEK_COLORS)] if name in o else P.WEEK_COLORS[0]


def _silk(color, small=False):
    return '<span class="silk" style="background:%s%s"></span>' % (color, ';width:9px;height:14px' if small else '')


def _board(title, sub, extra_head='', inner=''):
    return ('<div class="board"><div class="bh"><div><h2 class="bt">%s</h2><div class="sub">%s</div></div>%s</div>%s</div>' % (_X(title), sub, extra_head, inner))


# ---------------------------------------------------------------- polls
_WHAT = dict(
    win='Win rate, pulled toward the field average until a stable has run 8 or so races',
    itm='Win, place or show rate, same pull toward the average',
    fin='3 for a win, 2 for a second, 1 for a third, per race run',
    form='Finish quality in the last quarter of the range. A member who did not run then gets a middle score',
    qual='Stakes wins count 1, graded wins 2 more, stakes places and shows 0.4, all stables combined')


def _mv(r):
    if r['previous_rank'] is None:
        return '<span class="mv nw">NEW</span>'
    m = r['move'] or 0
    if m > 0:
        return '<span class="mv up">&#9650; %d</span>' % m
    if m < 0:
        return '<span class="mv dn">&#9660; %d</span>' % -m
    return '<span class="mv eq">&mdash;</span>'


def poll_inner(data, mode):
    """mode: 'claiming' or 'overall'. Returns (title, sub, body html)."""
    d = data[mode]
    claim = mode == 'claiming'
    title = 'Claiming Trainer Poll' if claim else 'Top 10 Poll'
    days = data['period']['days']
    sub = '%s to %s (%d days) &middot; %s' % (_X(data['period']['from']), _X(data['period']['to']), days,
                                              'claiming races only' if claim else 'all races, stakes and graded form counts here')
    rows = d['rows']
    top, also = rows[:d['top']], rows[d['top']:d['top'] + d['also']]
    if not rows:
        return title, sub, '<div class="none">No member has enough starts in these dates yet (%d needed).</div>' % d['min_starts']
    movers = [r for r in rows if r['move'] is not None]
    climb = max(movers, key=lambda r: r['move'], default=None)
    small = min(top, key=lambda r: r['starts'])
    labels = data['labels']
    weights = data['weights'][mode]

    def row(r):
        badges = ''
        if r is small and r['starts'] <= 12:
            badges += '<span class="badge">Light schedule</span>'
        if climb is not None and r is climb and r['move'] >= 2:
            badges += '<span class="badge">Climber</span>'
        stakes = ''
        comp = ''.join('<div><div class="k"><span>%s</span><span>%d%%</span></div><div class="meter"><i style="width:%d%%"></i></div></div>'
                       % (_X(labels[k]), weights[k], r['parts'][k]) for k in weights)
        return ('<details class="pd"><summary><div><div class="pn">%d</div>%s</div><div style="min-width:0"><div class="pnm">%s%s</div>'
                '<div class="rec">%d %s &middot; %d-%d-%d from %d %s &middot; %s%% wins &middot; %s%% ITM</div></div>'
                '<div class="prt"><b>%s</b><span>rating</span></div></summary><div class="comp">%s</div></details>'
                % (r['rank'], _mv(r), _X(r['name']), badges, r['stables'], 'stable' if r['stables'] == 1 else 'stables', r['wins'], r['seconds'], r['thirds'],
                   r['starts'], 'claiming starts' if claim else 'starts', _num(r['win_pct']), _num(r['itm_pct']), _num(r['rating']), comp))
    call = ('<div class="call">%s<div><b>Fewest races in the top %d</b><span>%s</span><small>%d starts across %d %s, rated %s</small></div>'
            '<div><b>In the field</b><span>%d members</span><small>%d+ starts to qualify</small></div></div>'
            % (('<div><b>Biggest climb</b><span>%s</span><small>up %d to No. %d</small></div>' % (_X(climb['name']), climb['move'], climb['rank'])) if climb is not None and climb['move'] > 0 else '',
               d['top'], _X(small['name']), small['starts'], small['stables'], 'stable' if small['stables'] == 1 else 'stables', _num(small['rating']), d['field'], d['min_starts']))
    al = ''.join('<tr><td><span class="rank" style="font-size:24px;width:30px">%d</span> <span class="who" style="font-size:19px">%s</span> <span class="mut">%d %s</span></td><td>%d-%d-%d</td><td>%d</td><td>%s</td></tr>'
                 % (r['rank'], _X(r['name']), r['stables'], 'stable' if r['stables'] == 1 else 'stables', r['wins'], r['seconds'], r['thirds'], r['starts'], _num(r['rating'])) for r in also)
    cfg = P.POLL_CFG[mode]
    vcap = int(round(max(6, cfg['vcap'] * min(1, days / 28.0))))
    what = dict(_WHAT, vol='Rises with races run and flattens at %d for this range, so a member with a small barn can still reach the top' % vcap)
    wt = ''.join('<tr><td>%s</td><td>%d%%</td><td>%s</td></tr>' % (_X(labels[k]), weights[k], _X(what[k])) for k in weights)
    algo = ('<details class="algo"><summary>How the rating works</summary><p>Each member gets %s scores, using the results of all their stables together, from 0 to 1, '
            'multiplied by these weights and added up to a rating out of 100.</p><table><thead><tr><th>Part</th><th>Weight</th><th>What it measures</th></tr></thead><tbody>%s</tbody></table>'
            '<p>Ties go to the member with more wins. Arrows compare with the poll for the dates just before, of the same length.</p></details>' % ('five' if claim else 'six', wt))
    body = (call + '<div class="poll">' + ''.join(row(r) for r in top) + '</div>'
            + (('<div class="votes">Also receiving votes</div><div class="scroll"><table class="also"><thead><tr><th>Stable</th><th>W-P-S</th><th>Starts</th><th>Rating</th></tr></thead><tbody>%s</tbody></table></div>' % al) if al else '')
            + algo)
    return title, sub, body


# ---------------------------------------------------------------- stakes
def stakes_wins(store, roster, d_from, d_to):
    """Every stakes or graded win by a roster stable in the dates, newest first. Stable ownership follows Joined and Left dates."""
    d0, d1 = P._date(d_from, 'From'), P._date(d_to, 'To')
    w0, w1 = P.week_of(store, d0), P.week_of(store, d1)
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
    out, ids = [], sorted(key_of_id)
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        sql = ('SELECT e.owner_id, e.week, r.restr, r.dist, r.surf, r.purse, r.age, r.loc_etc, h.hname, e.earn FROM entries e JOIN races r ON r.racenum = e.racenum '
               'LEFT JOIN horses h ON h.hid = e.hid WHERE e.owner_id IN (%s) AND e.week BETWEEN ? AND ? AND e.fin = 1 AND r.restr IN %s'
               % (','.join('?' * len(part)), P.STAKES_SQL))
        for oid, wk, restr, dist, surf, purse, age, loc, hname, earn in db.execute(sql, part + [w0, w1]):
            day = P.date_of(store, wk).isoformat()
            seen = set()
            for k in key_of_id[oid]:
                line = next((x for x in lines[k] if (not x['joined'] or x['joined'] <= day) and (not x['left'] or day <= x['left'])), None)
                if line is None or line['member_key'] in seen:
                    continue
                seen.add(line['member_key'])
                out.append(dict(member=line['member'], stable=line.get('name') or k, date=day, grade='G1' if restr in ('GI', 'G1') else restr,
                                horse=hname or 'Unnamed', dist=dist, surf=surf or '', purse=int(purse or earn or 0), age=age or '', loc=(loc or '').strip()))
    out.sort(key=lambda w: (w['date'], w['purse']), reverse=True)
    return out


def stakes_inner(store, roster, d_from, d_to, grade, base):
    wins = stakes_wins(store, roster, d_from, d_to)
    names = []
    for m in roster.get('members', []):
        if m['name'] not in names:
            names.append(m['name'])
    by = {n: [w for w in wins if w['member'] == n] for n in names}
    lad = []
    for n in sorted(names, key=lambda n: (-len(by[n]), n.lower())):
        w = by[n]
        c = lambda g: sum(1 for x in w if x['grade'] == g)
        cls = 'g1' if c('G1') else 'g2' if c('G2') else 'g3' if c('G3') else 'gs'
        lad.append('<div class="lad %s"><div class="nm">%s%s</div><div class="pips">%s</div><div class="mut" style="margin-top:6px">%d stakes win%s &middot; $%s in purses</div></div>'
                   % (cls, _silk(_color(roster, n)), _X(n), ''.join('<span class="pip">%s<b>%d</b></span>' % (g, c(g)) for g in ('G1', 'G2', 'G3', 'Stk')),
                   len(w), '' if len(w) == 1 else 's', format(sum(x['purse'] for x in w), ',')))
    shown = [w for w in wins if grade == 'All' or w['grade'] == grade][:FEED_MAX]
    feed = []
    for w in shown:
        meta = ' &middot; '.join(x for x in [_X(w['member']) + ' (' + _X(w['stable']) + ')' if w['stable'] and w['stable'] != w['member'] else _X(w['member']),
                                             _X(w['loc']) if w['loc'] and len(w['loc']) <= 24 else '',
                                             ('%g %s' % (w['dist'], _X(w['surf']))).strip() if w['dist'] else _X(w['surf']), _X(w['age']), _X(w['date'])] if x)
        feed.append('<div class="win"><div class="gr %s">%s</div><div><div class="h">%s</div><div class="m">%s%s</div></div><div class="p">$%s</div></div>'
                    % (_X(w['grade']), _X(w['grade']), _X(w['horse']), _silk(_color(roster, w['member']), True), meta, format(w['purse'], ',')))
    segs = '<div class="seg" role="group" aria-label="Grade">%s</div>' % ''.join('<a href="%sgrade=%s" %s>%s</a>' % (base, g, 'aria-current="true"' if g == grade else '', g) for g in GRADES)
    feed_html = ''.join(feed) or '<div class="none">No wins at this grade in these dates.</div>'
    note = ('<div class="foot" style="padding:12px 16px">Showing the newest %d.</div>' % FEED_MAX) if len(wins) > FEED_MAX and grade == 'All' else ''
    return 'Stakes Room', 'Every stakes and graded win in the dates, newest first', segs, '<div class="ladder">' + ''.join(lad) + '</div><div class="feed">' + feed_html + '</div>' + note


# ---------------------------------------------------------------- claiming crown
def crown_inner(store, roster, d_from, d_to, sort, minimum, base):
    tl = P._tally(store, roster, d_from, d_to, 'all')
    rows = []
    for a in tl.values():
        n = a['cs']
        if n < minimum or n == 0:
            continue
        w, p, s = a['cfin'][0], a['cfin'][1], a['cfin'][2]
        rows.append(dict(name=a['name'], n=n, w=w, p=p, s=s, win=P._pctf(w, n), itm=P._pctf(w + p + s, n)))
    key = {'itm': lambda r: (r['itm'], r['n']), 'win': lambda r: (r['win'], r['n']), 'w': lambda r: (r['w'], r['n'])}[sort]
    rows.sort(key=lambda r: (tuple(-x for x in key(r)), r['name'].lower()))
    W, H, L, B, T, R = 520, 300, 44, 34, 14, 16
    xmax = max(30, int(math.ceil(max([r['win'] for r in rows] or [0]) / 10.0)) * 10)
    ymax = max(60, int(math.ceil(max([r['itm'] for r in rows] or [0]) / 20.0)) * 20)
    xs = lambda v: L + (W - L - R) * v / float(xmax)
    ys = lambda v: H - B - (H - B - T) * v / float(ymax)
    g = ''
    for v in range(0, ymax + 1, 20):
        g += '<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="var(--line)"/><text x="%d" y="%.1f" text-anchor="end" font-size="12">%d%%</text>' % (L, W - R, ys(v), ys(v), L - 6, ys(v) + 4, v)
    for v in range(0, xmax + 1, 10):
        g += '<text x="%.1f" y="%d" text-anchor="middle" font-size="12">%d%%</text>' % (xs(v), H - B + 16, v)
    g += ('<text x="%.1f" y="%d" text-anchor="middle" font-size="12">win %% in claiming races</text>'
          '<text x="12" y="%.1f" font-size="12" transform="rotate(-90 12 %.1f)" text-anchor="middle">ITM %%</text>' % ((L + W - R) / 2.0, H - 4, (T + H - B) / 2.0, (T + H - B) / 2.0))
    for r in rows:
        rad = min(22, 6 + r['n'] / 3.2)
        c = _color(roster, r['name'])
        g += ('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" fill-opacity=".75" stroke="var(--panel)" stroke-width="2"/>'
              '<text x="%.1f" y="%.1f" font-size="13" text-anchor="%s" style="fill:var(--ink);font-weight:600">%s</text>'
              % (xs(r['win']), ys(r['itm']), rad, c, (xs(r['win']) - rad - 4) if xs(r['win']) > W - 110 else (xs(r['win']) + rad + 4), ys(r['itm']) + 4,
                 'end' if xs(r['win']) > W - 110 else 'start', _X(r['name'])))
    body = ''.join('<tr><td><span class="rank">%d</span><span class="who">%s%s</span></td><td>%d</td><td class="wcol">%d</td><td class="pcol">%d</td><td class="scol">%d</td>'
                   '<td style="min-width:120px"><div class="stack" role="img" aria-label="%d wins, %d seconds, %d thirds"><i style="width:%.1f%%;background:var(--w)"></i><i style="width:%.1f%%;background:var(--pl)"></i><i style="width:%.1f%%;background:var(--sh)"></i></div></td>'
                   '<td>%.1f%%</td><td style="color:var(--led);font-weight:700">%.1f%%</td></tr>'
                   % (i + 1, _silk(_color(roster, r['name'])), _X(r['name']), r['n'], r['w'], r['p'], r['s'], r['w'], r['p'], r['s'],
                      P._pctf(r['w'], r['n']), P._pctf(r['p'], r['n']), P._pctf(r['s'], r['n']), r['win'], r['itm']) for i, r in enumerate(rows))
    if not rows:
        body = '<tr><td colspan="8" style="text-align:left;padding:20px 16px;font:400 14px var(--body);color:var(--mute)">No member has %d claiming starts yet. Pick a lower minimum.</td></tr>' % minimum
    segs = '<div class="seg" role="group" aria-label="Rank by">%s</div>' % ''.join('<a href="%ssort=%s&amp;min=%d" %s>%s</a>' % (base, k, minimum, 'aria-current="true"' if k == sort else '', CROWN_NAMES[k]) for k in CROWN_SORTS)
    ctl = '<div class="ctl"><label>Minimum claiming starts</label><div class="seg" role="group" aria-label="Minimum claiming starts">%s</div><span class="mut">Keeps a 2-for-3 stable from topping the board.</span></div>' % ''.join(
        '<a href="%ssort=%s&amp;min=%d" %s>%d</a>' % (base, sort, m, 'aria-current="true"' if m == minimum else '', m) for m in MIN_CHOICES)
    chart = ('<div style="padding:10px 16px 0"><svg viewBox="0 0 %d %d" style="display:block;width:100%%;max-width:640px;margin:0 auto" role="img" aria-label="Win percent against in-the-money percent, one bubble per member, sized by starts">%s</svg></div>' % (W, H, g)) if rows else ''
    tbl = ('<div class="scroll"><table><thead><tr><th>Member</th><th>Starts</th><th class="wcol">Win</th><th class="pcol">Place</th><th class="scol">Show</th><th>W / P / S</th><th>Win %</th><th>ITM %</th></tr></thead><tbody>'
           + body + '</tbody></table></div>')
    return 'Claiming Crown', 'Claiming races only &middot; who wins and hits the board most', segs, ctl + chart + tbl


MIN_CHOICES = (0, 3, 6, 12, 20, 30)


def default_min(days):
    return max(3, int(round(12 * min(1, days / 28.0)))) if days < 28 else 12


# ---------------------------------------------------------------- member cards
def _radar(parts, keys, labels, color):
    S, cx, cy, r, n = 170, 130, 89, 60, len(keys)
    pt = lambda i, v: (cx + r * v * math.sin(2 * math.pi * i / n), cy - r * v * math.cos(2 * math.pi * i / n))
    g = ''
    for v in (.33, .66, 1):
        g += '<polygon points="%s" fill="none" stroke="var(--line)"/>' % ' '.join('%.1f,%.1f' % pt(i, v) for i in range(n))
    for i, k in enumerate(keys):
        e, l = pt(i, 1), pt(i, 1.28)
        g += ('<line x1="%d" y1="%d" x2="%.1f" y2="%.1f" stroke="var(--line)"/><text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" style="letter-spacing:.06em">%s</text>'
              % (cx, cy, e[0], e[1], l[0], l[1] + 4, _X(labels[k].upper())))
    vals = [parts[k] / 100.0 for k in keys]
    g += '<polygon points="%s" fill="%s" fill-opacity=".28" stroke="%s" stroke-width="2.5" stroke-linejoin="round"/>' % (' '.join('%.1f,%.1f' % pt(i, v) for i, v in enumerate(vals)), color, color)
    g += ''.join('<circle cx="%.1f" cy="%.1f" r="3" fill="%s"/>' % (pt(i, v) + (color,)) for i, v in enumerate(vals))
    desc = ', '.join('%s %d' % (labels[k], parts[k]) for k in keys)
    return '<svg viewBox="0 0 260 %d" width="100%%" style="max-width:260px" role="img" aria-label="Rating parts: %s">%s</svg>' % (S + 8, _X(desc), g)


def cards_inner(data, roster):
    d = data['claiming']
    keys = list(data['weights']['claiming'].keys())
    rows = d['rows'][:6]
    if not rows:
        return 'Member Cards', 'Claiming Poll top six, with the parts of each rating', '<div class="none">No member has enough claiming starts in these dates yet.</div>'
    cards = []
    for r in rows:
        col = _color(roster, r['name'])
        tier = 'Gold' if r['rating'] >= TIER_GOLD else 'Silver' if r['rating'] >= TIER_SILVER else 'Bronze'
        cards.append('<div class="tc" style="--c:%s"><div class="top"><div><div class="tier">%s &middot; %d %s</div><div class="tnm">%s</div></div><div class="trt">%s<small>RATING</small></div></div>%s'
                     '<div class="st"><div><b>%d-%d-%d</b><span>W-P-S</span></div><div><b>%s%%</b><span>Win %%</span></div><div><b>%s%%</b><span>ITM %%</span></div></div></div>'
                     % (col, tier, r['stables'], 'stable' if r['stables'] == 1 else 'stables', _X(r['name']), _num(r['rating']), _radar(r['parts'], keys, data['labels'], col),
                        r['wins'], r['seconds'], r['thirds'], _num(r['win_pct']), _num(r['itm_pct'])))
    return ('Member Cards', 'Claiming Poll top six, with the parts of each rating',
            '<div class="tcg">' + ''.join(cards) + '</div><div class="foot" style="padding:0 16px 14px">Gold is %d and up, Silver %d and up, then Bronze.</div>' % (TIER_GOLD, TIER_SILVER))


# ---------------------------------------------------------------- the page
def _url(code, board, look, rng, **kw):
    u = '/api/residency/live?code=%s&amp;board=%s&amp;look=%s&amp;range=%s' % (code, board, look, rng)
    for k, v in kw.items():
        if v not in (None, '', False):
            u += '&amp;%s=%s' % (k, v)
    return u


def site_document(store, roster, d_from, d_to, rule, code, board, look, rng, sort='pts', grade='All', minimum=None, bare=False):
    f, t = P._date(d_from, 'From'), P._date(d_to, 'To')
    days = (t - f).days + 1
    keep = dict(bare='1' if bare else None)
    title_name = dict(TABS)[board]
    base = _url(code, board, look, rng, **keep) + '&amp;'
    seg = ''
    if board == 'tote':
        sub, seg_html, inner = P.tote_inner(store, roster, d_from, d_to, rule, sort, base)
        content = _board('Standings', sub, '<div class="seg" role="group" aria-label="Sort by">%s</div>' % seg_html, inner)
        script = '<script>%s</script>' % P.TOTE_JS
    else:
        script = ''
        if board in ('poll', 'top10'):
            data = P.polls(store, roster, d_from, d_to, 'all')
            title, sub, inner = poll_inner(data, 'claiming' if board == 'poll' else 'overall')
            content = _board(title, sub, '', inner)
        elif board == 'stakes':
            title, sub, head, inner = stakes_inner(store, roster, d_from, d_to, grade, base)
            content = _board(title, sub, head, inner)
        elif board == 'crown':
            mn = default_min(days) if minimum is None else minimum
            title, sub, head, inner = crown_inner(store, roster, d_from, d_to, sort if sort in CROWN_SORTS else 'itm', mn, base)
            content = _board(title, sub, head, inner)
        elif board == 'cards':
            data = P.polls(store, roster, d_from, d_to, 'all')
            title, sub, inner = cards_inner(data, roster)
            content = _board(title, sub, '', inner)
        else:
            content = '<div style="max-width:560px;margin:0 auto">' + P.week_page(store, roster, d_from, d_to, rule).replace('class="wk"', 'class="wk" style="margin-top:0"', 1) + '</div>'
    res = _res(roster)
    nav = ''.join('<a href="%s" %s>%s</a>' % (_url(code, k, look, rng, **keep), 'aria-current="page"' if k == board else '', _X(n)) for k, n in TABS)
    ropts = ''.join('<a href="%s" %s>%s</a>' % (_url(code, board, look, k, **keep), 'aria-current="true"' if k == rng else '', n) for k, n in RANGE_NAMES)
    lopts = ''.join('<a href="%s" %s>%s</a>' % (_url(code, board, k, rng, **keep), 'aria-current="true"' if k == look else '', n) for k, n in LOOK_NAMES)
    head = ''
    if not bare:
        head = ('<header class="top"><div class="bar"><div class="brand">%s Leaderboards<small>%s to %s, %d</small></div><nav class="tabs" aria-label="Boards">%s</nav></div></header>'
                % (_X(res), P._md(f), P._md(t), t.year, nav))
    opts = '' if bare else ('<div class="opts"><div><label>Dates</label><div class="seg" role="group" aria-label="Dates">%s</div></div><div><label>Look</label><div class="seg" role="group" aria-label="Look">%s</div></div></div>' % (ropts, lopts))
    foot = '' if bare else '<div class="foot">Numbers come straight from the race database and update on their own. Data through %s.</div>' % _X(P._md(t))
    return ('<!doctype html><html lang="en" data-look="%s"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            '<title>%s %s</title>%s<style>%s%s%s</style></head><body>%s<main class="site">%s%s%s</main>%s</body></html>'
            % (look, _X(res), _X(title_name), P.FONTS_LINK, P.TOTE_CSS, SITE_CSS, _WEEK_CARD_CSS, head, opts, content, foot, script)).encode('utf-8')
