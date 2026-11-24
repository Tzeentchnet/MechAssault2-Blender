# MTB rules

**Files:** `*.mtb`
**Reference:** `_apply_mtb` and `_guess_mtb` in [formats/model.py](../../io_scene_mechassault2/formats/model.py)

An MTB is a [WDF](wdf.md) file of rules that drive a model's sub-objects at runtime: which parts show at which damage level, effect parts that only appear in some states, and heat-glow tints. Rules target model nodes by name (`SubObjectName`, matched case-insensitively against [MGMODEL](mgmodel.md#nodes) / [`.node`](maxport-text.md) node names).

The importer uses MTBs to show the model as it looks **undamaged (100% health), not abandoned**.

## Finding a model's MTB

Maps and templates usually name it (`CorporalMTB`). Otherwise try, in order, and take the first that exists:

1. `<model folder>/<model stem>.mtb`
2. `<geometry folder>/<geometry stem>.mtb` (geometry = the file that actually holds meshes, e.g. an `ANIMNODE` child)
3. `<model folder>/<geometry stem>.mtb`
4. `<parent of geometry folder>/<geometry stem>.mtb`

For each stem ending in `_low`, also try it without the suffix.

## `MAModSubObjectShow`: damage visibility

| Property | Default | Meaning |
| --- | --- | --- |
| `SubObjectName` | | Target node |
| `Enable` | 1 | Rule active |
| `MinDamagePercent` | 0 | Lower bound of the range |
| `MaxDamagePercent` | 100 | Upper bound of the range |
| `Abandoned` | 0 | Non-zero: rule applies to the abandoned state |
| `EnableTree` | 0 | Non-zero: rule also governs the node's descendants |

The percentages appear to be health remaining (100 = intact), since intact parts carry ranges that include 100. A node can have several rules. The importer shows a node if **any** of its rules has `Enable != 0`, `Abandoned == 0` and `Min ≤ 100 ≤ Max`. A node with rules that all fail is hidden, along with its whole subtree if any of them set `EnableTree`. Nodes with no rule are unaffected.

Related classes in the known-name table that the importer does not interpret: `MAModSubObjectDiffuse`, `MAModSubObjectParticleEffect`, `MAModSubObjRotRecoil`, `MASubObjRespondShow`, `MASubObjRespondRotate`, `CSubObjectNode`, and the `Damage Level Min` / `Damage Level Max` properties.

## `MAModSubObjectState`: state flags

| Property | Meaning |
| --- | --- |
| `SubObjectName` | Target node |
| `SubObjStateFlags` | Bit field |

Only bit `0x10` is used. When it is set and **every** mesh on the target node is an effect (distortion material, or additive/alpha-blended), the importer hides the node. These are state-specific effects such as shields and muzzle glows. The other bits' meanings are unknown.

## `MAModMechHeatSink`: heat tint

| Property | Meaning |
| --- | --- |
| `SubObjectName` | Target node |
| `MinimumHeatColorRed`, `MinimumHeatColorGreen`, `MinimumHeatColorBlue` | 0–255 tint channels |

The importer tints every mesh on the target node with this colour, the heat-sink glow at minimum heat.
