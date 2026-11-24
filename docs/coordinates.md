# Coordinate systems

**Reference:** [importers/coord.py](../io_scene_mechassault2/importers/coord.py)

## MA2 game space

Models, maps and textures use one convention:

| | |
| --- | --- |
| Handedness | Left-handed |
| Up | `+Y` |
| Units | Metres |
| Matrices | Row vectors (`p' = p · M`), translation in elements 12–14 of a 16-float array |
| Triangle winding | As stored; flip when mirroring into a right-handed system |
| UV / texture origin | Top-left |

[Granny2](formats/gr2.md) files are the exception: they keep 3ds Max's Z-up axes and say so in `ArtToolInfo`.

## Converting to right-handed Z-up (Blender)

Swap Y and Z. The swap is a reflection (determinant −1), which converts left-handed Y-up to right-handed Z-up in a single step:

| Data | Conversion |
| --- | --- |
| Positions, normals, directions | `(x, y, z) → (x, z, y)` |
| Triangles | `(i0, i1, i2) → (i0, i2, i1)` |
| UVs | `v → 1 − v` |
| 4×4 transforms | Transpose row-vector `M` into column-vector form, then `C · M · C`, with `C` the Y/Z swap matrix |
| Quaternions `(x, y, z, w)` | `(w, −x, −z, −y)` (w first, as in Blender) |
| Map entity placement | Build `T · Ry(heading) · Rx(pitch) · Rz(roll) · S` in MA2 space ([Maps](formats/maps.md#orientation)), then `C · M · C` |

`C · M · C` keeps rotations as proper rotations, so no negative scale ends up in object transforms.

For a right-handed **Y-up** target (glTF, OpenGL), negate Z instead (`(x, y, −z)`). Triangle winding still flips, because that is also a reflection.

## Granny2 files

Build a 3×3 basis change `K` from the file's `ArtToolInfo` vectors:

```
src  = [Right | Up | Back]            # columns, from ArtToolInfo
game = [(-1,0,0) | (0,1,0) | (0,0,1)] # MA2 game right, up, back as columns
K    = SwapYZ · game · src⁻¹          # file space → Blender (right-handed Z-up)
```

The game-space right vector is `−X`. This was derived by comparing a model's `.node` hierarchy with its skeleton: a Max point `(gx, gy, gz)` lands at MA2 `(−gx, gz, −gy)`. For a typical Max export (right `+X`, up `+Z`, back `−Y`), `K` comes out as a signed permutation.

Apply it to Granny transforms as `K · M · Kᵀ`, with `M = T · R · S` built from the [Granny transform](formats/gr2.md#transform-68-bytes). Without `ArtToolInfo`, treat the data as already in MA2 game space and use `SwapYZ` alone.

To target another convention, replace `SwapYZ` with the matching MA2 → target conversion.
