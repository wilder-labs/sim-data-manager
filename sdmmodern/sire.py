"""Saved sires, read from the local database only. Nothing here contacts the sim site."""


def _ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS sires(hid INTEGER PRIMARY KEY, sire TEXT, checked INTEGER)')


def cached(store, hid):
    _ensure(store.db)
    r = store.db.execute('SELECT sire, checked FROM sires WHERE hid=?', (hid,)).fetchone()
    if not r or not r['sire']:
        return None
    return {'sire': r['sire'], 'cached': True}
