"""Blender 5.2 Map builder: Terrain mesh + shader, Sky, Water, Roads, Buildings, Units, Culture, Lights & World."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict, Optional

import bpy
import numpy as np
from mathutils import Matrix, Vector

from ..formats.map_wdf import MapData, TerrainData
from ..formats.mgf import AssetSource
from ..formats.model import ModelDef, ModelLoadOptions, load_model
from ..formats.text_utils import file_stem, normalize_path
from .coord import SWAP_YZ, entity_matrix_to_blender, pos_to_blender
from .material_builder import build_terrain_material, new_mix_rgba
from .model_builder import ModelBuildCache, ModelImportSettings, build_model_in_blender
from .texture_builder import TextureLibrary


@dataclass
class MapImportSettings:
    scale: float = 1.0
    import_terrain: bool = True
    import_entities: bool = True
    import_roads: bool = True
    import_water: bool = True
    import_sky: bool = True
    import_lights: bool = True
    import_culture: bool = True
    max_culture_instances: int = 8000
    pack_textures: bool = True


def _ensure_collection(name: str, parent: bpy.types.Collection) -> bpy.types.Collection:
    col = bpy.data.collections.new(name)
    parent.children.link(col)
    return col


def _build_terrain_object(
    terrain: TerrainData,
    tex_lib: TextureLibrary,
    lightmap_path: str,
    scale: float,
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    w, h = terrain.w, terrain.h
    heights = terrain.heights
    assert heights is not None

    # Grid vertices: (gz, gx) -> Blender (gx * cell, gz * cell, heights[gz, gx])
    gx = np.arange(w, dtype=np.float32) * (terrain.scale * scale)
    gz = np.arange(h, dtype=np.float32) * (terrain.scale * scale)
    grid_x, grid_z = np.meshgrid(gx, gz)  # (h, w)
    grid_y = heights.astype(np.float32) * scale

    verts = np.stack((grid_x, grid_z, grid_y), axis=-1).reshape((-1, 3))

    # UVs: u = gx / (w - 1), v = gz / (h - 1) -> Blender bottom-up V matches 1 - (1 - gz/(h-1)) = gz/(h-1)?
    # Wait: stored row 0 is z = (h-1)*scale (north edge, colormap row 0, V_d3d = 0 -> V_bl = 1).
    # Stored row h-1 is z = 0 (south edge, colormap bottom, V_d3d = 1 -> V_bl = 0).
    # Since gz=0 is south (z=0), V_bl = gz / (h - 1), which is 0 at south and 1 at north!
    u_vals = np.linspace(0.0, 1.0, w, dtype=np.float32)
    v_vals = np.linspace(0.0, 1.0, h, dtype=np.float32)
    uv_x, uv_y = np.meshgrid(u_vals, v_vals)
    uvs = np.stack((uv_x, uv_y), axis=-1).reshape((-1, 2))

    # Diagonal split matching terrain.cpp::height_at: (gx, gz)-(gx+1, gz+1)
    # In Blender Z-up (x=gx, y=gz):
    # Tri1: (gx, gz), (gx+1, gz), (gx+1, gz+1) -> CCW upward (+Z)
    # Tri2: (gx, gz), (gx+1, gz+1), (gx, gz+1) -> CCW upward (+Z)
    iy, ix = np.meshgrid(np.arange(h - 1, dtype=np.uint32), np.arange(w - 1, dtype=np.uint32), indexing="ij")
    v00 = (iy * w + ix).ravel()
    v10 = (iy * w + (ix + 1)).ravel()
    v01 = ((iy + 1) * w + ix).ravel()
    v11 = ((iy + 1) * w + (ix + 1)).ravel()

    tri1 = np.stack((v00, v10, v11), axis=1)
    tri2 = np.stack((v00, v11, v01), axis=1)
    tris = np.concatenate((tri1, tri2), axis=0)

    tcount = len(tris)
    loop_count = tcount * 3
    mesh = bpy.data.meshes.new("MA2_Terrain_Mesh")
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", verts.ravel())
    mesh.loops.add(loop_count)
    mesh.loops.foreach_set("vertex_index", tris.ravel())
    mesh.polygons.add(tcount)
    mesh.polygons.foreach_set("loop_start", np.arange(0, loop_count, 3, dtype=np.int32))
    mesh.polygons.foreach_set("loop_total", np.full(tcount, 3, dtype=np.int32))
    mesh.polygons.foreach_set("use_smooth", np.ones(tcount, dtype=bool))

    uv_layer = mesh.uv_layers.new(name="UVMap")
    uv_layer.data.foreach_set("uv", uvs[tris.ravel()].ravel())
    mesh.update()

    mat = build_terrain_material(terrain, tex_lib, lightmap_path=lightmap_path)
    mesh.materials.append(mat)

    obj = bpy.data.objects.new("Terrain", mesh)
    collection.objects.link(obj)
    return obj


def _setup_world_environment(map_data: MapData, tex_lib: TextureLibrary, light_col: bpy.types.Collection, scale: float) -> None:
    env = map_data.environment

    # World shader (ambient + optional cubemap)
    world = bpy.context.scene.world
    if world is None:
        world = bpy.data.worlds.new(f"{map_data.name}_World")
        bpy.context.scene.world = world
    if hasattr(world, "use_nodes") and not world.use_nodes:
        world.use_nodes = True

    nt = world.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()
    wout = nodes.new("ShaderNodeOutputWorld")
    wout.location = (400, 0)
    bg = nodes.new("ShaderNodeBackground")
    bg.location = (150, 0)
    bg.inputs["Color"].default_value = (*env.ambient_color, 1.0)
    bg.inputs["Strength"].default_value = 1.0
    links.new(bg.outputs["Background"], wout.inputs["Surface"])

    if env.env_cube_path:
        cube_img = tex_lib.get_image(env.env_cube_path, "sRGB", as_equirect_cube=True)
        if cube_img is not None:
            env_node = nodes.new("ShaderNodeTexEnvironment")
            env_node.image = cube_img
            env_node.location = (-180, 0)
            _mb, fac_b, in_a_b, in_b_b, out_b = new_mix_rgba(nodes, "MIX", (0, 0))
            fac_b.default_value = 0.35
            in_a_b.default_value = (*env.ambient_color, 1.0)
            links.new(env_node.outputs["Color"], in_b_b)
            links.new(out_b, bg.inputs["Color"])

    # Sun light from MAEnvironment DirectionalLightDirection
    if env.sun_on:
        sun_data = bpy.data.lights.new(name="MA2_Sun", type="SUN")
        sun_data.color = env.sun_color
        sun_data.energy = 3.0
        sun_obj = bpy.data.objects.new("MA2_Sun", sun_data)
        dx, dy, dz = env.sun_dir
        dir_bl = Vector(pos_to_blender(dx, dy, dz))
        if dir_bl.length < 1e-6:
            dir_bl = Vector((1.0, 1.0, -1.0))
        dir_bl.normalize()
        # Blender lights shine along local -Z
        sun_obj.rotation_mode = "QUATERNION"
        sun_obj.rotation_quaternion = dir_bl.to_track_quat("-Z", "Y")
        if map_data.terrain is not None:
            sun_obj.location = (
                map_data.terrain.size_x * 0.5 * scale,
                map_data.terrain.size_z * 0.5 * scale,
                150.0 * scale,
            )
        light_col.objects.link(sun_obj)


def build_map_in_blender(
    map_data: MapData,
    source: AssetSource,
    settings: MapImportSettings,
) -> bpy.types.Collection:
    """Build an entire MechAssault 2 map inside organized Blender Collections."""
    scene_col = bpy.context.scene.collection
    root_col = _ensure_collection(map_data.name or "MA2_Map", scene_col)

    col_terrain = _ensure_collection("Terrain", root_col)
    col_water = _ensure_collection("Water", root_col)
    col_sky = _ensure_collection("Sky", root_col)
    col_roads = _ensure_collection("Roads", root_col)
    col_props = _ensure_collection("Buildings & Props", root_col)
    col_units = _ensure_collection("Units & Spawns", root_col)
    col_culture = _ensure_collection("Culture Scatter", root_col)
    col_lights = _ensure_collection("Lights & Environment", root_col)

    tex_lib = TextureLibrary(source, pack=settings.pack_textures)
    model_cache = ModelBuildCache(tex_lib, env_cube_path=map_data.environment.env_cube_path)
    model_def_cache: Dict[Tuple[str, str], Optional[ModelDef]] = {}
    group_cols: Dict[str, bpy.types.Collection] = {}

    def get_model_def(mpath: str, mtb: str) -> Optional[ModelDef]:
        key = (normalize_path(mpath).lower(), normalize_path(mtb).lower())
        if key in model_def_cache:
            return model_def_cache[key]
        try:
            mdef = load_model(source, mpath, ModelLoadOptions(mtb_path=mtb))
        except Exception as exc:
            map_data.warnings.append(f"Model {mpath}: {exc}")
            mdef = None
        model_def_cache[key] = mdef
        return mdef

    def get_prop_group_col(gpath: str) -> bpy.types.Collection:
        if not gpath:
            return col_props
        if gpath in group_cols:
            return group_cols[gpath]
        cur_parent = col_props
        accum = ""
        for part in gpath.split("/"):
            accum = f"{accum}/{part}" if accum else part
            if accum in group_cols:
                cur_parent = group_cols[accum]
            else:
                nc = _ensure_collection(part, cur_parent)
                group_cols[accum] = nc
                cur_parent = nc
        return cur_parent

    # 1. Terrain
    if settings.import_terrain and map_data.terrain is not None and map_data.terrain.heights is not None:
        _build_terrain_object(
            map_data.terrain,
            tex_lib,
            map_data.environment.lightmap_path,
            settings.scale,
            col_terrain,
        )

    # 2. Sky dome (MASky2 ignores CorporalHeading in-game; only SkyTerrainSkyScale & SkyTerrainSkyOffset apply)
    if settings.import_sky and map_data.sky is not None and map_data.sky.model_path:
        sky = map_data.sky
        sdef = get_model_def(sky.model_path, sky.mtb_path)
        if sdef is not None:
            hide_add = "env_sky_stormy" in sky.model_path.lower()
            s_obj = build_model_in_blender(
                sdef,
                source,
                tex_lib,
                ModelImportSettings(scale=settings.scale, import_skeleton=False, pack_textures=settings.pack_textures),
                collection=col_sky,
                name_override="SkyDome",
                cache=model_cache,
                hide_additive=hide_add,
            )
            sx, sy, sz = sky.scale
            ox, oy, oz = sky.offset
            s_obj.location = pos_to_blender(ox, oy, oz, scale=settings.scale)
            s_obj.scale = (sx, sz, sy)

    # 3. Lights & World
    if settings.import_lights:
        _setup_world_environment(map_data, tex_lib, col_lights, settings.scale)

    # 4. Placed Entities & Point Lights
    m_settings = ModelImportSettings(
        scale=settings.scale,
        import_materials=True,
        import_skeleton=False,
        import_animations=False,
        pack_textures=settings.pack_textures,
    )

    for ent in map_data.entities:
        if ent.is_point_light:
            if not settings.import_lights:
                continue
            ldata = bpy.data.lights.new(name=ent.name or f"PointLight_{ent.uid}", type="POINT")
            ldata.color = ent.light_color
            ldata.energy = max(50.0, ent.light_radius * 30.0)
            ldata.shadow_soft_size = max(0.25, ent.light_radius * 0.05) * settings.scale
            lobj = bpy.data.objects.new(ent.name or f"PointLight_{ent.uid}", ldata)
            lobj.location = pos_to_blender(*ent.pos, scale=settings.scale)
            col_lights.objects.link(lobj)
            continue

        if ent.is_road and not settings.import_roads:
            continue
        if ent.is_water and not settings.import_water:
            continue
        if not ent.is_road and not ent.is_water and not settings.import_entities:
            continue

        target_col = (
            col_roads if ent.is_road
            else col_water if ent.is_water
            else col_units if ent.is_unit
            else get_prop_group_col(ent.group_path)
        )

        mat_world = entity_matrix_to_blender(
            ent.pos,
            heading=ent.heading,
            pitch=ent.pitch,
            roll=ent.roll,
            scale_vec=ent.scale,
            world_scale=settings.scale,
        )

        if ent.model_path:
            mdef = get_model_def(ent.model_path, ent.mtb_path)
            if mdef is not None:
                obj = build_model_in_blender(
                    mdef,
                    source,
                    tex_lib,
                    m_settings,
                    collection=target_col,
                    name_override=ent.name or file_stem(ent.model_path),
                    cache=model_cache,
                    custom_color=ent.custom_color,
                )
                obj.matrix_world = mat_world
                if not ent.visible:
                    obj.hide_set(True)
                    obj.hide_render = True
                continue

        # Fallback Empty marker for non-modeled units/spawns (MALevelStart etc.)
        if ent.is_unit:
            marker = bpy.data.objects.new(ent.name or f"{ent.class_name}_{ent.uid}", None)
            marker.empty_display_type = "ARROWS"
            marker.empty_display_size = 1.5 * settings.scale
            marker.matrix_world = mat_world
            target_col.objects.link(marker)

    # 5. Culture Scatter
    if settings.import_culture and map_data.culture_instances:
        for idx, ci in enumerate(map_data.culture_instances[:settings.max_culture_instances]):
            mdef = get_model_def(ci.model_path, ci.mtb_path)
            if mdef is None:
                continue
            cobj = build_model_in_blender(
                mdef,
                source,
                tex_lib,
                m_settings,
                collection=col_culture,
                name_override=f"{ci.name}_{idx}",
                cache=model_cache,
            )
            bx, by, bz = ci.basis_x, ci.basis_y, ci.basis_z
            s = ci.scale
            m_col = Matrix((
                (bx[0] * s, by[0] * s, bz[0] * s, ci.pos[0] * settings.scale),
                (bx[1] * s, by[1] * s, bz[1] * s, ci.pos[1] * settings.scale),
                (bx[2] * s, by[2] * s, bz[2] * s, ci.pos[2] * settings.scale),
                (0.0, 0.0, 0.0, 1.0),
            ))
            cobj.matrix_world = SWAP_YZ @ m_col @ SWAP_YZ

    return root_col
