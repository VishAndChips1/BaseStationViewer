"""Point-cloud writers (binary PCD and PLY) in plain numpy.

The app used to call open3d for this: a ~300 MB dependency that fails to install
on some Python versions, and whose ImportError was not caught -- so on such a
machine every recorded frame raised. These writers need nothing but numpy, and
write to a temp file first so a failure never leaves a half-written cloud.
"""

import os

import numpy as np


def _pack_rgb(rgb_u8: np.ndarray) -> np.ndarray:
    c = rgb_u8.astype(np.uint32)
    return (c[:, 0] << 16) | (c[:, 1] << 8) | c[:, 2]


def _clean(xyz, rgb_u8):
    xyz = np.ascontiguousarray(xyz, dtype=np.float32)
    if rgb_u8 is not None and len(rgb_u8) != len(xyz):
        rgb_u8 = None                                   # mismatched colours: write geometry only
    return xyz, (None if rgb_u8 is None else np.ascontiguousarray(rgb_u8, dtype=np.uint8))


def _atomic_write(path: str, header: bytes, body: bytes) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(header)
            fh.write(body)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_pcd(path: str, xyz, rgb_u8=None) -> None:
    xyz, rgb_u8 = _clean(xyz, rgb_u8)
    n = len(xyz)
    if rgb_u8 is None:
        fields, size, typ, count = "x y z", "4 4 4", "F F F", "1 1 1"
        data = xyz.tobytes()
    else:
        rec = np.empty(n, dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("rgb", "<u4")])
        rec["x"], rec["y"], rec["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
        rec["rgb"] = _pack_rgb(rgb_u8)                 # PCL packs 0x00RRGGBB into a float field
        fields, size, typ, count = "x y z rgb", "4 4 4 4", "F F F F", "1 1 1 1"
        data = rec.tobytes()
    header = (f"# .PCD v0.7 - Point Cloud Data file format\nVERSION 0.7\nFIELDS {fields}\n"
              f"SIZE {size}\nTYPE {typ}\nCOUNT {count}\nWIDTH {n}\nHEIGHT 1\n"
              f"VIEWPOINT 0 0 0 1 0 0 0\nPOINTS {n}\nDATA binary\n").encode("ascii")
    _atomic_write(path, header, data)


def write_ply(path: str, xyz, rgb_u8=None) -> None:
    xyz, rgb_u8 = _clean(xyz, rgb_u8)
    n = len(xyz)
    if rgb_u8 is None:
        dtype = np.dtype([("p", "<f4", 3)])
        rec = np.empty(n, dtype=dtype)
        rec["p"] = xyz
        props = "property float x\nproperty float y\nproperty float z\n"
    else:
        dtype = np.dtype([("p", "<f4", 3), ("c", "u1", 3)])
        rec = np.empty(n, dtype=dtype)
        rec["p"], rec["c"] = xyz, rgb_u8
        props = ("property float x\nproperty float y\nproperty float z\n"
                 "property uchar red\nproperty uchar green\nproperty uchar blue\n")
    header = ("ply\nformat binary_little_endian 1.0\n"
              f"element vertex {n}\n{props}end_header\n").encode("ascii")
    _atomic_write(path, header, rec.tobytes())


def write_cloud(path: str, xyz, rgb_u8=None) -> None:
    """Write by file extension (.pcd / .ply). Raises ValueError for anything else."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".ply":
        write_ply(path, xyz, rgb_u8)
    elif ext == ".pcd":
        write_pcd(path, xyz, rgb_u8)
    else:
        raise ValueError(f"unsupported point-cloud format {ext or '(none)'!r}; use .pcd or .ply")
