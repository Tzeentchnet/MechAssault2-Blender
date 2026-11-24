"""Blender 5.2 Import operators, FileHandlers, and MGF Archive Browser operators."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from .formats.gr2 import parse_gr2
from .formats.index_cache import (
    BROWSER_CATEGORIES,
    SourceIndex,
    clear_index_cache,
    load_cached_index,
    load_state,
    record_last_source,
    scan_or_load_index,
)
from .formats.map_wdf import TemplateCatalog, discover_maps, load_map
from .formats.mgf import (
    ArchiveSet,
    AssetSource,
    MGFArchive,
    infer_game_files_dir,
    open_game_archives,
)
from .formats.xiso import is_xiso_file
from .formats.mgic_texture import decode_mgic_texture
from .formats.model import ModelLoadOptions, load_model, parse_animation_set
from .formats.text_utils import file_name, file_stem, normalize_path
from .importers.animation_builder import build_action, build_armature
from .importers.map_builder import MapImportSettings, build_map_in_blender
from .importers.model_builder import ModelImportSettings, build_model_in_blender
from .importers.texture_builder import TextureLibrary, rgba_to_blender_image
from .preferences import get_preferences


def _build_asset_source_for_file(
    filepath: str,
    context: bpy.types.Context,
    level_mgf_hint: str = "",
) -> tuple[AssetSource, str, Optional[ArchiveSet]]:
    """Construct an AssetSource combining loose files near `filepath` and any configured/inferred MGF archives."""
    fp = Path(filepath).resolve()
    prefs = get_preferences(context)
    gf_dir: Optional[Path] = None
    if prefs and prefs.game_files_dir:
        cand = Path(bpy.path.abspath(prefs.game_files_dir))
        if cand.exists():
            gf_dir = cand
    if gf_dir is None:
        state = load_state()
        if state.last_source:
            cand = Path(state.last_source)
            if cand.exists():
                gf_dir = cand
    if gf_dir is None:
        gf_dir = infer_game_files_dir(fp)

    # Infer disk root if inside Models/ or Missions/ or Multiplay/ etc.
    disk_root = fp.parent
    rel_path = fp.name
    parts_low = [p.lower() for p in fp.parts]
    for anchor in ("models", "texture_dump", "envmaps", "missions", "multiplay", "roads", "specialeffects"):
        if anchor in parts_low:
            idx = len(parts_low) - 1 - parts_low[::-1].index(anchor)
            disk_root = Path(*fp.parts[:idx])
            rel_path = normalize_path(fp.relative_to(disk_root).as_posix())
            break

    aset: Optional[ArchiveSet] = None
    if gf_dir is not None:
        level_path: Optional[str] = level_mgf_hint or None
        if not level_path and gf_dir.is_dir():
            stem = fp.stem.lower()
            cand_mgf = gf_dir / f"{stem}.mgf"
            if cand_mgf.is_file():
                level_path = str(cand_mgf)
        iso_slices = None
        if gf_dir.is_file() and is_xiso_file(gf_dir):
            cached = load_cached_index(gf_dir)
            if cached is not None and cached.iso_mgfs:
                iso_slices = [(s.name, s.offset, s.size) for s in cached.iso_mgfs]
        aset = open_game_archives(gf_dir, level_mgf=level_path, iso_mgf_entries=iso_slices)

    return AssetSource(archive_set=aset, disk_root=disk_root), rel_path, aset


class IMPORT_SCENE_OT_ma2_model(bpy.types.Operator, ImportHelper):
    """Import a MechAssault 2 model (.MGMODEL or .NODE)"""

    bl_idname = "import_scene.mechassault2_model"
    bl_label = "Import MechAssault 2 Model"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".mgmodel"
    filter_glob: StringProperty(default="*.mgmodel;*.node;*.MGMODEL;*.NODE", options={"HIDDEN"})

    scale: FloatProperty(name="Scale", default=1.0, min=0.001, max=1000.0)
    import_materials: BoolProperty(name="Import Materials & Textures", default=True)
    import_skeleton: BoolProperty(name="Import Skeleton (.gr2)", default=True)
    import_animations: BoolProperty(name="Import Linked Animations", default=False)
    apply_mtb: BoolProperty(name="Apply .mtb Visibility Rules", default=True)
    include_shadow_meshes: BoolProperty(name="Include Shadow Meshes", default=False)
    all_lods: BoolProperty(name="Include All LODs", default=False)
    fps: FloatProperty(name="Animation FPS", default=30.0, min=1.0, max=240.0)
    pack_textures: BoolProperty(name="Pack Textures", default=True)

    def execute(self, context: bpy.types.Context) -> set[str]:
        source, rel_path, aset = _build_asset_source_for_file(self.filepath, context)
        try:
            opts = ModelLoadOptions(
                include_shadow_meshes=self.include_shadow_meshes,
                include_invisible=False,
                all_lods=self.all_lods,
                apply_mtb=self.apply_mtb,
            )
            mdef = load_model(source, rel_path, opts)
            tex_lib = TextureLibrary(source, pack=self.pack_textures)
            settings = ModelImportSettings(
                scale=self.scale,
                import_materials=self.import_materials,
                import_skeleton=self.import_skeleton,
                import_animations=self.import_animations,
                apply_mtb=self.apply_mtb,
                include_shadow_meshes=self.include_shadow_meshes,
                all_lods=self.all_lods,
                fps=self.fps,
                pack_textures=self.pack_textures,
            )
            root_obj = build_model_in_blender(mdef, source, tex_lib, settings)
            bpy.context.view_layer.objects.active = root_obj
            for w in mdef.warnings[:10]:
                self.report({"WARNING"}, w)
            self.report({"INFO"}, f"Imported {mdef.path} ({len(mdef.meshes)} sub-meshes){_rig_note(mdef)}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to import model: {exc}")
            return {"CANCELLED"}
        finally:
            if aset is not None:
                aset.close()


class IMPORT_SCENE_OT_ma2_animation(bpy.types.Operator, ImportHelper):
    """Import a MechAssault 2 Granny2 skeleton or animation (.gr2 / .anm)"""

    bl_idname = "import_scene.mechassault2_animation"
    bl_label = "Import MechAssault 2 Animation / Skeleton"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".gr2"
    filter_glob: StringProperty(default="*.gr2;*.anm;*.GR2;*.ANM", options={"HIDDEN"})

    scale: FloatProperty(name="Scale", default=1.0, min=0.001, max=1000.0)
    fps: FloatProperty(name="FPS", default=30.0, min=1.0, max=240.0)
    import_skeleton: BoolProperty(name="Create Armature if Skeleton Present", default=True)
    use_active_armature: BoolProperty(name="Apply to Active Armature", default=True)

    def execute(self, context: bpy.types.Context) -> set[str]:
        source, rel_path, aset = _build_asset_source_for_file(self.filepath, context)
        try:
            arm_obj: Optional[bpy.types.Object] = None
            if self.use_active_armature and context.active_object and context.active_object.type == "ARMATURE":
                arm_obj = context.active_object

            gr2_paths = []
            if rel_path.lower().endswith(".anm"):
                clips = parse_animation_set(source.read(rel_path), rel_path)
                gr2_paths = [(c.name, c.path) for c in clips]
            else:
                gr2_paths = [(file_stem(rel_path), rel_path)]

            actions_created = 0
            for clip_name, gpath in gr2_paths:
                if not source.exists(gpath):
                    continue
                gr2 = parse_gr2(source.read(gpath))
                skel = gr2.skeletons[0] if gr2.skeletons else None
                if arm_obj is None and self.import_skeleton and skel is not None:
                    arm_obj, _ = build_armature(skel, name=skel.name or clip_name, scale=self.scale)
                    context.view_layer.objects.active = arm_obj

                if arm_obj is not None:
                    for anim in gr2.animations:
                        build_action(
                            arm_obj,
                            anim,
                            skeleton=skel,
                            fps=self.fps,
                            scale=self.scale,
                            action_name=clip_name if len(gr2.animations) == 1 else anim.name,
                        )
                        actions_created += 1

            if arm_obj is None and actions_created == 0:
                self.report({"WARNING"}, "No skeleton or active armature found for animation")
                return {"CANCELLED"}
            self.report({"INFO"}, f"Imported {actions_created} animation(s)")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to import animation: {exc}")
            return {"CANCELLED"}
        finally:
            if aset is not None:
                aset.close()


class IMPORT_SCENE_OT_ma2_map(bpy.types.Operator, ImportHelper):
    """Import a MechAssault 2 map (.wdf)"""

    bl_idname = "import_scene.mechassault2_map"
    bl_label = "Import MechAssault 2 Map"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".wdf"
    filter_glob: StringProperty(default="*.wdf;*.WDF", options={"HIDDEN"})

    scale: FloatProperty(name="Scale", default=1.0, min=0.001, max=1000.0)
    import_terrain: BoolProperty(name="Import Terrain", default=True)
    import_entities: BoolProperty(name="Import Buildings, Props & Units", default=True)
    import_roads: BoolProperty(name="Import Roads", default=True)
    import_water: BoolProperty(name="Import Water", default=True)
    import_sky: BoolProperty(name="Import Sky Dome", default=True)
    import_lights: BoolProperty(name="Import Lights & World", default=True)
    import_culture: BoolProperty(name="Import Culture Scatter", default=True)
    max_culture_instances: bpy.props.IntProperty(name="Max Scatter Instances", default=6000, min=0, max=100000)
    pack_textures: BoolProperty(name="Pack Textures", default=True)

    def execute(self, context: bpy.types.Context) -> set[str]:
        source, rel_path, aset = _build_asset_source_for_file(self.filepath, context)
        try:
            catalog = TemplateCatalog.from_source(source)
            mdata = load_map(
                source,
                rel_path,
                catalog=catalog,
                generate_culture=self.import_culture,
                max_culture_instances=self.max_culture_instances,
            )
            settings = MapImportSettings(
                scale=self.scale,
                import_terrain=self.import_terrain,
                import_entities=self.import_entities,
                import_roads=self.import_roads,
                import_water=self.import_water,
                import_sky=self.import_sky,
                import_lights=self.import_lights,
                import_culture=self.import_culture,
                max_culture_instances=self.max_culture_instances,
                pack_textures=self.pack_textures,
            )
            build_map_in_blender(mdata, source, settings)
            for w in mdata.warnings[:10]:
                self.report({"WARNING"}, w)
            self.report({"INFO"}, f"Imported map {mdata.name} ({len(mdata.entities)} entities)")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to import map: {exc}")
            return {"CANCELLED"}
        finally:
            if aset is not None:
                aset.close()


class IMPORT_IMAGE_OT_ma2_texture(bpy.types.Operator, ImportHelper):
    """Import a MechAssault 2 MGIc texture (.tif)"""

    bl_idname = "import_image.mechassault2_texture"
    bl_label = "Import MechAssault 2 MGIc Texture (.tif)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".tif"
    filter_glob: StringProperty(default="*.tif;*.TIF", options={"HIDDEN"})
    pack_textures: BoolProperty(name="Pack Texture", default=True)

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            data = Path(self.filepath).read_bytes()
            tex = decode_mgic_texture(data, all_mips=False)
            rgba = tex.top()
            if rgba is None:
                raise ValueError("Decoded texture has no pixel data")
            img = rgba_to_blender_image(file_name(self.filepath), rgba, pack=self.pack_textures)
            self.report({"INFO"}, f"Imported {img.name} ({img.size[0]}x{img.size[1]}, {tex.info.fmt.value})")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to import MGIc texture: {exc}")
            return {"CANCELLED"}


# ---------------------------------------------------------------------------
# Sidebar MGF Archive Browser Operators
# ---------------------------------------------------------------------------

_RESTORING_BROWSER = False


def _rig_note(mdef) -> str:
    """Report suffix naming the .NODE a model's rig was borrowed from, if it was."""
    return f"; rig and clips from {mdef.wrapper_path}" if mdef.wrapper_path else ""


def _browser_archive_paths(context: bpy.types.Context) -> Optional[Path]:
    props = context.scene.ma2_browser
    if props.game_files_dir:
        p = Path(bpy.path.abspath(props.game_files_dir)).expanduser()
        if p.exists():
            return p
    prefs = get_preferences(context)
    if prefs and prefs.game_files_dir:
        p = Path(bpy.path.abspath(prefs.game_files_dir)).expanduser()
        if p.exists():
            return p
    return None


def populate_browser_from_index(props, idx: SourceIndex, from_cache: bool) -> None:
    """Populate `scene.ma2_browser.items` for the active category from a `SourceIndex`."""
    props.items.clear()
    props.active_index = 0
    cat = props.category

    if cat == "MAPS":
        for m in idx.maps:
            item = props.items.add()
            item.name = m.display_name or m.name
            item.path = m.world_path
            item.archive_name = m.resource_mgf
            item.kind = "MAPS"
            item.resource_missing = not m.resource_present
    else:
        records = {
            "MECHS": idx.mechs,
            "MODELS": idx.models,
            "ANIMATIONS": idx.animations,
            "TEXTURES": idx.textures,
        }.get(cat, [])
        for rec in records:
            item = props.items.add()
            item.name = rec.name
            item.path = rec.path
            item.archive_name = rec.archive_name
            item.kind = cat

    origin = "Cached" if from_cache else "Indexed"
    kind_lbl = {"iso": "ISO", "dir": "Folder", "mgf": "MGF"}.get(idx.source_kind, idx.source_kind)
    props.cache_status = f"{origin} {kind_lbl}: {len(props.items)} {cat.lower()}"


def restore_browser_from_cache(scene: Optional[bpy.types.Scene] = None) -> bool:
    """Restore browser source path, category, and cached items after Blender startup or File->New."""
    global _RESTORING_BROWSER
    if _RESTORING_BROWSER:
        return False
    scn = scene or getattr(bpy.context, "scene", None)
    if scn is None or not hasattr(scn, "ma2_browser"):
        return False

    props = scn.ma2_browser
    _RESTORING_BROWSER = True
    try:
        st = load_state()
        if not props.game_files_dir and st.last_source and Path(st.last_source).exists():
            props.game_files_dir = st.last_source
            if st.last_category in BROWSER_CATEGORIES:
                props.category = st.last_category
        elif not props.game_files_dir:
            prefs = get_preferences()
            if prefs and prefs.game_files_dir:
                cand = Path(bpy.path.abspath(prefs.game_files_dir)).expanduser()
                if cand.exists():
                    props.game_files_dir = str(cand)

        if not props.game_files_dir:
            return False
        target = Path(bpy.path.abspath(props.game_files_dir)).expanduser()
        if not target.exists():
            return False
        cached = load_cached_index(target)
        if cached is None:
            return False
        populate_browser_from_index(props, cached, from_cache=True)
        return True
    except Exception:
        return False
    finally:
        _RESTORING_BROWSER = False


def on_browser_category_changed(props, _context: bpy.types.Context) -> None:
    """Switch browser items from cached index when user toggles Maps/Models/Animations/Textures."""
    if _RESTORING_BROWSER:
        return
    if not props.game_files_dir:
        return
    target = Path(bpy.path.abspath(props.game_files_dir)).expanduser()
    if not target.exists():
        return
    cached = load_cached_index(target)
    if cached is None:
        return
    populate_browser_from_index(props, cached, from_cache=True)
    try:
        record_last_source(target, category=props.category)
    except Exception:
        pass


class MA2_OT_scan_archives(bpy.types.Operator):
    """Scan and persistently index a MechAssault 2 directory, .mgf archive, or Xbox .iso"""

    bl_idname = "ma2.scan_archives"
    bl_label = "Scan / Index"

    force_rescan: BoolProperty(
        name="Force Re-Index",
        description="Rebuild the cached index from scratch instead of loading from disk cache",
        default=False,
    )

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = context.scene.ma2_browser
        target = _browser_archive_paths(context)
        if target is None:
            self.report({"ERROR"}, "Set a valid game_files folder, .mgf archive, or Xbox .iso file first")
            return {"CANCELLED"}

        try:
            idx, from_cache = scan_or_load_index(
                target,
                force_rescan=self.force_rescan,
                category=props.category,
            )
            populate_browser_from_index(props, idx, from_cache=from_cache)
            tag = "Loaded cached index" if from_cache else "Indexed & cached"
            self.report({"INFO"}, f"{tag}: {len(props.items)} item(s)")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Scan failed: {exc}")
            return {"CANCELLED"}


class MA2_OT_clear_cache(bpy.types.Operator):
    """Clear the cached MechAssault 2 index files"""

    bl_idname = "ma2.clear_cache"
    bl_label = "Clear Cache"

    def execute(self, context: bpy.types.Context) -> set[str]:
        removed = clear_index_cache()
        props = context.scene.ma2_browser
        props.items.clear()
        props.active_index = 0
        props.cache_status = "Cache cleared"
        self.report({"INFO"}, f"Removed {removed} cached index file(s)")
        return {"FINISHED"}


class MA2_OT_import_browser_item(bpy.types.Operator):
    """Import the selected asset or map directly from indexed MGF archives or Xbox ISO"""

    bl_idname = "ma2.import_browser_item"
    bl_label = "Import Selected"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = context.scene.ma2_browser
        if not props.items or props.active_index < 0 or props.active_index >= len(props.items):
            self.report({"ERROR"}, "No item selected")
            return {"CANCELLED"}
        item = props.items[props.active_index]
        target = _browser_archive_paths(context)
        if target is None:
            self.report({"ERROR"}, "Set a valid source folder, .mgf, or Xbox .iso first")
            return {"CANCELLED"}

        aset = ArchiveSet()
        try:
            if target.is_file() and is_xiso_file(target):
                cached = load_cached_index(target)
                iso_slices = [(s.name, s.offset, s.size) for s in cached.iso_mgfs] if (cached and cached.iso_mgfs) else None
                aset = open_game_archives(
                    target,
                    level_mgf=item.archive_name or None,
                    iso_mgf_entries=iso_slices,
                )
                source = AssetSource(archive_set=aset)
            elif target.is_file():
                aset.add(target)
                source = AssetSource(archive_set=aset)
            else:
                aset = open_game_archives(target, level_mgf=item.archive_name or None)
                source = AssetSource(archive_set=aset)

            note = ""
            if item.kind == "MAPS":
                catalog = TemplateCatalog.from_source(source)
                mdata = load_map(
                    source,
                    item.path,
                    catalog=catalog,
                    generate_culture=props.import_culture,
                    max_culture_instances=props.max_culture_instances,
                )
                build_map_in_blender(
                    mdata,
                    source,
                    MapImportSettings(
                        scale=props.scale,
                        import_culture=props.import_culture,
                        max_culture_instances=props.max_culture_instances,
                        pack_textures=props.pack_textures,
                    ),
                )
            elif item.kind in ("MECHS", "MODELS"):
                mdef = load_model(source, item.path, ModelLoadOptions(apply_mtb=props.apply_mtb))
                note = _rig_note(mdef)
                tex_lib = TextureLibrary(source, pack=props.pack_textures)
                build_model_in_blender(
                    mdef,
                    source,
                    tex_lib,
                    ModelImportSettings(
                        scale=props.scale,
                        import_skeleton=props.import_skeleton,
                        import_animations=props.import_animations,
                        apply_mtb=props.apply_mtb,
                        pack_textures=props.pack_textures,
                    ),
                )
            elif item.kind == "ANIMATIONS":
                arm_obj = context.active_object if (context.active_object and context.active_object.type == "ARMATURE") else None
                gr2 = parse_gr2(source.read(item.path))
                skel = gr2.skeletons[0] if gr2.skeletons else None
                if arm_obj is None and skel is not None:
                    arm_obj, _ = build_armature(skel, name=skel.name or item.name, scale=props.scale)
                if arm_obj is not None:
                    for anim in gr2.animations:
                        build_action(arm_obj, anim, skeleton=skel, scale=props.scale, action_name=item.name)
            elif item.kind == "TEXTURES":
                tex = decode_mgic_texture(source.read(item.path), all_mips=False)
                rgba = tex.top()
                if rgba is not None:
                    rgba_to_blender_image(file_name(item.path), rgba, pack=props.pack_textures)

            self.report({"INFO"}, f"Imported {item.path}{note}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Import failed: {exc}")
            return {"CANCELLED"}
        finally:
            aset.close()


# ---------------------------------------------------------------------------
# Blender 5.2 Drag & Drop FileHandlers
# ---------------------------------------------------------------------------

class MA2_FH_model(bpy.types.FileHandler):
    bl_idname = "MA2_FH_model"
    bl_label = "MechAssault 2 Model"
    bl_import_operator = "import_scene.mechassault2_model"
    bl_file_extensions = ".mgmodel;.node"

    @classmethod
    def poll_drop(cls, context: bpy.types.Context) -> bool:
        return context.area is not None and context.area.type == "VIEW_3D"


class MA2_FH_animation(bpy.types.FileHandler):
    bl_idname = "MA2_FH_animation"
    bl_label = "MechAssault 2 Animation / Skeleton"
    bl_import_operator = "import_scene.mechassault2_animation"
    bl_file_extensions = ".gr2;.anm"

    @classmethod
    def poll_drop(cls, context: bpy.types.Context) -> bool:
        return context.area is not None and context.area.type == "VIEW_3D"


class MA2_FH_map(bpy.types.FileHandler):
    bl_idname = "MA2_FH_map"
    bl_label = "MechAssault 2 Map"
    bl_import_operator = "import_scene.mechassault2_map"
    bl_file_extensions = ".wdf"

    @classmethod
    def poll_drop(cls, context: bpy.types.Context) -> bool:
        return context.area is not None and context.area.type == "VIEW_3D"
