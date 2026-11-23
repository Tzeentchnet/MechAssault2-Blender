"""Blender 5.2 Image creation, packing, cubemap-to-equirectangular projection, and caching."""
from __future__ import annotations

from typing import Dict, Optional, Tuple

import bpy
import numpy as np

from ..formats.mgf import AssetSource
from ..formats.mgic_texture import DecodedTexture, decode_mgic_texture
from ..formats.text_utils import file_name, normalize_path


def rgba_to_blender_image(
    name: str,
    rgba: np.ndarray,
    colorspace: str = "sRGB",
    pack: bool = True,
) -> bpy.types.Image:
    """Create a Blender Image from a top-to-bottom (H, W, 4) uint8 RGBA array."""
    h, w, _c = rgba.shape
    img = bpy.data.images.new(name=name, width=w, height=h, alpha=True)
    # Set colorspace BEFORE writing pixels (in Blender 5.2 assigning colorspace_settings.name clears pixel cache)
    try:
        img.colorspace_settings.name = colorspace
    except Exception:
        pass
    # Flip vertically because Blender image pixels are bottom-to-top, D3D/MGIc are top-to-bottom.
    flipped = np.ascontiguousarray(rgba[::-1, :, :], dtype=np.float32) * (1.0 / 255.0)
    img.pixels.foreach_set(flipped.ravel())
    img.update()
    if pack:
        try:
            img.pack()
        except Exception:
            pass
    return img


def cubemap_to_equirectangular(faces: list[np.ndarray], out_w: int = 512, out_h: int = 256) -> np.ndarray:
    """Project 6 cubemap faces (+X, -X, +Y, -Y, +Z, -Z in MA2 space) into an equirectangular RGBA image."""
    if len(faces) < 6:
        return faces[0] if faces else np.zeros((4, 4, 4), dtype=np.uint8)

    u = (np.arange(out_w, dtype=np.float32) + 0.5) / float(out_w)
    v = (np.arange(out_h, dtype=np.float32) + 0.5) / float(out_h)
    phi = (u - 0.5) * (2.0 * np.pi)       # [-pi, pi]
    theta = (0.5 - v) * np.pi             # [pi/2, -pi/2] (top = +Z_bl = +Y_ma2)

    cos_t = np.cos(theta)[:, None]
    sin_t = np.sin(theta)[:, None]
    cos_p = np.cos(phi)[None, :]
    sin_p = np.sin(phi)[None, :]

    # Blender world reflection vector (bx, by, bz) -> MA2 (x=bx, y=bz, z=by)
    dx = cos_t * cos_p
    dz = cos_t * sin_p
    dy = sin_t

    ax, ay, az = np.abs(dx), np.abs(dy), np.abs(dz)
    out = np.zeros((out_h, out_w, 4), dtype=np.uint8)

    def sample_face(mask: np.ndarray, face_img: np.ndarray, fu: np.ndarray, fv: np.ndarray) -> None:
        if not np.any(mask):
            return
        fh, fw, _ = face_img.shape
        ix = np.clip((fu[mask] * 0.5 + 0.5) * (fw - 1), 0, fw - 1).astype(np.intp)
        iy = np.clip((fv[mask] * 0.5 + 0.5) * (fh - 1), 0, fh - 1).astype(np.intp)
        out[mask] = face_img[iy, ix]

    # Major axes
    m_x = (ax >= ay) & (ax >= az)
    m_px = m_x & (dx > 0)
    m_nx = m_x & (dx <= 0)
    sample_face(m_px, faces[0], -dz / np.maximum(ax, 1e-6), -dy / np.maximum(ax, 1e-6))
    sample_face(m_nx, faces[1], dz / np.maximum(ax, 1e-6), -dy / np.maximum(ax, 1e-6))

    m_y = (~m_x) & (ay >= az)
    m_py = m_y & (dy > 0)
    m_ny = m_y & (dy <= 0)
    sample_face(m_py, faces[2], dx / np.maximum(ay, 1e-6), dz / np.maximum(ay, 1e-6))
    sample_face(m_ny, faces[3], dx / np.maximum(ay, 1e-6), -dz / np.maximum(ay, 1e-6))

    m_z = ~(m_x | m_y)
    m_pz = m_z & (dz > 0)
    m_nz = m_z & (dz <= 0)
    sample_face(m_pz, faces[4], dx / np.maximum(az, 1e-6), -dy / np.maximum(az, 1e-6))
    sample_face(m_nz, faces[5], -dx / np.maximum(az, 1e-6), -dy / np.maximum(az, 1e-6))
    return out


class TextureLibrary:
    """Session-scoped cache of decoded MGIc textures -> bpy.types.Image."""

    def __init__(self, source: AssetSource, pack: bool = True) -> None:
        self.source = source
        self.pack = pack
        self._images: Dict[Tuple[str, str, bool], bpy.types.Image] = {}
        self._decoded: Dict[str, Optional[DecodedTexture]] = {}

    def decode(self, path: str) -> Optional[DecodedTexture]:
        key = normalize_path(path).lower()
        if key in self._decoded:
            return self._decoded[key]
        if not key or not self.source.exists(path):
            self._decoded[key] = None
            return None
        try:
            tex = decode_mgic_texture(self.source.read(path), all_mips=False)
        except Exception:
            tex = None
        self._decoded[key] = tex
        return tex

    def get_image(
        self,
        path: str,
        colorspace: str = "sRGB",
        as_equirect_cube: bool = False,
    ) -> Optional[bpy.types.Image]:
        norm = normalize_path(path)
        if not norm:
            return None
        key = (norm.lower(), colorspace, as_equirect_cube)
        if key in self._images:
            img = self._images[key]
            if img.name in bpy.data.images:
                return img

        tex = self.decode(norm)
        if tex is None:
            return None

        if as_equirect_cube and tex.info.cube and len(tex.layers) >= 6:
            faces = [tex.layers[i][0] for i in range(6) if tex.layers[i]]
            rgba = cubemap_to_equirectangular(faces)
            img_name = f"{file_name(norm)}_env"
        else:
            rgba = tex.top()
            if rgba is None:
                return None
            img_name = file_name(norm)

        img = rgba_to_blender_image(img_name, rgba, colorspace=colorspace, pack=self.pack)
        self._images[key] = img
        return img
