# MechAssault 2 Importer for Blender 5.2

A zero-external-dependency **Blender 5.2 Extension** (`io_scene_mechassault2`) for importing **MechAssault 2: Lone Wolf** models, materials, textures, skeletons, animations, and full maps directly from Xbox `.iso` / `.xiso.iso` disc images, `.mgf` archives, or extracted files on disk.

## Features

- **Xbox ISO & MGF Archive Streaming (`formats/xiso.py`, `formats/mgf.py`, `formats/index_cache.py`)**:
  - Reads Xbox XDVDFS disc images (trimmed `.xiso.iso` and Redump `.iso`) directly in-place with zero-copy MGF slicing — no 2+ GB ISO extraction required.
  - Persistent disk-cached asset/map index (`index_<hash>.json` + `state.json` in Blender's user data directory) automatically restores the browser source, category, and indexed asset lists across Blender `File → New` resets and full restarts.
  - Reads MechAssault 2 v4 (plain & zlib block-compressed `0xC5735A0F`) and MechAssault 1 v3 `.mgf` archives directly in Blender.
- **Models (`formats/model.py`, `importers/model_builder.py`)**:
  - `.MGMODEL` XML and legacy MAXPort `.NODE` / `.mesh` / `.mat` text sets (including `ANIMNODE` redirects).
  - Full `MGIc` `VERS`/`INDX` geometry decoding: positions, float3 & `NORMPACKED3` (11:11:10) custom split normals, `UVMap` & `UVMap_1` (short2 `/1024` and float2), `D3DCOLOR` vertex colors, 4-bone skin weights (`0x80000`), and `0xFFFF`-restarted triangle strips.
  - `.mtb` damage/defence/heat-sink rules (`MAModSubObjectShow`, `MAModSubObjectState`, `MAModMechHeatSink`), LOD0 filtering, and `node_light` point/spot lights.
- **Materials & MGIc Textures (`formats/mgic_texture.py`, `importers/texture_builder.py`, `importers/material_builder.py`)**:
  - Decodes `DXT1`, `DXT3`, `DXT5`, `A8R8G8B8`, `R5G6B5`, `A4R4G4B4`, `L8`/`P8`, `A8`, `A8L8`, Xbox Morton swizzled textures, cubemaps (projected to equirectangular environment maps), and animated frame sequences.
  - Builds Blender 5.2 `Principled BSDF` & additive/subtractive/water node graphs for `material_base`, `material_complex` (`multitexture` specular/colorshift/detail masks), `material_detail`, `material_env`, `material_color_shift`, `material_specular_mask`, `material_water`, and `material_solid`.
- **Skeletons & Animations (`formats/gr2.py`, `importers/animation_builder.py`)**:
  - Pure-Python Granny2 (`.gr2`) v6/v7 parser with built-in Oodle1 decompression, type-tree reflection, and B-spline curve evaluation (`DaK32fC32f`, `D3K16uC16u`, `D4nK16uC15u`, `D4nK8uC7u`, etc.).
  - Builds Blender Armatures and **Blender 5.2 Slotted Actions** (`layers`/`strips`/`channelbag` via `action.slots` and `fcurve_ensure_for_datablock`).
  - NumPy-vectorized curve evaluation and transform baking: linear/stepped Granny curves are keyed exactly at their knots (`LINEAR`/`CONSTANT`), higher-degree B-splines are baked per frame; F-curves are grouped per bone, and each action gets a manual frame range (cyclic when `DefaultLoopCount` is 0) with the scene FPS/frame range matched to the clip.
- **Maps (`formats/map_wdf.py`, `importers/map_builder.py`)**:
  - Imports `MATerrain` 8-bit heightfields and multi-layer terrain shaders (`TerrainBaseMap`, `TerrainDetailMap`/`1`, `LightMapName`, and PC recomp `MA2L` `terrain_layers.bin`).
  - Places buildings, props, roads (`MARoad` / `.road`), water (`MAWater`), units & spawns, `MASky2` sky domes, `MAPointLight`s, `MAEnvironment` sun/world lighting, and deterministic `MATerrainInfo` culture scatter (`MT19937`).

## Installation (Blender 5.2)

1. Package the extension with Blender:
   ```bash
   blender --command extension build --source-dir io_scene_mechassault2 --output-dir dist
   ```
   (Or zip the `io_scene_mechassault2/` directory directly.)
2. In Blender 5.2: **Edit → Preferences → Get Extensions → Install from Disk...** and select the built `.zip`.
3. (Optional) Set your **MechAssault 2 Source (Folder / .mgf / .iso)** in the extension preferences so loose `.mgmodel`, `.node`, or `.wdf` imports automatically resolve shared textures, templates, and archives.

## Usage

- **File → Import**:
  - *MechAssault 2 Model (`.mgmodel` / `.node`)*
  - *MechAssault 2 Animation / Skeleton (`.gr2` / `.anm`)*
  - *MechAssault 2 Map (`.wdf`)*
  - *MechAssault 2 Texture (`.tif`)*
- **Drag & Drop**: Drop `.mgmodel`, `.node`, `.gr2`, `.anm`, or `.wdf` files directly onto the 3D Viewport.
- **3D Viewport Sidebar (`N` → `MechAssault 2`)**: Point `Source` at an Xbox `.xiso.iso`/`.iso`, a `game_files` folder, or any `.mgf` archive, click **Scan / Index**, filter by name, and click **Import Selected**. Scans are cached on disk and persist across Blender resets (`File → New`) and restarts; use the refresh button to force re-indexing.

## Coordinate System

MechAssault 2 uses left-handed Y-up coordinates (metres), while Blender uses right-handed Z-up coordinates. The importer applies a symmetric basis reflection swapping Y and Z (`(x, y, z) → (x, z, y)`) together with triangle winding reversal (`(i0, i1, i2) → (i0, i2, i1)`), `C · M · C` transform conjugation, and UV V-flipping (`1.0 - v`).

## Credits & References

- [MGF-Explorer](https://github.com/larvalextract/mgf-explorer) by LarvalExtract — MGF, MGIc, WDF, model, and shader format research.
- [MechAssault 2 PC Recomp](https://github.com/Mazillion/MechAssault2-PC-Recomp) — `ma2models`, scenario editor, terrain, culture scatter, and shader documentation.
