# MechAssault 2 Importer for Blender 5.2

A zero-external-dependency **Blender 5.2 Extension** (`io_scene_mechassault2`) for importing **MechAssault 2: Lone Wolf** models, materials, textures, skeletons, animations, and full maps directly from Xbox `.iso` / `.xiso.iso` disc images, `.mgf` archives, or extracted files on disk.

## Documentation

- [Features](docs/features.md): what the extension imports.
- [File format notes](docs/README.md): layouts of the MechAssault 2 formats it reads (XISO, MGF, MGIc textures and geometry, MGMODEL, WDF/MTB, maps, Granny2), written for people building their own tools.

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
  - **Mechs** lists each mech and power armor variant once (`[Heavy] NovaCat`, `[Heavy] NovaCat (low)`, `[Heavy] NovaCat (cinema_3_5)`, …), choosing the rigged `.NODE` entry point over the bare geometry it wraps. **Models** holds everything else; entries sharing a file name are labelled by folder or `NODE`.

## Coordinate System

MechAssault 2 uses left-handed Y-up coordinates (metres), while Blender uses right-handed Z-up coordinates. The importer applies a symmetric basis reflection swapping Y and Z (`(x, y, z) → (x, z, y)`) together with triangle winding reversal (`(i0, i1, i2) → (i0, i2, i1)`), `C · M · C` transform conjugation, and UV V-flipping (`1.0 - v`). See [docs/coordinates.md](docs/coordinates.md) for the full conversion, including Granny2 files.

## Credits & References

- [MGF-Explorer](https://github.com/larvalextract/mgf-explorer) by LarvalExtract — MGF, MGIc, WDF, model, and shader format research.
- [MechAssault 2 PC Recomp](https://github.com/Mazillion/MechAssault2-PC-Recomp) — `ma2models`, scenario editor, terrain, culture scatter, and shader documentation.
