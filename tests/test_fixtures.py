"""Synthetic binary fixture builders for pure-Python parser tests and Blender 5.2 integration tests.

No retail game files are required; every archive, texture, geometry buffer, model,
Granny2 skeleton/animation, WDF/MTB, and map is constructed deterministically in memory or tmpdir.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import struct
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
import zlib

import numpy as np

from io_scene_mechassault2.formats.wdf import K_CULTURE_MAP, PropType, wdf_key


# ---------------------------------------------------------------------------
# MGF v4 Archive Builder (plain + zlib-compressed)
# ---------------------------------------------------------------------------

def build_mgf_v4(files: Dict[str, bytes], compressed: bool = False, block_size: int = 4096) -> bytes:
    """Build an MGF v4 binary image mapping normalized relative paths -> file bytes."""
    norm_files = [(k.replace("\\", "/").strip("/"), v) for k, v in sorted(files.items())]

    # Build directory tree of rows:
    # row = [sibling_row, parent_row, first_child_row, name_crc_dummy, str_off, file_idx]
    rows: List[List[int]] = []
    children_of: Dict[int, List[int]] = {-1: []}
    dir_row_map: Dict[Tuple[str, ...], int] = {}
    str_buf = bytearray()
    strings_map: Dict[str, int] = {}

    def intern_str(s: str) -> int:
        if s in strings_map:
            return strings_map[s]
        off = len(str_buf)
        str_buf.extend(s.encode("latin-1") + b"\x00")
        strings_map[s] = off
        return off

    def ensure_dir(parts: Tuple[str, ...]) -> int:
        if not parts:
            return -1
        if parts in dir_row_map:
            return dir_row_map[parts]
        parent = ensure_dir(parts[:-1])
        r_idx = len(rows)
        rows.append([-1, parent, -1, 0, intern_str(parts[-1]), -1])
        dir_row_map[parts] = r_idx
        children_of.setdefault(r_idx, [])
        children_of[parent].append(r_idx)
        return r_idx

    file_records: List[Tuple[int, int]] = []  # filled after computing offsets
    payload_chunks: List[bytes] = []

    for f_idx, (path_str, content) in enumerate(norm_files):
        parts = tuple(p for p in path_str.split("/") if p)
        parent = ensure_dir(parts[:-1])
        r_idx = len(rows)
        rows.append([-1, parent, -1, 0, intern_str(parts[-1]), f_idx])
        children_of[parent].append(r_idx)
        payload_chunks.append(content)

    # Wire sibling / first_child links
    for p_idx, kids in children_of.items():
        if p_idx >= 0 and kids:
            rows[p_idx][2] = kids[0]
        for k_i in range(len(kids) - 1):
            rows[kids[k_i]][0] = kids[k_i + 1]

    nf = len(norm_files)
    nd = len(rows)
    lf = nf * 32
    ld = nd * 24
    ls = len(str_buf)

    of = 44
    od = of + lf
    os_off = od + ld
    data_start = (os_off + ls + 15) & ~15

    cur_off = data_start
    for content in payload_chunks:
        file_records.append((cur_off, len(content)))
        cur_off += len(content)

    logical = bytearray(cur_off)
    struct.pack_into(
        "<11I",
        logical,
        0,
        0x2066676D,  # 'mgf '
        4,
        0,
        nf,
        lf,
        of,
        nd,
        ld,
        od,
        ls,
        os_off,
    )

    for i, (foff, flen) in enumerate(file_records):
        # rec[3] = length, rec[6] = offset
        struct.pack_into("<8I", logical, of + i * 32, 0, 0, 0, flen, 0, 0, foff, 0)

    for i, r in enumerate(rows):
        struct.pack_into("<6i", logical, od + i * 24, *r)

    logical[os_off:os_off + ls] = str_buf
    for f_idx, content in enumerate(payload_chunks):
        foff, _ = file_records[f_idx]
        logical[foff:foff + len(content)] = content

    if not compressed:
        return bytes(logical)

    # Wrap in zlib-blocked compressed MGF container (0xC5735A0F)
    logical_size = len(logical)
    count = (logical_size + block_size - 1) // block_size
    comp_blocks: List[bytes] = []
    for bi in range(count):
        s = bi * block_size
        comp_blocks.append(zlib.compress(bytes(logical[s:s + block_size])))

    table_off = 32
    blk_off = table_off + count * 12
    out = bytearray(blk_off)
    struct.pack_into("<8I", out, 0, 1, 0xC5735A0F, count, logical_size, 0, block_size, 0, 0)
    cur_blk = blk_off
    for bi, cblk in enumerate(comp_blocks):
        struct.pack_into("<3I", out, table_off + bi * 12, cur_blk, len(cblk), 0)
        out.extend(cblk)
        cur_blk += len(cblk)
    return bytes(out)


# ---------------------------------------------------------------------------
# MGIc Picture & Heightfield / CultureMap Builders
# ---------------------------------------------------------------------------

def _mgic_chunk(tag: bytes, payload: bytes) -> bytes:
    return struct.pack("<4sI", tag, 8 + len(payload)) + payload


def _mgic_section(tag: bytes, chunks: Sequence[bytes]) -> bytes:
    body = b"".join(chunks)
    return struct.pack("<4sIII", tag, 0, 16 + len(body), len(chunks)) + body


def build_mgic_picture(
    width: int,
    height: int,
    flags: int,
    bits: bytes,
    mip_count: int = 1,
    frames: int = 1,
    depth: int = 1,
    palette_bgra: Optional[bytes] = None,
) -> bytes:
    """Build an MGIc texture (.tif) file with PIC and optional PAL sections."""
    pic_chunks = [
        _mgic_chunk(b"flgs", struct.pack("<I", flags)),
        _mgic_chunk(b"wdth", struct.pack("<I", width)),
        _mgic_chunk(b"hgt ", struct.pack("<I", height)),
        _mgic_chunk(b"mips", struct.pack("<I", mip_count)),
        _mgic_chunk(b"frms", struct.pack("<I", frames)),
        _mgic_chunk(b"dpth", struct.pack("<I", depth)),
        _mgic_chunk(b"bits", bits),
    ]
    sections = [_mgic_section(b"PIC ", pic_chunks)]
    if palette_bgra is not None:
        nclr = len(palette_bgra) // 4
        pal_chunks = [
            _mgic_chunk(b"nclr", struct.pack("<I", nclr)),
            _mgic_chunk(b"rgb ", palette_bgra),
        ]
        sections.append(_mgic_section(b"PAL ", pal_chunks))

    hdr = struct.pack("<4s4sII16s", b"\x00" * 4, b"MGIc", 0, len(sections), b"\x00" * 16)
    return hdr + b"".join(sections)


def _rgb888_to_565(r: int, g: int, b: int) -> int:
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def make_dxt1_solid_block(r: int, g: int, b: int) -> bytes:
    c = _rgb888_to_565(r, g, b)
    return struct.pack("<HHI", c, c, 0)


def make_dxt1_4x4_from_rgba(rgba: np.ndarray) -> bytes:
    """Encode a constant or 2-color 4x4 block into 8-byte DXT1."""
    # Pick min and max luminance pixels as endpoints
    r0, g0, b0 = int(rgba[0, 0, 0]), int(rgba[0, 0, 1]), int(rgba[0, 0, 2])
    c0 = _rgb888_to_565(r0, g0, b0)
    return struct.pack("<HHI", c0, c0, 0)


def make_dxt3_solid_block(r: int, g: int, b: int, a4: int = 0xF) -> bytes:
    arow = (a4 & 0xF) * 0x1111
    alpha = struct.pack("<4H", arow, arow, arow, arow)
    return alpha + make_dxt1_solid_block(r, g, b)


def make_dxt5_solid_block(r: int, g: int, b: int, a: int = 255) -> bytes:
    alpha = struct.pack("<BBHI", a, a, 0, 0)
    return alpha + make_dxt1_solid_block(r, g, b)


def morton_swizzle_rgba32(rgba: np.ndarray) -> bytes:
    """Swizzle (H, W, 4) uint8 RGBA into Xbox Morton-ordered B,G,R,A bytes (for testing unswizzle)."""
    h, w, _ = rgba.shape
    bgra = rgba[:, :, [2, 1, 0, 3]]
    out = np.zeros((h * w, 4), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            m = 0
            bx, by, bit = x, y, 0
            mx = w.bit_length() - 1
            my = h.bit_length() - 1
            for _ in range(max(mx, my)):
                if mx > 0:
                    m |= (bx & 1) << bit
                    bx >>= 1
                    mx -= 1
                    bit += 1
                if my > 0:
                    m |= (by & 1) << bit
                    by >>= 1
                    my -= 1
                    bit += 1
            out[m] = bgra[y, x]
    return out.tobytes()


# ---------------------------------------------------------------------------
# VERS / INDX Geometry Builders
# ---------------------------------------------------------------------------

def _mgic_chunk_16(tag: bytes, payload: bytes) -> bytes:
    return struct.pack("<4sIII", tag, 0, 16 + len(payload), 0) + payload


def pack_norm3(nx: float, ny: float, nz: float) -> int:
    ix = max(-1023, min(1023, int(round(nx * 1023.0)))) & 0x7FF
    iy = max(-1023, min(1023, int(round(ny * 1023.0)))) & 0x7FF
    iz = max(-511, min(511, int(round(nz * 511.0)))) & 0x3FF
    return ix | (iy << 11) | (iz << 22)


def build_vers_file(
    flags: int,
    positions: Sequence[Tuple[float, float, float]],
    normals: Optional[Sequence[Tuple[float, float, float]]] = None,
    colors: Optional[Sequence[int]] = None,
    tangents: Optional[Sequence[Tuple[float, float, float, float]]] = None,
    uv0: Optional[Sequence[Tuple[float, float]]] = None,
    uv1: Optional[Sequence[Tuple[float, float]]] = None,
    bone_indices: Optional[Sequence[Tuple[int, int, int, int]]] = None,
    bone_weights: Optional[Sequence[Tuple[float, float, float, float]]] = None,
    with_xmlb: bool = False,
) -> bytes:
    count = len(positions)
    arr = np.asarray(positions, dtype=np.float32)
    bmin = tuple(float(x) for x in arr.min(axis=0))
    bmax = tuple(float(x) for x in arr.max(axis=0))

    vbuf = bytearray()
    for i in range(count):
        vbuf.extend(struct.pack("<3f", *positions[i]))
        if flags & 0x2:
            n = normals[i] if normals else (0.0, 1.0, 0.0)
            vbuf.extend(struct.pack("<3f", *n))
        if flags & 0x100:
            n = normals[i] if normals else (0.0, 1.0, 0.0)
            vbuf.extend(struct.pack("<I", pack_norm3(*n)))
        if flags & 0x8:
            c = colors[i] if colors else 0xFFFFFFFF
            vbuf.extend(struct.pack("<I", c))
        if flags & 0x100000:
            t = tangents[i] if tangents else (1.0, 0.0, 0.0, 1.0)
            vbuf.extend(struct.pack("<4f", *t))
        if flags & 0x200:
            u, v = uv0[i] if uv0 else (0.0, 0.0)
            vbuf.extend(struct.pack("<2h", int(round(u * 1024.0)), int(round(v * 1024.0))))
        elif flags & 0x400:
            u, v = uv0[i] if uv0 else (0.0, 0.0)
            vbuf.extend(struct.pack("<2h", int(round(u * 1024.0)), int(round(v * 1024.0))))
            u2, v2 = uv1[i] if uv1 else (0.0, 0.0)
            vbuf.extend(struct.pack("<2h", int(round(u2 * 1024.0)), int(round(v2 * 1024.0))))
        elif flags & 0x1000:
            u, v = uv0[i] if uv0 else (0.0, 0.0)
            vbuf.extend(struct.pack("<2f", u, v))
        elif flags & 0x2000:
            u, v = uv0[i] if uv0 else (0.0, 0.0)
            u2, v2 = uv1[i] if uv1 else (0.0, 0.0)
            vbuf.extend(struct.pack("<4f", u, v, u2, v2))
        if flags & 0x80000:
            bi = bone_indices[i] if bone_indices else (0, 0, 0, 0)
            bw = bone_weights[i] if bone_weights else (1.0, 0.0, 0.0, 0.0)
            vbuf.extend(struct.pack("<4B4f", *bi, *bw))

    info_payload = struct.pack("<3I6f", 1, count, flags, *bmin, *bmax)
    sub = _mgic_chunk(b"info", info_payload) + _mgic_chunk(b"data", bytes(vbuf))
    hdr = struct.pack("<4s4s24s", b"\x00" * 4, b"MGIc", b"\x00" * 24)
    chunks = (_mgic_chunk_16(b"XMLB", b"<meta/>") if with_xmlb else b"") + _mgic_chunk_16(b"VERS", sub)
    return hdr + chunks


def build_indx_file(indices: Sequence[int], with_xmlb: bool = False) -> bytes:
    nidx = struct.pack("<I", len(indices))
    ibytes = struct.pack(f"<{len(indices)}H", *indices)
    sub = _mgic_chunk(b"nidx", nidx) + _mgic_chunk(b"data", ibytes)
    hdr = struct.pack("<4s4s24s", b"\x00" * 4, b"MGIc", b"\x00" * 24)
    chunks = (_mgic_chunk_16(b"XMLB", b"<meta/>") if with_xmlb else b"") + _mgic_chunk_16(b"INDX", sub)
    return hdr + chunks


# ---------------------------------------------------------------------------
# WDF Builder
# ---------------------------------------------------------------------------

def wdf_prop_u32(name_or_key: str | int, val: int, ptype: int = PropType.U32) -> Tuple[int, int, bytes]:
    k = wdf_key(name_or_key) if isinstance(name_or_key, str) else name_or_key
    return (ptype, k, struct.pack("<I", val & 0xFFFFFFFF))


def wdf_prop_i32(name_or_key: str | int, val: int) -> Tuple[int, int, bytes]:
    k = wdf_key(name_or_key) if isinstance(name_or_key, str) else name_or_key
    return (PropType.I32, k, struct.pack("<i", val))


def wdf_prop_f32(name_or_key: str | int, val: float) -> Tuple[int, int, bytes]:
    k = wdf_key(name_or_key) if isinstance(name_or_key, str) else name_or_key
    return (PropType.F32, k, struct.pack("<f", val))


def wdf_prop_vec2(name_or_key: str | int, x: float, y: float) -> Tuple[int, int, bytes]:
    k = wdf_key(name_or_key) if isinstance(name_or_key, str) else name_or_key
    return (PropType.VEC2, k, struct.pack("<2f", x, y))


def wdf_prop_vec3(name_or_key: str | int, x: float, y: float, z: float) -> Tuple[int, int, bytes]:
    k = wdf_key(name_or_key) if isinstance(name_or_key, str) else name_or_key
    return (PropType.VEC3, k, struct.pack("<3f", x, y, z))


def wdf_prop_str(name_or_key: str | int, text: str) -> Tuple[int, int, bytes]:
    k = wdf_key(name_or_key) if isinstance(name_or_key, str) else name_or_key
    return (PropType.STRING, k, text.encode("latin-1") + b"\x00")


def wdf_prop_blob(name_or_key: str | int, raw: bytes) -> Tuple[int, int, bytes]:
    k = wdf_key(name_or_key) if isinstance(name_or_key, str) else name_or_key
    return (PropType.BLOB, k, raw)


def build_wdf(objects: Sequence[Tuple[str | int, Sequence[Tuple[int, int, bytes]]]]) -> bytes:
    """Build a binary WDF/MTB stream from (class_name_or_key, properties) tuples."""
    buf = bytearray(struct.pack("<5I", 0xFF002F00, 1, 0, len(objects), 0))
    for cls, props in objects:
        ckey = wdf_key(cls) if isinstance(cls, str) else cls
        buf.extend(struct.pack("<3I", 0xFF012F01, len(props), ckey))
        for ptype, pkey, pdata in props:
            buf.extend(struct.pack("<4I", 0xFF022F02, ptype, pkey, len(pdata)))
            buf.extend(pdata)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Granny2 (.gr2 v6 LE) Builder
# ---------------------------------------------------------------------------

class Gr2Builder:
    """Minimal 32-bit LE Granny2 v6 builder emitting single-section GR2 files with relocations."""

    _MAGIC_V6_LE = bytes.fromhex("b867b0caf86db10f84728c7e5e19001e")

    def __init__(self) -> None:
        self.sec = bytearray()
        self.relocs: List[Tuple[int, int, int]] = []  # (src_off, dst_sec=0, dst_off)
        self._strings: Dict[str, int] = {}
        self._types: Dict[Tuple[Tuple[int, str, Optional[int], int], ...], int] = {}

    def align(self, a: int = 4) -> int:
        rem = len(self.sec) % a
        if rem:
            self.sec.extend(b"\x00" * (a - rem))
        return len(self.sec)

    def add_bytes(self, data: bytes, align: int = 4) -> int:
        off = self.align(align)
        self.sec.extend(data)
        return off

    def add_string(self, s: str) -> int:
        if s in self._strings:
            return self._strings[s]
        off = self.add_bytes(s.encode("latin-1") + b"\x00", align=1)
        self._strings[s] = off
        return off

    def add_reloc(self, src_off: int, dst_off: int) -> None:
        self.relocs.append((src_off, 0, dst_off))

    def add_type_def(self, members: Sequence[Tuple[int, str, Optional[int], int]]) -> int:
        """Emit a type definition table terminated by MT_NONE(0).

        Each member is (mtype, name, ref_type_off_or_None, array_width).
        """
        key = tuple(members)
        if key in self._types:
            return self._types[key]
        # Allocate contiguous member table + terminator BEFORE interning strings
        off = self.add_bytes(b"\x00" * ((len(members) + 1) * 32), align=4)
        self._types[key] = off
        for idx, (mtype, name, ref_off, arr_w) in enumerate(members):
            eoff = off + idx * 32
            struct.pack_into("<I", self.sec, eoff, mtype)
            struct.pack_into("<i", self.sec, eoff + 12, arr_w)
            n_off = self.add_string(name)
            self.add_reloc(eoff + 4, n_off)
            if ref_off is not None:
                self.add_reloc(eoff + 8, ref_off)
        return off

    def finalize(self, root_type_off: int, root_obj_off: int) -> bytes:
        header_size = 72
        sec_table_off = 72
        sec_table_size = 44
        data_off = sec_table_off + sec_table_size
        sec_bytes = bytes(self.sec)
        rel_off = data_off + len(sec_bytes)
        rel_bytes = b"".join(struct.pack("<3I", src, dsec, dst) for src, dsec, dst in self.relocs)
        total_size = rel_off + len(rel_bytes)

        out = bytearray(data_off)
        out[0:16] = self._MAGIC_V6_LE
        struct.pack_into("<II", out, 16, header_size, 0)
        struct.pack_into(
            "<10I",
            out,
            32,
            6,            # version
            total_size,   # file_size
            0,            # crc
            40,           # sections_rel_offset (32 + 40 = 72)
            1,            # section_count
            0,            # type_sec
            root_type_off,
            0,            # root_sec
            root_obj_off,
            0,            # tag
        )
        struct.pack_into(
            "<11I",
            out,
            sec_table_off,
            0,                # compression NONE
            data_off,
            len(sec_bytes),
            len(sec_bytes),
            4,                # align
            0, 0,             # first16, first8
            rel_off,
            len(self.relocs),
            0, 0,             # marsh_off, marsh_cnt
        )
        out.extend(sec_bytes)
        out.extend(rel_bytes)
        return bytes(out)


def _pack_d4n16(qx: float, qy: float, qz: float, qw: float) -> Tuple[int, int, int]:
    """Pack a unit quaternion (x,y,z,w) into D4nK16uC15u (using table entry 0 for all components)."""
    q = [qx, qy, qz, qw]
    # Normalize
    n = math.sqrt(sum(v * v for v in q)) or 1.0
    q = [v / n for v in q]
    # Choose largest abs component as missing index
    missing = max(range(4), key=lambda i: abs(q[i]))
    neg = q[missing] < 0.0
    # Table index 0 has s = sqrt(2), o = -1/sqrt(2); with qmax = 32767:
    # decoded = -1/sqrt(2) + (sqrt(2)/32767) * val
    inv_sqrt2 = 1.0 / math.sqrt(2.0)
    scale = math.sqrt(2.0) / 32767.0
    vals = []
    dst = missing
    for _ in range(3):
        dst = (dst + 1) & 3
        raw = int(round((q[dst] + inv_sqrt2) / scale))
        raw = max(0, min(0x7FFF, raw))
        vals.append(raw)
    c0 = vals[0] | (0x8000 if neg else 0)
    c1 = vals[1] | ((missing & 2) << 14)
    c2 = vals[2] | ((missing & 1) << 15)
    return c0, c1, c2


def _pack_d4n8(qx: float, qy: float, qz: float, qw: float) -> Tuple[int, int, int]:
    """Pack a unit quaternion into D4nK8uC7u (using table entry 0)."""
    q = [qx, qy, qz, qw]
    n = math.sqrt(sum(v * v for v in q)) or 1.0
    q = [v / n for v in q]
    missing = max(range(4), key=lambda i: abs(q[i]))
    neg = q[missing] < 0.0
    inv_sqrt2 = 1.0 / math.sqrt(2.0)
    scale = math.sqrt(2.0) / 127.0
    vals = []
    dst = missing
    for _ in range(3):
        dst = (dst + 1) & 3
        raw = int(round((q[dst] + inv_sqrt2) / scale))
        raw = max(0, min(0x7F, raw))
        vals.append(raw)
    c0 = vals[0] | (0x80 if neg else 0)
    c1 = vals[1] | ((missing & 2) << 6)
    c2 = vals[2] | ((missing & 1) << 7)
    return c0, c1, c2


def build_synthetic_gr2(
    bone_names: Sequence[str] = ("Pelvis", "Spine"),
    include_animation: bool = True,
    use_d4n8: bool = False,
) -> bytes:
    """Build a synthetic GR2 v6 LE file with Skeleton and multi-codec Animation."""
    b = Gr2Builder()

    # Common primitive type defs
    t_f32 = b.add_type_def([(10, "Real32", None, 0)])
    t_u16 = b.add_type_def([(16, "UInt16", None, 0)])
    t_u8 = b.add_type_def([(12, "UInt8", None, 0)])

    # CurveDataHeader
    t_hdr = b.add_type_def([
        (12, "Format", None, 0),
        (12, "Degree", None, 0),
    ])

    # Curve Types:
    # 1. DaK32fC32f (inline CurveDataHeader_DaK32fC32f + Knots array + Controls array)
    t_dak32 = b.add_type_def([
        (1, "CurveDataHeader_DaK32fC32f", t_hdr, 0),
        (3, "Knots", t_f32, 0),
        (3, "Controls", t_f32, 0),
    ])
    # 2. D3K16uC16u
    t_d3k16 = b.add_type_def([
        (1, "CurveDataHeader_D3K16uC16u", t_hdr, 0),
        (16, "OneOverKnotScaleTrunc", None, 0),
        (10, "ControlScales", None, 3),
        (10, "ControlOffsets", None, 3),
        (3, "KnotsControls", t_u16, 0),
    ])
    # 3. D4nK16uC15u
    t_d4k16 = b.add_type_def([
        (1, "CurveDataHeader_D4nK16uC15u", t_hdr, 0),
        (16, "ScaleOffsetTableEntries", None, 0),
        (10, "OneOverKnotScale", None, 0),
        (3, "KnotsControls", t_u16, 0),
    ])
    # 4. D4nK8uC7u
    t_d4k8 = b.add_type_def([
        (1, "CurveDataHeader_D4nK8uC7u", t_hdr, 0),
        (16, "ScaleOffsetTableEntries", None, 0),
        (10, "OneOverKnotScale", None, 0),
        (3, "KnotsControls", t_u8, 0),
    ])
    # 5. DaIdentity
    t_daid = b.add_type_def([
        (1, "CurveDataHeader_DaIdentity", t_hdr, 0),
        (19, "Dimension", None, 0),
    ])

    # Bone type: Name (str=4), ParentIndex (i32=4), Transform (68), InverseWorldTransform (real32[16]=64) -> 140 bytes
    t_bone = b.add_type_def([
        (8, "Name", None, 0),
        (19, "ParentIndex", None, 0),
        (9, "Transform", None, 0),
        (10, "InverseWorldTransform", None, 16),
    ])
    # Skeleton type: Name(4), Bones(8), LODType(4) -> 16 bytes
    t_skel = b.add_type_def([
        (8, "Name", None, 0),
        (3, "Bones", t_bone, 0),
        (19, "LODType", None, 0),
    ])

    # TransformTrack: Name(4), Flags(4), OrientationCurve(variant=8), PositionCurve(variant=8), ScaleShearCurve(variant=8) -> 32 bytes
    t_track = b.add_type_def([
        (8, "Name", None, 0),
        (19, "Flags", None, 0),
        (5, "OrientationCurve", None, 0),
        (5, "PositionCurve", None, 0),
        (5, "ScaleShearCurve", None, 0),
    ])
    # TrackGroup: Name(4), TransformTracks(8), AccumulationFlags(4) -> 16 bytes
    t_tg = b.add_type_def([
        (8, "Name", None, 0),
        (3, "TransformTracks", t_track, 0),
        (19, "AccumulationFlags", None, 0),
    ])
    # Animation: Name(4), Duration(4), TimeStep(4), Oversampling(4), DefaultLoopCount(4), Flags(4), TrackGroups(8) -> 32 bytes
    t_anim = b.add_type_def([
        (8, "Name", None, 0),
        (10, "Duration", None, 0),
        (10, "TimeStep", None, 0),
        (10, "Oversampling", None, 0),
        (19, "DefaultLoopCount", None, 0),
        (19, "Flags", None, 0),
        (4, "TrackGroups", t_tg, 0),
    ])
    # Root: Skeletons (array_of_refs=8), TrackGroups (8), Animations (8) -> 24 bytes
    t_root = b.add_type_def([
        (4, "Skeletons", t_skel, 0),
        (4, "TrackGroups", t_tg, 0),
        (4, "Animations", t_anim, 0),
    ])

    # --- Emit Skeleton Data ---
    inv_id = (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )
    bone_count = len(bone_names)
    bones_off = b.add_bytes(b"\x00" * (bone_count * 140), align=4)
    for bi, bname in enumerate(bone_names):
        boff = bones_off + bi * 140
        n_off = b.add_string(bname)
        b.add_reloc(boff + 0, n_off)
        parent = -1 if bi == 0 else bi - 1
        struct.pack_into("<i", b.sec, boff + 4, parent)
        # Transform: flags=7, pos=(0, 1.0 if bi > 0 else 0.0, 0), quat=(0,0,0,1), ss=identity
        pos_y = 1.0 if bi > 0 else 0.0
        struct.pack_into("<I3f4f9f", b.sec, boff + 8, 7, 0.0, pos_y, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        struct.pack_into("<16f", b.sec, boff + 76, *inv_id)

    skel_off = b.add_bytes(b"\x00" * 16, align=4)
    b.add_reloc(skel_off + 0, b.add_string("TestSkeleton"))
    struct.pack_into("<I", b.sec, skel_off + 4, bone_count)
    b.add_reloc(skel_off + 8, bones_off)
    skel_ptr_arr = b.add_bytes(b"\x00\x00\x00\x00", align=4)
    b.add_reloc(skel_ptr_arr, skel_off)

    # --- Emit Curves & Animation ---
    anim_ptr_arr = 0
    tg_ptr_arr = 0
    anim_count = 0
    tg_count = 0

    if include_animation:
        # Bone 0 Pos: DaK32fC32f (linear motion along Z: (0,0,0) -> (0,0,2.0))
        k_off = b.add_bytes(struct.pack("<2f", 0.0, 1.0), align=4)
        c_off = b.add_bytes(struct.pack("<6f", 0.0, 0.0, 0.0, 0.0, 0.0, 2.0), align=4)
        pos0_obj = b.add_bytes(b"\x00" * 18, align=4)
        struct.pack_into("<BB", b.sec, pos0_obj, 0, 1)  # degree 1
        struct.pack_into("<I", b.sec, pos0_obj + 2, 2)
        b.add_reloc(pos0_obj + 6, k_off)
        struct.pack_into("<I", b.sec, pos0_obj + 10, 6)
        b.add_reloc(pos0_obj + 14, c_off)

        # Bone 0 Rot: D4nK16uC15u or D4nK8uC7u rotating 90deg around Y axis in MA2
        ang0, ang1 = 0.0, math.pi * 0.5
        q0 = (0.0, math.sin(ang0 * 0.5), 0.0, math.cos(ang0 * 0.5))
        q1 = (0.0, math.sin(ang1 * 0.5), 0.0, math.cos(ang1 * 0.5))
        if use_d4n8:
            p0 = _pack_d4n8(*q0)
            p1 = _pack_d4n8(*q1)
            kc_off = b.add_bytes(struct.pack("<8B", 0, 100, *p0, *p1), align=4)
            rot0_type = t_d4k8
        else:
            p0 = _pack_d4n16(*q0)
            p1 = _pack_d4n16(*q1)
            kc_off = b.add_bytes(struct.pack("<8H", 0, 100, *p0, *p1), align=4)
            rot0_type = t_d4k16

        rot0_obj = b.add_bytes(b"\x00" * 16, align=4)
        struct.pack_into("<BB", b.sec, rot0_obj, 0, 1)  # degree 1
        struct.pack_into("<H", b.sec, rot0_obj + 2, 0)  # entries all table 0
        struct.pack_into("<f", b.sec, rot0_obj + 4, 100.0)  # OneOverKnotScale -> t = k/100
        struct.pack_into("<I", b.sec, rot0_obj + 8, 8)
        b.add_reloc(rot0_obj + 12, kc_off)

        # Shared Identity ScaleShear (DaIdentity dim=9)
        ss_obj = b.add_bytes(struct.pack("<BBi", 0, 0, 9), align=4)

        # Bone 1 Pos: D3K16uC16u from (0,1,0) to (0,1.5,0)
        one_over_trunc = struct.unpack("<I", struct.pack("<f", 100.0))[0] >> 16
        d3_kc = b.add_bytes(struct.pack("<8H", 0, 100, 0, 1000, 0, 0, 1500, 0), align=4)
        pos1_obj = b.add_bytes(b"\x00" * 36, align=4)
        struct.pack_into("<BBH", b.sec, pos1_obj, 0, 1, one_over_trunc)
        struct.pack_into("<3f3f", b.sec, pos1_obj + 4, 0.001, 0.001, 0.001, 0.0, 0.0, 0.0)
        struct.pack_into("<I", b.sec, pos1_obj + 28, 8)
        b.add_reloc(pos1_obj + 32, d3_kc)

        # Bone 1 Rot: DaIdentity dim=4
        rot1_obj = b.add_bytes(struct.pack("<BBi", 0, 0, 4), align=4)

        # Tracks
        tracks_off = b.add_bytes(b"\x00" * (bone_count * 32), align=4)
        for bi, bname in enumerate(bone_names):
            toff = tracks_off + bi * 32
            b.add_reloc(toff + 0, b.add_string(bname))
            struct.pack_into("<i", b.sec, toff + 4, 0)
            if bi == 0:
                b.add_reloc(toff + 8, rot0_type)
                b.add_reloc(toff + 12, rot0_obj)
                b.add_reloc(toff + 16, t_dak32)
                b.add_reloc(toff + 20, pos0_obj)
            else:
                b.add_reloc(toff + 8, t_daid)
                b.add_reloc(toff + 12, rot1_obj)
                b.add_reloc(toff + 16, t_d3k16)
                b.add_reloc(toff + 20, pos1_obj)
            b.add_reloc(toff + 24, t_daid)
            b.add_reloc(toff + 28, ss_obj)

        tg_off = b.add_bytes(b"\x00" * 16, align=4)
        b.add_reloc(tg_off + 0, b.add_string("TestSkeleton"))
        struct.pack_into("<I", b.sec, tg_off + 4, bone_count)
        b.add_reloc(tg_off + 8, tracks_off)

        tg_ptr_arr = b.add_bytes(b"\x00\x00\x00\x00", align=4)
        b.add_reloc(tg_ptr_arr, tg_off)
        tg_count = 1

        anim_off = b.add_bytes(b"\x00" * 32, align=4)
        b.add_reloc(anim_off + 0, b.add_string("Walk"))
        struct.pack_into("<3f2i", b.sec, anim_off + 4, 1.0, 1.0 / 30.0, 1.0, 1, 0)
        struct.pack_into("<I", b.sec, anim_off + 24, 1)
        b.add_reloc(anim_off + 28, tg_ptr_arr)

        anim_ptr_arr = b.add_bytes(b"\x00\x00\x00\x00", align=4)
        b.add_reloc(anim_ptr_arr, anim_off)
        anim_count = 1

    # Root object (24 bytes)
    root_off = b.add_bytes(b"\x00" * 24, align=4)
    struct.pack_into("<I", b.sec, root_off + 0, 1)
    b.add_reloc(root_off + 4, skel_ptr_arr)
    struct.pack_into("<I", b.sec, root_off + 8, tg_count)
    if tg_count:
        b.add_reloc(root_off + 12, tg_ptr_arr)
    struct.pack_into("<I", b.sec, root_off + 16, anim_count)
    if anim_count:
        b.add_reloc(root_off + 20, anim_ptr_arr)

    return b.finalize(t_root, root_off)


# ---------------------------------------------------------------------------
# Full Synthetic Workspace Assembly (MGF archives + Models + Maps + Textures)
# ---------------------------------------------------------------------------

def build_synthetic_workspace(base_dir: Path) -> Dict[str, Path]:
    """Create a complete synthetic MA2 GameFiles directory in `base_dir` for end-to-end tests."""
    base_dir.mkdir(parents=True, exist_ok=True)

    # Textures (DXT1, DXT5 with cutout alpha, A8R8G8B8 swizzled, DXT1 6-face cubemap)
    tex_diff = build_mgic_picture(4, 4, (1 << 8), make_dxt1_solid_block(200, 120, 60))
    tex_multi = build_mgic_picture(4, 4, (5 << 8), make_dxt5_solid_block(180, 60, 220, a=255))
    tex_det = build_mgic_picture(4, 4, (1 << 8), make_dxt1_solid_block(128, 128, 128))
    swiz_arr = np.full((4, 4, 4), [40, 200, 80, 255], dtype=np.uint8)
    tex_legacy = build_mgic_picture(4, 4, 0x10, morton_swizzle_rgba32(swiz_arr))
    tex_cube = build_mgic_picture(
        4, 4, (1 << 8) | 0x80,
        b"".join(make_dxt1_solid_block(30 * (f + 1), 60, 180) for f in range(6)),
        frames=6,
    )
    tex_colormap = build_mgic_picture(4, 4, (5 << 8), make_dxt5_solid_block(100, 150, 90, a=180))
    tex_lightmap = build_mgic_picture(4, 4, (1 << 8), make_dxt1_solid_block(220, 220, 220))

    # Geometry 1: Skinned + Packed Normals + D3DCOLOR + UV0+UV1 (.MGMODEL embedded streams)
    vflags_skinned = 0x1 | 0x100 | 0x8 | 0x2000 | 0x80000
    verts_mech = build_vers_file(
        vflags_skinned,
        positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)],
        normals=[(0.0, 0.0, 1.0)] * 4,
        colors=[0xFFFFFFFF, 0xFFFF8080, 0xFF80FF80, 0xFF8080FF],
        uv0=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
        uv1=[(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)],
        bone_indices=[(0, 1, 0, 0)] * 4,
        bone_weights=[(0.75, 0.25, 0.0, 0.0)] * 4,
        with_xmlb=True,
    )
    # Strip with 0xFFFF restart
    indx_mech = build_indx_file([0, 1, 2, 0xFFFF, 0, 2, 3], with_xmlb=True)

    # Box/plane for prop / hidden / LOD1
    vflags_simple = 0x1 | 0x2 | 0x1000
    verts_box = build_vers_file(
        vflags_simple,
        positions=[(-1.0, 0.0, -1.0), (1.0, 0.0, -1.0), (1.0, 0.0, 1.0), (-1.0, 0.0, 1.0)],
        normals=[(0.0, 1.0, 0.0)] * 4,
        uv0=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
    )
    indx_box = build_indx_file([0, 1, 2, 0, 2, 3])

    gr2_bytes = build_synthetic_gr2(("Pelvis", "Spine"), include_animation=True, use_d4n8=False)
    anm_text = "anim_walk\t../Anims/Walk.gr2\t0.5\t1\n"

    mgmodel_xml = """<model>
  <material_complex name="HullMat" diffuse="255,255,255" opacity="1.0"
                    colorshift0_color="0,255,255" colorshift1_color="255,128,0" colorshift1_diffuse="true"
                    reflectivity="0.25" detail_blend="modulate" detail_blend_level="0.5">
    <basetexture filename="../Textures/mech_diff.tif"/>
    <multitexture filename="../Textures/mech_multi.tif"/>
    <detailtexture filename="../Textures/mech_det.tif"/>
    <cubetexture filename="../Textures/env_cube.tif"/>
  </material_complex>
  <meshxml name="HullMesh" type="IndexedStrip" vertices="verts_hull" indices="idx_hull" material="HullMat"/>
  <meshxml name="HiddenPartMesh" type="IndexedList" vertices="verts_box" indices="idx_box" material="HullMat"/>
  <meshxml name="LowLodMesh" type="IndexedList" vertices="verts_box" indices="idx_box" material="HullMat"/>
  <node_anim name="RootAnim" position="0,0,0" skeleton="MechSkel.gr2" animation_set="MechAnims.anm">
    <children>
      <node_switch_lod name="HullSwitchLOD" position="0,0,0">
        <children>
          <node_skinned name="PelvisMeshNode" position="0,0,0">
            <bone name="Pelvis"/>
            <bone name="Spine"/>
            <mesh name="HullMesh"/>
          </node_skinned>
          <node_geom name="LowHullNode" position="0,0,0">
            <mesh name="LowLodMesh"/>
          </node_geom>
        </children>
      </node_switch_lod>
      <node_geom name="HiddenWing" position="2,0,0">
        <mesh name="HiddenPartMesh"/>
      </node_geom>
      <node_light name="Headlight" position="0,2,1" light_type="spot" light_color="255,200,150"
                  light_inner_radius="2.0" light_outer_radius="12.0" light_inner_angle="20.0" light_outer_angle="45.0"/>
    </children>
  </node_anim>
</model>""".encode("utf-8")

    # MTB hiding "HiddenWing" & tinting "PelvisMeshNode"
    mtb_bytes = build_wdf([
        ("MAModSubObjectShow", [wdf_prop_str("SubObjectName", "HiddenWing"), wdf_prop_i32("Enable", 0)]),
        (
            "MAModMechHeatSink",
            [
                wdf_prop_str("SubObjectName", "PelvisMeshNode"),
                wdf_prop_u32("MinimumHeatColorRed", 200),
                wdf_prop_u32("MinimumHeatColorGreen", 220),
                wdf_prop_u32("MinimumHeatColorBlue", 255),
            ],
        ),
    ])

    # Legacy .NODE / .mesh / .mat + ANIMNODE redirect
    legacy_root_node = """[LegacyRoot]
type = ANIMNODE
skeleton = MechSkel.gr2
child = LegacyBody.NODE
""".encode("utf-8")

    legacy_body_node = """[BodyNode]
type = NODE
position = 0, 1, 0
mesh = Box.mesh
num_children = 0
""".encode("utf-8")

    legacy_mesh = """[mesh]
name = BoxMesh
type = IndexedList
material = Box.mat
vertices = Box.geomVert
faces = Box.geomFace
""".encode("utf-8")

    legacy_mat = """[material]
name = BoxMat
type = BASE
diff_color = 255,255,255
base = ../Textures/legacy_tex.tif
""".encode("utf-8")

    # Simple static prop / sky / building MGMODEL
    prop_mgmodel = """<model>
  <material_base name="PropMat" diffuse="255,255,255" colorshift1_diffuse="true" colorshift1_color="255,0,0">
    <basetexture filename="../Textures/mech_diff.tif"/>
    <multitexture filename="../Textures/mech_multi.tif"/>
  </material_base>
  <meshxml name="PropMesh" type="IndexedList" vertices="verts" indices="idx" material="PropMat"/>
  <node_geom name="PropRoot" position="0,0,0">
    <mesh name="PropMesh"/>
  </node_geom>
</model>""".encode("utf-8")

    water_mgmodel = """<model>
  <material_water name="WaterMat" shallow_color="40,160,220" deep_color="10,40,90" opacity="0.7"/>
  <meshxml name="WaterMesh" type="IndexedList" vertices="verts" indices="idx" material="WaterMat"/>
  <node_geom name="WaterRoot" position="0,0,0">
    <mesh name="WaterMesh"/>
  </node_geom>
</model>""".encode("utf-8")

    road_ini = """[Road]
Model = ../Models/RoadSeg.MGMODEL
XRepeat = 1.0
ZRepeat = 2.0
""".encode("utf-8")

    # GlobalEntities.wdf
    global_entities = build_wdf([
        ("MARoot", [wdf_prop_u32("UID", 1), wdf_prop_u32("child", 10)]),
        (
            "MABuilding",
            [
                wdf_prop_u32("UID", 10),
                wdf_prop_u32("sibling", 20),
                wdf_prop_str("Description", "SynthBldg"),
                wdf_prop_str("Corporal3DObj", "Models/Prop.MGMODEL"),
            ],
        ),
        (
            "MATree",
            [
                wdf_prop_u32("UID", 20),
                wdf_prop_str("Description", "SynthGrass"),
                wdf_prop_str("Corporal3DObj", "Models/Prop.MGMODEL"),
            ],
        ),
    ])

    # Map terrain heightfield: 5x5 uint8 with row 0 at North (+Z), row 4 at South (Z=0)
    # Row 0 = 255 -> max_h (100), Row 4 = 0 -> min_h (0)
    hf_rows = np.array(
        [
            [255, 255, 255, 255, 255],
            [191, 191, 191, 191, 191],
            [127, 127, 127, 127, 127],
            [64, 64, 64, 64, 64],
            [0, 0, 0, 0, 0],
        ],
        dtype=np.uint8,
    )
    # Pad rows to 64-byte pitch to exercise 64-byte alignment detection
    hf_padded = np.zeros((5, 64), dtype=np.uint8)
    hf_padded[:, :5] = hf_rows
    hf_mgic = build_mgic_picture(5, 5, 4, hf_padded.tobytes())

    # Culture map 4x4 uint16: lower 4 bits set to 15 everywhere
    cmap_arr = np.full((4, 4), 15, dtype="<u2")
    cmap_mgic = build_mgic_picture(4, 4, 1, cmap_arr.tobytes())  # R5G6B5 (2 bytes per pixel)

    map_wdf = build_wdf([
        ("MARoot", [wdf_prop_u32("UID", 1), wdf_prop_u32("child", 2)]),
        (
            "MATerrain",
            [
                wdf_prop_u32("UID", 2),
                wdf_prop_u32("sibling", 3),
                wdf_prop_f32("TerrainMinHeight", 0.0),
                wdf_prop_f32("TerrainMaxHeight", 100.0),
                wdf_prop_f32("TerrainWorldScale", 10.0),
                wdf_prop_str("TerrainBaseMap", "Terrain/colormap.tif"),
                wdf_prop_str("TerrainDetailMap", "Textures/mech_det.tif"),
                wdf_prop_blob("TerrainHeight Field", hf_mgic),
            ],
        ),
        (
            "MAEnvironment",
            [
                wdf_prop_u32("UID", 3),
                wdf_prop_u32("sibling", 4),
                wdf_prop_u32("LightAmbientColor", 0xFF334455, PropType.COLOR),
                wdf_prop_i32("DirectionalLightOn", 1),
                wdf_prop_u32("DirectionalLightColor", 0xFFFFFFCC, PropType.COLOR),
                wdf_prop_vec3("DirectionalLightDirection", 1.0, -1.0, 0.5),
                wdf_prop_str("EnvironmentMapName", "Textures/env_cube.tif"),
                wdf_prop_str("LightMapName", "Terrain/lightmap.tif"),
            ],
        ),
        (
            "MASky2",
            [
                wdf_prop_u32("UID", 4),
                wdf_prop_u32("sibling", 5),
                wdf_prop_str("Corporal3DObj", "Models/Sky.MGMODEL"),
                wdf_prop_f32("CorporalHeading", 90.0),  # Must be ignored in Blender
                wdf_prop_vec3("SkyTerrainSkyScale", 200.0, 200.0, 200.0),
                wdf_prop_vec3("SkyTerrainSkyOffset", 1.0, 2.0, 3.0),
            ],
        ),
        (
            "MABuilding",
            [
                wdf_prop_u32("UID", 5),
                wdf_prop_u32("sibling", 6),
                wdf_prop_u32("TemplateID", 10),
                wdf_prop_str("Description", "Depot_01"),
                wdf_prop_vec3("CorporalPos", 10.0, 5.0, 20.0),
                wdf_prop_f32("CorporalHeading", 45.0),
            ],
        ),
        (
            "MATank",
            [
                wdf_prop_u32("UID", 6),
                wdf_prop_u32("sibling", 7),
                wdf_prop_str("Description", "Rommel_01"),
                wdf_prop_str("Corporal3DObj", "Models/Prop.MGMODEL"),
                wdf_prop_vec3("CorporalPos", 15.0, 2.0, 25.0),
                wdf_prop_i32("SupportsColorShift", 1),
                wdf_prop_i32("UseCustomColors", 1),
                wdf_prop_u32("CustomSinglePlayerDiffuseColor", 0xFF1080E0, PropType.COLOR),
            ],
        ),
        (
            "MARoad",
            [
                wdf_prop_u32("UID", 7),
                wdf_prop_u32("sibling", 8),
                wdf_prop_str("Description", "MainRoad"),
                wdf_prop_str("RoadDefinition", "Roads/Test.road"),
                wdf_prop_vec3("RoadPosition", 20.0, 1.0, 20.0),
                wdf_prop_vec3("RoadRotation", 0.5, 0.0, 0.0),
            ],
        ),
        (
            "MAWater",
            [
                wdf_prop_u32("UID", 8),
                wdf_prop_u32("sibling", 9),
                wdf_prop_str("Description", "Lake"),
                wdf_prop_str("Corporal3DObj", "Models/Water.MGMODEL"),
                wdf_prop_vec3("CorporalPos", 25.0, 0.5, 10.0),
            ],
        ),
        (
            "MAPointLight",
            [
                wdf_prop_u32("UID", 9),
                wdf_prop_u32("sibling", 100),
                wdf_prop_str("Description", "StreetLamp"),
                wdf_prop_i32("LightOn", 1),
                wdf_prop_vec3("CorporalPos", 12.0, 4.0, 12.0),
                wdf_prop_u32("LightColor", 0xFFFFAA44, PropType.COLOR),
                wdf_prop_f32("LightRadius", 15.0),
            ],
        ),
        (
            "MATerrainInfo",
            [
                wdf_prop_u32("UID", 100),
                wdf_prop_u32("child", 101),
                wdf_prop_i32("culturenBitfields", 1),
                wdf_prop_str("cultureBF0Name", "Grass"),
                wdf_prop_u32("cultureBF0Shift", 0),
                wdf_prop_i32("cultureBF0NBits", 4),
                wdf_prop_u32("cultureBF0Mask", 0x000F),
                wdf_prop_blob(K_CULTURE_MAP, cmap_mgic),
            ],
        ),
        (
            "MACultureType",
            [
                wdf_prop_u32("UID", 101),
                wdf_prop_u32("child", 102),
                wdf_prop_str("Name", "Grass"),
            ],
        ),
        (
            "MACultureGenerator",
            [
                wdf_prop_u32("UID", 102),
                wdf_prop_u32("GenerationTemplate", 20),
                wdf_prop_f32("Density", 2000.0),
                wdf_prop_vec2("GridScale", 10.0, 10.0),
                wdf_prop_i32("SnapToGrid", 1),
                wdf_prop_i32("RandomRotation", 1),
                wdf_prop_f32("RandomScaleRange", 0.2),
            ],
        ),
    ])

    mission_ini = """[Mission]
WorldFile = SynthArena.wdf
ResourceFile = syntharena.mgf
GameType = Destruction
""".encode("utf-8")

    common_mgf_bytes = build_mgf_v4(
        {
            "Templates/GlobalEntities.wdf": global_entities,
            "Multiplay/SynthArena.ini": mission_ini,
            "Multiplay/SynthArena.wdf": map_wdf,
            "Models/Mech.MGMODEL": mgmodel_xml,
            "Models/Mech.MGMODEL{verts_hull}": verts_mech,
            "Models/Mech.MGMODEL{idx_hull}": indx_mech,
            "Models/Mech.MGMODEL{verts_box}": verts_box,
            "Models/Mech.MGMODEL{idx_box}": indx_box,
            "Models/Mech.mtb": mtb_bytes,
            "Models/MechSkel.gr2": gr2_bytes,
            "Models/MechAnims.anm": anm_text.encode("utf-8"),
            "Anims/Walk.gr2": gr2_bytes,
            "Models/Legacy.NODE": legacy_root_node,
            "Models/LegacyBody.NODE": legacy_body_node,
            "Models/Box.mesh": legacy_mesh,
            "Models/Box.mat": legacy_mat,
            "Models/Box.geomVert": verts_box,
            "Models/Box.geomFace": indx_box,
            "Models/Prop.MGMODEL": prop_mgmodel,
            "Models/Prop.MGMODEL{verts}": verts_box,
            "Models/Prop.MGMODEL{idx}": indx_box,
            "Models/Sky.MGMODEL": prop_mgmodel,
            "Models/Sky.MGMODEL{verts}": verts_box,
            "Models/Sky.MGMODEL{idx}": indx_box,
            "Models/RoadSeg.MGMODEL": prop_mgmodel,
            "Models/RoadSeg.MGMODEL{verts}": verts_box,
            "Models/RoadSeg.MGMODEL{idx}": indx_box,
            "Models/Water.MGMODEL": water_mgmodel,
            "Models/Water.MGMODEL{verts}": verts_box,
            "Models/Water.MGMODEL{idx}": indx_box,
            "Roads/Test.road": road_ini,
            "Textures/mech_diff.tif": tex_diff,
            "Textures/mech_multi.tif": tex_multi,
            "Textures/mech_det.tif": tex_det,
            "Textures/legacy_tex.tif": tex_legacy,
            "Textures/env_cube.tif": tex_cube,
        },
        compressed=False,
    )

    arena_mgf_bytes = build_mgf_v4(
        {
            "Terrain/colormap.tif": tex_colormap,
            "Terrain/lightmap.tif": tex_lightmap,
        },
        compressed=True,
    )

    common_path = base_dir / "common.mgf"
    arena_path = base_dir / "syntharena.mgf"
    common_path.write_bytes(common_mgf_bytes)
    arena_path.write_bytes(arena_mgf_bytes)

    # Also write loose texture/model/gr2 files for file-picker operator tests
    loose_tex = base_dir / "loose_diff.tif"
    loose_tex.write_bytes(tex_diff)

    loose_gr2 = base_dir / "loose_walk.gr2"
    loose_gr2.write_bytes(gr2_bytes)

    return {
        "dir": base_dir,
        "common_mgf": common_path,
        "arena_mgf": arena_path,
        "loose_tex": loose_tex,
        "loose_gr2": loose_gr2,
    }


# ---------------------------------------------------------------------------
# Xbox XDVDFS (.iso / .xiso.iso) Builder
# ---------------------------------------------------------------------------

def _build_xdvdfs_dir_tree(names_sorted: List[str], children_info: Dict[str, Tuple[int, int, int]]) -> bytes:
    """Build an XDVDFS balanced BST directory buffer for `names_sorted` (upper/case-insensitive sorted).

    `children_info[name] = (sector, size, attr)` where attr=0x10 for dir, 0x00 for file.
    """
    if not names_sorted:
        return b""

    # Assign offsets first by building tree nodes recursively
    entries_buf = bytearray()
    node_Meta: Dict[int, int] = {}

    def build_node(lo: int, hi: int) -> int:
        if lo > hi:
            return 0
        mid = (lo + hi) // 2
        off = len(entries_buf)
        # Align node to 4 bytes
        rem = off % 4
        if rem:
            entries_buf.extend(b"\xFF" * (4 - rem))
            off = len(entries_buf)
        name = names_sorted[mid]
        name_bytes = name.encode("latin-1")
        nlen = len(name_bytes)
        rec_size = 14 + nlen
        pad = (-rec_size) % 4
        entries_buf.extend(b"\x00" * (rec_size + pad))
        if pad:
            entries_buf[off + rec_size:off + rec_size + pad] = b"\xFF" * pad

        left_off = build_node(lo, mid - 1)
        right_off = build_node(mid + 1, hi)
        sec, sz, attr = children_info[name]
        struct.pack_into("<HHIIBB", entries_buf, off, left_off // 4, right_off // 4, sec, sz, attr, nlen)
        entries_buf[off + 14:off + 14 + nlen] = name_bytes
        return off

    build_node(0, len(names_sorted) - 1)
    # Pad to 2048-byte sector boundary if non-empty
    rem = len(entries_buf) % 2048
    if rem:
        entries_buf.extend(b"\xFF" * (2048 - rem))
    return bytes(entries_buf)


def write_synthetic_xiso(
    out_path: Path,
    files: Dict[str, bytes],
    partition_offset: int = 0,
) -> Path:
    """Write a valid Xbox XDVDFS (.xiso.iso) file mapping relative paths -> file bytes.

    Supports non-zero `partition_offset` (e.g., Redump 0x18300000 or 0x2080000) via seek/sparse write.
    """
    SECTOR = 2048
    # Build directory hierarchy
    tree: Dict[str, Dict[str, Optional[bytes]]] = {"": {}}
    for rel_path, content in files.items():
        parts = [p for p in rel_path.replace("\\", "/").strip("/").split("/") if p]
        cur = ""
        for d in parts[:-1]:
            nxt = f"{cur}/{d}" if cur else d
            tree[cur].setdefault(d, None)
            tree.setdefault(nxt, {})
            cur = nxt
        tree[cur][parts[-1]] = content

    # Allocate sectors starting at sector 33 inside partition
    next_sec = 33
    dir_blobs: Dict[str, Tuple[int, int, bytes]] = {}
    file_allocs: List[Tuple[int, bytes]] = []

    def alloc_dir(dir_path: str) -> Tuple[int, int]:
        nonlocal next_sec
        kids = tree[dir_path]
        child_meta: Dict[str, Tuple[int, int, int]] = {}
        # Allocate subdirectories and files first so we know their sectors & sizes
        for name in sorted(kids.keys(), key=lambda s: s.upper()):
            val = kids[name]
            if val is None:
                sub_path = f"{dir_path}/{name}" if dir_path else name
                d_sec, d_sz = alloc_dir(sub_path)
                child_meta[name] = (d_sec, d_sz, 0x10)
            else:
                f_sec = next_sec
                f_sz = len(val)
                n_secs = max(1, (f_sz + SECTOR - 1) // SECTOR)
                next_sec += n_secs
                file_allocs.append((f_sec, val))
                child_meta[name] = (f_sec, f_sz, 0x00)

        names_sorted = sorted(kids.keys(), key=lambda s: s.upper())
        blob = _build_xdvdfs_dir_tree(names_sorted, child_meta)
        d_sec = next_sec
        d_sz = len(blob)
        next_sec += max(1, (d_sz + SECTOR - 1) // SECTOR)
        dir_blobs[dir_path] = (d_sec, d_sz, blob)
        return d_sec, d_sz

    root_sec, root_sz = alloc_dir("")

    # Build 2048-byte XDVDFS volume descriptor sector (sector 32)
    magic = b"MICROSOFT*XBOX*MEDIA"
    vol_sec = bytearray(SECTOR)
    vol_sec[0:20] = magic
    struct.pack_into("<II", vol_sec, 20, root_sec, root_sz)
    vol_sec[0x7EC:0x800] = magic

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fp:
        fp.seek(partition_offset + 32 * SECTOR)
        fp.write(vol_sec)
        for _dp, (d_sec, _d_sz, blob) in dir_blobs.items():
            fp.seek(partition_offset + d_sec * SECTOR)
            fp.write(blob)
        for f_sec, content in file_allocs:
            fp.seek(partition_offset + f_sec * SECTOR)
            fp.write(content)
        # Ensure end of last allocated sector exists on disk
        end_byte = partition_offset + next_sec * SECTOR
        fp.seek(end_byte - 1)
        fp.write(b"\x00")
    return out_path

