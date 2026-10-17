"""Minimal read-only reader for Access 97 (Jet 3) .mdb files. Pure Python, no dependencies."""
import struct, datetime

PG = 2048
T = {1: 'bool', 2: 'byte', 3: 'int16', 4: 'int32', 5: 'money', 6: 'float', 7: 'double',
     8: 'datetime', 9: 'text', 10: 'text', 11: 'ole', 12: 'memo'}
# Jet3: type 10 = text, 9 = binary, 11 = OLE, 12 = memo
T = {1: 'bool', 2: 'byte', 3: 'int16', 4: 'int32', 5: 'money', 6: 'float', 7: 'double',
     8: 'datetime', 9: 'binary', 10: 'text', 11: 'ole', 12: 'memo', 15: 'guid'}
FIXED_SIZE = {1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 6: 4, 7: 8, 8: 8, 15: 16}


class Col:
    pass


class MDB:
    def __init__(self, path):
        with open(path, 'rb') as f:
            self.d = f.read()
        if self.d[0x14] != 0:
            raise ValueError('Only Access 97 (Jet 3) files are supported by this reader')
        self.npages = len(self.d) // PG
        self._by_tdef = None
        self._tables = None

    def page(self, n):
        return self.d[n * PG:(n + 1) * PG]

    # ---- table definitions
    def _tdef(self, pg):
        buf = bytearray(self.page(pg))
        nxt = struct.unpack_from('<I', buf, 4)[0]
        while nxt:
            p = self.page(nxt)
            buf += p[8:]
            nxt = struct.unpack_from('<I', p, 4)[0]
        buf = bytes(buf)
        nrows = struct.unpack_from('<I', buf, 12)[0]
        ncols = struct.unpack_from('<H', buf, 25)[0]
        nidx = struct.unpack_from('<I', buf, 27)[0]
        nridx = struct.unpack_from('<I', buf, 31)[0]
        pos = 43 + nridx * 8
        cols = []
        for i in range(ncols):
            e = buf[pos:pos + 18]
            c = Col()
            c.type = e[0]
            c.num = struct.unpack_from('<H', e, 1)[0]
            c.var_idx = struct.unpack_from('<H', e, 3)[0]
            c.flags = e[13]
            c.fixed = bool(c.flags & 1)
            c.fixed_off = struct.unpack_from('<H', e, 14)[0]
            c.size = struct.unpack_from('<H', e, 16)[0]
            cols.append(c)
            pos += 18
        for c in cols:
            ln = buf[pos]
            c.name = buf[pos + 1:pos + 1 + ln].decode('cp1252', 'replace')
            pos += 1 + ln
        return cols, nrows

    # ---- rows
    def _rows_of_page(self, pg_no):
        p = self.page(pg_no)
        n = struct.unpack_from('<H', p, 8)[0]
        prev = PG
        for i in range(n):
            raw = struct.unpack_from('<H', p, 10 + 2 * i)[0]
            start = raw & 0x1FFF
            end = prev  # exclusive
            prev = start
            if raw & 0x8000:
                continue
            if raw & 0x4000:
                # overflow pointer
                ptr = p[start:start + 4]
                row = ptr[0]
                opg = ptr[1] | (ptr[2] << 8) | (ptr[3] << 16)
                op = self.page(opg)
                oraw = struct.unpack_from('<H', op, 10 + 2 * row)[0]
                ostart = oraw & 0x1FFF
                oend = PG if row == 0 else struct.unpack_from('<H', op, 10 + 2 * (row - 1))[0] & 0x1FFF
                yield op[ostart:oend]
                continue
            yield p[start:end]

    def _decode(self, cols, r):
        L = len(r)
        ncols = r[0]
        bm = (ncols + 7) // 8
        nullmask = r[L - bm:]
        nvar = r[L - bm - 1]
        jumps = (L - 1) // 256
        ptr = L - bm - jumps - 2   # index of first var offset
        offs = []
        used = 0
        for i in range(nvar + 1):
            while used < jumps and i == r[L - bm - 2 - used]:
                used += 1
            offs.append(r[ptr - i] + 256 * used)
        rec = {}
        for c in cols:
            nonnull = (nullmask[c.num // 8] >> (c.num % 8)) & 1 if c.num // 8 < len(nullmask) else 0
            if c.type == 1:
                rec[c.name] = bool(nonnull)
                continue
            if not nonnull:
                rec[c.name] = None
                continue
            if c.fixed:
                sz = FIXED_SIZE.get(c.type, c.size)
                b = r[1 + c.fixed_off:1 + c.fixed_off + sz]
            else:
                if c.var_idx >= nvar:
                    rec[c.name] = None
                    continue
                b = r[offs[c.var_idx]:offs[c.var_idx + 1]]
            rec[c.name] = self._val(c.type, b)
        return rec

    def _val(self, t, b):
        if t == 2: return b[0]
        if t == 3: return struct.unpack('<h', b)[0]
        if t == 4: return struct.unpack('<i', b)[0]
        if t == 5: return struct.unpack('<q', b)[0] / 10000.0
        if t == 6: return struct.unpack('<f', b)[0]
        if t == 7: return struct.unpack('<d', b)[0]
        if t == 8:
            v = struct.unpack('<d', b)[0]
            try:
                return datetime.datetime(1899, 12, 30) + datetime.timedelta(days=v)
            except Exception:
                return None
        if t == 10: return b.decode('cp1252', 'replace')
        if t == 12: return self._memo(b)
        return bytes(b)

    def _memo(self, b):
        if len(b) < 12: return ''
        ln = struct.unpack_from('<I', b, 0)[0]
        flag = ln >> 24
        ln &= 0xFFFFFF
        if flag & 0x80:
            return b[12:12 + ln].decode('cp1252', 'replace')
        ptr = struct.unpack_from('<I', b, 4)[0]
        row, pg = ptr & 0xFF, ptr >> 8
        p = self.page(pg)
        if flag & 0x40:
            start = struct.unpack_from('<H', p, 10 + 2 * row)[0] & 0x1FFF
            end = PG if row == 0 else struct.unpack_from('<H', p, 10 + 2 * (row - 1))[0] & 0x1FFF
            return p[start:end].decode('cp1252', 'replace')
        out = b''
        while True:
            start = struct.unpack_from('<H', p, 10 + 2 * row)[0] & 0x1FFF
            end = PG if row == 0 else struct.unpack_from('<H', p, 10 + 2 * (row - 1))[0] & 0x1FFF
            seg = p[start:end]
            nxt = struct.unpack_from('<I', seg, 0)[0]
            out += seg[4:]
            if not nxt: break
            row, pg = nxt & 0xFF, nxt >> 8
            p = self.page(pg)
        return out.decode('cp1252', 'replace')

    def _index_pages(self):
        if self._by_tdef is None:
            m = {}
            for n in range(self.npages):
                p = self.d[n * PG:n * PG + 8]
                if p[0] == 1 and p[1] == 1:
                    m.setdefault(struct.unpack_from('<I', p, 4)[0], []).append(n)
            self._by_tdef = m
        return self._by_tdef

    def rows_at(self, tdef_pg):
        cols, _ = self._tdef(tdef_pg)
        for pg in self._index_pages().get(tdef_pg, []):
            for r in self._rows_of_page(pg):
                yield self._decode(cols, r)

    def tables(self):
        """dict name -> tdef page, user tables only"""
        if self._tables is None:
            t = {}
            for r in self.rows_at(2):
                if r.get('Type') == 1 and not str(r['Name']).startswith('MSys') and not str(r['Name']).startswith('~'):
                    t[r['Name']] = r['Id'] & 0xFFFFFF
            self._tables = t
        return self._tables

    def columns(self, name):
        return [(c.name, T.get(c.type, c.type)) for c in sorted(self._tdef(self.tables()[name])[0], key=lambda c: c.num)]

    def rows(self, name):
        return self.rows_at(self.tables()[name])
