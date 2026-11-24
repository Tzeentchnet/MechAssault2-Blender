"""Persistent disk-cached asset & map indexer for MechAssault 2 directories, MGFs, and Xbox ISOs."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from .map_wdf import MapEntry, discover_maps_from_common
from .mgf import MGFArchive, model_path_fallbacks
from .model import read_anim_node
from .text_utils import extension, file_name, file_stem, folder_of, normalize_path
from .xiso import XisoArchive, is_xiso_file

# 2: adds the Mechs category and gives Models entries that share a file name distinct labels
INDEX_SCHEMA_VERSION = 2

BROWSER_CATEGORIES = ("MAPS", "MECHS", "MODELS", "ANIMATIONS", "TEXTURES")

_MECHS_PREFIX = "models/mechs/"
#: Weight classes in the order the Mechs list shows them; other folders (bosses' parts etc.) follow
_MECH_CLASS_ORDER = ("light", "medium", "heavy", "assault", "boss", "powerarmor")


@dataclass
class CachedAssetItem:
    name: str
    path: str
    archive_name: str


@dataclass
class CachedMapItem:
    name: str
    display_name: str
    multiplayer: bool
    world_path: str
    resource_mgf: str
    ini_path: str = ""
    game_types: List[str] = field(default_factory=list)
    resource_present: bool = False


@dataclass
class IsoMgfSlice:
    name: str
    iso_path: str
    offset: int
    size: int


@dataclass
class SourceIndex:
    schema_version: int
    source_path: str
    source_kind: str  # "iso" | "dir" | "mgf"
    fingerprint: Dict[str, Any]
    scanned_at: float
    iso_partition_offset: int = 0
    iso_mgfs: List[IsoMgfSlice] = field(default_factory=list)
    maps: List[CachedMapItem] = field(default_factory=list)
    models: List[CachedAssetItem] = field(default_factory=list)
    animations: List[CachedAssetItem] = field(default_factory=list)
    textures: List[CachedAssetItem] = field(default_factory=list)
    mechs: List[CachedAssetItem] = field(default_factory=list)


@dataclass
class BrowserState:
    last_source: str = ""
    last_category: str = "MAPS"
    recent_sources: List[str] = field(default_factory=list)


def get_cache_dir(override_dir: Optional[str | os.PathLike[str]] = None) -> Path:
    """Resolve and ensure a persistent cache directory outside the workspace and .blend file."""
    if override_dir is not None:
        p = Path(override_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    env_dir = os.environ.get("MA2_CACHE_DIR", "").strip()
    if env_dir:
        p = Path(env_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    try:
        import bpy  # Optional outside Blender

        pkg_parts = (__package__ or "").split(".")
        if len(pkg_parts) >= 3 and pkg_parts[0] == "bl_ext":
            ext_pkg = ".".join(pkg_parts[:3])
            try:
                p = Path(bpy.utils.extension_path_user(ext_pkg, path="cache", create=True))
                p.mkdir(parents=True, exist_ok=True)
                return p
            except Exception:
                pass
        datafiles = bpy.utils.user_resource("DATAFILES")
        if datafiles:
            p = Path(datafiles) / "mechassault2_importer" / "cache"
            p.mkdir(parents=True, exist_ok=True)
            return p
    except ImportError:
        pass

    p = Path.home() / ".cache" / "mechassault2_importer"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_source_path(source_path: str | os.PathLike[str]) -> Path:
    return Path(source_path).expanduser().resolve()


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}_", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, separators=(",", ":"), ensure_ascii=True)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def state_file_path(cache_dir: Optional[Path] = None) -> Path:
    return get_cache_dir(cache_dir) / "state.json"


def index_file_for_source(source_path: str | os.PathLike[str], cache_dir: Optional[Path] = None) -> Path:
    resolved = str(_resolve_source_path(source_path))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return get_cache_dir(cache_dir) / f"index_{digest}.json"


def load_state(cache_dir: Optional[Path] = None) -> BrowserState:
    sp = state_file_path(cache_dir)
    if not sp.is_file():
        return BrowserState()
    try:
        raw = json.loads(sp.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return BrowserState()
        cat = str(raw.get("last_category", "MAPS"))
        if cat not in BROWSER_CATEGORIES:
            cat = "MAPS"
        recents = [str(x) for x in raw.get("recent_sources", []) if isinstance(x, str)]
        return BrowserState(
            last_source=str(raw.get("last_source", "")),
            last_category=cat,
            recent_sources=recents[:12],
        )
    except Exception:
        return BrowserState()


def save_state(state: BrowserState, cache_dir: Optional[Path] = None) -> None:
    sp = state_file_path(cache_dir)
    _write_json_atomic(sp, asdict(state))


def record_last_source(
    source_path: str | os.PathLike[str],
    category: Optional[str] = None,
    cache_dir: Optional[Path] = None,
) -> BrowserState:
    resolved = str(_resolve_source_path(source_path))
    st = load_state(cache_dir)
    st.last_source = resolved
    if category in BROWSER_CATEGORIES:
        st.last_category = category
    recents = [resolved] + [s for s in st.recent_sources if s != resolved]
    st.recent_sources = recents[:12]
    save_state(st, cache_dir)
    return st


def compute_source_fingerprint(source_path: str | os.PathLike[str]) -> Tuple[str, Dict[str, Any]]:
    p = _resolve_source_path(source_path)
    if p.is_file():
        st = p.stat()
        if is_xiso_file(p):
            return "iso", {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
        if p.suffix.lower() == ".mgf":
            return "mgf", {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
        raise ValueError(f"Unsupported file source (expected .mgf or Xbox .iso): {p}")
    if p.is_dir():
        mgf_stats = []
        for mp in sorted(p.glob("*.mgf"), key=lambda q: q.name.lower()):
            if mp.is_file():
                mst = mp.stat()
                mgf_stats.append([mp.name, int(mst.st_size), int(mst.st_mtime_ns)])
        return "dir", {"mgfs": mgf_stats}
    raise FileNotFoundError(f"Source does not exist: {p}")


def _index_from_dict(raw: Dict[str, Any]) -> SourceIndex:
    return SourceIndex(
        schema_version=int(raw["schema_version"]),
        source_path=str(raw["source_path"]),
        source_kind=str(raw["source_kind"]),
        fingerprint=dict(raw["fingerprint"]),
        scanned_at=float(raw["scanned_at"]),
        iso_partition_offset=int(raw.get("iso_partition_offset", 0)),
        iso_mgfs=[IsoMgfSlice(**s) for s in raw.get("iso_mgfs", [])],
        maps=[CachedMapItem(**m) for m in raw.get("maps", [])],
        models=[CachedAssetItem(**a) for a in raw.get("models", [])],
        animations=[CachedAssetItem(**a) for a in raw.get("animations", [])],
        textures=[CachedAssetItem(**a) for a in raw.get("textures", [])],
        mechs=[CachedAssetItem(**a) for a in raw.get("mechs", [])],
    )


def load_cached_index(
    source_path: str | os.PathLike[str],
    cache_dir: Optional[Path] = None,
) -> Optional[SourceIndex]:
    """Load cached index if it exists, matches schema, and matches current file/dir fingerprint."""
    try:
        resolved = _resolve_source_path(source_path)
        kind, fp_data = compute_source_fingerprint(resolved)
    except Exception:
        return None

    idx_path = index_file_for_source(resolved, cache_dir)
    if not idx_path.is_file():
        return None
    try:
        raw = json.loads(idx_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        if int(raw.get("schema_version", -1)) != INDEX_SCHEMA_VERSION:
            return None
        if raw.get("source_kind") != kind or raw.get("fingerprint") != fp_data:
            return None
        return _index_from_dict(raw)
    except Exception:
        return None


def _prioritize_mgf_names(items: List[Tuple[str, Any]]) -> List[Tuple[str, Any]]:
    base = [it for it in items if it[0].lower().startswith("base")]
    prio = [it for it in items if it[0].lower() in ("common.mgf", "templates.mgf")]
    rest = [it for it in items if it not in base and it not in prio]
    base.sort(key=lambda it: it[0].lower())
    prio.sort(key=lambda it: it[0].lower())
    rest.sort(key=lambda it: it[0].lower())
    return base + prio + rest


def _collect_archive_assets(
    ar: MGFArchive,
    ar_label: str,
    seen_models: set[str],
    seen_anims: set[str],
    seen_tex: set[str],
    seen_wdfs: set[str],
    models: List[CachedAssetItem],
    animations: List[CachedAssetItem],
    textures: List[CachedAssetItem],
    fallback_wdfs: List[CachedMapItem],
    anim_node_children: List[str],
) -> None:
    for entry in ar.entries:
        p = entry.path
        low = p.lower()
        if low.endswith((".mgmodel", ".node")):
            if low not in seen_models:
                seen_models.add(low)
                models.append(CachedAssetItem(name=file_stem(p), path=p, archive_name=ar_label))
                if low.endswith(".node"):
                    try:
                        anim_node_children.extend(read_anim_node(ar.read(p), p).children)
                    except Exception:
                        pass
        elif low.endswith((".gr2", ".anm")):
            if low not in seen_anims:
                seen_anims.add(low)
                animations.append(CachedAssetItem(name=file_stem(p), path=p, archive_name=ar_label))
        elif low.endswith(".tif"):
            if low not in seen_tex:
                seen_tex.add(low)
                textures.append(CachedAssetItem(name=file_stem(p), path=p, archive_name=ar_label))
        elif low.endswith(".wdf"):
            if low not in seen_wdfs:
                seen_wdfs.add(low)
                stem = file_stem(p)
                fallback_wdfs.append(
                    CachedMapItem(
                        name=stem,
                        display_name=stem,
                        multiplayer=False,
                        world_path=p,
                        resource_mgf=ar_label,
                        resource_present=True,
                    )
                )


def _wrapped_model_paths(model_paths: set[str], anim_node_children: List[str]) -> set[str]:
    """Lower-case paths of the models an ANIMNODE .NODE animates, matched the way the loader resolves them."""
    wrapped: set[str] = set()
    for child in anim_node_children:
        norm = normalize_path(child)
        for cand in [norm, *model_path_fallbacks(norm)]:
            if cand.lower() in model_paths:
                wrapped.add(cand.lower())
                break
    return wrapped


def mech_label(path: str) -> Tuple[Tuple[Any, ...], str]:
    """`(sort key, display name)` for a model under Models/Mechs, e.g. "[Heavy] NovaCat (low)".

    Folders are Models/Mechs/<class>/<mech>/...; the variant is the file stem without the mech's name.
    """
    rel = normalize_path(path).split("/")[2:]
    stem = file_stem(path)
    if len(rel) >= 3:
        group, mech = rel[0], rel[1]
    else:
        group, mech = "", (rel[0] if len(rel) == 2 else stem)
    s_low, m_low = stem.lower(), mech.lower()
    if s_low == m_low:
        variant = ""
    elif s_low.startswith(f"{m_low}_"):
        variant = stem[len(mech) + 1:]
    else:
        variant = stem
    name = f"[{group}] {mech}" if group else mech
    if variant:
        name += f" ({variant})"
    g_low = group.lower()
    order = _MECH_CLASS_ORDER.index(g_low) if g_low in _MECH_CLASS_ORDER else len(_MECH_CLASS_ORDER)
    return (order, g_low, m_low, variant != "", variant.lower(), path.lower()), name


def split_mech_models(
    models: List[CachedAssetItem],
    anim_node_children: List[str],
) -> Tuple[List[CachedAssetItem], List[CachedAssetItem]]:
    """Split `models` into `(mechs, everything else)`, one Mechs entry per importable variant.

    A mech's rigged entry point is its .NODE; the .MGMODEL that .NODE wraps would import the same parts, so
    it is left out. Mech models no .NODE wraps (explosion debris, powered-down set pieces) stay in.
    """
    wrapped = _wrapped_model_paths({m.path.lower() for m in models}, anim_node_children)
    mechs: List[Tuple[Tuple[Any, ...], CachedAssetItem]] = []
    rest: List[CachedAssetItem] = []
    for m in models:
        low = m.path.lower()
        if not low.startswith(_MECHS_PREFIX):
            rest.append(m)
            continue
        if low in wrapped:
            continue
        key, name = mech_label(m.path)
        mechs.append((key, CachedAssetItem(name=name, path=m.path, archive_name=m.archive_name)))
    mechs.sort(key=lambda kv: kv[0])
    out = [it for _k, it in mechs]
    disambiguate_names(out)
    return out, rest


def _variant_tag(path: str) -> str:
    return "NODE" if extension(path) == "node" else (file_name(folder_of(path)) or extension(path).upper())


def disambiguate_names(items: List[CachedAssetItem]) -> None:
    """Give items that share a display name distinct ones, e.g. "novacat (NODE)" / "novacat (model_low)"."""
    groups: Dict[str, List[CachedAssetItem]] = {}
    for it in items:
        groups.setdefault(it.name.lower(), []).append(it)
    for group in groups.values():
        if len(group) < 2:
            continue
        tags = [_variant_tag(it.path) for it in group]
        if len({t.lower() for t in tags}) < len(tags):
            # Same tag in different trees: name each by the folders that differ
            folders = [folder_of(it.path).split("/") for it in group]
            common = 0
            while all(len(f) > common for f in folders) and len({f[common].lower() for f in folders}) == 1:
                common += 1
            tags = [
                "/".join(f[common:]) + (" NODE" if extension(it.path) == "node" else "")
                for f, it in zip(folders, group)
            ]
        for it, tag in zip(group, tags):
            if tag.strip():
                it.name = f"{it.name} ({tag.strip()})"


def _convert_map_entries(entries: List[MapEntry]) -> List[CachedMapItem]:
    return [
        CachedMapItem(
            name=m.name,
            display_name=f"[{'MP' if m.multiplayer else 'SP'}] {m.name}",
            multiplayer=m.multiplayer,
            world_path=m.world_path,
            resource_mgf=m.resource_mgf,
            ini_path=m.ini_path,
            game_types=list(m.game_types),
            resource_present=m.resource_present,
        )
        for m in entries
    ]


def build_source_index(source_path: str | os.PathLike[str]) -> SourceIndex:
    """Scan an Xbox ISO, directory of MGFs, or single MGF and return a full SourceIndex."""
    resolved = _resolve_source_path(source_path)
    kind, fp_data = compute_source_fingerprint(resolved)

    models: List[CachedAssetItem] = []
    animations: List[CachedAssetItem] = []
    textures: List[CachedAssetItem] = []
    maps: List[CachedMapItem] = []
    fallback_wdfs: List[CachedMapItem] = []
    seen_m: set[str] = set()
    seen_a: set[str] = set()
    seen_t: set[str] = set()
    seen_w: set[str] = set()
    anim_node_children: List[str] = []
    iso_slices: List[IsoMgfSlice] = []
    iso_part_off = 0

    if kind == "iso":
        with XisoArchive(resolved) as xiso:
            iso_part_off = xiso.partition_offset
            mgf_entries = xiso.mgf_entries()
            iso_slices = [
                IsoMgfSlice(name=e.name, iso_path=e.path, offset=e.offset, size=e.size)
                for e in sorted(mgf_entries, key=lambda q: q.name.lower())
            ]
        existing_mgfs = {s.name.lower() for s in iso_slices}
        ordered = _prioritize_mgf_names([(s.name, s) for s in iso_slices])
        with open(resolved, "rb") as fp:
            for _nm, sl in ordered:
                with MGFArchive(
                    resolved,
                    base_offset=sl.offset,
                    slice_size=sl.size,
                    virtual_name=sl.name,
                    fp=fp,
                ) as ar:
                    if sl.name.lower() == "common.mgf" and not maps:
                        maps = _convert_map_entries(discover_maps_from_common(ar, existing_mgfs))
                    _collect_archive_assets(
                        ar,
                        sl.name,
                        seen_m,
                        seen_a,
                        seen_t,
                        seen_w,
                        models,
                        animations,
                        textures,
                        fallback_wdfs,
                        anim_node_children,
                    )
    elif kind == "mgf":
        with MGFArchive(resolved) as ar:
            if resolved.name.lower() == "common.mgf":
                maps = _convert_map_entries(discover_maps_from_common(ar, {resolved.name.lower()}))
            _collect_archive_assets(
                ar,
                resolved.name,
                seen_m,
                seen_a,
                seen_t,
                seen_w,
                models,
                animations,
                textures,
                fallback_wdfs,
                anim_node_children,
            )
    else:  # dir
        mgf_paths = sorted(resolved.glob("*.mgf"), key=lambda q: q.name.lower())
        existing_mgfs = {p.name.lower() for p in mgf_paths}
        ordered = _prioritize_mgf_names([(p.name, p) for p in mgf_paths])
        for _nm, mp in ordered:
            with MGFArchive(mp) as ar:
                if mp.name.lower() == "common.mgf" and not maps:
                    maps = _convert_map_entries(discover_maps_from_common(ar, existing_mgfs))
                _collect_archive_assets(
                    ar,
                    mp.name,
                    seen_m,
                    seen_a,
                    seen_t,
                    seen_w,
                    models,
                    animations,
                    textures,
                    fallback_wdfs,
                    anim_node_children,
                )

    if not maps and fallback_wdfs:
        fallback_wdfs.sort(key=lambda m: m.name.lower())
        maps = fallback_wdfs

    mechs, models = split_mech_models(models, anim_node_children)
    disambiguate_names(models)
    models.sort(key=lambda a: (a.name.lower(), a.path.lower()))
    animations.sort(key=lambda a: (a.name.lower(), a.path.lower()))
    textures.sort(key=lambda a: (a.name.lower(), a.path.lower()))

    return SourceIndex(
        schema_version=INDEX_SCHEMA_VERSION,
        source_path=str(resolved),
        source_kind=kind,
        fingerprint=fp_data,
        scanned_at=time.time(),
        iso_partition_offset=iso_part_off,
        iso_mgfs=iso_slices,
        maps=maps,
        models=models,
        animations=animations,
        textures=textures,
        mechs=mechs,
    )


def save_source_index(index: SourceIndex, cache_dir: Optional[Path] = None) -> Path:
    out_path = index_file_for_source(index.source_path, cache_dir)
    _write_json_atomic(out_path, asdict(index))
    return out_path


def scan_or_load_index(
    source_path: str | os.PathLike[str],
    force_rescan: bool = False,
    category: Optional[str] = None,
    cache_dir: Optional[Path] = None,
) -> Tuple[SourceIndex, bool]:
    """Return `(index, loaded_from_cache)` and persist index + browser state to disk."""
    resolved = _resolve_source_path(source_path)
    if not force_rescan:
        cached = load_cached_index(resolved, cache_dir=cache_dir)
        if cached is not None:
            record_last_source(resolved, category=category, cache_dir=cache_dir)
            return cached, True

    idx = build_source_index(resolved)
    save_source_index(idx, cache_dir=cache_dir)
    record_last_source(resolved, category=category, cache_dir=cache_dir)
    return idx, False


def clear_index_cache(
    source_path: Optional[str | os.PathLike[str]] = None,
    cache_dir: Optional[Path] = None,
) -> int:
    """Remove cached index file(s). Returns number of deleted files."""
    removed = 0
    if source_path is not None:
        p = index_file_for_source(source_path, cache_dir)
        if p.is_file():
            p.unlink()
            removed += 1
        return removed

    cdir = get_cache_dir(cache_dir)
    for f in cdir.glob("index_*.json"):
        try:
            f.unlink()
            removed += 1
        except Exception:
            pass
    sp = state_file_path(cdir)
    if sp.is_file():
        try:
            sp.unlink()
            removed += 1
        except Exception:
            pass
    return removed
