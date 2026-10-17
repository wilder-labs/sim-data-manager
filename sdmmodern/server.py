"""Tiny local web server that powers the Sim Data Manager window. It reads public list pages (race schedule, Find Sires table) and, once per stable group, the public
master stable page. It does not look up single horses or sires and it cannot make claims."""
import json, os, sys, time, threading, datetime, subprocess, socket, webbrowser, shutil
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from .store import Store
from . import search as S
from . import archive as A
from . import shared as SD
from . import sire as SIRE
from . import claims as CL
from . import sirefinder as SF
from . import share as SH
from . import updater as UP

STATIC = os.path.join(os.path.dirname(__file__), 'static')


class App:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.last_ping = time.time()
        self.seen_ping = False
        self.job = None
        self.pull = None
        self.share_job = None
        self.update = None
        self.quit = False
        self.bye_at = 0
        self.packs_cache = (0, [])


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200, ctype='application/json'):
            b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(b)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == '/':
                    return self._send(open(os.path.join(STATIC, 'index.html'), 'rb').read(), ctype='text/html; charset=utf-8')
                if u.path == '/api/ping':
                    app.last_ping, app.seen_ping = time.time(), True
                    return self._send({'ok': True})
                if u.path == '/api/share/status':
                    with app.lock:
                        d, last = app.store.get_setting('share_dir', ''), app.store.get_setting('share_last', '')
                    j = app.share_job
                    return self._send(dict(dir=d, last=last, job=j.state if j else None))
                if u.path == '/api/shared/status':
                    with app.lock:
                        link, last = app.store.get_setting('shared_link', '') or SD.DEFAULT_LINK, app.store.get_setting('shared_last', '')
                        sp = app.store.get_setting('pack_source', 'auto') == 'site'
                    return self._send(dict(link=link, last=last, site_packs=sp, job=app.pull.state if app.pull else None))
                if u.path == '/api/update/status':
                    if app.update and app.update.state['running']:
                        return self._send(dict(state='busy'))
                    return self._send(UP.status(app.store, app.lock))
                if u.path == '/api/update/progress':
                    return self._send(app.update.state if app.update else {'running': False, 'idle': True})
                if u.path == '/api/archive/status':
                    return self._send(app.job.state if app.job else {'running': False, 'idle': True})
                if u.path == '/api/archive/settings':
                    with app.lock:
                        return self._send(dict(auto_enabled=app.store.get_setting('auto_enabled', '0') == '1',
                                               auto_from=int(app.store.get_setting('auto_from', '0') or 0)))
                if u.path == '/api/archive/check':
                    if app.job and app.job.state['running']:
                        return self._send(dict(state='busy'))
                    try:
                        res = A.new_pack_status(app.store, app.lock)
                    except Exception as e:
                        return self._send(dict(state='error', error=str(e)[:200]))
                    with app.lock:
                        res['auto'] = app.store.get_setting('auto_enabled', '0') == '1'
                    return self._send(res)
                if u.path == '/api/archive/packs':
                    t, w = app.packs_cache
                    if time.time() - t > 600:
                        with app.lock:
                            A.configure(app.store)
                        w = A.list_packs(); app.packs_cache = (time.time(), w)
                    return self._send(dict(weeks=w, first_2020=A.FIRST_2020_PACK))
                if u.path == '/api/stable-masters':
                    # master stable for each trainer in one race: saved groups first, then one public request per unknown group
                    names, out, offline = [], {}, False
                    for n in (q.get('names') or '').split(',')[:20]:
                        n = n.strip()
                        if n and not n.startswith('*') and n.lower() not in [x.lower() for x in names]:
                            names.append(n)
                    for n in names:
                        with app.lock:
                            grp = SIRE.cached_group(app.store, n)
                        if not grp and not offline:
                            try:                        # network call made outside the lock; one group saves every member's name
                                grp = SIRE.fetch_stables(n)
                                with app.lock:
                                    SIRE.save_group(app.store, n, grp)
                                time.sleep(0.5)
                            except Exception:
                                offline = True          # stop at the first failure
                        out[n] = (grp or {}).get('master')
                    return self._send({'masters': out, 'offline': offline})
                if u.path in ('/api/my-stables', '/api/stable-group'):
                    nm = (q.get('name') or '').strip()
                    if nm.startswith('*') or not nm:
                        return self._send({'master': None, 'names': []})
                    with app.lock:
                        grp = SIRE.cached_group(app.store, nm)
                    if grp:
                        return self._send(grp)
                    try:                                # network call made outside the lock
                        grp = SIRE.fetch_stables(nm)
                        with app.lock:
                            SIRE.save_group(app.store, nm, grp)
                        return self._send(grp)
                    except ValueError as e:
                        return self._send({'error': str(e)}, 400)
                    except Exception:
                        return self._send({'master': None, 'names': [], 'offline': True})
                if u.path == '/api/sires/watch':
                    with app.lock:
                        return self._send({'watch': SF.watch_list(app.store)})
                if u.path == '/api/sires/history':
                    with app.lock:
                        return self._send({'rows': SF.history(app.store, q.get('id') or '')})
                if u.path == '/api/sire':              # cache only: a sire already saved in the database; nothing is looked up on the site
                    hid = int(q.get('hid') or 0)
                    with app.lock:
                        c = SIRE.cached(app.store, hid)
                    return self._send(c or {'sire': None})
                with app.lock:
                    if u.path == '/api/info':
                        return self._send(app.store.info())
                    if u.path == '/api/tracks':
                        return self._send(app.store.tracks())
                    if u.path == '/api/race-form':
                        try:
                            return self._send(S.race_form(app.store, float(q.get('racenum') or 0)))
                        except ValueError as e:
                            return self._send({'error': str(e)}, 400)
                    if u.path == '/api/date':
                        return self._send(S.week_to_date_info(app.store, q.get('week'), q.get('date')))
                self._send({'error': 'not found'}, 404)
            except Exception as e:
                self._send({'error': str(e)}, 400)

        def do_POST(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            n = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(n) if n else b''
            try:
                if u.path == '/api/archive/start':
                    a = json.loads(body or b'{}')
                    if app.job and app.job.state['running']:
                        return self._send({'error': 'A download is already running.'}, 400)
                    fw = int(a.get('from_week') or 0)
                    with app.lock:                      # remember the choice: from now on new weeks arrive by themselves
                        app.store.set_setting('auto_from', fw)
                        if app.store.get_setting('auto_enabled') is None:
                            app.store.set_setting('auto_enabled', '0')      # new packs are offered in a pop-up; downloading by itself is opt-in
                    app.job = A.Job(app.store, app.lock, fw, redo=bool(a.get('redo'))).start()
                    return self._send({'ok': True})
                if u.path == '/api/archive/settings':
                    a = json.loads(body or b'{}')
                    with app.lock:
                        app.store.set_setting('auto_enabled', '1' if a.get('auto_enabled') else '0')
                    return self._send({'ok': True})
                if u.path == '/api/claim-finder':
                    a = json.loads(body or b'{}')
                    today = datetime.date.today()
                    d1 = a.get('from') or (today + datetime.timedelta(days=1)).isoformat()
                    d2 = a.get('to') or (today + datetime.timedelta(days=2)).isoformat()
                    with app.lock:
                        known = list(app.store.tracks())
                    try:                        # network calls are made outside the lock
                        races = CL.collect(d1, d2, tracks=known)
                    except Exception as e:
                        return self._send({'error': 'Could not read the race schedule from eHorseRacing (%s: %s).' % (type(e).__name__, e)}, 502)
                    with app.lock:
                        rows, tracks = CL.build(app.store, races)
                    return self._send(dict(kind='claims', candidates=[], rows=rows, summary=None, truncated=False,
                                           claims=dict(first_date=d1, last_date=d2, races=len(races), horses=len(rows),
                                                       debuts=sum(1 for r in rows if r['debut']), tracks=tracks)))
                if u.path == '/api/sires/search':
                    a = json.loads(body or b'{}')
                    filt = a.get('filter') if a.get('filter') in SF.FILTERS else 'a'
                    sort = a.get('sort') if a.get('sort') in SF.SORTS else 'creditcost'
                    try:                        # network calls are made outside the lock
                        rows, more = SF.collect(filt, sort, (a.get('name') or '').strip(), a.get('loc') or 'all', int(a.get('pages') or 1))
                    except Exception as e:
                        return self._send({'error': 'Could not read the sire list from eHorseRacing (%s: %s).' % (type(e).__name__, e)}, 502)
                    with app.lock:
                        SF.snapshot(app.store, rows)
                        rg = SF.ranges(app.store, [r['sire_id'] for r in rows])
                    for r in rows:
                        r['range'] = rg.get(r['sire_id'])
                    return self._send({'rows': rows, 'more': more})
                if u.path == '/api/sires/check':        # one read of the Find Sires table (a few pages in a row), then the watch list is matched against it
                    with app.lock:
                        w = SF.watch_list(app.store)
                    try:                                # network calls are made outside the lock
                        rows, more = SF.read_table()
                    except Exception as e:
                        return self._send({'error': 'Could not read the sire list from eHorseRacing (%s: %s).' % (type(e).__name__, e)}, 502)
                    found = SF.found_from(rows, w, more)
                    with app.lock:
                        SF.snapshot(app.store, rows)
                        res = SF.apply_check(app.store, w, found)
                        wl = SF.watch_list(app.store)
                    return self._send({'results': res, 'watch': wl})
                if u.path == '/api/sires/watch':
                    a = json.loads(body or b'{}')
                    with app.lock:
                        SF.watch_set(app.store, str(a.get('sire_id') or ''), a.get('name') or '', a.get('max_cost'), a.get('max_bp'))
                        return self._send({'watch': SF.watch_list(app.store)})
                if u.path == '/api/sires/watch/delete':
                    a = json.loads(body or b'{}')
                    with app.lock:
                        SF.watch_delete(app.store, str(a.get('sire_id') or ''))
                        return self._send({'watch': SF.watch_list(app.store)})
                if u.path.startswith('/api/share/'):
                    if self.headers.get('X-SDM') != '1':        # a web page on another site cannot add this header
                        return self._send({'error': 'Request refused.'}, 403)
                    a = json.loads(body or b'{}')
                    if u.path == '/api/share/start':
                        if app.share_job and app.share_job.state['running']:
                            return self._send({'error': 'A share is already running.'}, 400)
                        folder = (a.get('dir') or '').strip().strip('"')
                        try:
                            SH.check_dest(folder)
                        except ValueError as e:
                            return self._send({'error': str(e)}, 400)
                        with app.lock:
                            app.store.set_setting('share_dir', folder)
                        job = SH.ShareJob(app.store.path, folder)
                        orig = job._run
                        def run_and_note():
                            orig()
                            if job.state.get('result'):
                                with app.lock:
                                    app.store.set_setting('share_last', json.dumps(job.state['result']))
                        job.thread = threading.Thread(target=run_and_note, daemon=True)
                        app.share_job = job.start()
                        return self._send({'ok': True})
                    return self._send({'error': 'Unknown request.'}, 404)
                if u.path.startswith('/api/update/'):
                    if self.headers.get('X-SDM') != '1':
                        return self._send({'error': 'Request refused.'}, 403)
                    if u.path == '/api/update/check':
                        return self._send(UP.status(app.store, app.lock, force=True))
                    if u.path == '/api/update/install':
                        if app.update and app.update.state['running']:
                            return self._send({'error': 'An update is already running.'}, 400)
                        if (app.job and app.job.state['running']) or (app.pull and app.pull.state['running']):
                            return self._send({'error': 'A download is running. Wait for it to finish, then update.'}, 400)
                        try:
                            rel = UP.fetch_latest()
                        except Exception as e:
                            return self._send({'error': 'Could not reach GitHub: %s' % str(e)[:150]}, 400)
                        if not rel or not UP.is_newer(rel['tag']):
                            return self._send({'error': 'You already have the newest version.'}, 400)
                        app.update = UP.Install(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel).start()
                        return self._send({'ok': True})
                    if u.path == '/api/update/quit':
                        app.quit = True
                        return self._send({'ok': True})
                    return self._send({'error': 'Unknown request.'}, 404)
                if u.path.startswith('/api/shared/'):
                    if self.headers.get('X-SDM') != '1':        # a web page on another site cannot add this header
                        return self._send({'error': 'Request refused.'}, 403)
                    a = json.loads(body or b'{}')
                    if u.path == '/api/shared/link':
                        link = (a.get('link') or '').strip()
                        try:
                            SD.check_link(link)
                        except ValueError as e:
                            return self._send({'error': str(e)}, 400)
                        with app.lock:
                            app.store.set_setting('shared_link', link)
                            app.store.set_setting('shared_stamp', '')
                            app.store.set_setting('pack_source', 'site' if a.get('site_packs') else 'auto')
                        app.packs_cache = (0, [])
                        return self._send({'ok': True})
                    if u.path == '/api/shared/start':
                        if app.pull and app.pull.state['running']:
                            return self._send({'error': 'A download is already running.'}, 400)
                        if app.job and app.job.state['running']:
                            return self._send({'error': 'A weekly update is running. Wait for it to finish first.'}, 400)
                        with app.lock:
                            link = app.store.get_setting('shared_link', '') or SD.DEFAULT_LINK
                        if not link:
                            return self._send({'error': 'Paste the database link first.'}, 400)
                        app.pull = SD.Pull(app, link, bool(a.get('force'))).start()
                        return self._send({'ok': True})
                    return self._send({'error': 'Unknown request.'}, 404)
                if u.path == '/api/bye':                   # the window is closing (or reloading); a reload pings again within seconds
                    app.bye_at = time.time()
                    return self._send({'ok': True})
                if u.path == '/api/quit':
                    if self.headers.get('X-SDM') != '1':
                        return self._send({'error': 'Request refused.'}, 403)
                    app.quit = True
                    return self._send({'ok': True})
                if u.path == '/api/archive/cancel':
                    if app.job: app.job.cancel()
                    return self._send({'ok': True})
                with app.lock:
                    st = app.store
                    if u.path == '/api/search':
                        a = json.loads(body)
                        return self._send(S.search(st, a.get('kind', 'horse'), name=a.get('name', ''), hid=a.get('hid'),
                                                   first_week=a.get('first_week'), last_week=a.get('last_week'),
                                                   two_yo=bool(a.get('two_yo')), three_yo=bool(a.get('three_yo')), graded=bool(a.get('graded')), current_owner_only=bool(a.get('current_owner_only')),
                                                   min_speed=float(a.get('min_speed') or 0), exact=bool(a.get('exact', True))))
                    if u.path == '/api/owner-changes':
                        a = json.loads(body)
                        return self._send(S.owner_changes(st, first_week=a.get('first_week'), last_week=a.get('last_week'),
                                                          two_yo=bool(a.get('two_yo')), three_yo=bool(a.get('three_yo')), graded=bool(a.get('graded')), min_speed=float(a.get('min_speed') or 0), no_cpu=bool(a.get('no_cpu', True))))
                    if u.path == '/api/best-speeds':
                        a = json.loads(body)
                        return self._send(S.best_speeds(st, two_yo=bool(a.get('two_yo')), three_yo=bool(a.get('three_yo')), graded=bool(a.get('graded')), min_speed=float(a.get('min_speed') or 0)))
                    if u.path == '/api/current-horses':
                        a = json.loads(body)
                        return self._send(S.current_horses(st, a.get('name', ''), exact=bool(a.get('exact', True))))
                    if u.path == '/api/first-time-starters':
                        return self._send(S.first_time_starters(st))
                    if u.path == '/api/graded-races':
                        a = json.loads(body)
                        return self._send(S.graded_races(st, first_week=a.get('first_week'), last_week=a.get('last_week'),
                                                         two_yo=bool(a.get('two_yo')), three_yo=bool(a.get('three_yo')),
                                                         graded=bool(a.get('graded'))))
                    if u.path == '/api/stakes-year':
                        return self._send(S.stakes_this_year(st))
                    if u.path == '/api/owner-rankings':
                        return self._send(S.owner_rankings(st))
                    if u.path == '/api/import/preview':
                        return self._send(st.preview_mdb(body))
                    if u.path == '/api/import':
                        res = st.import_mdb(body, replace=q.get('replace') == '1')
                        mm = __import__('re').search(r'(\d+)_SSSdata', q.get('name', ''))
                        if mm:                          # a pack added by hand counts as downloaded, so auto-update will not fetch it again
                            st.db.execute("INSERT OR REPLACE INTO packs VALUES(?,?)", (int(mm.group(1)), 'added from file'))
                            st.db.commit()
                        return self._send(res)
                    if u.path == '/api/remove':
                        a = json.loads(body)
                        return self._send(st.remove_weeks(int(a['first']), int(a['last'])))
                    if u.path == '/api/tracks':
                        a = json.loads(body)
                        st.set_track(a['num'], a['code'])
                        return self._send(st.tracks())
                    if u.path == '/api/anchor':
                        a = json.loads(body)
                        st.set_anchor(a['week'], datetime.date.fromisoformat(a['date']))
                        return self._send(st.info())
                    if u.path == '/api/compact':
                        st.compact()
                        return self._send({'ok': True, 'size': os.path.getsize(st.path)})
                self._send({'error': 'not found'}, 404)
            except Exception as e:
                self._send({'error': str(e)}, 400)
    return H


def default_browser_exe():
    """Windows: the program set as the default for http links, if it is a Chromium browser that supports --app."""
    if not sys.platform.startswith('win'):
        return None
    try:
        import winreg, re
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice')
        prog = winreg.QueryValueEx(k, 'ProgId')[0]
        c = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, prog + r'\shell\open\command')
        m = re.match(r'\s*"([^"]+)"', c) or re.match(r'\s*(\S+)', c)
        exe = m.group(1) if m else None
        if exe and os.path.exists(exe) and os.path.basename(exe).lower() in ('chrome.exe', 'msedge.exe', 'brave.exe', 'vivaldi.exe'):
            return exe
    except Exception:
        pass
    return None


def find_app_browser():
    cands = [default_browser_exe()]
    if sys.platform.startswith('win'):
        for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'), os.environ.get('LOCALAPPDATA')):
            if base:
                cands += [os.path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe')]
        for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'), os.environ.get('LOCALAPPDATA')):
            if base:                                     # Edge only if Chrome isn't installed
                cands += [os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe')]
    elif sys.platform == 'darwin':
        cands += ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge']
    else:
        cands += [shutil.which(x) for x in ('google-chrome', 'chromium', 'chromium-browser', 'microsoft-edge')]
    return next((c for c in cands if c and os.path.exists(c)), None)


def run(db_path=None, open_window=True, port=0):
    if sys.stdout is None:          # pythonw has no console
        sys.stdout = sys.stderr = open(os.devnull, 'w')
    store = Store(db_path)
    from . import viewer as _vw; _vw.use_store(store)
    app = App(store)
    srv = ThreadingHTTPServer(('127.0.0.1', port), make_handler(app))
    url = 'http://127.0.0.1:%d/' % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if os.environ.get('SDM_NO_AUTOUPDATE') != '1':
        A.AutoUpdater(app)
    print('Sim Data Manager is running at', url)
    print('Database:', store.path)
    if open_window:
        exe = find_app_browser()
        if exe:
            prof = os.path.join(os.path.dirname(store.path), 'window-profile')
            subprocess.Popen([exe, '--app=' + url, '--user-data-dir=' + prof, '--window-size=1920,1080', '--no-first-run'])
        else:
            webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
            now = time.time()
            busy = any(j and j.state.get('running') for j in (app.job, app.pull, app.share_job, app.update))
            if app.quit:
                break       # the user chose Exit, or closed the app after an update
            if app.seen_ping and not busy and app.bye_at and now - app.bye_at > 20 and app.last_ping < app.bye_at:
                break       # the window was closed and did not come back within 20 seconds
            if app.seen_ping and now - app.last_ping > 180:
                break       # no window for 3 minutes (a download that was running is stopped)
    except KeyboardInterrupt:
        pass
    srv.shutdown()
