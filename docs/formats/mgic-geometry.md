# MGIc geometry

**Files:** `.mgmodel{<vertices>}` / `.mgmodel{<indices>}` sub-entries, and the vertex/face files named by legacy `.mesh` files
**Reference:** [formats/geometry.py](../../io_scene_mechassault2/formats/geometry.py)

Each mesh has two separate [MGIc](mgic.md) files: a vertex buffer (`"VERS"` section) and an index buffer (`"INDX"` section). An optional `"XMLB"` section may come first; skip it by its length. The payload section's body is a run of 8-byte-header chunks (`tag`, `u32 length incl. header`).

## `VERS`: vertex buffers

Chunks:

| Tag | Meaning |
| --- | --- |
| `info` | Buffer description (36 bytes, below) |
| `data` | Interleaved vertices, `count * stride` bytes |

`info` payload:

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x00` | `u32` | Version (`1` in fixtures; not checked) |
| `0x04` | `u32` | Vertex count |
| `0x08` | `u32` | Vertex format flags |
| `0x0C` | `f32[3]` | Bounding box min |
| `0x18` | `f32[3]` | Bounding box max |

### Vertex format flags

Fields appear in each vertex **in this order** when their flag is set:

| Order | Flag | Field | Bytes | Encoding |
| --- | --- | --- | --- | --- |
| 1 | `0x1` | Position | 12 | `f32[3]` (always present) |
| 2 | `0x2` | Normal | 12 | `f32[3]` |
| 3 | `0x100` | Packed normal | 4 | `NORMPACKED3`, below |
| 4 | `0x8` | Colour | 4 | `D3DCOLOR`: `u32` `0xAARRGGBB` (bytes `B, G, R, A`) |
| 5 | `0x100000` | Tangent | 16 | `f32[4]` (read but not used) |
| 6 | `0x200` | 1 UV set | 4 | `i16[2]`, divide by 1024 |
| 6 | `0x400` | 2 UV sets | 8 | 2 × `i16[2]`, divide by 1024 |
| 6 | `0x1000` | 1 UV set | 8 | `f32[2]` |
| 6 | `0x2000` | 2 UV sets | 16 | 2 × `f32[2]` |
| 7 | `0x80000` | Skin | 20 | `u8[4]` bone indices, then `f32[4]` weights |

Expect at most one UV flag. The importer rejects buffers with unknown flag bits, or whose `len(data) / count` differs from the stride the flags give, rather than guess.

### `NORMPACKED3`

A `u32` holding three signed fields, **X in the low bits**:

| Bits | Component | Scale |
| --- | --- | --- |
| `0–10` | X, signed 11-bit | `/ 1023` |
| `11–21` | Y, signed 11-bit | `/ 1023` |
| `22–31` | Z, signed 10-bit | `/ 511` |

Sign-extend each field, scale, then renormalise the vector.

```python
def unpack_norm3(p):
    x = ((p & 0x7FF) ^ 0x400) - 0x400
    y = (((p >> 11) & 0x7FF) ^ 0x400) - 0x400
    z = (((p >> 22) & 0x3FF) ^ 0x200) - 0x200
    return x / 1023.0, y / 1023.0, z / 511.0
```

### UVs

UV origin is top-left (matching [texture rows](mgic-texture.md#images-are-stored-top-row-first)); flip V (`1 - v`) for bottom-left tools. Values outside 0–1 tile.

### Skin weights

Up to four influences per vertex. A bone index points into the mesh's **bone name list**, which comes from the owning node (`<bone>` children in [MGMODEL](mgmodel.md#skinning), `bone=` lines in [legacy `.mesh`/`.node`](maxport-text.md)). Without a list, indices are skeleton bone indices. Match names to [Granny2](gr2.md) bones case-insensitively: the mesh says `bone_clavicle_r` where the skeleton says `BONE_clavicle_R`. Ignore weights at or below `1e-5`.

## `INDX`: index buffers

| Tag | Meaning |
| --- | --- |
| `data` | `u16` indices |
| `nidx` | Optional `u32` index count; use it when it is ≤ the `data` length / 2 |

Whether the indices form a list or a strip is not stored here: the referencing mesh says so (`type="indexedstrip"` in MGMODEL, `type=indexedstrip` in `.mesh`). Anything else is a triangle list.

### Triangle strips

- `0xFFFF` restarts the strip.
- Within a run, triangle `k` (counting from 0 after each restart) uses `(i[k], i[k+1], i[k+2])` when `k` is even and `(i[k+1], i[k], i[k+2])` when odd, to keep winding consistent.
- Drop degenerate triangles (any two indices equal) and those that reference vertices past the end.

Triangle lists get the same degenerate and range filtering.

Winding is for MA2's left-handed space. After mirroring into a right-handed system, swap two indices per triangle (see [Coordinate systems](../coordinates.md)).
