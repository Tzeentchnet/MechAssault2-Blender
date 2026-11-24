# Xbox disc images (XDVDFS)

**Files:** `*.iso` (full Redump dumps), `*.xiso.iso` / `*.xiso` (trimmed game partition)
**Reference:** [formats/xiso.py](../../io_scene_mechassault2/formats/xiso.py)

MechAssault 2 ships its data as a set of [MGF archives](mgf.md) on an Xbox DVD. A tool does not need to extract the image: find each `.mgf`'s byte range inside the image and read the archive from there.

## Sectors and the game partition

- Sector size: **2048 bytes**.
- The XDVDFS volume descriptor is at sector 32 (`0x10000`) **from the start of the game partition**.
- A trimmed XISO has the game partition at offset 0. A full Redump image starts with a video partition, so the game partition begins later.

Probe these partition base offsets in order and take the first that holds a valid descriptor:

| Base offset | Image kind |
| --- | --- |
| `0x00000000` | Trimmed XISO |
| `0x18300000` | Redump XGD1 (the usual case for MA2) |
| `0x0FD90000` | XGD2 |
| `0x02080000` | XGD3 |

All sector numbers below are relative to this partition base: `absolute = base + sector * 2048`.

## Volume descriptor (sector 32, 2048 bytes)

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x000` | `char[20]` | `MICROSOFT*XBOX*MEDIA` |
| `0x014` | `u32` | Root directory table sector |
| `0x018` | `u32` | Root directory table size in bytes |
| `0x01C` | ... | Not read (timestamp and padding) |
| `0x7EC` | `char[20]` | `MICROSOFT*XBOX*MEDIA` again |

Both magic strings must match.

## Directory tables

A directory table is a binary search tree of entries packed into `size` bytes starting at its sector. Walk it from the entry at offset 0.

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x00` | `u16` | Left subtree offset, in **4-byte units** (0 or `0xFFFF` = none) |
| `0x02` | `u16` | Right subtree offset, in 4-byte units (0 or `0xFFFF` = none) |
| `0x04` | `u32` | Start sector of the file data or of the subdirectory's table |
| `0x08` | `u32` | Size in bytes |
| `0x0C` | `u8` | Attributes; `0x10` = directory |
| `0x0D` | `u8` | Name length |
| `0x0E` | `char[n]` | Name (no terminator) |

Notes for implementers:

- Track visited offsets: the tree is untrusted input and a cycle would loop forever.
- Skip entries with a zero name length (padding) and `.` / `..`.
- A directory's `sector`/`size` point at its own table; recurse into it.
- File data is contiguous: `[base + sector * 2048, + size)`.

## What MA2 keeps on disc

Only the `*.mgf` files matter for asset extraction. `common.mgf` lists the maps (see [Maps](maps.md#finding-maps)); each map also has its own resource archive. The importer opens every `.mgf` it finds in the tree as a slice of the ISO file (`base_offset`, `slice_size`) and never copies it out.
