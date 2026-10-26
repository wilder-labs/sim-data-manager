"""Residency rosters: read a roster file (CSV or Excel) and turn it into a clean JSON file.

A roster says which stables belong to which member, and which master stable each came from.
Leaderboards and exports use the JSON, so every problem in the file is found here, once.

Nothing here contacts the sim site or any other server. Only the standard library is used,
including a small reader for .xlsx files. A roster file is treated as data and never run.

Command line, from the program folder:
    python -m sdmmodern.residency build roster.xlsx --code orphans --name "Orphans Racing"
writes residency-data/<code>/roster.json next to the database (or into the folder given with --out).
"""
import csv, datetime, hashlib, io, json, os, re, sys, zipfile
import xml.etree.ElementTree as ET

FORMAT = 1
MAX_FILE = 5 * 1024 * 1024            # a roster file bigger than this is refused
MAX_XML = 20 * 1024 * 1024            # biggest single part allowed inside an .xlsx
MAX_ROWS = 20000
DATA_DIR = 'residency-data'
CODE_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,23}$')
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'

# heading names we accept for each column, compared after lower-casing and dropping spaces and punctuation
HEADS = {
    'member': ('member', 'membername', 'player', 'playername', 'owner'),
    'master': ('masterstable', 'master', 'mastername', 'masterstablename'),
    'stable': ('stable', 'stablename'),
    'joined': ('joined', 'joineddate', 'from', 'start', 'startdate'),
    'left': ('left', 'leftdate', 'to', 'end', 'enddate'),
    'notes': ('notes', 'note', 'comments', 'comment'),
}
EXAMPLE_MEMBER = 'example member'


class RosterError(Exception):
    """The file cannot be read at all (wrong type, too big, no heading row)."""


def make_code(name):
    """A short code for a residency name: letters, numbers and dashes, for example 'orphans-racing'."""
    c = re.sub(r'[^a-z0-9]+', '-', str(name or '').lower()).strip('-')[:24].strip('-')
    return c if CODE_RE.match(c) else ''


def norm_key(s):
    """The form used to compare names: lower case, trimmed, runs of spaces collapsed."""
    return re.sub(r'\s+', ' ', str(s or '')).strip().lower()


def _head_key(s):
    return re.sub(r'[^a-z0-9]', '', str(s or '').lower())


# ---------------------------------------------------------------- reading files
def _col_index(ref):
    n = 0
    for ch in re.match(r'[A-Z]+', ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xml(z, name):
    info = z.getinfo(name)
    if info.file_size > MAX_XML:
        raise RosterError('That Excel file has a part that is too large.')
    raw = z.read(name)
    if b'<!DOCTYPE' in raw[:2048].upper() or b'<!ENTITY' in raw.upper():
        raise RosterError('That Excel file contains something that is not allowed.')
    return ET.fromstring(raw)


def _read_xlsx_sheets(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise RosterError('That file is not a readable Excel (.xlsx) file.')
    names = set(z.namelist())
    if 'xl/workbook.xml' not in names:
        raise RosterError('That file is not a readable Excel (.xlsx) file.')
    shared = []
    if 'xl/sharedStrings.xml' in names:
        for si in _xml(z, 'xl/sharedStrings.xml').findall('m:si', NS):
            shared.append(''.join(t.text or '' for t in si.iter('{%s}t' % NS['m'])))
    wb = _xml(z, 'xl/workbook.xml')
    rels = {}
    if 'xl/_rels/workbook.xml.rels' in names:
        for r in _xml(z, 'xl/_rels/workbook.xml.rels').findall('{%s}Relationship' % REL_NS):
            rels[r.get('Id')] = r.get('Target')
    sheets = []
    for s in wb.find('m:sheets', NS) or []:
        rid = s.get('{%s}id' % NS['r'])
        tgt = rels.get(rid, '')
        tgt = tgt.lstrip('/')
        path = tgt if tgt.startswith('xl/') else 'xl/' + tgt
        if path in names:
            sheets.append((s.get('name') or '', path))
    if not sheets:
        raise RosterError('That Excel file has no sheets.')
    date1904 = (wb.find('m:workbookPr', NS) is not None and wb.find('m:workbookPr', NS).get('date1904') in ('1', 'true'))
    styles_dates = _date_styles(z, names)
    return [(n, _read_sheet(z, p, shared, styles_dates, date1904)) for n, p in sheets]


def _date_styles(z, names):
    """Cell style numbers that hold dates, so a date typed in Excel comes back as a date."""
    out = set()
    if 'xl/styles.xml' not in names:
        return out
    st = _xml(z, 'xl/styles.xml')
    custom = {}
    nf = st.find('m:numFmts', NS)
    if nf is not None:
        for f in nf.findall('m:numFmt', NS):
            custom[int(f.get('numFmtId'))] = (f.get('formatCode') or '').lower()
    xfs = st.find('m:cellXfs', NS)
    if xfs is None:
        return out
    for i, xf in enumerate(xfs.findall('m:xf', NS)):
        fid = int(xf.get('numFmtId') or 0)
        code = custom.get(fid, '')
        if 14 <= fid <= 22 or 45 <= fid <= 47 or (code and re.search(r'[ymd]', re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', '', code))):
            out.add(i)
    return out


def _read_sheet(z, path, shared, date_styles, date1904):
    root = _xml(z, path)
    rows = []
    for row in root.iter('{%s}row' % NS['m']):
        rn = int(row.get('r') or len(rows) + 1)
        if rn > MAX_ROWS:
            break
        cells = {}
        for c in row.findall('m:c', NS):
            ref = c.get('r')
            if not ref:
                continue
            t = c.get('t')
            v = c.find('m:v', NS)
            val = ''
            if t == 's' and v is not None and v.text is not None:
                i = int(v.text)
                val = shared[i] if 0 <= i < len(shared) else ''
            elif t == 'inlineStr':
                val = ''.join(x.text or '' for x in c.iter('{%s}t' % NS['m']))
            elif v is not None and v.text is not None:
                val = v.text
                if t in (None, 'n') and int(c.get('s') or 0) in date_styles:
                    try:
                        val = _serial_to_date(float(val), date1904).isoformat()
                    except (ValueError, OverflowError):
                        pass
            cells[_col_index(ref)] = val
        rows.append((rn, [cells.get(i, '') for i in range(max(cells) + 1)] if cells else []))
    return rows


def _serial_to_date(x, date1904=False):
    base = datetime.date(1904, 1, 1) if date1904 else datetime.date(1899, 12, 30)
    return base + datetime.timedelta(days=int(x))


def _read_csv(data):
    for enc in ('utf-8-sig', 'cp1252'):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            text = None
    if text is None:
        raise RosterError('That file could not be read as text.')
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
    except csv.Error:
        dialect = csv.excel
    rows = []
    for i, r in enumerate(csv.reader(io.StringIO(text), dialect), 1):
        if i > MAX_ROWS:
            break
        rows.append((i, [c for c in r]))
    return rows


def _is_grid(rows):
    """A grid sheet: no Stable heading, first cell of the first used row says Member."""
    if _find_header(rows) is not None:
        return False
    for rn, cells in rows[:5]:
        if any(_clean_name(c) for c in cells):
            return bool(cells) and _head_key(cells[0]) in HEADS['member']
    return False


def read_sheets(filename, data):
    """Returns (long_rows, grids). grids is a list of (sheet name, rows), one per member tab or grid sheet. Either may be None. long_rows has a Member/Stable heading row;
    grid_rows has one column per master stable (see the template). Raises RosterError if unreadable."""
    if len(data) > MAX_FILE:
        raise RosterError('That file is larger than %d MB.' % (MAX_FILE // 1048576))
    low = str(filename).lower()
    if data[:2] == b'PK' or low.endswith(('.xlsx', '.xlsm')):
        parsed = _read_xlsx_sheets(data)
        long_rows, grids = None, []
        for n, rows in parsed:
            if _is_grid(rows):
                grids.append((n, rows))
            elif _find_header(rows) is not None and (long_rows is None or n.strip().lower() == 'roster'):
                long_rows = rows
        return long_rows, (grids or None)
    if low.endswith('.xls') and data[:4] == b'\xd0\xcf\x11\xe0':
        raise RosterError('That is an old Excel (.xls) file. Please save it as .xlsx or .csv and send it again.')
    rows = _read_csv(data)
    if _is_grid(rows):
        return None, [('', rows)]
    return (rows if _find_header(rows) is not None else None), None


def read_rows(filename, data):
    """The long-format rows of a roster file. Kept for callers that only want those."""
    long_rows, grid_rows = read_sheets(filename, data)
    return long_rows if long_rows is not None else (grid_rows[0][1] if grid_rows else [])


def _col_letter(i):
    s = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def grid_to_long(grid, start_rn, sheet=''):
    """Grid: row 1 Member (blank = same member as the column to the left), row 2 Master stable, rows 3+ stables.
    Column A holds the labels. Returns (rows in long form numbered from start_rn, {row number: where it came from})."""
    used = [(rn, c) for rn, c in grid if any(_clean_name(x) for x in c)]
    if len(used) < 2:
        return [], {}
    (mrn, mrow), (rrn, rrow) = used[0], used[1]
    out, where, n = [], {}, start_rn
    member = ''
    for ci in range(1, max(len(mrow), len(rrow))):
        m = _clean_name(mrow[ci]) if ci < len(mrow) else ''
        member = m or member
        master = _clean_name(rrow[ci]) if ci < len(rrow) else ''
        for rn, cells in used[2:]:
            st = _clean_name(cells[ci]) if ci < len(cells) else ''
            if not st:
                continue
            if norm_key(member) == EXAMPLE_MEMBER:
                continue
            out.append((n, [member, master, st, '', '']))
            where[n] = '%scolumn %s, row %d' % (('tab %s, ' % sheet) if sheet else 'grid ', _col_letter(ci), rn)
            n += 1
    return out, where


# ---------------------------------------------------------------- understanding the rows
def _find_header(rows):
    for rn, cells in rows[:40]:
        keys = [_head_key(c) for c in cells]
        if any(k in HEADS['stable'] for k in keys) and any(k in HEADS['member'] for k in keys):
            return rn
    return None


def _parse_date(s):
    """YYYY-MM-DD, M/D/YYYY, D Mon YYYY or an Excel date. Returns an ISO date string, '' for empty, None for unreadable."""
    s = str(s or '').strip()
    if not s:
        return ''
    s = s.split(' ')[0] if re.match(r'^\d{4}-\d{2}-\d{2}[ T]', s) else s
    s = s[:10] if re.match(r'^\d{4}-\d{2}-\d{2}T', s) else s
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%Y/%m/%d', '%d %b %Y', '%b %d, %Y', '%B %d, %Y'):
        try:
            d = datetime.datetime.strptime(s, fmt).date()
            return d.isoformat() if 1990 <= d.year <= 2100 else None
        except ValueError:
            pass
    if re.match(r'^\d{5}(\.\d+)?$', s):
        try:
            return _serial_to_date(float(s)).isoformat()
        except (ValueError, OverflowError):
            return None
    return None


def _clean_name(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip()


def _overlap(a, b):
    """Do two active periods overlap? Each is (joined, left) as ISO strings, '' meaning open ended."""
    a0, a1, b0, b1 = a[0] or '0000', a[1] or '9999', b[0] or '0000', b[1] or '9999'
    return a0 <= b1 and b0 <= a1


def build(filename, data, code=None, name=None):
    """Turn a roster file into the roster dictionary. Returns a dict with 'ok', 'problems', 'members', 'stables'.
    Rows that cannot be trusted are left out and listed under problems; nothing is guessed."""
    long_rows, grid_rows = read_sheets(filename, data)
    if long_rows is None and grid_rows is None:
        raise RosterError('Could not find the heading row. The file needs at least the headings Member and Stable, or the grid layout from the template.')
    where = {}
    if long_rows is None:
        long_rows = [(0, ['Member', 'Master stable', 'Stable', 'Joined', 'Left'])]
    rows = list(long_rows)
    if grid_rows is not None:
        base = (max(rn for rn, _ in long_rows) + 1) if long_rows else 1
        for sheet, g in grid_rows:
            extra, w = grid_to_long(g, base, sheet)
            rows.extend(extra); where.update(w)
            base += len(extra)
    h = _find_header(rows)
    if h is None:
        raise RosterError('Could not find the heading row. The file needs at least the headings Member and Stable.')
    meta, head = {}, None
    errors, warnings = [], []
    for rn, cells in rows:
        if rn < h:
            lab = _head_key(cells[0]) if cells else ''
            val = next((_clean_name(c) for c in cells[1:] if _clean_name(c)), '')
            if lab in ('residencyname', 'residency'):
                meta['residency'] = val
            elif lab in ('contactname', 'contact'):
                meta['contact'] = val
        elif rn == h:
            head = {}
            for i, c in enumerate(cells):
                k = _head_key(c)
                for field, names in HEADS.items():
                    if k in names and field not in head:
                        head[field] = i
    if 'member' not in head or 'stable' not in head:
        raise RosterError('The heading row needs the headings Member and Stable.')
    if 'master' not in head:
        warnings.append(dict(row=h, message='There is no Master stable column, so stables are grouped under the member only.'))
    residency = _clean_name(name) or meta.get('residency', '')
    code = (code or '').strip().lower() or make_code(residency)
    if not CODE_RE.match(code or ''):
        raise RosterError('The residency code must be 2 to 24 letters, numbers or dashes, for example orphans.')

    def cell(cells, field):
        i = head.get(field)
        return _clean_name(cells[i]) if i is not None and i < len(cells) else ''

    stables, seen_example = [], 0
    for rn, cells in rows:
        if rn <= h or not any(_clean_name(c) for c in cells):
            continue
        member, master, stable = cell(cells, 'member'), cell(cells, 'master'), cell(cells, 'stable')
        if norm_key(member) == EXAMPLE_MEMBER:
            seen_example += 1
            continue
        if not member and not stable:
            continue
        if not member:
            errors.append(dict(row=rn, message='Row has a stable (%s) but no member.' % stable)); continue
        if not stable:
            errors.append(dict(row=rn, message='Row for %s has no stable name.' % member)); continue
        j, l = _parse_date(cell(cells, 'joined')), _parse_date(cell(cells, 'left'))
        if j is None or l is None:
            errors.append(dict(row=rn, message='Stable %s has a date that could not be read. Use the form 2026-03-14.' % stable)); continue
        if j and l and j > l:
            errors.append(dict(row=rn, message='Stable %s has a Joined date after its Left date.' % stable)); continue
        stables.append(dict(row=rn, name=stable, key=norm_key(stable), member=member, member_key=norm_key(member),
                            master=master or member, master_key=norm_key(master or member), joined=j, left=l))
    if seen_example:
        warnings.append(dict(row=0, message='%d example row%s ignored.' % (seen_example, '' if seen_example == 1 else 's')))

    # the same stable listed twice: an exact repeat is dropped, a different member with overlapping dates is a conflict
    by_stable, kept, conflicted = {}, [], set()
    for s in stables:
        by_stable.setdefault(s['key'], []).append(s)
    for key, lst in by_stable.items():
        ok = []
        for s in lst:
            dup = next((o for o in ok if o['member_key'] == s['member_key'] and (o['joined'], o['left']) == (s['joined'], s['left'])), None)
            if dup:
                warnings.append(dict(row=s['row'], message='Stable %s is listed twice for %s (rows %d and %d). The repeat was ignored.' % (s['name'], s['member'], dup['row'], s['row'])))
                continue
            ok.append(s)
        bad = set()
        for i, a in enumerate(ok):
            for b in ok[i + 1:]:
                if a['member_key'] != b['member_key'] and _overlap((a['joined'], a['left']), (b['joined'], b['left'])):
                    bad.update((a['row'], b['row']))
                elif a['member_key'] == b['member_key'] and _overlap((a['joined'], a['left']), (b['joined'], b['left'])):
                    warnings.append(dict(row=b['row'], message='Stable %s has overlapping dates for %s (rows %d and %d).' % (b['name'], b['member'], a['row'], b['row'])))
        if bad:
            names = sorted({'%s (row %d)' % (s['member'], s['row']) for s in ok if s['row'] in bad})
            errors.append(dict(row=min(bad), message='Stable %s is under more than one member at the same time: %s. It was left out.' % (ok[0]['name'], ', '.join(names))))
            conflicted.add(key)
            ok = [s for s in ok if s['row'] not in bad]
        kept.extend(ok)
    kept.sort(key=lambda s: s['row'])

    members = {}
    for s in kept:
        m = members.setdefault(s['member_key'], dict(name=s['member'], key=s['member_key'], masters={}))
        ms = m['masters'].setdefault(s['master_key'], dict(name=s['master'], key=s['master_key'], stables=[]))
        ms['stables'].append(dict(name=s['name'], key=s['key'], joined=s['joined'], left=s['left']))
    if head.get('master') is not None:
        missing = []
        for m in members.values():
            for x in m['masters'].values():
                if not any(st['key'] == x['key'] for st in x['stables']):
                    missing.append('%s (%s)' % (x['name'], m['name']))
        if missing:
            more = '' if len(missing) <= 5 else ' and %d more' % (len(missing) - 5)
            warnings.append(dict(row=0, message='%d master stable%s not listed as one of its own stables, for example %s%s. Add each master to its own list if its horses should count.' % (len(missing), ' is' if len(missing) == 1 else 's are', ', '.join(missing[:5]), more)))
    out_members = []
    for m in sorted(members.values(), key=lambda m: m['key']):
        out_members.append(dict(name=m['name'], key=m['key'],
                                masters=[dict(name=x['name'], key=x['key'], stables=x['stables']) for x in sorted(m['masters'].values(), key=lambda x: x['key'])]))
    flat = [dict(name=s['name'], key=s['key'], member=s['member'], master=s['master'], joined=s['joined'], left=s['left']) for s in kept]
    for lst in (errors, warnings):
        for p in lst:
            if p.get('row') in where:
                p['message'] += ' (%s)' % where[p['row']]
    return dict(
        format=FORMAT, code=code, residency=residency, contact=meta.get('contact', ''),
        built=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        source=dict(file=os.path.basename(str(filename)), sha256=hashlib.sha256(data).hexdigest()),
        ok=not errors, counts=dict(members=len(out_members), masters=sum(len(m['masters']) for m in out_members), stables=len(flat),
                                   errors=len(errors), warnings=len(warnings)),
        problems=dict(errors=errors, warnings=warnings), members=out_members, stables=flat)


# ---------------------------------------------------------------- saving
def residency_dir(db_path, code, base=None):
    if not CODE_RE.match(code or ''):
        raise RosterError('Bad residency code.')
    root = base or os.path.join(os.path.dirname(os.path.abspath(db_path)), DATA_DIR)
    return os.path.join(root, code)


def save(roster, base_dir):
    """Write roster.json into base_dir (created if needed). Writes to a temporary file first, then swaps it in."""
    os.makedirs(base_dir, exist_ok=True)
    path = os.path.join(base_dir, 'roster.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(roster, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load(base_dir):
    try:
        with open(os.path.join(base_dir, 'roster.json'), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get('format') == FORMAT else None
    except (OSError, ValueError):
        return None


def summary(r):
    c = r['counts']
    lines = ['%s (%s): %d members, %d master stables, %d stables.' % (r.get('residency') or '(no name)', r['code'], c['members'], c['masters'], c['stables'])]
    for e in r['problems']['errors']:
        lines.append('  ERROR  row %s: %s' % (e['row'] or '-', e['message']))
    for w in r['problems']['warnings']:
        lines.append('  note   row %s: %s' % (w['row'] or '-', w['message']))
    lines.append('Roster is ready.' if r['ok'] else 'Fix the errors above and run it again. Rows with errors were left out.')
    return '\n'.join(lines)


<<<<<<< Updated upstream
def match_conn(roster, con):
    """Check every roster stable against an open database connection (read only use). Returns a report dictionary.
    A stable matches when an owner row has the same name in any of its four name columns, ignoring capitals.
    For each match: races, wins, first and last week in the database. Unmatched stables get up to three look-alike names."""
    import difflib
    want = {}
    for st in roster.get('stables', []):
        want.setdefault(st['key'], st)
    ids, names = {}, set()
    for row in con.execute('SELECT id, oname, o15, o12, o10 FROM owners'):
        for v in row[1:]:
            k = norm_key(v or '')
            if not k:
                continue
            names.add(k)
            if k in want:
                ids.setdefault(k, set()).add(row[0])
    rows, unmatched = [], 0
    for key, st in want.items():
        oids = sorted(ids.get(key, ()))
        row = dict(stable=st['name'], key=key, member=st['member'], master=st['master'], matched=bool(oids))
        if oids:
            n = w = 0
            a = b = None
            for i in range(0, len(oids), 400):
                part = oids[i:i + 400]
                c, ww, lo, hi = con.execute('SELECT COUNT(*), SUM(fin=1), MIN(week), MAX(week) FROM entries WHERE owner_id IN (%s)' % ','.join('?' * len(part)), part).fetchone()
                n += c or 0; w += ww or 0
                a = lo if a is None or (lo is not None and lo < a) else a
                b = hi if b is None or (hi is not None and hi > b) else b
            row.update(races=n, wins=w, first_week=a, last_week=b)
        else:
            unmatched += 1
            cand = [x for x in names if x[0] == key[:1] and abs(len(x) - len(key)) <= 3]
            row['close'] = difflib.get_close_matches(key, cand, n=3, cutoff=0.8)
        rows.append(row)
    return dict(code=roster.get('code'), checked=len(rows), matched=len(rows) - unmatched, unmatched=unmatched, stables=rows)


def match_db(roster, db_path):
    """Same check, opening the database file read only."""
    import pathlib, sqlite3
    con = sqlite3.connect(pathlib.Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        return match_conn(roster, con)
    finally:
        con.close()


def match_summary(rep):
    lines = ['%d of %d stables found in the database.' % (rep['matched'], rep['checked'])]
    for r in rep['stables']:
        if not r['matched']:
            lines.append('  NOT FOUND  %s (%s)%s' % (r['stable'], r['member'], ('  close: ' + ', '.join(r['close'])) if r['close'] else ''))
    quiet = [r for r in rep['stables'] if r['matched'] and not r['races']]
    for r in quiet:
        lines.append('  no races    %s (%s)' % (r['stable'], r['member']))
    return '\n'.join(lines)


=======
>>>>>>> Stashed changes
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='Residency roster tools')
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build', help='read a roster file and write roster.json')
    b.add_argument('file')
    b.add_argument('--code', help='short residency code, for example orphans')
    b.add_argument('--name', help='residency name (default: the name inside the file)')
    b.add_argument('--out', help='folder to write into (default: residency-data/<code> next to the database)')
    b.add_argument('--db', default='sdm.db', help='database path, only used to find the default folder')
<<<<<<< Updated upstream
    c = sub.add_parser('check', help='check a roster.json against the database (read only)')
    c.add_argument('roster', help='path to roster.json')
    c.add_argument('--db', default='sdm.db')
    c.add_argument('--out', help='folder for match-report.json (default: next to roster.json)')
    a = ap.parse_args(argv)
    if a.cmd == 'check':
        try:
            with open(a.roster, encoding='utf-8') as f:
                r = json.load(f)
            rep = match_db(r, a.db)
            out = a.out or os.path.dirname(os.path.abspath(a.roster))
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, 'match-report.json'), 'w', encoding='utf-8') as f:
                json.dump(rep, f, indent=1, ensure_ascii=False)
        except Exception as e:
            print('Could not check the roster: %s' % e)
            return 1
        print(match_summary(rep))
        print('Written: %s' % os.path.join(out, 'match-report.json'))
        return 0 if not rep['unmatched'] else 2
=======
    a = ap.parse_args(argv)
>>>>>>> Stashed changes
    try:
        with open(a.file, 'rb') as f:
            data = f.read(MAX_FILE + 1)
        r = build(a.file, data, a.code, a.name)
        out = a.out or residency_dir(a.db, r['code'])
        path = save(r, out)
    except (RosterError, OSError) as e:
        print('Could not read the roster: %s' % e)
        return 1
    print(summary(r))
    print('Written: %s' % path)
    return 0 if r['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
