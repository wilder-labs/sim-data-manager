"""Horse id mapping between the sim site's numbers and the database's numbers. No network access."""

STORE = None


def use_store(store):
    """Lets site_to_hid look horses up in the database."""
    global STORE
    STORE = store


def _known(hid):
    try:
        return STORE is not None and STORE.db.execute('SELECT 1 FROM horses WHERE hid = ?', (hid,)).fetchone() is not None
    except Exception:
        return False


def site_to_hid(s):
    """The site writes a horse id as 1 + year + 8 digits. The database keeps 10-digit ids as 'year + 8 digits' and
    9-digit ids with one 0 dropped, so a 0 in the fourth place can mean either; the database decides."""
    s = str(s).strip()
    if not (len(s) >= 6 and s.isdigit()):
        return None
    short = int(s[1:3] + s[4:])
    if len(s) == 11:
        if s[3] != '0':
            return int(s[1:])
        full = int(s[1:])
        if _known(full):
            return full
        if _known(short):
            return short
    return short
