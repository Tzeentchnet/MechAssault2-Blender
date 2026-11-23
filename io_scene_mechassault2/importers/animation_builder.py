"""Blender 5.2 Armature & Slotted Action builders for Granny2 skeletons and animations."""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import bpy
import numpy as np
from mathutils import Matrix, Vector

from ..formats.gr2 import Gr2Animation, Gr2Curve, Gr2Skeleton, Gr2Transform, evaluate_curve_np
from .coord import gr2_trs_to_blender_matrix


def build_armature(
    skeleton: Gr2Skeleton,
    name: str = "",
    scale: float = 1.0,
    collection: Optional[bpy.types.Collection] = None,
) -> Tuple[bpy.types.Object, List[Matrix]]:
    """Build a Blender Armature from a Gr2Skeleton.

    Returns (armature_obj, rest_local_matrices_bl).
    """
    arm_name = name or skeleton.name or "MA2_Armature"
    arm_data = bpy.data.armatures.new(arm_name)
    arm_data.display_type = "STICK"
    arm_obj = bpy.data.objects.new(arm_name, arm_data)

    target_col = collection or bpy.context.scene.collection
    target_col.objects.link(arm_obj)

    bones = skeleton.bones
    n_bones = len(bones)

    local_mats: List[Matrix] = []
    world_mats: List[Matrix] = [Matrix.Identity(4)] * n_bones
    children_map: Dict[int, List[int]] = {i: [] for i in range(n_bones)}

    for i, b in enumerate(bones):
        lt = b.local_transform
        lm = gr2_trs_to_blender_matrix(lt.position, lt.orientation, lt.scale_shear, world_scale=scale)
        local_mats.append(lm)
        if 0 <= b.parent_index < n_bones:
            children_map[b.parent_index].append(i)

    # Compute world matrices in topological / parent-first order
    computed = [False] * n_bones

    def ensure_world(idx: int) -> Matrix:
        if computed[idx]:
            return world_mats[idx]
        p = bones[idx].parent_index
        if 0 <= p < n_bones and p != idx:
            world_mats[idx] = ensure_world(p) @ local_mats[idx]
        else:
            world_mats[idx] = local_mats[idx]
        computed[idx] = True
        return world_mats[idx]

    for i in range(n_bones):
        ensure_world(i)

    # Enter Edit Mode on the armature to create bones
    prev_active = bpy.context.view_layer.objects.active
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    bl_bone_names: List[str] = []
    try:
        ebones: List[bpy.types.EditBone] = []
        for i, b in enumerate(bones):
            eb = arm_data.edit_bones.new(b.name or f"Bone_{i}")
            bl_bone_names.append(eb.name)
            wm = world_mats[i]
            head = wm.translation
            rot3 = wm.to_3x3()
            # Orient +Y along bone axis so EditBone matrix aligns cleanly with wm
            y_dir = rot3 @ Vector((0.0, 1.0, 0.0))
            if y_dir.length < 1e-6:
                y_dir = Vector((0.0, 1.0, 0.0))
            else:
                y_dir.normalize()

            bone_len = 0.2 * scale
            if children_map[i]:
                c_pos = world_mats[children_map[i][0]].translation
                dist = (c_pos - head).length
                if dist > 0.02 * scale:
                    bone_len = dist
            eb.head = head
            eb.tail = head + y_dir * bone_len
            z_dir = rot3 @ Vector((0.0, 0.0, 1.0))
            if z_dir.length > 1e-6:
                eb.align_roll(z_dir)
            ebones.append(eb)

        for i, b in enumerate(bones):
            if 0 <= b.parent_index < n_bones and b.parent_index != i:
                ebones[i].parent = ebones[b.parent_index]
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.objects.active = prev_active

    # Record exact rest local matrices from Blender's built bone hierarchy
    actual_rest_local: List[Matrix] = []
    for i, b in enumerate(bones):
        bl_bone = arm_data.bones[bl_bone_names[i]]
        if 0 <= b.parent_index < n_bones and b.parent_index != i:
            parent_bl = arm_data.bones[bl_bone_names[b.parent_index]]
            actual_rest_local.append(parent_bl.matrix_local.inverted() @ bl_bone.matrix_local)
        else:
            actual_rest_local.append(bl_bone.matrix_local.copy())

    return arm_obj, actual_rest_local


# --------------------------------------------------------------------------- #
# Vectorized Granny2 -> Blender transform helpers
# --------------------------------------------------------------------------- #

# Blender F-Curve keyframe interpolation enum values (for foreach_set)
_INTERP_CONSTANT = 0
_INTERP_LINEAR = 1

# MA2 (X, Y-up, Z) <-> Blender (X, Z, Y) axis permutation
_SWAP_YZ_PERM = [0, 2, 1]


def _quat_wxyz_to_mat3_np(q: np.ndarray) -> np.ndarray:
    """(T, 4) unit quaternions (w, x, y, z) -> (T, 3, 3) rotation matrices."""
    w, x, y, z = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    out = np.empty((q.shape[0], 3, 3), dtype=np.float64)
    out[:, 0, 0] = 1.0 - 2.0 * (y * y + z * z)
    out[:, 0, 1] = 2.0 * (x * y - w * z)
    out[:, 0, 2] = 2.0 * (x * z + w * y)
    out[:, 1, 0] = 2.0 * (x * y + w * z)
    out[:, 1, 1] = 1.0 - 2.0 * (x * x + z * z)
    out[:, 1, 2] = 2.0 * (y * z - w * x)
    out[:, 2, 0] = 2.0 * (x * z - w * y)
    out[:, 2, 1] = 2.0 * (y * z + w * x)
    out[:, 2, 2] = 1.0 - 2.0 * (x * x + y * y)
    return out


def _mat3_to_quat_wxyz_np(m: np.ndarray) -> np.ndarray:
    """(T, 3, 3) orthonormal rotation matrices -> (T, 4) unit quaternions (w, x, y, z), w >= 0."""
    m00, m01, m02 = m[:, 0, 0], m[:, 0, 1], m[:, 0, 2]
    m10, m11, m12 = m[:, 1, 0], m[:, 1, 1], m[:, 1, 2]
    m20, m21, m22 = m[:, 2, 0], m[:, 2, 1], m[:, 2, 2]
    trace = m00 + m11 + m22

    # Shepperd's method: pick the numerically largest of w, x, y, z per row
    cand = np.stack((trace, m00, m11, m22), axis=1)
    choice = np.argmax(cand, axis=1)

    s_w = 2.0 * np.sqrt(np.maximum(1.0 + trace, 1e-12))
    s_x = 2.0 * np.sqrt(np.maximum(1.0 + m00 - m11 - m22, 1e-12))
    s_y = 2.0 * np.sqrt(np.maximum(1.0 + m11 - m00 - m22, 1e-12))
    s_z = 2.0 * np.sqrt(np.maximum(1.0 + m22 - m00 - m11, 1e-12))

    q_w = np.stack((0.25 * s_w, (m21 - m12) / s_w, (m02 - m20) / s_w, (m10 - m01) / s_w), axis=1)
    q_x = np.stack(((m21 - m12) / s_x, 0.25 * s_x, (m01 + m10) / s_x, (m02 + m20) / s_x), axis=1)
    q_y = np.stack(((m02 - m20) / s_y, (m01 + m10) / s_y, 0.25 * s_y, (m12 + m21) / s_y), axis=1)
    q_z = np.stack(((m10 - m01) / s_z, (m02 + m20) / s_z, (m12 + m21) / s_z, 0.25 * s_z), axis=1)

    q = np.choose(choice[:, None], (q_w, q_x, q_y, q_z))
    q /= np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-12)
    q[q[:, 0] < 0.0] *= -1.0
    return q


def _scale_shear_to_mat3_np(ss: np.ndarray) -> np.ndarray:
    """(T, 9) row-major 3x3, (T, 3) diagonal, or (T, 1) uniform scale -> (T, 3, 3)."""
    n, dim = ss.shape
    if dim >= 9:
        return ss[:, :9].reshape((n, 3, 3)).copy()
    out = np.zeros((n, 3, 3), dtype=np.float64)
    if dim >= 3:
        out[:, 0, 0], out[:, 1, 1], out[:, 2, 2] = ss[:, 0], ss[:, 1], ss[:, 2]
    else:
        out[:, 0, 0] = out[:, 1, 1] = out[:, 2, 2] = ss[:, 0]
    return out


def gr2_trs_to_blender_np(
    pos: np.ndarray,
    rot_xyzw: np.ndarray,
    scale_shear: np.ndarray,
    world_scale: float = 1.0,
) -> np.ndarray:
    """Batched `coord.gr2_trs_to_blender_matrix`: (T, 3), (T, 4), (T, 9|3|1) -> (T, 4, 4) Blender matrices."""
    n = pos.shape[0]
    # Quaternion (x, y, z, w) under the Y<->Z reflection -> Blender (w, -x, -z, -y)
    q = np.stack((rot_xyzw[:, 3], -rot_xyzw[:, 0], -rot_xyzw[:, 2], -rot_xyzw[:, 1]), axis=1)
    q_norm = np.linalg.norm(q, axis=1, keepdims=True)
    q = np.where(q_norm > 1e-12, q / np.where(q_norm > 1e-12, q_norm, 1.0), np.array([1.0, 0.0, 0.0, 0.0]))

    s_ma2 = _scale_shear_to_mat3_np(scale_shear)
    s_bl = s_ma2[:, _SWAP_YZ_PERM][:, :, _SWAP_YZ_PERM]  # C * S * C

    out = np.zeros((n, 4, 4), dtype=np.float64)
    out[:, :3, :3] = _quat_wxyz_to_mat3_np(q) @ s_bl
    out[:, :3, 3] = pos[:, _SWAP_YZ_PERM] * world_scale
    out[:, 3, 3] = 1.0
    return out


def decompose_np(mats: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Batched `Matrix.decompose()`: (T, 4, 4) -> loc (T, 3), quat wxyz (T, 4), scale (T, 3)."""
    loc = mats[:, :3, 3].copy()
    m3 = mats[:, :3, :3]
    size = np.linalg.norm(m3, axis=1)  # column lengths, (T, 3)
    negative = np.linalg.det(m3) < 0.0
    size[negative] *= -1.0
    rot = m3 / np.where(np.abs(size) > 1e-12, size, 1.0)[:, None, :]
    return loc, _mat3_to_quat_wxyz_np(rot), size


def make_quats_continuous(quats: np.ndarray) -> np.ndarray:
    """Flip quaternion signs so consecutive keys stay in the same hemisphere (shortest-path interpolation)."""
    if quats.shape[0] < 2:
        return quats
    dots = np.einsum("ij,ij->i", quats[1:], quats[:-1])
    signs = np.concatenate(([1.0], np.cumprod(np.where(dots < 0.0, -1.0, 1.0))))
    return quats * signs[:, None]


def _animated_curves(curves: Sequence[Optional[Gr2Curve]]) -> List[Gr2Curve]:
    return [
        c for c in curves
        if c is not None and not c.constant_value and len(c.knots) > 1 and len(c.controls) > 1
    ]


def track_key_times(
    curves: Sequence[Optional[Gr2Curve]],
    duration: float,
    sample_fps: float,
) -> Tuple[np.ndarray, int]:
    """Choose key times (seconds) and F-Curve interpolation for one bone track.

    Degree-1 (linear) and degree-0 (stepped) Granny curves are keyed exactly at their own knots,
    which reproduces them losslessly with LINEAR / CONSTANT interpolation. Higher-degree B-splines,
    or tracks mixing stepped and linear curves, are baked at `sample_fps` with LINEAR interpolation.
    """
    animated = _animated_curves(curves)
    if not animated:
        return np.zeros(1, dtype=np.float64), _INTERP_CONSTANT

    degrees = {max(0, c.degree) for c in animated}
    if degrees in ({0}, {1}):
        knots = np.concatenate([np.asarray(c.knots, dtype=np.float64) for c in animated])
        knots = knots[(knots > 0.0) & (knots < duration)]
        times = np.unique(np.concatenate(([0.0, duration], knots)))
        return times, (_INTERP_CONSTANT if degrees == {0} else _INTERP_LINEAR)

    frame_count = max(1, int(round(duration * sample_fps)) + 1)
    times = np.minimum(np.arange(frame_count, dtype=np.float64) / sample_fps, duration)
    return times, _INTERP_LINEAR


def _write_fcurve(
    action: bpy.types.Action,
    armature_obj: bpy.types.Object,
    data_path: str,
    index: int,
    group_name: str,
    frames: np.ndarray,
    values: np.ndarray,
    interpolation: int,
) -> None:
    fc = action.fcurve_ensure_for_datablock(armature_obj, data_path, index=index, group_name=group_name)
    count = frames.size
    co = np.empty((count, 2), dtype=np.float32)
    co[:, 0] = frames
    co[:, 1] = values
    fc.keyframe_points.add(count)
    fc.keyframe_points.foreach_set("co", co.ravel())
    fc.keyframe_points.foreach_set("interpolation", np.full(count, interpolation, dtype=np.int32))
    fc.update()


def build_action(
    armature_obj: bpy.types.Object,
    anim: Gr2Animation,
    skeleton: Optional[Gr2Skeleton] = None,
    fps: float = 30.0,
    scale: float = 1.0,
    action_name: str = "",
    push_to_nla: bool = True,
    set_scene_range: bool = True,
) -> bpy.types.Action:
    """Create a Blender 5.2 Slotted Action on `armature_obj` from a Gr2Animation.

    Keys are generated with NumPy per bone track (see `track_key_times`), grouped per bone, and the
    action gets a manual frame range (and cyclic flag for looping clips) for NLA / Action Editor use.
    """
    clip_name = action_name or anim.name or "MA2_Animation"
    action = bpy.data.actions.new(name=clip_name)
    action.use_fake_user = True
    slot = action.slots.new(id_type="OBJECT", name=armature_obj.name)

    if armature_obj.animation_data is None:
        armature_obj.animation_data_create()
    armature_obj.animation_data.action = action
    armature_obj.animation_data.action_slot = slot

    arm_data = armature_obj.data
    bone_by_lower = {b.name.lower(): b for b in arm_data.bones}

    skel_rest_by_lower: Dict[str, Gr2Transform] = {}
    if skeleton is not None:
        for sb in skeleton.bones:
            skel_rest_by_lower[sb.name.lower()] = sb.local_transform

    duration = max(0.0, anim.duration)
    sample_fps = fps if fps > 0.0 else (1.0 / anim.time_step if anim.time_step > 0.0 else 30.0)

    for tg in anim.track_groups:
        for track in tg.transform_tracks:
            bl_bone = bone_by_lower.get(track.name.lower())
            if bl_bone is None:
                continue
            bname = bl_bone.name
            pose_bone = armature_obj.pose.bones.get(bname)
            if pose_bone is None:
                continue
            pose_bone.rotation_mode = "QUATERNION"

            if bl_bone.parent is not None:
                rest_local = bl_bone.parent.matrix_local.inverted() @ bl_bone.matrix_local
            else:
                rest_local = bl_bone.matrix_local
            rest_inv = np.array(rest_local.inverted(), dtype=np.float64)
            s_rest = skel_rest_by_lower.get(track.name.lower(), Gr2Transform())

            times, interpolation = track_key_times(
                (track.position, track.orientation, track.scale_shear), duration, sample_fps
            )
            pos = evaluate_curve_np(track.position, times, s_rest.position)
            rot = evaluate_curve_np(track.orientation, times, s_rest.orientation)
            ss = evaluate_curve_np(track.scale_shear, times, s_rest.scale_shear)

            basis = rest_inv @ gr2_trs_to_blender_np(pos, rot, ss, world_scale=scale)
            locs, quats, scales = decompose_np(basis)
            quats = make_quats_continuous(quats)

            frames = 1.0 + times * sample_fps
            bone_path = f'pose.bones["{bpy.utils.escape_identifier(bname)}"]'
            for prop, values in (("location", locs), ("rotation_quaternion", quats), ("scale", scales)):
                for idx in range(values.shape[1]):
                    _write_fcurve(
                        action, armature_obj, f"{bone_path}.{prop}", idx, bname,
                        frames, values[:, idx], interpolation,
                    )

    # Manual frame range for the Action Editor / NLA, flagged cyclic for looping clips
    frame_end = 1.0 + duration * sample_fps
    action.use_frame_range = True
    action.frame_end = max(frame_end, 1.0)
    action.frame_start = 1.0
    # Granny DefaultLoopCount: 0 = loop forever
    action.use_cyclic = anim.default_loop_count == 0 and duration > 0.0

    if set_scene_range:
        scene = bpy.context.scene
        fps_int = max(1, int(round(sample_fps)))
        scene.render.fps = fps_int
        scene.render.fps_base = fps_int / sample_fps
        scene.frame_start = 1
        scene.frame_end = max(1, int(math.ceil(frame_end - 1e-6)))

    if push_to_nla:
        track_nla = armature_obj.animation_data.nla_tracks.new()
        track_nla.name = clip_name
        strip = track_nla.strips.new(clip_name, 1, action)
        strip.action_slot = slot
        track_nla.mute = True

    return action
