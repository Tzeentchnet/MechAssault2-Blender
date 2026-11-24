"""Blender 5.2 Sidebar UI Panel & UIList for browsing and importing MechAssault 2 MGF archives."""
from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)


def _on_category_update(self, context: bpy.types.Context) -> None:
    from .operators import on_browser_category_changed

    on_browser_category_changed(self, context)


class MA2BrowserItem(bpy.types.PropertyGroup):
    name: StringProperty(name="Name")
    path: StringProperty(name="Archive Path")
    archive_name: StringProperty(name="Archive")
    kind: StringProperty(name="Kind")
    resource_missing: BoolProperty(
        name="Level Archive Missing",
        description="This map's level .mgf is not present in the source; textures/props may be incomplete",
        default=False,
    )


class MA2BrowserSettings(bpy.types.PropertyGroup):
    game_files_dir: StringProperty(
        name="Source",
        description="Path to MechAssault 2 game_files directory, .mgf archive, or Xbox .iso / .xiso.iso image",
        subtype="FILE_PATH",
        default="",
    )
    category: EnumProperty(
        name="Category",
        items=[
            ("MAPS", "Maps", "Browse campaign and multiplayer maps"),
            ("MECHS", "Mechs", "Browse mechs and power armor, one entry per variant, rigged where the game rigs them"),
            ("MODELS", "Models", "Browse .mgmodel and .node models other than mechs"),
            ("ANIMATIONS", "Animations", "Browse .gr2 and .anm skeletons & animations"),
            ("TEXTURES", "Textures", "Browse MGIc .tif textures"),
        ],
        default="MAPS",
        update=_on_category_update,
    )
    cache_status: StringProperty(name="Cache Status", default="")
    search_filter: StringProperty(name="Filter", default="")
    items: CollectionProperty(type=MA2BrowserItem)
    active_index: IntProperty(default=0)

    scale: FloatProperty(name="Scale", default=1.0, min=0.001, max=1000.0)
    import_skeleton: BoolProperty(name="Import Skeleton", default=True)
    import_animations: BoolProperty(name="Import Animations", default=False)
    apply_mtb: BoolProperty(name="Apply .mtb Rules", default=True)
    import_culture: BoolProperty(name="Culture Scatter", default=True)
    max_culture_instances: IntProperty(name="Max Scatter", default=6000, min=0, max=100000)
    pack_textures: BoolProperty(name="Pack Textures", default=True)


class MA2_UL_asset_list(bpy.types.UIList):
    bl_idname = "MA2_UL_asset_list"

    def filter_items(self, context: bpy.types.Context, data, propname: str):
        items = getattr(data, propname)
        flt = context.scene.ma2_browser.search_filter.strip().lower()
        flags = []
        for item in items:
            if not flt or flt in item.name.lower() or flt in item.path.lower():
                flags.append(self.bitflag_filter_item)
            else:
                flags.append(0)
        return flags, []

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index=0):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            row.label(text=item.name, icon="ERROR" if item.resource_missing else "NONE")
            if item.resource_missing:
                row.label(text=f"{item.archive_name} (missing)")
            elif item.archive_name:
                row.label(text=item.archive_name, icon="PACKAGE")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text=item.name)


class MA2_PT_browser_panel(bpy.types.Panel):
    bl_label = "MechAssault 2 Browser"
    bl_idname = "MA2_PT_browser_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MechAssault 2"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.ma2_browser

        col = layout.column(align=True)
        col.prop(props, "game_files_dir", text="Source")
        row = col.row(align=True)
        row.prop(props, "category", expand=True)
        scan_row = col.row(align=True)
        op_scan = scan_row.operator("ma2.scan_archives", text="Scan / Index", icon="VIEWZOOM")
        op_scan.force_rescan = False
        op_re = scan_row.operator("ma2.scan_archives", text="", icon="FILE_REFRESH")
        op_re.force_rescan = True
        scan_row.operator("ma2.clear_cache", text="", icon="TRASH")
        if props.cache_status:
            col.label(text=props.cache_status, icon="DISC")

        layout.prop(props, "search_filter", text="", icon="VIEWZOOM")
        layout.template_list(
            "MA2_UL_asset_list",
            "",
            props,
            "items",
            props,
            "active_index",
            rows=7,
        )

        if props.items and 0 <= props.active_index < len(props.items):
            sel = props.items[props.active_index]
            layout.label(text=sel.path, icon="FILE")

        box = layout.box()
        box.label(text="Import Options")
        box.prop(props, "scale")
        if props.category in ("MECHS", "MODELS"):
            box.prop(props, "import_skeleton")
            box.prop(props, "import_animations")
            box.prop(props, "apply_mtb")
        elif props.category == "MAPS":
            box.prop(props, "import_culture")
            if props.import_culture:
                box.prop(props, "max_culture_instances")
        box.prop(props, "pack_textures")

        layout.operator("ma2.import_browser_item", icon="IMPORT")
