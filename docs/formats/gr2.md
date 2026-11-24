# Granny2 skeletons and animations

**Files:** `*.gr2` (skeletons and animation clips), `*.anm` (animation sets)
**Reference:** [formats/gr2.py](../../io_scene_mechassault2/formats/gr2.py), [importers/animation_builder.py](../../io_scene_mechassault2/importers/animation_builder.py)

MechAssault 2 uses RAD Game Tools' Granny 2 runtime format. A file is a set of sections joined by pointer relocations, plus a self-describing **type tree** that tells you how to read the root object. MA2 skeleton files hold `Skeletons`; clip files hold `Animations` and their `TrackGroups`.

Mature third-party readers of this format exist (for example the `LSLib` and `opengr2` projects). This page covers what MA2 files need.

## File header

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x00` | `u8[16]` | Magic, below |
| `0x10` | `u32` | Header size |
| `0x14` | `u32` | Header format; must be 0 |
| `0x18` | `u8[8]` | Reserved |
| `0x20` | `u32` | Version (6 or 7) |
| `0x24` | `u32` | Total file size |
| `0x28` | `u32` | CRC (not checked) |
| `0x2C` | `u32` | Section table offset, **relative to `0x20`** |
| `0x30` | `u32` | Section count |
| `0x34` | `u32, u32` | Root **type** reference: section, offset |
| `0x3C` | `u32, u32` | Root **object** reference: section, offset |
| `0x44` | `u32` | Type tag (not read) |
| | | Version 7 adds 16 bytes of extra tags (not read) |

Magic values:

| Bytes | Format | Supported |
| --- | --- | --- |
| `b867b0caf86db10f84728c7e5e19001e` | v6, 32-bit, little-endian | Yes |
| `29de6cc0baa4532b25f5b7a5f666e2ee` | v7, 32-bit, little-endian | Yes |
| `e59b495e6f631f141e13eba990beedc4` | v7, 64-bit, little-endian | Yes (8-byte pointers) |
| `b595110e4bb5a56a502828eb04b37825` | v6, big-endian | No |

## Sections

Section table entries are 44 bytes (11 × `u32`):

| Word | Meaning |
| --- | --- |
| 0 | Compression: `0` none, `1` Oodle0, `2` Oodle1 |
| 1 | File offset of section data |
| 2 | Compressed size |
| 3 | Decompressed size |
| 4 | Alignment |
| 5 | Oodle stop 0 (`first16bit`) |
| 6 | Oodle stop 1 (`first8bit`) |
| 7 | Relocation table file offset |
| 8 | Relocation count |
| 9–10 | Mixed-marshalling table offset and count (not needed for little-endian files) |

Relocations are 12 bytes: `u32 source offset` (within this section), `u32 target section`, `u32 target offset`. Every pointer in the file is a relocation: the stored pointer bytes are meaningless, so look the source offset up in the relocation table. A pointer with no relocation is null.

### Oodle1 decompression

Compressed sections use Oodle1: an adaptive arithmetic-coded LZ77 variant. The output is decoded in up to three runs ending at `first16bit`, `first8bit` and the decompressed size. Each run has its own 12-byte parameter block at the start of the compressed data (36 bytes in all), and the arithmetic-coded stream begins at byte 36. Each parameter block sets up the symbol models for literals (four, chosen by output position & 3), match lengths (65), and offsets (low/mid/high). Matches can overlap their own output.

[gr2.py](../../io_scene_mechassault2/formats/gr2.py) (`_decompress_oodle1`, `_ArithModel`, `_ArithBits`) is a complete pure-Python implementation that decodes the game's files. Port it rather than re-deriving the format: the model updates and renormalisation must match exactly or the output goes wrong after a few bytes. The importer decodes Oodle0 sections with the same routine.

## Type tree

A type is an array of member definitions, ended by a member of type 0. Each member is `20 + 3 × pointer size` bytes:

| Offset (32-bit) | Type | Meaning |
| --- | --- | --- |
| `0x00` | `u32` | Member type |
| `0x04` | ptr | Name (C string) |
| `0x08` | ptr | Referenced type (for inline structs and references) |
| `0x0C` | `i32` | Array width (0 = scalar) |
| `0x10` | | Extra fields (not read) |

Member types and their size in the parent struct (*p* = pointer size):

| ID | Kind | Size |
| --- | --- | --- |
| 1 | Inline struct | size of the referenced type |
| 2 | Reference | *p* |
| 3 | Reference to array: `u32 count`, ptr | 4 + *p* |
| 4 | Array of references: `u32 count`, ptr to ptr[] | 4 + *p* |
| 5 | Variant reference: type ptr, object ptr | 2*p* |
| 7 | Reference to variant array: type ptr, `u32 count`, ptr | 2*p* + 4 |
| 8 | String | *p* |
| 9 | Transform | 68 |
| 10 | `f32` | 4 |
| 11 / 12 | `i8` / `u8` | 1 |
| 13 / 14 | `i8` / `u8` normalised | 1 |
| 15 / 16 | `i16` / `u16` | 2 |
| 17 / 18 | `i16` / `u16` normalised | 2 |
| 19 / 20 | `i32` / `u32` | 4 |
| 21 | `f16` | 2 |
| 22 | Empty reference | *p* |

Multiply by the array width when it is above 0. Members are packed with no padding. Read objects **by member name**, because field order and presence vary between Granny versions.

### Transform (68 bytes)

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x00` | `u32` | Flags (which parts are non-identity) |
| `0x04` | `f32[3]` | Position |
| `0x10` | `f32[4]` | Orientation quaternion, **x, y, z, w** |
| `0x20` | `f32[9]` | Scale/shear 3×3, row-major |

The local matrix is `T(position) · R(orientation) · S(scale/shear)` with column vectors.

## Objects MA2 uses

Root (`granny_file_info`) members:

| Member | Meaning |
| --- | --- |
| `ArtToolInfo` | Reference. Its `RightVector`, `UpVector`, `BackVector` (`f32[3]` each) give the file's axis system |
| `Skeletons` | Array of references to skeletons |
| `Animations` | Array of references to animations |
| `TrackGroups` | Array of references to track groups (shared with `Animations`) |

**Skeleton:** `Name`, `LODType`, `Bones` (reference to array of bones).
**Bone:** `Name`, `ParentIndex` (`-1` = root), `Transform` (local rest pose), `InverseWorldTransform` (`f32[16]`).
**Animation:** `Name`, `Duration` (seconds), `TimeStep`, `Oversampling`, `DefaultLoopCount` (0 = loops forever), `Flags`, `TrackGroups`.
**Track group:** `Name`, `TransformTracks` (reference to array), `AccumulationFlags`.
**Transform track:** `Name` (the bone it drives), `Flags`, `OrientationCurve`, `PositionCurve`, `ScaleShearCurve`.

A track replaces its bone's local transform; tracks with no curve for a channel keep the rest value. Match track names to bones case-insensitively.

### Axis systems

MA2 assets were exported from 3ds Max, so `ArtToolInfo` usually says right `+X`, up `+Z`, back `−Y`: Granny data is **Z-up**, unlike the Y-up model and map files. Convert with the basis change in [Coordinate systems](../coordinates.md#granny2-files). Files without `ArtToolInfo` are taken to be in MA2 game space already. A clip uses its own `ArtToolInfo` if present, else its skeleton's.

## Curves

Each curve member is either a **legacy inline curve** (`Degree`, `Knots[]`, `Controls[]` of `f32`; one knot means a constant) or a **variant reference** to a `CurveData*` struct. For variants, the codec is the name of the referenced type's first member, minus the `CurveDataHeader_` prefix. That first member is an inline header with `Format` and `Degree`.

The codec names follow the pattern `D<dim>[n][I<k>]K<knot type>C<control type>`:

| Codec | Dim | Contents |
| --- | --- | --- |
| `DaIdentity` | any | Identity: zero position, unit quaternion, identity scale. `Dimension` member gives *dim* |
| `DaConstant32f`, `D3Constant32f`, `D4Constant32f` | any | `Controls` holds one value |
| `DaK32fC32f`, `DaKeyframes32f` | any | `Knots[]` `f32`, `Controls[]` `f32` (dim = controls / knots) |
| `DaK16uC16u`, `DaK8uC8u` | any | Quantised (below), per-component `ControlScales[]`, `ControlOffsets[]` |
| `D3K16uC16u`, `D3K8uC8u` | 3 | Quantised 3D position |
| `D3I1K16uC16u`, `D3I1K8uC8u`, `D3I1K32fC32f` | 3 | One scalar per key, times a per-axis scale |
| `D4nK16uC15u`, `D4nK8uC7u` | 4 | Packed normalised quaternion (below) |
| `D9I1K16uC16u`, `D9I1K8uC8u` | 9 | Uniform scale: one scalar per key, times a per-axis scale |
| `D9I3K16uC16u`, `D9I3K8uC8u` | 9 | Diagonal scale: three values per key |

### Quantised curves

These store one `KnotsControls` array (`u16` or `u8`): all knots first, then all controls.

- Knot count = array length / (values per key + 1) (e.g. ÷4 for `D3`, ÷2 for `I1`).
- Knot time = `value / OneOverKnotScale`. For `D3`/`D9` codecs, `OneOverKnotScaleTrunc` is a `u16` holding the **top 16 bits of an `f32`**: `f32_from_bits(trunc << 16)`.
- Control component *j* = `ControlOffsets[j] + ControlScales[j] * value`.
- `D9` controls expand to a diagonal 3×3: `(sx,0,0, 0,sy,0, 0,0,sz)`.

### `D4n` quaternions

Three stored components per key (`u16` for 16u/15u, `u8` for 8u/7u); the fourth is rebuilt from unit length.

```
mask   = 0x7FFF (u16) or 0x7F (u8);  qmax = 32767 or 127
sign   = c0 & 0x8000 (u16) or 0x80 (u8)          # sign of the missing component
missing = ((c1 >> 14) & 2) | (c2 >> 15)           # u16; for u8 use >> 6 and >> 7
scales/offsets: for i in 0..3, entry = (ScaleOffsetTableEntries >> (4*i)) & 0xF
    scale[i]  = TABLE[entry].scale / qmax
    offset[i] = TABLE[entry].offset
dst = missing
for c in (c0, c1, c2):
    dst = (dst + 1) & 3
    q[dst] = offset[dst] + scale[dst] * (c & mask)
q[missing] = ±sqrt(max(0, 1 - sum of squares of the other three))
```

`TABLE` is the 16-entry scale/offset table in `_QUAT_SCALE_OFFSET_TABLE` (multiples of 1/√2). The resulting quaternion is in **x, y, z, w** order.

### Evaluation

Curves are B-splines of the stored degree, evaluated as follows:

- Find `k` = the first knot index with `knot[k] > t`, or the last index if there is none.
- Degree 0: value = `control[k]` (stepped).
- Degree ≥ 1: de Boor over controls `k-degree … k`. At step `r` (1…degree), for `j` from `degree` down to `r`: `α = (t - knot[k-degree+j-1]) / (knot[k+j-r] - knot[k-degree+j-1])`, clamped to 0–1, and `d[j] = (1-α)·d[j-1] + α·d[j]`. Indices outside the arrays clamp to the first or last element.
- Renormalise 4D results (quaternions).

Degree 1 is linear interpolation between keys. Degree 0 and 1 curves can be keyed exactly at their knots; higher degrees need sampling (the importer bakes at the scene frame rate, usually 30 fps).

### Looping

For clips that loop (`DefaultLoopCount` 0, and the importer also requires every curve to end within 0.1 of where it starts), the exporter keys frames 0…N inclusive, so the last knot (at `Duration`) duplicates the first. To evaluate seamlessly across the seam, drop that last key, then extend the knot and control arrays periodically by `degree + 2` keys on each side (shifting knots by ±`Duration`).

## `.anm` animation sets

An `.anm` lists the clips available to a model. The importer accepts two shapes:

- **XML:** any attribute whose value ends in `.gr2` is a clip path (relative to the `.anm`); the element's `name` attribute, if any, names the clip.
- **Anything else:** every token matching `<path>.gr2` is a clip, named by its file stem.

Clip lists can also live in legacy [`.node` `[animation…]` sections](maxport-text.md#node-hierarchy), which add `synch_point`, `loop_count` and `primary`.
