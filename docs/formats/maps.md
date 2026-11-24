# Maps

**Files:** world `*.wdf` inside `common.mgf` and per-map resource archives, `Multiplay/*.ini`, `*.road`, `terrain_layers.bin` (PC recomp)
**Reference:** [formats/map_wdf.py](../../io_scene_mechassault2/formats/map_wdf.py), [importers/map_builder.py](../../io_scene_mechassault2/importers/map_builder.py)

A map is one [WDF](wdf.md) world file. A handful of singleton objects describe the terrain, lighting and sky, and every other object with a position is a placed entity. Assets come from the map's own resource MGF first, then the shared archives (see [archive priority](mgf.md#opening-a-whole-game)).

All positions are MA2 world space: metres, left-handed, **Y up**. See [Coordinate systems](../coordinates.md).

## Finding maps

Maps are listed inside `common.mgf`:

- **Multiplayer:** each `Multiplay/<name>.ini` has a `[Mission]` section (INI dialect as in [MAXPort text](maxport-text.md#ini-dialect)):

  | Key | Meaning |
  | --- | --- |
  | `WorldFile` | World file name, relative to `Multiplay/` |
  | `ResourceFile` | Resource MGF on disc (e.g. `<map>.mgf`) |
  | `GameType` (repeated) | Supported game modes |

- **Campaign:** every `Missions/mission_*.wdf`. Its resource archive is `<file stem>.mgf`.

## Terrain (`MATerrain`)

| Property | Default | Meaning |
| --- | --- | --- |
| `TerrainHeight Field` | | Embedded [MGIc](mgic-texture.md#non-colour-uses) 8-bit image |
| `TerrainMinHeight` | `-20` | Height of sample value 0 |
| `TerrainMaxHeight` | `100` | Height of sample value 255 |
| `TerrainWorldScale` | `8` | Metres between samples (values outside 0.25–1000 are treated as 8) |
| `TerrainBaseMap` | | Colour map texture covering the whole terrain |
| `TerrainDetailMap`, `TerrainDetailMap 1` | | Two tiling detail textures |
| `TerrainDetailMult`, `TerrainDetailMult1` | `64` | Detail tiling repeats |
| `TerrainMicroDetailMap0`, `TerrainMicroDetailMap1` | | Micro-detail textures |
| `TerrainMicroDetailTiling0`, `TerrainMicroDetailTiling1` | `240` | Micro-detail tiling |
| `TerrainDetailBlend` | `0.3` | Detail blend strength |
| `TerrainDetailModulate` | `1` | Detail modulates (multiplies) the base colour |

If `TerrainBaseMap` is missing, the importer uses the first `terrain/…colormap…` file in the map's archives.

### Heightfield geometry

For a `w × h` image with stored rows `r = 0 … h-1`:

```
gz          = h - 1 - r                            # stored rows run from +Z to -Z
height[gz][gx] = min + sample[r][gx] / 255 * (max - min)
world(gx, gz)  = (gx * scale, height[gz][gx], gz * scale)
```

The terrain spans `(w-1)·scale` by `(h-1)·scale` from the world origin. Between samples, the game splits each cell along its diagonal: with `tx`, `tz` the fractional cell position and corners `a=(0,0) b=(1,0) c=(0,1) d=(1,1)`:

```
if tx >= tz:  y = a + (b - a)·tx + (d - b)·tz
else:         y = a + (c - a)·tz + (d - c)·tx
```

Use the same split when you triangulate, so that objects placed with this rule sit on your mesh.

### `terrain_layers.bin` (MA2L, PC recomp)

The [PC recomp](https://github.com/Mazillion/MechAssault2-PC-Recomp) project can write extra ground layers to `terrain_layers.bin` at a map archive's root. This file is not part of the original game.

| Offset | Type | Meaning |
| --- | --- | --- |
| `0x00` | `char[4]` | `"MA2L"` |
| `0x04` | `u32` | Version (`1`) |
| `0x08` | `u32` | Layer count *n* (2–8) |
| `0x0C` | `u32` | Weight map width *w* |
| `0x10` | `u32` | Weight map height *h* |
| `0x14` | `u32` | Layer texture size *t* (square) |
| `0x18` | `u32[2]` | Colour map width and height (not read) |

Then:

1. *n* layer records of 128 bytes: `f32` repeats, `u32` kind, `char[120]` source path (NUL-terminated; if it contains `|`, the path is the part after it).
2. Weights, run-length encoded row by row: each run is `u16 count` + `u8[8]` weights (one per layer), repeated until the row's *w* texels are filled.
3. For layers 2 … *n*−1: raw RGBA8 texture, `t × t × 4` bytes. Layers 0 and 1 carry no pixels here; the importer takes them to be the base terrain layers from the WDF.

## Environment (`MAEnvironment`)

| Property | Default | Meaning |
| --- | --- | --- |
| `LightAmbientColor` | `0xFF202028` | Ambient light (COLOR, ARGB) |
| `DirectionalLightOn` | `1` | Sun enabled |
| `DirectionalLightColor` | `0xFF595959` | Sun colour |
| `DirectionalLightDirection` | `(1, -1, 1)` | Direction the light travels (VEC3) |
| `IsNightTime` | `1` | Night flag |
| `FogColor`, `FogStart`, `FogEnd`, `FogMode`, `FogTop`, `FogBottom` | grey, 0, 0, 1, 0, 0 | Distance and height fog |
| `EnvironmentMapName` | | Reflection [cubemap](mgic-texture.md#cubemaps) |
| `LightMapName` | | Terrain light map (fallback: first `terrain/…lightmap…` file) |

## Sky (`MASky2`)

| Property | Default | Meaning |
| --- | --- | --- |
| `Corporal3DObj` | | Sky dome model |
| `CorporalMTB` | | Its MTB |
| `SkyTerrainSkyScale` | `(400, 400, 400)` | Dome scale |
| `SkyTerrainSkyOffset` | `(0, 0, 0)` | Dome offset |
| `SkyFogOn`, `SkyFogColor`, `SkyFogTop`, `SkyFogBottom` | | Sky fog band; the importer applies it only when both top and bottom are present |

## Entities

Every object that is not a structural, logic, AI, camera or UI class (`MARoot`, `MAGroup`, triggers, actions, waypoints, and similar; full list in `_NON_PLACED_CLASSES`) and has a position becomes a placed entity.

### Position

The first of these properties with at least 12 bytes: `RoadPosition`, `CorporalPos`, `ObjPosition`, `Position`.

### Orientation

| Source | Units | Meaning |
| --- | --- | --- |
| `RoadRotation` | radians | `f32 heading`, optionally `f32 pitch, f32 roll` (takes priority) |
| `CorporalHeading` | **degrees** | Heading |
| `StartOrientation` | **radians** | Heading (used when there is no `CorporalHeading`) |
| `VolumeHeading` | degrees | Heading (last resort) |
| `CorporalPitch`, `CorporalRoll` | degrees | Pitch and roll |

The rotation, in MA2 space with column vectors, is:

```
M = T(pos) · Ry(heading) · Rx(pitch) · Rz(roll) · S(CorporalScale)
```

`CorporalScale` is a VEC3; if any component is 0, the importer ignores it and uses `(1, 1, 1)`.

### Model

1. `Corporal3DObj` / `CorporalMTB` on the object itself.
2. Otherwise the template's, through `TemplateID` → [`GlobalEntities.wdf`](wdf.md#templates-globalentitieswdf).
3. `MARoad` objects instead name a [`.road` file](maxport-text.md#road-road-segment-definition) in `RoadDefinition`; its `model` key gives the segment model.

### Other entity properties

| Property | Meaning |
| --- | --- |
| `Description` | Display name (falls back to the template's) |
| `VisibleEntity` | `0` = hidden |
| `ShadowReceiverType` | `1` = receives shadows (roads always do) |
| `SupportsColorShift`, `UseCustomColors`, `CustomSinglePlayerDiffuseColor` | Custom unit colour. The importer applies it only to `MATank` / `MATurret`, and ignores the default `0xFFCC0000` |

Units and spawns are `MAMech`, `MATank`, `MAFlyer`, `MAVehicle`, `MATurret`, `MAPerson`, `MAUberBoss`, `MAMiniSpider`, `MASpider`, `MAPowerUp`, `MAFlagCTF`, `MASnatchItBase`, `MASnatchItDataCore` and `MALevelStart`, either directly or through their template's class. `MAWater` marks water planes.

### Point lights (`MAPointLight`)

| Property | Default | Meaning |
| --- | --- | --- |
| `LightOn` | `1` | `0` = skip |
| `CorporalPos` / `ObjPosition` / `Position` | | Position |
| `LightColor` | white | Colour (ARGB) |
| `LightRadius` | `10` | Radius |
| `LightMap` | `0` | Also baked into the terrain light map |
| `LightMapColor`, `LightMapRadius` | `LightColor`, `20` | Light-map contribution |

## Culture scatter

Trees, rocks and other ground clutter are not placed objects. The game scatters them at load time, deterministically, from a 16-bit **culture map** and per-type generator settings. A tool that wants them must run the same algorithm.

### Inputs

**`MATerrainInfo`** (one per map):

| Property | Meaning |
| --- | --- |
| key `0x89FB5118` | Culture map: embedded MGIc with 2 bytes per texel, read as raw `u16` |
| `culturenBitfields` | Number of bit fields (default 16) |
| `cultureBF<i>Name` | Culture type name for field *i* |
| `cultureBF<i>Shift`, `cultureBF<i>NBits` | Bit position and width of field *i* |
| `cultureBF<i>Mask` | 16-bit mask; when it is one contiguous run of bits, it overrides `Shift`/`NBits` |

**`MACultureType`**: `Name` matches a `cultureBF<i>Name` (case-insensitively). Its child objects are generators.

**`MACultureGenerator`** (child of a type):

| Property | Default | Meaning |
| --- | --- | --- |
| `GenerationTemplate` | | Template UID giving the model |
| `Density` | `1000` | Instances per km² at full field value |
| `GridScale` | `(10, 10)` | Cell size in metres when snapping |
| `RandomScaleRange` | `0` | ± uniform scale jitter (clamped to 0.95) |
| `RandomRotation` | `1` | Random yaw |
| `AlignWithSlope` | `1` | Tilt the up axis to the terrain normal |
| `SnapToGrid` | `0` | One instance at each cell corner instead of random placement |
| `MinSlope`, `MaxSlope` | `0`, `90` | Allowed slope in degrees |

### Algorithm

```
size_x, size_z = terrain extent;  cw, ch = culture map size
cell_w = snap ? grid_x : size_x / cw
cell_h = snap ? grid_z : size_z / ch
per_cell = density * cell_w * cell_h * 1e-6
top = (1 << nbits) - 1

for gz in 0 .. floor(size_z / cell_h) - 1:
  for gx in 0 .. floor(size_x / cell_w) - 1:
    mx = clamp(int((gx + 0.5) * cell_w / size_x * cw), 0, cw-1)
    mz = clamp(int((gz + 0.5) * cell_h / size_z * ch), 0, ch-1)   # no row flip here
    level = (cmap[mz][mx] >> shift) & top
    if level == 0: continue
    rng = GameRng((gx << 16) ^ gz)
    n = per_cell * level / top
    count = int(n) + (1 if frac(n) > rng.range(0, 1) else 0)
    if snap: count = min(count, 1)
    repeat count times:
      if snap: px, pz = (gx + 0.001) * cell_w, (gz + 0.001) * cell_h
      else:    px = gx*cell_w + rng.range(0.001, 0.999)*cell_w
               pz = gz*cell_h + rng.range(0.001, 0.999)*cell_h
      normal = terrain normal at (px, pz); slope = acos(normal.y) in degrees
      if slope outside [min_slope, max_slope]: continue      # RNG not advanced further
      yaw   = random_rot ? rng.range(0, 2π) : 0
      scale = 1 + rng.range(-scale_range, scale_range)
      place at (px, height(px, pz), pz)
```

The RNG draws must happen in exactly this order, or the placements drift.

Orientation: start from yaw only (Y up, Z = `(sin yaw, 0, cos yaw)`). With `AlignWithSlope`, Y becomes the terrain normal and Z is made orthogonal to it and renormalised. X = Y × Z.

The terrain normal at a point is `normalize(-(h(x+s) - h(x-s)), 2s, -(h(z+s) - h(z-s)))`, with `s` = `TerrainWorldScale` and `h` the triangulated height above.

### `GameRng`

MT19937 with the **old-style seeding** (multiplier 69069) and a zero seed replaced by `0x1105`:

```python
class GameRng:
    def __init__(self, seed):
        seed &= 0xFFFFFFFF
        self.mt = [seed or 0x1105]
        for i in range(1, 624):
            self.mt.append((69069 * self.mt[-1]) & 0xFFFFFFFF)
        self.idx = 624
    # next_u32(): standard MT19937 twist and tempering
    def range(self, a, b):
        return a + self.next_u32() * 2.3283064365386963e-10 * (b - a)   # 2^-32
```

The importer stops at 12,000 instances per map by default.
