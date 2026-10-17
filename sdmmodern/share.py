"""Makes a shareable copy of the race database (race data only) and copies it into a folder, for example a Google Drive folder.
The copy is written over the previous one in place, so a Drive share link keeps working."""
import os, sqlite3, threading, time, zipfile, datetime

NAME = 'SDM-database.zip'
README = 'SDM-database-read-me.txt'
DROP_TABLES = ('stable_groups', 'sire_watch', 'sire_prices', 'race_calls')   # your own lists and caches
KEEP_SETTINGS = ('anchor_week', 'anchor_date', 'earn_v', 'oc_sig')           # date anchor etc.; your update choices stay private
CHUNK = 1 << 20

README_TEXT = """Sim Data Manager: shared race database
Updated: %s
Races up to sim day %s, %s runners.

How to use it
1. Close Sim Data Manager.
2. Unzip %s. It holds one file, sdm.db.
3. Put sdm.db in your Sim Data Manager folder (next to run_sdm.py), replacing the old sdm.db if there is one.
   Delete sdm.db-wal and sdm.db-shm in that folder if they are there.
4. Start the app. Use Data > Get New Race Data to add anything newer.
It holds race data only. Nobody's settings, logins or saved lists are in it.
"""


class ShareJob:
    def __init__(self, db_path, dest_dir, lock=None):
        self.db_path, self.dest, self.lock = db_path, dest_dir, lock
        self.work = os.path.join(os.path.dirname(db_path), 'share')
        self.state = dict(running=True, finished=False, phase='Starting', pct=0, error=None, result=None)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _set(self, phase, pct):
        self.state['phase'], self.state['pct'] = phase, int(pct)

    def _run(self):
        st = self.state
        tmpdb = os.path.join(self.work, 'sdm.db')
        tmpzip = os.path.join(self.work, NAME)
        try:
            check_dest(self.dest)
            os.makedirs(self.work, exist_ok=True)
            need = int(os.path.getsize(self.db_path) * 1.3) + (50 << 20)
            if _free(self.work) < need:
                raise OSError('Not enough free disk space for the working copy (about %d MB needed).' % (need >> 20))
            for f in (tmpdb, tmpdb + '-wal', tmpdb + '-shm', tmpdb + '-journal', tmpzip):
                _rm(f)
            # 1. consistent snapshot, taken without stopping the app
            self._set('Copying the database', 1)
            src = sqlite3.connect(self.db_path, timeout=60)
            src.execute('PRAGMA query_only=1')
            dst = sqlite3.connect(tmpdb)
            def prog(status, remaining, total):
                if total:
                    self._set('Copying the database', 1 + 39 * (total - remaining) / total)
            try:
                src.backup(dst, pages=4000, progress=prog)
            finally:
                src.close()
            # 2. take the personal tables out
            self._set('Removing personal settings and lists', 41)
            for t in DROP_TABLES:
                dst.execute('DROP TABLE IF EXISTS "%s"' % t)
            dst.execute('DELETE FROM settings WHERE k NOT IN (%s)' % ','.join('?' * len(KEEP_SETTINGS)), KEEP_SETTINGS)
            dst.commit()
            dst.execute('PRAGMA journal_mode=DELETE')
            weeks = dst.execute('SELECT COALESCE(MAX(week),0), COALESCE(SUM(entries),0) FROM weeks').fetchone()
            dst.close()
            if not weeks[1]:
                raise ValueError('The copy has no race data in it, so nothing was shared.')
            for f in (tmpdb + '-wal', tmpdb + '-shm'):
                _rm(f)
            # 3. zip it
            total = os.path.getsize(tmpdb)
            now = datetime.datetime.now()
            with zipfile.ZipFile(tmpzip, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                done = 0
                with open(tmpdb, 'rb') as f, z.open('sdm.db', 'w', force_zip64=True) as o:
                    while True:
                        b = f.read(CHUNK)
                        if not b:
                            break
                        o.write(b)
                        done += len(b)
                        self._set('Zipping', 42 + 43 * done / total)
            _rm(tmpdb)
            with zipfile.ZipFile(tmpzip) as z:
                if z.namelist() != ['sdm.db']:
                    raise ValueError('The zip file did not come out right.')
            size = os.path.getsize(tmpzip)
            # 4. copy over the old one in place (same file, so the share link stays valid)
            dest_zip = os.path.join(self.dest, NAME)
            done = 0
            with open(tmpzip, 'rb') as f, open(dest_zip, 'wb') as o:
                while True:
                    b = f.read(CHUNK)
                    if not b:
                        break
                    o.write(b)
                    done += len(b)
                    self._set('Copying to the folder', 86 + 13 * done / size)
                o.flush()
                os.fsync(o.fileno())
            if os.path.getsize(dest_zip) != size:
                raise OSError('The copy in the folder is the wrong size. Try again.')
            with open(os.path.join(self.dest, README), 'w', encoding='utf-8') as o:
                o.write(README_TEXT % (now.strftime('%Y-%m-%d %H:%M'), weeks[0], format(weeks[1], ','), NAME))
            _rm(tmpzip)
            st['result'] = dict(mb=round(size / 1048576, 1), folder=self.dest, at=now.isoformat(timespec='seconds'),
                                runners=weeks[1], last_week=weeks[0])
            self._set('Done', 100)
        except Exception as e:
            st['error'] = '%s' % (str(e)[:300] or type(e).__name__)
            st['phase'] = 'Stopped'
            for f in (tmpdb, tmpdb + '-wal', tmpdb + '-shm', tmpdb + '-journal', tmpzip):
                _rm(f)
        finally:
            st['running'] = False
            st['finished'] = True


def check_dest(path):
    """The folder must already exist and be writable. Returns an error message or None; raises nothing."""
    if not path or not isinstance(path, str):
        raise ValueError('Pick a folder first.')
    if not os.path.isabs(path):
        raise ValueError('Give the full folder path, for example G:\\My Drive\\SDM.')
    if not os.path.isdir(path):
        raise ValueError('That folder does not exist. Make it in Google Drive first, then try again.')
    if not os.access(path, os.W_OK):
        raise ValueError('The app is not allowed to write in that folder.')
    return None


def _free(path):
    import shutil
    return shutil.disk_usage(path).free


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass
