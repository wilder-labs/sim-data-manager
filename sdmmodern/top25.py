"""Top 25 Speeds of the Week: zapspride's weekly lists from the Black Type Bugler, used with his permission.

Nothing here contacts the Bugler or ehorseracing.com. The lists come from two local files:
  - data/top25_2026.json inside the program package (shipped with Sim Data Manager), and
  - top25-speeds.json next to the database (weeks the user adds with Top 25 Speeds > Add a Top 25 Speeds File).
A week in the user's file replaces the shipped copy of the same week.
"""
import datetime, json, os, re

LISTS = ('overall', 'two', 'three')
BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'top25_2026.json')
USER_NAME = 'top25-speeds.json'
MAX_IMPORT = 5 * 1024 * 1024
ROW = re.compile(r'^[^|\n]{1,40}\|[^|\n]{1,40}\|\d{1,3}(\.\d{1,8})?\|[^|\n]{0,12}$')
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
          'November', 'December']


def user_path(store):
    return os.path.join(os.path.dirname(os.path.abspath(store.path)), USER_NAME)


def _read(path):
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _clean_rows(v):
    """A list as 'stable|horse|figure|race type' lines. Bad lines are dropped, never guessed at."""
    lines = v.split('\n') if isinstance(v, str) else (v if isinstance(v, list) else [])
    out = []
    for ln in lines[:40]:
        if isinstance(ln, str):
            ln = ln.strip()
            if ROW.match(ln):
                out.append(ln)
    return out


def _clean_week(w):
    post = str(w.get('post') or '')
    return dict(post=post if post.isdigit() else '', posted=str(w.get('posted') or '')[:80],
                **{k: _clean_rows(w.get(k)) for k in LISTS}, **{k + 'Year': _clean_rows(w.get(k + 'Year')) for k in LISTS})


def load(store):
    """All weeks, shipped and user-added, keyed by the Sunday the week ends (YYYY-MM-DD)."""
    weeks = {}
    for src in (BUNDLED, user_path(store)):
        for k, w in _read(src).items():
            if re.match(r'^\d{4}-\d{2}-\d{2}$', k) and isinstance(w, dict):
                weeks[k] = _clean_week(w)
    return dict(weeks=weeks, source='zapspride, Top 25 Speeds of the Week, Black Type Bugler')


def _week_from_export(w):
    """One week in the browser-export format: {week, id, posted, title, overall:[...], overallYear:[...], ...}.
    Fixes the two title mistakes seen in 2026: a wrong year, and a date that is not the Sunday before the post."""
    title_wk = str(w.get('week') or '')
    try:
        posted = datetime.date.fromisoformat(str(w.get('posted') or '')[:10])
        wk = datetime.date.fromisoformat(title_wk)
    except ValueError:
        return None, None
    note = ''
    if wk.year != posted.year:
        wk = wk.replace(year=posted.year)
        note = 'title reads %s/%s/%s' % (title_wk[5:7], title_wk[8:10], title_wk[:4])
    if (posted - wk).days > 7 or (posted - wk).days < 0 or wk.weekday() != 6:
        wk = posted - datetime.timedelta(days=(posted.weekday() + 1) % 7 or 7)
        note = 'title reads %s/%s/%s' % (title_wk[5:7], title_wk[8:10], title_wk[:4])
    label = '%s, %s %d' % (DAYS[posted.weekday()], MONTHS[posted.month - 1], posted.day) + (' (%s)' % note if note else '')
    out = dict(post=str(w.get('id') or ''), posted=label)
    for k in LISTS:
        out[k] = w.get(k) or []
        out[k + 'Year'] = w.get(k + 'Year') or []
    return wk.isoformat(), out


def parse_import(data):
    """Accepts the browser-export file ({weeks:[...]}) or a file shaped like data/top25_2026.json. Returns {week: lists}."""
    if len(data) > MAX_IMPORT:
        raise ValueError('That file is too big for a Top 25 Speeds file.')
    try:
        d = json.loads(data.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError):
        raise ValueError('That is not a Top 25 Speeds file (it is not readable JSON).')
    found = {}
    if isinstance(d, dict) and isinstance(d.get('weeks'), list):
        for w in d['weeks']:
            if isinstance(w, dict):
                k, v = _week_from_export(w)
                if k:
                    found[k] = v
    elif isinstance(d, dict):
        for k, w in d.items():
            if re.match(r'^\d{4}-\d{2}-\d{2}$', k) and isinstance(w, dict):
                found[k] = w
    weeks = {k: _clean_week(w) for k, w in found.items()}
    weeks = {k: w for k, w in weeks.items() if any(w[x] for x in LISTS)}
    if not weeks:
        raise ValueError('No Top 25 Speeds weeks were found in that file.')
    return weeks


def add(store, data):
    """Merge the weeks in an imported file into top25-speeds.json next to the database."""
    weeks = parse_import(data)
    path = user_path(store)
    cur = _read(path)
    have = set(load(store)['weeks'])
    new = sorted(k for k in weeks if k not in have)
    cur.update(weeks)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cur, f, ensure_ascii=False)
    os.replace(tmp, path)
    return dict(ok=True, weeks=len(weeks), new=len(new), replaced=len(weeks) - len(new),
                first=min(weeks), last=max(weeks), file=USER_NAME)
