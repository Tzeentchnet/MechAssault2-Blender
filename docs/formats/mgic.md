# MGIc container

**Used by:** textures (`.tif` and heightfield/culture blobs inside WDF files) and geometry (`.mgmodel{...}` sub-entries, `.mesh` vertex/face files)
**Reference:** [formats/mgic_texture.py](../../io_scene_mechassault2/formats/mgic_texture.py), [formats/geometry.py](../../io_scene_mechassault2/formats/geometry.py)

MGIc is a chunked binary container. Despite the `.tif` extension, MA2 textures are MGIc files, not TIFF.

## File header (32 bytes)

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x00` | `u32` | Unknown |
| `0x04` | `char[4]` | `"MGIc"` |
| `0x08` | `u32` | Unknown |
| `0x0C` | `u32` | Section count (textures read this; geometry does not need it) |
| `0x10` | `u8[16]` | Unknown |

## Sections

Sections follow the header back to back from offset `0x20`. Each has a 16-byte header:

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x0` | `char[4]` | Section tag |
| `0x4` | `u32` | Unknown |
| `0x8` | `u32` | Section length **including** this 16-byte header |
| `0xC` | `u32` | Chunk count (set in texture sections; geometry files may leave it 0) |

The section body is a run of chunks.

## Chunks

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x0` | `char[4]` | Chunk tag |
| `0x4` | `u32` | Chunk length **including** this 8-byte header |
| `0x8` | ... | Payload (`length - 8` bytes) |

Single-value chunks hold one `u32`. Walk chunks until the section end; stop on a length below 8 or one that would overrun the section.

## Known sections

| Tag | Found in | Contents |
| --- | --- | --- |
| `"PIC "` | Textures | Picture header chunks and the pixel payload: see [MGIc textures](mgic-texture.md) |
| `"PAL "` | Textures | Palette for paletted (`P8`) pictures |
| `"XMLB"` | Geometry | Optional metadata block, always first when present; skip it |
| `"VERS"` | Vertex files | Vertex buffer: see [MGIc geometry](mgic-geometry.md#vers-vertex-buffers) |
| `"INDX"` | Index files | Index buffer: see [MGIc geometry](mgic-geometry.md#indx-index-buffers) |
