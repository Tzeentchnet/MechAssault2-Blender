"""MechAssault 2 map (.wdf), template catalog (GlobalEntities.wdf), terrain, and culture scatter parser."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import struct
from typing import Dict, List, Optional, Tuple

import numpy as np

from .mgf import ArchiveSet, AssetSource, MGFArchive
from .xiso import XisoArchive, is_xiso_file
from .mgic_texture import decode_culture_map_u16, decode_heightfield_samples8
from .text_utils import (
    file_stem,
    normalize_path,
    parse_ini,
    parse_road_definition,
)
from .wdf import K_CULTURE_MAP, WdfFile, WdfObject, WdfTree, parse_wdf, wdf_key


@dataclass
class TemplateInfo:
    uid: int
    class_name: str
    name: str
    category: str = ""
    model: str = ""
    mtb: str = ""
    extra_files: Dict[str, str] = field(default_factory=dict)


class TemplateCatalog:
    def __init__(self) -> None:
        self.by_uid: Dict[int, TemplateInfo] = {}

    @classmethod
    def from_wdf_bytes(cls, data: bytes) -> "TemplateCatalog":
        cat = cls()
        wdf = parse_wdf(data)
        tree = WdfTree(wdf)
        k_root = wdf_key("MARoot")
        for i, o in enumerate(wdf.objs):
            uid = o.uid
            if not uid:
                continue
            parts: List[str] = []
            p = tree.parent_of[i]
            while p >= 0 and wdf.objs[p].cls != k_root:
                desc = wdf.objs[p].description.strip()
                if desc:
                    parts.append(desc)
                p = tree.parent_of[p]
            category = "/".join(reversed(parts))
            extra: Dict[str, str] = {}
            for prop_name in ("AbandonedModel", "AbandonedMTB", "TransitionModel", "TransitionMTB", "CamouflageTexture"):
                val = o.str_val(prop_name)
                if val:
                    extra[prop_name] = normalize_path(val)
            cat.by_uid[uid] = TemplateInfo(
                uid=uid,
                class_name=o.class_name,
                name=o.description,
                category=category,
                model=normalize_path(o.str_val("Corporal3DObj")),
                mtb=normalize_path(o.str_val("CorporalMTB")),
                extra_files=extra,
            )
        return cat

    @classmethod
    def from_source(cls, source: AssetSource) -> "TemplateCatalog":
        for candidate in ("Templates/GlobalEntities.wdf", "GlobalEntities.wdf"):
            if source.exists(candidate):
                return cls.from_wdf_bytes(source.read(candidate))
        return cls()

    def find(self, uid: int) -> Optional[TemplateInfo]:
        return self.by_uid.get(uid)


@dataclass
class MapEntry:
    name: str
    multiplayer: bool
    world_path: str
    resource_mgf: str
    ini_path: str = ""
    game_types: List[str] = field(default_factory=list)
    resource_present: bool = False


def discover_maps_from_common(common: MGFArchive, existing_mgfs: set[str]) -> List[MapEntry]:
    """Discover multiplayer & campaign maps inside an already-opened `common.mgf` archive."""
    maps: List[MapEntry] = []
    for entry in common.entries:
        low = entry.path.lower()
        if low.startswith("multiplay/") and low.endswith(".ini"):
            try:
                secs = parse_ini(common.read(entry.path))
            except Exception:
                continue
            mission = next((s for s in secs if s.name.lower() == "mission"), None)
            if mission is None:
                continue
            world = mission.get("WorldFile", "") or ""
            if not world:
                continue
            world_path = f"Multiplay/{world}"
            if not common.exists(world_path):
                continue
            res = (mission.get("ResourceFile", "") or "").lower()
            maps.append(
                MapEntry(
                    name=file_stem(entry.path),
                    multiplayer=True,
                    world_path=common.canonical_path(world_path),
                    resource_mgf=res,
                    ini_path=entry.path,
                    game_types=mission.get_all("GameType"),
                    resource_present=(res in existing_mgfs),
                )
            )
        elif low.startswith("missions/mission_") and low.endswith(".wdf"):
            name = file_stem(entry.path)
            res = f"{name.lower()}.mgf"
            maps.append(
                MapEntry(
                    name=name,
                    multiplayer=False,
                    world_path=entry.path,
                    resource_mgf=res,
                    resource_present=(res in existing_mgfs),
                )
            )

    maps.sort(key=lambda m: (not m.multiplayer, m.name.lower()))
    return maps


def discover_maps(game_files_dir: str | os.PathLike[str]) -> List[MapEntry]:
    """Discover multiplayer & campaign maps from common.mgf in `game_files_dir` or an Xbox ISO."""
    root = Path(game_files_dir)
    if root.is_file() and is_xiso_file(root):
        with XisoArchive(root) as xiso:
            mgfs = xiso.mgf_entries()
            existing_mgfs = {e.name.lower() for e in mgfs}
            common_entry = next((e for e in mgfs if e.name.lower() == "common.mgf"), None)
            if common_entry is None:
                return []
            with MGFArchive(
                root,
                base_offset=common_entry.offset,
                slice_size=common_entry.size,
                virtual_name=common_entry.name,
            ) as common:
                return discover_maps_from_common(common, existing_mgfs)

    common_path = root / "common.mgf"
    if not common_path.is_file():
        return []

    existing_mgfs = {p.name.lower() for p in root.glob("*.mgf")}
    with MGFArchive(common_path) as common:
        return discover_maps_from_common(common, existing_mgfs)


@dataclass
class TerrainLayerInfo:
    source_path: str = ""
    repeats: float = 64.0
    rgba: Optional[np.ndarray] = None


@dataclass
class TerrainData:
    w: int = 0
    h: int = 0
    scale: float = 8.0
    min_h: float = -20.0
    max_h: float = 100.0
    heights: Optional[np.ndarray] = None  # (h, w) float32; row gz -> world z = gz * scale
    colormap_path: str = ""
    detail0_path: str = ""
    detail1_path: str = ""
    micro0_path: str = ""
    micro1_path: str = ""
    detail_mult: float = 64.0
    detail_mult1: float = 64.0
    micro_tiling0: float = 240.0
    micro_tiling1: float = 240.0
    detail_blend: float = 0.3
    detail_modulate: bool = True
    extra_layers_w: int = 0
    extra_layers_h: int = 0
    extra_layers_weights: Optional[np.ndarray] = None  # (h, w, 8) uint8
    extra_layers: List[TerrainLayerInfo] = field(default_factory=list)

    @property
    def size_x(self) -> float:
        return max(0.0, (self.w - 1) * self.scale)

    @property
    def size_z(self) -> float:
        return max(0.0, (self.h - 1) * self.scale)

    def height_at(self, x: float, z: float) -> float:
        if self.heights is None or self.w < 2 or self.h < 2:
            return 0.0
        fx = min(max(x / self.scale, 0.0), float(self.w - 1))
        fz = min(max(z / self.scale, 0.0), float(self.h - 1))
        x0 = min(int(fx), self.w - 2)
        z0 = min(int(fz), self.h - 2)
        tx = fx - float(x0)
        tz = fz - float(z0)
        a = float(self.heights[z0, x0])
        b = float(self.heights[z0, x0 + 1])
        c = float(self.heights[z0 + 1, x0])
        d = float(self.heights[z0 + 1, x0 + 1])
        if tx >= tz:
            return a + (b - a) * tx + (d - b) * tz
        return a + (c - a) * tz + (d - c) * tx

    def normal_at(self, x: float, z: float) -> Tuple[float, float, float]:
        e = self.scale
        hx = self.height_at(x + e, z) - self.height_at(x - e, z)
        hz = self.height_at(x, z + e) - self.height_at(x, z - e)
        nx, ny, nz = -hx, 2.0 * e, -hz
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length < 1e-8:
            return (0.0, 1.0, 0.0)
        return (nx / length, ny / length, nz / length)


@dataclass
class EnvironmentData:
    ambient_color: Tuple[float, float, float] = (0.125, 0.125, 0.157)
    sun_on: bool = True
    sun_color: Tuple[float, float, float] = (0.35, 0.35, 0.35)
    sun_dir: Tuple[float, float, float] = (1.0, -1.0, 1.0)
    fog_color: Tuple[float, float, float] = (0.5, 0.5, 0.5)
    fog_start: float = 0.0
    fog_end: float = 0.0
    fog_mode: int = 1
    fog_top: float = 0.0
    fog_bottom: float = 0.0
    env_cube_path: str = ""
    lightmap_path: str = ""
    is_night: bool = False


@dataclass
class SkyData:
    model_path: str = ""
    mtb_path: str = ""
    scale: Tuple[float, float, float] = (400.0, 400.0, 400.0)
    offset: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    fog_on: bool = False
    fog_color: Tuple[float, float, float] = (0.5, 0.5, 0.5)
    fog_top: float = 0.0
    fog_bottom: float = 0.0


@dataclass
class PlacedEntity:
    uid: int
    class_name: str
    name: str
    group_path: str = ""
    template_id: int = 0
    model_path: str = ""
    mtb_path: str = ""
    pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    heading: float = 0.0  # radians
    pitch: float = 0.0    # radians
    roll: float = 0.0     # radians
    scale: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    custom_color: Optional[Tuple[float, float, float, float]] = None
    shadow_receiver: bool = False
    visible: bool = True
    is_road: bool = False
    is_water: bool = False
    is_unit: bool = False
    is_point_light: bool = False
    light_color: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    light_radius: float = 10.0
    light_map: bool = False
    light_map_color: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    light_map_radius: float = 20.0


@dataclass
class CultureInstance:
    name: str
    template_uid: int
    model_path: str
    mtb_path: str
    pos: Tuple[float, float, float]
    basis_x: Tuple[float, float, float]
    basis_y: Tuple[float, float, float]
    basis_z: Tuple[float, float, float]
    scale: float


@dataclass
class MapData:
    name: str
    world_path: str
    terrain: Optional[TerrainData] = None
    environment: EnvironmentData = field(default_factory=EnvironmentData)
    sky: Optional[SkyData] = None
    entities: List[PlacedEntity] = field(default_factory=list)
    culture_instances: List[CultureInstance] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# GameRng (MT19937 with old-style seed, matching app_culture.cpp)
# ---------------------------------------------------------------------------

class _GameRng:
    __slots__ = ("mt", "idx")

    def __init__(self, seed: int) -> None:
        mt = [0] * 624
        mt[0] = (seed & 0xFFFFFFFF) if (seed & 0xFFFFFFFF) else 0x1105
        for i in range(1, 624):
            mt[i] = (69069 * mt[i - 1]) & 0xFFFFFFFF
        self.mt = mt
        self.idx = 624

    def next_u32(self) -> int:
        if self.idx >= 624:
            mt = self.mt
            for k in range(624):
                y = (mt[k] & 0x80000000) | (mt[(k + 1) % 624] & 0x7FFFFFFF)
                mt[k] = mt[(k + 397) % 624] ^ (y >> 1) ^ (0x9908B0DF if (y & 1) else 0)
            self.idx = 0
        y = self.mt[self.idx]
        self.idx += 1
        y ^= y >> 11
        y ^= (y << 7) & 0x9D2C5680
        y ^= (y << 15) & 0xEFC60000
        y ^= y >> 18
        return y & 0xFFFFFFFF

    def range(self, a: float, b: float) -> float:
        return a + float(self.next_u32()) * 2.3283064365386963e-10 * (b - a)


def _decode_ma2l_ground_layers(data: bytes, terrain: TerrainData) -> None:
    """Decode PC recomp terrain_layers.bin (MA2L v1)."""
    if len(data) < 32 or data[:4] != b"MA2L":
        return
    _magic, ver, nlayers, w, h, tex_sz, _cm_w, _cm_h = struct.unpack_from("<8I", data, 0)
    if ver != 1 or nlayers < 2 or nlayers > 8 or w == 0 or h == 0:
        return
    pos = 32
    layers: List[TerrainLayerInfo] = []
    for _ in range(nlayers):
        if pos + 128 > len(data):
            return
        rep, _kind = struct.unpack_from("<fI", data, pos)
        src_raw = data[pos + 8:pos + 128].split(b"\x00", 1)[0].decode("latin-1", errors="replace")
        src_path = src_raw.split("|", 1)[-1] if "|" in src_raw else src_raw
        layers.append(TerrainLayerInfo(source_path=normalize_path(src_path), repeats=rep))
        pos += 128

    weights = np.zeros((h, w, 8), dtype=np.uint8)
    for y in range(h):
        x = 0
        while x < w and pos + 10 <= len(data):
            cnt = struct.unpack_from("<H", data, pos)[0]
            w8 = np.frombuffer(data, dtype=np.uint8, count=8, offset=pos + 2)
            pos += 10
            end_x = min(w, x + cnt)
            weights[y, x:end_x, :] = w8
            x = end_x

    for li in range(2, nlayers):
        need = tex_sz * tex_sz * 4
        if pos + need > len(data):
            break
        rgba = np.frombuffer(data, dtype=np.uint8, count=need, offset=pos).reshape((tex_sz, tex_sz, 4)).copy()
        layers[li].rgba = rgba
        pos += need

    terrain.extra_layers_w = w
    terrain.extra_layers_h = h
    terrain.extra_layers_weights = weights
    terrain.extra_layers = layers


def _unpack_argb_rgb(c: int) -> Tuple[float, float, float]:
    return (((c >> 16) & 0xFF) / 255.0, ((c >> 8) & 0xFF) / 255.0, (c & 0xFF) / 255.0)


def _unpack_argb_rgba(c: int) -> Tuple[float, float, float, float]:
    return (((c >> 16) & 0xFF) / 255.0, ((c >> 8) & 0xFF) / 255.0, (c & 0xFF) / 255.0, ((c >> 24) & 0xFF) / 255.0)


_UNIT_CLASSES = frozenset({
    "MAMech", "MATank", "MAFlyer", "MAVehicle", "MATurret", "MAPerson",
    "MAUberBoss", "MAMiniSpider", "MASpider", "MAPowerUp", "MAFlagCTF",
    "MASnatchItBase", "MASnatchItDataCore", "MALevelStart",
})

_NON_PLACED_CLASSES = frozenset({
    "MARoot", "MAGroup", "MARoadGroup", "MATerrain", "MATerrainInfo", "MAEnvironment",
    "MASky2", "MASky", "MACloud", "MAPathDB", "MACultureSystem", "MACultureType",
    "MACultureGenerator", "MAWaypointGroup", "MAWaypoint", "MATriggerLogic",
    "MATriggerTimer", "MATriggerRadius", "MATriggerNationRadius", "MATriggerDestroyed",
    "MAActionResponse", "MAActionSound", "MAActionMoeEFX", "MAActionRemoveEntity",
    "MAActionCamera", "MAActionDropShip", "MAActionGameCondition", "MAActionPopupMenu",
    "MAActionAttribute", "MAActionMissionVariableSet", "MAActionNation", "MAActionShow",
    "MAActionAI", "MAAIGroup", "MAEvent", "MAPatrol", "MAAttack", "MADefend",
    "MAFollow", "MAOwnerController", "MAPlayer", "MAPlayerConfiguration",
    "MAPlayerConfigurationContainer", "MAObjectiveType", "MACameraPan",
    "MACameraSpiral", "MACameraThird", "MACameraFirst", "MACameraProperty",
    "MAFreeCameraController", "MechAssaultSettings", "MACinema", "MACollectionArea",
    "MAInput", "MAPrecipitation", "MADirectionalLight",
})


def load_map(
    source: AssetSource,
    wdf_path: str,
    catalog: Optional[TemplateCatalog] = None,
    generate_culture: bool = True,
    max_culture_instances: int = 12000,
) -> MapData:
    """Load a complete MechAssault 2 map (.wdf) from `source`."""
    if catalog is None:
        catalog = TemplateCatalog.from_source(source)

    raw_wdf = source.read(wdf_path)
    wdf = parse_wdf(raw_wdf)
    tree = WdfTree(wdf)
    map_data = MapData(name=file_stem(wdf_path), world_path=normalize_path(wdf_path))

    # 1. MATerrain
    k_terrain = wdf_key("MATerrain")
    t_obj = next((o for o in wdf.objs if o.cls == k_terrain), None)
    if t_obj is not None:
        td = TerrainData(
            min_h=t_obj.f32("TerrainMinHeight", -20.0),
            max_h=t_obj.f32("TerrainMaxHeight", 100.0),
            scale=t_obj.f32("TerrainWorldScale", 8.0),
            colormap_path=normalize_path(t_obj.str_val("TerrainBaseMap")),
            detail0_path=normalize_path(t_obj.str_val("TerrainDetailMap")),
            detail1_path=normalize_path(t_obj.str_val("TerrainDetailMap 1")),
            micro0_path=normalize_path(t_obj.str_val("TerrainMicroDetailMap0")),
            micro1_path=normalize_path(t_obj.str_val("TerrainMicroDetailMap1")),
            detail_mult=t_obj.f32("TerrainDetailMult", 64.0),
            detail_mult1=t_obj.f32("TerrainDetailMult1", 64.0),
            micro_tiling0=t_obj.f32("TerrainMicroDetailTiling0", 240.0),
            micro_tiling1=t_obj.f32("TerrainMicroDetailTiling1", 240.0),
            detail_blend=t_obj.f32("TerrainDetailBlend", 0.3),
            detail_modulate=(t_obj.i32("TerrainDetailModulate", 1) != 0),
        )
        if not (0.25 <= td.scale <= 1000.0):
            td.scale = 8.0
        hf_prop = t_obj.find("TerrainHeight Field")
        if hf_prop is not None:
            try:
                w, h, samples = decode_heightfield_samples8(hf_prop.data)
                td.w = w
                td.h = h
                # Stored row r -> gz = h - 1 - r
                flipped = samples[::-1, :].astype(np.float32)
                td.heights = td.min_h + (flipped / 255.0) * (td.max_h - td.min_h)
            except Exception as exc:
                map_data.warnings.append(f"Terrain heightfield decode failed: {exc}")

        if not td.colormap_path:
            for p in source.list_all("colormap"):
                if p.lower().startswith("terrain/"):
                    td.colormap_path = p
                    break
        if source.exists("terrain_layers.bin"):
            try:
                _decode_ma2l_ground_layers(source.read("terrain_layers.bin"), td)
            except Exception as exc:
                map_data.warnings.append(f"terrain_layers.bin decode failed: {exc}")
        map_data.terrain = td

    # 2. MAEnvironment
    k_env = wdf_key("MAEnvironment")
    env_obj = next((o for o in wdf.objs if o.cls == k_env), None)
    if env_obj is not None:
        env = map_data.environment
        env.ambient_color = _unpack_argb_rgb(env_obj.u32("LightAmbientColor", 0xFF202028))
        env.is_night = env_obj.i32("IsNightTime", 1) != 0
        env.sun_on = env_obj.i32("DirectionalLightOn", 1) != 0
        env.sun_color = _unpack_argb_rgb(env_obj.u32("DirectionalLightColor", 0xFF595959)) if env.sun_on else (0.0, 0.0, 0.0)
        if env_obj.find("DirectionalLightDirection"):
            env.sun_dir = env_obj.vec3("DirectionalLightDirection", (1.0, -1.0, 1.0))
        env.fog_color = _unpack_argb_rgb(env_obj.u32("FogColor", 0xFF808080))
        env.fog_start = env_obj.f32("FogStart", 0.0)
        env.fog_end = env_obj.f32("FogEnd", 0.0)
        env.fog_mode = env_obj.i32("FogMode", 1)
        env.fog_top = env_obj.f32("FogTop", 0.0)
        env.fog_bottom = env_obj.f32("FogBottom", 0.0)
        env.env_cube_path = normalize_path(env_obj.str_val("EnvironmentMapName"))
        env.lightmap_path = normalize_path(env_obj.str_val("LightMapName"))
        if not env.lightmap_path:
            for p in source.list_all("lightmap"):
                if p.lower().startswith("terrain/"):
                    env.lightmap_path = p
                    break

    # 3. MASky2
    k_sky = wdf_key("MASky2")
    sky_obj = next((o for o in wdf.objs if o.cls == k_sky), None)
    if sky_obj is not None:
        sky_model = normalize_path(sky_obj.str_val("Corporal3DObj"))
        if sky_model:
            sky = SkyData(
                model_path=sky_model,
                mtb_path=normalize_path(sky_obj.str_val("CorporalMTB")),
                scale=sky_obj.vec3("SkyTerrainSkyScale", (400.0, 400.0, 400.0)) if sky_obj.find("SkyTerrainSkyScale") else (400.0, 400.0, 400.0),
                offset=sky_obj.vec3("SkyTerrainSkyOffset", (0.0, 0.0, 0.0)),
                fog_on=(sky_obj.i32("SkyFogOn", 1) != 0 and sky_obj.find("SkyFogTop") is not None and sky_obj.find("SkyFogBottom") is not None),
                fog_color=_unpack_argb_rgb(sky_obj.u32("SkyFogColor", env_obj.u32("FogColor", 0xFF808080) if env_obj else 0xFF808080)),
                fog_top=sky_obj.f32("SkyFogTop", 0.0),
                fog_bottom=sky_obj.f32("SkyFogBottom", 0.0),
            )
            map_data.sky = sky

    # 4. Placed Entities & Point Lights
    road_model_cache: Dict[str, str] = {}
    k_root = wdf_key("MARoot")
    k_group = wdf_key("MAGroup")
    deg2rad = math.pi / 180.0

    def group_path_of(obj_idx: int) -> str:
        parts: List[str] = []
        p = tree.parent_of[obj_idx]
        while p >= 0 and wdf.objs[p].cls != k_root:
            if wdf.objs[p].cls == k_group:
                desc = wdf.objs[p].description.strip()
                if desc:
                    parts.append(desc)
            p = tree.parent_of[p]
        return "/".join(reversed(parts))

    for i, o in enumerate(wdf.objs):
        cls_name = o.class_name
        if cls_name == "MAPointLight":
            if o.i32("LightOn", 1) == 0:
                continue
            p_prop = o.find("CorporalPos") or o.find("ObjPosition") or o.find("Position")
            if p_prop is None:
                continue
            lcol = o.u32("LightColor", 0xFFFFFFFF)
            lpcol = o.u32("LightMapColor", lcol)
            map_data.entities.append(
                PlacedEntity(
                    uid=o.uid,
                    class_name=cls_name,
                    name=o.description or f"PointLight_{o.uid}",
                    group_path=group_path_of(i),
                    pos=p_prop.as_vec3(),
                    is_point_light=True,
                    light_color=_unpack_argb_rgb(lcol),
                    light_radius=o.f32("LightRadius", 10.0),
                    light_map=(o.i32("LightMap", 0) != 0),
                    light_map_color=_unpack_argb_rgb(lpcol),
                    light_map_radius=o.f32("LightMapRadius", 20.0),
                )
            )
            continue

        if cls_name in _NON_PLACED_CLASSES:
            continue

        is_road = cls_name == "MARoad"
        is_water = cls_name == "MAWater"
        tid = o.template_id
        tinfo = catalog.find(tid) if tid else None

        pos = None
        for pk in ("RoadPosition", "CorporalPos", "ObjPosition", "Position"):
            prop = o.find(pk)
            if prop is not None and len(prop.data) >= 12:
                pos = prop.as_vec3()
                break
        if pos is None:
            continue

        model_path = ""
        mtb_path = ""
        if is_road:
            rdef = normalize_path(o.str_val("RoadDefinition"))
            if rdef:
                if rdef in road_model_cache:
                    model_path = road_model_cache[rdef]
                elif source.exists(rdef):
                    mpath, _xr, _zr = parse_road_definition(source.read(rdef), rdef)
                    road_model_cache[rdef] = mpath
                    model_path = mpath
        else:
            model_path = normalize_path(o.str_val("Corporal3DObj"))
            mtb_path = normalize_path(o.str_val("CorporalMTB"))
            if not model_path and tinfo is not None:
                model_path = tinfo.model
                if not mtb_path:
                    mtb_path = tinfo.mtb

        # Orientations
        heading = 0.0
        pitch = 0.0
        roll = 0.0
        rrot = o.find("RoadRotation")
        if rrot is not None and len(rrot.data) >= 4:
            heading = struct.unpack_from("<f", rrot.data, 0)[0]
            if len(rrot.data) >= 12:
                pitch, roll = struct.unpack_from("<2f", rrot.data, 4)
        else:
            if o.find("CorporalHeading") is not None:
                heading = o.f32("CorporalHeading") * deg2rad
            elif o.find("StartOrientation") is not None:
                heading = o.f32("StartOrientation")
            elif o.find("VolumeHeading") is not None:
                heading = o.f32("VolumeHeading") * deg2rad
            if o.find("CorporalPitch") is not None:
                pitch = o.f32("CorporalPitch") * deg2rad
            if o.find("CorporalRoll") is not None:
                roll = o.f32("CorporalRoll") * deg2rad

        sc_prop = o.find("CorporalScale")
        scale_vec = sc_prop.as_vec3() if sc_prop is not None else (1.0, 1.0, 1.0)
        if any(abs(v) < 1e-6 for v in scale_vec):
            scale_vec = (1.0, 1.0, 1.0)

        custom_col = None
        if (
            o.i32("SupportsColorShift", 0)
            and o.i32("UseCustomColors", 0)
            and o.find("CustomSinglePlayerDiffuseColor") is not None
        ):
            cval = o.u32("CustomSinglePlayerDiffuseColor")
            if cval != 0xFFCC0000 and cls_name in ("MATank", "MATurret"):
                custom_col = _unpack_argb_rgba(cval | 0xFF000000)

        is_unit = cls_name in _UNIT_CLASSES or (tinfo is not None and tinfo.class_name in _UNIT_CLASSES)
        eff_class = cls_name if not cls_name.startswith("0x") else (tinfo.class_name if tinfo else cls_name)

        map_data.entities.append(
            PlacedEntity(
                uid=o.uid,
                class_name=eff_class,
                name=o.description or (tinfo.name if tinfo else f"{eff_class}_{o.uid}"),
                group_path=group_path_of(i),
                template_id=tid,
                model_path=model_path,
                mtb_path=mtb_path,
                pos=pos,
                heading=heading,
                pitch=pitch,
                roll=roll,
                scale=scale_vec,
                custom_color=custom_col,
                shadow_receiver=(is_road or o.i32("ShadowReceiverType", 0) == 1),
                visible=(o.i32("VisibleEntity", 1) != 0),
                is_road=is_road,
                is_water=is_water,
                is_unit=is_unit,
            )
        )

    # 5. Culture Scatter (MATerrainInfo + MACultureType + MACultureGenerator)
    if generate_culture and map_data.terrain is not None and map_data.terrain.heights is not None:
        _generate_culture(wdf, tree, catalog, map_data, max_culture_instances)

    return map_data


def _generate_culture(
    wdf: WdfFile,
    tree: WdfTree,
    catalog: TemplateCatalog,
    map_data: MapData,
    max_instances: int,
) -> None:
    k_tinfo = wdf_key("MATerrainInfo")
    k_ctype = wdf_key("MACultureType")
    k_cgen = wdf_key("MACultureGenerator")
    tinfo_obj = next((o for o in wdf.objs if o.cls == k_tinfo), None)
    if tinfo_obj is None:
        return
    cmap_prop = tinfo_obj.find(K_CULTURE_MAP)
    if cmap_prop is None:
        return
    try:
        cw, ch, cmap = decode_culture_map_u16(cmap_prop.data)
    except Exception:
        return

    terrain = map_data.terrain
    assert terrain is not None
    sx = terrain.size_x
    sz = terrain.size_z
    if sx <= 0.0 or sz <= 0.0:
        return

    layers = min(64, max(0, tinfo_obj.i32("culturenBitfields", 16)))
    count_total = 0

    for i, ty in enumerate(wdf.objs):
        if ty.cls != k_ctype:
            continue
        tname = ty.str_val("Name")
        if not tname:
            continue
        field_idx = -1
        for b in range(layers):
            if tinfo_obj.str_val(f"cultureBF{b}Name").lower() == tname.lower():
                field_idx = b
                break
        if field_idx < 0:
            continue

        bf = f"cultureBF{field_idx}"
        shift = tinfo_obj.u32(f"{bf}Shift", 0)
        nbits = min(15, max(1, tinfo_obj.i32(f"{bf}NBits", 1)))
        mask = tinfo_obj.u32(f"{bf}Mask", 0) & 0xFFFF
        if mask:
            lsb = (mask & -mask).bit_length() - 1
            shifted = mask >> lsb
            if (shifted & (shifted + 1)) == 0:
                shift = lsb
                nbits = min(15, max(1, shifted.bit_length()))
        top_val = (1 << nbits) - 1

        for k in tree.children[i]:
            g = wdf.objs[k]
            if g.cls != k_cgen:
                continue
            templ_uid = g.u32("GenerationTemplate", 0)
            tpl = catalog.find(templ_uid)
            if tpl is None or not tpl.model:
                continue

            density = g.f32("Density", 1000.0)
            gs_prop = g.find("GridScale")
            grid_x, grid_z = gs_prop.as_vec2() if gs_prop is not None else (10.0, 10.0)
            if not (grid_x >= 1.0):
                grid_x = 10.0
            if not (grid_z >= 1.0):
                grid_z = 10.0
            scale_range = min(0.95, max(0.0, g.f32("RandomScaleRange", 0.0)))
            random_rot = g.i32("RandomRotation", 1) != 0
            align = g.i32("AlignWithSlope", 1) != 0
            snap = g.i32("SnapToGrid", 0) != 0
            min_slope = g.f32("MinSlope", 0.0)
            max_slope = g.f32("MaxSlope", 90.0)

            cell_w = grid_x if snap else (sx / float(cw))
            cell_h = grid_z if snap else (sz / float(ch))
            per_cell = density * cell_w * cell_h * 1e-6
            if per_cell <= 0.0:
                continue

            nx_cells = int(math.floor(sx / cell_w))
            nz_cells = int(math.floor(sz / cell_h))
            for gz in range(nz_cells):
                cz = (gz + 0.5) * cell_h
                mz = min(ch - 1, max(0, int(cz / sz * ch)))
                for gx in range(nx_cells):
                    cx = (gx + 0.5) * cell_w
                    mx = min(cw - 1, max(0, int(cx / sx * cw)))
                    level = (int(cmap[mz, mx]) >> shift) & top_val
                    if level == 0:
                        continue
                    rng = _GameRng(((gx & 0xFFFFFFFF) << 16) ^ (gz & 0xFFFFFFFF))
                    n_f = per_cell * float(level) / float(top_val)
                    r0 = rng.range(0.0, 1.0)
                    cnt = int(n_f) + (1 if (n_f - float(int(n_f))) > r0 else 0)
                    if snap:
                        cnt = min(cnt, 1)
                    for _ in range(cnt):
                        if snap:
                            px = (gx + 0.001) * cell_w
                            pz = (gz + 0.001) * cell_h
                        else:
                            px = gx * cell_w + rng.range(0.001, 0.999) * cell_w
                            pz = gz * cell_h + rng.range(0.001, 0.999) * cell_h
                        nx, ny, nz = terrain.normal_at(px, pz)
                        slope = math.degrees(math.acos(min(1.0, max(-1.0, ny))))
                        if slope < min_slope or slope > max_slope:
                            continue
                        yaw = rng.range(0.0, math.tau) if random_rot else 0.0
                        sc = 1.0 + rng.range(-scale_range, scale_range)
                        cy, sy = math.cos(yaw), math.sin(yaw)
                        yx, yy, yz = 0.0, 1.0, 0.0
                        zx, zy, zz = sy, 0.0, cy
                        if align:
                            yx, yy, yz = nx, ny, nz
                            dot_zy = zx * yx + zz * yz
                            zx -= yx * dot_zy
                            zy -= yy * dot_zy
                            zz -= yz * dot_zy
                            zl = max(1e-4, math.sqrt(zx * zx + zy * zy + zz * zz))
                            zx /= zl
                            zy /= zl
                            zz /= zl
                        xx = yy * zz - yz * zy
                        xy = yz * zx - yx * zz
                        xz = yx * zy - yy * zx

                        py = terrain.height_at(px, pz)
                        map_data.culture_instances.append(
                            CultureInstance(
                                name=tname,
                                template_uid=templ_uid,
                                model_path=tpl.model,
                                mtb_path=tpl.mtb,
                                pos=(px, py, pz),
                                basis_x=(xx, xy, xz),
                                basis_y=(yx, yy, yz),
                                basis_z=(zx, zy, zz),
                                scale=sc,
                            )
                        )
                        count_total += 1
                        if count_total >= max_instances:
                            map_data.warnings.append(f"Culture scatter capped at {max_instances} instances")
                            return
