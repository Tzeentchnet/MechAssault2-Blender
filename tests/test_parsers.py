"""Pure-Python unit tests for io_scene_mechassault2.formats (runs without Blender)."""
from __future__ import annotations

import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

# Ensure repository root is importable regardless of cwd
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from io_scene_mechassault2.formats.geometry import (
    build_triangles,
    decode_index_file,
    decode_vertex_file,
    vertex_stride,
)
from io_scene_mechassault2.formats.gr2 import Gr2Curve, evaluate_curve, evaluate_curve_np, parse_gr2
from io_scene_mechassault2.formats.index_cache import (
    clear_index_cache,
    load_cached_index,
    load_state,
    scan_or_load_index,
)
from io_scene_mechassault2.formats.map_wdf import TemplateCatalog, discover_maps, load_map
from io_scene_mechassault2.formats.mgf import (
    ArchiveSet,
    AssetSource,
    MGFArchive,
    open_game_archives,
    resolve_model_path,
)
from io_scene_mechassault2.formats.xiso import XisoArchive, is_xiso_file
from io_scene_mechassault2.formats.mgic_texture import (
    PixelFormat,
    decode_culture_map_u16,
    decode_heightfield_samples8,
    decode_mgic_texture,
    parse_mgic_picture_header,
)
from io_scene_mechassault2.formats.model import (
    AlphaMode,
    ModelLoadOptions,
    load_model,
    parse_animation_set,
)
from io_scene_mechassault2.formats.wdf import (
    K_CULTURE_MAP,
    PropType,
    WdfTree,
    parse_wdf,
    wdf_key,
)

from tests.test_fixtures import (
    build_indx_file,
    build_mgf_v4,
    build_mgic_picture,
    build_synthetic_gr2,
    build_synthetic_workspace,
    build_vers_file,
    build_wdf,
    make_dxt1_solid_block,
    make_dxt3_solid_block,
    make_dxt5_solid_block,
    morton_swizzle_rgba32,
    wdf_prop_i32,
    wdf_prop_str,
    wdf_prop_u32,
    write_synthetic_xiso,
)


class TestMgfAndSource(unittest.TestCase):
    def test_plain_and_compressed_mgf_v4(self) -> None:
        files = {
            "Textures/Hero.tif": b"TIFDATA" * 100,
            "Models/Mech.MGMODEL": b"<model/>",
            "Nested/Dir/Item.txt": bytes(range(256)) * 40,
        }
        plain_bytes = build_mgf_v4(files, compressed=False)
        comp_bytes = build_mgf_v4(files, compressed=True, block_size=1024)

        with tempfile.TemporaryDirectory() as td:
            p_plain = Path(td) / "plain.mgf"
            p_comp = Path(td) / "comp.mgf"
            p_plain.write_bytes(plain_bytes)
            p_comp.write_bytes(comp_bytes)

            with MGFArchive(p_plain) as a_plain, MGFArchive(p_comp) as a_comp:
                self.assertEqual(a_plain.version, 4)
                self.assertEqual(a_comp.version, 4)
                self.assertTrue(a_plain.exists("textures/hero.tif"))  # case-insensitive
                self.assertEqual(a_plain.canonical_path("TEXTURES/HERO.TIF"), "Textures/Hero.tif")
                for k, v in files.items():
                    self.assertEqual(a_plain.read(k), v)
                    self.assertEqual(a_comp.read(k), v)

    def test_archive_set_and_resolve_model_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = build_synthetic_workspace(root)
            archives = ArchiveSet()
            archives.add(ws["common_mgf"])
            archives.add(ws["arena_mgf"])
            try:
                src = AssetSource(archive_set=archives, disk_root=root)
                self.assertTrue(src.exists("Models/Mech.MGMODEL"))
                self.assertTrue(src.exists("Terrain/colormap.tif"))  # from compressed arena.mgf
                self.assertTrue(src.exists("loose_diff.tif"))       # from disk_root
                self.assertEqual(resolve_model_path(src, "Models/Mech.NODE"), "Models/Mech.MGMODEL")
                self.assertEqual(resolve_model_path(src, "Models/Legacy.MGMODEL"), "Models/Legacy.NODE")
            finally:
                archives.close()


class TestMgiCTexture(unittest.TestCase):
    def test_all_pixel_formats_and_special_layouts(self) -> None:
        # 1. DXT1
        raw_dxt1 = build_mgic_picture(4, 4, (1 << 8), make_dxt1_solid_block(248, 0, 0))
        tex = decode_mgic_texture(raw_dxt1)
        self.assertEqual(tex.info.fmt, PixelFormat.DXT1)
        top = tex.top()
        self.assertEqual(top.shape, (4, 4, 4))
        self.assertGreater(top[0, 0, 0], 235)
        self.assertEqual(top[0, 0, 3], 255)

        # 2. DXT3 (half alpha 0x8 -> 0x88)
        raw_dxt3 = build_mgic_picture(4, 4, (3 << 8), make_dxt3_solid_block(0, 252, 0, a4=0x8))
        tex = decode_mgic_texture(raw_dxt3)
        self.assertEqual(tex.info.fmt, PixelFormat.DXT3)
        self.assertTrue(tex.has_alpha)
        self.assertFalse(tex.alpha_is_binary)
        self.assertEqual(int(tex.top()[0, 0, 3]), 0x88)

        # 3. DXT5
        raw_dxt5 = build_mgic_picture(4, 4, (5 << 8), make_dxt5_solid_block(0, 0, 248, a=100))
        tex = decode_mgic_texture(raw_dxt5)
        self.assertEqual(tex.info.fmt, PixelFormat.DXT5)
        self.assertEqual(int(tex.top()[0, 0, 3]), 100)

        # 4. A8R8G8B8 linear & Morton swizzled
        img = np.zeros((4, 4, 4), dtype=np.uint8)
        for y in range(4):
            for x in range(4):
                img[y, x] = [10 + x * 40, 20 + y * 50, 150, 200]
        bgra_lin = img[:, :, [2, 1, 0, 3]].tobytes()
        tex_lin = decode_mgic_texture(build_mgic_picture(4, 4, 0x0, bgra_lin))
        np.testing.assert_array_equal(tex_lin.top(), img)

        tex_swiz = decode_mgic_texture(build_mgic_picture(4, 4, 0x10, morton_swizzle_rgba32(img)))
        np.testing.assert_array_equal(tex_swiz.top(), img)

        # 5. R5G6B5
        c565 = ((31) << 11) | (0 << 5) | 0  # pure red
        raw_565 = build_mgic_picture(2, 2, 1, struct.pack("<4H", c565, c565, c565, c565))
        tex_565 = decode_mgic_texture(raw_565)
        self.assertEqual(tex_565.info.fmt, PixelFormat.R5G6B5)
        self.assertEqual(tuple(int(x) for x in tex_565.top()[0, 0]), (255, 0, 0, 255))

        # 6. A4R4G4B4 (0x8F0F -> A=8->0x88, R=F->0xFF, G=0, B=F->0xFF)
        raw_4444 = build_mgic_picture(2, 2, 3, struct.pack("<4H", 0x8F0F, 0x8F0F, 0x8F0F, 0x8F0F))
        tex_4444 = decode_mgic_texture(raw_4444)
        self.assertEqual(tuple(int(x) for x in tex_4444.top()[0, 0]), (255, 0, 255, 0x88))

        # 7. A8 (white RGB + alpha)
        raw_a8 = build_mgic_picture(2, 2, 5, bytes([12, 34, 56, 78]))
        tex_a8 = decode_mgic_texture(raw_a8)
        self.assertEqual(tuple(int(x) for x in tex_a8.top()[1, 1]), (255, 255, 255, 78))

        # 8. A8L8
        raw_a8l8 = build_mgic_picture(2, 2, 7, struct.pack("<4H", 0xAB40, 0xAB40, 0xAB40, 0xAB40))
        tex_a8l8 = decode_mgic_texture(raw_a8l8)
        self.assertEqual(tuple(int(x) for x in tex_a8l8.top()[0, 0]), (0x40, 0x40, 0x40, 0xAB))

        # 9. P8 + PAL
        pal = bytes([10, 20, 30, 255, 90, 80, 70, 128])  # BGRA -> RGB (30,20,10,255), (70,80,90,128)
        raw_p8 = build_mgic_picture(2, 2, 4, bytes([0, 1, 1, 0]), palette_bgra=pal)
        tex_p8 = decode_mgic_texture(raw_p8)
        self.assertEqual(tuple(int(x) for x in tex_p8.top()[0, 0]), (30, 20, 10, 255))
        self.assertEqual(tuple(int(x) for x in tex_p8.top()[0, 1]), (70, 80, 90, 128))

        # 10. Cubemap (6 faces) & Volume (depth=2)
        cube_bits = b"".join(make_dxt1_solid_block(40 * i, 200, 50) for i in range(6))
        tex_cube = decode_mgic_texture(build_mgic_picture(4, 4, (1 << 8) | 0x80, cube_bits, frames=6))
        self.assertTrue(tex_cube.info.cube)
        self.assertEqual(len(tex_cube.layers), 6)

        vol_bits = make_dxt1_solid_block(100, 100, 100) + make_dxt1_solid_block(200, 50, 50)
        tex_vol = decode_mgic_texture(build_mgic_picture(4, 4, (1 << 8), vol_bits, depth=2))
        self.assertTrue(tex_vol.info.volume)
        self.assertEqual(len(tex_vol.layers), 2)

        # 11. 64B-pitched Heightfield8 & CultureMap u16
        hf = np.zeros((4, 64), dtype=np.uint8)
        hf[:, :4] = np.arange(16, dtype=np.uint8).reshape(4, 4)
        w, h, out_hf = decode_heightfield_samples8(build_mgic_picture(4, 4, 4, hf.tobytes()))
        self.assertEqual((w, h), (4, 4))
        np.testing.assert_array_equal(out_hf, hf[:, :4])

        cm = np.arange(16, dtype="<u2").reshape(4, 4)
        cw, ch, out_cm = decode_culture_map_u16(build_mgic_picture(4, 4, 1, cm.tobytes()))
        self.assertEqual((cw, ch), (4, 4))
        np.testing.assert_array_equal(out_cm, cm)


class TestGeometry(unittest.TestCase):
    def test_vers_indx_and_strips(self) -> None:
        flags = 0x1 | 0x100 | 0x8 | 0x200 | 0x80000
        stride, known = vertex_stride(flags)
        self.assertTrue(known)
        self.assertEqual(stride, 12 + 4 + 4 + 4 + 20)

        vdata = build_vers_file(
            flags,
            positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)],
            normals=[(0.0, 1.0, 0.0)] * 4,
            colors=[0xAABBCCDD] * 4,  # AA=0xAA, RR=0xBB, GG=0xCC, BB=0xDD
            uv0=[(0.25, 0.5)] * 4,
            bone_indices=[(1, 2, 0, 0)] * 4,
            bone_weights=[(0.6, 0.4, 0.0, 0.0)] * 4,
            with_xmlb=True,
        )
        vb = decode_vertex_file(vdata)
        self.assertEqual(vb.count, 4)
        self.assertTrue(vb.skinned)
        self.assertAlmostEqual(float(vb.normals[0, 1]), 1.0, places=2)
        self.assertEqual(tuple(int(x) for x in vb.colors[0]), (0xBB, 0xCC, 0xDD, 0xAA))
        self.assertAlmostEqual(float(vb.uv0[0, 0]), 0.25, places=3)
        self.assertAlmostEqual(float(vb.uv0[0, 1]), 0.5, places=3)
        self.assertEqual(tuple(int(x) for x in vb.bone_indices[0]), (1, 2, 0, 0))

        # IndexedStrip with 0xFFFF restart and alternating winding
        idata = build_indx_file([0, 1, 2, 3, 0xFFFF, 0, 2, 3], with_xmlb=True)
        indices = decode_index_file(idata)
        tris = build_triangles(indices, is_strip=True, vertex_count=4)
        self.assertEqual(tris.tolist(), [[0, 1, 2], [2, 1, 3], [0, 2, 3]])


class TestWdfAndModel(unittest.TestCase):
    def test_wdf_crc_and_tree(self) -> None:
        self.assertEqual(K_CULTURE_MAP, 0x89FB5118)
        raw = build_wdf([
            ("MARoot", [wdf_prop_u32("UID", 1), wdf_prop_u32("child", 2)]),
            ("MABuilding", [wdf_prop_u32("UID", 2), wdf_prop_u32("sibling", 3), wdf_prop_str("Description", "Bldg")]),
            ("MATank", [wdf_prop_u32("UID", 3), wdf_prop_str("Description", "Tank")]),
        ])
        wdf = parse_wdf(raw)
        tree = WdfTree(wdf)
        self.assertEqual(tree.root, 0)
        self.assertEqual(tree.children[0], [1, 2])
        self.assertEqual(wdf.objs[1].class_name, "MABuilding")

    def test_mgmodel_legacy_node_mtb_and_anm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = build_synthetic_workspace(Path(td))
            archives = ArchiveSet()
            archives.add(ws["common_mgf"])
            try:
                src = AssetSource(archive_set=archives)
                m = load_model(src, "Models/Mech.MGMODEL", ModelLoadOptions())
                self.assertEqual(m.skeleton, "Models/MechSkel.gr2")
                self.assertEqual(len(m.anim_clips), 1)
                self.assertEqual(m.anim_clips[0].name, "Walk")
                self.assertEqual(m.anim_clips[0].path, "Anims/Walk.gr2")
                # Only HullMesh visible: LowLodMesh filtered by LOD != 0, HiddenWing filtered by Mech.mtb!
                self.assertEqual(len(m.meshes), 1)
                sm = m.meshes[0]
                self.assertEqual(sm.name, "HullMesh")
                self.assertTrue(sm.skinned)
                self.assertEqual(sm.triangles.shape, (2, 3))
                self.assertEqual(sm.bone_names, ["Pelvis", "Spine"])
                self.assertAlmostEqual(sm.tint[0], 200 / 255.0, places=3)
                # NodeLight parsed
                lights = [n for n in m.nodes if n.is_light]
                self.assertEqual(len(lights), 1)
                self.assertTrue(lights[0].light.spot)

                # Legacy .NODE + ANIMNODE redirect
                leg = load_model(src, "Models/Legacy.NODE", ModelLoadOptions())
                self.assertEqual(leg.skeleton, "Models/MechSkel.gr2")
                self.assertEqual(len(leg.meshes), 1)
                self.assertEqual(leg.meshes[0].name, "BoxMesh")
                self.assertEqual(leg.materials[0].alpha, AlphaMode.OPAQUE)

                # Animation set parser directly (text and XML)
                clips = parse_animation_set(b'idle\t"../Anims/Idle.gr2"\t0.25\t0\n', "Models/MechAnims.anm")
                self.assertEqual(len(clips), 1)
                self.assertEqual(clips[0].path, "Anims/Idle.gr2")
                self.assertEqual(clips[0].name, "Idle")
                xml_clips = parse_animation_set(b'<anims><clip name="anim_idle" file="../Anims/Idle.gr2"/></anims>', "Models/MechAnims.anm")
                self.assertEqual(len(xml_clips), 1)
                self.assertEqual(xml_clips[0].name, "anim_idle")
            finally:
                archives.close()


class TestGranny2(unittest.TestCase):
    def test_gr2_skeleton_and_curves(self) -> None:
        for use_d4n8 in (False, True):
            raw = build_synthetic_gr2(("Pelvis", "Spine"), include_animation=True, use_d4n8=use_d4n8)
            gr2 = parse_gr2(raw)
            self.assertEqual(len(gr2.skeletons), 1)
            skel = gr2.skeletons[0]
            self.assertEqual([b.name for b in skel.bones], ["Pelvis", "Spine"])
            self.assertEqual(skel.bones[0].parent_index, -1)
            self.assertEqual(skel.bones[1].parent_index, 0)

            self.assertEqual(len(gr2.animations), 1)
            anim = gr2.animations[0]
            self.assertEqual(anim.name, "Walk")
            self.assertAlmostEqual(anim.duration, 1.0, places=4)
            tracks = anim.track_groups[0].transform_tracks
            self.assertEqual(len(tracks), 2)

            # Bone 0 Pos (DaK32fC32f): t=0 -> (0,0,0), t=1 -> (0,0,2)
            p_mid = evaluate_curve(tracks[0].position, 0.5, (0.0, 0.0, 0.0))
            self.assertAlmostEqual(p_mid[2], 1.0, places=3)

            # Bone 0 Rot (D4nK16uC15u / D4nK8uC7u): t=1 -> 90deg around Y => qy=sin(pi/4), qw=cos(pi/4)
            q_end = evaluate_curve(tracks[0].orientation, 1.0, (0.0, 0.0, 0.0, 1.0))
            self.assertAlmostEqual(abs(q_end[1]), math.sin(math.pi / 4.0), places=2)
            self.assertAlmostEqual(abs(q_end[3]), math.cos(math.pi / 4.0), places=2)

            # Bone 1 Pos (D3K16uC16u): t=1 -> (0, 1.5, 0)
            p1_end = evaluate_curve(tracks[1].position, 1.0, (0.0, 0.0, 0.0))
            self.assertAlmostEqual(p1_end[1], 1.5, places=3)


    def test_evaluate_curve_np_matches_scalar(self) -> None:
        rng = np.random.default_rng(1234)
        times = np.concatenate(([-0.25, 0.0, 1.0, 1.5], np.linspace(0.0, 1.0, 97), rng.uniform(0.0, 1.0, 64)))
        curves = []
        for degree in (0, 1, 2, 3):
            for dim in (3, 4, 9):
                n_ctrl = 7
                knots = tuple(float(v) for v in np.sort(rng.uniform(0.0, 1.0, n_ctrl)))
                if degree == 1:
                    knots = (0.0, 0.0) + knots[2:]  # repeated knot (zero-width span)
                controls = tuple(tuple(float(v) for v in row) for row in rng.normal(size=(n_ctrl, dim)))
                curves.append(Gr2Curve(codec="test", fmt=0, degree=degree, dimension=dim, knots=knots, controls=controls))
        curves.append(Gr2Curve(codec="test", fmt=0, degree=1, dimension=3, constant_value=(1.0, 2.0, 3.0)))
        curves.append(Gr2Curve(codec="test", fmt=0, degree=1, dimension=3, knots=(0.5,), controls=((4.0, 5.0, 6.0),)))
        curves.append(None)

        for curve in curves:
            dim = curve.dimension if curve is not None else 3
            default = tuple(float(i) for i in range(dim))
            got = evaluate_curve_np(curve, times, default)
            want = np.array([evaluate_curve(curve, float(t), default) for t in times], dtype=np.float64)
            self.assertEqual(got.shape, want.shape)
            np.testing.assert_allclose(got, want, rtol=1e-9, atol=1e-9)

        # Synthetic file curves too
        gr2 = parse_gr2(build_synthetic_gr2(("Pelvis", "Spine"), include_animation=True))
        for track in gr2.animations[0].track_groups[0].transform_tracks:
            for curve, default in (
                (track.position, (0.0, 0.0, 0.0)),
                (track.orientation, (0.0, 0.0, 0.0, 1.0)),
                (track.scale_shear, (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)),
            ):
                got = evaluate_curve_np(curve, times, default)
                want = np.array([evaluate_curve(curve, float(t), default) for t in times], dtype=np.float64)
                np.testing.assert_allclose(got, want, rtol=1e-9, atol=1e-9)


class TestMapWdf(unittest.TestCase):
    def test_discover_and_load_map(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = build_synthetic_workspace(root)
            maps = discover_maps(root)
            self.assertEqual(len(maps), 1)
            self.assertEqual(maps[0].name, "SynthArena")
            self.assertTrue(maps[0].resource_present)

            archives = ArchiveSet()
            archives.add(ws["common_mgf"])
            archives.add(ws["arena_mgf"])
            try:
                src = AssetSource(archive_set=archives, disk_root=root)
                cat = TemplateCatalog.from_source(src)
                self.assertIsNotNone(cat.find(10))

                mdata = load_map(src, maps[0].world_path, catalog=cat, generate_culture=True, max_culture_instances=500)
                self.assertIsNotNone(mdata.terrain)
                # Row 4 (stored bottom = Z=0) had height sample 0 -> min_h=0; row 0 (+Z north) = max_h=100
                self.assertAlmostEqual(mdata.terrain.height_at(0.0, 0.0), 0.0, places=2)
                self.assertAlmostEqual(mdata.terrain.height_at(0.0, 40.0), 100.0, places=2)

                self.assertIsNotNone(mdata.sky)
                self.assertEqual(mdata.sky.model_path, "Models/Sky.MGMODEL")
                self.assertEqual(mdata.environment.env_cube_path, "Textures/env_cube.tif")

                by_cls = {e.class_name: e for e in mdata.entities}
                self.assertIn("MABuilding", by_cls)
                self.assertEqual(by_cls["MABuilding"].model_path, "Models/Prop.MGMODEL")
                self.assertIn("MARoad", by_cls)
                self.assertEqual(by_cls["MARoad"].model_path, "Models/RoadSeg.MGMODEL")
                self.assertIn("MAWater", by_cls)
                self.assertTrue(by_cls["MAWater"].is_water)
                self.assertIn("MATank", by_cls)
                self.assertIsNotNone(by_cls["MATank"].custom_color)
                self.assertIn("MAPointLight", by_cls)

                self.assertGreater(len(mdata.culture_instances), 0)
            finally:
                archives.close()


class TestXisoAndIndexCache(unittest.TestCase):
    def test_xiso_trimmed_and_redump_offset_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws_dir = root / "ws"
            cache_dir = root / "cache"
            ws = build_synthetic_workspace(ws_dir)
            common_bytes = ws["common_mgf"].read_bytes()
            arena_bytes = ws["arena_mgf"].read_bytes()

            xiso_files = {
                "common.mgf": common_bytes,
                "syntharena.mgf": arena_bytes,
                "default.xbe": b"XBEH" + b"\x00" * 60,
                "Media/Readme.txt": b"MechAssault 2 XISO test",
            }

            iso0 = write_synthetic_xiso(root / "ma2_trimmed.xiso.iso", xiso_files, partition_offset=0)
            self.assertTrue(is_xiso_file(iso0))
            with XisoArchive(iso0) as xiso:
                self.assertEqual(xiso.partition_offset, 0)
                self.assertTrue(xiso.exists("common.mgf"))
                self.assertTrue(xiso.exists("MEDIA/README.TXT"))
                self.assertEqual(xiso.read("Media/Readme.txt"), b"MechAssault 2 XISO test")
                self.assertEqual(sorted(e.name.lower() for e in xiso.mgf_entries()), ["common.mgf", "syntharena.mgf"])

            # Non-zero XGD3/Redump partition offset (0x2080000)
            iso_off = write_synthetic_xiso(root / "ma2_xgd.iso", xiso_files, partition_offset=0x2080000)
            with XisoArchive(iso_off) as xiso2:
                self.assertEqual(xiso2.partition_offset, 0x2080000)
                self.assertEqual(xiso2.read("default.xbe")[:4], b"XBEH")

            # Discover maps & stream full map from trimmed XISO without extracting MGFs
            maps = discover_maps(iso0)
            self.assertEqual(len(maps), 1)
            self.assertEqual(maps[0].name, "SynthArena")
            self.assertTrue(maps[0].resource_present)

            aset = open_game_archives(iso0, level_mgf="syntharena.mgf")
            try:
                src = AssetSource(archive_set=aset)
                mdata = load_map(src, maps[0].world_path)
                self.assertIsNotNone(mdata.terrain)
                self.assertTrue(src.exists("Terrain/colormap.tif"))
            finally:
                aset.close()

            # Persistent index cache: first scan writes cache, second scan hits cache
            idx1, cached1 = scan_or_load_index(iso0, category="MAPS", cache_dir=cache_dir)
            self.assertFalse(cached1)
            self.assertEqual(idx1.source_kind, "iso")
            self.assertEqual(len(idx1.iso_mgfs), 2)
            self.assertEqual(len(idx1.maps), 1)
            self.assertGreaterEqual(len(idx1.models), 2)
            self.assertGreaterEqual(len(idx1.animations), 2)
            self.assertGreaterEqual(len(idx1.textures), 5)

            idx2, cached2 = scan_or_load_index(iso0, category="MODELS", cache_dir=cache_dir)
            self.assertTrue(cached2)
            self.assertEqual(idx2.source_path, idx1.source_path)
            st = load_state(cache_dir=cache_dir)
            self.assertEqual(st.last_source, str(iso0.resolve()))
            self.assertEqual(st.last_category, "MODELS")

            # Force rescan rebuilds cleanly
            idx3, cached3 = scan_or_load_index(iso0, force_rescan=True, cache_dir=cache_dir)
            self.assertFalse(cached3)
            self.assertEqual(len(idx3.maps), 1)

            # Mutating file mtime/size invalidates cached index
            with open(iso0, "ab") as fp:
                fp.write(b"\x00" * 16)
            self.assertIsNone(load_cached_index(iso0, cache_dir=cache_dir))

            removed = clear_index_cache(cache_dir=cache_dir)
            self.assertGreaterEqual(removed, 1)


if __name__ == "__main__":
    unittest.main()
