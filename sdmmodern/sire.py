"""Saved sires (read from the local database only) and master stable groups (one public request per group, then saved).
Nothing here looks up a single sire or horse on the site."""
import re, time, json, urllib.request, urllib.parse

BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

MISS_DAYS = 7


def _ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS sires(hid INTEGER PRIMARY KEY, sire TEXT, checked INTEGER)')


def cached(store, hid):
    _ensure(store.db)
    r = store.db.execute('SELECT sire, checked FROM sires WHERE hid=?', (hid,)).fetchone()
    if not r or not r['sire']:
        return None
    return {'sire': r['sire'], 'cached': True}


STABLES_URL = 'http://ehorseracing.com/allstables.asp?lookat=%s&sel=misc'

def parse_stables(html):
    """(master, names): the master stable and every stable under it, from the public 'all stables' page."""
    k = html.find('All Stables')
    if k < 0:
        return None, []
    names = []
    for m in re.finditer(r'<td[^>]*align=left>\s*<table[^>]*>\s*<tr><td\s*align=left>\s*<b>([\s\S]*?)</b>', html[k:]):
        n = re.sub(r'\s+', ' ', m.group(1)).strip()
        if n and n not in names:
            names.append(n)
    mm = re.search(r'Master Stable\s*:\s*<b>([^<]+)</b>', html)
    master = mm.group(1).strip() if mm else (names[0] if names else None)
    return master, names

def fetch_stables(name, timeout=10):
    """One request: returns {'master': ..., 'names': [...]}."""
    if not re.fullmatch(r'[A-Za-z0-9_.\-]{1,40}', name or ''):
        raise ValueError('Enter a stable name using letters and numbers only.')
    req = urllib.request.Request(STABLES_URL % urllib.parse.quote(name), headers={'User-Agent': BROWSER_UA, 'Referer': 'http://ehorseracing.com/allstables.asp'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        master, names = parse_stables(r.read().decode('latin-1', 'replace'))
    return {'master': master, 'names': names}

def _ensure_groups(db):
    db.execute('CREATE TABLE IF NOT EXISTS stable_groups(name TEXT PRIMARY KEY, master TEXT, members TEXT, checked INTEGER)')

def cached_group(store, name):
    import json
    _ensure_groups(store.db)
    r = store.db.execute('SELECT master, members, checked FROM stable_groups WHERE name=?', (name.lower(),)).fetchone()
    if not r:
        return None
    if r['master']:                      # a lookup that found nothing is never kept, so it is tried again next time
        return {'master': r['master'], 'names': json.loads(r['members'] or '[]')}
    return None

def save_group(store, name, grp):
    """Remember the group under every member's name, so one lookup labels the whole group."""
    import json
    _ensure_groups(store.db)
    now = int(time.time())
    if grp and grp.get('names'):
        mem = json.dumps(grp['names'])
        for n in set(grp['names']) | {name}:
            store.db.execute('INSERT OR REPLACE INTO stable_groups(name,master,members,checked) VALUES(?,?,?,?)', (n.lower(), grp['master'], mem, now))
    else:
        store.db.execute('INSERT OR REPLACE INTO stable_groups(name,master,members,checked) VALUES(?,?,?,?)', (name.lower(), '', '[]', now))
    store.db.commit()
