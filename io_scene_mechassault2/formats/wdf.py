"""MechAssault 2 WDF binary object parser (world files, GlobalEntities.wdf, and .mtb files)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import struct
from typing import Dict, List, Optional, Tuple
import zlib


class PropType(IntEnum):
    I8 = 1
    I16 = 2
    I32 = 3
    U8 = 4
    U16 = 5
    U32 = 6
    F32 = 7
    F64 = 8
    STRING = 9
    VEC2 = 10
    VEC3 = 11
    COLOR = 12
    BLOB = 13
    U8B = 16


WDF_MAGIC = 0xFF002F00
WDF_OBJ_MAGIC = 0xFF012F01
WDF_PROP_MAGIC = 0xFF022F02


def wdf_key(name: str) -> int:
    """WDF CRC32 key without final complement: crc32(name) ^ 0xFFFFFFFF."""
    return (zlib.crc32(name.encode("ascii", errors="replace")) ^ 0xFFFFFFFF) & 0xFFFFFFFF


_BASE_KNOWN_NAMES = [
    # Tree & template links
    "UID", "parent", "child", "sibling", "TemplateID", "OverrideTemplate", "Description", "Name",
    "MARoot", "MoeEntity", "MoeEntityReference", "VisibleEntity", "Nation", "IsCollidable",
    # Model & file properties
    "Corporal3DObj", "CorporalMTB", "AbandonedModel", "AbandonedMTB", "TransitionModel", "TransitionMTB",
    "CamouflageTexture", "HUDIconImage", "HUDEnemyIconImage", "HUDFriendIconImage", "MechFootprintDecal",
    "MechClawDecal", "EnvironmentMapName", "EffectFile", "SceneNode", "SubObjectName",
    "CorporalPos", "CorporalHeading", "CorporalPitch", "CorporalRoll", "CorporalScale",
    "ObjPosition", "TriggerPoint", "RoadPosition", "RoadRotation", "RoadDefinition",
    "Position", "CameraPosition", "StartOrientation", "VolumeHeading",
    "SupportsColorShift", "UseCustomColors", "CustomSinglePlayerDiffuseColor", "ShadowReceiverType",
    # Template links
    "DestroyedBuilding", "CorporalDestroyed", "DestroyedEffect", "HitEffect", "HoleEdgeEffect",
    "HoleTopEffect", "WeaponLoadout", "MAEffectID", "GenerationTemplate",
    # .mtb visibility & state rules
    "MAModSubObjectShow", "MAModSubObjectState", "MAModSubObjectDiffuse", "MAModSubObjectParticleEffect",
    "MAModSubObjRotRecoil", "MASubObjRespondShow", "MASubObjRespondRotate", "MAModMechHeatSink",
    "CSubObjectNode", "Enable", "MinDamagePercent", "MaxDamagePercent", "Damage Level Min",
    "Damage Level Max", "Abandoned", "EnableTree", "SubObjStateFlags",
    "MinimumHeatColorRed", "MinimumHeatColorGreen", "MinimumHeatColorBlue",
    # Terrain / Environment / Sky / Culture / Lights
    "MATerrain", "MATerrainInfo", "MAEnvironment", "MASky2", "MASky", "MACloud",
    "TerrainHeight Field", "TerrainMinHeight", "TerrainMaxHeight", "TerrainWorldScale",
    "TerrainBaseMap", "TerrainDetailMap", "TerrainDetailMap 1",
    "TerrainMicroDetailMap0", "TerrainMicroDetailMap1",
    "TerrainDetailMult", "TerrainDetailMult1",
    "TerrainMicroDetailTiling0", "TerrainMicroDetailTiling1",
    "TerrainDetailBlend", "TerrainDetailModulate",
    "LightMapName", "LightAmbientColor", "DirectionalLightOn", "DirectionalLightColor",
    "DirectionalLightDirection", "IsNightTime",
    "FogColor", "FogStart", "FogEnd", "FogMode", "FogTop", "FogBottom",
    "SkyTerrainSkyScale", "SkyTerrainSkyOffset", "SkyFogOn", "SkyFogColor", "SkyFogTop", "SkyFogBottom",
    "MAPointLight", "MALight", "MADirectionalLight", "LightOn", "LightColor", "LightRadius",
    "LightMap", "LightMapColor", "LightMapRadius",
    "MACultureSystem", "MACultureType", "MACultureGenerator", "culturenBitfields",
    "Density", "GridScale", "RandomScaleRange", "RandomRotation", "AlignWithSlope",
    "MinSlope", "MaxSlope", "SnapToGrid", "StartFade", "StopFade",
    # Entity classes
    "MAWpnGrpRef", "MACorporeal", "MAGroup", "MAEffect", "MACorporealDestroyed", "MABuilding",
    "MAPowerupDrop", "MAWpnGrp", "MABarrel", "MABuildingDestroyed", "MAProjectile", "MAWeaponLoadout",
    "MAMech", "MAPlayerConfiguration", "MAPlayerConfigurationContainer", "MAOwnerController", "MAPerson",
    "MACameraThird", "MAObjectiveType", "MAPowerUp", "MATurret", "MATank", "MAFlyer", "MASky2",
    "MACameraProperty", "MAVehicle", "MAWater", "DropPackManager", "DropPack",
    "MATriggerLogic", "MATriggerTimer", "MATriggerRadius", "MARubble", "MASnatchItBase", "MACheckpoint",
    "MAFreeCameraController", "MAPrecipitation", "MAFlagCTF", "MechAssaultSettings", "MACameraPan",
    "MACameraSpiral", "MACameraFirst", "MAEffectCluster", "MAEffectChain", "MAEffectShield",
    "MAKillReference", "MAPlayer", "MAUberBoss", "MAMiniSpider", "MASpider",
    "MASnatchItDataCore", "MACinema", "MACollectionArea", "MALevelStart", "MAPathDB",
    "MATree", "MATreeGroup", "MADecal", "MARoad", "MARoadGroup", "MACrateGenerator",
    "MAMissionTypeGroup", "MAWaypointGroup", "MAWaypoint", "MASpawner",
]

KNOWN_WDF_NAMES: Dict[int, str] = {wdf_key(n): n for n in _BASE_KNOWN_NAMES}
for _b in range(16):
    for _field in ("Name", "Shift", "NBits", "Mask", "Color0"):
        _n = f"cultureBF{_b}{_field}"
        KNOWN_WDF_NAMES[wdf_key(_n)] = _n

# Special binary-only key for culture map (MATerrainInfo)
K_CULTURE_MAP = 0x89FB5118


def register_known_names(names: List[str]) -> None:
    for n in names:
        KNOWN_WDF_NAMES.setdefault(wdf_key(n), n)


def known_wdf_name(key: int) -> Optional[str]:
    return KNOWN_WDF_NAMES.get(key)


@dataclass
class WdfProperty:
    ptype: int
    key: int
    data: bytes

    def as_int(self) -> int:
        d = self.data
        t = self.ptype
        if t == PropType.I8 and len(d) >= 1:
            return struct.unpack_from("<b", d, 0)[0]
        if t == PropType.I16 and len(d) >= 2:
            return struct.unpack_from("<h", d, 0)[0]
        if t == PropType.I32 and len(d) >= 4:
            return struct.unpack_from("<i", d, 0)[0]
        if t in (PropType.U8, PropType.U8B) and len(d) >= 1:
            return d[0]
        if t == PropType.U16 and len(d) >= 2:
            return struct.unpack_from("<H", d, 0)[0]
        if t in (PropType.U32, PropType.COLOR) and len(d) >= 4:
            return struct.unpack_from("<I", d, 0)[0]
        if t == PropType.F32 and len(d) >= 4:
            return int(struct.unpack_from("<f", d, 0)[0])
        if t == PropType.F64 and len(d) >= 8:
            return int(struct.unpack_from("<d", d, 0)[0])
        if len(d) >= 4:
            return struct.unpack_from("<i", d, 0)[0]
        if len(d) == 2:
            return struct.unpack_from("<H", d, 0)[0]
        if len(d) == 1:
            return d[0]
        return 0

    def as_u32(self) -> int:
        return self.as_int() & 0xFFFFFFFF

    def as_i32(self) -> int:
        v = self.as_u32()
        return v - 0x100000000 if (v & 0x80000000) else v

    def as_float(self) -> float:
        if self.ptype == PropType.F32 and len(self.data) >= 4:
            return float(struct.unpack_from("<f", self.data, 0)[0])
        if self.ptype == PropType.F64 and len(self.data) >= 8:
            return float(struct.unpack_from("<d", self.data, 0)[0])
        return float(self.as_int())

    def as_string(self) -> str:
        end = self.data.find(b"\x00")
        raw = self.data if end < 0 else self.data[:end]
        return raw.decode("latin-1", errors="replace")

    def as_vec2(self) -> Tuple[float, float]:
        if len(self.data) >= 8:
            return struct.unpack_from("<2f", self.data, 0)
        return (0.0, 0.0)

    def as_vec3(self) -> Tuple[float, float, float]:
        if len(self.data) >= 12:
            return struct.unpack_from("<3f", self.data, 0)
        if len(self.data) >= 8:
            x, y = struct.unpack_from("<2f", self.data, 0)
            return (x, y, 0.0)
        return (0.0, 0.0, 0.0)

    def as_color_rgba(self) -> Tuple[float, float, float, float]:
        c = self.as_u32()
        r = ((c >> 16) & 0xFF) / 255.0
        g = ((c >> 8) & 0xFF) / 255.0
        b = (c & 0xFF) / 255.0
        a = ((c >> 24) & 0xFF) / 255.0
        return (r, g, b, a)


@dataclass
class WdfObject:
    cls: int
    props: List[WdfProperty] = field(default_factory=list)
    _by_key: Optional[Dict[int, WdfProperty]] = field(default=None, repr=False, compare=False)

    def _ensure_map(self) -> Dict[int, WdfProperty]:
        if self._by_key is None:
            self._by_key = {p.key: p for p in self.props}
        return self._by_key

    def find(self, key_or_name: int | str) -> Optional[WdfProperty]:
        k = wdf_key(key_or_name) if isinstance(key_or_name, str) else key_or_name
        return self._ensure_map().get(k)

    def u32(self, key_or_name: int | str, default: int = 0) -> int:
        p = self.find(key_or_name)
        return p.as_u32() if p is not None else default

    def i32(self, key_or_name: int | str, default: int = 0) -> int:
        p = self.find(key_or_name)
        return p.as_i32() if p is not None else default

    def f32(self, key_or_name: int | str, default: float = 0.0) -> float:
        p = self.find(key_or_name)
        return p.as_float() if p is not None else default

    def str_val(self, key_or_name: int | str, default: str = "") -> str:
        p = self.find(key_or_name)
        return p.as_string() if p is not None else default

    def vec2(self, key_or_name: int | str, default: Tuple[float, float] = (0.0, 0.0)) -> Tuple[float, float]:
        p = self.find(key_or_name)
        return p.as_vec2() if p is not None else default

    def vec3(self, key_or_name: int | str, default: Tuple[float, float, float] = (0.0, 0.0, 0.0)) -> Tuple[float, float, float]:
        p = self.find(key_or_name)
        return p.as_vec3() if p is not None else default

    @property
    def class_name(self) -> str:
        return KNOWN_WDF_NAMES.get(self.cls, f"0x{self.cls:08X}")

    @property
    def uid(self) -> int:
        return self.u32("UID")

    @property
    def parent(self) -> int:
        return self.u32("parent")

    @property
    def child(self) -> int:
        return self.u32("child")

    @property
    def sibling(self) -> int:
        return self.u32("sibling")

    @property
    def template_id(self) -> int:
        return self.u32("TemplateID")

    @property
    def description(self) -> str:
        return self.str_val("Description")


@dataclass
class WdfFile:
    header: Tuple[int, int, int, int, int]
    objs: List[WdfObject] = field(default_factory=list)


def parse_wdf(data: bytes) -> WdfFile:
    n = len(data)
    if n < 20:
        raise ValueError("WDF: short header")
    hdr = struct.unpack_from("<5I", data, 0)
    if hdr[0] != WDF_MAGIC:
        raise ValueError(f"WDF: bad header marker 0x{hdr[0]:08X}")
    pos = 20
    objs: List[WdfObject] = []
    while pos < n:
        if pos + 12 > n:
            raise ValueError(f"WDF: short object header at 0x{pos:X}")
        marker, prop_count, cls = struct.unpack_from("<3I", data, pos)
        if marker != WDF_OBJ_MAGIC:
            raise ValueError(f"WDF: bad object marker 0x{marker:08X} at 0x{pos:X}")
        pos += 12
        if prop_count > (n - pos) // 16:
            raise ValueError(f"WDF: impossible property count {prop_count} at 0x{pos:X}")
        props: List[WdfProperty] = []
        for _ in range(prop_count):
            if pos + 16 > n:
                raise ValueError(f"WDF: short property header at 0x{pos:X}")
            pm, ptype, pkey, psize = struct.unpack_from("<4I", data, pos)
            if pm != WDF_PROP_MAGIC or psize > n - pos - 16:
                raise ValueError(f"WDF: bad property at 0x{pos:X}")
            pdata = data[pos + 16:pos + 16 + psize]
            props.append(WdfProperty(ptype=ptype, key=pkey, data=pdata))
            pos += 16 + psize
        objs.append(WdfObject(cls=cls, props=props))
    return WdfFile(header=hdr, objs=objs)


class WdfTree:
    """Tree view over a WdfFile built via UID / child / sibling links."""

    def __init__(self, wdf: WdfFile) -> None:
        self.wdf = wdf
        self.by_uid: Dict[int, int] = {}
        self.children: List[List[int]] = [[] for _ in wdf.objs]
        self.parent_of: List[int] = [-1] * len(wdf.objs)
        self.root: int = -1
        k_root = wdf_key("MARoot")

        for i, o in enumerate(wdf.objs):
            u = o.uid
            if u:
                self.by_uid.setdefault(u, i)
            if self.root < 0 and o.cls == k_root:
                self.root = i
        if self.root < 0 and wdf.objs:
            self.root = 0

        for i, o in enumerate(wdf.objs):
            c = o.child
            guard = 0
            while c and guard < 100_000:
                guard += 1
                ci = self.by_uid.get(c, -1)
                if ci < 0 or self.parent_of[ci] >= 0 or ci == i:
                    break
                self.parent_of[ci] = i
                self.children[i].append(ci)
                c = wdf.objs[ci].sibling
