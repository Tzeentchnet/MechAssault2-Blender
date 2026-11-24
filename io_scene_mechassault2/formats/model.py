"""MechAssault 2 Model loader: .MGMODEL XML, legacy .NODE/.mesh/.mat INI, ANIMNODE, .anm, and .mtb rules."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import re
from typing import Dict, List, Optional, Tuple

import numpy as np

from .geometry import VertexBuffer, build_triangles, decode_index_file, decode_vertex_file
from .mgf import AssetSource, resolve_model_path
from .mgic_texture import parse_mgic_picture_header, decode_mgic_texture
from .text_utils import (
    IniSection,
    XmlNode,
    extension,
    file_stem,
    folder_of,
    normalize_path,
    parse_bool,
    parse_color_255,
    parse_floats,
    parse_ini,
    parse_xml,
    resolve_relative,
)
from .wdf import parse_wdf, wdf_key


class AlphaMode(Enum):
    OPAQUE = "opaque"
    CUTOUT = "cutout"
    BLEND = "blend"
    ADDITIVE = "additive"
    SUBTRACTIVE = "subtractive"


@dataclass
class TextureSlot:
    slot: str
    path: str
    found: bool = False
    tiling: str = ""
    frames: List[str] = field(default_factory=list)


@dataclass
class MaterialDef:
    name: str = ""
    mat_type: str = "material_base"
    diffuse_texture: str = ""
    diffuse_found: bool = False
    textures: List[TextureSlot] = field(default_factory=list)
    diffuse: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    selfillum: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    opacity: float = 1.0
    two_sided: bool = False
    alpha: AlphaMode = AlphaMode.OPAQUE
    blending: str = "normal"
    shading: str = "gouraud"
    mirror_u: bool = False
    mirror_v: bool = False
    colorshift0: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    colorshift1: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    colorshift1_diffuse: bool = False
    colorshift_bloom: Tuple[float, float] = (-1.0, -1.0)
    specular_bloom: float = -1.0
    spec_color: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    shin_strength: float = 1.0
    shininess: float = 16.0
    reflectivity: float = 0.0
    env_masked: bool = False
    envmap_type: str = ""
    detail_blend: str = ""
    detail_level: float = -1.0
    water: bool = False
    deep: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    shallow: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    fresnel_min: float = 0.77
    fresnel_max: float = 1.0
    distortion: bool = False


@dataclass
class NodeLight:
    spot: bool = False
    color: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    inner_radius: float = 0.0
    outer_radius: float = 0.0
    inner_angle: float = 0.0
    outer_angle: float = 0.0
    priority: int = 0
    has_lightmap: bool = False
    min_height: float = 0.0
    max_height: float = 0.0
    min_radius: float = 0.0
    max_radius: float = 0.0
    min_color: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    max_color: Tuple[float, float, float] = (1.0, 1.0, 1.0)


@dataclass
class NodeDef:
    name: str
    node_type: str
    parent: int
    local: Tuple[float, ...]  # 16 floats row-vector
    world: Tuple[float, ...]  # 16 floats row-vector
    turned_on: bool = True
    hidden_by_mtb: bool = False
    lod: int = 0
    is_light: bool = False
    light: Optional[NodeLight] = None
    bone_names: List[str] = field(default_factory=list)


@dataclass
class SubMeshDef:
    name: str
    node_name: str
    node_index: int
    material_index: int
    transform: Tuple[float, ...]  # 16 floats row-vector (= node.world)
    positions: np.ndarray         # (N, 3) float32
    normals: Optional[np.ndarray]
    uv0: Optional[np.ndarray]
    uv1: Optional[np.ndarray]
    colors: Optional[np.ndarray]
    bone_indices: Optional[np.ndarray]
    bone_weights: Optional[np.ndarray]
    triangles: np.ndarray         # (T, 3) uint32
    vertex_flags: int = 0
    skinned: bool = False
    shadow: bool = False
    visible: bool = True
    tint: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    bone_names: List[str] = field(default_factory=list)


@dataclass
class AnimClipRef:
    name: str
    path: str
    synch_point: float = 0.0
    loop_count: int = 0
    primary: bool = False


@dataclass
class ModelLoadOptions:
    include_shadow_meshes: bool = False
    include_invisible: bool = False
    all_lods: bool = False
    apply_mtb: bool = True
    mtb_path: str = ""
    check_texture_alpha: bool = True
    # A .MGMODEL names no skeleton: the .NODE wrapping it does. Borrow that wrapper's rig and clips.
    use_wrapping_node: bool = True


@dataclass
class ModelDef:
    requested_path: str = ""
    path: str = ""
    geometry_path: str = ""
    mtb_path: str = ""
    skeleton: str = ""
    # The ANIMNODE .NODE the skeleton and clips were taken from, when the model itself names none
    wrapper_path: str = ""
    animations: List[str] = field(default_factory=list)
    anim_clips: List[AnimClipRef] = field(default_factory=list)
    nodes: List[NodeDef] = field(default_factory=list)
    materials: List[MaterialDef] = field(default_factory=list)
    meshes: List[SubMeshDef] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    failed_meshes: int = 0


def _mat_identity() -> Tuple[float, ...]:
    return (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )


def _mat_mul(a: Tuple[float, ...], b: Tuple[float, ...]) -> Tuple[float, ...]:
    r = [0.0] * 16
    for i in range(4):
        ai0, ai1, ai2, ai3 = a[i * 4], a[i * 4 + 1], a[i * 4 + 2], a[i * 4 + 3]
        for j in range(4):
            r[i * 4 + j] = ai0 * b[j] + ai1 * b[4 + j] + ai2 * b[8 + j] + ai3 * b[12 + j]
    return tuple(r)


def _mat_from_srt(
    scale: Tuple[float, float, float],
    axis: Tuple[float, float, float],
    angle: float,
    pos: Tuple[float, float, float],
) -> Tuple[float, ...]:
    ax, ay, az = axis
    length = math.sqrt(ax * ax + ay * ay + az * az)
    if length < 1e-8:
        ax, ay, az, angle = 1.0, 0.0, 0.0, 0.0
    else:
        ax /= length
        ay /= length
        az /= length
    c = math.cos(angle)
    s = math.sin(angle)
    t = 1.0 - c
    rot = (
        (t * ax * ax + c, t * ax * ay - s * az, t * ax * az + s * ay),
        (t * ax * ay + s * az, t * ay * ay + c, t * ay * az - s * ax),
        (t * ax * az - s * ay, t * ay * az + s * ax, t * az * az + c),
    )
    out = [0.0] * 16
    for i in range(3):
        for j in range(3):
            out[i * 4 + j] = scale[i] * rot[j][i]
    out[12] = pos[0]
    out[13] = pos[1]
    out[14] = pos[2]
    out[15] = 1.0
    return tuple(out)


def _is_cutout_capable(mat: MaterialDef) -> bool:
    t = mat.mat_type.lower()
    return (
        t in ("material_base", "material_detail", "material_animated", "base", "detail", "animated")
        or mat.shading == "translucent"
    )


_GR2_RE = re.compile(r'([^\s"\'=<>]+\.gr2)', re.IGNORECASE)


def parse_animation_set(raw_text: bytes | str, anm_path: str) -> List[AnimClipRef]:
    """Parse an .anm animation set file to extract AnimClipRef entries."""
    if isinstance(raw_text, (bytes, bytearray)):
        text = raw_text.rstrip(b"\x00").decode("latin-1", errors="replace")
    else:
        text = raw_text.rstrip("\x00")
    clips: List[AnimClipRef] = []
    seen = set()

    # Try XML or INI first if structured
    stripped = text.lstrip()
    if stripped.startswith("<"):
        try:
            root = parse_xml(text)
            stack = [root]
            while stack:
                node = stack.pop()
                for k, v in node.attrs:
                    if v.lower().endswith(".gr2"):
                        p = resolve_relative(anm_path, v)
                        if p.lower() not in seen:
                            seen.add(p.lower())
                            name = node.attr_or("name", file_stem(p))
                            clips.append(AnimClipRef(name=name, path=p))
                for c in node.children:
                    stack.append(c)
        except Exception:
            pass

    if not clips:
        # Try INI style [animation] blocks or general .gr2 references
        for m in _GR2_RE.finditer(text):
            rel = m.group(1).strip()
            p = resolve_relative(anm_path, rel)
            if p.lower() not in seen:
                seen.add(p.lower())
                clips.append(AnimClipRef(name=file_stem(p), path=p))
    return clips


def node_animation_clip(section: IniSection, node_path: str) -> Optional[AnimClipRef]:
    """The clip an [animationN] section of a .NODE file declares, or None for any other section."""
    if not section.name.lower().startswith("animation"):
        return None
    rel = section.get("animation", "") or ""
    if not rel:
        return None
    path = resolve_relative(node_path, rel)
    sp = parse_floats(section.get("synch_point", "0") or "0", 1)
    lc = parse_floats(section.get("loop_count", "0") or "0", 1)
    return AnimClipRef(
        name=section.get("name", "") or file_stem(path),
        path=path,
        synch_point=sp[0] if sp else 0.0,
        loop_count=int(lc[0]) if lc else 0,
        primary=parse_bool(section.get("primary", "false") or "false", False),
    )


@dataclass
class AnimNodeInfo:
    """What a .NODE file's ANIMNODE sections declare: the skeleton, the models it animates, and its clips."""

    path: str
    skeleton: str = ""
    children: List[str] = field(default_factory=list)  # resolved against the .NODE, not checked to exist
    clips: List[AnimClipRef] = field(default_factory=list)


def read_anim_node(raw: bytes | str, node_path: str) -> AnimNodeInfo:
    """Read the ANIMNODE skeleton, child models and animation clips of the .NODE file `node_path`."""
    info = AnimNodeInfo(path=node_path)
    for s in parse_ini(raw):
        clip = node_animation_clip(s, node_path)
        if clip is not None:
            info.clips.append(clip)
            continue
        if (s.get("type", "") or "").lower() != "animnode":
            continue
        sk = s.get("skeleton", "") or ""
        if sk and not info.skeleton:
            info.skeleton = resolve_relative(node_path, sk)
        info.children.extend(resolve_relative(node_path, c) for c in s.get_all("child") if c)
    return info


def find_wrapping_nodes(source: AssetSource, model_path: str) -> List[AnimNodeInfo]:
    """The skeleton-bearing .NODE files whose ANIMNODE animates `model_path`, best match first.

    A mech's geometry (`NovaCat/Model/novacat.mgmodel`) names no skeleton or clips; the .NODE beside its
    folder (`NovaCat/novacat.NODE`) wraps it and names both. Wrappers sit in the model's own folder or its
    parent. Several may share one model (cinematic variants): prefer the one named like the model, then the
    one with the most clips.
    """
    canon = source.canonical_path(model_path) or normalize_path(model_path)
    target = canon.lower()
    folder = folder_of(canon)
    folders = {f.lower() for f in (folder, folder_of(folder)) if f}
    if not folders:
        return []

    found: List[AnimNodeInfo] = []
    seen: set[str] = set()
    for f in folders:
        for p in source.list_all(f"{f}/"):
            pl = p.lower()
            if pl in seen or pl == target or extension(p) != "node" or folder_of(p).lower() not in folders:
                continue
            seen.add(pl)
            try:
                info = read_anim_node(source.read(p), p)
            except Exception:
                continue
            if not info.skeleton:
                continue
            for child in info.children:
                resolved = resolve_model_path(source, child) or child
                if resolved.lower() == target:
                    found.append(info)
                    break

    stem = file_stem(canon).lower()
    found.sort(key=lambda w: (file_stem(w.path).lower() != stem, -len(w.clips), w.path.lower()))
    return found


class _ModelAssembler:
    def __init__(self, source: AssetSource, opts: ModelLoadOptions) -> None:
        self.source = source
        self.opts = opts
        self.model = ModelDef()
        self._alpha_cache: Dict[str, bool] = {}
        self._legacy_materials: Dict[str, int] = {}
        self._node_budget = 0
        self._file_depth = 0
        self._seen_anims: set[str] = set()

    def _find_texture(self, base: str, rel: str) -> str:
        p = resolve_relative(base, rel)
        if self.source.exists(p):
            return self.source.canonical_path(p)
        norm = normalize_path(rel)
        low = norm.lower()
        for prefix in ("texture_dump/", "envmaps/"):
            at = low.find(prefix)
            if at >= 0:
                sub = norm[at:]
                if self.source.exists(sub):
                    return self.source.canonical_path(sub)
        return p

    def _resolve_texture(self, base: str, rel: str, tiling: str = "") -> TextureSlot:
        path = self._find_texture(base, rel)
        found = self.source.exists(path)
        frames: List[str] = []
        if not found and path:
            ext = extension(path)
            stem = path[:-(len(ext) + 1)] if ext else path
            for k in range(1, 100):
                cand = f"{stem} {k:02d}" + (f".{ext}" if ext else "")
                if not self.source.exists(cand):
                    break
                frames.append(self.source.canonical_path(cand))
            if frames:
                path = frames[0]
                found = True
        return TextureSlot(slot="", path=path, found=found, tiling=tiling, frames=frames)

    def _texture_has_alpha(self, path: str) -> bool:
        if not path or not self.opts.check_texture_alpha or not self.source.exists(path):
            return False
        key = normalize_path(path).lower()
        if key in self._alpha_cache:
            return self._alpha_cache[key]
        has_alpha = False
        try:
            data = self.source.read(path)
            info = parse_mgic_picture_header(data)
            if info.fmt.value != "R5G6B5":
                tex = decode_mgic_texture(data, all_mips=False)
                has_alpha = tex.has_alpha
        except Exception:
            has_alpha = False
        self._alpha_cache[key] = has_alpha
        return has_alpha

    def _finish_material(self, mat: MaterialDef, alpha_kill: float) -> None:
        lt = mat.mat_type.lower()
        mat.distortion = lt in ("material_distort", "distort")
        for slot in mat.textures:
            if slot.slot in ("basetexture", "base"):
                mat.diffuse_texture = slot.path
                mat.diffuse_found = slot.found
                tl = slot.tiling.lower()
                mat.mirror_u = "u_mirror" in tl
                mat.mirror_v = "v_mirror" in tl
                break
        b = mat.blending.lower()
        if b == "alpha":
            mat.alpha = AlphaMode.BLEND
        elif b == "additive":
            mat.alpha = AlphaMode.ADDITIVE
        elif b == "subtractive":
            mat.alpha = AlphaMode.SUBTRACTIVE
        else:
            mat.alpha = AlphaMode.OPAQUE
            if (_is_cutout_capable(mat) or alpha_kill > 0) and mat.diffuse_found and self._texture_has_alpha(mat.diffuse_texture):
                mat.alpha = AlphaMode.CUTOUT
            if mat.opacity < 0.999:
                mat.alpha = AlphaMode.BLEND

        if mat.diffuse_texture and not mat.diffuse_found:
            self.model.warnings.append(f"missing texture {mat.diffuse_texture} (material {mat.name})")

    def _new_node(
        self,
        name: str,
        node_type: str,
        parent: int,
        local: Tuple[float, ...],
        turned_on: bool,
        lod: int,
    ) -> int:
        world = _mat_mul(local, self.model.nodes[parent].world) if parent >= 0 else local
        self.model.nodes.append(
            NodeDef(
                name=name,
                node_type=node_type,
                parent=parent,
                local=local,
                world=world,
                turned_on=turned_on,
                lod=lod,
            )
        )
        return len(self.model.nodes) - 1

    def _add_submesh(
        self,
        name: str,
        node_index: int,
        material_index: int,
        vdata: bytes,
        idata: bytes,
        is_strip: bool,
        shadow: bool,
        where: str,
        bone_names: Optional[List[str]] = None,
    ) -> None:
        try:
            vb: VertexBuffer = decode_vertex_file(vdata)
            idx = decode_index_file(idata)
        except Exception as exc:
            self.model.warnings.append(f"{where}: {exc}")
            self.model.failed_meshes += 1
            return

        tris = build_triangles(idx, is_strip, vb.count)
        node = self.model.nodes[node_index] if node_index >= 0 else None
        sm = SubMeshDef(
            name=name,
            node_name=node.name if node else "",
            node_index=node_index,
            material_index=material_index,
            transform=node.world if node else _mat_identity(),
            positions=vb.positions,
            normals=vb.normals,
            uv0=vb.uv0,
            uv1=vb.uv1,
            colors=vb.colors,
            bone_indices=vb.bone_indices,
            bone_weights=vb.bone_weights,
            triangles=tris,
            vertex_flags=vb.flags,
            skinned=vb.skinned,
            shadow=shadow,
            bone_names=list(bone_names or (node.bone_names if node else [])),
        )
        self.model.meshes.append(sm)

    def _add_anim_path(self, path: str, name: str = "", synch: float = 0.0, loops: int = 0, primary: bool = False) -> None:
        canon = self.source.canonical_path(path) if self.source.exists(path) else normalize_path(path)
        if not canon:
            return
        kl = canon.lower()
        if kl not in self._seen_anims:
            self._seen_anims.add(kl)
            self.model.animations.append(canon)
            self.model.anim_clips.append(
                AnimClipRef(
                    name=name or file_stem(canon),
                    path=canon,
                    synch_point=synch,
                    loop_count=loops,
                    primary=primary,
                )
            )

    def _add_clip(self, clip: AnimClipRef) -> None:
        self._add_anim_path(clip.path, clip.name, clip.synch_point, clip.loop_count, clip.primary)

    def _adopt_wrapping_node(self) -> None:
        """Take the skeleton and clips of the .NODE that wraps this model, when the model names none."""
        wrappers = find_wrapping_nodes(self.source, self.model.path)
        if not wrappers:
            return
        w = wrappers[0]
        self.model.wrapper_path = w.path
        self.model.skeleton = self.source.canonical_path(w.skeleton) or w.skeleton
        for clip in w.clips:
            self._add_clip(clip)

    def _read_animation_set(self, path: str) -> None:
        if not self.source.exists(path):
            return
        try:
            data = self.source.read(path)
            for clip in parse_animation_set(data, path):
                self._add_anim_path(clip.path, clip.name, clip.synch_point, clip.loop_count, clip.primary)
        except Exception:
            pass

    def _load_mgmodel(self, path: str, parent: int, lod: int) -> None:
        data = self.source.read(path)
        canon = self.source.canonical_path(path) or normalize_path(path)
        root = parse_xml(data)
        if not self.model.geometry_path:
            self.model.geometry_path = canon

        mat_map: Dict[str, int] = {}
        mesh_map: Dict[str, XmlNode] = {}

        for el in root.children:
            tag_low = el.tag.lower()
            if tag_low.startswith("material"):
                mat = MaterialDef()
                mat.name = el.attr_or("name")
                mat.mat_type = el.tag
                mat.diffuse = parse_color_255(el.attr_or("diffuse", "255,255,255"))
                mat.selfillum = parse_color_255(el.attr_or("selfillum", "0,0,0"), (0.0, 0.0, 0.0))
                op = parse_floats(el.attr_or("opacity", "1"), 1)
                if op:
                    mat.opacity = op[0]
                mat.two_sided = parse_bool(el.attr_or("two_sided", "false"), False)
                mat.shading = el.attr_or("shading").lower()
                mat.blending = el.attr_or("blending", "normal").lower()
                refl = parse_floats(el.attr_or("reflectivity", "0"), 1)
                if refl:
                    mat.reflectivity = refl[0]
                mat.envmap_type = el.attr_or("envmaptype").lower()
                mat.env_masked = mat.envmap_type == "masked"
                mat.colorshift0 = parse_color_255(el.attr_or("colorshift0_color", "0,0,0"), (0.0, 0.0, 0.0))
                mat.colorshift1 = parse_color_255(el.attr_or("colorshift1_color", "0,0,0"), (0.0, 0.0, 0.0))
                mat.colorshift1_diffuse = parse_bool(el.attr_or("colorshift1_diffuse", "false"), False)
                cb0 = parse_floats(el.attr_or("colorshift0_bloom", "-1"), 1)
                cb1 = parse_floats(el.attr_or("colorshift1_bloom", "-1"), 1)
                sb = parse_floats(el.attr_or("specular_bloom", "-1"), 1)
                mat.colorshift_bloom = (cb0[0] if cb0 else -1.0, cb1[0] if cb1 else -1.0)
                if sb:
                    mat.specular_bloom = sb[0]
                mat.spec_color = parse_color_255(el.attr_or("spec_color", "0,0,0"), (0.0, 0.0, 0.0))
                ss = parse_floats(el.attr_or("shin_strength", "1"), 1)
                if ss:
                    mat.shin_strength = ss[0]
                sh = parse_floats(el.attr_or("shininess", "16"), 1)
                if sh:
                    mat.shininess = sh[0]
                mat.detail_blend = el.attr_or("blend", el.attr_or("detail_blend")).lower()
                dl = parse_floats(el.attr_or("blend_level", el.attr_or("detail_blend_level", "-1")), 1)
                if dl:
                    mat.detail_level = dl[0]
                if "water" in tag_low:
                    mat.water = True
                    mat.deep = parse_color_255(el.attr_or("deep_color", "0,0,0"), (0.0, 0.0, 0.0))
                    mat.shallow = parse_color_255(el.attr_or("shallow_color", "0,0,0"), (0.0, 0.0, 0.0))
                    fmin = parse_floats(el.attr_or("fresnel_min", "0.77"), 1)
                    fmax = parse_floats(el.attr_or("fresnel_max", "1.0"), 1)
                    if fmin:
                        mat.fresnel_min = fmin[0]
                    if fmax:
                        mat.fresnel_max = fmax[0]

                for c in el.children:
                    fn = c.attr("filename")
                    if not fn:
                        continue
                    ts = self._resolve_texture(canon, fn, c.attr_or("tiling"))
                    ts.slot = c.tag.lower()
                    mat.textures.append(ts)

                ak_vals = parse_floats(el.attr_or("alpha_kill", "-1"), 1)
                ak = ak_vals[0] if ak_vals else -1.0
                self._finish_material(mat, ak)
                mat_map[mat.name.lower()] = len(self.model.materials)
                self.model.materials.append(mat)
            elif tag_low == "meshxml":
                mesh_map[el.attr_or("name").lower()] = el

        for el in root.children:
            if el.tag.lower().startswith("node_"):
                self._add_xml_node(el, canon, parent, lod, mat_map, mesh_map)

    def _add_xml_node(
        self,
        el: XmlNode,
        file_path: str,
        parent: int,
        lod: int,
        materials: Dict[str, int],
        meshes: Dict[str, XmlNode],
    ) -> None:
        self._node_budget += 1
        if self._node_budget > 20_000:
            return

        p_vals = parse_floats(el.attr_or("position"), 3)
        pos = (p_vals[0], p_vals[1], p_vals[2]) if len(p_vals) == 3 else (0.0, 0.0, 0.0)
        a_vals = parse_floats(el.attr_or("rot_axis"), 3)
        axis = (a_vals[0], a_vals[1], a_vals[2]) if len(a_vals) == 3 else (1.0, 0.0, 0.0)
        ang_vals = parse_floats(el.attr_or("rot_angle"), 1)
        angle = ang_vals[0] if ang_vals else 0.0
        s_vals = parse_floats(el.attr_or("scale"), 3)
        scale = (s_vals[0], s_vals[1], s_vals[2]) if len(s_vals) == 3 else (1.0, 1.0, 1.0)

        local = _mat_from_srt(scale, axis, angle, pos)
        turned_on = parse_bool(el.attr_or("turned_on", "true"), True)
        tag_low = el.tag.lower()
        ni = self._new_node(el.attr_or("name"), tag_low, parent, local, turned_on, lod)
        node = self.model.nodes[ni]

        if tag_low == "node_light":
            node.is_light = True
            light = NodeLight()
            light.spot = el.attr_or("light_type").lower() == "spot"
            light.color = parse_color_255(el.attr_or("light_color", "255,255,255"))
            for attr_name, field_name in (
                ("light_inner_radius", "inner_radius"),
                ("light_outer_radius", "outer_radius"),
                ("light_inner_angle", "inner_angle"),
                ("light_outer_angle", "outer_angle"),
            ):
                vals = parse_floats(el.attr_or(attr_name), 1)
                if vals:
                    setattr(light, field_name, vals[0])
            pr = parse_floats(el.attr_or("light_priority"), 1)
            if pr:
                light.priority = int(pr[0])
            lm = el.child("lightmap")
            if lm is not None:
                light.has_lightmap = True
                for attr_name, field_name in (
                    ("min_height", "min_height"),
                    ("max_height", "max_height"),
                    ("min_radius", "min_radius"),
                    ("max_radius", "max_radius"),
                ):
                    vals = parse_floats(lm.attr_or(attr_name), 1)
                    if vals:
                        setattr(light, field_name, vals[0])
                light.min_color = parse_color_255(lm.attr_or("min_color", "255,255,255"))
                light.max_color = parse_color_255(lm.attr_or("max_color", "255,255,255"))
            node.light = light

        if tag_low == "node_anim":
            sk = el.attr_or("skeleton")
            if sk:
                sk_path = resolve_relative(file_path, sk)
                if not self.model.skeleton:
                    self.model.skeleton = self.source.canonical_path(sk_path) or sk_path
            anm = el.attr_or("animation_set")
            if anm:
                anm_path = resolve_relative(file_path, anm)
                if anm_path.lower().endswith(".gr2"):
                    self._add_anim_path(anm_path)
                else:
                    self._read_animation_set(anm_path)

        # Collect optional bone list for skinned nodes
        for c in el.children:
            cl = c.tag.lower()
            if cl in ("bone", "bone_name", "skin_bone"):
                bn = c.attr_or("name", c.attr_or("bone"))
                if bn:
                    node.bone_names.append(bn)
            elif cl in ("bones", "bone_list"):
                for bc in c.children:
                    bn = bc.attr_or("name")
                    if bn:
                        node.bone_names.append(bn)

        for c in el.children:
            cl = c.tag.lower()
            shadow = cl == "shadow_mesh"
            if cl == "mesh" or shadow:
                if shadow and not self.opts.include_shadow_meshes:
                    continue
                mname = c.attr_or("name")
                mx = meshes.get(mname.lower())
                if mx is None:
                    self.model.warnings.append(f"{file_path}: node {node.name} uses unknown mesh {mname}")
                    continue
                vname = f"{file_path}{{{mx.attr_or('vertices')}}}"
                iname = f"{file_path}{{{mx.attr_or('indices')}}}"
                mat_idx = materials.get(mx.attr_or("material").lower(), -1)
                if not self.source.exists(vname) or not self.source.exists(iname):
                    self.model.warnings.append(f"missing mesh data: {vname} / {iname}")
                    self.model.failed_meshes += 1
                    continue
                vdata = self.source.read(vname)
                idata = self.source.read(iname)
                mtype = mx.attr_or("type").lower()
                self._add_submesh(
                    mx.attr_or("name"),
                    ni,
                    mat_idx,
                    vdata,
                    idata,
                    is_strip=(mtype == "indexedstrip"),
                    shadow=shadow,
                    where=vname,
                    bone_names=node.bone_names,
                )

        kids = el.child("children")
        if kids is not None:
            lod_switch = tag_low == "node_switch_lod"
            k = 0
            for c in kids.children:
                if not c.tag.lower().startswith("node_"):
                    continue
                self._add_xml_node(c, file_path, ni, max(lod, k) if lod_switch else lod, materials, meshes)
                k += 1

    # ------------------------------------------------------------------
    # Legacy MAXPort text set (.NODE / .mesh / .mat)
    # ------------------------------------------------------------------

    def _legacy_material(self, path: str) -> int:
        key = normalize_path(path).lower()
        if key in self._legacy_materials:
            return self._legacy_materials[key]
        index = -1
        if self.source.exists(path):
            canon = self.source.canonical_path(path) or path
            secs = parse_ini(self.source.read(path))
            for s in secs:
                if s.name.lower() != "material":
                    continue
                mat = MaterialDef()
                mat.name = s.get("name", file_stem(path)) or file_stem(path)
                mat.mat_type = s.get("type", "BASE") or "BASE"
                mat.diffuse = parse_color_255(s.get("diff_color", "255,255,255") or "255,255,255")
                mat.selfillum = parse_color_255(s.get("selfillum_color", "0,0,0") or "0,0,0", (0.0, 0.0, 0.0))
                mat.colorshift0 = parse_color_255(s.get("colorshift0_color", "0,0,0") or "0,0,0", (0.0, 0.0, 0.0))
                mat.colorshift1 = parse_color_255(s.get("colorshift1_color", "0,0,0") or "0,0,0", (0.0, 0.0, 0.0))
                mat.colorshift1_diffuse = parse_bool(s.get("colorshift1_diffuse", "false") or "false", False)
                cb0 = parse_floats(s.get("colorshift0_bloom", "-1") or "-1", 1)
                cb1 = parse_floats(s.get("colorshift1_bloom", "-1") or "-1", 1)
                sb = parse_floats(s.get("specular_bloom", "-1") or "-1", 1)
                mat.colorshift_bloom = (cb0[0] if cb0 else -1.0, cb1[0] if cb1 else -1.0)
                if sb:
                    mat.specular_bloom = sb[0]
                mat.spec_color = parse_color_255(s.get("spec_color", "0,0,0") or "0,0,0", (0.0, 0.0, 0.0))
                ss = parse_floats(s.get("shin_strength", "1") or "1", 1)
                if ss:
                    mat.shin_strength = ss[0]
                sh = parse_floats(s.get("shininess", "16") or "16", 1)
                if sh:
                    mat.shininess = sh[0]
                if mat.mat_type.lower() == "water":
                    mat.water = True
                    mat.deep = parse_color_255(s.get("deep_color", "0,0,0") or "0,0,0", (0.0, 0.0, 0.0))
                    mat.shallow = parse_color_255(s.get("shallow_color", "0,0,0") or "0,0,0", (0.0, 0.0, 0.0))
                    fmin = parse_floats(s.get("fresnel_min", "0.77") or "0.77", 1)
                    fmax = parse_floats(s.get("fresnel_max", "1") or "1", 1)
                    if fmin:
                        mat.fresnel_min = fmin[0]
                    if fmax:
                        mat.fresnel_max = fmax[0]
                op = parse_floats(s.get("opacity", "1") or "1", 1)
                if op:
                    mat.opacity = op[0]
                mat.two_sided = parse_bool(s.get("two_sided", "false") or "false", False)
                mat.shading = (s.get("shading", "") or "").lower()
                mat.blending = (s.get("blending", "normal") or "normal").lower()
                for ini_k, slot_k in (
                    ("base", "basetexture"),
                    ("detail", "detailtexture"),
                    ("multi", "multitexture"),
                    ("waves", "wavetexture"),
                    ("unique", "uniquetexture"),
                ):
                    v = s.get(ini_k)
                    if not v:
                        continue
                    ts = self._resolve_texture(canon, v, s.get(f"{ini_k}_tiling", "") or "")
                    ts.slot = slot_k
                    mat.textures.append(ts)
                ak_vals = parse_floats(s.get("alpha_kill", "-1") or "-1", 1)
                ak = ak_vals[0] if ak_vals else -1.0
                self._finish_material(mat, ak)
                index = len(self.model.materials)
                self.model.materials.append(mat)
                break
        else:
            self.model.warnings.append(f"missing material {path}")
        self._legacy_materials[key] = index
        return index

    def _legacy_mesh(self, mesh_path: str, node_index: int) -> None:
        if not self.source.exists(mesh_path):
            self.model.warnings.append(f"missing mesh {mesh_path}")
            self.model.failed_meshes += 1
            return
        canon = self.source.canonical_path(mesh_path) or mesh_path
        secs = parse_ini(self.source.read(mesh_path))
        mesh_sec = next((s for s in secs if s.name.lower() == "mesh"), None)
        if mesh_sec is None:
            self.model.warnings.append(f"no [mesh] in {mesh_path}")
            self.model.failed_meshes += 1
            return

        vpath = resolve_relative(canon, mesh_sec.get("vertices", "") or "")
        fpath = resolve_relative(canon, mesh_sec.get("faces", "") or "")
        mrel = mesh_sec.get("material", "") or ""
        mat_idx = self._legacy_material(resolve_relative(canon, mrel)) if mrel else -1

        if not self.source.exists(vpath) or not self.source.exists(fpath):
            self.model.warnings.append(f"missing geometry for {mesh_path}")
            self.model.failed_meshes += 1
            return
        vdata = self.source.read(vpath)
        idata = self.source.read(fpath)
        mtype = (mesh_sec.get("type", "") or "").lower()
        bone_names = mesh_sec.get_all("bone")
        self._add_submesh(
            mesh_sec.get("name", file_stem(mesh_path)) or file_stem(mesh_path),
            node_index,
            mat_idx,
            vdata,
            idata,
            is_strip=(mtype == "indexedstrip"),
            shadow=False,
            where=vpath,
            bone_names=bone_names,
        )

    def _load_node_file(self, path: str, parent: int, lod: int) -> None:
        self._file_depth += 1
        try:
            if self._file_depth > 8:
                raise ValueError(f"ANIMNODE nesting too deep at {path}")
            data = self.source.read(path)
            canon = self.source.canonical_path(path) or path
            all_secs = parse_ini(data)
            secs = []
            for s in all_secs:
                sl = s.name.lower()
                if sl == "fileinfo":
                    continue
                clip = node_animation_clip(s, canon)
                if clip is not None:
                    self._add_clip(clip)
                    continue
                secs.append(s)

            if not secs:
                raise ValueError(f"{path}: no node sections")

            if any(s.get("mesh") for s in secs) and not self.model.geometry_path:
                self.model.geometry_path = canon

            cursor = 0
            pending_anim_node = -1
            pending_children: List[str] = []

            def walk_section() -> None:
                nonlocal cursor, pending_anim_node, pending_children
                if cursor >= len(secs):
                    return
                self._node_budget += 1
                if self._node_budget > 20_000:
                    return
                s = secs[cursor]
                cursor += 1

                p_vals = parse_floats(s.get("position", "") or "", 3)
                pos = (p_vals[0], p_vals[1], p_vals[2]) if len(p_vals) == 3 else (0.0, 0.0, 0.0)
                a_vals = parse_floats(s.get("rot_axis", "") or "", 3)
                axis = (a_vals[0], a_vals[1], a_vals[2]) if len(a_vals) == 3 else (1.0, 0.0, 0.0)
                ang_vals = parse_floats(s.get("rot_angle", "") or "", 1)
                angle = ang_vals[0] if ang_vals else 0.0
                s_vals = parse_floats(s.get("scale", "") or "", 3)
                scale = (s_vals[0], s_vals[1], s_vals[2]) if len(s_vals) == 3 else (1.0, 1.0, 1.0)
                local = _mat_from_srt(scale, axis, angle, pos)
                ntype = s.get("type", "NODE") or "NODE"
                turned_on = parse_bool(s.get("turned_on", "true") or "true", True)
                ni = self._new_node(s.name, ntype, parent, local, turned_on, lod)

                if ntype.lower() == "animnode":
                    sk = s.get("skeleton", "") or ""
                    if sk:
                        skp = resolve_relative(canon, sk)
                        if not self.model.skeleton:
                            self.model.skeleton = self.source.canonical_path(skp) or skp
                    pending_children = [resolve_relative(canon, c) for c in s.get_all("child") if c]
                    pending_anim_node = ni
                    return

                bone_names = s.get_all("bone")
                if bone_names:
                    self.model.nodes[ni].bone_names.extend(bone_names)

                for m_rel in s.get_all("mesh"):
                    if m_rel:
                        self._legacy_mesh(resolve_relative(canon, m_rel), ni)

                k_vals = parse_floats(s.get("num_children", "0") or "0", 1)
                kids = int(k_vals[0]) if k_vals else 0
                for _ in range(kids):
                    if cursor >= len(secs):
                        break
                    # Recursive child under ni
                    _walk_child(ni)

            def _walk_child(cur_parent: int) -> None:
                nonlocal cursor, pending_anim_node, pending_children
                if cursor >= len(secs):
                    return
                self._node_budget += 1
                if self._node_budget > 20_000:
                    return
                s = secs[cursor]
                cursor += 1
                p_vals = parse_floats(s.get("position", "") or "", 3)
                pos = (p_vals[0], p_vals[1], p_vals[2]) if len(p_vals) == 3 else (0.0, 0.0, 0.0)
                a_vals = parse_floats(s.get("rot_axis", "") or "", 3)
                axis = (a_vals[0], a_vals[1], a_vals[2]) if len(a_vals) == 3 else (1.0, 0.0, 0.0)
                ang_vals = parse_floats(s.get("rot_angle", "") or "", 1)
                angle = ang_vals[0] if ang_vals else 0.0
                s_vals = parse_floats(s.get("scale", "") or "", 3)
                scale = (s_vals[0], s_vals[1], s_vals[2]) if len(s_vals) == 3 else (1.0, 1.0, 1.0)
                local = _mat_from_srt(scale, axis, angle, pos)
                ntype = s.get("type", "NODE") or "NODE"
                turned_on = parse_bool(s.get("turned_on", "true") or "true", True)
                ni = self._new_node(s.name, ntype, cur_parent, local, turned_on, lod)

                if ntype.lower() == "animnode":
                    sk = s.get("skeleton", "") or ""
                    if sk:
                        skp = resolve_relative(canon, sk)
                        if not self.model.skeleton:
                            self.model.skeleton = self.source.canonical_path(skp) or skp
                    pending_children = [resolve_relative(canon, c) for c in s.get_all("child") if c]
                    pending_anim_node = ni
                    return

                bone_names = s.get_all("bone")
                if bone_names:
                    self.model.nodes[ni].bone_names.extend(bone_names)

                for m_rel in s.get_all("mesh"):
                    if m_rel:
                        self._legacy_mesh(resolve_relative(canon, m_rel), ni)

                k_vals = parse_floats(s.get("num_children", "0") or "0", 1)
                kids = int(k_vals[0]) if k_vals else 0
                for _ in range(kids):
                    if cursor >= len(secs):
                        break
                    _walk_child(ni)

            while cursor < len(secs):
                pending_anim_node = -1
                pending_children = []
                walk_section()
                if pending_anim_node >= 0:
                    an = pending_anim_node
                    for c_path in pending_children:
                        resolved_child = resolve_model_path(self.source, c_path) or c_path
                        self._load_any(resolved_child, an, lod)
        finally:
            self._file_depth -= 1

    def _load_any(self, path: str, parent: int, lod: int) -> None:
        ext = extension(path)
        if ext == "mgmodel":
            self._load_mgmodel(path, parent, lod)
        elif ext == "node":
            self._load_node_file(path, parent, lod)
        else:
            raise ValueError(f"Unsupported model type: {path}")

    def _guess_mtb(self) -> str:
        candidates: List[str] = []

        def add_cand(folder: str, stem: str) -> None:
            if not stem:
                return
            prefix = f"{folder}/" if folder else ""
            candidates.append(f"{prefix}{stem}.mtb")
            if stem.lower().endswith("_low"):
                candidates.append(f"{prefix}{stem[:-4]}.mtb")

        top = self.model.path
        geo = self.model.geometry_path or self.model.path
        add_cand(folder_of(top), file_stem(top))
        add_cand(folder_of(geo), file_stem(geo))
        add_cand(folder_of(top), file_stem(geo))
        add_cand(folder_of(folder_of(geo)), file_stem(geo))
        for c in candidates:
            if self.source.exists(c):
                return self.source.canonical_path(c)
        return ""

    def _apply_mtb(self, mtb_path: str) -> None:
        if not self.source.exists(mtb_path):
            self.model.warnings.append(f"missing mtb {mtb_path}")
            return
        self.model.mtb_path = self.source.canonical_path(mtb_path) or mtb_path
        try:
            wdf = parse_wdf(self.source.read(mtb_path))
        except Exception as exc:
            self.model.warnings.append(f"{mtb_path}: {exc}")
            return

        k_heat = wdf_key("MAModMechHeatSink")
        k_name = wdf_key("SubObjectName")
        k_r = wdf_key("MinimumHeatColorRed")
        k_g = wdf_key("MinimumHeatColorGreen")
        k_b = wdf_key("MinimumHeatColorBlue")

        for o in wdf.objs:
            if o.cls != k_heat:
                continue
            pn, pr, pg, pb = o.find(k_name), o.find(k_r), o.find(k_g), o.find(k_b)
            if not (pn and pr and pg and pb):
                continue
            node_l = pn.as_string().lower()
            tint = (pr.as_u32() / 255.0, pg.as_u32() / 255.0, pb.as_u32() / 255.0)
            for sm in self.model.meshes:
                if sm.node_name.lower() == node_l:
                    sm.tint = tint

        k_state = wdf_key("MAModSubObjectState")
        k_flags = wdf_key("SubObjStateFlags")
        for o in wdf.objs:
            if o.cls != k_state:
                continue
            pn, pfl = o.find(k_name), o.find(k_flags)
            if not (pn and pfl) or not (pfl.as_u32() & 0x10):
                continue
            node_l = pn.as_string().lower()
            matching = [sm for sm in self.model.meshes if sm.node_name.lower() == node_l]
            if not matching:
                continue
            effect_only = all(
                sm.material_index >= 0
                and (
                    self.model.materials[sm.material_index].distortion
                    or self.model.materials[sm.material_index].alpha in (AlphaMode.ADDITIVE, AlphaMode.BLEND)
                )
                for sm in matching
            )
            if effect_only:
                for nd in self.model.nodes:
                    if nd.name.lower() == node_l:
                        nd.hidden_by_mtb = True

        k_show = wdf_key("MAModSubObjectShow")
        k_enable = wdf_key("Enable")
        k_min = wdf_key("MinDamagePercent")
        k_max = wdf_key("MaxDamagePercent")
        k_aband = wdf_key("Abandoned")
        k_tree = wdf_key("EnableTree")
        rules: Dict[str, Tuple[bool, bool]] = {}  # lower_name -> (visible_at_100, tree)
        for o in wdf.objs:
            if o.cls != k_show:
                continue
            pn = o.find(k_name)
            if pn is None:
                continue
            nl = pn.as_string().lower()
            vis_prev, tree_prev = rules.get(nl, (False, False))
            en = o.i32(k_enable, 1)
            lo = o.i32(k_min, 0)
            hi = o.i32(k_max, 100)
            ab = o.i32(k_aband, 0)
            tr = o.i32(k_tree, 0) != 0
            vis_now = bool(en and ab == 0 and lo <= 100 <= hi)
            rules[nl] = (vis_prev or vis_now, tree_prev or tr)

        for i, nd in enumerate(self.model.nodes):
            rule = rules.get(nd.name.lower())
            if rule is None or rule[0]:
                continue
            nd.hidden_by_mtb = True
            if rule[1]:
                for j in range(i + 1, len(self.model.nodes)):
                    p = self.model.nodes[j].parent
                    while p >= 0 and p != i:
                        p = self.model.nodes[p].parent
                    if p == i:
                        self.model.nodes[j].hidden_by_mtb = True

    def run(self, path: str) -> ModelDef:
        canon = resolve_model_path(self.source, path)
        if not canon:
            raise FileNotFoundError(f"Model not found: {path}")
        self.model.requested_path = path
        self.model.path = canon
        if canon.lower() != normalize_path(path).lower():
            self.model.warnings.append(f"resolved {path} as {canon}")

        self._load_any(canon, -1, 0)
        if self.opts.use_wrapping_node and not self.model.skeleton:
            self._adopt_wrapping_node()
        if self.opts.apply_mtb:
            mtb = normalize_path(self.opts.mtb_path) if self.opts.mtb_path else self._guess_mtb()
            if mtb:
                self._apply_mtb(mtb)

        kept: List[SubMeshDef] = []
        for sm in self.model.meshes:
            vis = not sm.shadow
            if 0 <= sm.node_index < len(self.model.nodes):
                nd = self.model.nodes[sm.node_index]
                if not nd.turned_on or nd.hidden_by_mtb:
                    vis = False
                if nd.lod > 0 and not self.opts.all_lods:
                    vis = False
            sm.visible = vis
            if vis or self.opts.include_invisible:
                kept.append(sm)
        self.model.meshes = kept
        return self.model


def load_model(source: AssetSource, path: str, options: Optional[ModelLoadOptions] = None) -> ModelDef:
    """Load and assemble a MechAssault 2 .MGMODEL or .NODE model from `source`."""
    opts = options or ModelLoadOptions()
    return _ModelAssembler(source, opts).run(path)
