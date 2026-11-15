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


# -- reading ---------------------------------------------------------------------------
#
# Reads what this app writes (and what PCL / Open3D / CloudCompare commonly write):
# PCD and PLY, binary or ASCII, with x y z and optional colour. Anything else is
# refused with a message instead of being guessed at.

MAX_HEADER_BYTES = 16 * 1024
MAX_READ_POINTS = 200_000_000

_PCD_TYPES = {("F", 4): "<f4", ("F", 8): "<f8", ("U", 1): "u1", ("U", 2): "<u2",
              ("U", 4): "<u4", ("U", 8): "<u8", ("I", 1): "i1", ("I", 2): "<i2",
              ("I", 4): "<i4", ("I", 8): "<i8"}
_PLY_TYPES = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
              "short": "<i2", "int16": "<i2", "ushort": "<u2", "uint16": "<u2",
              "int": "<i4", "int32": "<i4", "uint": "<u4", "uint32": "<u4",
              "float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8"}


def _split_header(raw: bytes, terminator: bytes) -> tuple[list[str], int]:
    """Header lines (text) and the offset of the first data byte."""
    end = raw.find(terminator, 0, MAX_HEADER_BYTES)
    if end < 0:
        raise ValueError("not a point-cloud file (no header found)")
    body_at = raw.index(b"\n", end) + 1 if terminator[-1:] != b"\n" else end + len(terminator)
    return raw[:body_at].decode("ascii", errors="replace").splitlines(), body_at


def _finish(columns: dict, n: int):
    """(xyz float32 (n,3), rgb uint8 (n,3) or None) from named columns."""
    for axis in "xyz":
        if axis not in columns:
            raise ValueError("the file has no x/y/z coordinates")
    xyz = np.column_stack([columns["x"], columns["y"], columns["z"]]).astype(np.float32)
    rgb = None
    if "rgb" in columns:                                   # PCL: 0x00RRGGBB packed in 4 bytes
        packed = np.ascontiguousarray(columns["rgb"])
        if packed.dtype.kind == "f":
            packed = packed.astype(np.float32).view(np.uint32)
        packed = packed.astype(np.uint32)
        rgb = np.column_stack([(packed >> 16) & 255, (packed >> 8) & 255, packed & 255]).astype(np.uint8)
    elif all(c in columns for c in ("red", "green", "blue")):
        rgb = np.column_stack([columns["red"], columns["green"], columns["blue"]]).astype(np.uint8)
    good = np.isfinite(xyz).all(axis=1)                    # same rule as live frames
    if not good.all():
        xyz = xyz[good]
        rgb = None if rgb is None else rgb[good]
    return xyz, rgb


def read_pcd(raw: bytes):
    lines, at = _split_header(raw, b"DATA ")
    # the DATA line itself is the last header line
    fields = size = typ = count = None
    n = None
    data_kind = None
    for line in lines:
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        key, vals = parts[0].upper(), parts[1:]
        if key == "FIELDS":
            fields = vals
        elif key == "SIZE":
            size = [int(v) for v in vals]
        elif key == "TYPE":
            typ = vals
        elif key == "COUNT":
            count = [int(v) for v in vals]
        elif key == "POINTS":
            n = int(vals[0])
        elif key == "DATA":
            data_kind = vals[0].lower()
    if not (fields and size and typ) or n is None or data_kind is None:
        raise ValueError("incomplete PCD header")
    count = count or [1] * len(fields)
    if not (len(fields) == len(size) == len(typ) == len(count)):
        raise ValueError("inconsistent PCD header")
    if n < 0 or n > MAX_READ_POINTS:
        raise ValueError(f"implausible point count {n:,}")
    dtype_fields = []
    for name, s, t, c in zip(fields, size, typ, count):
        code = _PCD_TYPES.get((t.upper(), s))
        if code is None:
            raise ValueError(f"unsupported PCD field type {t}{s}")
        dtype_fields.append((name, code) if c == 1 else (name, code, (c,)))
    dtype = np.dtype(dtype_fields)

    if data_kind == "binary":
        if len(raw) - at < n * dtype.itemsize:
            raise ValueError("the file is truncated")
        arr = np.frombuffer(raw, dtype=dtype, count=n, offset=at)
        columns = {f: arr[f] for f in fields}
    elif data_kind == "ascii":
        total = sum(count)
        flat = np.fromstring(raw[at:].decode("ascii", errors="replace"), sep=" ")
        if flat.size < n * total:
            raise ValueError("the file is truncated")
        table = flat[: n * total].reshape(n, total)
        columns, col = {}, 0
        for name, c in zip(fields, count):
            columns[name] = table[:, col]
            col += c
        if "rgb" in columns:                               # ASCII rgb is the packed integer's value
            columns["rgb"] = columns["rgb"].astype(np.float64).astype(np.uint32) \
                if columns["rgb"].max(initial=0) > 1.0 else columns["rgb"].astype(np.float32)
    else:
        raise ValueError(f"unsupported PCD data format {data_kind!r} (use binary or ascii)")
    return _finish(columns, n)


def read_ply(raw: bytes):
    lines, at = _split_header(raw, b"end_header")
    if not lines or lines[0].strip() != "ply":
        raise ValueError("not a PLY file")
    fmt, n, props, in_vertex = None, None, [], False
    for line in lines[1:]:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "format":
            fmt = parts[1]
        elif parts[0] == "element":
            in_vertex = parts[1] == "vertex"
            if in_vertex:
                n = int(parts[2])
        elif parts[0] == "property" and in_vertex:
            if parts[1] == "list":
                raise ValueError("PLY vertex lists are not supported")
            code = _PLY_TYPES.get(parts[1])
            if code is None:
                raise ValueError(f"unsupported PLY property type {parts[1]!r}")
            props.append((parts[2], code))
    if n is None or not props:
        raise ValueError("PLY file has no vertices")
    if n < 0 or n > MAX_READ_POINTS:
        raise ValueError(f"implausible point count {n:,}")
    if fmt == "binary_little_endian":
        dtype = np.dtype(props)
        if len(raw) - at < n * dtype.itemsize:
            raise ValueError("the file is truncated")
        arr = np.frombuffer(raw, dtype=dtype, count=n, offset=at)
        columns = {name: arr[name] for name, _ in props}
    elif fmt == "ascii":
        flat = np.fromstring(raw[at:].decode("ascii", errors="replace"), sep=" ")
        width = len(props)
        if flat.size < n * width:
            raise ValueError("the file is truncated")
        table = flat[: n * width].reshape(n, width)
        columns = {name: table[:, i] for i, (name, _) in enumerate(props)}
    else:
        raise ValueError(f"unsupported PLY format {fmt!r} (use binary little-endian or ascii)")
    return _finish(columns, n)


def read_cloud(path: str):
    """Load a .pcd / .ply file -> (xyz float32 (N, 3), rgb uint8 (N, 3) or None).

    Raises OSError if unreadable and ValueError (with a plain-English message)
    if the file is not a cloud this reader understands.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".pcd", ".ply"):
        raise ValueError(f"unsupported file type {ext or '(none)'!r}; open a .pcd or .ply")
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return read_pcd(raw) if ext == ".pcd" else read_ply(raw)
    except (KeyError, IndexError) as exc:
        raise ValueError("the file is not a valid point cloud") from exc


def write_cloud(path: str, xyz, rgb_u8=None) -> None:
    """Write by file extension (.pcd / .ply). Raises ValueError for anything else."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".ply":
        write_ply(path, xyz, rgb_u8)
    elif ext == ".pcd":
        write_pcd(path, xyz, rgb_u8)
    else:
        raise ValueError(f"unsupported point-cloud format {ext or '(none)'!r}; use .pcd or .ply")
