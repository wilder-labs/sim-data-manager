"""Hosted, read-only demo of Sim Data Manager (for Railway or any server).

It serves the same search page, but only the search routes. No logins, no updates, no downloads of other data,
no changes to the database, no way to quit. The data is one year of the public shared race database.

Settings (environment variables):
  PORT       port to listen on (the host sets this)
  DATA_DIR   a folder that survives restarts, for example a Railway volume mounted at /data (default ./data)
  DB_URL     address of the one-year database zip (default: SDM-2026.zip on this project's 'data' release)
  DEMO_LABEL text shown in the banner (default: Demo: 2026 races only)
"""
import os, sys, time, json, shutil, zipfile, threading, collections, importlib
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

DEFAULT_URL = 'https://github.com/wilder-labs/sim-data-manager/releases/download/data/SDM-2026.zip'
GET_OK = {'/', '/api/info', '/api/tracks', '/api/date', '/api/race-form', '/api/ping', '/healthz'}
POST_OK = {'/api/search', '/api/owner-changes', '/api/best-speeds', '/api/current-horses', '/api/first-time-starters',
           '/api/graded-races', '/api/stakes-year', '/api/owner-rankings'}
MAX_BODY = 20000                      # a search request is a few hundred bytes
RATE_PER_MIN = 120                    # per visitor address
QUERY_SECONDS = 8                     # a query running longer than this is stopped
HIDE_JS = r"""<style>#datachip{pointer-events:none}.hosted-bar{background:#243b53;color:#fff;padding:6px 14px;font:13px system-ui,sans-serif;text-align:center}</style>
<script>(function(){var hide=['denovo','import','shared','share','remove','compact','update','exit','simreset','archive','tracks'];var ids=['mystfs','claims','sirefind'];
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
    log('database ready: %.0f MB' % (os.path.getsize(db) / 1048576))
    return db


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


def build_handler(app, label, limiter):
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
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
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
            if n < 0 or n > MAX_BODY:
                return self._deny(413, 'request too large')
            return super().do_POST()

        def do_PUT(self):
            self._deny(405, 'not allowed')
        do_DELETE = do_PATCH = do_PUT
    return G


def main():
    from sdmmodern.store import Store
    from sdmmodern import server as SV
    from sdmmodern import viewer as VW
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
    srv = ThreadingHTTPServer(('0.0.0.0', port), build_handler(app, label, Limiter()))
    srv.daemon_threads = True
    log('listening on port', port)
    srv.serve_forever()


if __name__ == '__main__':
    main()
