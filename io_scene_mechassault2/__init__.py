"""MechAssault 2: Lone Wolf Importer Extension for Blender 5.2."""
from __future__ import annotations

try:
    import bpy
    from bpy.props import PointerProperty
except ImportError:  # Allow importing pure-Python submodules (e.g. .formats) outside Blender
    bpy = None  # type: ignore[assignment]
    PointerProperty = None  # type: ignore[assignment]

if bpy is not None:
    from bpy.app.handlers import persistent

    from .operators import (
        IMPORT_IMAGE_OT_ma2_texture,
        IMPORT_SCENE_OT_ma2_animation,
        IMPORT_SCENE_OT_ma2_map,
        IMPORT_SCENE_OT_ma2_model,
        MA2_FH_animation,
        MA2_FH_map,
        MA2_FH_model,
        MA2_OT_clear_cache,
        MA2_OT_import_browser_item,
        MA2_OT_scan_archives,
        restore_browser_from_cache,
    )
    from .preferences import MA2AddonPreferences
    from .ui import MA2BrowserItem, MA2BrowserSettings, MA2_PT_browser_panel, MA2_UL_asset_list


    @persistent
    def _on_load_post(_dummy=None) -> None:
        restore_browser_from_cache()


    def _startup_restore_timer():
        restore_browser_from_cache()
        return None


    def _menu_func_import_model(self, context: bpy.types.Context) -> None:
        self.layout.operator(IMPORT_SCENE_OT_ma2_model.bl_idname, text="MechAssault 2 Model (.mgmodel / .node)")


    def _menu_func_import_anim(self, context: bpy.types.Context) -> None:
        self.layout.operator(IMPORT_SCENE_OT_ma2_animation.bl_idname, text="MechAssault 2 Animation / Skeleton (.gr2 / .anm)")


    def _menu_func_import_map(self, context: bpy.types.Context) -> None:
        self.layout.operator(IMPORT_SCENE_OT_ma2_map.bl_idname, text="MechAssault 2 Map (.wdf)")


    def _menu_func_import_tex(self, context: bpy.types.Context) -> None:
        self.layout.operator(IMPORT_IMAGE_OT_ma2_texture.bl_idname, text="MechAssault 2 Texture (.tif)")


    _CLASSES = (
        MA2AddonPreferences,
        MA2BrowserItem,
        MA2BrowserSettings,
        IMPORT_SCENE_OT_ma2_model,
        IMPORT_SCENE_OT_ma2_animation,
        IMPORT_SCENE_OT_ma2_map,
        IMPORT_IMAGE_OT_ma2_texture,
        MA2_OT_scan_archives,
        MA2_OT_clear_cache,
        MA2_OT_import_browser_item,
        MA2_FH_model,
        MA2_FH_animation,
        MA2_FH_map,
        MA2_UL_asset_list,
        MA2_PT_browser_panel,
    )
else:
    _CLASSES = ()


def register() -> None:
    if bpy is None:
        return
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ma2_browser = PointerProperty(type=MA2BrowserSettings)
    bpy.types.TOPBAR_MT_file_import.append(_menu_func_import_model)
    bpy.types.TOPBAR_MT_file_import.append(_menu_func_import_anim)
    bpy.types.TOPBAR_MT_file_import.append(_menu_func_import_map)
    bpy.types.TOPBAR_MT_file_import.append(_menu_func_import_tex)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    try:
        restore_browser_from_cache()
    except Exception:
        pass
    try:
        if not bpy.app.timers.is_registered(_startup_restore_timer):
            bpy.app.timers.register(_startup_restore_timer, first_interval=0.05)
    except Exception:
        pass


def unregister() -> None:
    if bpy is None:
        return
    try:
        if bpy.app.timers.is_registered(_startup_restore_timer):
            bpy.app.timers.unregister(_startup_restore_timer)
    except Exception:
        pass
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_func_import_tex)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_func_import_map)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_func_import_anim)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_func_import_model)
    if hasattr(bpy.types.Scene, "ma2_browser"):
        del bpy.types.Scene.ma2_browser
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
