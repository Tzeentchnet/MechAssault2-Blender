# MGIc textures

**Files:** `*.tif` (MGIc, not TIFF); also embedded in WDF properties for terrain heightfields and culture maps
**Reference:** [formats/mgic_texture.py](../../io_scene_mechassault2/formats/mgic_texture.py), [importers/texture_builder.py](../../io_scene_mechassault2/importers/texture_builder.py)

A texture is an [MGIc container](mgic.md) with a `"PIC "` section and, for paletted images, a `"PAL "` section.

## `"PIC "` chunks

| Tag | Type | Meaning |
| --- | --- | --- |
| `flgs` | `u32` | Format flags (required, below) |
| `wdth` | `u32` | Width in pixels (required) |
| `"hgt "` | `u32` | Height in pixels (required) |
| `mips` | `u32` | Mip level count (default 1) |
| `frms` | `u32` | Frame count (default 1): animation frames or cube faces |
| `dpth` | `u32` | Depth (default 1); above 1 means a volume texture |
| `bits` | bytes | Pixel payload; the last chunk the decoder needs |

Other chunks may appear before `bits`; skip them. Clamp `mips` to `floor(log2(max(w, h, d))) + 1`.

## Format flags (`flgs`)

| Bits | Meaning |
| --- | --- |
| `0–3` | Uncompressed pixel format code (used when bits 8–11 are 0) |
| `4` (`0x10`) | Xbox swizzled (Morton order); only for uncompressed formats |
| `7` (`0x80`) | Cubemap |
| `8–11` | Block compression: `1` = DXT1, `3` = DXT3, `5` = DXT5, `0` = none |

Uncompressed format codes:

| Code | Format | Bytes/pixel | Layout |
| --- | --- | --- | --- |
| 0 | A8R8G8B8 | 4 | Bytes `B, G, R, A` |
| 1 | R5G6B5 | 2 | `u16`: R bits 11–15, G 5–10, B 0–4 |
| 3 | A4R4G4B4 | 2 | `u16`: A 12–15, R 8–11, G 4–7, B 0–3 |
| 4 | L8 / P8 | 1 | Luminance, or a palette index when a `"PAL "` section exists |
| 5 | A8 | 1 | Alpha only; decode as white with that alpha |
| 7 | A8L8 | 2 | Bytes `L, A` |

Expand 5- and 6-bit channels by bit replication (`(v << 3) | (v >> 2)`, `(v << 2) | (v >> 4)`) and 4-bit channels by `v * 17`.

A texture is also a cubemap when `frms == 6` and `dpth == 1`, even without bit 7.

## Payload layout

```
for frame in 0 .. frms-1:          # frame stride, see below
    for mip in 0 .. mips-1:
        level data (w >> mip, h >> mip, d >> mip; each at least 1)
```

Level sizes:

- **DXT:** `ceil(w/4) * ceil(h/4) * d * block_size`, where block size is 8 bytes for DXT1 and 16 for DXT3/5.
- **Uncompressed:** `pitch * h * d`, where `pitch = round_up(w * bpp, row_align)`.

`row_align` is not stored. Linear (unswizzled) uncompressed images use 1, but some use 64. The importer picks 64 when the payload size matches 64-byte row alignment better than tight rows; the total may also be padded to a 16-byte multiple.

Frame stride is normally the sum of a frame's level sizes. When `frms > 1` and the payload equals `frms * round_up(frame_size, 128)`, each frame is padded to 128 bytes.

### Images are stored top row first

Decoded rows run top to bottom. Tools that use a bottom-left origin (OpenGL, Blender) must flip vertically. Meshes compensate with `v' = 1 - v`.

## DXT blocks

Standard S3TC/BCn: DXT1 is BC1, DXT3 is BC2 and DXT5 is BC3, with the usual DXT1 3-colour-plus-transparent mode when `c0 <= c1`. Blocks are stored row-major across the image. For volume textures, DXT data is grouped in slabs of up to 4 slices. Within a slab, blocks are stored row-major over the block grid, and each grid position holds one 4×4 block per slice in a row (slice order).

## Swizzled images (`flgs & 0x10`)

Xbox swizzle stores pixels in Morton (Z-order) sequence. Dimensions must be powers of two. To compute the linear pixel index for `(x, y, z)`, interleave coordinate bits starting from bit 0. At each bit level, take x's bit if `bit < w`, then y's if `bit < h`, then z's if `bit < d`, and append each to the output index in that order. Mip levels are swizzled independently.

```python
def swizzle_index(x, y, z, w, h, d):
    out, pos, bit = 0, 0, 1
    while bit < max(w, h, d):
        if bit < w: out |= ((x & bit) != 0) << pos; pos += 1
        if bit < h: out |= ((y & bit) != 0) << pos; pos += 1
        if bit < d: out |= ((z & bit) != 0) << pos; pos += 1
        bit <<= 1
    return out          # pixel at (x, y, z) is stored at payload[out * bpp]
```

## Palettes (`"PAL "` section)

| Chunk | Type | Meaning |
| --- | --- | --- |
| `nclr` | `u32` | Colour count |
| `"rgb "` | bytes | `nclr` entries of 4 bytes, `B, G, R, A` |

If the `"rgb "` payload is at least 4 bytes longer than `nclr * 4`, the entries are the **last** `nclr * 4` bytes. Code-4 pixels index this palette; clamp out-of-range indices.

## Cubemaps

Six faces are stored as six frames in the order **+X, −X, +Y, −Y, +Z, −Z** in MA2 (left-handed, Y-up) space. The importer projects them to an equirectangular map for Blender's world shader.

## Animated textures

Material references sometimes name a texture that does not exist because it is a frame sequence. If `name.tif` is missing, look for `name 01.tif`, `name 02.tif`, … (space, two-digit counter, from 01) and use them as frames.

## Alpha handling

The importer treats a texture as having alpha when any top-level pixel has `A != 255`. R5G6B5 textures never do. This decides whether an opaque material becomes alpha-tested (see [MGMODEL materials](mgmodel.md#alpha-mode)).

## Non-colour uses

The same container carries data images:

- **Terrain heightfield:** 8-bit single-channel (code 4 or 5), linear, inside the `TerrainHeight Field` WDF property. See [Maps](maps.md#terrain-materrain).
- **Culture map:** 16-bit (a 2-byte format code), linear, read as raw `u16` values rather than colours. See [Maps](maps.md#culture-scatter).

Read these without colour conversion, using the same `row_align` rule.
