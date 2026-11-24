# MGMODEL models

**Files:** `*.mgmodel` (XML), plus `*.mgmodel{...}` geometry sub-entries
**Reference:** [formats/model.py](../../io_scene_mechassault2/formats/model.py), [formats/text_utils.py](../../io_scene_mechassault2/formats/text_utils.py)

An MGMODEL is an XML document describing materials, meshes and a node hierarchy. Vertex and index data live in separate [MGIc geometry](mgic-geometry.md) entries that sit next to the XML in the archive as `<model>.mgmodel{<name>}` (see [sub-entries](mgf.md#sub-entries-namepart)).

Older content uses the [MAXPort text format](maxport-text.md) (`.node`) instead. Both describe the same data, and the two can reference each other.

## XML dialect

The files are XML-like but not always well-formed. A tolerant reader should:

- strip trailing NUL bytes and decode as Latin-1;
- skip `<?...?>`, `<!--...-->` and `<!...>`;
- accept unquoted attribute values and attributes without a value;
- match tag and attribute names case-insensitively;
- decode only the basic entities (`&amp; &lt; &gt; &quot; &apos;`, `&#N;`, `&#xN;`).

Value conventions:

| Kind | Format | Example |
| --- | --- | --- |
| Colour | `"r,g,b"` with 0–255 channels | `diffuse="255,255,255"` |
| Vector | three numbers, any separator | `position="0 1.5 -2"` |
| Angle | radians | `rot_angle="1.5708"` |
| Bool | `true/false`, `yes/no`, `1/0`, `on/off` | `two_sided="true"` |
| Path | relative to this file's folder, or archive-root with a leading `/` | `filename="../textures/hull.tif"` |

## Document structure

The root element's direct children are of three kinds, in any order:

```xml
<root>
  <material_base name="hull" diffuse="255,255,255" ...>
    <basetexture filename="hull.tif" tiling="u_mirror"/>
  </material_base>
  <meshxml name="hull_mesh" vertices="hull_v" indices="hull_i" material="hull" type="indexedstrip"/>
  <node_xxx name="root" position="0 0 0" rot_axis="1 0 0" rot_angle="0" scale="1 1 1">
    <mesh name="hull_mesh"/>
    <children>
      <node_xxx .../>
    </children>
  </node_xxx>
</root>
```

(The element and attribute names are real; the values are illustrative.)

## Materials

Any root child whose tag starts with `material` defines a material. **The tag is the material type** (`material_base`, `material_complex`, `material_detail`, `material_env`, `material_color_shift`, `material_specular_mask`, `material_water`, `material_solid`, `material_distort`, `material_animated`, …).

| Attribute | Default | Meaning |
| --- | --- | --- |
| `name` | | Referenced by `meshxml material=` (case-insensitive) |
| `diffuse` | `255,255,255` | Diffuse tint |
| `selfillum` | `0,0,0` | Emissive colour |
| `opacity` | `1` | Below `0.999` forces alpha blending |
| `two_sided` | `false` | Disable back-face culling |
| `shading` | | e.g. `gouraud`, `translucent` |
| `blending` | `normal` | `normal`, `alpha`, `additive`, `subtractive` |
| `alpha_kill` | `-1` | Above 0 enables alpha testing |
| `reflectivity` | `0` | Environment map strength |
| `envmaptype` | | `masked` = environment map masked by a texture |
| `spec_color` | `0,0,0` | Specular colour |
| `shin_strength` | `1` | Specular strength |
| `shininess` | `16` | Specular exponent |
| `colorshift0_color`, `colorshift1_color` | `0,0,0` | Team/camo colour-shift colours |
| `colorshift1_diffuse` | `false` | Colour-shift 1 also replaces diffuse |
| `colorshift0_bloom`, `colorshift1_bloom`, `specular_bloom` | `-1` | Bloom amounts (`-1` = unset) |
| `blend` / `detail_blend` | | Detail texture blend mode |
| `blend_level` / `detail_blend_level` | `-1` | Detail blend amount |
| `deep_color`, `shallow_color` | `0,0,0` | Water only (tag contains `water`) |
| `fresnel_min`, `fresnel_max` | `0.77`, `1.0` | Water only |

### Texture slots

Each child element of a material with a `filename` attribute is a texture. **The child tag is the slot**: `basetexture` (diffuse), `detailtexture`, `multitexture` (packed masks in `material_complex`), `wavetexture`, `uniquetexture`, and others. `tiling` holds flags such as `u_mirror` and `v_mirror`. A missing file may be an [animated frame sequence](mgic-texture.md#animated-textures).

### Alpha mode

The importer derives one alpha mode per material:

1. `blending="alpha"` → blended; `additive` → additive; `subtractive` → subtractive.
2. Otherwise opaque, upgraded to **alpha-tested** when the base texture has alpha and either the type is base/detail/animated, `shading="translucent"`, or `alpha_kill > 0`.
3. `opacity < 0.999` → blended.

## `meshxml`

Declares a mesh once so nodes can instance it.

| Attribute | Meaning |
| --- | --- |
| `name` | Referenced by `<mesh name=>` inside nodes |
| `vertices` | Sub-entry name of the `VERS` file: read `<this file>{<vertices>}` |
| `indices` | Sub-entry name of the `INDX` file: read `<this file>{<indices>}` |
| `material` | Material name |
| `type` | `indexedstrip` for strips, anything else for a triangle list |

## Nodes

Every root child whose tag starts with `node_` is a top-level node. Child nodes sit inside a `<children>` wrapper. **The tag is the node type.**

Common attributes:

| Attribute | Default | Meaning |
| --- | --- | --- |
| `name` | | Node name, used by MTB rules, skin bone lists and hardpoints |
| `position` | `0 0 0` | Local translation |
| `rot_axis` | `1 0 0` | Rotation axis (normalised on read) |
| `rot_angle` | `0` | Rotation angle in radians |
| `scale` | `1 1 1` | Local scale |
| `turned_on` | `true` | `false` hides the node's meshes by default |

Node children:

| Element | Meaning |
| --- | --- |
| `<mesh name="...">` | Instance a `meshxml` at this node |
| `<shadow_mesh name="...">` | Shadow-volume mesh; normally not displayed |
| `<children>` | Child nodes |
| `<bone name>`, `<bone_name>`, `<skin_bone>` | Skin bone list entry (attribute `name` or `bone`) |
| `<bones>` / `<bone_list>` | Wrapper whose children each carry a `name` |

### Local matrix

Build a row-vector 4×4 (`p' = p · M`, translation in elements 12–14):

```
R = axis-angle rotation matrix (column-vector convention) for (rot_axis, rot_angle)
M[i][j] = scale[i] * R[j][i]      for i, j in 0..2     (rows are scaled, transposed R)
M[3] = (position.x, position.y, position.z, 1)
world = local · parent_world
```

See [Coordinate systems](../coordinates.md) for converting this to other conventions.

### Special node types

| Tag | Meaning |
| --- | --- |
| `node_switch_lod` | Level-of-detail switch: child *k* (counting `node_` children only) is LOD *k*. LOD 0 is full detail; the importer hides LOD > 0 by default. |
| `node_light` | Light (below) |
| `node_anim` | Animated sub-tree: `skeleton="x.gr2"` names the [Granny2](gr2.md) skeleton; `animation_set` names an [`.anm` set](gr2.md#anm-animation-sets) or a single `.gr2` |

### Skinning

A skinned mesh's vertex bone indices point into its node's bone list. Bone names match [Granny2](gr2.md) skeleton bones case-insensitively. Some character meshes are skinned to more joints than their game skeleton has (fingers, face); those joints exist as ordinary nodes with no mesh, so their rest pose can be taken from the node hierarchy.

### `node_light`

| Attribute | Meaning |
| --- | --- |
| `light_type` | `spot` or point (anything else) |
| `light_color` | 0–255 RGB |
| `light_inner_radius`, `light_outer_radius` | Falloff radii |
| `light_inner_angle`, `light_outer_angle` | Spot cone angles |
| `light_priority` | Integer priority |

An optional `<lightmap>` child adds `min_height`, `max_height`, `min_radius`, `max_radius`, `min_color` and `max_color`.

## Companion files

- **MTB:** visibility, damage state and heat-sink rules for the model's nodes, in a [WDF](wdf.md) file next to the model. See [MTB rules](mtb.md).
- **Skeleton and animations:** referenced from `node_anim`, or from a legacy `ANIMNODE` in a `.node` file.
