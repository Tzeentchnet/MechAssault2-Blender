# WDF object files

**Files:** world/map `*.wdf`, `Templates/GlobalEntities.wdf`, model rule files `*.mtb`
**Reference:** [formats/wdf.py](../../io_scene_mechassault2/formats/wdf.py)

WDF is a flat list of objects, each a bag of typed properties. Class names and property names are stored as 32-bit hashes, so a reader needs a table of known names (below) to make sense of them. Objects link into a tree through UID properties.

## Layout

```
header      5 × u32
object*     until end of file
  marker    u32  0xFF012F01
  count     u32  property count
  class     u32  class-name hash
  property* count times
    marker  u32  0xFF022F02
    type    u32  property type
    key     u32  property-name hash
    size    u32  payload size in bytes
    data    u8[size]
```

Header:

| Word | Meaning |
| --- | --- |
| 0 | `0xFF002F00` |
| 1–4 | Unknown (the test fixtures write `1, 0, object count, 0`; the importer does not read them) |

There is no padding between records. Reject a property count that could not fit in the remaining bytes.

## Name hashes

```python
import zlib
def wdf_key(name: str) -> int:
    return zlib.crc32(name.encode("ascii")) ^ 0xFFFFFFFF
```

That is standard CRC-32 (reflected, polynomial `0xEDB88320`, initial value `0xFFFFFFFF`) **without the final XOR**. Names are case-sensitive and may contain spaces (`"TerrainHeight Field"`, `"Damage Level Min"`).

The hash cannot be reversed, so names are recovered by hashing candidate strings. [formats/wdf.py](../../io_scene_mechassault2/formats/wdf.py) has a table of about 200 known class and property names (`_BASE_KNOWN_NAMES`), plus the generated `cultureBF<0–15><Name|Shift|NBits|Mask|Color0>` family. One key whose name is not known is used anyway: `0x89FB5118`, the culture map blob on `MATerrainInfo`.

## Property types

| Type | Name | Payload |
| --- | --- | --- |
| 1 | I8 | `i8` |
| 2 | I16 | `i16` |
| 3 | I32 | `i32` |
| 4 | U8 | `u8` |
| 5 | U16 | `u16` |
| 6 | U32 | `u32` (also used for UIDs and template references) |
| 7 | F32 | `f32` |
| 8 | F64 | `f64` |
| 9 | STRING | NUL-terminated Latin-1 text |
| 10 | VEC2 | `f32[2]` |
| 11 | VEC3 | `f32[3]` |
| 12 | COLOR | `u32` `0xAARRGGBB` |
| 13 | BLOB | Raw bytes (e.g. an embedded [MGIc](mgic.md) image) |
| 16 | U8B | `u8` (a bool or byte variant) |

The type field does not always match what a property means: booleans can arrive as `I32` or `U8`, and a position may be `VEC3` or a 12-byte blob. Read by property name and convert leniently: an integer read of a float truncates it, and a 3-vector read of 8 bytes gives `(x, y, 0)`.

## Object tree

Tree links are ordinary `U32` properties holding UIDs (0 = none):

| Property | Meaning |
| --- | --- |
| `UID` | This object's ID |
| `parent` | Parent UID |
| `child` | First child UID |
| `sibling` | Next sibling UID |

Build children by following each object's `child`, then that child's `sibling` chain. The root is the first `MARoot` object. Guard against cycles and objects claimed by two parents.

Two descriptive properties appear on most objects:

- `Description`: display name. On `MAGroup` objects it gives the folder name, so the group path is the `/`-joined descriptions of an object's `MAGroup` ancestors.
- `TemplateID`: UID of a template object in `GlobalEntities.wdf` (below).

## Templates (`GlobalEntities.wdf`)

`Templates/GlobalEntities.wdf` (or `GlobalEntities.wdf` at the root) is a WDF catalogue of entity templates, keyed by `UID`. Placed map objects reference one through `TemplateID` and inherit what they do not override. Useful template properties:

| Property | Meaning |
| --- | --- |
| `Description` | Template name; ancestor descriptions give its category path |
| `Corporal3DObj` | Model path |
| `CorporalMTB` | [MTB](mtb.md) path |
| `AbandonedModel`, `AbandonedMTB` | Abandoned-state model and rules |
| `TransitionModel`, `TransitionMTB` | Transition-state model and rules |
| `CamouflageTexture` | Camo texture |

Culture generators reference templates through `GenerationTemplate` the same way.

## Where WDF is used

- [Maps](maps.md): one world `.wdf` per mission or multiplayer map.
- [MTB rules](mtb.md): per-model visibility and damage rules.
- Templates: `GlobalEntities.wdf`, as above.
