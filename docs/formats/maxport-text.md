# MAXPort text models (`.node`, `.mesh`, `.mat`, `.road`)

**Files:** `*.node`, `*.mesh`, `*.mat`, `*.road`, and `Multiplay/*.ini`
**Reference:** [formats/model.py](../../io_scene_mechassault2/formats/model.py), [formats/text_utils.py](../../io_scene_mechassault2/formats/text_utils.py)

The legacy model format is a set of INI-style text files written by the game's 3ds Max exporter. A `.node` file holds the hierarchy, each `.mesh` names one mesh's geometry and material, and each `.mat` is one material. Geometry is the same [MGIc `VERS`/`INDX`](mgic-geometry.md) data that MGMODEL uses, stored as ordinary files.

## INI dialect

- Decode as Latin-1 and strip trailing NULs.
- `[section]` headers. Section names repeat freely, and **order matters**.
- `key = value`. Keys repeat (`mesh=`, `bone=`, `child=`); keep every occurrence in order.
- `;` starts a comment, except inside double quotes. Surrounding quotes are removed from values.
- A line with no `=` continues the previous value (joined with a space).
- Keys are case-insensitive.

Colours, vectors, angles (radians), bools and paths follow the same conventions as [MGMODEL](mgmodel.md#xml-dialect).

## `.node`: hierarchy

Sections are written in **depth-first pre-order**. Each node section's name is the node name; its children follow it immediately.

| Key | Default | Meaning |
| --- | --- | --- |
| `type` | `NODE` | Node type (`NODE`, `ANIMNODE`, …) |
| `position`, `rot_axis`, `rot_angle`, `scale` | identity | Local transform, same maths as [MGMODEL](mgmodel.md#local-matrix) |
| `turned_on` | `true` | Default visibility |
| `mesh` (repeated) | | Path to a `.mesh` file to attach here |
| `bone` (repeated) | | Skin bone list for this node |
| `num_children` | `0` | Number of child nodes; that many subtrees follow |

Section-level rules:

- `[fileinfo]` is metadata; skip it.
- A section whose name starts with `animation` and that has an `animation=` key is an animation entry, not a node:

  | Key | Meaning |
  | --- | --- |
  | `animation` | Path to a `.gr2` |
  | `name` | Clip name (defaults to the file stem) |
  | `synch_point` | Sync point |
  | `loop_count` | Loop count (0 = forever) |
  | `primary` | Primary clip flag |

- Several top-level node trees can follow one another in the same file.

### `ANIMNODE` redirects

A node with `type=ANIMNODE` holds no meshes of its own. It points elsewhere:

| Key | Meaning |
| --- | --- |
| `skeleton` | [Granny2](gr2.md) skeleton |
| `child` (repeated) | Path to another `.node` or `.mgmodel` loaded as a subtree of this node |

`child` paths go through the usual [model fallbacks](mgf.md#path-resolution) (`_low`, `.node` ↔ `.mgmodel`). Redirects can nest; the importer caps the depth at 8.

The `.mgmodel` an `ANIMNODE` points at usually names no skeleton itself: shipped mechs keep their geometry in `<Mech>/Model/<mech>.mgmodel` and their rig only in the `ANIMNODE` beside it, `<Mech>/<mech>.NODE`, which also lists the `[animationN]` clips. One geometry file can have several wrappers (`novacat.NODE` for gameplay and `novacat_cinema_3_1a.NODE` / `_3_5.NODE` for cutscenes, each with its own clip list).

## `.mesh`

One `[mesh]` section:

| Key | Meaning |
| --- | --- |
| `name` | Mesh name (defaults to the file stem) |
| `vertices` | Path to the `VERS` file |
| `faces` | Path to the `INDX` file |
| `material` | Path to a `.mat` file |
| `type` | `indexedstrip` or a triangle list |
| `bone` (repeated) | Skin bone list; overrides the node's |

## `.mat`

One `[material]` section. Keys mirror MGMODEL's attributes with a few renames:

| `.mat` key | MGMODEL equivalent |
| --- | --- |
| `type` (e.g. `BASE`, `WATER`) | element tag |
| `diff_color` | `diffuse` |
| `selfillum_color` | `selfillum` |
| `base`, `detail`, `multi`, `waves`, `unique` | `basetexture`, `detailtexture`, `multitexture`, `wavetexture`, `uniquetexture` slots |
| `<slot>_tiling` | the slot's `tiling` |

`opacity`, `two_sided`, `shading`, `blending`, `alpha_kill`, `spec_color`, `shin_strength`, `shininess`, the `colorshift*` keys, `*_bloom`, and (for `type=water`) `deep_color`, `shallow_color`, `fresnel_min` and `fresnel_max` keep the same names. Several meshes can share one `.mat`.

## `.road`: road segment definition

Referenced by `MARoad` entities in [maps](maps.md#entities).

| Key | Default | Meaning |
| --- | --- | --- |
| `model` | | Road segment model (relative path) |
| `point1` | `13 0 0` | One corner of the segment's footprint |
| `point2` | `-13 0 -30` | Opposite corner |

The footprint is the XZ box spanned by the two points.

## `Multiplay/*.ini`: multiplayer map descriptors

See [Maps](maps.md#finding-maps).
