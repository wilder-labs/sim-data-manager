"""Pulls the shared race database from a Google Drive link or a GitHub release link and swaps it in for the local one.
Your own settings, saved stables and watch lists are carried over. The old database is kept once as sdm-before-shared.db."""
import os, re, sqlite3, threading, time, zipfile, http.cookiejar, urllib.request, urllib.parse, urllib.error, html as H, json

DEFAULT_LINK = ''                                   # set to the shared database link (a GitHub release file or a Drive link); users can also paste their own
ALLOW_HOSTS = {'drive.google.com', 'drive.usercontent.google.com', 'docs.google.com', 'github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com'}
SCHEMES = ('https',)
UA = 'SimDataManager/0.9 (shared database pull)'
PRIVATE_TABLES = ('stable_groups', 'sire_watch', 'sire_prices', 'race_calls')
KEEP_NEW_SETTINGS = ('anchor_week', 'anchor_date', 'earn_v', 'oc_sig')
CHUNK = 1 << 20
MAX_BYTES = 8 << 30
FOLDER_URL = 'https://drive.google.com/embeddedfolderview?id='
DB_NAME = 'SDM-database.zip'
START_URL = 'https://drive.google.com/uc?export=download&id='
BACKUP = 'sdm-before-shared.db'


def file_id(link):
    link = (link or '').strip()
    for rx in (r'/file/d/([\w-]{10,})', r'[?&]id=([\w-]{10,})', r'/d/([\w-]{10,})'):
        m = re.search(rx, link)
        if m:
            return m.group(1)
    if re.fullmatch(r'[\w-]{20,}', link):
        return link
    raise ValueError('That does not look like a GitHub release link or a Google Drive share link.')


def _host_ok(url):
    p = urllib.parse.urlparse(url)
    return p.scheme in SCHEMES and p.hostname in ALLOW_HOSTS or (p.hostname or '').endswith(('.googleusercontent.com', '.githubusercontent.com')) and p.scheme in SCHEMES


def is_direct(link):
    """True for a GitHub release download link, such as https://github.com/OWNER/REPO/releases/latest/download/SDM-database.zip"""
    try:
        p = urllib.parse.urlparse((link or '').strip())
    except ValueError:
        return False
    return p.scheme == 'https' and p.hostname == 'github.com' and '..' not in p.path and bool(re.match(r'^/[\w.-]+/[\w.-]+/releases/(latest/)?download/[^?#]+$', p.path))


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_ok(newurl):
            raise urllib.error.URLError('The download was sent somewhere unexpected, so it was stopped.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _Redirect)


def _open(op, url):
    if not _host_ok(url):
        raise ValueError('Refusing to download from an unexpected address.')
    return op.open(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=60)


def _confirm_url(page, fid):
    """The 'too large to scan for viruses' page holds a form; build the real download address from it."""
    m = re.search(r'<form[^>]+action="([^"]+)"', page)
    if not m:
        return None
    action = H.unescape(m.group(1))
    q = {k: H.unescape(v) for k, v in re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', page)}
    q.setdefault('id', fid); q.setdefault('export', 'download'); q.setdefault('confirm', 't')
    return action + ('&' if '?' in action else '?') + urllib.parse.urlencode(q)


def _explain(page):
    p = page.lower()
    if 'quota' in p or 'too many users' in p or 'download quota' in p:
        return 'Google says this file has been downloaded too many times today. Try again tomorrow, or add the file by hand.'
    if 'sign in' in p or 'accounts.google.com' in p or 'you need access' in p or 'request access' in p:
        return 'The Drive link is not shared with "Anyone with the link". Ask the owner to change it.'
    if 'not found' in p or 'does not exist' in p or '404' in p:
        return 'Google could not find that file. Check the link.'
    return 'Google sent a web page instead of the file.'


def open_download(link, forced_url=None):
    """Returns (response, headers) for the file itself, following Google's large-file confirmation page."""
    if is_direct(link):                                  # a GitHub release file: one plain download, no confirmation page
        try:
            return _open(_opener(), link.strip())
        except urllib.error.HTTPError as e:
            raise ValueError('GitHub could not find that release file (error %d). Check the link.' % e.code)
    fid = file_id(link)
    op = _opener()
    url = forced_url or START_URL + urllib.parse.quote(fid)
    for _ in range(3):
        r = _open(op, url)
        ct = (r.headers.get('Content-Type') or '').lower()
        if 'text/html' not in ct:
            return r
        page = r.read(400000).decode('utf-8', 'replace')
        nxt = _confirm_url(page, fid)
        if not nxt or nxt == url:
            raise ValueError(_explain(page))
        url = nxt
    raise ValueError('Google kept asking for confirmation. Try again later.')


def folder_id(link):
    """The folder id when the link is a Drive folder link, otherwise None."""
    m = re.search(r'/folders/([\w-]{10,})', link or '')
    return m.group(1) if m else None


def check_link(link):
    """Accepts a GitHub release link, a Drive file link or a Drive folder link. Raises ValueError when it is none of them."""
    if folder_id(link) or is_direct(link):
        return
    file_id(link)


def list_folder(link):
    """[(name, file id)] for a shared Drive folder, read from the folder's public embedded view."""
    fid = folder_id(link)
    if not fid:
        raise ValueError('That is not a Google Drive folder link.')
    op = _opener()
    r = _open(op, FOLDER_URL + urllib.parse.quote(fid))
    page = r.read(3000000).decode('utf-8', 'replace')
    out, seen = [], set()
    for m in re.finditer(r'id="entry-([\w-]{10,})"(.*?)(?=id="entry-|\Z)', page, re.S):
        t = re.search(r'flip-entry-title"[^>]*>([^<]+)<', m.group(2))
        if t and m.group(1) not in seen:
            seen.add(m.group(1)); out.append((H.unescape(t.group(1)).strip(), m.group(1)))
    if not out:                                   # other layout: links to /file/d/<id> followed by the title
        for m in re.finditer(r'/file/d/([\w-]{10,})[^>]*>.*?flip-entry-title"[^>]*>([^<]+)<', page, re.S):
            if m.group(1) not in seen:
                seen.add(m.group(1)); out.append((H.unescape(m.group(2)).strip(), m.group(1)))
    if not out:
        raise ValueError(_explain(page) if len(page) < 200000 and 'sign in' in page.lower() else 'Could not read the file list from that Drive folder. Check that the folder is shared with "Anyone with the link".')
    return out


def pack_ids(link):
    """{week number: file id} for the weekly packs (NNNNN_SSSdata.7z) in the folder."""
    d = {}
    for name, i in list_folder(link):
        m = re.fullmatch(r'(\d+)_SSSdata\.7z', name)
        if m:
            d[int(m.group(1))] = i
    return d


def resolve_db(link):
    """Link to the database file itself: either the file link, or the SDM-database.zip inside a folder link."""
    if is_direct(link):
        return link.strip()
    if folder_id(link):
        for name, i in list_folder(link):
            if name == DB_NAME:
                return i
        raise ValueError('That Drive folder has no %s in it yet.' % DB_NAME)
    return file_id(link)


def download_bytes(fid, limit=400 << 20):
    """A small file (a weekly pack) from Drive, held in memory."""
    r = open_download(fid)
    data = r.read(limit + 1)
    r.close()
    if len(data) > limit:
        raise ValueError('That file is bigger than expected.')
    return data


class Pull:
    def __init__(self, app, link, force=False):
        self.app, self.link, self.force = app, link, force
        self.state = dict(running=True, finished=False, phase='Contacting the download server', pct=0, error=None, result=None)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _set(self, phase, pct):
        self.state['phase'], self.state['pct'] = phase, int(pct)

    def _run(self):
        st, app = self.state, self.app
        path = app.store.path
        d = os.path.dirname(os.path.abspath(path))
        work = os.path.join(d, 'share')
        zpath, newdb = os.path.join(work, 'download.zip'), os.path.join(work, 'new-sdm.db')
        try:
            os.makedirs(work, exist_ok=True)
            for f in (zpath, newdb, newdb + '-wal', newdb + '-shm'):
                _rm(f)
            r = open_download(resolve_db(self.link))
            stamp = '%s|%s' % (r.headers.get('Last-Modified') or '', r.headers.get('Content-Length') or '')
            with app.lock:
                old_stamp = app.store.get_setting('shared_stamp', '')
            if not self.force and stamp != '|' and stamp == old_stamp:
                r.close()
                st['result'] = dict(uptodate=True)
                self._set('Already up to date', 100)
                return
            total = int(r.headers.get('Content-Length') or 0)
            if total > MAX_BYTES:
                raise ValueError('The file is bigger than expected, so it was not downloaded.')
            need = int(os.path.getsize(path) * 1.5) + total
            if _free(work) < need:
                raise OSError('Not enough free disk space (about %d MB needed).' % (need >> 20))
            done = 0
            with open(zpath, 'wb') as o:
                while True:
                    b = r.read(CHUNK)
                    if not b:
                        break
                    o.write(b)
                    done += len(b)
                    if done > MAX_BYTES:
                        raise ValueError('The download is bigger than expected, so it was stopped.')
                    self._set('Downloading', 2 + 55 * (done / total if total else 0.5))
            r.close()
            if total and done != total:
                raise OSError('The download stopped early. Try again.')
            self._set('Unpacking', 58)
            try:
                z = zipfile.ZipFile(zpath)
            except zipfile.BadZipFile:
                raise ValueError('The downloaded file is not a zip file.')
            with z:
                names = [n for n in z.namelist() if n == 'sdm.db']
                if not names:
                    raise ValueError('The zip file does not contain sdm.db.')
                size = z.getinfo('sdm.db').file_size
                if size > MAX_BYTES * 2:
                    raise ValueError('The database inside is bigger than expected.')
                got = 0
                with z.open('sdm.db') as f, open(newdb, 'wb') as o:
                    while True:
                        b = f.read(CHUNK)
                        if not b:
                            break
                        o.write(b)
                        got += len(b)
                        self._set('Unpacking', 58 + 22 * got / max(size, 1))
            _rm(zpath)
            self._set('Checking the new database', 81)
            c = sqlite3.connect(newdb)
            try:
                names_ = {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'races', 'entries', 'horses', 'weeks'} <= names_:
                    raise ValueError('The downloaded file is not a Sim Data Manager database.')
                n = c.execute('SELECT COALESCE(SUM(entries),0), COALESCE(MAX(week),0) FROM weeks').fetchone()
                if not n[0]:
                    raise ValueError('The downloaded database is empty, so it was not used.')
                # carry over this computer's own lists and settings
                self._set('Keeping your settings and lists', 88)
                c.execute('ATTACH DATABASE ? AS o', (path,))      # read only use: nothing is written to your current database
            except Exception:
                c.close()
                raise
            try:
                self._carry(c)
                c.commit()
            finally:
                c.close()
            # make sure it opens as a normal app database (adds any newer tables and indexes)
            from .store import Store
            Store(newdb).db.close()
            self._set('Swapping databases', 94)
            self._swap(path, newdb)
            with app.lock:
                app.store.set_setting('shared_stamp', stamp)
                app.store.set_setting('shared_last', json.dumps(dict(at=time.strftime('%Y-%m-%dT%H:%M:%S'), runners=n[0], last_week=n[1])))
            st['result'] = dict(runners=n[0], last_week=n[1], backup=BACKUP)
            self._set('Done', 100)
        except Exception as e:
            st['error'] = str(e)[:300] or type(e).__name__
            st['phase'] = 'Stopped'
            for f in (zpath, newdb, newdb + '-wal', newdb + '-shm'):
                _rm(f)
        finally:
            st['running'] = False
            st['finished'] = True

    @staticmethod
    def _carry(c):
        have = {x[0]: x[1] for x in c.execute("SELECT name, sql FROM o.sqlite_master WHERE type='table'")}
        for t in PRIVATE_TABLES:
            if t in have:
                c.execute('DROP TABLE IF EXISTS main."%s"' % t)
                c.execute(have[t])
                c.execute('INSERT INTO main."%s" SELECT * FROM o."%s"' % (t, t))
        if 'settings' in have:
            ph = ','.join('?' * len(KEEP_NEW_SETTINGS))
            c.execute('INSERT OR REPLACE INTO main.settings SELECT k, v FROM o.settings WHERE k NOT IN (%s)' % ph, KEEP_NEW_SETTINGS)

    def _swap(self, path, newdb):
        app = self.app
        if app.job and app.job.state.get('running'):
            raise RuntimeError('A weekly update is running. Wait for it to finish, then try again.')
        d = os.path.dirname(os.path.abspath(path))
        backup = os.path.join(d, BACKUP)
        with app.lock:
            app.store.db.commit()
            app.store.db.close()
            moved = False
            try:
                _rm(backup)
                os.replace(path, backup)
                moved = True
                for suf in ('-wal', '-shm'):
                    _rm(path + suf)
                os.replace(newdb, path)
            except Exception:
                if moved and not os.path.exists(path):
                    os.replace(backup, path)
                raise
            finally:
                from .store import Store
                app.store = Store(path)


def _free(path):
    import shutil
    return shutil.disk_usage(path).free


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass
