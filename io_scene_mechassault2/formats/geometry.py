"""MGIc geometry decoder: VERS vertex buffers (.geomVert / {verts}) & INDX index buffers (.geomFace / {indices})."""
from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Optional, Tuple

import numpy as np


@dataclass
class VertexBuffer:
    flags: int
    count: int
    stride: int
    bbox_min: Tuple[float, float, float]
    bbox_max: Tuple[float, float, float]
    positions: np.ndarray              # (N, 3) float32
    normals: Optional[np.ndarray]      # (N, 3) float32
    uv0: Optional[np.ndarray]          # (N, 2) float32
    uv1: Optional[np.ndarray]          # (N, 2) float32
    colors: Optional[np.ndarray]       # (N, 4) uint8 RGBA
    tangents: Optional[np.ndarray]     # (N, 4) float32
    bone_indices: Optional[np.ndarray] # (N, 4) uint8
    bone_weights: Optional[np.ndarray] # (N, 4) float32
    skinned: bool = False


_KNOWN_FLAGS = 0x1 | 0x2 | 0x8 | 0x100 | 0x200 | 0x400 | 0x1000 | 0x2000 | 0x80000 | 0x100000


def vertex_stride(flags: int) -> Tuple[int, bool]:
    known = (flags & ~_KNOWN_FLAGS) == 0
    s = 0
    if flags & 0x1:
        s += 12
    if flags & 0x2:
        s += 12
    if flags & 0x100:
        s += 4
    if flags & 0x8:
        s += 4
    if flags & 0x100000:
        s += 16
    if flags & 0x200:
        s += 4
    if flags & 0x400:
        s += 8
    if flags & 0x1000:
        s += 8
    if flags & 0x2000:
        s += 16
    if flags & 0x80000:
        s += 20
    return s, known


def _payload_chunk(data: bytes, tag: bytes) -> Tuple[int, int]:
    if len(data) < 0x30 or data[4:8] != b"MGIc":
        raise ValueError("Not an MGIc geometry file")
    off = 0x20
    if data[off:off + 4] == b"XMLB":
        xlen = struct.unpack_from("<I", data, off + 8)[0]
        if xlen < 16 or off + xlen + 16 > len(data):
            raise ValueError("Invalid XMLB chunk")
        off += xlen
    if off + 16 > len(data) or data[off:off + 4] != tag:
        raise ValueError(f"Missing {tag!r} chunk")
    clen = struct.unpack_from("<I", data, off + 8)[0]
    if clen < 16 or off + clen > len(data):
        clen = len(data) - off
    return off + 16, off + clen


def _find_sub(data: bytes, start: int, end: int, tag: bytes) -> Optional[memoryview]:
    mv = memoryview(data)
    p = start
    while p + 8 <= end:
        clen = struct.unpack_from("<I", data, p + 4)[0]
        if clen < 8 or p + clen > end:
            return None
        if data[p:p + 4] == tag:
            return mv[p + 8:p + clen]
        p += clen
    return None


def decode_vertex_file(data: bytes) -> VertexBuffer:
    start, end = _payload_chunk(data, b"VERS")
    info = _find_sub(data, start, end, b"info")
    body = _find_sub(data, start, end, b"data")
    if info is None or len(info) < 36:
        raise ValueError("Vertex file missing 'info' sub-chunk")
    if body is None:
        raise ValueError("Vertex file missing 'data' sub-chunk")

    _ver, count, flags = struct.unpack_from("<3I", info, 0)
    bmin = struct.unpack_from("<3f", info, 12)
    bmax = struct.unpack_from("<3f", info, 24)
    if count == 0:
        raise ValueError("Vertex file has zero vertices")

    stride, known = vertex_stride(flags)
    actual = len(body) // count
    if not known or stride != actual or not (flags & 0x1):
        raise ValueError(f"Unsupported vertex layout 0x{flags:X} (actual stride {actual}, expected {stride})")

    # Build a NumPy structured dtype matching the exact MA2 vertex field order
    fields = []
    offset = 0
    fields.append(("pos", np.float32, (3,)))
    offset += 12

    if flags & 0x2:
        fields.append(("normal", np.float32, (3,)))
        offset += 12
    if flags & 0x100:
        fields.append(("pnorm", np.uint32))
        offset += 4
    if flags & 0x8:
        fields.append(("color", np.uint32))
        offset += 4
    if flags & 0x100000:
        fields.append(("tangent", np.float32, (4,)))
        offset += 16

    uv_sets = 1 if (flags & 0x200) else 2 if (flags & 0x400) else 1 if (flags & 0x1000) else 2 if (flags & 0x2000) else 0
    uv_short = bool(flags & (0x200 | 0x400))
    if uv_sets >= 1:
        fields.append(("uv0", np.int16 if uv_short else np.float32, (2,)))
        offset += 4 if uv_short else 8
    if uv_sets >= 2:
        fields.append(("uv1", np.int16 if uv_short else np.float32, (2,)))
        offset += 4 if uv_short else 8
    if flags & 0x80000:
        fields.append(("bidx", np.uint8, (4,)))
        fields.append(("bwt", np.float32, (4,)))
        offset += 20

    dt = np.dtype(fields, align=False)
    arr = np.frombuffer(body, dtype=dt, count=count)

    positions = arr["pos"].astype(np.float32, copy=True)
    normals: Optional[np.ndarray] = None
    if flags & 0x2:
        normals = arr["normal"].astype(np.float32, copy=True)
    elif flags & 0x100:
        p = arr["pnorm"].astype(np.int32)
        # 11-bit X, 11-bit Y, 10-bit Z signed (NORMPACKED3)
        nx = ((p << 21) >> 21).astype(np.float32) / 1023.0
        ny = (((p >> 11) << 21) >> 21).astype(np.float32) / 1023.0
        nz = ((p >> 22) << 22 >> 22).astype(np.float32) / 511.0
        normals = np.stack((nx, ny, nz), axis=1)
        lens = np.linalg.norm(normals, axis=1, keepdims=True)
        valid = lens > 1e-6
        normals = np.where(valid, normals / np.maximum(lens, 1e-6), normals)

    colors: Optional[np.ndarray] = None
    if flags & 0x8:
        c = arr["color"]  # 0xAARRGGBB -> RGBA uint8
        colors = np.empty((count, 4), dtype=np.uint8)
        colors[:, 0] = ((c >> 16) & 0xFF).astype(np.uint8)
        colors[:, 1] = ((c >> 8) & 0xFF).astype(np.uint8)
        colors[:, 2] = (c & 0xFF).astype(np.uint8)
        colors[:, 3] = ((c >> 24) & 0xFF).astype(np.uint8)

    tangents = arr["tangent"].astype(np.float32, copy=True) if (flags & 0x100000) else None

    uv0 = None
    uv1 = None
    if uv_sets >= 1:
        uv0 = arr["uv0"].astype(np.float32) / (1024.0 if uv_short else 1.0)
    if uv_sets >= 2:
        uv1 = arr["uv1"].astype(np.float32) / (1024.0 if uv_short else 1.0)

    skinned = bool(flags & 0x80000)
    bidx = arr["bidx"].copy() if skinned else None
    bwt = arr["bwt"].astype(np.float32, copy=True) if skinned else None

    return VertexBuffer(
        flags=flags,
        count=count,
        stride=stride,
        bbox_min=bmin,
        bbox_max=bmax,
        positions=positions,
        normals=normals,
        uv0=uv0,
        uv1=uv1,
        colors=colors,
        tangents=tangents,
        bone_indices=bidx,
        bone_weights=bwt,
        skinned=skinned,
    )


def decode_index_file(data: bytes) -> np.ndarray:
    start, end = _payload_chunk(data, b"INDX")
    body = _find_sub(data, start, end, b"data")
    if body is None:
        raise ValueError("Index file missing 'data' sub-chunk")
    count = len(body) // 2
    nidx = _find_sub(data, start, end, b"nidx")
    if nidx is not None and len(nidx) >= 4:
        n = struct.unpack_from("<I", nidx, 0)[0]
        if n <= count:
            count = n
    if count == 0:
        return np.zeros((0,), dtype=np.uint16)
    return np.frombuffer(body, dtype="<u2", count=count).copy()


def build_triangles(indices: np.ndarray, is_strip: bool, vertex_count: int) -> np.ndarray:
    """Expand triangle list or 0xFFFF-restarted triangle strip into (T, 3) uint32 triangles."""
    if indices.size < 3:
        return np.zeros((0, 3), dtype=np.uint32)

    if not is_strip:
        tri_count = indices.size // 3
        tris = indices[:tri_count * 3].reshape((tri_count, 3)).astype(np.uint32)
        valid = (
            (tris[:, 0] != tris[:, 1])
            & (tris[:, 1] != tris[:, 2])
            & (tris[:, 0] != tris[:, 2])
            & (tris[:, 0] < vertex_count)
            & (tris[:, 1] < vertex_count)
            & (tris[:, 2] < vertex_count)
        )
        return tris[valid]

    out = []
    idx_list = indices.tolist()
    run_start = 0
    for i, val in enumerate(idx_list):
        if val == 0xFFFF:
            run_start = i + 1
            continue
        if i - run_start < 2:
            continue
        k = i - run_start - 2
        if k & 1:
            a, b, c = idx_list[i - 1], idx_list[i - 2], val
        else:
            a, b, c = idx_list[i - 2], idx_list[i - 1], val
        if a != b and b != c and a != c and a < vertex_count and b < vertex_count and c < vertex_count:
            out.append((a, b, c))
    if not out:
        return np.zeros((0, 3), dtype=np.uint32)
    return np.asarray(out, dtype=np.uint32)
