"""Checks the project's GitHub releases for a newer version of the app and, when the user agrees, swaps in the new program files.
Only the sdmmodern folder is replaced. The database, settings, saved stables and the python folder are not touched.
The old program files are kept as sdmmodern.prev until the next update."""
import os, re, io, json, time, shutil, hashlib, zipfile, threading, urllib.request, urllib.parse, urllib.error
from . import APP_VERSION

REPO = 'wilder-labs/sim-data-manager'                      # fixed on purpose: the app never updates from any other address
API = 'https://api.github.com/repos/%s/releases/latest' % REPO
ASSET = 'SDM-app-update.zip'
DL_PREFIX = 'https://github.com/%s/releases/download/' % REPO
HOSTS = {'api.github.com', 'github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com'}
UA = 'SimDataManager/%s (update check)' % APP_VERSION
CHECK_EVERY = 24 * 3600
MAX_ZIP = 60 << 20            # the program files are a few MB; refuse anything near this
MAX_UNPACKED = 200 << 20
PKG = 'sdmmodern'


def parse_version(s):
    """'v0.9.1' -> (0, 9, 1). Returns None when it is not a plain number version."""
    m = re.fullmatch(r'v?(\d+(?:\.\d+){0,3})', (s or '').strip())
    return tuple(int(x) for x in m.group(1).split('.')) if m else None


def is_newer(tag, current=APP_VERSION):
    a, b = parse_version(tag), parse_version(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urllib.parse.urlparse(newurl)
        if p.scheme != 'https' or p.hostname not in HOSTS:
            raise urllib.error.URLError('The download was sent somewhere unexpected, so it was stopped.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, accept='*/*'):
    p = urllib.parse.urlparse(url)
    if p.scheme != 'https' or p.hostname not in HOSTS:
        raise ValueError('Refusing to download from an unexpected address.')
    op = urllib.request.build_opener(_Redirect)
    return op.open(urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': accept}), timeout=60)


def fetch_latest():
    """Reads the newest release. Returns dict(tag, notes, url, sha256, size) or None when there is no usable release."""
    with _open(API, 'application/vnd.github+json') as r:
        d = json.loads(r.read(1 << 20).decode('utf-8', 'replace'))
    if d.get('draft') or d.get('prerelease'):
        return None
    tag = str(d.get('tag_name') or '')
    if not parse_version(tag):
        return None
    for a in d.get('assets') or []:
        if a.get('name') == ASSET:
            url = str(a.get('browser_download_url') or '')
            if not url.startswith(DL_PREFIX):
                return None
            dig = str(a.get('digest') or '')
            return dict(tag=tag, notes=str(d.get('body') or '')[:3000], url=url,
                        sha256=dig.split(':', 1)[1].lower() if dig.startswith('sha256:') else '', size=int(a.get('size') or 0))
    return None


def status(store, lock, force=False, now=None):
    """What the pop-up needs. Reads GitHub at most once a day (or when force is set); the answer is cached in settings."""
    now = now or time.time()
    with lock:
        cached = store.get_setting('update_check', '')
    c = None
    try:
        c = json.loads(cached) if cached else None
    except ValueError:
        c = None
    if force or not c or now - c.get('at', 0) > CHECK_EVERY or c.get('seen_version') != APP_VERSION:
        try:
            rel = fetch_latest()
        except Exception as e:
            if force:
                return dict(state='error', current=APP_VERSION, error='Could not reach GitHub: %s' % str(e)[:150])
            return dict(state='unknown', current=APP_VERSION)      # offline: stay quiet, try again next launch
        c = dict(at=now, seen_version=APP_VERSION, rel=rel)
        with lock:
            store.set_setting('update_check', json.dumps(c))
    rel = c.get('rel')
    if rel and is_newer(rel['tag']):
        return dict(state='available', current=APP_VERSION, tag=rel['tag'], notes=rel['notes'], size=rel['size'])
    return dict(state='current', current=APP_VERSION)


def _safe_members(z):
    """Only files inside sdmmodern/, no absolute paths, no '..', and a sane total size."""
    total, names = 0, []
    for i in z.infolist():
        n = i.filename.replace('\\', '/')
        if n.startswith('/') or '..' in n.split('/') or re.match(r'^[A-Za-z]:', n):
            raise ValueError('The update file has an unsafe path in it.')
        if not (n == PKG + '/' or n.startswith(PKG + '/')):
            raise ValueError('The update file has something outside the program folder.')
        if n.endswith('/'):
            continue
        total += i.file_size
        names.append((i, n))
    if total > MAX_UNPACKED:
        raise ValueError('The update file is larger than expected.')
    have = {n for _, n in names}
    for need in (PKG + '/server.py', PKG + '/__init__.py', PKG + '/static/index.html'):
        if need not in have:
            raise ValueError('The update file is missing %s.' % need.split('/', 1)[1])
    return names


class Install:
    """Downloads, checks and swaps in the new program files in the background. state is polled by the page."""
    def __init__(self, base_dir, rel):
        self.base, self.rel = base_dir, rel                 # base_dir = the folder that holds sdmmodern
        self.state = dict(running=True, phase='Starting', done=False, error='', tag=rel['tag'], pct=0)
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _set(self, **k):
        self.state.update(k)

    def _run(self):
        new = os.path.join(self.base, PKG + '.new')
        cur = os.path.join(self.base, PKG)
        prev = os.path.join(self.base, PKG + '.prev')
        try:
            if not os.path.isdir(cur):
                raise ValueError('Could not find the program folder to update.')
            self._set(phase='Downloading')
            buf = io.BytesIO()
            with _open(self.rel['url']) as r:
                total = int(r.headers.get('Content-Length') or self.rel.get('size') or 0)
                while True:
                    b = r.read(1 << 16)
                    if not b:
                        break
                    buf.write(b)
                    if buf.tell() > MAX_ZIP:
                        raise ValueError('The update file is larger than expected.')
                    if total:
                        self._set(pct=min(99, int(buf.tell() * 100 / total)))
            data = buf.getvalue()
            self._set(phase='Checking the download', pct=100)
            want = self.rel.get('sha256')
            if want and hashlib.sha256(data).hexdigest() != want:
                raise ValueError('The download did not match its checksum, so nothing was changed.')
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                members = _safe_members(z)
                shutil.rmtree(new, ignore_errors=True)
                self._set(phase='Unpacking')
                for info, n in members:
                    dest = os.path.join(new, *n.split('/')[1:])
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with z.open(info) as src, open(dest, 'wb') as out:
                        shutil.copyfileobj(src, out)
            self._set(phase='Installing')
            shutil.rmtree(prev, ignore_errors=True)
            os.rename(cur, prev)
            try:
                os.rename(new, cur)
            except OSError:
                os.rename(prev, cur)                         # put the old files back
                raise
            self._set(running=False, done=True, phase='Updated to %s' % self.rel['tag'])
        except Exception as e:
            shutil.rmtree(new, ignore_errors=True)
            self._set(running=False, done=False, phase='Failed', error=str(e)[:250] or 'The update failed. Nothing was changed.')

