"""Pure-Python Granny2 (.gr2) v6/v7 parser: Oodle1 decompression, type-tree reflection, skeletons, and B-spline animations."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import struct
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


# Magic signatures (32-bit LE/BE, 64-bit LE/BE)
_MAGIC_V6_LE = bytes.fromhex("b867b0caf86db10f84728c7e5e19001e")
_MAGIC_V7_LE = bytes.fromhex("29de6cc0baa4532b25f5b7a5f666e2ee")
_MAGIC_V6_BE = bytes.fromhex("b595110e4bb5a56a502828eb04b37825")
_MAGIC_V7_64LE = bytes.fromhex("e59b495e6f631f141e13eba990beedc4")

_COMPRESSION_NONE = 0
_COMPRESSION_OODLE0 = 1
_COMPRESSION_OODLE1 = 2

# Granny2 member type IDs
_MT_NONE = 0
_MT_INLINE = 1
_MT_REF = 2
_MT_REF_ARRAY = 3
_MT_ARRAY_OF_REFS = 4
_MT_VARIANT_REF = 5
_MT_REMOVED = 6
_MT_REF_VARIANT_ARRAY = 7
_MT_STRING = 8
_MT_TRANSFORM = 9
_MT_REAL32 = 10
_MT_INT8 = 11
_MT_UINT8 = 12
_MT_BINORMAL_INT8 = 13
_MT_NORMAL_UINT8 = 14
_MT_INT16 = 15
_MT_UINT16 = 16
_MT_BINORMAL_INT16 = 17
_MT_NORMAL_UINT16 = 18
_MT_INT32 = 19
_MT_UINT32 = 20
_MT_REAL16 = 21
_MT_EMPTY_REF = 22


@dataclass
class Gr2Transform:
    flags: int = 0
    position: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    orientation: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)  # xyzw
    scale_shear: Tuple[float, ...] = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


@dataclass
class Gr2Bone:
    index: int
    name: str
    parent_index: int
    local_transform: Gr2Transform
    inverse_world_transform: Tuple[float, ...] = ()  # 16 floats


@dataclass
class Gr2Skeleton:
    name: str
    bones: List[Gr2Bone] = field(default_factory=list)
    lod_type: int = 0


@dataclass
class Gr2Curve:
    codec: str
    fmt: int
    degree: int
    dimension: int
    knots: Tuple[float, ...] = ()
    controls: Tuple[Tuple[float, ...], ...] = ()
    constant_value: Tuple[float, ...] = ()


@dataclass
class Gr2TransformTrack:
    name: str
    flags: int = 0
    orientation: Optional[Gr2Curve] = None
    position: Optional[Gr2Curve] = None
    scale_shear: Optional[Gr2Curve] = None


@dataclass
class Gr2TrackGroup:
    name: str
    transform_tracks: List[Gr2TransformTrack] = field(default_factory=list)
    accumulation_flags: int = 0


@dataclass
class Gr2Animation:
    name: str
    duration: float
    time_step: float
    oversampling: float = 1.0
    default_loop_count: int = 0
    flags: int = 0
    track_groups: List[Gr2TrackGroup] = field(default_factory=list)


@dataclass
class Gr2File:
    version: int = 6
    skeletons: List[Gr2Skeleton] = field(default_factory=list)
    animations: List[Gr2Animation] = field(default_factory=list)
    track_groups: List[Gr2TrackGroup] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Oodle1 / Oodle0 Decompressor (pure Python)
# ---------------------------------------------------------------------------

_NORM_BITS = 14
_NORM_COUNT = 1 << _NORM_BITS
_ADJ_SUMS = _NORM_COUNT << 1
_MAX_LENS = 64
_LOW_BITS = 2
_MED_BITS = 8
_ADDRESS_MASK = 3
_LONG_LENGTHS = (128, 192, 256, 512)


class _EscapeRef:
    __slots__ = ("index",)

    def __init__(self, index: int) -> None:
        self.index = index


class _ArithBits:
    __slots__ = ("data", "ptr", "low", "range", "underflow", "carry", "length")

    def __init__(self, data: bytes) -> None:
        if not data:
            raise ValueError("Oodle1 stream is empty")
        first = data[0]
        self.data = data
        self.length = len(data)
        self.ptr = 1
        self.low = first >> 1
        self.range = 1 << 7
        self.underflow = 0
        self.carry = first & 1

    def renorm(self) -> None:
        rng = self.range
        if rng > 0x800000:
            return
        low = self.low
        carry = self.carry
        ptr = self.ptr
        data = self.data
        length = self.length
        while rng <= 0x800000:
            low = ((low + low + carry) << 7) & 0xFFFFFFFF
            b = data[ptr] if ptr < length else 0
            ptr += 1
            low |= b >> 1
            carry = b & 1
            rng = (rng << 8) & 0xFFFFFFFF
        self.low = low
        self.range = rng
        self.carry = carry
        self.ptr = ptr

    def get_bits(self, bits: int, scale: int) -> int:
        self.renorm()
        uf = self.range >> bits
        self.underflow = uf
        v = self.low // uf if uf else 0
        return scale - 1 if v >= scale else v

    def get_bits_value(self, bits: int, scale: int) -> int:
        self.renorm()
        div = self.range >> bits
        v = self.low // div if div else 0
        if v >= scale:
            v = scale - 1
        off = (div * v) & 0xFFFFFFFF
        self.low = (self.low - off) & 0xFFFFFFFF
        self.range = div if v + 1 < scale else (self.range - off) & 0xFFFFFFFF
        return v

    def get_value(self, scale: int) -> int:
        self.renorm()
        div = self.range // scale if scale else 0
        v = self.low // div if div else 0
        if v >= scale:
            v = scale - 1
        off = (div * v) & 0xFFFFFFFF
        self.low = (self.low - off) & 0xFFFFFFFF
        self.range = div if v + 1 < scale else (self.range - off) & 0xFFFFFFFF
        return v

    def remove(self, start: int, count: int, scale: int) -> None:
        off = (self.underflow * start) & 0xFFFFFFFF
        self.low = (self.low - off) & 0xFFFFFFFF
        if start + count < scale:
            self.range = (self.underflow * count) & 0xFFFFFFFF
        else:
            self.range = (self.range - off) & 0xFFFFFFFF


class _ArithModel:
    __slots__ = (
        "unique_count",
        "values",
        "single_counts",
        "summed_counts",
        "singles_tot",
        "update_tot",
        "update_range",
        "rescale_tot",
        "update_max",
        "singles_length",
        "summed_length",
    )

    def __init__(self, max_value: int, unique_count: int) -> None:
        self.unique_count = unique_count
        size = max(max_value, unique_count) + 16
        self.values = [0] * size
        self.single_counts = [0] * (size + 16)
        self.summed_counts = [_NORM_COUNT + _ADJ_SUMS] * (size + 32)
        self.singles_tot = 4
        self.update_tot = 8
        self.update_range = 4
        self.rescale_tot = min(max(max_value * 32, 256), 15160)
        umax = max_value * 2
        if umax < 128:
            umax = 128
        else:
            cap = (self.rescale_tot >> 1) - 32
            if umax > cap:
                umax = cap
        self.update_max = max(0, umax)
        self.singles_length = 0
        self.summed_length = 0
        self.single_counts[0] = 4
        self.summed_counts[0] = _ADJ_SUMS

    def _rescale(self) -> None:
        self.single_counts[0] >>= 1
        self.singles_tot = self.single_counts[0]
        max_count = 0
        max_pos = 0
        i = 1
        while i <= self.singles_length:
            while self.single_counts[i] <= 1:
                if i < self.singles_length:
                    self.single_counts[i] = self.single_counts[self.singles_length]
                    self.single_counts[self.singles_length] = 0
                    self.values[i] = self.values[self.singles_length]
                    self.singles_length -= 1
                else:
                    self.single_counts[i] = 0
                    self.singles_length -= 1
                    break
            if i > self.singles_length:
                break
            self.single_counts[i] >>= 1
            self.singles_tot += self.single_counts[i]
            if self.single_counts[i] > max_count:
                max_count = self.single_counts[i]
                max_pos = i
            i += 1
        if max_count and self.singles_length:
            last = self.singles_length
            if max_pos != last:
                self.single_counts[last], self.single_counts[max_pos] = (
                    self.single_counts[max_pos],
                    self.single_counts[last],
                )
                self.values[last], self.values[max_pos] = self.values[max_pos], self.values[last]
        if self.singles_length != self.unique_count and self.single_counts[0] == 0:
            self.single_counts[0] += 1
            self.singles_tot += 1

    def _update_counts(self) -> None:
        adj = (_NORM_COUNT * 8) // max(self.singles_tot, 1)
        total = ((self.single_counts[0] * adj) >> 3) + _ADJ_SUMS
        self.summed_counts[0] = _ADJ_SUMS
        i = 1
        while True:
            self.summed_counts[i] = total
            if i > self.singles_length:
                break
            total += (self.single_counts[i] * adj) >> 3
            i += 1
        nr = self.update_range << 1
        if nr > self.update_max:
            self.update_tot = self.singles_tot + self.update_max
        else:
            self.update_range = nr
            self.update_tot = self.singles_tot + nr
        self.summed_length = self.singles_length
        for pad in range(1, 8):
            self.summed_counts[self.summed_length + pad] = _NORM_COUNT + _ADJ_SUMS

    def decode(self, bits: _ArithBits) -> int | _EscapeRef:
        if self.singles_tot >= self.update_tot:
            if self.update_tot >= self.rescale_tot:
                self._rescale()
            self._update_counts()

        offset = bits.get_bits(_NORM_BITS, _NORM_COUNT) + _ADJ_SUMS
        lo = 0
        hi = self.summed_length + 1
        while lo < hi:
            mid = (lo + hi + 1) >> 1
            if self.summed_counts[mid] > offset:
                hi = mid - 1
            else:
                lo = mid
        idx = lo
        bits.remove(
            self.summed_counts[idx] - _ADJ_SUMS,
            self.summed_counts[idx + 1] - self.summed_counts[idx],
            _NORM_COUNT,
        )
        self.single_counts[idx] += 1
        self.singles_tot += 1

        if idx <= 0:
            if self.singles_length == self.summed_length or bits.get_bits_value(1, 2) == 0:
                if self.singles_length < len(self.single_counts) - 1:
                    self.singles_length += 1
                    ni = self.singles_length
                    self.single_counts[ni] += 2
                    self.singles_tot += 2
                    if self.singles_length == self.unique_count:
                        self.singles_tot -= self.single_counts[0]
                        self.single_counts[0] = 0
                    return _EscapeRef(ni)
                return _EscapeRef(0)
            idx = bits.get_value(self.singles_length - self.summed_length) + self.summed_length + 1
            if idx < len(self.single_counts):
                self.single_counts[idx] += 2
                self.singles_tot += 2
            return self.values[idx] if idx < len(self.values) else 0
        return self.values[idx]

    def set_symbol(self, ref: _EscapeRef, value: int) -> None:
        if ref.index >= len(self.values):
            self.values.extend([0] * (ref.index + 1 - len(self.values)))
        self.values[ref.index] = value


def _decode_sym(model: _ArithModel, bits: _ArithBits, escape_scale: int) -> int:
    v = model.decode(bits)
    if isinstance(v, _EscapeRef):
        esc = bits.get_value(escape_scale)
        model.set_symbol(v, esc)
        return esc
    return v


def _decompress_oodle1(compressed: bytes, expanded_size: int, first_16bit: int, first_8bit: int) -> bytes:
    if expanded_size == 0:
        return b""
    if len(compressed) < 36:
        raise ValueError("Oodle1 compressed stream too short")
    bits = _ArithBits(compressed[36:])
    out = bytearray(expanded_size)
    pos = 0
    for block_idx, stop in enumerate((first_16bit, first_8bit, expanded_size)):
        stop = min(stop, expanded_size)
        if pos >= stop:
            continue
        p0, p1, p2 = struct.unpack_from("<3I", compressed, block_idx * 12)
        max_byte = p0 & 0x1FF
        max_offset_limit = p0 >> 9
        unique_byte = p1 & 0x1FF
        low_count = min(max_offset_limit + 1, 1 << _LOW_BITS)
        mid_count = min((max_offset_limit >> _LOW_BITS) + 1, 1 << _MED_BITS)
        high_count = (max_offset_limit >> (_LOW_BITS + _MED_BITS)) + 1

        byte_models = [_ArithModel(max_byte - 1, unique_byte) for _ in range(_ADDRESS_MASK + 1)]
        length_models = []
        last_u = 0
        for g in range(4):
            last_u = (p2 >> ((3 - g) * 8)) & 0xFF
            for _ in range(_MAX_LENS // 4):
                length_models.append(_ArithModel(_MAX_LENS, last_u))
        for _ in range((_MAX_LENS // 4) * 4, _MAX_LENS + 1):
            length_models.append(_ArithModel(_MAX_LENS, last_u))
        offset_low = _ArithModel(low_count - 1, low_count)
        offset_high = _ArithModel(high_count - 1, ((p1 >> 9) >> (_LOW_BITS + _MED_BITS)) + 1)
        offset_mid = [_ArithModel(mid_count - 1, mid_count) for _ in range(high_count)]

        last_len = 0
        decoded_in_block = 0
        while pos < stop:
            lsym = _decode_sym(length_models[min(last_len, _MAX_LENS)], bits, _MAX_LENS + 1)
            last_len = lsym
            if lsym:
                max_off = min(max_offset_limit, decoded_in_block)
                length = _LONG_LENGTHS[lsym - (_MAX_LENS - 3)] if lsym >= _MAX_LENS - 3 else lsym + 1
                low = _decode_sym(offset_low, bits, low_count)
                high = _decode_sym(offset_high, bits, (max_off >> (_LOW_BITS + _MED_BITS)) + 1)
                mid_scale = (1 << _MED_BITS) if max_off >= (1 << (_MED_BITS + _LOW_BITS)) else ((max_off >> _LOW_BITS) + 1)
                mid = _decode_sym(offset_mid[high], bits, mid_scale)
                dist = low + 1 + (mid << _LOW_BITS) + (high << (_LOW_BITS + _MED_BITS))
                for _ in range(length):
                    if pos < expanded_size and pos >= dist:
                        out[pos] = out[pos - dist]
                    pos += 1
                decoded_in_block += length
            else:
                out[pos] = _decode_sym(byte_models[pos & _ADDRESS_MASK], bits, max_byte) & 0xFF
                pos += 1
                decoded_in_block += 1
    return bytes(out)


# ---------------------------------------------------------------------------
# Type-tree reflection & curve decoders
# ---------------------------------------------------------------------------

@dataclass
class _TypeMember:
    mtype: int
    name: str
    ref_type: Optional[Tuple[int, int]]
    array_width: int
    stride: int


class _Gr2Reader:
    def __init__(self, data: bytes) -> None:
        if len(data) < 0x58:
            raise ValueError("GR2 file too small")
        magic = data[:16]
        if magic == _MAGIC_V6_LE:
            self.is_64 = False
            self.extra16 = False
        elif magic == _MAGIC_V7_LE:
            self.is_64 = False
            self.extra16 = True
        elif magic == _MAGIC_V7_64LE:
            self.is_64 = True
            self.extra16 = True
        elif magic == _MAGIC_V6_BE:
            raise ValueError("Big-endian GR2 files are not supported")
        else:
            raise ValueError("Not a Granny2 (.gr2) file")

        self.ptr_size = 8 if self.is_64 else 4
        _header_size, header_fmt = struct.unpack_from("<II", data, 16)
        if header_fmt != 0:
            raise ValueError(f"Unsupported GR2 header format {header_fmt}")

        (
            self.version,
            _file_size,
            _crc,
            sections_rel_offset,
            section_count,
            type_sec,
            type_off,
            root_sec,
            root_off,
            _tag,
        ) = struct.unpack_from("<10I", data, 32)
        self.type_ref = (type_sec, type_off)
        self.root_ref = (root_sec, root_off)

        sec_table_off = 32 + sections_rel_offset
        if sec_table_off + section_count * 44 > len(data):
            raise ValueError("GR2 section table out of bounds")

        self.sections: List[bytes] = []
        self.relocs: Dict[Tuple[int, int], Tuple[int, int]] = {}
        self._type_cache: Dict[Tuple[int, int], List[_TypeMember]] = {}

        for s_idx in range(section_count):
            (
                comp,
                data_off,
                comp_len,
                exp_len,
                _align,
                first16,
                first8,
                rel_off,
                rel_cnt,
                _marsh_off,
                _marsh_cnt,
            ) = struct.unpack_from("<11I", data, sec_table_off + s_idx * 44)

            if comp == _COMPRESSION_NONE:
                raw = data[data_off:data_off + exp_len]
                if len(raw) < exp_len:
                    raw = raw + b"\x00" * (exp_len - len(raw))
                self.sections.append(raw)
            elif comp in (_COMPRESSION_OODLE0, _COMPRESSION_OODLE1):
                comp_buf = data[data_off:data_off + comp_len] + b"\x00\x00\x00\x00"
                self.sections.append(_decompress_oodle1(comp_buf, exp_len, first16, first8))
            else:
                raise ValueError(f"Unsupported GR2 section compression {comp}")

            if rel_cnt and rel_off + rel_cnt * 12 <= len(data):
                for r_i in range(rel_cnt):
                    src_off, dst_sec, dst_off = struct.unpack_from("<3I", data, rel_off + r_i * 12)
                    self.relocs[(s_idx, src_off)] = (dst_sec, dst_off)

    def _reloc(self, sec: int, off: int) -> Optional[Tuple[int, int]]:
        return self.relocs.get((sec, off))

    def _read_u32(self, sec: int, off: int) -> int:
        buf = self.sections[sec]
        if off + 4 > len(buf):
            return 0
        return struct.unpack_from("<I", buf, off)[0]

    def _read_str_ptr(self, sec: int, off: int) -> str:
        target = self._reloc(sec, off)
        if target is None:
            return ""
        tsec, toff = target
        if tsec >= len(self.sections):
            return ""
        buf = self.sections[tsec]
        if toff >= len(buf):
            return ""
        end = buf.find(b"\x00", toff)
        if end < 0:
            end = len(buf)
        return buf[toff:end].decode("latin-1", errors="replace")

    def _member_base_size(self, mtype: int, ref_type: Optional[Tuple[int, int]]) -> int:
        p = self.ptr_size
        if mtype == _MT_INLINE:
            return self._struct_size(ref_type) if ref_type else 0
        if mtype in (_MT_REF, _MT_EMPTY_REF, _MT_STRING):
            return p
        if mtype in (_MT_REF_ARRAY, _MT_ARRAY_OF_REFS):
            return 4 + p
        if mtype == _MT_VARIANT_REF:
            return 2 * p
        if mtype == _MT_REF_VARIANT_ARRAY:
            return p + 4 + p
        if mtype == _MT_TRANSFORM:
            return 68
        if mtype in (_MT_REAL32, _MT_INT32, _MT_UINT32):
            return 4
        if mtype in (_MT_INT16, _MT_UINT16, _MT_BINORMAL_INT16, _MT_NORMAL_UINT16, _MT_REAL16):
            return 2
        if mtype in (_MT_INT8, _MT_UINT8, _MT_BINORMAL_INT8, _MT_NORMAL_UINT8):
            return 1
        return 0

    def _parse_type_def(self, tref: Optional[Tuple[int, int]]) -> List[_TypeMember]:
        if tref is None:
            return []
        if tref in self._type_cache:
            return self._type_cache[tref]
        sec, off = tref
        if sec >= len(self.sections):
            return []
        buf = self.sections[sec]
        members: List[_TypeMember] = []
        self._type_cache[tref] = members  # Guard cycles
        entry_size = 20 + 3 * self.ptr_size
        while off + entry_size <= len(buf):
            mtype = struct.unpack_from("<I", buf, off)[0]
            if mtype == _MT_NONE or mtype >= 23:
                break
            name = self._read_str_ptr(sec, off + 4)
            child_ptr_off = off + 4 + self.ptr_size
            ref_type = self._reloc(sec, child_ptr_off)
            arr_width = struct.unpack_from("<i", buf, child_ptr_off + self.ptr_size)[0]
            count = max(1, arr_width)
            stride = self._member_base_size(mtype, ref_type) * count
            members.append(_TypeMember(mtype=mtype, name=name, ref_type=ref_type, array_width=arr_width, stride=stride))
            off += entry_size
        return members

    def _struct_size(self, tref: Optional[Tuple[int, int]]) -> int:
        return sum(m.stride for m in self._parse_type_def(tref))

    def _read_scalar(self, mtype: int, buf: bytes, off: int) -> Any:
        if mtype == _MT_REAL32:
            return struct.unpack_from("<f", buf, off)[0]
        if mtype == _MT_INT32:
            return struct.unpack_from("<i", buf, off)[0]
        if mtype == _MT_UINT32:
            return struct.unpack_from("<I", buf, off)[0]
        if mtype in (_MT_INT16, _MT_BINORMAL_INT16):
            return struct.unpack_from("<h", buf, off)[0]
        if mtype in (_MT_UINT16, _MT_NORMAL_UINT16, _MT_REAL16):
            return struct.unpack_from("<H", buf, off)[0]
        if mtype in (_MT_INT8, _MT_BINORMAL_INT8):
            return struct.unpack_from("<b", buf, off)[0]
        if mtype in (_MT_UINT8, _MT_NORMAL_UINT8):
            return buf[off]
        return 0

    def _read_transform(self, sec: int, off: int) -> Gr2Transform:
        buf = self.sections[sec]
        if off + 68 > len(buf):
            return Gr2Transform()
        flags = struct.unpack_from("<I", buf, off)[0]
        vals = struct.unpack_from("<16f", buf, off + 4)
        return Gr2Transform(
            flags=flags,
            position=vals[0:3],
            orientation=vals[3:7],
            scale_shear=vals[7:16],
        )

    def _read_struct(self, tref: Optional[Tuple[int, int]], obj_ref: Optional[Tuple[int, int]]) -> Dict[str, Any]:
        if tref is None or obj_ref is None:
            return {}
        sec, off = obj_ref
        if sec >= len(self.sections):
            return {}
        buf = self.sections[sec]
        out: Dict[str, Any] = {"__ref__": obj_ref}
        cur = off
        for m in self._parse_type_def(tref):
            cnt = max(1, m.array_width)
            if m.mtype == _MT_INLINE:
                out[m.name] = self._read_struct(m.ref_type, (sec, cur))
            elif m.mtype in (_MT_REF, _MT_EMPTY_REF):
                target = self._reloc(sec, cur)
                out[m.name] = (m.ref_type, target)
            elif m.mtype == _MT_STRING:
                out[m.name] = self._read_str_ptr(sec, cur)
            elif m.mtype == _MT_TRANSFORM:
                out[m.name] = self._read_transform(sec, cur)
            elif m.mtype == _MT_REF_ARRAY:
                arr_cnt = self._read_u32(sec, cur)
                target = self._reloc(sec, cur + 4)
                out[m.name] = (m.ref_type, target, arr_cnt)
            elif m.mtype == _MT_ARRAY_OF_REFS:
                arr_cnt = self._read_u32(sec, cur)
                target = self._reloc(sec, cur + 4)
                refs: List[Optional[Tuple[int, int]]] = []
                if target is not None and arr_cnt > 0:
                    tsec, toff = target
                    for i in range(arr_cnt):
                        refs.append(self._reloc(tsec, toff + i * self.ptr_size))
                out[m.name] = (m.ref_type, refs)
            elif m.mtype == _MT_VARIANT_REF:
                vtype = self._reloc(sec, cur)
                vobj = self._reloc(sec, cur + self.ptr_size)
                out[m.name] = (vtype, vobj)
            elif m.mtype == _MT_REF_VARIANT_ARRAY:
                vtype = self._reloc(sec, cur)
                arr_cnt = self._read_u32(sec, cur + self.ptr_size)
                target = self._reloc(sec, cur + self.ptr_size + 4)
                out[m.name] = (vtype, target, arr_cnt)
            elif m.mtype in (
                _MT_REAL32, _MT_INT8, _MT_UINT8, _MT_BINORMAL_INT8, _MT_NORMAL_UINT8,
                _MT_INT16, _MT_UINT16, _MT_BINORMAL_INT16, _MT_NORMAL_UINT16,
                _MT_INT32, _MT_UINT32, _MT_REAL16,
            ):
                unit = m.stride // cnt
                if m.array_width <= 0:
                    out[m.name] = self._read_scalar(m.mtype, buf, cur)
                else:
                    out[m.name] = tuple(self._read_scalar(m.mtype, buf, cur + i * unit) for i in range(cnt))
            cur += m.stride
        return out

    def _read_ref_array_structs(
        self,
        tref: Optional[Tuple[int, int]],
        target: Optional[Tuple[int, int]],
        count: int,
    ) -> List[Dict[str, Any]]:
        if tref is None or target is None or count <= 0:
            return []
        stride = self._struct_size(tref)
        if stride <= 0:
            return []
        tsec, toff = target
        return [self._read_struct(tref, (tsec, toff + i * stride)) for i in range(count)]

    def parse(self) -> Gr2File:
        root = self._read_struct(self.type_ref, self.root_ref)
        gr2 = Gr2File(version=self.version)

        # Skeletons
        if "Skeletons" in root:
            sk_type, sk_refs = root["Skeletons"]
            for sref in sk_refs:
                if sref is None:
                    continue
                sobj = self._read_struct(sk_type, sref)
                bones: List[Gr2Bone] = []
                if "Bones" in sobj:
                    btype, btarget, bcnt = sobj["Bones"]
                    for bi, bobj in enumerate(self._read_ref_array_structs(btype, btarget, bcnt)):
                        inv = bobj.get("InverseWorldTransform", ())
                        bones.append(
                            Gr2Bone(
                                index=bi,
                                name=str(bobj.get("Name") or f"Bone_{bi}"),
                                parent_index=int(bobj.get("ParentIndex", -1)),
                                local_transform=bobj.get("Transform") or Gr2Transform(),
                                inverse_world_transform=tuple(float(x) for x in inv) if inv else (),
                            )
                        )
                gr2.skeletons.append(
                    Gr2Skeleton(
                        name=str(sobj.get("Name") or f"Skeleton_{len(gr2.skeletons)}"),
                        bones=bones,
                        lod_type=int(sobj.get("LODType", 0)),
                    )
                )

        # TrackGroups at root
        tg_by_ref: Dict[Tuple[int, int], Gr2TrackGroup] = {}
        if "TrackGroups" in root:
            tg_type, tg_refs = root["TrackGroups"]
            for tref in tg_refs:
                if tref is None:
                    continue
                tg = self._parse_track_group(tg_type, tref)
                tg_by_ref[tref] = tg
                gr2.track_groups.append(tg)

        # Animations
        if "Animations" in root:
            an_type, an_refs = root["Animations"]
            for aref in an_refs:
                if aref is None:
                    continue
                aobj = self._read_struct(an_type, aref)
                tgs: List[Gr2TrackGroup] = []
                if "TrackGroups" in aobj:
                    atg_type, atg_refs = aobj["TrackGroups"]
                    for tref in atg_refs:
                        if tref is None:
                            continue
                        if tref in tg_by_ref:
                            tgs.append(tg_by_ref[tref])
                        else:
                            tg = self._parse_track_group(atg_type, tref)
                            tg_by_ref[tref] = tg
                            tgs.append(tg)
                gr2.animations.append(
                    Gr2Animation(
                        name=str(aobj.get("Name") or f"Animation_{len(gr2.animations)}"),
                        duration=float(aobj.get("Duration", 0.0)),
                        time_step=float(aobj.get("TimeStep", 1.0 / 30.0)),
                        oversampling=float(aobj.get("Oversampling", 1.0)),
                        default_loop_count=int(aobj.get("DefaultLoopCount", 0)),
                        flags=int(aobj.get("Flags", 0)),
                        track_groups=tgs,
                    )
                )

        return gr2

    def _parse_track_group(self, tg_type: Optional[Tuple[int, int]], tref: Tuple[int, int]) -> Gr2TrackGroup:
        tg_obj = self._read_struct(tg_type, tref)
        tracks: List[Gr2TransformTrack] = []
        if "TransformTracks" in tg_obj:
            tt_type, tt_target, tt_cnt = tg_obj["TransformTracks"]
            for ti, tobj in enumerate(self._read_ref_array_structs(tt_type, tt_target, tt_cnt)):
                tracks.append(
                    Gr2TransformTrack(
                        name=str(tobj.get("Name") or f"Track_{ti}"),
                        flags=int(tobj.get("Flags", 0)),
                        orientation=self._parse_curve(tobj.get("OrientationCurve")),
                        position=self._parse_curve(tobj.get("PositionCurve")),
                        scale_shear=self._parse_curve(tobj.get("ScaleShearCurve")),
                    )
                )
        return Gr2TrackGroup(
            name=str(tg_obj.get("Name") or "TrackGroup"),
            transform_tracks=tracks,
            accumulation_flags=int(tg_obj.get("AccumulationFlags", 0)),
        )

    def _parse_curve(self, curve_field: Any) -> Optional[Gr2Curve]:
        if curve_field is None:
            return None
        # VariantReference (type_ref, obj_ref) or inline CurveData struct
        if isinstance(curve_field, tuple) and len(curve_field) == 2:
            ctype, cobj = curve_field
        elif isinstance(curve_field, dict) and "CurveData" in curve_field:
            cd = curve_field["CurveData"]
            if not isinstance(cd, tuple) or len(cd) != 2:
                return None
            ctype, cobj = cd
        else:
            return None

        if ctype is None or cobj is None:
            return None

        members = self._parse_type_def(ctype)
        if not members:
            return None
        codec = members[0].name.replace("CurveDataHeader_", "")
        cstruct = self._read_struct(ctype, cobj)
        hdr = cstruct.get(members[0].name, {})
        if not isinstance(hdr, dict):
            hdr = cstruct
        fmt = int(hdr.get("Format", 0))
        degree = int(hdr.get("Degree", 0))
        dim = int(codec[1]) if (len(codec) >= 2 and codec[0] == "D" and codec[1].isdigit()) else 0
        if codec == "DaIdentity":
            dim = int(cstruct.get("Dimension", dim or 4))

        return self._decode_curve(codec, fmt, degree, dim, cstruct)

    def _decode_curve(
        self,
        codec: str,
        fmt: int,
        degree: int,
        dim: int,
        cstruct: Dict[str, Any],
    ) -> Optional[Gr2Curve]:
        # Constant / Identity curves
        if codec == "DaIdentity":
            if dim == 9:
                val = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
            elif dim == 4:
                val = (0.0, 0.0, 0.0, 1.0)
            else:
                val = tuple(0.0 for _ in range(dim))
            return Gr2Curve(codec=codec, fmt=fmt, degree=degree, dimension=dim, constant_value=val)

        if codec.endswith("Constant32f"):
            ctrl = cstruct.get("Controls")
            if isinstance(ctrl, tuple):
                val = tuple(float(x) for x in ctrl)
                return Gr2Curve(codec=codec, fmt=fmt, degree=degree, dimension=len(val), constant_value=val)
            return None

        # Uncompressed 32-bit float curves (DaK32fC32f / DaKeyframes32f)
        if codec in ("DaK32fC32f", "DaKeyframes32f"):
            k_info = cstruct.get("Knots")
            c_info = cstruct.get("Controls")
            if not (isinstance(k_info, tuple) and isinstance(c_info, tuple)):
                return None
            knots = self._read_typed_array(k_info, "<f")
            controls_flat = self._read_typed_array(c_info, "<f")
            if not knots or not controls_flat:
                return None
            d = len(controls_flat) // len(knots)
            if d <= 0:
                return None
            controls = tuple(tuple(controls_flat[i * d:(i + 1) * d]) for i in range(len(knots)))
            return Gr2Curve(codec=codec, fmt=fmt, degree=degree, dimension=d, knots=knots, controls=controls)

        # Uncompressed quantized array curves (DaK16uC16u, DaK8uC8u)
        if codec in ("DaK16uC16u", "DaK8uC8u"):
            is_u8 = codec == "DaK8uC8u"
            kc_info = cstruct.get("KnotsControls")
            if not isinstance(kc_info, tuple):
                return None
            arr = self._read_typed_array(kc_info, "<B" if is_u8 else "<H")
            if not arr:
                return None
            one_over = float(cstruct.get("OneOverKnotScale", 0.0))
            if one_over == 0.0 and "OneOverKnotScaleTrunc" in cstruct:
                one_over = _float_from_high_u16(int(cstruct["OneOverKnotScaleTrunc"]))
            if one_over == 0.0:
                return None
            scales = self._read_float_ref_array(cstruct.get("ControlScales"))
            offsets = self._read_float_ref_array(cstruct.get("ControlOffsets"))
            d = len(scales) or dim
            if d <= 0 or len(offsets) != d:
                return None
            kcnt = len(arr) // (d + 1)
            if kcnt <= 0:
                return None
            kscale = 1.0 / one_over
            knots = tuple(kscale * float(v) for v in arr[:kcnt])
            cdata = arr[kcnt:]
            controls = tuple(
                tuple(offsets[j] + scales[j] * float(cdata[i * d + j]) for j in range(d))
                for i in range(kcnt)
            )
            return Gr2Curve(codec=codec, fmt=fmt, degree=degree, dimension=d, knots=knots, controls=controls)

        # Packed 3D / 9D curves with 16u / 8u knots & controls
        kc_info = cstruct.get("KnotsControls")
        if not isinstance(kc_info, tuple):
            return None

        if codec in ("D3K16uC16u", "D3I1K16uC16u", "D3K8uC8u", "D3I1K8uC8u"):
            is_u8 = codec.endswith("K8uC8u")
            is_i1 = "I1" in codec
            arr = self._read_typed_array(kc_info, "<B" if is_u8 else "<H")
            if not arr:
                return None
            one_over = _float_from_high_u16(int(cstruct.get("OneOverKnotScaleTrunc", 0)))
            if one_over == 0.0:
                return None
            scales = tuple(float(x) for x in cstruct.get("ControlScales", (1.0, 1.0, 1.0)))
            offsets = tuple(float(x) for x in cstruct.get("ControlOffsets", (0.0, 0.0, 0.0)))
            kcnt = len(arr) // (2 if is_i1 else 4)
            if kcnt <= 0:
                return None
            kscale = 1.0 / one_over
            knots = tuple(kscale * float(v) for v in arr[:kcnt])
            cdata = arr[kcnt:]
            if is_i1:
                controls = tuple(
                    (offsets[0] + scales[0] * cdata[i], offsets[1] + scales[1] * cdata[i], offsets[2] + scales[2] * cdata[i])
                    for i in range(kcnt)
                )
            else:
                controls = tuple(
                    (
                        offsets[0] + scales[0] * cdata[i * 3],
                        offsets[1] + scales[1] * cdata[i * 3 + 1],
                        offsets[2] + scales[2] * cdata[i * 3 + 2],
                    )
                    for i in range(kcnt)
                )
            return Gr2Curve(codec=codec, fmt=fmt, degree=degree, dimension=3, knots=knots, controls=controls)

        if codec == "D3I1K32fC32f":
            arr = self._read_typed_array(kc_info, "<f")
            if not arr:
                return None
            scales = tuple(float(x) for x in cstruct.get("ControlScales", (1.0, 1.0, 1.0)))
            offsets = tuple(float(x) for x in cstruct.get("ControlOffsets", (0.0, 0.0, 0.0)))
            kcnt = len(arr) // 2
            if kcnt <= 0:
                return None
            knots = tuple(float(v) for v in arr[:kcnt])
            cdata = arr[kcnt:]
            controls = tuple(
                (offsets[0] + scales[0] * cdata[i], offsets[1] + scales[1] * cdata[i], offsets[2] + scales[2] * cdata[i])
                for i in range(kcnt)
            )
            return Gr2Curve(codec=codec, fmt=fmt, degree=degree, dimension=3, knots=knots, controls=controls)

        if codec in ("D4nK16uC15u", "D4nK8uC7u"):
            is_u8 = codec == "D4nK8uC7u"
            arr = self._read_typed_array(kc_info, "<B" if is_u8 else "<H")
            if not arr:
                return None
            kcnt = len(arr) // 4
            if kcnt <= 0:
                return None
            one_over = float(cstruct.get("OneOverKnotScale", 0.0))
            if one_over == 0.0:
                return None
            kscale = 1.0 / one_over
            knots = tuple(kscale * float(v) for v in arr[:kcnt])
            entries = int(cstruct.get("ScaleOffsetTableEntries", 0))
            qmax = 127.0 if is_u8 else 32767.0
            scales, offsets = _quat_scales_offsets(entries, qmax)
            cdata = arr[kcnt:]
            controls_list: List[Tuple[float, ...]] = []
            mask = 0x7F if is_u8 else 0x7FFF
            sign_bit = 0x80 if is_u8 else 0x8000
            shift_hi = 6 if is_u8 else 14
            shift_lo = 7 if is_u8 else 15
            for i in range(kcnt):
                c0, c1, c2 = cdata[i * 3], cdata[i * 3 + 1], cdata[i * 3 + 2]
                neg = (c0 & sign_bit) != 0
                missing_idx = ((c1 >> shift_hi) & 0x2) | (c2 >> shift_lo)
                res = [0.0, 0.0, 0.0, 0.0]
                dst = missing_idx
                sum_sq = 0.0
                for val_raw in (c0, c1, c2):
                    dst = (dst + 1) & 3
                    v = offsets[dst] + scales[dst] * float(val_raw & mask)
                    res[dst] = v
                    sum_sq += v * v
                missing = math.sqrt(max(0.0, 1.0 - sum_sq))
                res[missing_idx] = -missing if neg else missing
                controls_list.append((res[0], res[1], res[2], res[3]))
            return Gr2Curve(codec=codec, fmt=fmt, degree=degree, dimension=4, knots=knots, controls=tuple(controls_list))

        if codec in ("D9I1K16uC16u", "D9I3K16uC16u", "D9I1K8uC8u", "D9I3K8uC8u"):
            is_u8 = codec.endswith("K8uC8u")
            is_i1 = "I1" in codec
            arr = self._read_typed_array(kc_info, "<B" if is_u8 else "<H")
            if not arr:
                return None
            one_over = _float_from_high_u16(int(cstruct.get("OneOverKnotScaleTrunc", 0)))
            if one_over == 0.0:
                return None
            scales = tuple(float(x) for x in cstruct.get("ControlScales", (1.0, 1.0, 1.0)))
            offsets = tuple(float(x) for x in cstruct.get("ControlOffsets", (0.0, 0.0, 0.0)))
            kcnt = len(arr) // (2 if is_i1 else 4)
            if kcnt <= 0:
                return None
            knots = tuple((1.0 / one_over) * float(v) for v in arr[:kcnt])
            cdata = arr[kcnt:]
            controls_list = []
            for i in range(kcnt):
                if is_i1:
                    sx = offsets[0] + scales[0] * cdata[i]
                    sy = offsets[1] + scales[1] * cdata[i]
                    sz = offsets[2] + scales[2] * cdata[i]
                else:
                    sx = offsets[0] + scales[0] * cdata[i * 3]
                    sy = offsets[1] + scales[1] * cdata[i * 3 + 1]
                    sz = offsets[2] + scales[2] * cdata[i * 3 + 2]
                controls_list.append((sx, 0.0, 0.0, 0.0, sy, 0.0, 0.0, 0.0, sz))
            return Gr2Curve(codec=codec, fmt=fmt, degree=degree, dimension=9, knots=knots, controls=tuple(controls_list))

        return None

    def _read_typed_array(self, arr_info: Any, fmt_char: str) -> Tuple[Any, ...]:
        if not isinstance(arr_info, tuple) or len(arr_info) != 3:
            return ()
        _tref, target, cnt = arr_info
        if target is None or cnt <= 0:
            return ()
        tsec, toff = target
        if tsec >= len(self.sections):
            return ()
        buf = self.sections[tsec]
        item_sz = struct.calcsize(fmt_char)
        if toff + cnt * item_sz > len(buf):
            cnt = max(0, (len(buf) - toff) // item_sz)
        if cnt <= 0:
            return ()
        return struct.unpack_from(f"<{cnt}{fmt_char[-1]}", buf, toff)

    def _read_float_ref_array(self, arr_info: Any) -> Tuple[float, ...]:
        if isinstance(arr_info, tuple) and len(arr_info) == 3:
            return tuple(float(x) for x in self._read_typed_array(arr_info, "<f"))
        if isinstance(arr_info, tuple):
            return tuple(float(x) for x in arr_info)
        return ()


def _float_from_high_u16(v: int) -> float:
    return struct.unpack("<f", struct.pack("<I", (v & 0xFFFF) << 16))[0]


_ONE_OVER_SQRT2 = 0.7071067811865476
_QUAT_SCALE_OFFSET_TABLE = (
    (_ONE_OVER_SQRT2 * 2.0, -_ONE_OVER_SQRT2),
    (_ONE_OVER_SQRT2 * 1.0, -_ONE_OVER_SQRT2 * 0.5),
    (_ONE_OVER_SQRT2 * 0.5, -_ONE_OVER_SQRT2 * 0.75),
    (_ONE_OVER_SQRT2 * 0.5, -_ONE_OVER_SQRT2 * 0.25),
    (_ONE_OVER_SQRT2 * 0.5, _ONE_OVER_SQRT2 * 0.25),
    (_ONE_OVER_SQRT2 * 0.25, -_ONE_OVER_SQRT2 * 0.250),
    (_ONE_OVER_SQRT2 * 0.25, -_ONE_OVER_SQRT2 * 0.125),
    (_ONE_OVER_SQRT2 * 0.25, 0.0),
    (-_ONE_OVER_SQRT2 * 2.0, _ONE_OVER_SQRT2),
    (-_ONE_OVER_SQRT2 * 1.0, _ONE_OVER_SQRT2 * 0.5),
    (-_ONE_OVER_SQRT2 * 0.5, _ONE_OVER_SQRT2 * 0.75),
    (-_ONE_OVER_SQRT2 * 0.5, _ONE_OVER_SQRT2 * 0.25),
    (-_ONE_OVER_SQRT2 * 0.5, -_ONE_OVER_SQRT2 * 0.25),
    (-_ONE_OVER_SQRT2 * 0.25, _ONE_OVER_SQRT2 * 0.250),
    (-_ONE_OVER_SQRT2 * 0.25, _ONE_OVER_SQRT2 * 0.125),
    (-_ONE_OVER_SQRT2 * 0.25, 0.0),
)


def _quat_scales_offsets(entries: int, qmax: float) -> Tuple[Tuple[float, ...], Tuple[float, ...]]:
    scales = []
    offsets = []
    for _ in range(4):
        idx = entries & 0xF
        entries >>= 4
        s, o = _QUAT_SCALE_OFFSET_TABLE[idx]
        scales.append(s / qmax)
        offsets.append(o)
    return tuple(scales), tuple(offsets)


# ---------------------------------------------------------------------------
# B-spline Curve Evaluation (degrees 0, 1, 2, 3+)
# ---------------------------------------------------------------------------

def evaluate_curve(curve: Optional[Gr2Curve], t: float, default: Tuple[float, ...]) -> Tuple[float, ...]:
    if curve is None:
        return default
    if curve.constant_value:
        return curve.constant_value
    knots = curve.knots
    controls = curve.controls
    if not knots or not controls:
        return default
    if len(knots) == 1 or len(controls) == 1:
        return controls[0]

    degree = max(0, curve.degree)
    # Locate knot interval: first index where knots[idx] > t
    k_idx = len(knots) - 1
    for i, kv in enumerate(knots):
        if kv > t:
            k_idx = i
            break

    if degree == 0:
        return controls[min(k_idx, len(controls) - 1)]

    # Build local knot and control window around k_idx (clamped endpoints)
    n_knots = len(knots)
    n_ctrls = len(controls)
    dim = curve.dimension

    def knot_at(idx: int) -> float:
        return knots[0] if idx < 0 else (knots[-1] if idx >= n_knots else knots[idx])

    def ctrl_at(idx: int) -> Tuple[float, ...]:
        return controls[0] if idx < 0 else (controls[-1] if idx >= n_ctrls else controls[idx])

    # Cox-de Boor evaluation on window [k_idx - degree .. k_idx]
    d_vecs = [list(ctrl_at(k_idx - degree + j)) for j in range(degree + 1)]
    for r in range(1, degree + 1):
        for j in range(degree, r - 1, -1):
            i_ctrl = k_idx - degree + j
            left = knot_at(i_ctrl - r)
            right = knot_at(i_ctrl + degree - r)
            # Standard Granny B-spline knot span [ti[i], ti[i + degree - r + 1]]:
            # Specifically when t in [t_{k-1}, t_k), control j uses span [t_{k - degree + j - 1}, t_{k + j - r})
            k_lo = knot_at(k_idx - degree + j - 1)
            k_hi = knot_at(k_idx + j - r)
            denom = k_hi - k_lo
            alpha = 0.0 if abs(denom) < 1e-12 else (t - k_lo) / denom
            alpha = max(0.0, min(1.0, alpha))
            one_m = 1.0 - alpha
            cur = d_vecs[j]
            prev = d_vecs[j - 1]
            for c in range(dim):
                cur[c] = one_m * prev[c] + alpha * cur[c]

    res = tuple(d_vecs[degree])
    if dim == 4:
        norm = math.sqrt(sum(v * v for v in res))
        if norm > 1e-12:
            res = tuple(v / norm for v in res)
        else:
            res = (0.0, 0.0, 0.0, 1.0)
    return res


def evaluate_curve_np(
    curve: Optional[Gr2Curve],
    times: "np.ndarray",
    default: Tuple[float, ...],
) -> "np.ndarray":
    """Vectorized `evaluate_curve`: evaluate `curve` at every time in `times`.

    Returns a (T, dim) float64 array that matches `evaluate_curve` sample-for-sample.
    """
    t = np.asarray(times, dtype=np.float64).reshape(-1)
    n = t.size

    def _broadcast(value: Tuple[float, ...]) -> np.ndarray:
        row = np.asarray(value, dtype=np.float64).reshape(1, -1)
        return np.repeat(row, n, axis=0)

    if curve is None:
        return _broadcast(default)
    if curve.constant_value:
        return _broadcast(curve.constant_value)
    if not curve.knots or not curve.controls:
        return _broadcast(default)
    if len(curve.knots) == 1 or len(curve.controls) == 1:
        return _broadcast(curve.controls[0])

    knots = np.asarray(curve.knots, dtype=np.float64)
    controls = np.asarray(curve.controls, dtype=np.float64)
    n_knots = knots.size
    n_ctrls = controls.shape[0]
    degree = max(0, curve.degree)

    # First knot index strictly greater than t (clamped to the last knot)
    k_idx = np.minimum(np.searchsorted(knots, t, side="right"), n_knots - 1)

    if degree == 0:
        return controls[np.minimum(k_idx, n_ctrls - 1)].copy()

    def knot_at(idx: np.ndarray) -> np.ndarray:
        return knots[np.clip(idx, 0, n_knots - 1)]

    # Cox-de Boor on the window [k_idx - degree .. k_idx], vectorized over all times
    d_vecs = [controls[np.clip(k_idx - degree + j, 0, n_ctrls - 1)] for j in range(degree + 1)]
    for r in range(1, degree + 1):
        for j in range(degree, r - 1, -1):
            k_lo = knot_at(k_idx - degree + j - 1)
            k_hi = knot_at(k_idx + j - r)
            denom = k_hi - k_lo
            valid = np.abs(denom) >= 1e-12
            alpha = np.where(valid, (t - k_lo) / np.where(valid, denom, 1.0), 0.0)
            alpha = np.clip(alpha, 0.0, 1.0)[:, None]
            d_vecs[j] = (1.0 - alpha) * d_vecs[j - 1] + alpha * d_vecs[j]

    res = d_vecs[degree]
    if curve.dimension == 4:
        norm = np.linalg.norm(res, axis=1, keepdims=True)
        ok = norm[:, 0] > 1e-12
        res = np.where(ok[:, None], res / np.where(norm > 1e-12, norm, 1.0), np.array([0.0, 0.0, 0.0, 1.0]))
    return res


def parse_gr2(data: bytes) -> Gr2File:
    """Parse a Granny2 .gr2 file (uncompressed or Oodle0/Oodle1 compressed)."""
    return _Gr2Reader(data).parse()
