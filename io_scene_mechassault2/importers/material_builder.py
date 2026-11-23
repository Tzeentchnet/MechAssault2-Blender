"""Blender 5.2 Material & Shader Node builders for MechAssault 2 models and terrain."""
from __future__ import annotations

from typing import Dict, Optional, Tuple

import bpy

from ..formats.map_wdf import TerrainData
from ..formats.model import AlphaMode, MaterialDef
from .texture_builder import TextureLibrary, rgba_to_blender_image


def new_mix_rgba(
    nodes: bpy.types.Nodes,
    blend_type: str = "MIX",
    location: Tuple[float, float] = (0.0, 0.0),
) -> Tuple[bpy.types.Node, bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    """Create a ShaderNodeMix configured for RGBA and return (node, factor, in_a, in_b, result).

    Blender's ShaderNodeMix exposes multiple inputs/outputs sharing names ('A', 'B', 'Result')
    across FLOAT/VECTOR/RGBA types, so selecting by socket.type avoids linking/setting the float sockets.
    """
    mix = nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.blend_type = blend_type
    mix.location = location
    fac = next(s for s in mix.inputs if s.name == "Factor" and s.type == "VALUE")
    in_a = next(s for s in mix.inputs if s.name == "A" and s.type == "RGBA")
    in_b = next(s for s in mix.inputs if s.name == "B" and s.type == "RGBA")
    out_r = next(s for s in mix.outputs if s.name == "Result" and s.type == "RGBA")
    return mix, fac, in_a, in_b, out_r


def _tex_node(
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    image: bpy.types.Image,
    location: Tuple[float, float],
    uv_name: str = "UVMap",
    mirror_u: bool = False,
    mirror_v: bool = False,
    scale_uv: Optional[Tuple[float, float]] = None,
) -> bpy.types.ShaderNodeTexImage:
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    tex.location = location
    if mirror_u or mirror_v:
        tex.extension = "EXTEND"

    uv = nodes.new("ShaderNodeUVMap")
    uv.uv_map = uv_name
    uv.location = (location[0] - 420, location[1])

    if scale_uv is not None:
        mapping = nodes.new("ShaderNodeMapping")
        mapping.location = (location[0] - 220, location[1])
        mapping.inputs["Scale"].default_value = (scale_uv[0], scale_uv[1], 1.0)
        links.new(uv.outputs["UV"], mapping.inputs["Vector"])
        links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
    else:
        links.new(uv.outputs["UV"], tex.inputs["Vector"])
    return tex


def build_model_material(
    mat_def: MaterialDef,
    tex_lib: TextureLibrary,
    tint: Tuple[float, float, float] = (1.0, 1.0, 1.0),
    custom_color: Optional[Tuple[float, float, float, float]] = None,
    has_vertex_color: bool = False,
    env_cube_path: str = "",
) -> bpy.types.Material:
    """Create a Blender 5.2 material node graph matching MechAssault 2 material types."""
    mat = bpy.data.materials.new(name=mat_def.name or "MA2_Material")
    if hasattr(mat, "use_nodes") and not mat.use_nodes:
        mat.use_nodes = True
    mat.use_backface_culling = not mat_def.two_sided

    if mat_def.alpha == AlphaMode.CUTOUT:
        mat.surface_render_method = "DITHERED"
    elif mat_def.alpha in (AlphaMode.BLEND, AlphaMode.ADDITIVE, AlphaMode.SUBTRACTIVE):
        mat.surface_render_method = "BLENDED"
    else:
        mat.surface_render_method = "DITHERED"

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out_node = nodes.new("ShaderNodeOutputMaterial")
    out_node.location = (700, 0)

    # Resolve slots
    slot_map = {s.slot: s for s in mat_def.textures}
    base_slot = slot_map.get("basetexture") or slot_map.get("base")
    multi_slot = slot_map.get("multitexture")
    detail_slot = slot_map.get("detailtexture")
    norm_slot = slot_map.get("normal_map")
    cube_slot = slot_map.get("cubetexture") or slot_map.get("reflectiontexture")

    base_img = tex_lib.get_image(base_slot.path, "sRGB") if (base_slot and base_slot.found) else None
    multi_img = tex_lib.get_image(multi_slot.path, "Non-Color") if (multi_slot and multi_slot.found) else None
    detail_img = tex_lib.get_image(detail_slot.path, "sRGB") if (detail_slot and detail_slot.found) else None
    norm_img = tex_lib.get_image(norm_slot.path, "Non-Color") if (norm_slot and norm_slot.found) else None
    cube_path = cube_slot.path if (cube_slot and cube_slot.found) else env_cube_path
    cube_img = tex_lib.get_image(cube_path, "sRGB", as_equirect_cube=True) if cube_path else None

    # Base color node / chain
    diff_rgba = (
        mat_def.diffuse[0] * tint[0],
        mat_def.diffuse[1] * tint[1],
        mat_def.diffuse[2] * tint[2],
        1.0,
    )

    base_tex_node = None
    if base_img is not None:
        base_tex_node = _tex_node(
            nodes, links, base_img, (-450, 220),
            uv_name="UVMap",
            mirror_u=mat_def.mirror_u,
            mirror_v=mat_def.mirror_v,
        )
        color_socket = base_tex_node.outputs["Color"]
        alpha_socket = base_tex_node.outputs["Alpha"]
    else:
        rgb_node = nodes.new("ShaderNodeRGB")
        rgb_node.location = (-450, 220)
        rgb_node.outputs[0].default_value = diff_rgba
        color_socket = rgb_node.outputs[0]
        alpha_socket = None

    # Detail texture blend (material_detail / material_complex)
    if detail_img is not None:
        det_node = _tex_node(nodes, links, detail_img, (-450, -100), uv_name="UVMap_1")
        level = mat_def.detail_level if mat_def.detail_level >= 0.0 else 0.5
        if mat_def.detail_blend == "alphablend":
            _m, fac, in_a, in_b, out_r = new_mix_rgba(nodes, "MIX", (-160, 140))
            fac.default_value = min(1.0, max(0.0, level))
            links.new(color_socket, in_a)
            links.new(det_node.outputs["Color"], in_b)
            color_socket = out_r
        else:
            # modulate / modulate2x
            _m, fac, in_a, in_b, out_r = new_mix_rgba(nodes, "MULTIPLY", (-160, 140))
            fac.default_value = min(1.0, max(0.0, level))
            links.new(color_socket, in_a)
            links.new(det_node.outputs["Color"], in_b)
            color_socket = out_r

    # Multiply diffuse constant when textured
    if base_img is not None and diff_rgba[:3] != (1.0, 1.0, 1.0):
        _m, fac, in_a, in_b, out_r = new_mix_rgba(nodes, "MULTIPLY", (20, 220))
        fac.default_value = 1.0
        links.new(color_socket, in_a)
        in_b.default_value = diff_rgba
        color_socket = out_r

    # Vertex color multiplier
    if has_vertex_color:
        vcol = nodes.new("ShaderNodeVertexColor")
        vcol.layer_name = "Col"
        vcol.location = (-160, 380)
        _m, fac, in_a, in_b, out_r = new_mix_rgba(nodes, "MULTIPLY", (80, 320))
        fac.default_value = 1.0
        links.new(color_socket, in_a)
        links.new(vcol.outputs["Color"], in_b)
        color_socket = out_r

    # Special Additive / Subtractive / Distortion transparent shaders
    if mat_def.distortion:
        transp = nodes.new("ShaderNodeBsdfTransparent")
        transp.location = (350, 0)
        transp.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        links.new(transp.outputs["BSDF"], out_node.inputs["Surface"])
        return mat

    if mat_def.alpha == AlphaMode.ADDITIVE:
        transp = nodes.new("ShaderNodeBsdfTransparent")
        transp.location = (240, 100)
        emis = nodes.new("ShaderNodeEmission")
        emis.location = (240, -60)
        emis.inputs["Strength"].default_value = max(0.0, mat_def.opacity)
        links.new(color_socket, emis.inputs["Color"])
        add_sh = nodes.new("ShaderNodeAddShader")
        add_sh.location = (470, 0)
        links.new(transp.outputs["BSDF"], add_sh.inputs[0])
        links.new(emis.outputs["Emission"], add_sh.inputs[1])
        links.new(add_sh.outputs["Shader"], out_node.inputs["Surface"])
        return mat

    if mat_def.alpha == AlphaMode.SUBTRACTIVE:
        transp = nodes.new("ShaderNodeBsdfTransparent")
        transp.location = (350, 0)
        inv = nodes.new("ShaderNodeInvert")
        inv.location = (120, 0)
        links.new(color_socket, inv.inputs["Color"])
        links.new(inv.outputs["Color"], transp.inputs["Color"])
        links.new(transp.outputs["BSDF"], out_node.inputs["Surface"])
        return mat

    # Water shader
    if mat_def.water:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (340, 0)
        lw = nodes.new("ShaderNodeLayerWeight")
        lw.location = (-200, 180)
        lw.inputs["Blend"].default_value = 0.35
        _mw, fac_w, in_a_w, in_b_w, out_w = new_mix_rgba(nodes, "MIX", (40, 180))
        in_a_w.default_value = (*mat_def.shallow, 1.0)
        in_b_w.default_value = (*mat_def.deep, 1.0)
        links.new(lw.outputs["Facing"], fac_w)
        links.new(out_w, bsdf.inputs["Base Color"])
        bsdf.inputs["Roughness"].default_value = 0.08
        bsdf.inputs["IOR"].default_value = 1.333
        bsdf.inputs["Alpha"].default_value = max(0.35, min(1.0, mat_def.opacity))
        mat.surface_render_method = "BLENDED"
        links.new(bsdf.outputs["BSDF"], out_node.inputs["Surface"])
        return mat

    # Principled BSDF for Opaque / Cutout / Blend / Complex / Env / Detail / SpecularMask
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (340, 0)
    bsdf.inputs["Roughness"].default_value = 0.55

    # Complex / ColorShift emission & spec & tint
    emission_socket = None
    if multi_img is not None:
        multi_node = _tex_node(nodes, links, multi_img, (-450, -400), uv_name="UVMap")
        sep = nodes.new("ShaderNodeSeparateColor")
        sep.location = (-180, -400)
        links.new(multi_node.outputs["Color"], sep.inputs["Color"])

        # R channel -> Specular IOR Level
        links.new(sep.outputs["Red"], bsdf.inputs["Specular IOR Level"])

        # G channel -> colorshift0 emission
        cs0 = mat_def.colorshift0
        if cs0 != (0.0, 0.0, 0.0):
            mul_g = nodes.new("ShaderNodeVectorMath")
            mul_g.operation = "SCALE"
            mul_g.location = (40, -340)
            mul_g.inputs[0].default_value = cs0
            links.new(sep.outputs["Green"], mul_g.inputs["Scale"])
            emission_socket = mul_g.outputs["Vector"]

        # B channel -> colorshift1 (diffuse tint or emission)
        cs1 = custom_color[:3] if (custom_color is not None and mat_def.colorshift1_diffuse) else mat_def.colorshift1
        if mat_def.colorshift1_diffuse and cs1 != (0.0, 0.0, 0.0):
            _mt, fac_t, in_a_t, in_b_t, out_t = new_mix_rgba(nodes, "MIX", (120, 220))
            links.new(sep.outputs["Blue"], fac_t)
            links.new(color_socket, in_a_t)
            _mu, fac_u, in_a_u, in_b_u, out_u = new_mix_rgba(nodes, "MULTIPLY", (-20, 40))
            fac_u.default_value = 1.0
            links.new(color_socket, in_a_u)
            in_b_u.default_value = (*cs1, 1.0)
            links.new(out_u, in_b_t)
            color_socket = out_t
        elif cs1 != (0.0, 0.0, 0.0):
            mul_b = nodes.new("ShaderNodeVectorMath")
            mul_b.operation = "SCALE"
            mul_b.location = (40, -480)
            mul_b.inputs[0].default_value = cs1
            links.new(sep.outputs["Blue"], mul_b.inputs["Scale"])
            if emission_socket is not None:
                add_e = nodes.new("ShaderNodeVectorMath")
                add_e.operation = "ADD"
                add_e.location = (190, -410)
                links.new(emission_socket, add_e.inputs[0])
                links.new(mul_b.outputs["Vector"], add_e.inputs[1])
                emission_socket = add_e.outputs["Vector"]
            else:
                emission_socket = mul_b.outputs["Vector"]

    # Env cube map reflection blend (material_env or complex with env)
    if cube_img is not None and (mat_def.reflectivity > 0.001 or "env" in mat_def.mat_type.lower()):
        tc = nodes.new("ShaderNodeTexCoord")
        tc.location = (-450, 520)
        env = nodes.new("ShaderNodeTexEnvironment")
        env.image = cube_img
        env.location = (-220, 520)
        links.new(tc.outputs["Reflection"], env.inputs["Vector"])

        _me, fac_e, in_a_e, in_b_e, out_e = new_mix_rgba(nodes, "MIX", (160, 400))
        refl_val = mat_def.reflectivity if mat_def.reflectivity > 0.001 else 0.35
        fac_e.default_value = min(1.0, max(0.0, refl_val))
        links.new(color_socket, in_a_e)
        links.new(env.outputs["Color"], in_b_e)
        color_socket = out_e
        bsdf.inputs["Metallic"].default_value = min(0.85, refl_val * 0.6)
        bsdf.inputs["Roughness"].default_value = max(0.08, 0.45 - refl_val * 0.35)

    links.new(color_socket, bsdf.inputs["Base Color"])

    # Normal map
    if norm_img is not None:
        ntex = _tex_node(nodes, links, norm_img, (-450, -700), uv_name="UVMap")
        nmap = nodes.new("ShaderNodeNormalMap")
        nmap.uv_map = "UVMap"
        nmap.location = (40, -700)
        links.new(ntex.outputs["Color"], nmap.inputs["Color"])
        links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # Self-illumination / emission
    if emission_socket is not None:
        links.new(emission_socket, bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = 1.5
    elif mat_def.selfillum != (0.0, 0.0, 0.0):
        bsdf.inputs["Emission Color"].default_value = (*mat_def.selfillum, 1.0)
        bsdf.inputs["Emission Strength"].default_value = 1.0

    # Alpha handling
    if mat_def.alpha == AlphaMode.CUTOUT and alpha_socket is not None:
        gt = nodes.new("ShaderNodeMath")
        gt.operation = "GREATER_THAN"
        gt.location = (140, -140)
        gt.inputs[1].default_value = 0.5
        links.new(alpha_socket, gt.inputs[0])
        links.new(gt.outputs["Value"], bsdf.inputs["Alpha"])
    elif mat_def.alpha == AlphaMode.BLEND:
        if alpha_socket is not None:
            mul_a = nodes.new("ShaderNodeMath")
            mul_a.operation = "MULTIPLY"
            mul_a.location = (140, -140)
            links.new(alpha_socket, mul_a.inputs[0])
            mul_a.inputs[1].default_value = mat_def.opacity
            links.new(mul_a.outputs["Value"], bsdf.inputs["Alpha"])
        else:
            bsdf.inputs["Alpha"].default_value = mat_def.opacity

    links.new(bsdf.outputs["BSDF"], out_node.inputs["Surface"])
    return mat


def build_terrain_material(
    terrain: TerrainData,
    tex_lib: TextureLibrary,
    lightmap_path: str = "",
) -> bpy.types.Material:
    """Create a Blender 5.2 material replicating MechAssault 2's terrain detail/colormap/lightmap shader."""
    mat = bpy.data.materials.new(name="MA2_Terrain")
    if hasattr(mat, "use_nodes") and not mat.use_nodes:
        mat.use_nodes = True

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out_node = nodes.new("ShaderNodeOutputMaterial")
    out_node.location = (900, 0)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (600, 0)
    bsdf.inputs["Roughness"].default_value = 0.92
    bsdf.inputs["Specular IOR Level"].default_value = 0.1

    cm_img = tex_lib.get_image(terrain.colormap_path, "sRGB") if terrain.colormap_path else None
    d0_img = tex_lib.get_image(terrain.detail0_path, "sRGB") if terrain.detail0_path else None
    d1_img = tex_lib.get_image(terrain.detail1_path, "sRGB") if terrain.detail1_path else None
    lm_img = tex_lib.get_image(lightmap_path, "Non-Color") if lightmap_path else None

    if cm_img is not None:
        cm_node = _tex_node(nodes, links, cm_img, (-500, 300), uv_name="UVMap")
        base_col = cm_node.outputs["Color"]
        base_alpha = cm_node.outputs["Alpha"]
    else:
        rgb = nodes.new("ShaderNodeRGB")
        rgb.location = (-500, 300)
        rgb.outputs[0].default_value = (0.4, 0.4, 0.4, 1.0)
        base_col = rgb.outputs[0]
        base_alpha = None

    # Detail mix: lerp(detail1, detail0, colormap.a)
    detail_mix = None
    if d0_img is not None and d1_img is not None:
        d0_node = _tex_node(nodes, links, d0_img, (-500, 0), uv_name="UVMap", scale_uv=(terrain.detail_mult, terrain.detail_mult))
        d1_node = _tex_node(nodes, links, d1_img, (-500, -300), uv_name="UVMap", scale_uv=(terrain.detail_mult1, terrain.detail_mult1))
        _md, fac_d, in_a_d, in_b_d, out_d = new_mix_rgba(nodes, "MIX", (-160, -100))
        if base_alpha is not None:
            links.new(base_alpha, fac_d)
        else:
            fac_d.default_value = 0.5
        links.new(d1_node.outputs["Color"], in_a_d)
        links.new(d0_node.outputs["Color"], in_b_d)
        detail_mix = out_d
    elif d0_img is not None:
        d0_node = _tex_node(nodes, links, d0_img, (-500, 0), uv_name="UVMap", scale_uv=(terrain.detail_mult, terrain.detail_mult))
        detail_mix = d0_node.outputs["Color"]
    elif d1_img is not None:
        d1_node = _tex_node(nodes, links, d1_img, (-500, -300), uv_name="UVMap", scale_uv=(terrain.detail_mult1, terrain.detail_mult1))
        detail_mix = d1_node.outputs["Color"]

    # Optional MA2L extra layers (2..7) if present
    if terrain.extra_layers_weights is not None and len(terrain.extra_layers) > 2:
        # Pack layer weights 0..3 and 4..7 as RGBA textures and blend over detail_mix
        w_rgba0 = terrain.extra_layers_weights[:, :, :4]
        w_img0 = rgba_to_blender_image("MA2L_Weights0", w_rgba0, colorspace="Non-Color", pack=tex_lib.pack)
        w0_node = _tex_node(nodes, links, w_img0, (-500, -600), uv_name="UVMap")
        sep_w0 = nodes.new("ShaderNodeSeparateColor")
        sep_w0.location = (-260, -600)
        links.new(w0_node.outputs["Color"], sep_w0.inputs["Color"])

        for li in range(2, min(4, len(terrain.extra_layers))):
            layer = terrain.extra_layers[li]
            if layer.rgba is None:
                continue
            l_img = rgba_to_blender_image(f"MA2L_Layer_{li}", layer.rgba, colorspace="sRGB", pack=tex_lib.pack)
            l_node = _tex_node(nodes, links, l_img, (-500, -600 - li * 280), uv_name="UVMap", scale_uv=(layer.repeats, layer.repeats))
            if detail_mix is not None:
                _ml, fac_l, in_a_l, in_b_l, out_l = new_mix_rgba(nodes, "MIX", (-40, -350 - li * 120))
                w_out = sep_w0.outputs["Blue"] if li == 2 else w0_node.outputs["Alpha"]
                links.new(w_out, fac_l)
                links.new(detail_mix, in_a_l)
                links.new(l_node.outputs["Color"], in_b_l)
                detail_mix = out_l

    albedo_socket = base_col
    if detail_mix is not None:
        if terrain.detail_modulate:
            # 4 * DetailBlend * detail * colormap
            scale_d = nodes.new("ShaderNodeVectorMath")
            scale_d.operation = "SCALE"
            scale_d.location = (60, 0)
            scale_d.inputs["Scale"].default_value = max(0.1, 4.0 * terrain.detail_blend)
            links.new(detail_mix, scale_d.inputs[0])

            _mc, fac_c, in_a_c, in_b_c, out_c = new_mix_rgba(nodes, "MULTIPLY", (240, 150))
            fac_c.default_value = 1.0
            links.new(base_col, in_a_c)
            links.new(scale_d.outputs["Vector"], in_b_c)
            albedo_socket = out_c
        else:
            _mc, fac_c, in_a_c, in_b_c, out_c = new_mix_rgba(nodes, "MIX", (240, 150))
            fac_c.default_value = min(1.0, max(0.0, terrain.detail_blend))
            links.new(base_col, in_a_c)
            links.new(detail_mix, in_b_c)
            albedo_socket = out_c

    if lm_img is not None:
        lm_node = _tex_node(nodes, links, lm_img, (60, 420), uv_name="UVMap")
        _mlm, fac_lm, in_a_lm, in_b_lm, out_lm = new_mix_rgba(nodes, "MULTIPLY", (420, 200))
        fac_lm.default_value = 0.65
        links.new(albedo_socket, in_a_lm)
        links.new(lm_node.outputs["Color"], in_b_lm)
        albedo_socket = out_lm

    links.new(albedo_socket, bsdf.inputs["Base Color"])
    links.new(bsdf.outputs["BSDF"], out_node.inputs["Surface"])
    return mat
