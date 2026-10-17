"""Tiny pure-Python 7z reader (stdlib only). Supports the common case: LZMA / LZMA2 / copy folders."""
import lzma, struct, zlib, io

K_END, K_HEADER, K_ARCHIVE_PROPS, K_ADD_STREAMS, K_MAIN_STREAMS, K_FILES_INFO = 0, 1, 2, 3, 4, 5
K_PACK_INFO, K_UNPACK_INFO, K_SUBSTREAMS, K_SIZE, K_CRC, K_FOLDER, K_CODERS_UNPACK = 6, 7, 8, 9, 0xA, 0xB, 0xC
K_NUM_UNPACK, K_EMPTY_STREAM, K_EMPTY_FILE, K_NAMES, K_ENCODED_HEADER = 0xD, 0xE, 0xF, 0x11, 0x17


class R:
    def __init__(self, b): self.b, self.p = b, 0
    def byte(self):
        v = self.b[self.p]; self.p += 1; return v
    def take(self, n):
        v = self.b[self.p:self.p + n]; self.p += n; return v
    def num(self):
        first = self.byte(); mask = 0x80; val = 0
        for i in range(8):
            if not first & mask:
                return val | ((first & (mask - 1)) << (8 * i))
            val |= self.byte() << (8 * i); mask >>= 1
        return val
    def bits(self, n):
        out, cur, m = [], 0, 0
        for i in range(n):
            if m == 0: cur, m = self.byte(), 0x80
            out.append(bool(cur & m)); m >>= 1
        return out
    def opt_bits(self, n):
        return [True] * n if self.byte() else self.bits(n)


def _decode(coder, data, unpack_size):
    cid, props = coder
    if cid == b'\x00':
        return data[:unpack_size]
    if cid == b'\x21':
        d = props[0] & 0x3F
        dict_size = 0xFFFFFFFF if d == 40 else (2 | (d & 1)) << (d // 2 + 11)
        f = [{'id': lzma.FILTER_LZMA2, 'dict_size': dict_size}]
    elif cid == b'\x03\x01\x01':
        p = props[0]; lc, rem = p % 9, p // 9
        f = [{'id': lzma.FILTER_LZMA1, 'dict_size': struct.unpack('<I', props[1:5])[0], 'lc': lc, 'lp': rem % 5, 'pb': rem // 5}]
    else:
        raise ValueError('Unsupported 7z compression method %s' % cid.hex())
    return lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=f).decompress(data, max_length=unpack_size)


def _streams_info(r, base):
    pack_pos, pack_sizes, folders, unpack, subs = 0, [], [], [], None
    while True:
        t = r.byte()
        if t == K_END: break
        if t == K_PACK_INFO:
            pack_pos = r.num(); n = r.num()
            while True:
                t2 = r.byte()
                if t2 == K_END: break
                if t2 == K_SIZE: pack_sizes = [r.num() for _ in range(n)]
                elif t2 == K_CRC:
                    ok = r.opt_bits(n); [r.take(4) for x in ok if x]
        elif t == K_UNPACK_INFO:
            assert r.byte() == K_FOLDER
            n = r.num(); assert r.byte() == 0
            for _ in range(n):
                nc = r.num(); coders = []; tin = tout = 0
                for _ in range(nc):
                    fl = r.byte(); cid = bytes(r.take(fl & 0xF))
                    if fl & 0x10: ci, co = r.num(), r.num()
                    else: ci = co = 1
                    props = bytes(r.take(r.num())) if fl & 0x20 else b''
                    coders.append((cid, props)); tin += ci; tout += co
                for _ in range(tout - 1): r.num(); r.num()
                if tin - (tout - 1) > 1:
                    for _ in range(tin - (tout - 1)): r.num()
                folders.append(coders)
            assert r.byte() == K_CODERS_UNPACK
            unpack = [[r.num()] for _ in folders] if all(len(c) == 1 for c in folders) else None
            if unpack is None: raise ValueError('Unsupported 7z folder layout')
            t2 = r.byte()
            if t2 == K_CRC:
                ok = r.opt_bits(n); [r.take(4) for x in ok if x]; t2 = r.byte()
            assert t2 == K_END
        elif t == K_SUBSTREAMS:
            counts = [1] * len(folders); sizes = []
            t2 = r.byte()
            if t2 == K_NUM_UNPACK:
                counts = [r.num() for _ in folders]; t2 = r.byte()
            if t2 == K_SIZE:
                for fi, c in enumerate(counts):
                    tot = 0
                    for _ in range(c - 1):
                        s = r.num(); sizes.append(s); tot += s
                    if c: sizes.append(unpack[fi][0] - tot)
                t2 = r.byte()
            else:
                for fi, c in enumerate(counts):
                    if c == 1: sizes.append(unpack[fi][0])
            while t2 != K_END:
                if t2 == K_CRC:
                    nd = sum(c for c in counts if c != 1) + sum(1 for c in counts if c == 1)
                    ok = r.opt_bits(nd); [r.take(4) for x in ok if x]
                t2 = r.byte()
            subs = (counts, sizes)
    if subs is None:
        subs = ([1] * len(folders), [u[0] for u in unpack])
    return pack_pos, pack_sizes, folders, unpack, subs


def _unpack_folders(buf, base, pack_pos, pack_sizes, folders, unpack):
    out, off = [], base + pack_pos
    for i, coders in enumerate(folders):
        raw = buf[off:off + pack_sizes[i]]; off += pack_sizes[i]
        out.append(_decode(coders[0], bytes(raw), unpack[i][0]))
    return out


def extract(data):
    if data[:6] != b'7z\xbc\xaf\x27\x1c':
        raise ValueError('Not a 7z archive')
    off, size = struct.unpack('<QQ', data[12:28])
    hdr = data[32 + off:32 + off + size]
    r = R(hdr)
    t = r.byte()
    while t == K_ENCODED_HEADER:
        pp, ps, fo, up, _ = _streams_info(r, 32)
        hdr = _unpack_folders(data, 32, pp, ps, fo, up)[0]
        r = R(hdr); t = r.byte()
    assert t == K_HEADER
    folders_data, counts, sizes = [], [], []
    names, empty_stream, empty_file = [], None, None
    nfiles = 0
    while True:
        t = r.byte()
        if t == K_END: break
        if t == K_MAIN_STREAMS:
            pp, ps, fo, up, subs = _streams_info(r, 32)
            folders_data = _unpack_folders(data, 32, pp, ps, fo, up)
            counts, sizes = subs
        elif t == K_FILES_INFO:
            nfiles = r.num()
            while True:
                pt = r.byte()
                if pt == K_END: break
                sz = r.num(); body = R(r.take(sz))
                if pt == K_EMPTY_STREAM: empty_stream = body.bits(nfiles)
                elif pt == K_EMPTY_FILE:
                    ne = sum(empty_stream or []); empty_file = body.bits(ne)
                elif pt == K_NAMES:
                    body.byte(); raw = bytes(body.b[body.p:]).decode('utf-16-le').split('\x00')
                    names = raw[:nfiles]
        elif t == K_ARCHIVE_PROPS or t == K_ADD_STREAMS:
            raise ValueError('Unsupported 7z header')
    out, si, ei = {}, 0, 0
    stream_files = []
    for fi in range(nfiles):
        if empty_stream and empty_stream[fi]:
            out[names[fi]] = b''
        else:
            stream_files.append(names[fi])
    k = 0
    for fd, c in zip(folders_data, counts):
        pos = 0
        for _ in range(c):
            s = sizes[k]; out[stream_files[k]] = fd[pos:pos + s]; pos += s; k += 1
    return out
