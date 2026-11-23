"""MGIc (.tif / terrain heightfield / culture map) texture decoder using vectorized NumPy."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import struct
from typing import List, Optional, Tuple

import numpy as np


class PixelFormat(Enum):
    UNKNOWN = "UNKNOWN"
    DXT1 = "DXT1"
    DXT3 = "DXT3"
    DXT5 = "DXT5"
    A8R8G8B8 = "A8R8G8B8"
    R5G6B5 = "R5G6B5"
    A4R4G4B4 = "A4R4G4B4"
    L8 = "L8"       # code 4: 8-bit luminance or P8 paletted
    A8 = "A8"       # code 5: alpha-only -> white RGB + alpha
    A8L8 = "A8L8"   # code 7: luminance + alpha


def _bpp(fmt: PixelFormat) -> int:
    if fmt == PixelFormat.A8R8G8B8:
        return 4
    if fmt in (PixelFormat.R5G6B5, PixelFormat.A4R4G4B4, PixelFormat.A8L8):
        return 2
    if fmt in (PixelFormat.L8, PixelFormat.A8):
        return 1
    return 0


def _is_compressed(fmt: PixelFormat) -> bool:
    return fmt in (PixelFormat.DXT1, PixelFormat.DXT3, PixelFormat.DXT5)


@dataclass
class TextureInfo:
    width: int = 0
    height: int = 0
    depth: int = 1
    frames: int = 1
    mip_count: int = 1
    flags: int = 0
    fmt: PixelFormat = PixelFormat.UNKNOWN
    swizzled: bool = False
    cube: bool = False
    volume: bool = False
    payload_offset: int = 0
    payload_bytes: int = 0
    row_align: int = 1
    palette: Optional[np.ndarray] = None  # (N, 4) uint8 RGBA


@dataclass
class DecodedTexture:
    info: TextureInfo
    # layers[frame * depth + slice][mip] -> (H, W, 4) uint8 RGBA (top-to-bottom)
    layers: List[List[np.ndarray]] = field(default_factory=list)
    has_alpha: bool = False
    alpha_is_binary: bool = False

    def top(self) -> Optional[np.ndarray]:
        if not self.layers or not self.layers[0]:
            return None
        return self.layers[0][0]


def _level_size(info: TextureInfo, level: int, row_align: int) -> int:
    w = max(1, info.width >> level)
    h = max(1, info.height >> level)
    d = max(1, info.depth >> level)
    if _is_compressed(info.fmt):
        bs = 8 if info.fmt == PixelFormat.DXT1 else 16
        return ((w + 3) // 4) * ((h + 3) // 4) * d * bs
    bpp = _bpp(info.fmt)
    pitch = ((w * bpp + row_align - 1) // row_align) * row_align
    return pitch * h * d


def _frame_bytes(info: TextureInfo, row_align: int) -> int:
    return sum(_level_size(info, l, row_align) for l in range(info.mip_count))


def parse_mgic_picture_header(data: bytes) -> TextureInfo:
    """Parse MGIc PIC (+ optional PAL) header without decoding pixels."""
    if len(data) < 56 or data[4:8] != b"MGIc":
        raise ValueError("Not an MGIc file")

    sec_count = struct.unpack_from("<I", data, 12)[0]
    pos = 0x20
    pic_found = False
    fields = {}
    payload_offset = 0
    payload_bytes = 0
    palette: Optional[np.ndarray] = None

    for _ in range(max(1, sec_count)):
        if pos + 16 > len(data):
            break
        sec_tag = data[pos:pos + 4]
        sec_len, nchunks = struct.unpack_from("<II", data, pos + 8)
        if sec_len < 16 or pos + sec_len > len(data):
            break
        sec_end = pos + sec_len
        cur = pos + 16
        if sec_tag == b"PIC ":
            pic_found = True
            while cur + 8 <= sec_end:
                tag = data[cur:cur + 4]
                clen = struct.unpack_from("<I", data, cur + 4)[0]
                if clen < 8 or cur + clen > sec_end:
                    raise ValueError(f"Invalid PIC chunk {tag!r}")
                if tag == b"bits":
                    payload_offset = cur + 8
                    payload_bytes = clen - 8
                    break
                if clen >= 12:
                    fields[tag] = struct.unpack_from("<I", data, cur + 8)[0]
                elif clen > 8:
                    fields[tag] = data[cur + 8]
                cur += clen
        elif sec_tag == b"PAL ":
            nclr = 0
            rgb_data = b""
            for _k in range(nchunks):
                if cur + 8 > sec_end:
                    break
                tag = data[cur:cur + 4]
                clen = struct.unpack_from("<I", data, cur + 4)[0]
                if clen < 8 or cur + clen > sec_end:
                    break
                if tag == b"nclr" and clen >= 12:
                    nclr = struct.unpack_from("<I", data, cur + 8)[0]
                elif tag == b"rgb ":
                    rgb_data = data[cur + 8:cur + clen]
                cur += clen
            if nclr > 0 and len(rgb_data) >= nclr * 4:
                start = len(rgb_data) - nclr * 4 if len(rgb_data) >= nclr * 4 + 4 else 0
                raw_pal = np.frombuffer(rgb_data, dtype=np.uint8, count=nclr * 4, offset=start).reshape((nclr, 4))
                # Palette entries are B, G, R, A -> convert to R, G, B, A
                palette = raw_pal[:, [2, 1, 0, 3]].copy()
        pos = sec_end

    if not pic_found or payload_offset == 0:
        raise ValueError("Missing PIC section or bits payload in MGIc")
    if b"flgs" not in fields or b"wdth" not in fields or b"hgt " not in fields:
        raise ValueError("Missing flgs/wdth/hgt in MGIc PIC")

    flags = fields[b"flgs"]
    width = int(fields[b"wdth"])
    height = int(fields[b"hgt "])
    mip_count = max(1, int(fields.get(b"mips", 1)))
    frames = max(1, int(fields.get(b"frms", 1)))
    depth = max(1, int(fields.get(b"dpth", 1)))

    if width < 1 or height < 1 or width > 16384 or height > 16384 or depth > 2048 or frames > 256:
        raise ValueError(f"MGIc dimensions out of range: {width}x{height}x{depth}x{frames}")

    comp = (flags >> 8) & 15
    fmt_code = flags & 15
    if comp:
        fmt = {1: PixelFormat.DXT1, 3: PixelFormat.DXT3, 5: PixelFormat.DXT5}.get(comp, PixelFormat.UNKNOWN)
    else:
        fmt = {
            0: PixelFormat.A8R8G8B8,
            1: PixelFormat.R5G6B5,
            3: PixelFormat.A4R4G4B4,
            4: PixelFormat.L8,
            5: PixelFormat.A8,
            7: PixelFormat.A8L8,
        }.get(fmt_code, PixelFormat.UNKNOWN)
    if fmt == PixelFormat.UNKNOWN:
        raise ValueError(f"Unsupported MGIc pixel format (flags=0x{flags:08X})")

    swizzled = (comp == 0) and bool(flags & 0x10)
    cube = bool(flags & 0x80) or (frames == 6 and depth == 1)
    volume = depth > 1

    maxdim = max(width, height, depth)
    max_levels = 1
    while (maxdim >> max_levels) >= 1 and max_levels < 16:
        max_levels += 1
    mip_count = min(mip_count, max_levels)

    info = TextureInfo(
        width=width,
        height=height,
        depth=depth,
        frames=frames,
        mip_count=mip_count,
        flags=flags,
        fmt=fmt,
        swizzled=swizzled,
        cube=cube,
        volume=volume,
        payload_offset=payload_offset,
        payload_bytes=payload_bytes,
        row_align=1,
        palette=palette,
    )

    if not comp and not swizzled:
        need1 = _frame_bytes(info, 1) * frames
        if need1 != payload_bytes and ((need1 + 15) & ~15) != payload_bytes:
            need64 = _frame_bytes(info, 64) * frames
            if need64 == payload_bytes or ((need64 + 15) & ~15) == payload_bytes or need1 > payload_bytes:
                info.row_align = 64
            elif need64 <= payload_bytes:
                info.row_align = 64

    return info


def _decode_rgb565(v: np.ndarray) -> np.ndarray:
    r = (v >> 11) & 0x1F
    g = (v >> 5) & 0x3F
    b = v & 0x1F
    out = np.empty(v.shape + (4,), dtype=np.uint8)
    out[..., 0] = ((r << 3) | (r >> 2)).astype(np.uint8)
    out[..., 1] = ((g << 2) | (g >> 4)).astype(np.uint8)
    out[..., 2] = ((b << 3) | (b >> 2)).astype(np.uint8)
    out[..., 3] = 255
    return out


def _decode_dxt_blocks(fmt: PixelFormat, src: bytes, block_count: int) -> np.ndarray:
    """Decode `block_count` DXT1/3/5 4x4 blocks -> (block_count, 4, 4, 4) uint8 RGBA."""
    bs = 8 if fmt == PixelFormat.DXT1 else 16
    raw = np.frombuffer(src, dtype=np.uint8, count=block_count * bs).reshape((block_count, bs))
    color_bytes = raw if fmt == PixelFormat.DXT1 else raw[:, 8:16]

    c0 = color_bytes[:, 0].astype(np.uint16) | (color_bytes[:, 1].astype(np.uint16) << 8)
    c1 = color_bytes[:, 2].astype(np.uint16) | (color_bytes[:, 3].astype(np.uint16) << 8)
    bits = (
        color_bytes[:, 4].astype(np.uint32)
        | (color_bytes[:, 5].astype(np.uint32) << 8)
        | (color_bytes[:, 6].astype(np.uint32) << 16)
        | (color_bytes[:, 7].astype(np.uint32) << 24)
    )

    p0 = _decode_rgb565(c0).astype(np.uint16)  # (N, 4)
    p1 = _decode_rgb565(c1).astype(np.uint16)
    pal = np.empty((block_count, 4, 4), dtype=np.uint8)
    pal[:, 0, :] = p0.astype(np.uint8)
    pal[:, 1, :] = p1.astype(np.uint8)

    if fmt == PixelFormat.DXT1:
        opaque4 = c0 > c1
        # 4-color mode
        c2_4 = ((2 * p0[:, :3] + p1[:, :3] + 1) // 3).astype(np.uint8)
        c3_4 = ((p0[:, :3] + 2 * p1[:, :3] + 1) // 3).astype(np.uint8)
        # 3-color + transparent mode
        c2_3 = ((p0[:, :3] + p1[:, :3]) // 2).astype(np.uint8)

        pal[:, 2, :3] = np.where(opaque4[:, None], c2_4, c2_3)
        pal[:, 2, 3] = 255
        pal[:, 3, :3] = np.where(opaque4[:, None], c3_4, 0)
        pal[:, 3, 3] = np.where(opaque4, 255, 0)
    else:
        pal[:, 2, :3] = ((2 * p0[:, :3] + p1[:, :3] + 1) // 3).astype(np.uint8)
        pal[:, 2, 3] = 255
        pal[:, 3, :3] = ((p0[:, :3] + 2 * p1[:, :3] + 1) // 3).astype(np.uint8)
        pal[:, 3, 3] = 255

    shifts = np.arange(0, 32, 2, dtype=np.uint32)  # (16,)
    idx = ((bits[:, None] >> shifts[None, :]) & 3).astype(np.intp)  # (N, 16)
    texels = pal[np.arange(block_count)[:, None], idx]  # (N, 16, 4)

    if fmt == PixelFormat.DXT3:
        alpha_raw = raw[:, :8]  # (N, 8) -> 16 nibbles
        lo = (alpha_raw & 0x0F) * 17
        hi = ((alpha_raw >> 4) & 0x0F) * 17
        alphas = np.empty((block_count, 16), dtype=np.uint8)
        alphas[:, 0::2] = lo
        alphas[:, 1::2] = hi
        texels[:, :, 3] = alphas
    elif fmt == PixelFormat.DXT5:
        a0 = raw[:, 0].astype(np.uint16)
        a1 = raw[:, 1].astype(np.uint16)
        apal = np.empty((block_count, 8), dtype=np.uint8)
        apal[:, 0] = a0.astype(np.uint8)
        apal[:, 1] = a1.astype(np.uint8)
        gt = a0 > a1
        for k in range(1, 7):
            v_gt = ((7 - k) * a0 + k * a1 + 3) // 7
            if k < 5:
                v_le = ((5 - k) * a0 + k * a1 + 2) // 5
                apal[:, k + 1] = np.where(gt, v_gt, v_le).astype(np.uint8)
            else:
                v_le = 0 if k == 5 else 255
                apal[:, k + 1] = np.where(gt, v_gt, v_le).astype(np.uint8)
        abits = np.zeros(block_count, dtype=np.uint64)
        for k in range(6):
            abits |= raw[:, 2 + k].astype(np.uint64) << (8 * k)
        ashifts = np.arange(0, 48, 3, dtype=np.uint64)
        aidx = ((abits[:, None] >> ashifts[None, :]) & 7).astype(np.intp)
        texels[:, :, 3] = apal[np.arange(block_count)[:, None], aidx]

    return texels.reshape((block_count, 4, 4, 4))


def _swizzle_indices(w: int, h: int, d: int) -> np.ndarray:
    """Compute Xbox Morton swizzle flat index map for a (d, h, w) volume/image."""
    xs = np.zeros(w, dtype=np.uint32)
    ys = np.zeros(h, dtype=np.uint32)
    zs = np.zeros(d, dtype=np.uint32)
    bit_pos = 0
    bit = 1
    mx = max(w, h, d)
    while bit < mx:
        if bit < w:
            xs |= ((np.arange(w, dtype=np.uint32) & bit) != 0).astype(np.uint32) << bit_pos
            bit_pos += 1
        if bit < h:
            ys |= ((np.arange(h, dtype=np.uint32) & bit) != 0).astype(np.uint32) << bit_pos
            bit_pos += 1
        if bit < d:
            zs |= ((np.arange(d, dtype=np.uint32) & bit) != 0).astype(np.uint32) << bit_pos
            bit_pos += 1
        bit <<= 1
    return (zs[:, None, None] | ys[None, :, None] | xs[None, None, :]).ravel()


def _expand_pixels(fmt: PixelFormat, raw_bytes: bytes, count: int, palette: Optional[np.ndarray]) -> np.ndarray:
    """Expand `count` packed pixels of `fmt` -> (count, 4) uint8 RGBA."""
    if fmt == PixelFormat.A8R8G8B8:
        arr = np.frombuffer(raw_bytes, dtype=np.uint8, count=count * 4).reshape((count, 4))
        return arr[:, [2, 1, 0, 3]].copy()
    if fmt == PixelFormat.R5G6B5:
        v = np.frombuffer(raw_bytes, dtype="<u2", count=count)
        return _decode_rgb565(v)
    if fmt == PixelFormat.A4R4G4B4:
        v = np.frombuffer(raw_bytes, dtype="<u2", count=count)
        out = np.empty((count, 4), dtype=np.uint8)
        out[:, 0] = (((v >> 8) & 0xF) * 17).astype(np.uint8)
        out[:, 1] = (((v >> 4) & 0xF) * 17).astype(np.uint8)
        out[:, 2] = ((v & 0xF) * 17).astype(np.uint8)
        out[:, 3] = (((v >> 12) & 0xF) * 17).astype(np.uint8)
        return out
    if fmt == PixelFormat.A8:
        a = np.frombuffer(raw_bytes, dtype=np.uint8, count=count)
        out = np.full((count, 4), 255, dtype=np.uint8)
        out[:, 3] = a
        return out
    if fmt == PixelFormat.A8L8:
        arr = np.frombuffer(raw_bytes, dtype=np.uint8, count=count * 2).reshape((count, 2))
        out = np.empty((count, 4), dtype=np.uint8)
        out[:, 0] = arr[:, 0]
        out[:, 1] = arr[:, 0]
        out[:, 2] = arr[:, 0]
        out[:, 3] = arr[:, 1]
        return out
    if fmt == PixelFormat.L8:
        idx = np.frombuffer(raw_bytes, dtype=np.uint8, count=count)
        if palette is not None and len(palette) > 0:
            safe_idx = np.clip(idx, 0, len(palette) - 1)
            return palette[safe_idx].copy()
        out = np.empty((count, 4), dtype=np.uint8)
        out[:, 0] = idx
        out[:, 1] = idx
        out[:, 2] = idx
        out[:, 3] = 255
        return out
    raise ValueError(f"Unsupported pixel format: {fmt}")


def _decode_level(info: TextureInfo, src: bytes, level: int) -> List[np.ndarray]:
    w = max(1, info.width >> level)
    h = max(1, info.height >> level)
    d = max(1, info.depth >> level)

    if _is_compressed(info.fmt):
        bw = (w + 3) // 4
        bh = (h + 3) // 4
        bs = 8 if info.fmt == PixelFormat.DXT1 else 16
        if len(src) < bw * bh * d * bs:
            raise ValueError("Short DXT payload")
        slices: List[np.ndarray] = []
        if d == 1:
            blocks = _decode_dxt_blocks(info.fmt, src, bw * bh)
            img = blocks.reshape((bh, bw, 4, 4, 4)).transpose((0, 2, 1, 3, 4)).reshape((bh * 4, bw * 4, 4))
            slices.append(img[:h, :w, :].copy())
        else:
            cursor = 0
            for z0 in range(0, d, 4):
                group = min(4, d - z0)
                blocks = _decode_dxt_blocks(info.fmt, src[cursor:], bw * bh * group)
                grid = blocks.reshape((bh, bw, group, 4, 4, 4)).transpose((2, 0, 3, 1, 4, 5))
                grid = grid.reshape((group, bh * 4, bw * 4, 4))
                for g in range(group):
                    slices.append(grid[g, :h, :w, :].copy())
                cursor += bw * bh * group * bs
        return slices

    bpp = _bpp(info.fmt)
    if info.swizzled:
        if (w & (w - 1)) or (h & (h - 1)) or (d & (d - 1)):
            raise ValueError(f"Swizzled texture dimensions must be power-of-two: {w}x{h}x{d}")
        total = w * h * d
        if len(src) < total * bpp:
            raise ValueError("Short swizzled texture payload")
        pixels = _expand_pixels(info.fmt, src, total, info.palette)
        table = _swizzle_indices(w, h, d)
        unswizzled = pixels[table].reshape((d, h, w, 4))
        return [unswizzled[z].copy() for z in range(d)]

    pitch = ((w * bpp + info.row_align - 1) // info.row_align) * info.row_align
    if len(src) < pitch * h * d:
        raise ValueError("Short linear texture payload")
    slices = []
    for z in range(d):
        z_off = z * h * pitch
        if pitch == w * bpp:
            px = _expand_pixels(info.fmt, src[z_off:z_off + h * pitch], w * h, info.palette)
            slices.append(px.reshape((h, w, 4)))
        else:
            rows = np.frombuffer(src, dtype=np.uint8, count=h * pitch, offset=z_off).reshape((h, pitch))
            tight = np.ascontiguousarray(rows[:, :w * bpp]).tobytes()
            px = _expand_pixels(info.fmt, tight, w * h, info.palette)
            slices.append(px.reshape((h, w, 4)))
    return slices


def decode_mgic_texture(data: bytes, all_mips: bool = False) -> DecodedTexture:
    """Decode an MGIc (.tif) image buffer into RGBA8 NumPy arrays."""
    info = parse_mgic_picture_header(data)
    payload = data[info.payload_offset:info.payload_offset + info.payload_bytes]

    frame_bytes = _frame_bytes(info, info.row_align)
    stride = frame_bytes
    rounded = (frame_bytes + 127) & ~127
    if info.frames > 1 and rounded * info.frames == len(payload):
        stride = rounded
    lvl0 = _level_size(info, 0, info.row_align)
    if lvl0 * info.frames > len(payload):
        raise ValueError("MGIc payload shorter than level 0")
    if stride * info.frames > len(payload):
        stride = len(payload) // info.frames

    levels = info.mip_count if all_mips else 1
    layers: List[List[np.ndarray]] = [[] for _ in range(info.frames * info.depth)]

    for f in range(info.frames):
        frame_start = f * stride
        frame_buf = payload[frame_start:len(payload)]
        off = 0
        for lvl in range(levels):
            ls = _level_size(info, lvl, info.row_align)
            if off + ls > len(frame_buf):
                break
            slices = _decode_level(info, frame_buf[off:off + ls], lvl)
            for z, img in enumerate(slices):
                layers[f * info.depth + z].append(img)
            off += ls

    tex = DecodedTexture(info=info, layers=layers)
    top = tex.top()
    if top is not None:
        alpha = top[..., 3]
        has_alpha = bool(np.any(alpha != 255))
        tex.has_alpha = has_alpha
        tex.alpha_is_binary = has_alpha and bool(np.all((alpha == 0) | (alpha == 255)))
    return tex


def decode_heightfield_samples8(data: bytes) -> Tuple[int, int, np.ndarray]:
    """Decode MATerrain 8-bit heightfield -> (width, height, uint8[height, width]) in stored row order."""
    info = parse_mgic_picture_header(data)
    if _is_compressed(info.fmt) or info.swizzled or _bpp(info.fmt) != 1:
        raise ValueError("Heightfield is not an uncompressed 8-bit MGIc picture")
    payload = data[info.payload_offset:info.payload_offset + info.payload_bytes]
    pitch = ((info.width + info.row_align - 1) // info.row_align) * info.row_align
    if len(payload) < pitch * info.height:
        raise ValueError("Short heightfield payload")
    rows = np.frombuffer(payload, dtype=np.uint8, count=info.height * pitch).reshape((info.height, pitch))
    return info.width, info.height, rows[:, :info.width].copy()


def decode_culture_map_u16(data: bytes) -> Tuple[int, int, np.ndarray]:
    """Decode MATerrainInfo 16-bit culture map -> (width, height, uint16[height, width])."""
    info = parse_mgic_picture_header(data)
    if _is_compressed(info.fmt) or info.swizzled or _bpp(info.fmt) != 2:
        raise ValueError("Culture map is not an uncompressed 16-bit MGIc picture")
    payload = data[info.payload_offset:info.payload_offset + info.payload_bytes]
    pitch = ((info.width * 2 + info.row_align - 1) // info.row_align) * info.row_align
    if len(payload) < pitch * info.height:
        raise ValueError("Short culture map payload")
    rows = np.frombuffer(payload, dtype=np.uint8, count=info.height * pitch).reshape((info.height, pitch))
    tight = np.ascontiguousarray(rows[:, :info.width * 2])
    return info.width, info.height, tight.view("<u2").reshape((info.height, info.width)).copy()
