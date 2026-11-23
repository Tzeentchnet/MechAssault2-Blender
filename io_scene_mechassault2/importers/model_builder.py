"""Blender 5.2 Model builder: node hierarchy, fast NumPy meshes, skinning, lights, and animations."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Dict, List, Optional, Tuple

import bpy
import numpy as np
from mathutils import Matrix

from ..formats.gr2 import Gr2Bone, Gr2Skeleton, Gr2Transform, parse_gr2
from ..formats.mgf import AssetSource
from ..formats.model import ModelDef, SubMeshDef
from ..formats.text_utils import file_stem, normalize_path
from .animation_builder import build_action, build_armature, playback_end_frame
from .coord import (
    mat4_row_to_blender,
    normals_to_blender_np,
    positions_to_blender_np,
    triangles_to_blender_np,
)
from .material_builder import build_model_material
from .texture_builder import TextureLibrary


@dataclass
class ModelImportSettings:
    scale: float = 1.0
    import_materials: bool = True
    import_skeleton: bool = True
    import_animations: bool = False
    apply_mtb: bool = True
    include_shadow_meshes: bool = False
    include_invisible: bool = False
    all_lods: bool = False
    fps: float = 30.0
    pack_textures: bool = True


class ModelBuildCache:
    """Reusable mesh & material cache across multiple instances in a map."""

    def __init__(self, tex_lib: TextureLibrary, env_cube_path: str = "") -> None:
        self.tex_lib = tex_lib
        self.env_cube_path = env_cube_path
        self.materials: Dict[Tuple, bpy.types.Material] = {}
        self.meshes: Dict[Tuple, bpy.types.Mesh] = {}


def _create_blender_mesh(
    sm: SubMeshDef,
    mesh_name: str,
    mat: Optional[bpy.types.Material],
    scale: float,
) -> bpy.types.Mesh:
    mesh = bpy.data.meshes.new(mesh_name)
    vcount = len(sm.positions)
    tcount = len(sm.triangles)

    if vcount > 0:
        pos_bl = positions_to_blender_np(sm.positions, scale=scale)
        mesh.vertices.add(vcount)
        mesh.vertices.foreach_set("co", pos_bl.ravel())

    if tcount > 0:
        tris_bl = triangles_to_blender_np(sm.triangles)
        loop_count = tcount * 3
        mesh.loops.add(loop_count)
        mesh.loops.foreach_set("vertex_index", tris_bl.ravel())

        mesh.polygons.add(tcount)
        loop_starts = np.arange(0, loop_count, 3, dtype=np.int32)
        loop_totals = np.full(tcount, 3, dtype=np.int32)
        mesh.polygons.foreach_set("loop_start", loop_starts)
        mesh.polygons.foreach_set("loop_total", loop_totals)
        mesh.polygons.foreach_set("use_smooth", np.ones(tcount, dtype=bool))

        # UV0 & UV1
        for uv_idx, uv_arr, uv_name in (
            (0, sm.uv0, "UVMap"),
            (1, sm.uv1, "UVMap_1"),
        ):
            if uv_arr is None or len(uv_arr) != vcount:
                continue
            uv_bl = np.empty((vcount, 2), dtype=np.float32)
            uv_bl[:, 0] = uv_arr[:, 0]
            uv_bl[:, 1] = 1.0 - uv_arr[:, 1]
            loop_uvs = uv_bl[tris_bl.ravel()].ravel()
            uv_layer = mesh.uv_layers.new(name=uv_name)
            uv_layer.data.foreach_set("uv", loop_uvs)

        # Vertex colors (D3DCOLOR)
        if sm.colors is not None and len(sm.colors) == vcount:
            col_f = sm.colors.astype(np.float32) * (1.0 / 255.0)
            loop_cols = col_f[tris_bl.ravel()].ravel()
            cattr = mesh.color_attributes.new(name="Col", type="BYTE_COLOR", domain="CORNER")
            cattr.data.foreach_set("color", loop_cols)

    mesh.validate(verbose=False, clean_customdata=False)
    mesh.update()

    # Custom split normals from vertex normals
    if sm.normals is not None and len(sm.normals) == vcount and vcount > 0 and len(mesh.polygons) > 0:
        nrm_bl = normals_to_blender_np(sm.normals)
        try:
            mesh.normals_split_custom_set_from_vertices(nrm_bl)
        except Exception:
            pass

    if mat is not None:
        mesh.materials.append(mat)

    return mesh


def _assign_skin_weights(
    mesh_obj: bpy.types.Object,
    sm: SubMeshDef,
    skeleton: Gr2Skeleton,
    arm_obj: Optional[bpy.types.Object] = None,
) -> None:
    if sm.bone_indices is None or sm.bone_weights is None:
        return
    skel_names = [b.name for b in skeleton.bones]
    bone_map = sm.bone_names if sm.bone_names else skel_names
    vcount = len(sm.positions)
    if len(sm.bone_indices) != vcount:
        return

    # Vertex groups bind to bones by exact name, but the mesh's skin list and the .gr2 differ in case
    # ("bone_clavicle_r" vs "BONE_clavicle_R"), so name each group after the armature's own bone
    arm_names: Dict[str, str] = {}
    if arm_obj is not None:
        arm_names = {b.name.lower(): b.name for b in arm_obj.data.bones}

    # Create vertex groups only for referenced bone indices
    used_indices = np.unique(sm.bone_indices)
    vgroups: Dict[int, bpy.types.VertexGroup] = {}
    by_name: Dict[str, bpy.types.VertexGroup] = {}
    for bi in used_indices.tolist():
        gname = bone_map[bi] if 0 <= bi < len(bone_map) else f"Bone_{bi}"
        gname = arm_names.get(gname.lower(), gname)
        if gname not in by_name:
            by_name[gname] = mesh_obj.vertex_groups.new(name=gname)
        vgroups[bi] = by_name[gname]

    bidx_list = sm.bone_indices.tolist()
    bwt_list = sm.bone_weights.tolist()
    for vi in range(vcount):
        inds = bidx_list[vi]
        wts = bwt_list[vi]
        for slot in range(4):
            w = wts[slot]
            if w > 1e-5:
                vgroups[inds[slot]].add((vi,), w, "ADD")


def _skeleton_with_model_joints(skeleton: Gr2Skeleton, model: ModelDef) -> Gr2Skeleton:
    """`skeleton` plus the skinned model's joints that the .gr2 lacks, as extra bones.

    Characters share small game skeletons (e.g. Natalia's 23-bone N_G_Skeleton) while their meshes are
    skinned to the full cinematic joint set (fingers, face bones). Without these bones the vertices
    weighted to them stay behind, and cinematic clips have nothing to drive. Extra bones are the skin
    joints, plus mesh-less joint chains hanging off a bone (e.g. bone_hair01..03, animated only by
    cinematic skeletons). Each hangs off its nearest ancestor bone; its rest pose is the joint node's.
    Rigid models (no skinned meshes) are left alone, so their hardpoint Empties stay Empties.
    """
    skin_names = {n.lower() for sm in model.meshes if sm.skinned for n in sm.bone_names}
    if not skin_names:
        return skeleton
    nodes = model.nodes
    index_by_lower = {b.name.lower(): i for i, b in enumerate(skeleton.bones)}

    # A node holds content if it or a descendant has a mesh or light (nodes are listed parent-first)
    has_content = [nd.is_light for nd in nodes]
    for sm in model.meshes:
        if 0 <= sm.node_index < len(nodes):
            has_content[sm.node_index] = True
    for ni in range(len(nodes) - 1, -1, -1):
        p = nodes[ni].parent
        if has_content[ni] and 0 <= p < ni:
            has_content[p] = True

    extra: List[Gr2Bone] = []
    for ni, nd in enumerate(nodes):
        low = (nd.name or "").lower()
        if not low or low in index_by_lower or has_content[ni]:
            continue
        p = nd.parent
        parent_is_bone = 0 <= p < len(nodes) and (nodes[p].name or "").lower() in index_by_lower
        if low not in skin_names and not parent_is_bone:
            continue
        parent_idx = -1
        while 0 <= p < len(nodes):
            parent_idx = index_by_lower.get((nodes[p].name or "").lower(), -1)
            if parent_idx >= 0:
                break
            p = nodes[p].parent
        index_by_lower[low] = len(skeleton.bones) + len(extra)
        extra.append(Gr2Bone(index=index_by_lower[low], name=nd.name, parent_index=parent_idx, local_transform=Gr2Transform()))
    if not extra:
        return skeleton
    return replace(skeleton, bones=list(skeleton.bones) + extra)


def _parent_to_bone(
    obj: bpy.types.Object,
    arm_obj: bpy.types.Object,
    bone_name: str,
    rest_matrix: Matrix,
) -> None:
    """Bone-parent `obj` so it sits at `rest_matrix` (armature space) in rest pose and rigidly follows the bone."""
    bone = arm_obj.data.bones[bone_name]
    # Blender's bone-parent matrix is the bone's frame moved to its tail
    parent_rest = bone.matrix_local @ Matrix.Translation((0.0, bone.length, 0.0))
    obj.parent = arm_obj
    obj.parent_type = "BONE"
    obj.parent_bone = bone_name
    obj.matrix_parent_inverse = parent_rest.inverted()
    obj.matrix_basis = rest_matrix


def build_model_in_blender(
    model: ModelDef,
    source: AssetSource,
    tex_lib: TextureLibrary,
    settings: ModelImportSettings,
    collection: Optional[bpy.types.Collection] = None,
    name_override: str = "",
    cache: Optional[ModelBuildCache] = None,
    custom_color: Optional[Tuple[float, float, float, float]] = None,
    hide_additive: bool = False,
) -> bpy.types.Object:
    """Instantiate a parsed ModelDef into Blender objects, Armature, Actions, Materials, and Lights."""
    target_col = collection or bpy.context.scene.collection
    root_name = name_override or file_stem(model.path) or "MA2_Model"

    # Optional skeleton (.gr2)
    skeleton: Optional[Gr2Skeleton] = None
    arm_obj: Optional[bpy.types.Object] = None
    if settings.import_skeleton and model.skeleton and source.exists(model.skeleton):
        try:
            gr2_skel = parse_gr2(source.read(model.skeleton))
            if gr2_skel.skeletons:
                skeleton = gr2_skel.skeletons[0]
                rig = _skeleton_with_model_joints(skeleton, model)
                # The model's joint nodes are the real pivots; the .gr2 rest pose may be shared or offset
                node_rest: Dict[str, Matrix] = {}
                for nd in model.nodes:
                    if nd.name:
                        node_rest.setdefault(nd.name.lower(), mat4_row_to_blender(nd.world, scale=settings.scale))
                arm_obj, _rest = build_armature(
                    rig,
                    name=f"{root_name}_Armature",
                    scale=settings.scale,
                    collection=target_col,
                    rest_world=node_rest,
                )
        except Exception as exc:
            model.warnings.append(f"Skeleton {model.skeleton} load failed: {exc}")

    # Rigid MA2 models drive their node hierarchy from the skeleton: nodes named like a bone
    # (e.g. "joint_luarm" <-> "joint_luarm") follow that bone, along with everything beneath them.
    bone_for_node: Dict[str, str] = {}
    if arm_obj is not None:
        bone_for_node = {b.name.lower(): b.name for b in arm_obj.data.bones}

    if arm_obj is not None:
        root_obj = arm_obj
    else:
        root_obj = bpy.data.objects.new(root_name, None)
        root_obj.empty_display_type = "PLAIN_AXES"
        root_obj.empty_display_size = 0.5 * settings.scale
        target_col.objects.link(root_obj)

    # Build materials (keyed so cached meshes share materials)
    env_cube = cache.env_cube_path if cache else ""
    built_materials: List[Optional[bpy.types.Material]] = []
    if settings.import_materials:
        for m_idx, mdef in enumerate(model.materials):
            # Determine if any submesh using this material has vertex colors or tint
            has_vc = any(sm.material_index == m_idx and sm.colors is not None for sm in model.meshes)
            tints = [sm.tint for sm in model.meshes if sm.material_index == m_idx]
            tint = tints[0] if tints else (1.0, 1.0, 1.0)
            cache_key = (normalize_path(model.path).lower(), m_idx, tint, custom_color, has_vc)
            if cache is not None and cache_key in cache.materials and cache.materials[cache_key].name in bpy.data.materials:
                built_materials.append(cache.materials[cache_key])
            else:
                bmat = build_model_material(
                    mdef,
                    tex_lib,
                    tint=tint,
                    custom_color=custom_color,
                    has_vertex_color=has_vc,
                    env_cube_path=env_cube,
                )
                if cache is not None:
                    cache.materials[cache_key] = bmat
                built_materials.append(bmat)
    else:
        built_materials = [None] * len(model.materials)

    # Group meshes by node_index
    meshes_by_node: Dict[int, List[Tuple[int, SubMeshDef]]] = {}
    for sm_idx, sm in enumerate(model.meshes):
        meshes_by_node.setdefault(sm.node_index, []).append((sm_idx, sm))

    # Build node hierarchy
    node_objs: List[Optional[bpy.types.Object]] = [None] * len(model.nodes)

    def _attach_bone(obj: bpy.types.Object, nd) -> None:
        bone_name = bone_for_node.get((nd.name or "").lower())
        if bone_name is not None:
            _parent_to_bone(obj, arm_obj, bone_name, mat4_row_to_blender(nd.world, scale=settings.scale))

    # Joint nodes whose subtree holds no mesh or light are fully represented by their armature bone;
    # an Empty for each would only duplicate the skeleton (nodes are listed parent-first)
    has_content = [ni in meshes_by_node or nd.is_light for ni, nd in enumerate(model.nodes)]
    for ni in range(len(model.nodes) - 1, -1, -1):
        p = model.nodes[ni].parent
        if has_content[ni] and 0 <= p < ni:
            has_content[p] = True

    for ni, nd in enumerate(model.nodes):
        if not has_content[ni] and (nd.name or "").lower() in bone_for_node:
            continue
        if not settings.include_invisible and (not nd.turned_on or nd.hidden_by_mtb or (nd.lod > 0 and not settings.all_lods)):
            # Still create parent Empty if needed by visible descendants, else skip
            has_mesh = ni in meshes_by_node
            if not has_mesh and not nd.is_light:
                # Create lightweight Empty only if children exist
                has_child = any(c.parent == ni for c in model.nodes[ni + 1:])
                if not has_child:
                    continue

        parent_obj = node_objs[nd.parent] if (0 <= nd.parent < len(node_objs) and node_objs[nd.parent] is not None) else root_obj
        local_mat = mat4_row_to_blender(nd.local, scale=settings.scale)

        if nd.is_light and nd.light is not None:
            ld = nd.light
            ltype = "SPOT" if ld.spot else "POINT"
            ldata = bpy.data.lights.new(name=nd.name or f"Light_{ni}", type=ltype)
            ldata.color = ld.color
            ldata.energy = max(50.0, ld.outer_radius * 25.0)
            if ld.spot:
                ldata.spot_size = math.radians(max(1.0, min(179.0, ld.outer_angle * 2.0 or 60.0)))
                if ld.outer_angle > 0.0:
                    ldata.spot_blend = max(0.0, min(1.0, 1.0 - ld.inner_angle / ld.outer_angle))
            lobj = bpy.data.objects.new(nd.name or f"Light_{ni}", ldata)
            lobj.parent = parent_obj
            lobj.matrix_local = local_mat
            target_col.objects.link(lobj)
            node_objs[ni] = lobj
            _attach_bone(lobj, nd)
            continue

        node_meshes = meshes_by_node.get(ni, [])
        if len(node_meshes) == 1:
            sm_idx, sm = node_meshes[0]
            if hide_additive and 0 <= sm.material_index < len(model.materials):
                if model.materials[sm.material_index].alpha.value == "additive":
                    continue
            mat = built_materials[sm.material_index] if 0 <= sm.material_index < len(built_materials) else None
            mkey = (normalize_path(model.path).lower(), sm_idx, custom_color)
            if cache is not None and not sm.skinned and mkey in cache.meshes and cache.meshes[mkey].name in bpy.data.meshes:
                bmesh = cache.meshes[mkey]
            else:
                bmesh = _create_blender_mesh(sm, sm.name or nd.name or f"Mesh_{sm_idx}", mat, settings.scale)
                if cache is not None and not sm.skinned:
                    cache.meshes[mkey] = bmesh

            mobj = bpy.data.objects.new(nd.name or sm.name or f"Mesh_{ni}", bmesh)
            if sm.skinned and arm_obj is not None and skeleton is not None:
                mobj.parent = arm_obj
                mobj.matrix_local = mat4_row_to_blender(nd.world, scale=settings.scale)
                _assign_skin_weights(mobj, sm, skeleton, arm_obj)
                mod = mobj.modifiers.new(name="Armature", type="ARMATURE")
                mod.object = arm_obj
            else:
                mobj.parent = parent_obj
                mobj.matrix_local = local_mat
                _attach_bone(mobj, nd)
            if not sm.visible:
                mobj.hide_set(True)
                mobj.hide_render = True
            target_col.objects.link(mobj)
            node_objs[ni] = mobj
        else:
            eobj = bpy.data.objects.new(nd.name or f"Node_{ni}", None)
            eobj.empty_display_type = "PLAIN_AXES"
            eobj.empty_display_size = 0.25 * settings.scale
            eobj.parent = parent_obj
            eobj.matrix_local = local_mat
            target_col.objects.link(eobj)
            node_objs[ni] = eobj
            _attach_bone(eobj, nd)

            for sm_idx, sm in node_meshes:
                if hide_additive and 0 <= sm.material_index < len(model.materials):
                    if model.materials[sm.material_index].alpha.value == "additive":
                        continue
                mat = built_materials[sm.material_index] if 0 <= sm.material_index < len(built_materials) else None
                mkey = (normalize_path(model.path).lower(), sm_idx, custom_color)
                if cache is not None and not sm.skinned and mkey in cache.meshes and cache.meshes[mkey].name in bpy.data.meshes:
                    bmesh = cache.meshes[mkey]
                else:
                    bmesh = _create_blender_mesh(sm, sm.name or f"Mesh_{sm_idx}", mat, settings.scale)
                    if cache is not None and not sm.skinned:
                        cache.meshes[mkey] = bmesh

                mobj = bpy.data.objects.new(sm.name or f"SubMesh_{sm_idx}", bmesh)
                if sm.skinned and arm_obj is not None and skeleton is not None:
                    mobj.parent = arm_obj
                    mobj.matrix_local = mat4_row_to_blender(nd.world, scale=settings.scale)
                    _assign_skin_weights(mobj, sm, skeleton, arm_obj)
                    mod = mobj.modifiers.new(name="Armature", type="ARMATURE")
                    mod.object = arm_obj
                else:
                    mobj.parent = eobj
                if not sm.visible:
                    mobj.hide_set(True)
                    mobj.hide_render = True
                target_col.objects.link(mobj)

    # Also build any orphan meshes (node_index < 0)
    for sm_idx, sm in meshes_by_node.get(-1, []):
        mat = built_materials[sm.material_index] if 0 <= sm.material_index < len(built_materials) else None
        bmesh = _create_blender_mesh(sm, sm.name or f"Mesh_{sm_idx}", mat, settings.scale)
        mobj = bpy.data.objects.new(sm.name or f"SubMesh_{sm_idx}", bmesh)
        mobj.parent = root_obj
        target_col.objects.link(mobj)

    # Import associated animations onto Armature
    if settings.import_animations and arm_obj is not None and model.anim_clips:
        # Leave the primary clip (else the first one) active, rather than whichever loaded last
        preferred = next((c for c in model.anim_clips if c.primary), model.anim_clips[0])
        active: Optional[bpy.types.Action] = None
        for clip in model.anim_clips:
            if not source.exists(clip.path):
                continue
            try:
                gr2_anim = parse_gr2(source.read(clip.path))
                for anim in gr2_anim.animations:
                    action = build_action(
                        arm_obj,
                        anim,
                        skeleton=skeleton,
                        fps=settings.fps,
                        scale=settings.scale,
                        action_name=clip.name or anim.name,
                        set_scene_range=False,
                        # .anm loop_count: -1 loops forever, N > 0 plays N times, 0 = unspecified
                        loop=True if clip.loop_count < 0 else (False if clip.loop_count > 0 else None),
                    )
                    if active is None or clip is preferred:
                        active = action
            except Exception as exc:
                model.warnings.append(f"Animation {clip.path} load failed: {exc}")
        if active is not None:
            arm_obj.animation_data.action = active
            if active.slots:
                arm_obj.animation_data.action_slot = active.slots[0]
            scene = bpy.context.scene
            if settings.fps > 0.0:
                fps_int = max(1, int(round(settings.fps)))
                scene.render.fps = fps_int
                scene.render.fps_base = fps_int / settings.fps
            scene.frame_start = int(active.frame_start)
            scene.frame_end = playback_end_frame(active)

    return root_obj
