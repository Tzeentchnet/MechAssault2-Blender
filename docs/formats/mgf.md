# MGF archives

**Files:** `*.mgf`, MechAssault 2 (v4) and MechAssault (v3)
**Reference:** [formats/mgf.py](../../io_scene_mechassault2/formats/mgf.py)

An MGF is a flat package of files with a directory tree and a string table. MA2 ships both plain archives and zlib block-compressed ones. Inside, every offset uses the **logical** (uncompressed) address space.

## Detecting the wrapper

Read the first 32 bytes:

- If bytes 0–3 are `"mgf "` (`u32` `0x2066676D`), the file is a **plain** archive and logical address = file offset.
- Else, if the `u32` at offset 4 is `0xC5735A0F`, it is a **compressed** archive (below).
- Otherwise it is not an MGF.

## Compressed wrapper

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x00` | `u32` | Unknown (`1` in files written by the test fixtures) |
| `0x04` | `u32` | `0xC5735A0F` |
| `0x08` | `u32` | Block count |
| `0x0C` | `u32` | Logical (uncompressed) size |
| `0x10` | `u32` | Unknown |
| `0x14` | `u32` | Block size (uncompressed bytes per block) |
| `0x18` | `u32[2]` | Unknown |
| `0x20` | `block[count]` | Block table, 12 bytes each |

Block table entry:

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x0` | `u32` | File offset of the compressed block |
| `0x4` | `u32` | Compressed length |
| `0x8` | `u32` | Checksum (not verified by the importer) |

Each block is a complete zlib stream that inflates to `block_size` bytes (the last one may be shorter). To read logical range `[a, a+n)`, inflate blocks `a / block_size` onward and slice. Caching a few recently inflated blocks makes directory parsing cheap.

## Archive header (logical offset 0, 44 bytes)

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x00` | `char[4]` | `"mgf "` |
| `0x04` | `u8` | Version: `4` = MechAssault 2, `3` = MechAssault |
| `0x05` | `u8[3]` | Unknown |
| `0x08` | `u32` | Unknown |
| `0x0C` | `u32` | `nf`: file record count |
| `0x10` | `u32` | File record table length (`nf * 32` for v4, `nf * 28` for v3) |
| `0x14` | `u32` | File record table offset |
| `0x18` | `u32` | `nd`: directory row count |
| `0x1C` | `u32` | Directory row table length (`nd * 24`) |
| `0x20` | `u32` | Directory row table offset |
| `0x24` | `u32` | String table length |
| `0x28` | `u32` | String table offset |

The length fields are redundant with the counts; a mismatch means the file is damaged or not an MGF.

## File records

The importer reads only offset and length; the other words are unknown.

| Version | Size | Data offset | Data length |
| --- | --- | --- | --- |
| v4 | 8 × `u32` | word 6 | word 3 |
| v3 | 7 × `u32` | word 6 | word 5 |

Both are logical offsets.

## Directory rows (6 × `i32`)

Rows describe both folders and files as a tree. Full paths come from walking the parent links.

| Word | Meaning |
| --- | --- |
| 0 | Next sibling row, `-1` for none (not read by the importer) |
| 1 | **Parent row**, `-1` at the root |
| 2 | First child row, `-1` for none (not read) |
| 3 | Unknown (possibly a name hash) |
| 4 | **Name**: offset into the string table (NUL-terminated) |
| 5 | **File record index**, or `-1` for a folder |

To list files: for every row with word 5 ≥ 0, follow word 1 up to the root, collecting names, and join them with `/`. Guard against cycles (the importer stops after 256 levels).

## Sub-entries: `name{part}`

Some files are containers whose parts are stored as separate archive entries named `<file>{<part>}`. A `.mgmodel`'s geometry, for example, is stored as entries named `<model>.mgmodel{<vertices name>}` and `<model>.mgmodel{<indices name>}` beside the XML itself. The name inside the braces is whatever the parent file references (see [MGMODEL](mgmodel.md#meshxml)). When taking an extension from such a path, drop the `{...}` suffix first.

## Path resolution

All lookups the importer makes (from models, maps and templates) follow these rules:

- Convert `\` to `/`, drop empty and `.` segments, resolve `..`, and remove any leading `/`.
- Compare paths case-insensitively. Report the archive's own spelling (the "canonical" path).
- A reference inside a file is relative to **that file's folder**; a leading `/` or `\` means the archive root.
- Model references fall back, in order, to `<stem>_low.<ext>`, then the other model extension (`.node` ↔ `.mgmodel`), then its `_low` variant.
- Texture references that do not resolve are retried from the first `texture_dump/` or `envmaps/` component in the path.

## Opening a whole game

Asset references cross archives, so a tool needs every MGF open at once with a defined priority (the first archive that holds a path wins):

1. The map's own resource archive, when loading a map.
2. `Base*.mgf`
3. `common.mgf`, `Templates.mgf`
4. Every other `*.mgf`, sorted by name.

Loose extracted files on disk, if present, take priority over all archives.
