"""Headless Blender 5.2 integration tests for the io_scene_mechassault2 extension."""
from __future__ import annotations

import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import bpy
from mathutils import Matrix

# Ensure repository root is importable when launched via `blender --python`
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import io_scene_mechassault2
from io_scene_mechassault2.formats.index_cache import index_file_for_source
from io_scene_mechassault2.formats.mgf import MGFArchive
from tests.test_fixtures import build_synthetic_legacy_gr2, build_synthetic_workspace, write_synthetic_xiso


def reset_scene() -> None:
    """Clear all scene objects, collections, and datablocks between tests."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for col in list(bpy.data.collections):
        if col != bpy.context.scene.collection:
            bpy.data.collections.remove(col)
    for mesh in list(bpy.data.meshes):
        bpy.data.meshes.remove(mesh)
    for arm in list(bpy.data.armatures):
        bpy.data.armatures.remove(arm)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)
    for img in list(bpy.data.images):
        bpy.data.images.remove(img)
    for act in list(bpy.data.actions):
        bpy.data.actions.remove(act)
    for lgt in list(bpy.data.lights):
        bpy.data.lights.remove(lgt)
    bpy.context.scene.frame_set(1)


def assert_mix_rgba_links(mat: bpy.types.Material) -> None:
    """Verify every ShaderNodeMix in `mat` has all linked/active A/B/Result sockets as RGBA, not VALUE."""
    nt = mat.node_tree
    assert nt is not None, f"Material {mat.name} has no node_tree"
    for node in nt.nodes:
        if node.bl_idname == "ShaderNodeMix" and node.data_type == "RGBA":
            for link in nt.links:
                if link.to_node == node and link.to_socket.name in ("A", "B"):
                    assert link.to_socket.type == "RGBA", (
                        f"{mat.name}/{node.name} input {link.to_socket.name} has type {link.to_socket.type}, expected RGBA"
                    )
                if link.from_node == node and link.from_socket.name == "Result":
                    assert link.from_socket.type == "RGBA", (
                        f"{mat.name}/{node.name} output Result has type {link.from_socket.type}, expected RGBA"
                    )


def get_action_fcurves(action: bpy.types.Action, slot: bpy.types.ActionSlot):
    """Retrieve F-curves for a Blender 5.2 Slotted Action."""
    assert len(action.layers) >= 1, "Slotted action has no layers"
    assert len(action.layers[0].strips) >= 1, "Slotted action layer has no strips"
    cbag = action.layers[0].strips[0].channelbag(slot)
    assert cbag is not None, f"No channelbag found for slot {slot.name_display}"
    return cbag.fcurves


def extract_for_loose_test(common_mgf: Path, target_dir: Path, rel_paths: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    with MGFArchive(common_mgf) as ar:
        for rp in rel_paths:
            dest = target_dir / rp
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(ar.read(rp))
            out[rp] = dest
    return out


def test_registration() -> None:
    io_scene_mechassault2.register()
    assert hasattr(bpy.ops.import_scene, "mechassault2_model")
    assert hasattr(bpy.ops.import_scene, "mechassault2_animation")
    assert hasattr(bpy.ops.import_scene, "mechassault2_map")
    assert hasattr(bpy.ops.import_image, "mechassault2_texture")
    assert hasattr(bpy.ops.ma2, "scan_archives")
    assert hasattr(bpy.ops.ma2, "import_browser_item")
    assert hasattr(bpy.context.scene, "ma2_browser")
    print("[PASS] Registration")


def test_model_mgmodel(ws_dir: Path, loose: dict[str, Path]) -> None:
    reset_scene()
    res = bpy.ops.import_scene.mechassault2_model(
        filepath=str(loose["Models/Mech.MGMODEL"]),
        import_materials=True,
        import_skeleton=True,
        import_animations=True,
        apply_mtb=True,
        all_lods=False,
    )
    assert res == {"FINISHED"}, f"Model import returned {res}"

    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    assert arm is not None, "Armature not created for Mech.MGMODEL"
    assert [b.name for b in arm.data.bones] == ["Pelvis", "Spine"]

    # HiddenWing (hidden by Mech.mtb) and LowHullNode (LOD > 0) must be absent
    obj_names = {o.name for o in bpy.data.objects}
    assert not any("HiddenWing" in n for n in obj_names), f"HiddenWing should be hidden by .mtb: {obj_names}"
    assert not any("LowHullNode" in n for n in obj_names), f"LowHullNode (LOD1) should be excluded: {obj_names}"

    mesh_obj = next((o for o in bpy.data.objects if o.type == "MESH" and "PelvisMeshNode" in o.name), None)
    assert mesh_obj is not None, f"Hull mesh object not found in {obj_names}"
    me = mesh_obj.data
    assert len(me.polygons) == 2, f"Expected 2 triangles, got {len(me.polygons)}"
    assert "UVMap" in me.uv_layers and "UVMap_1" in me.uv_layers, f"Expected UV0+UV1, got {list(me.uv_layers.keys())}"
    assert "Col" in me.color_attributes, "Expected vertex color layer 'Col'"
    assert "Pelvis" in mesh_obj.vertex_groups and "Spine" in mesh_obj.vertex_groups
    assert any(m.type == "ARMATURE" and m.object == arm for m in mesh_obj.modifiers)

    # Spot light node
    spot = next((o for o in bpy.data.objects if o.type == "LIGHT" and "Headlight" in o.name), None)
    assert spot is not None and spot.data.type == "SPOT", "Expected Headlight SPOT object"

    # Complex material + Mix RGBA link verification
    hull_mat = next((m for m in me.materials if m is not None), None)
    assert hull_mat is not None, "Expected HullMat assigned to mesh"
    assert_mix_rgba_links(hull_mat)

    # Slotted Action created by import_animations=True
    assert arm.animation_data is not None and arm.animation_data.action is not None
    act = arm.animation_data.action
    assert len(act.slots) >= 1 and arm.animation_data.action_slot is not None
    fcurves = get_action_fcurves(act, arm.animation_data.action_slot)
    assert len(fcurves) >= 10, f"Expected location+quat+scale F-curves, got {len(fcurves)}"
    print("[PASS] MGMODEL Import (Skinning, UV0+UV1, VCol, Lights, MTB, Complex Mat, Slotted Action)")


def test_model_legacy_node(loose: dict[str, Path]) -> None:
    reset_scene()
    res = bpy.ops.import_scene.mechassault2_model(
        filepath=str(loose["Models/Legacy.NODE"]),
        import_materials=True,
        import_skeleton=True,
    )
    assert res == {"FINISHED"}, f"Legacy.NODE import returned {res}"
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    assert arm is not None, "ANIMNODE skeleton armature not created for Legacy.NODE"
    mesh_obj = next((o for o in bpy.data.objects if o.type == "MESH"), None)
    assert mesh_obj is not None and len(mesh_obj.data.polygons) == 2
    assert len(mesh_obj.data.materials) >= 1 and mesh_obj.data.materials[0] is not None
    assert_mix_rgba_links(mesh_obj.data.materials[0])
    print("[PASS] Legacy .NODE Import (ANIMNODE redirect + .mesh/.mat + swizzled texture)")


def test_animation_gr2(loose_gr2: Path) -> None:
    reset_scene()
    res = bpy.ops.import_scene.mechassault2_animation(
        filepath=str(loose_gr2),
        fps=30.0,
        import_skeleton=True,
    )
    assert res == {"FINISHED"}, f"GR2 animation import returned {res}"
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    assert arm is not None
    assert arm.animation_data is not None and arm.animation_data.action is not None
    act = arm.animation_data.action
    slot = arm.animation_data.action_slot
    assert slot is not None
    fcurves = get_action_fcurves(act, slot)
    assert len(fcurves) > 0

    # Verify pose changes between frame 1 and frame 31 (1.0s at 30fps)
    bpy.context.scene.frame_set(1)
    bpy.context.view_layer.update()
    m1 = arm.pose.bones["Pelvis"].matrix_basis.copy()

    bpy.context.scene.frame_set(31)
    bpy.context.view_layer.update()
    m31 = arm.pose.bones["Pelvis"].matrix_basis.copy()

    diff = (m31.to_translation() - m1.to_translation()).length
    assert diff > 1.0, f"Expected Pelvis pose translation change >= 1.0 across animation, got {diff}"

    # F-curves are grouped per bone, keys are non-Bezier, and the action has a manual frame range
    for fc in fcurves:
        assert fc.group is not None, f"F-curve {fc.data_path}[{fc.array_index}] has no channel group"
        bone_name = fc.data_path.split('"')[1]
        assert fc.group.name == bone_name, f"F-curve group {fc.group.name!r} != bone {bone_name!r}"
        assert all(k.interpolation in {"LINEAR", "CONSTANT"} for k in fc.keyframe_points)
    groups = {fc.group.name for fc in fcurves}
    assert {"Pelvis", "Spine"} <= groups, f"Missing bone groups: {groups}"
    assert act.use_frame_range
    assert abs(act.frame_start - 1.0) < 1e-4 and abs(act.frame_end - 31.0) < 1e-4, tuple(act.frame_range)
    # Fixture clip has DefaultLoopCount = 1 (play once); only 0 (loop forever) is flagged cyclic
    assert not act.use_cyclic, "DefaultLoopCount 1 clip should not be flagged cyclic"
    scene = bpy.context.scene
    assert scene.render.fps == 30 and scene.frame_start == 1 and scene.frame_end == 31
    print("[PASS] GR2 Skeleton & Slotted Action Animation Import")


def test_max_space_rig_and_rigid_parts() -> None:
    """3ds Max (Z-up) GR2 rigs land upright, and bone-parented rigid parts follow the pose exactly."""
    import numpy as np
    from mathutils import Vector
    from io_scene_mechassault2.formats.gr2 import parse_gr2
    from io_scene_mechassault2.importers.animation_builder import build_action, build_armature
    from io_scene_mechassault2.importers.model_builder import _parent_to_bone

    reset_scene()
    gr2 = parse_gr2(build_synthetic_legacy_gr2())
    skel = gr2.skeletons[0]
    arm, _ = build_armature(skel, name="LegacyRig")
    bones = arm.data.bones
    # Max (gx, gy, gz) -> Blender (-gx, -gy, gz); the 1.25 root scale carries into child offsets
    assert (bones["joint_hip"].head_local - Vector((0.0, 0.0, 5.0))).length < 1e-4, bones["joint_hip"].head_local
    assert (bones["joint_leg"].head_local - Vector((-1.25, 0.0, 2.5))).length < 1e-4, bones["joint_leg"].head_local

    # A rigid part on joint_leg, offset from the joint and rotated, as MA2 mesh nodes are
    part = bpy.data.objects.new("leg_part", None)
    bpy.context.scene.collection.objects.link(part)
    part_rest = Matrix.Translation((-1.25, 0.5, 2.0)) @ Matrix.Rotation(0.3, 4, "X")
    _parent_to_bone(part, arm, "joint_leg", part_rest)

    act = build_action(arm, gr2.animations[0], skeleton=skel, fps=30.0)
    scene = bpy.context.scene
    scene.frame_set(1)
    bpy.context.view_layer.update()
    for pb in arm.pose.bones:  # rest-valued keys must reproduce the rest pose exactly
        assert np.allclose(np.array(pb.matrix), np.array(pb.bone.matrix_local), atol=1e-4), pb.name
    assert np.allclose(np.array(part.matrix_world), np.array(part_rest), atol=1e-4)

    # Granny's 90 degrees about Max +Z is 90 degrees about Blender +Z: the leg swings from -X to -Y
    scene.frame_set(31)
    bpy.context.view_layer.update()
    leg_head = arm.pose.bones["joint_leg"].head
    assert (leg_head - Vector((0.0, -1.25, 2.5))).length < 1e-3, leg_head
    swing = Matrix.Translation((0.0, 0.0, 5.0)) @ Matrix.Rotation(math.pi / 2.0, 4, "Z") @ Matrix.Translation((0.0, 0.0, -5.0))
    assert np.allclose(np.array(part.matrix_world), np.array(swing @ part_rest), atol=1e-3), part.matrix_world
    assert act.frame_end == 31.0
    print("[PASS] 3ds Max-space GR2 rig, legacy curves & bone-parented rigid parts")


def test_vectorized_transforms_match_mathutils() -> None:
    """NumPy GR2->Blender transform + decompose must match the mathutils reference path."""
    import numpy as np
    from io_scene_mechassault2.importers.animation_builder import (
        decompose_np,
        gr2_trs_to_blender_np,
        make_quats_continuous,
    )
    from io_scene_mechassault2.importers.coord import gr2_trs_to_blender_matrix

    rng = np.random.default_rng(7)
    n = 200
    pos = rng.normal(size=(n, 3)) * 3.0
    rot = rng.normal(size=(n, 4))
    rot /= np.linalg.norm(rot, axis=1, keepdims=True)
    ss = np.zeros((n, 9))
    ss[:, [0, 4, 8]] = rng.uniform(0.3, 2.0, size=(n, 3))
    ss[:20, [0, 4, 8]] = 1.0
    rest = Matrix.Translation((0.1, 0.2, 0.3)) @ Matrix.Rotation(0.7, 4, "X") @ Matrix.Rotation(-0.4, 4, "Z")
    rest_inv = rest.inverted()

    basis_np = np.array(rest_inv, dtype=np.float64) @ gr2_trs_to_blender_np(pos, rot, ss, world_scale=2.0)
    locs, quats, scales = decompose_np(basis_np)
    for i in range(n):
        ref = rest_inv @ gr2_trs_to_blender_matrix(pos[i], rot[i], ss[i], world_scale=2.0)
        assert np.allclose(np.array(ref), basis_np[i], atol=1e-5), f"matrix mismatch at {i}"
        l_ref, q_ref, s_ref = ref.decompose()
        assert np.allclose(locs[i], tuple(l_ref), atol=1e-5)
        assert np.allclose(scales[i], tuple(s_ref), atol=1e-5)
        q = np.array(tuple(q_ref))
        assert np.allclose(quats[i], q, atol=1e-5) or np.allclose(quats[i], -q, atol=1e-5), f"quat mismatch at {i}"

    cont = make_quats_continuous(quats)
    assert np.all(np.einsum("ij,ij->i", cont[1:], cont[:-1]) >= 0.0)
    assert np.allclose(np.abs(np.einsum("ij,ij->i", cont, quats)), 1.0, atol=1e-6)
    print("[PASS] Vectorized GR2 transforms match mathutils")


def test_map_import(loose: dict[str, Path]) -> None:
    reset_scene()
    res = bpy.ops.import_scene.mechassault2_map(
        filepath=str(loose["Multiplay/SynthArena.wdf"]),
        import_terrain=True,
        import_entities=True,
        import_roads=True,
        import_water=True,
        import_sky=True,
        import_lights=True,
        import_culture=True,
    )
    assert res == {"FINISHED"}, f"Map import returned {res}"

    col_names = {c.name for c in bpy.data.collections}
    for req in ("Terrain", "Water", "Sky", "Roads", "Buildings & Props", "Units & Spawns", "Culture Scatter", "Lights & Environment"):
        assert req in col_names, f"Missing expected collection {req} in {col_names}"

    # Terrain mesh + heights + material
    terrain = bpy.data.objects.get("Terrain")
    assert terrain is not None and terrain.type == "MESH"
    assert len(terrain.data.vertices) == 25, f"Expected 5x5=25 verts, got {len(terrain.data.vertices)}"
    assert len(terrain.data.polygons) == 32, f"Expected 32 tris, got {len(terrain.data.polygons)}"
    zs = [v.co.z for v in terrain.data.vertices]
    assert abs(min(zs) - 0.0) < 0.01 and abs(max(zs) - 100.0) < 0.01, f"Unexpected terrain Z range {min(zs)}..{max(zs)}"
    assert len(terrain.data.materials) == 1
    assert_mix_rgba_links(terrain.data.materials[0])

    # SkyDome ignores CorporalHeading=90 and uses SkyTerrainSkyScale=(200,200,200)
    sky = bpy.data.objects.get("SkyDome")
    assert sky is not None
    assert sky.rotation_euler.to_matrix().to_3x3() == Matrix.Identity(3) or max(abs(a) for a in sky.rotation_euler) < 1e-5
    assert abs(sky.scale.x - 200.0) < 0.01

    # Roads, Water, Building, Tank, Lights, Culture Scatter
    assert len(bpy.data.collections["Roads"].all_objects) >= 1
    assert len(bpy.data.collections["Water"].all_objects) >= 1
    assert len(bpy.data.collections["Buildings & Props"].all_objects) >= 1
    assert len(bpy.data.collections["Units & Spawns"].all_objects) >= 1
    assert len(bpy.data.collections["Culture Scatter"].all_objects) >= 1
    assert bpy.data.objects.get("MA2_Sun") is not None
    assert any(o.type == "LIGHT" and o.data.type == "POINT" for o in bpy.data.collections["Lights & Environment"].all_objects)

    # World shader Mix RGBA links
    world = bpy.context.scene.world
    assert world is not None and world.node_tree is not None
    for node in world.node_tree.nodes:
        if node.bl_idname == "ShaderNodeMix" and node.data_type == "RGBA":
            for link in world.node_tree.links:
                if link.to_node == node and link.to_socket.name in ("A", "B"):
                    assert link.to_socket.type == "RGBA"

    print("[PASS] Map Import (Terrain, SkyDome, Roads, Water, Entities, Culture, Lights, World)")


def test_texture_import(loose_tex: Path) -> None:
    reset_scene()
    res = bpy.ops.import_image.mechassault2_texture(filepath=str(loose_tex), pack_textures=True)
    assert res == {"FINISHED"}
    img = next((im for im in bpy.data.images if "loose_diff" in im.name), None)
    assert img is not None
    assert tuple(img.size) == (4, 4), f"Expected 4x4 image, got {tuple(img.size)}"
    assert img.pixels[0] > 0.7, f"Expected reddish top pixel, R={img.pixels[0]}"
    print("[PASS] Standalone MGIc Texture Import")


def test_sidebar_browser(ws_dir: Path) -> None:
    reset_scene()
    props = bpy.context.scene.ma2_browser
    props.game_files_dir = str(ws_dir)

    props.category = "MAPS"
    assert bpy.ops.ma2.scan_archives() == {"FINISHED"}
    assert len(props.items) == 1
    assert "SynthArena" in props.items[0].name

    props.category = "MODELS"
    assert bpy.ops.ma2.scan_archives() == {"FINISHED"}
    assert len(props.items) >= 2
    mech_idx = next(i for i, it in enumerate(props.items) if "Mech" in it.path and it.path.endswith(".MGMODEL"))
    props.active_index = mech_idx
    assert bpy.ops.ma2.import_browser_item() == {"FINISHED"}
    assert any(o.type == "MESH" for o in bpy.data.objects)
    print("[PASS] Sidebar MGF Archive Browser Scan & Direct Import")


def test_xiso_scan_import_and_persistence(iso_path: Path, cache_dir: Path) -> None:
    reset_scene()
    props = bpy.context.scene.ma2_browser
    props.game_files_dir = str(iso_path)
    props.category = "MAPS"

    # First scan builds cached index on disk
    assert bpy.ops.ma2.scan_archives(force_rescan=False) == {"FINISHED"}
    assert "Indexed ISO" in props.cache_status, f"Unexpected cache_status: {props.cache_status}"
    assert len(props.items) == 1
    assert "SynthArena" in props.items[0].name
    assert (cache_dir / "state.json").is_file()
    assert index_file_for_source(iso_path).is_file()

    # Import full map directly from XISO
    props.active_index = 0
    assert bpy.ops.ma2.import_browser_item() == {"FINISHED"}
    assert bpy.data.objects.get("Terrain") is not None

    # Switching categories populates instantly from cache & allows model import
    props.category = "MODELS"
    assert "Cached ISO" in props.cache_status
    mech_idx = next(i for i, it in enumerate(props.items) if "Mech" in it.path and it.path.endswith(".MGMODEL"))
    props.active_index = mech_idx
    reset_scene()
    assert bpy.ops.ma2.import_browser_item() == {"FINISHED"}
    assert any(o.type == "MESH" for o in bpy.data.objects)

    # Repeat scan hits cache
    props.category = "MAPS"
    assert bpy.ops.ma2.scan_archives(force_rescan=False) == {"FINISHED"}
    assert "Cached ISO" in props.cache_status

    # Simulate Blender reset (File -> New) and verify load_post restores browser from disk cache
    bpy.ops.wm.read_homefile(use_empty=True)
    restored = bpy.context.scene.ma2_browser
    assert Path(restored.game_files_dir).resolve() == iso_path.resolve(), (
        f"Expected restored ISO path {iso_path}, got {restored.game_files_dir!r}"
    )
    assert restored.category == "MAPS"
    assert len(restored.items) == 1 and "SynthArena" in restored.items[0].name

    # Simulate fresh Blender restart in a subprocess using the same MA2_CACHE_DIR
    child_code = f"""
import sys
from pathlib import Path
import bpy
sys.path.insert(0, {str(_REPO_ROOT)!r})
import io_scene_mechassault2
io_scene_mechassault2.register()
props = bpy.context.scene.ma2_browser
assert Path(props.game_files_dir).resolve() == Path({str(iso_path)!r}).resolve(), props.game_files_dir
assert len(props.items) == 1 and "SynthArena" in props.items[0].name
assert bpy.ops.ma2.import_browser_item() == {{"FINISHED"}}
assert bpy.data.objects.get("Terrain") is not None
io_scene_mechassault2.unregister()
print("CHILD_RESTART_OK")
"""
    env = os.environ.copy()
    env["MA2_CACHE_DIR"] = str(cache_dir)
    proc = subprocess.run(
        [bpy.app.binary_path, "--background", "--factory-startup", "--python-exit-code", "1", "--python-expr", child_code],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0 and "CHILD_RESTART_OK" in proc.stdout, (
        f"Restart subprocess failed (rc={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    print("[PASS] XISO Scan, Streamed Import, Blender Reset & Restart Cache Persistence")


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        ws_dir = base / "game_files"
        cache_dir = base / "cache"
        os.environ["MA2_CACHE_DIR"] = str(cache_dir)

        ws = build_synthetic_workspace(ws_dir)
        iso_path = write_synthetic_xiso(
            base / "MechAssault2_Test.xiso.iso",
            {
                "common.mgf": ws["common_mgf"].read_bytes(),
                "syntharena.mgf": ws["arena_mgf"].read_bytes(),
            },
        )
        loose = extract_for_loose_test(
            ws["common_mgf"],
            ws_dir,
            ["Models/Mech.MGMODEL", "Models/Legacy.NODE", "Multiplay/SynthArena.wdf"],
        )

        test_registration()
        test_model_mgmodel(ws_dir, loose)
        test_model_legacy_node(loose)
        test_vectorized_transforms_match_mathutils()
        test_animation_gr2(ws["loose_gr2"])
        test_max_space_rig_and_rigid_parts()
        test_map_import(loose)
        test_texture_import(ws["loose_tex"])
        test_sidebar_browser(ws_dir)
        test_xiso_scan_import_and_persistence(iso_path, cache_dir)

        io_scene_mechassault2.unregister()
        assert not hasattr(bpy.context.scene, "ma2_browser")
        print("[PASS] Clean Unregister")

    print("ALL BLENDER INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
