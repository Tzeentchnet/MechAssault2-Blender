"""Extension preferences for MechAssault 2 Importer."""
from __future__ import annotations

import bpy
from bpy.props import BoolProperty, FloatProperty, StringProperty


class MA2AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    game_files_dir: StringProperty(
        name="MechAssault 2 Source (Folder / .mgf / .iso)",
        description="Path to MechAssault 2 game_files directory, .mgf archive, or Xbox .iso / .xiso.iso image",
        subtype="FILE_PATH",
        default="",
    )
    default_scale: FloatProperty(
        name="Default Scale",
        description="Default world scale multiplier on import",
        default=1.0,
        min=0.001,
        max=1000.0,
    )
    pack_textures: BoolProperty(
        name="Pack Decoded Textures",
        description="Pack decoded MGIc textures into the .blend file automatically",
        default=True,
    )

    sync_scene_range: BoolProperty(
        name="Sync Scene Range to Clip",
        description=(
            "When another clip (action) is made active on an imported MechAssault 2 rig, set the scene "
            "frame range to that clip's length"
        ),
        default=True,
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.prop(self, "game_files_dir")
        layout.prop(self, "default_scale")
        layout.prop(self, "pack_textures")
        layout.prop(self, "sync_scene_range")


def get_preferences(context: bpy.types.Context | None = None) -> MA2AddonPreferences | None:
    ctx = context or bpy.context
    prefs = ctx.preferences.addons.get(__package__)
    return prefs.preferences if prefs is not None else None
