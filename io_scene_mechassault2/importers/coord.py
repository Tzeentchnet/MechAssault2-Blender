"""Coordinate conversion between MechAssault 2 (left-handed Y-up) and Blender (right-handed Z-up)."""
from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

import numpy as np
from mathutils import Euler, Matrix, Quaternion, Vector

# Basis swap matrix C: swaps Y and Z axes (C = C^-1 = C^T, det(C) = -1).
SWAP_YZ = Matrix((
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
))


def pos_to_blender(x: float, y: float, z: float, scale: float = 1.0) -> Tuple[float, float, float]:
    """Convert a single MA2 (X, Y-up, Z) position to Blender (X, Y=Z_ma2, Z=Y_ma2)."""
    return (x * scale, z * scale, y * scale)


def positions_to_blender_np(positions: np.ndarray, scale: float = 1.0) -> np.ndarray:
    """Convert an (N, 3) float32 array from MA2 (X, Y, Z) to Blender (X, Z, Y)."""
    if positions.size == 0:
        return positions.reshape((-1, 3)).astype(np.float32)
    out = np.empty_like(positions, dtype=np.float32)
    out[:, 0] = positions[:, 0] * scale
    out[:, 1] = positions[:, 2] * scale
    out[:, 2] = positions[:, 1] * scale
    return out


def normals_to_blender_np(normals: np.ndarray) -> np.ndarray:
    """Convert an (N, 3) float32 normal array from MA2 (X, Y, Z) to Blender (X, Z, Y)."""
    if normals.size == 0:
        return normals.reshape((-1, 3)).astype(np.float32)
    out = np.empty_like(normals, dtype=np.float32)
    out[:, 0] = normals[:, 0]
    out[:, 1] = normals[:, 2]
    out[:, 2] = normals[:, 1]
    return out


def triangles_to_blender_np(triangles: np.ndarray) -> np.ndarray:
    """Flip triangle winding (i0, i1, i2) -> (i0, i2, i1) for Y<->Z reflection."""
    if triangles.size == 0:
        return triangles.reshape((-1, 3)).astype(np.uint32)
    out = triangles.astype(np.uint32, copy=True)
    out[:, [1, 2]] = out[:, [2, 1]]
    return out


def mat4_row_to_blender(m16: Sequence[float], scale: float = 1.0) -> Matrix:
    """Convert a 16-float row-vector MA2 transform matrix (translation in m[12..14]) to Blender Matrix."""
    # Row-vector matrix M_row: p' = p * M_row -> column-vector M_col = M_row^T.
    m_col = Matrix((
        (m16[0], m16[4], m16[8], m16[12] * scale),
        (m16[1], m16[5], m16[9], m16[13] * scale),
        (m16[2], m16[6], m16[10], m16[14] * scale),
        (m16[3], m16[7], m16[11], m16[15]),
    ))
    return SWAP_YZ @ m_col @ SWAP_YZ


def mat4_col_to_blender(m4x4: Sequence[Sequence[float]], scale: float = 1.0) -> Matrix:
    """Convert a 4x4 column-vector MA2 matrix to Blender Matrix."""
    m_col = Matrix((
        (m4x4[0][0], m4x4[0][1], m4x4[0][2], m4x4[0][3] * scale),
        (m4x4[1][0], m4x4[1][1], m4x4[1][2], m4x4[1][3] * scale),
        (m4x4[2][0], m4x4[2][1], m4x4[2][2], m4x4[2][3] * scale),
        (m4x4[3][0], m4x4[3][1], m4x4[3][2], m4x4[3][3]),
    ))
    return SWAP_YZ @ m_col @ SWAP_YZ


def quat_xyzw_to_blender(qx: float, qy: float, qz: float, qw: float) -> Quaternion:
    """Convert an MA2 quaternion (x, y, z, w) to a Blender Quaternion (w, x, y, z).

    Under similarity transform C = swap(Y, Z) (det(C) = -1, improper reflection):
    a rotation matrix R = I + 2*w*[v]_x + 2*[v]_x^2 becomes
    C R C = I - 2*w*[C v]_x + 2*[C v]_x^2, corresponding to axis-negation (-qx, -qz, -qy, qw).
    """
    q = Quaternion((qw, -qx, -qz, -qy))
    q.normalize()
    return q


def mat_from_srt_ma2(
    scale: Sequence[float],
    axis: Sequence[float],
    angle: float,
    pos: Sequence[float],
) -> Tuple[float, ...]:
    """Build a 16-float row-vector MA2 local matrix from scale, axis-angle (radians), and pos.

    Matches editor/models/src/geometry.cpp::MatFromSRT.
    """
    ax, ay, az = float(axis[0]), float(axis[1]), float(axis[2])
    length = math.sqrt(ax * ax + ay * ay + az * az)
    if length < 1e-8:
        ax, ay, az = 1.0, 0.0, 0.0
        angle = 0.0
    else:
        ax /= length
        ay /= length
        az /= length
    c = math.cos(angle)
    s = math.sin(angle)
    t = 1.0 - c
    r = (
        (t * ax * ax + c, t * ax * ay - s * az, t * ax * az + s * ay),
        (t * ax * ay + s * az, t * ay * ay + c, t * ay * az - s * ax),
        (t * ax * az - s * ay, t * ay * az + s * ax, t * az * az + c),
    )
    out = [0.0] * 16
    for i in range(3):
        for j in range(3):
            out[i * 4 + j] = float(scale[i]) * r[j][i]
    out[12] = float(pos[0])
    out[13] = float(pos[1])
    out[14] = float(pos[2])
    out[15] = 1.0
    return tuple(out)


def entity_matrix_to_blender(
    pos: Sequence[float],
    heading: float = 0.0,
    pitch: float = 0.0,
    roll: float = 0.0,
    scale_vec: Sequence[float] = (1.0, 1.0, 1.0),
    world_scale: float = 1.0,
) -> Matrix:
    """Convert MA2 entity placement (Ry(heading) * Rx(pitch) * Rz(roll), pos) to Blender Matrix.

    Matches editor/src/app/gizmo.cpp::orient_instance and editor/src/core/placement.cpp::mat_of.
    """
    ch, sh = math.cos(heading), math.sin(heading)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)

    sx, sy, sz = float(scale_vec[0]), float(scale_vec[1]), float(scale_vec[2])
    r00 = (ch * cr + sh * sp * sr) * sx
    r01 = (-ch * sr + sh * sp * cr) * sy
    r02 = (sh * cp) * sz

    r10 = (cp * sr) * sx
    r11 = (cp * cr) * sy
    r12 = (-sp) * sz

    r20 = (-sh * cr + ch * sp * sr) * sx
    r21 = (sh * sr + ch * sp * cr) * sy
    r22 = (ch * cp) * sz

    m_col = Matrix((
        (r00, r01, r02, float(pos[0]) * world_scale),
        (r10, r11, r12, float(pos[1]) * world_scale),
        (r20, r21, r22, float(pos[2]) * world_scale),
        (0.0, 0.0, 0.0, 1.0),
    ))
    return SWAP_YZ @ m_col @ SWAP_YZ


#: MA2 game-space right, up and back vectors (as matrix columns: right = -X, up = +Y, back = +Z).
#: Derived from atlas.NODE vs atl_skeleton.gr2: a Max point (gx, gy, gz) lands at MA2 (-gx, gz, -gy).
_MA2_RIGHT_UP_BACK = np.array(((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), dtype=np.float64).T

_SWAP_YZ_NP = np.array(((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)), dtype=np.float64)


def gr2_basis_to_blender(axes: Optional[Sequence[Sequence[float]]]) -> np.ndarray:
    """3x3 orthogonal matrix K mapping a Granny2 file-space vector to Blender space (v_bl = K @ v_gr2).

    `axes` is the file's ArtToolInfo (RightVector, UpVector, BackVector). Every MA2 asset exported from
    3D Studio MAX is Z-up (right +X, up +Z, back -Y). Without ArtToolInfo the data is assumed to
    already be in MA2 game space (Y-up), which is the legacy behaviour.
    """
    if not axes:
        return _SWAP_YZ_NP.copy()
    src = np.array(axes, dtype=np.float64).T  # columns: right, up, back in file space
    try:
        src_inv = np.linalg.inv(src)
    except np.linalg.LinAlgError:
        return _SWAP_YZ_NP.copy()
    k = _SWAP_YZ_NP @ _MA2_RIGHT_UP_BACK @ src_inv
    # Snap to a clean signed permutation when the input is axis-aligned
    return np.where(np.abs(k) < 1e-6, 0.0, k)


def gr2_trs_to_blender_matrix(
    pos: Sequence[float],
    rot_xyzw: Sequence[float],
    scale_shear_3x3: Sequence[float],
    world_scale: float = 1.0,
    axes: Optional[Sequence[Sequence[float]]] = None,
) -> Matrix:
    """Convert a Granny2 transform (pos3, rot4 xyzw, scale_shear 9 floats row-major) to Blender Matrix.

    In Granny2, local transform applies scale_shear, then rotation, then translation:
    M_gr2 = T(pos) * R(rot) * S(scale_shear). The result is K * M_gr2 * K^T with K from
    `gr2_basis_to_blender(axes)`.
    """
    if axes:
        k = Matrix(gr2_basis_to_blender(axes).tolist()).to_4x4()
        ss = scale_shear_3x3
        m = (
            Matrix.Translation(Vector((float(pos[0]), float(pos[1]), float(pos[2]))))
            @ Quaternion((float(rot_xyzw[3]), float(rot_xyzw[0]), float(rot_xyzw[1]), float(rot_xyzw[2])))
            .normalized().to_matrix().to_4x4()
            @ Matrix(((ss[0], ss[1], ss[2], 0.0), (ss[3], ss[4], ss[5], 0.0), (ss[6], ss[7], ss[8], 0.0), (0.0, 0.0, 0.0, 1.0)))
        )
        out = k @ m @ k.transposed()
        out.translation = out.translation * world_scale
        return out

    loc = Vector(pos_to_blender(float(pos[0]), float(pos[1]), float(pos[2]), world_scale))
    rot = quat_xyzw_to_blender(float(rot_xyzw[0]), float(rot_xyzw[1]), float(rot_xyzw[2]), float(rot_xyzw[3]))
    # Convert 3x3 scale-shear matrix via C * S * C (swap Y and Z rows/cols)
    ss = scale_shear_3x3
    s_ma2 = Matrix((
        (ss[0], ss[1], ss[2], 0.0),
        (ss[3], ss[4], ss[5], 0.0),
        (ss[6], ss[7], ss[8], 0.0),
        (0.0, 0.0, 0.0, 1.0),
    ))
    s_bl = SWAP_YZ @ s_ma2 @ SWAP_YZ
    return Matrix.Translation(loc) @ rot.to_matrix().to_4x4() @ s_bl
