"""Keep the scene frame range in step with the clip shown on an imported MA2 rig.

Each imported clip is its own action. Picking another one (Action Editor, NLA tweak mode, Python)
would otherwise leave the scene range at the previous clip's length, so playback would stop early
or run on into empty frames.
"""
from __future__ import annotations

from typing import Dict

import bpy
from bpy.app.handlers import persistent

from .animation_builder import MA2_RIG_PROP, playback_end_frame

# Last action seen per rig object (by pointer), so only actual changes move the scene range
_last_action: Dict[int, int] = {}


def _sync_enabled() -> bool:
    try:
        addon = bpy.context.preferences.addons.get(__package__.rsplit(".", 1)[0])
        return addon is None or bool(getattr(addon.preferences, "sync_scene_range", True))
    except Exception:
        return True


def sync_scene_range(scene: bpy.types.Scene) -> bool:
    """Match `scene`'s frame range to any MA2 rig whose active action changed. Returns True if changed."""
    changed = False
    for obj in scene.objects:
        if obj.type != "ARMATURE" or not obj.get(MA2_RIG_PROP):
            continue
        adt = obj.animation_data
        action = adt.action if adt is not None else None
        key = obj.as_pointer()
        ptr = action.as_pointer() if action is not None else 0
        previous = _last_action.get(key)
        _last_action[key] = ptr
        if previous is None or previous == ptr or action is None or not action.use_frame_range:
            continue  # first sight (import / file load) keeps the current range
        start = int(round(action.frame_start))
        end = playback_end_frame(action)
        if (scene.frame_start, scene.frame_end) != (start, end):
            scene.frame_start = start
            scene.frame_end = end
            changed = True
        if not start <= scene.frame_current <= end:
            scene.frame_current = start
    return changed


@persistent
def _on_depsgraph_update(scene: bpy.types.Scene, _depsgraph=None) -> None:
    if _sync_enabled():
        sync_scene_range(scene)


@persistent
def _on_load_post(_dummy=None) -> None:
    _last_action.clear()


def register() -> None:
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister() -> None:
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    _last_action.clear()
