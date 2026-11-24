# Features

What the `io_scene_mechassault2` Blender extension imports, and the modules that do it. For the file layouts behind each feature, see the [format notes](README.md).

- **Xbox ISO & MGF Archive Streaming (`formats/xiso.py`, `formats/mgf.py`, `formats/index_cache.py`)**:
  - Reads Xbox XDVDFS disc images (trimmed `.xiso.iso` and Redump `.iso`) directly in-place with zero-copy MGF slicing — no 2+ GB ISO extraction required.
  - Browser categories for Maps, Mechs, Models, Animations and Textures. Mechs shows one entry per mech variant (its rigged `.NODE`, plus unwrapped extras like explosion debris and powered-down set pieces), labelled `[Class] Mech (variant)`; the `.mgmodel` geometry a `.NODE` wraps is left out as a duplicate. Models entries that share a file name get a folder or `NODE` suffix.
  - Persistent disk-cached asset/map index (`index_<hash>.json` + `state.json` in Blender's user data directory) automatically restores the browser source, category, and indexed asset lists across Blender `File → New` resets and full restarts.
  - Reads MechAssault 2 v4 (plain & zlib block-compressed `0xC5735A0F`) and MechAssault 1 v3 `.mgf` archives directly in Blender.
- **Models (`formats/model.py`, `importers/model_builder.py`)**:
  - `.MGMODEL` XML and legacy MAXPort `.NODE` / `.mesh` / `.mat` text sets (including `ANIMNODE` redirects).
  - An `.mgmodel` imported on its own still gets its rig: when it names no skeleton, the importer finds the `ANIMNODE` `.NODE` that wraps it (in its own folder or the parent) and takes that file's skeleton and animation clips. With several wrappers, the one named like the model wins, then the one with the most clips.
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
