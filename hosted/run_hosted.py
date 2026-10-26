"""Hosted, read-only demo of Sim Data Manager (for Railway or any server).

It serves the same search page, but only the search routes. No logins, no updates, no downloads of other data,
no changes to the database, no way to quit. The data is one year of the public shared race database.

Settings (environment variables):
  PORT       port to listen on (the host sets this)
  DATA_DIR   a folder that survives restarts, for example a Railway volume mounted at /data (default ./data)
  DB_URL     address of the one-year database zip (default: SDM-2026.zip on this project's 'data' release)
  DEMO_LABEL text shown in the banner (default: Demo: 2026 races only)
  QUERY_SECONDS longest a single search may run before it is stopped (default 20)
  AUTO_UPDATE set to 1 to check the release every 30 minutes (CHECK_MINUTES changes it, 10 at the least); when the database file there changes, download it
             beside the old one, then restart so the new one is used (the host starts the app again)
"""
import os, sys, io, time, json, shutil, zipfile, threading, collections, importlib
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

DEFAULT_URL = 'https://github.com/wilder-labs/sim-data-manager/releases/download/data/SDM-2026.zip'
GET_OK = {'/', '/m', '/api/info', '/api/tracks', '/api/date', '/api/race-form', '/api/ping', '/healthz',
          '/top25', '/api/top25',
          '/residency', '/api/residency/status', '/api/residency/roster', '/api/residency/board', '/api/residency/export'}           # Top 25 Speeds: read-only lists from files in the program; adding files stays off
POST_OK = {'/api/search', '/api/owner-changes', '/api/best-speeds', '/api/current-horses', '/api/first-time-starters',
           '/api/graded-races', '/api/stakes-year', '/api/owner-rankings',
           '/api/residency/upload', '/api/residency/rules'}   # the two residency routes also need the upload key
MAX_BODY = 20000                      # a search request is a few hundred bytes
MAX_BODY_BY_PATH = {'/api/residency/upload': 4 * 1024 * 1024, '/api/residency/rules': 4000}
RATE_PER_MIN = 120                    # per visitor address
QUERY_SECONDS = int(os.environ.get('QUERY_SECONDS', '20'))   # a query running longer than this is stopped
CACHE_PATHS = {'/api/owner-rankings', '/api/first-time-starters', '/api/stakes-year', '/api/best-speeds', '/api/graded-races'}
CACHE_MAX_BYTES = 64 * 1048576        # all kept answers together; the oldest are dropped first
WARM_PATHS = ['/api/owner-rankings', '/api/first-time-starters', '/api/stakes-year']
HIDE_JS = r"""<style>#datachip{pointer-events:none}.hosted-bar{background:#243b53;color:#fff;padding:6px 14px;font:13px system-ui,sans-serif;text-align:center}</style>
<script>(function(){try{if(Math.min(screen.width,screen.height)<700&&location.search.indexOf('desktop')<0){location.replace('/m');return}}catch(e){}try{Object.keys(DEFHID).forEach(function(k){DEFHID[k]=DEFHID[k].filter(function(x){return x==='hid'})})}catch(e){}var hide=['t25import','denovo','import','shared','share','remove','compact','update','exit','simreset','archive','tracks'];var ids=['mystfs','claims','sirefind'];
function go(){var d=document;ids.forEach(function(i){var e=d.getElementById(i);if(e)e.style.display='none'});var em=d.getElementById('empty');if(em&&em.textContent.indexOf('First time')>-1)em.textContent='Search for a horse, a trainer or a race. This demo holds 2026 races only.';hide.forEach(function(a){d.querySelectorAll('[data-act="'+a+'"]').forEach(function(e){e.style.display='none'})});
d.querySelectorAll('.menu .drop hr + hr').forEach(function(e){e.style.display='none'});
if(!d.querySelector('.hosted-bar')){var b=d.createElement('div');b.className='hosted-bar';b.textContent=%LABEL%;d.body.insertBefore(b,d.body.firstChild)}}
if(document.readyState!=='loading')go();else document.addEventListener('DOMContentLoaded',go);setInterval(go,1500)})();</script>"""


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def ensure_db(data_dir, url):
    """Gets the one-year database onto the volume the first time (or when REFRESH=1). Only github.com addresses are accepted."""
    from sdmmodern import updater as UP
    os.makedirs(data_dir, exist_ok=True)
    db = os.path.join(data_dir, 'sdm.db')
    new, mark = db + '.new', os.path.join(data_dir, 'db.version')
    if os.path.exists(new) and os.path.exists(new + '.ok'):      # a finished download is waiting: swap it in
        for suf in ('', '-wal', '-shm'):
            if os.path.exists(db + suf):
                os.remove(db + suf)
        os.replace(new, db)
        os.replace(new + '.ok', mark)
        log('new database in place: %.0f MB' % (os.path.getsize(db) / 1048576))
        return db
    if os.path.exists(db) and os.environ.get('REFRESH') != '1':
        log('database already on the volume: %.0f MB' % (os.path.getsize(db) / 1048576))
        return db
    if not url.startswith('https://github.com/wilder-labs/sim-data-manager/releases/download/'):
        raise SystemExit('DB_URL must be a release file of wilder-labs/sim-data-manager')
    tmpz, tmpdb = os.path.join(data_dir, 'download.zip'), os.path.join(data_dir, 'sdm.db.new')
    log('downloading', url)
    with UP._open(url) as r, open(tmpz, 'wb') as f:
        shutil.copyfileobj(r, f, 1 << 20)
    with zipfile.ZipFile(tmpz) as z:
        if z.namelist() != ['sdm.db']:
            raise SystemExit('the database zip should hold only sdm.db')
        with z.open('sdm.db') as s, open(tmpdb, 'wb') as o:
            shutil.copyfileobj(s, o, 1 << 20)
    os.remove(tmpz)
    for suf in ('', '-wal', '-shm'):
        if os.path.exists(db + suf):
            os.remove(db + suf)
    os.replace(tmpdb, db)
    try:
        open(mark, 'w').write(latest_version(url))
    except Exception:
        pass
    log('database ready: %.0f MB' % (os.path.getsize(db) / 1048576))
    return db


def latest_version(url):
    """Asks GitHub which version of the release file is newest. Returns a short text, or '' if it could not tell."""
    import urllib.request
    name = url.rsplit('/', 1)[-1]
    req = urllib.request.Request('https://api.github.com/repos/wilder-labs/sim-data-manager/releases/tags/data',
                                 headers={'User-Agent': 'sdm-hosted', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        for a in json.loads(r.read(2000000)).get('assets', []):
            if a.get('name') == name:
                return '%s|%s|%s' % (a.get('id'), a.get('updated_at'), a.get('size'))
    return ''


def fetch_new(url, data_dir, version):
    """Downloads the new database beside the old one. The running app keeps using the old file until it restarts."""
    from sdmmodern import updater as UP
    if not url.startswith('https://github.com/wilder-labs/sim-data-manager/releases/download/'):
        raise RuntimeError('DB_URL must be a release file of wilder-labs/sim-data-manager')
    db = os.path.join(data_dir, 'sdm.db')
    tmpz, new = os.path.join(data_dir, 'download.zip'), db + '.new'
    for f in (new, new + '.ok'):
        if os.path.exists(f):
            os.remove(f)
    log('new database found, downloading', version)
    with UP._open(url) as r, open(tmpz, 'wb') as f:
        shutil.copyfileobj(r, f, 1 << 20)
    with zipfile.ZipFile(tmpz) as z:
        if z.namelist() != ['sdm.db']:
            raise RuntimeError('the database zip should hold only sdm.db')
        with z.open('sdm.db') as s, open(new, 'wb') as o:
            shutil.copyfileobj(s, o, 1 << 20)
    os.remove(tmpz)
    import sqlite3
    c = sqlite3.connect('file:%s?mode=ro' % new.replace('\\', '/'), uri=True)   # sanity check before swapping
    try:
        if c.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
            raise RuntimeError('new database failed its check')
        c.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
    finally:
        c.close()
    with open(new + '.ok', 'w') as f:
        f.write(version)


def check_seconds():
    """How long to wait between release checks. CHECK_MINUTES sets it; default 30, never under 10."""
    try:
        m = int(os.environ.get('CHECK_MINUTES', '30'))
    except ValueError:
        m = 30
    return max(10, m) * 60


def updater_loop(url, data_dir):
    mark = os.path.join(data_dir, 'db.version')
    while True:
        try:
            v = latest_version(url)
            have = open(mark).read().strip() if os.path.exists(mark) else ''
            if v and v != have:
                fetch_new(url, data_dir, v)
                log('restarting to use the new database')
                time.sleep(2)
                os._exit(1)                            # non-zero so the host starts the app again
            else:
                log('database is current' if v else 'could not read the release version, will try later')
        except Exception as e:
            log('update check failed:', type(e).__name__)
        time.sleep(check_seconds())


class TimedLock:
    """The app's one lock. While it is held, a query that runs too long is interrupted so one request cannot block everyone."""
    def __init__(self, conn_getter):
        self._l, self._conn, self.deadline = threading.Lock(), conn_getter, 0

    def __enter__(self):
        self._l.acquire()
        self.deadline = time.time() + QUERY_SECONDS
        return self

    def __exit__(self, *a):
        self.deadline = 0
        self._l.release()


class AnswerCache:
    """Keeps the answers to the heavy list pages in memory. The data only changes when a new database is installed,
    and that restarts the app, so the cache is empty again after every update. Only whitelisted pages with small
    request bodies are kept, and the total size is capped."""
    def __init__(self):
        self.d, self.size, self.lock, self.keylocks = collections.OrderedDict(), 0, threading.Lock(), {}

    def get(self, key):
        with self.lock:
            v = self.d.get(key)
            if v is not None:
                self.d.move_to_end(key)
            return v

    def put(self, key, b):
        with self.lock:
            if len(b) > CACHE_MAX_BYTES // 2:
                return
            if key in self.d:
                self.size -= len(self.d.pop(key))
            self.d[key] = b
            self.size += len(b)
            while self.size > CACHE_MAX_BYTES and self.d:
                self.size -= len(self.d.popitem(last=False)[1])

    def keylock(self, key):
        with self.lock:
            if len(self.keylocks) > 200:
                self.keylocks.clear()
            return self.keylocks.setdefault(key, threading.Lock())


class Limiter:
    def __init__(self):
        self.hits, self.lock = collections.defaultdict(collections.deque), threading.Lock()

    def ok(self, ip):
        now = time.time()
        with self.lock:
            q = self.hits[ip]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= RATE_PER_MIN:
                return False
            q.append(now)
            if len(self.hits) > 5000:
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 60]:
                    self.hits.pop(k, None)
            return True


def build_handler(app, label, limiter, cache):
    from sdmmodern.server import make_handler
    H = make_handler(app)
    page_extra = HIDE_JS.replace('%LABEL%', json.dumps(label))

    class G(H):
        timeout = 15                                    # a connection that stalls is dropped

        def _ip(self):
            xff = self.headers.get('X-Forwarded-For', '')
            return (xff.split(',')[-1].strip() if xff else self.client_address[0]) or '?'

        def end_headers(self):
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')   # the Top 25 Speeds screen opens inside the page; other sites still cannot frame it
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
            super().end_headers()

        def _deny(self, code, msg):
            b = json.dumps({'error': msg}).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _guard(self, ok_set):
            path = self.path.split('?', 1)[0]
            if path not in ok_set:
                self._deny(404, 'not available in the demo')
                return None
            if not limiter.ok(self._ip()):
                self._deny(429, 'too many requests, wait a minute')
                return None
            return path

        def do_GET(self):
            path = self._guard(GET_OK)
            if path is None:
                return
            if path == '/healthz':
                return self._send({'ok': True})
            if path == '/m':
                try:
                    return self._send(open(os.path.join(HERE, 'm.html'), 'rb').read(), ctype='text/html; charset=utf-8')
                except OSError:
                    return self._deny(500, 'page missing')
            if path == '/':
                try:
                    html = open(os.path.join(ROOT, 'sdmmodern', 'static', 'index.html'), 'rb').read().decode('utf-8')
                    html = html.replace('</body>', page_extra + '</body>', 1) if '</body>' in html else html + page_extra
                    return self._send(html.encode('utf-8'), ctype='text/html; charset=utf-8')
                except OSError:
                    return self._deny(500, 'page missing')
            return super().do_GET()

        def do_POST(self):
            path = self._guard(POST_OK)
            if path is None:
                return
            try:
                n = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY_BY_PATH.get(path, MAX_BODY):
                return self._deny(413, 'request too large')
            if path not in CACHE_PATHS or n > 400:
                return super().do_POST()
            body = self.rfile.read(n) if n else b''
            key = (path, body)
            hit = cache.get(key)
            if hit is None:
                with cache.keylock(key):                 # two visitors asking at once: one works, the other waits for the answer
                    hit = cache.get(key)
                    if hit is None:
                        self.rfile = io.BytesIO(body)
                        self._capkey = key
                        return super().do_POST()
            return super()._send(hit)

        def _send(self, obj, code=200, ctype='application/json'):
            b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            k = getattr(self, '_capkey', None)
            if k and code == 200:
                cache.put(k, b)
            return super()._send(b, code, ctype)

        def do_PUT(self):
            self._deny(405, 'not allowed')
        do_DELETE = do_PATCH = do_PUT
    return G


def warm_up(port):
    """Asks for the slow list pages once after start, so the first visitor does not wait for them."""
    import urllib.request
    time.sleep(5)
    for path in WARM_PATHS:
        t0 = time.time()
        try:
            req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), data=b'{}', headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=120).read()
            log('warmed', path, '%.1fs' % (time.time() - t0))
        except Exception as e:
            log('warm-up skipped', path, type(e).__name__)


def main():
    from sdmmodern.store import Store
    from sdmmodern import server as SV
    from sdmmodern import viewer as VW
    os.environ.setdefault('SDM_HOSTED', '1')            # the residency portal then needs SDM_RESIDENCY_KEY to accept uploads
    port = int(os.environ.get('PORT', '8080'))
    data_dir = os.environ.get('DATA_DIR', os.path.join(ROOT, 'data'))
    db = ensure_db(data_dir, os.environ.get('DB_URL', DEFAULT_URL))
    store = Store(db)
    VW.use_store(store)
    app = SV.App(store)
    tl = TimedLock(None)
    app.lock = tl
    store.db.set_progress_handler(lambda: 1 if tl.deadline and time.time() > tl.deadline else 0, 20000)
    label = os.environ.get('DEMO_LABEL', 'Demo: 2026 races only. Read-only.')
    if os.environ.get('AUTO_UPDATE') == '1':
        threading.Thread(target=updater_loop, args=(os.environ.get('DB_URL', DEFAULT_URL), data_dir), daemon=True).start()
    srv = ThreadingHTTPServer(('0.0.0.0', port), build_handler(app, label, Limiter(), AnswerCache()))
    srv.daemon_threads = True
    threading.Thread(target=warm_up, args=(port,), daemon=True).start()
    log('listening on port', port)
    srv.serve_forever()


if __name__ == '__main__':
    main()

