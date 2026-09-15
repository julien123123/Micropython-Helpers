import struct,sys
cache_f = 'memcache.txt'

class RegCache:
    """
    manages saving and loading of memory cache to a JSON file
    """
    def __init__(self, fnm):
        self.fnm = fnm
        self.cache = None

    def _ld(self):
        if self.cache is not None:
            return
        try:
            with open(self.fnm, 'rb') as f:
                s = f.read().decode()
                self.cache = eval(s)
        except OSError:
            self.cache = {}

    def get(self, nm, h):
        self._ld()
        if nm in self.cache and (h is False or self.cache[nm]['*HSH'] == h):
            r = self.cache[nm].copy()
            r.pop('*HSH', None)
            return r
        return False

    def push(self, name, value, hsh):
        self._ld()
        self.cache[name] = value.copy()
        self.cache[name]['*HSH'] = hsh
        with open(self.fnm, 'wb') as f:
            f.write(repr(self.cache).encode())

CACHE = RegCache(cache_f)

def clear_cache():
    CACHE.cache = None

def delete_cache():
    import os
    os.remove(cache_f)

class Mem:
    """
    Base class for memory-mapped registers that manages a memory buffer.
    This avoids breaking micropython when modifying memory directly.
    """
    def __init__(self, name, mem, offset, span, auto_ld = True, lock = False, chksm = False):
        self.name = name
        self.mem = mem
        self.memstart = offset
        self.span = span
        self.buf = bytearray(span)
        self._lk = lock
        self.chksm = chksm
        self.ld_buf() if auto_ld else None

    @property
    def sm(self):
        if self.chksm:
            return self.buf[-2:]
        else:
            return False

    def post_all(self):
        # Dans le fond, c'est juste bon pour struct
        # Pour pack, chaque item écrit dans mem directement
        if self.chksm:
            h = self.getcsm()
            self.buf[-2] = h >> 8
            self.buf[-1] = h & 0xFF
        self.mem[self.memstart:self.memstart + self.span] = self.buf
        c = self.ld_buf()
        return True if self.chksm and self.check(c) or not self.chksm else False

    def ld_buf(self):
        self.buf[:] = self.mem[self.memstart:self.memstart + self.span]
        if self.chksm:
            return self.sm
        return False

    def check(self, s):
        c = self.getcsm()
        if  isinstance(s, int) and c == s:
            return True
        if isinstance(s, bytes) and s == c.to_bytes(2, 'big'):
            return True
        if isinstance(s, bytearray) and bytes(s) == c.to_bytes(2, 'big'):
            return True
        return False

    def getcsm(self):
        return self.crc16(self.buf, len(self.buf))if not self.chksm else self.crc16(self.buf, len(self.buf)-2)

    @staticmethod
    @micropython.viper
    def crc16(data: ptr8, length: int) -> int:
        crc = 0xFFFF
        for i in range(length):
            crc ^= data[i]
            for j in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc
try:
    import uctypes
    class Struct(Mem):
        """
        This class dynamically creates and manages a memory-mapped structure using uctypes.struct.
        Structs can be fickle. Be sure that the memory area you give it is big enough otherwise it will crash micropython.
        """
        def __init__(self, name, mem, offset, *args, span=32, lock = False, chksm = False):
            super().__init__(name, mem, offset, span, True, lock, chksm)
            self.layout = {}
            if not lock:
                self._hsh = hash(args + tuple([offset, span]))
                sav = CACHE.get(self.name, self._hsh)
                if not sav:
                    self._parse_args(args)
                    CACHE.push(self.name, self.layout, self._hsh)
                else:
                    self.layout = sav
            else:
                self.layout = CACHE.get(self.name, False)
            self.struct = uctypes.struct(uctypes.addressof(self.buf), self.layout, uctypes.LITTLE_ENDIAN)

        def __getitem__(self, it):
            return getattr(self.struct, it)

        def __setitem__(self, key, value):
            if isinstance(value, (str, bytes, bytearray)):
                value = value.encode('utf-8') if type(value) is str else value
                for i, b in enumerate(value):
                    self[key][i] = b
            else:
                setattr(self.struct, key, value)

        def __str__(self):
            return '\n'.join(str(v)+": "+str(self[v]) for v in self.layout.keys())

        #@micropython.native
        def _parse_args(self, ar):
            bit_pos, byte_pos = 0, 0
            binl=[]
            l =[]
            for args in ar:
                args = self._ngst(*args)
                if args[2]:
                    binl.append(args)
                else:
                    l.append(args)
            for dt in binl:
                name, ln, *rest = dt
                self.layout.update({name: byte_pos | bit_pos << uctypes.BF_POS | ln << uctypes.BF_LEN | uctypes.BFUINT8})
                bit_pos += ln
                if bit_pos >= 8:
                    byte_pos += bit_pos // 8
                    bit_pos = bit_pos % 8
            if bit_pos > 0:
                byte_pos += 1
                bit_pos = 0
            for dt in l:
                # name, length, bin, format
                if dt[3] == uctypes.ARRAY:
                    self.layout.update({dt[0]: (byte_pos | dt[3], dt[1] | uctypes.UINT8)})
                    byte_pos += dt[1]
                else:
                    self.layout.update({dt[0]: (byte_pos | dt[3])})
                    byte_pos = byte_pos + (2 ** ((dt[3] >> 28) & 0xF)) * dt[1] if dt[3] not in (uctypes.FLOAT32, uctypes.FLOAT64) else 4 if dt[3] is uctypes.FLOAT32 else 8
            if byte_pos > self.span and not self.chksm or byte_pos > (self.span - 2 ) and self.chksm:
                raise ValueError(f"Struct {self.name} is too big for the memory area ({byte_pos if not self.chksm else byte_pos + 2} bytes used, {self.span} bytes available)")

        @staticmethod
        def _ngst(name, span, *rst):
            lr = len(rst)
            bn, fmt = False, False
            if lr == 2:
                bn, fmt = rst
            if lr == 1:
                if type(*rst) == bool:
                    bn = rst[0]
                else:
                    fmt = rst[0]
            fmt = uctypes.UINT8 if not fmt else getattr(uctypes, fmt)
            return name, span, bn, fmt

        def toggle(self, key):
            self[key] = self[key] ^ 1
except ImportError:
    pass

        
class Pack(Mem):
    """
    Pythonic class that manages memory-mapped registers with individual items.
    Can be a better choice when uctypes.struct is too rigid.
    """
    def __init__(self, name, mem, offset, *args, span = 32, lock = False):
        super().__init__(name, mem, offset, span, lock)
        self.items = {}
        if not lock:
            self._hsh = hash(args + tuple([offset, span]))
            sav = CACHE.get(self.name, self._hsh)
            if not sav:
                self.items = {ar[0]: Memitem(i, *ar) for i, ar in enumerate(args)}
                self._order_items()
                CACHE.push(self.name, {
                    k: {attr: (int.from_bytes(val, 'big') if attr == "inreg" else val) for attr, val in
                        v.__dict__.items() if
                        attr not in ("memref", "buf")} for k, v in self.items.items()}, self._hsh)
            else:
                for k, d in sav.items():
                    self.items[k] = Memitem.from_dict(d, self)
        else:
            sav = CACHE.get(self.name, False)
            for k, d in sav.items():
                self.items[k] = Memitem.from_dict(d, self)

    def __str__(self): return '\n'.join(str(v) for v in self.items.values())
    def __getitem__(self, k): return self.items[k]
    def __setitem__(self, k, v): self.items[k].ch_val(v)

    #@micropython.native
    def _order_items(self):
        """
            This function ensures that all items stay in the same order every time
            It also makes it very easy to combine bits with bytes
        """
        wdcsr, bt_csr = 0, 0
        # binary items
        for itm in sorted((i for i in self.items.values() if i.inreg[0] & (1 <<7)), key=lambda obj: obj.indx):
            if bt_csr >= 8:
                wdcsr += 1
                bt_csr %= 8
            itm.inreg[3] = wdcsr
            itm.inreg[0] |= bt_csr & ((1 << 6)-1)
            itm.mask = 1 << bt_csr
            bt_csr += itm.inreg[2]
            itm.memref = memoryview(self.buf)[itm.inreg[3]:itm.inreg[3] + 1]
        if bt_csr > 0:
            wdcsr += 1
        # Non-binary items
        for itm in sorted((i for i in self.items.values() if not i.inreg[0] & (1<<7)), key=lambda obj: obj.indx):
            itm.inreg[3] = wdcsr
            wdcsr += (2 ** ((itm.inreg[0] >> 5) & 0b11)) * itm.inreg[2]
            itm.memref = memoryview(self.buf)[itm.inreg[3]:itm.inreg[3] + itm.inreg[2]*(1 <<((itm.inreg[0] >>5)&0b11))]
        if wdcsr > self.span and not self.chksm or wdcsr > (self.span-2) and self.chksm:
            raise ValueError(f"Pack {self.name} is too big for the memory area ({wdcsr if not self.chksm else wdcsr + 2} bytes used, {self.span} bytes available)")

class Memitem:
    # BITPOS  = 5 BITS : inreg[0], bit 0-4
    # BIN = 1 BIT : inreg[0], bit 7
    # PACKFMT =  8 BITS: inreg[1]
    # Packmultiplier = 2 Bits : inreg[0], bit 5, 6
    # LENGTH = 8 BITS : inreg[2]
    # BYTEPOS = 8 BITS : inreg[3]

    __slots__ = ('indx','name','length','pack_format','memref','mask', 'inreg', 'bin')
    @classmethod
    def from_dict(cls,d, reg):
        obj = cls(d['indx'], d['name'], 0, inreg=d['inreg'])
        obj.length = obj.inreg[2]
        obj.memref = memoryview(reg.buf)[obj.inreg[3]:obj.inreg[3] + (1 if (obj.inreg[0] & (1<<7)) else obj.inreg[2]*2**((obj.inreg[0]>>5)&0b11))]
        obj.mask = 1 << (obj.inreg[0] & 0b11111)
        return obj

    def __init__(self,indx, name, length, bin = False, pack_format=False, inreg= None):
        self.indx = indx
        self.name = name
        if not inreg:
            self.inreg = bytearray(4)
            if bin:
                self.inreg[0] |= 1 << 7
            if pack_format:
                self.inreg[1] = ord(pack_format)
            else:
                self.inreg[1] = ord('B')
            try:
                self.inreg[0] |= ((len(struct.pack(chr(self.inreg[1]), 0)) >> 1) & 3) << 5
            except TypeError:
                pass

            self.inreg[2] = length
        else:
            self.inreg = inreg.to_bytes(4, 'big')
        self.memref = None
        self.mask = 0

    @property
    def value(self):
        return (lambda v: v[0] if isinstance(v, tuple) and len(v) == 1 else v)(struct.unpack(chr(self.inreg[1]), bytes(self.memref))) if self.inreg[1] != ord('B') else (bytes(self.memref) if not self.inreg[0] & (1 << 7) else (bytes(self.memref)[0] >> (self.inreg[0] & 0b11111)) & 1)

    @value.setter
    def value(self, new_dt):
        self.ch_val(new_dt)

    @property
    def raw_val(self):
        return bytes(self.memref) if not self.inreg[0] & (1<<7) else (bytes(self.memref)[0] >> (self.inreg[0] & 0b11111)) & 1

    def __iadd__(self, other):
        if self.inreg[2] is 1:
            return self.value[0] + other
        else:
            raise TypeError("Cannot use += on non scalar values")

    def __str__(self):
        return f'{self.name}: \n\t index = {self.indx}\n\t val ={self.value}\n\t byte_pos = {self.inreg[3]}\n\t bin = {bool(self.inreg[0] >> 7)}\n\t bit_pos = {self.inreg[0]&0b11111}'

    #@micropython.native
    def ch_val(self, new_val):
        if self.inreg[0] & (1<<7): # Binary
            self.memref[:] = bytes([self.memref[0] | self.mask]) if new_val else bytes([self.memref[0] &~ self.mask])
        elif isinstance(new_val, (bytes, bytearray, str)):
            new_val = new_val.encode() if isinstance(new_val, str) else new_val
            if len(self.memref) > len(new_val):
                new_val = new_val + b'\x00' * (len(self.memref) - len(new_val))  # Adjust the length of v.buf
            self.memref[:] = new_val
        else:
            self.memref[:] = struct.pack(chr(self.inreg[1])*self.inreg[2], new_val)

    def toggle(self):
        if not (self.inreg[0] >> 7) & 1:
            raise AttributeError('item is not defined as binary, cannot toggle!')
        if self.inreg[2] >1:
            raise ValueError("item's length is superior to 1, cannot toggle")
        self.memref[:] = bytes([self.memref[0] ^ self.mask])
try:
    class OrderedStruct(Struct):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

        def _parse_args(self, ar):
            bit_pos, byte_pos = 0, 0
            for args in ar:
                args = self._ngst(*args) # name, span, bn, fmt
                if args[2]: # if binary
                    self.layout.update({args[0]: byte_pos | bit_pos << uctypes.BF_POS | args[1] << uctypes.BF_LEN | uctypes.BFUINT8})
                    bit_pos += args[1]
                    if bit_pos >= 8:
                        byte_pos += bit_pos // 8
                        bit_pos %= 8
                else:
                    if bit_pos > 0:
                        byte_pos += 1
                    if args[3] == uctypes.ARRAY:
                        self.layout.update({args[0]: (byte_pos | args[3], args[1] | uctypes.UINT8)})
                        byte_pos+= args[1]
                    else:
                        self.layout.update({args[0]: (byte_pos | args[3])})
                        byte_pos = byte_pos + (2 ** ((args[3] >> 28) & 0xF)) * args[1] if args[3] not in (uctypes.FLOAT32, uctypes.FLOAT64) else 4 if args[3] is uctypes.FLOAT32 else 8
                if byte_pos > self.span and not self.chksm or byte_pos > (self.span - 2) and self.chksm:
                    raise ValueError(f"Struct {self.name} is too big for the memory area ({byte_pos if not self.chksm else byte_pos + 2} bytes used, {self.span} bytes available)")


    class IndexBinStruct(Struct):
        def __init__(self, name, mem, offset, *args, span=32, lock=False):
            import machine
            self._hsh = hash(args + tuple([offset, span])) if not lock else False

            if span in (8, 16, 32):
                self.fmt = f"BFUINT{span}"
            else:
                raise ValueError("span must be 8, 16 or 32")
            span = span >> 3  # for the buffer
            Mem.__init__(self, name, mem, offset, span, False)
            self.layout = {}
            sav = CACHE.get(self.name, self._hsh)

            if not sav:
                for ar in args:
                    # name, position, span
                    self.layout.update(
                        {ar[0]: 0 | (ar[1] << uctypes.BF_POS) | (ar[2] << uctypes.BF_LEN) | getattr(uctypes, self.fmt)})
                CACHE.push(self.name, self.layout, self._hsh)
            else:
                self.layout = sav

            self.buf_adr = uctypes.addressof(self.buf)
            self.struct = uctypes.struct(self.buf_adr, self.layout, uctypes.LITTLE_ENDIAN)
            self.ld_buf()
        '''

        def post_all(self):
            self.mmtd[self.mem + self.memstart] = self.mmtd[self.buf_adr]
            self.ld_buf()

        def ld_buf(self):
            self.buf[:] = self.mmtd[self.mem + self.memstart].to_bytes(self.span, 'little')
        '''

except NameError:
    pass

class OrderedPack(Pack):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _order_items(self):
        wdcsr, bt_csr = 0, 0
        for itm in sorted((i for i in self.items.values()), key=lambda obj: obj.indx):
            # binary items (name, position, span)
            if itm.inreg[0] & (1 << 7):
                if bt_csr >= 8:
                    wdcsr += 1
                    bt_csr %= 8
                itm.inreg[3] = wdcsr
                itm.inreg[0] |= bt_csr & ((1 << 6) - 1)
                itm.mask = 1 << bt_csr
                bt_csr += itm.inreg[2]
                itm.memref = memoryview(self.buf)[itm.inreg[3]:itm.inreg[3] + 1]
            else:
                # Non-binary items
                itm.inreg[3] = wdcsr + 1 if bt_csr > 0 else wdcsr # if the bits are not aligned
                wdcsr += (2 ** ((itm.inreg[0] >> 5) & 0b11)) * itm.inreg[2]
                itm.memref = memoryview(self.buf)[itm.inreg[3]:itm.inreg[3] + itm.inreg[2] * (1 << ((itm.inreg[0] >> 5) & 0b11))]
        if wdcsr > self.span and not self.chksm or wdcsr > (self.span-2) and self.chksm:
            raise ValueError(f"Pack {self.name} is too big for the memory area ({wdcsr if not self.chksm else wdcsr + 2} bytes used, {self.span} bytes available)")

if __name__ == "__main__":
    import os
    
    b = bytearray(os.urandom(32))
    nvm = memoryview(b)
    nvam = Struct('nvam', nvm, 0, ('REPL', 1, True), ('DATA', 1, True), ('MNT', 1, True), ('SAC', 3, 'ARRAY'), ('PLUS', 4), span =10, chksm=True)
    mimi = Pack('mimi', nvm, 8, ('passw', 10), ('flag', 1, True), ('Nan', 4), span=16)
    nvam['SAC'] = 'IOU'
    nvam.post_all()

    ca = bytearray(4)
    cca = uctypes.addressof(ca)
    nim = IndexBinStruct('nim', cca, 0, ('allo', 0, 1), ('cl', 1, 1), ('pe', 4, 1), span = 32)