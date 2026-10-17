"""Downloads weekly update packs from the public archive and merges them into the local database."""
import os, re, json, threading, time, urllib.request, urllib.error, urllib.parse, datetime
from .store import NoDatabase, date_to_week, week_to_date

# The archive moved to denovo.eu5.net; the old address is kept as a fallback. SDM_ARCHIVE_BASE overrides both.
BASES = [os.environ['SDM_ARCHIVE_BASE']] if os.environ.get('SDM_ARCHIVE_BASE') else ['http://denovo.eu5.net/', 'http://denovo.we.bs/sim/']
BASE = BASES[0]
PAGE = BASE + 'SSSarch.php'
PACKDIR = BASE + 'SSS/'
DRIVE_LINK = ''      # Google Drive folder that holds the weekly packs; set by configure()
DRIVE_IDS = {}       # week -> Drive file id, filled by list_packs()      # folder the pack links point to; set from the page's own links by list_packs()
UA = 'SimDataManager/0.9.1 (personal use; weekly pack fetch)'
MISS_LIMIT = 60            # this many 'not found' answers in a row, with nothing downloaded yet, means the archive is down
FIRST_2020_PACK = 7838     # covers Dec 30 2019 - Jan 5 2020, the first pack that reaches into 2020


def _get(url, timeout=30, deadline=150):
    """Download with a per-read timeout AND an overall deadline, so a stalled connection can never hang the run."""
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    t0, parts = time.time(), []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            parts.append(chunk)
            if time.time() - t0 > deadline:
                raise TimeoutError('download took longer than %d seconds' % deadline)
    return b''.join(parts)


def configure(store):
    """Read the pack source from the settings. Call with the database lock held."""
    global DRIVE_LINK
    from . import shared
    link = (store.get_setting('shared_link', '') or shared.DEFAULT_LINK).strip()
    DRIVE_LINK = link if shared.folder_id(link) and store.get_setting('pack_source', 'auto') != 'site' else ''


MISS_RECHECK_DAYS = 14     # a pack the server did not have is asked for again after this many days
RECENT_WEEKS_DAYS = 84     # the newest 12 weeks of packs are never marked missing (they may just not be uploaded yet)


def _recent_miss(how):
    """True if a 'not on server <date>' mark is newer than MISS_RECHECK_DAYS. A bare 'not on server' (old format) counts as stale."""
    try:
        d = datetime.date.fromisoformat(str(how).split()[-1])
        return (datetime.date.today() - d).days < MISS_RECHECK_DAYS
    except Exception:
        return False


def list_packs():
    """Return the sorted list of pack week numbers the archive page offers.
    Tries each known address; the one that works becomes the address used for the downloads."""
    global BASE, PAGE, PACKDIR, DRIVE_IDS
    problems = []
    DRIVE_IDS = {}
    if DRIVE_LINK:                                # the shared Google Drive folder comes first
        try:
            from . import shared
            ids = shared.pack_ids(DRIVE_LINK)
            if ids:
                DRIVE_IDS = ids
                return sorted(ids)
            problems.append('Google Drive folder: no weekly packs in it yet')
        except Exception as e:
            problems.append('Google Drive folder: %s' % str(e)[:100])
    for base in BASES:
        page = base + 'SSSarch.php'
        try:
            html = _get(page).decode('latin-1')
        except urllib.error.HTTPError as e:
            problems.append('%s: blocked (HTTP %d%s)' % (base, e.code, ', probably Cloudflare refusing this connection' if e.code in (403, 429, 503) else ''))
            continue
        except Exception as e:
            problems.append('%s: %s' % (base, str(e)[:80]))
            continue
        weeks = sorted({int(w) for w in re.findall(r'(\d+)_SSSdata\.7z', html)})
        if weeks:
            m = re.search(r'["\'=\s]([^"\'<>=\s]*?)\d+_SSSdata\.7z', html, re.I)     # any attribute, any case: the page now uses a drop-down (VALUE=), it used to use links (HREF=)
            BASE, PAGE = base, page
            PACKDIR = urllib.parse.urljoin(page, m.group(1)) if m else base + 'SSS/'
            return weeks
        if re.search(r'document\.cookie|aes\.js|__test=|Just a moment|cf-chl|challenge-platform', html):
            problems.append('%s: the site asked for a browser check instead of showing the list' % base)
        else:
            problems.append('%s: the page loaded but listed no update packs' % base)
    raise ValueError('Could not find any update packs. ' + ' | '.join(problems))


class Job:
    def __init__(self, store, lock, from_week, to_week=None, redo=False, fetch=_get, pause=0.25, auto=False):
        self.store, self.lock, self.fetch, self.pause, self.auto = store, lock, fetch, pause, auto
        self.from_week, self.to_week, self.redo = from_week, to_week, redo
        self.state = dict(running=True, phase='Checking the archive', total=0, done=0, skipped=0, current=None,
                          imported_entries=0, missing=0, empty=0, skipped_rows=0, errors=[], cancelled=False, finished=False, started=time.time(), mb=0.0, auto=auto)
        self.state.update(stage='', stage_since=time.time())
        self.logpath = os.path.join(os.path.dirname(store.path), 'archive.log')
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _stage(self, text, week=None):
        self.state['stage'], self.state['stage_since'] = text, time.time()
        try:
            with open(self.logpath, 'a', encoding='utf-8') as f:
                f.write('%s  %s %s\n' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), text, week or ''))
        except Exception:
            pass

    def _mark_missing(self, week, newest):
        """Remember that the server did not have this pack, with today's date, so the next run skips it for a while and then tries again."""
        if newest is not None and week > newest - RECENT_WEEKS_DAYS:
            return
        with self.lock:
            self.store.db.execute("INSERT OR REPLACE INTO packs VALUES(?,?)", (week, 'not on server ' + datetime.date.today().isoformat()))
            self.store.db.commit()

    def _keep_failed(self, week, data):
        try:
            if data:
                fd = os.path.join(os.path.dirname(self.store.path), 'failed')
                os.makedirs(fd, exist_ok=True)
                open(os.path.join(fd, '%d_SSSdata.7z' % week), 'wb').write(data)
        except Exception:
            pass

    def cancel(self):
        self.state['cancelled'] = True
        if self.state.get('running'):
            self.state['phase'] = 'Stopping...'

    def _run(self):
        st = self.state
        try:
            with self.lock:
                configure(self.store)
            weeks = [w for w in list_packs() if w >= self.from_week and (self.to_week is None or w <= self.to_week)]
            with self.lock:
                done, gone = set(), set()
                for w_, how in self.store.db.execute('SELECT week, done_at FROM packs'):
                    if str(how).startswith('not on server'):
                        if _recent_miss(how):
                            gone.add(w_); done.add(w_)       # skipped for now; looked at again after MISS_RECHECK_DAYS
                        # older marks (and marks from before dates were saved) are forgotten, so the pack is tried again
                    else:
                        done.add(w_)
            todo = weeks if self.redo else [w for w in weeks if w not in done]
            st['skipped'] = len(weeks) - len(todo) - (0 if self.redo else len([w for w in weeks if w in gone]) * 0)
            st['missing'] = 0 if self.redo else len([w for w in weeks if w in gone])
            st['skipped'] -= st['missing']
            # the page can lag behind the files: also try the next two weekly packs by their predictable names
            newest = max(weeks) if weeks else None
            optional = set()
            if newest is not None and self.to_week is None and not DRIVE_IDS:
                for k in (1, 2):
                    w = newest + 7 * k
                    if w not in done:
                        todo.append(w); optional.add(w)
            st['total'] = len(todo)
            st['phase'] = 'Downloading and merging'
            pending, streak, got = [], 0, False      # 'not on server' marks are saved only once a real pack has arrived
            for w in todo:
                if st['cancelled']:
                    break
                st['current'] = w
                try:
                    try:
                        self._stage('Downloading pack', w)
                        data = self._download(w, quiet404=w in optional)
                    except urllib.error.HTTPError as e:
                        if e.code == 404:
                            if st['missing'] < 3:
                                self._stage('Not found (404) at %s%d_SSSdata.7z  pack' % (PACKDIR, w), w)
                            st['total'] -= 1                    # not published yet, or listed but never uploaded
                            if w not in optional:
                                st['missing'] += 1
                                streak += 1
                                if got:
                                    self._mark_missing(w, newest)
                                else:
                                    pending.append(w)
                                    if streak >= MISS_LIMIT:
                                        st['errors'].append(dict(week=None, error='The archive said "not found" for %d packs in a row and nothing downloaded. It may be down or moved. Nothing was saved as missing. Try again later, use Data > Get the Shared Database (Google Drive), or open the archive in your browser.' % streak))
                                        st['phase'] = 'Stopped'
                                        break
                            continue
                        raise
                    if not got:
                        got = True
                        for pw in pending:
                            self._mark_missing(pw, newest)
                        pending = []
                    streak = 0
                    st['mb'] += len(data) / 1048576
                    self._stage('Unpacking and importing pack', w)
                    with self.lock:
                        r = self.store.import_mdb(data, replace=True)
                        self.store.db.execute("INSERT OR REPLACE INTO packs VALUES(?,?)", (w, datetime.datetime.now().isoformat(timespec='seconds')))
                        self.store.db.commit()
                    st['imported_entries'] += r['entries']
                    st['skipped_rows'] += r.get('skipped_rows', 0)
                except NoDatabase as e:
                    # a pack that only holds empty marker files (for example "no update this week") is not a problem
                    if all(sz < 4096 for _, sz in e.listing):
                        st['empty'] += 1
                        with self.lock:
                            self.store.db.execute("INSERT OR REPLACE INTO packs VALUES(?,?)", (w, 'empty pack'))
                            self.store.db.commit()
                    else:
                        self._keep_failed(w, data)
                        st['errors'].append(dict(week=w, error=str(e)[:300] + '  (a copy was saved in the "failed" folder)'))
                except Exception as e:                      # keep going; report at the end
                    msg = str(e)[:200]
                    self._stage('Error: %s  pack' % msg[:120], w)
                    try:                                    # keep the pack that failed so it can be looked at
                        if 'data' in locals() and data:
                            fd = os.path.join(os.path.dirname(self.store.path), 'failed')
                            os.makedirs(fd, exist_ok=True)
                            open(os.path.join(fd, '%d_SSSdata.7z' % w), 'wb').write(data)
                            msg += '  (copy saved in the "failed" folder)'
                    except Exception:
                        pass
                    st['errors'].append(dict(week=w, error=msg))
                    data = None
                st['done'] += 1
                self._stage('Finished pack', w)
                time.sleep(self.pause)
            if st['phase'] != 'Stopped':
                st['phase'] = 'Cancelled' if st['cancelled'] else 'Finished'
        except Exception as e:
            st['phase'] = 'Stopped'
            st['errors'].append(dict(week=None, error=str(e)[:300]))
        finally:
            st['running'] = False
            st['finished'] = True
            st['current'] = None

    def _download(self, week, quiet404=False):
        last = None
        if week in DRIVE_IDS and self.fetch is _get:
            from . import shared
            last = None
            for attempt in range(3):
                try:
                    return shared.download_bytes(DRIVE_IDS[week])
                except Exception as e:
                    last = e
                    time.sleep(1.5 * (attempt + 1))
            raise last
        for attempt in range(3):
            try:
                return self.fetch('%s%d_SSSdata.7z' % (PACKDIR, week))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise
                last = e
                time.sleep(1.5 * (attempt + 1))
            except Exception as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise last


CHECK_EVERY = 6 * 3600     # while a new pack is due but not up yet, look at the archive page at most this often


def new_pack_status(store, lock, today=None):
    """Is there a weekly pack on the archive that this database does not have yet? Costs no request at all until the next pack is due.
    Packs come out weekly and carry the date of a Sunday. The page is read only once that Sunday has arrived, and then at most every
    CHECK_EVERY seconds until the pack shows up. Returns {state: empty | not_due | none | available, have, new, newest, ...}."""
    today = today or datetime.date.today()
    with lock:
        rows = list(store.db.execute('SELECT week, done_at FROM packs'))
        cached = store.get_setting('pack_check', '')
    seen = {w for w, _ in rows}
    mine = [w for w, how in rows if not str(how).startswith('not on server')]
    if not mine:
        return dict(state='empty')
    have = max(mine)
    out = dict(have=have, have_date=week_to_date(have).isoformat())
    if date_to_week(today) < have + 7:
        return dict(out, state='not_due')
    weeks, fresh = None, False
    try:
        c = json.loads(cached) if cached else {}
        if c.get('have') == have and time.time() - c.get('at', 0) < CHECK_EVERY:
            weeks, fresh = c.get('weeks', []), True
    except Exception:
        pass
    if weeks is None:
        with lock:
            configure(store)
        weeks = [w for w in list_packs() if w > have]          # one page request, made outside the lock
        with lock:
            store.set_setting('pack_check', json.dumps(dict(at=time.time(), have=have, weeks=weeks)))
    new = sorted(w for w in weeks if w > have and w not in seen)
    if not new:
        return dict(out, state='none')
    return dict(out, state='available', new=new, newest=new[-1], newest_date=week_to_date(new[-1]).isoformat(), count=len(new))


class AutoUpdater:
    """Checks the archive for new weekly packs once, shortly after the app starts. Packs come out weekly."""
    def __init__(self, app, first_delay=8):
        self.app, self.delay = app, first_delay
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        time.sleep(self.delay)
        try:
            self.run_once()
        except Exception:
            pass

    def run_once(self):
        app = self.app
        with app.lock:
            enabled = app.store.get_setting('auto_enabled', '0') == '1'
            from_week = int(app.store.get_setting('auto_from', '0') or 0)
        if not enabled or (app.job and app.job.state['running']):
            return
        if new_pack_status(app.store, app.lock)['state'] != 'available':      # nothing is requested until a new pack is due and listed
            return
        app.job = Job(app.store, app.lock, from_week, auto=True).start()
