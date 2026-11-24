# MechAssault 2 File Format Notes

These pages describe the **MechAssault 2: Lone Wolf** (Xbox) data formats that the `io_scene_mechassault2` Blender extension reads. They are written for people building their own tools (viewers, extractors, converters, mod tools), so they explain what is in the files and how to interpret it, not how to drive Blender.

Each page notes what the importer actually relies on. Fields marked *unknown* or *not read* are ones the importer skips; treat any meaning given for them as a guess.

## Contents

| Page | Covers |
| --- | --- |
| [Features](features.md) | What the Blender extension imports (moved from the top-level README) |
| [Xbox disc images (XDVDFS)](formats/xiso.md) | `.iso` / `.xiso.iso` game partition and directory tree |
| [MGF archives](formats/mgf.md) | `.mgf` v3 (MA1) and v4 (MA2), zlib block compression, lookup priority |
| [MGIc container](formats/mgic.md) | The chunked binary container shared by textures and geometry |
| [MGIc textures](formats/mgic-texture.md) | `.tif` pictures: pixel formats, DXT, swizzle, mips, cubemaps, palettes |
| [MGIc geometry](formats/mgic-geometry.md) | `VERS` vertex buffers and `INDX` index buffers |
| [MGMODEL models](formats/mgmodel.md) | `.mgmodel` XML: materials, node hierarchy, meshes, lights |
| [MAXPort text models](formats/maxport-text.md) | Legacy `.node` / `.mesh` / `.mat` INI sets and `.road` |
| [WDF object files](formats/wdf.md) | Binary property-bag format behind `.wdf` and `.mtb` |
| [MTB rules](formats/mtb.md) | Per-model damage visibility, state and heat-sink rules |
| [Maps](formats/maps.md) | World `.wdf` contents: terrain, environment, sky, entities, culture scatter |
| [Granny2 animation](formats/gr2.md) | `.gr2` skeletons and animations, Oodle1, curve codecs, `.anm` sets |
| [Coordinate systems](coordinates.md) | Axis conventions, matrix layout, and how to convert to right-handed Z-up |

## Conventions used throughout

- **Byte order:** all binary formats are little-endian.
- **Offsets** are in bytes, written in hex (`0x20`) or decimal.
- **Type names:** `u8`/`u16`/`u32` unsigned, `i8`/`i16`/`i32` signed, `f32` IEEE float, `char[4]` a four-byte ASCII tag.
- **Tags** such as `"PIC "` and `"hgt "` include their trailing space.
- **Text encoding:** names and text files are read as Latin-1. Text files may end in NUL padding, which should be stripped.
- **Asset paths** use `/` (the game writes `\` in places), are matched case-insensitively, and are relative to the archive root. See [Path resolution](formats/mgf.md#path-resolution).

## Reference implementation

The parsers in [io_scene_mechassault2/formats/](../io_scene_mechassault2/formats/) are pure Python plus NumPy, with no Blender dependency, so they can be imported from any script:

```python
from io_scene_mechassault2.formats.mgf import open_game_archives, AssetSource
from io_scene_mechassault2.formats.model import load_model

archives = open_game_archives("/path/to/MechAssault2.xiso.iso")
source = AssetSource(archives)
print(source.list_all(".mgmodel")[:10])   # browse what is available
model = load_model(source, "<path from the list>.mgmodel")
```

The unit tests in [tests/test_fixtures.py](../tests/test_fixtures.py) build small synthetic files of every format. They are a useful starting point for writing files.

## Credits

The format knowledge here builds on:

- [MGF-Explorer](https://github.com/larvalextract/mgf-explorer) by LarvalExtract: MGF, MGIc, WDF, model and shader research.
- [MechAssault 2 PC Recomp](https://github.com/Mazillion/MechAssault2-PC-Recomp): `ma2models`, scenario editor, terrain, culture scatter and shader documentation.
