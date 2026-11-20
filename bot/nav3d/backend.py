"""Versioned, offline build and ctypes binding for the bundled Recast backend."""
from __future__ import annotations

import ctypes as C
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native/nav3d"
VENDOR = SOURCE / "vendor/recastnavigation"
REVISION = "6dc1667f580357e8a2154c28b7867bea7e8ad3a7"
# The pre-tiles bridge wrote raw Detour v7 tiles, which the current loader still accepts.
SINGLE_TILE_SOURCE_IDS = {
    ("darwin", "arm64"): "06f760784197886f7ae3f912afe049882ca982d9030dadb5111d05c6aba1dafd",
    ("darwin", "x86_64"): "8bc144031554e18c87f2647ca8de9cdb5beb240f3cd37ac1efbc88ab36655ea0",
    ("linux", "x86_64"): "ff92c2be31eaaa173d338d14192ff34482b496d4a5c3cf9edb8fe0b4a921d718",
    ("linux", "aarch64"): "a33f8efedbc1b0121f85d4e23bc6252fd81eb626521b383e6447564589a6155c",
}
# Same tile-set and Detour payload format before the live corridor ABI.
PRE_CORRIDOR_SOURCE_IDS = {
    ("darwin", "arm64"): "789efe3bd002bc7c285e2ef142b16948139445bf40c1ff90594843bcd36bc27c",
    ("darwin", "x86_64"): "8808d776a60042223380838941f7fa0e461d8f5c481ec3db31464b5f15702f2a",
    ("linux", "x86_64"): "c8f40685898d39172ed8dbffd87d135b89a59569b2ff9f4cb30d29be043e9cf0",
    ("linux", "aarch64"): "f1490379159ada6653552803e119e05ada940661dc074260930fc7c60bde6848",
}
# The forward raycast only adds a query. Existing tile and Detour payloads still load.
PRE_RAYCAST_SOURCE_IDS = {
    ("darwin", "arm64"): "caa44594afce0476408dc9bcd6045327ad1963e7bdf2d47ff7e6c28b7bf8894c",
    ("darwin", "x86_64"): "1508231798c4f2c269aafcabc9ef674114f12ffd26240d233b2b1d0476fe78d4",
    ("linux", "x86_64"): "bf2f74a6481a057382e9657c409f870eb5821f07f9c76fac914d9b1a76c4652c",
    ("linux", "aarch64"): "09adc596c170a055c02cf8893282409942c47f5ae174c840629db436145746c5",
}


class NavigationError(ValueError):
    """A specific, non-executable navigation failure."""


def source_id() -> str:
    digest = hashlib.sha256((REVISION + sys.platform + platform.machine()).encode())
    for p in sorted(SOURCE.rglob("*")):
        if p.suffix in {".cpp", ".h"}:
            digest.update(p.relative_to(SOURCE).as_posix().encode())
            digest.update(p.read_bytes())
    return digest.hexdigest()


def artifact_source_matches(value: str | None, *, tiled: bool) -> bool:
    platform_key = (sys.platform, platform.machine())
    return isinstance(value, str) and (value in {source_id(), PRE_RAYCAST_SOURCE_IDS.get(platform_key),
        PRE_CORRIDOR_SOURCE_IDS.get(platform_key)}
        or (not tiled and value == SINGLE_TILE_SOURCE_IDS.get(platform_key)))


def build_backend() -> Path:
    if sys.platform not in {"darwin", "linux"}:
        raise NavigationError("backend_platform_unsupported")
    folder = ROOT / "data/nav3d/backend" / source_id()
    library = folder / ("libnav3d.dylib" if sys.platform == "darwin" else "libnav3d.so")
    if library.is_file():
        return library
    compiler = shutil.which(os.environ.get("CXX", "c++"))
    if not compiler:
        raise NavigationError("backend_cpp_compiler_missing")
    folder.mkdir(parents=True, exist_ok=True)
    sources = [SOURCE / "bridge.cpp"]
    for module in ("Recast", "Detour"):
        sources.extend(sorted((VENDOR / module / "Source").glob("*.cpp")))
    # Unique output makes concurrent test/CLI builds safe; publish atomically.
    with tempfile.TemporaryDirectory(dir=folder) as temp:
        output = Path(temp) / library.name
        args = [compiler, "-std=c++17", "-O2", "-fPIC",
                "-dynamiclib" if sys.platform == "darwin" else "-shared",
                "-I", str(VENDOR / "Recast/Include"), "-I", str(VENDOR / "Detour/Include"),
                *map(str, sources), "-o", str(output)]
        if sys.platform == "darwin":
            sdk = subprocess.run(["xcrun", "--show-sdk-path"], capture_output=True,
                                 text=True, timeout=10)
            if sdk.returncode:
                raise NavigationError("backend_macos_sdk_missing")
            sdk_path = Path(sdk.stdout.strip())
            args += ["-isysroot", str(sdk_path)]
            # Some CLT distributions ship libc++ headers inside the SDK only.
            headers = sdk_path / "usr/include/c++/v1"
            if headers.is_dir():
                args += ["-isystem", str(headers)]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired as exc:
            raise NavigationError("backend_compile_timeout") from exc
        if result.returncode:
            raise NavigationError("backend_compile_failed: " + result.stderr[-5000:])
        output.replace(library)
    return library


FLOAT = C.POINTER(C.c_float)
INT = C.POINTER(C.c_int)
BYTE = C.POINTER(C.c_ubyte)
REF = C.POINTER(C.c_uint64)
MAX_PATH_POLYGONS = 8192


def floats(value):
    return np.ascontiguousarray(value, dtype=np.float32)


def fp(value):
    return value.ctypes.data_as(FLOAT)


def to_native(value):
    value = np.asarray(value)
    return floats(np.stack((value[..., 0], value[..., 2], -value[..., 1]), axis=-1))


def from_native(value):
    value = np.asarray(value)
    return np.stack((value[..., 0], -value[..., 2], value[..., 1]), axis=-1)


class Backend:
    def __init__(self):
        self.lib = C.CDLL(str(build_backend()))
        self.lib.wa_free.argtypes = [C.c_void_p]
        self.lib.wa_free.restype = None
        self.lib.wa_build.argtypes = [FLOAT, C.c_int, INT, C.c_int, FLOAT,
                                     C.POINTER(BYTE), INT, C.c_char_p, C.c_int]
        self.lib.wa_build_tiles.argtypes = [FLOAT, C.c_int, INT, C.c_int, FLOAT, C.c_int,
                                           C.POINTER(BYTE), INT, C.c_char_p, C.c_int]
        self.lib.wa_query.argtypes = [BYTE, C.c_int, FLOAT, FLOAT, C.c_float, C.c_float,
                                     C.c_float, FLOAT, C.c_int, FLOAT, C.c_int, C.c_char_p, C.c_int]
        self.lib.wa_open_mesh.argtypes = [BYTE, C.c_int, C.c_char_p, C.c_int]
        self.lib.wa_open_mesh.restype = C.c_void_p
        self.lib.wa_close_mesh.argtypes = [C.c_void_p]
        self.lib.wa_close_mesh.restype = None
        self.lib.wa_query_mesh.argtypes = [C.c_void_p, FLOAT, FLOAT, C.c_float, C.c_float,
                                          C.c_float, FLOAT, C.c_int, REF, INT, C.c_char_p, C.c_int]
        self.lib.wa_advance_corridor.argtypes = [C.c_void_p, FLOAT, FLOAT, C.c_float, FLOAT,
                                                REF, C.c_int, C.c_char_p, C.c_int]
        self.lib.wa_render_corridor.argtypes = [C.c_void_p, FLOAT, FLOAT, C.c_float, REF,
                                               C.c_int, FLOAT, C.c_int, C.c_char_p, C.c_int]
        self.lib.wa_corridor_raycast.argtypes = [C.c_void_p, C.c_uint64, FLOAT, FLOAT, FLOAT, C.c_char_p, C.c_int]
        self.lib.wa_debug.argtypes = [BYTE, C.c_int, FLOAT, C.c_int, C.c_char_p, C.c_int]

    @staticmethod
    def check(result, error):
        if result < 0:
            raise NavigationError(error.value.decode("utf-8", errors="replace"))
        return result

    def build(self, vertices, triangles, config, *, tile_cells=0):
        if type(tile_cells) is not int or (tile_cells and not 32 <= tile_cells <= 512):
            raise NavigationError("tile_cell_limit")
        vertices = to_native(vertices)
        triangles = np.ascontiguousarray(triangles, dtype=np.int32)
        cfg = floats(config)
        output, size, err = BYTE(), C.c_int(), C.create_string_buffer(512)
        args = (fp(vertices), len(vertices), triangles.ctypes.data_as(INT), len(triangles), fp(cfg))
        build = self.lib.wa_build
        if tile_cells:
            build = self.lib.wa_build_tiles
            args = (*args, tile_cells)
        count = self.check(build(*args, C.byref(output), C.byref(size), err, len(err)), err)
        try:
            return C.string_at(output, size.value), count
        finally:
            self.lib.wa_free(output)

    def prepare(self, blob):
        data = C.cast(C.c_char_p(blob), BYTE)
        err = C.create_string_buffer(512)
        handle = self.lib.wa_open_mesh(data, len(blob), err, len(err))
        if not handle:
            self.check(-1, err)
        return handle

    def query(self, blob, start, goal, horizontal_snap, vertical_snap, spacing, boxes, *, prepared=None,
              corridor=None):
        start, goal = to_native(start), to_native(goal)
        out, err = np.empty((32768, 3), dtype=np.float32), C.create_string_buffer(512)
        endpoints = (fp(start), fp(goal), horizontal_snap, vertical_snap, spacing)
        output = (fp(out), len(out), err, len(err))
        if prepared and not len(boxes):
            count = C.c_int() if corridor is not None else None
            result = self.lib.wa_query_mesh(prepared, *endpoints, fp(out), len(out), corridor,
                                            C.byref(count) if count is not None else None, err, len(err))
            if count is not None:
                return from_native(out[:self.check(result, err)]).astype(float), count.value
        else:
            data = C.cast(C.c_char_p(blob), BYTE)
            # Transform both box corners, then reorder after the axis reflection.
            transformed = to_native(np.asarray(boxes, dtype=float).reshape(-1, 2, 3))
            boxes = floats(np.stack((transformed.min(axis=1), transformed.max(axis=1)), axis=1))
            result = self.lib.wa_query(data, len(blob), *endpoints, fp(boxes), len(boxes), *output)
        n = self.check(result, err)
        return from_native(out[:n]).astype(float)

    def advance_corridor(self, prepared, start, position, tolerance, refs, count):
        start, position = to_native(start), to_native(position)
        projected, err = np.empty(3, dtype=np.float32), C.create_string_buffer(512)
        count = self.check(self.lib.wa_advance_corridor(prepared, fp(start), fp(position), tolerance,
                            fp(projected), refs, count, err, len(err)), err)
        return from_native(projected).astype(float), count

    def render_corridor(self, prepared, start, goal, spacing, refs, count):
        start, goal = to_native(start), to_native(goal)
        out, err = np.empty((32768, 3), dtype=np.float32), C.create_string_buffer(512)
        size = self.check(self.lib.wa_render_corridor(prepared, fp(start), fp(goal), spacing,
                          refs, count, fp(out), len(out), err, len(err)), err)
        return from_native(out[:size]).astype(float)

    def corridor_raycast(self, prepared, start_ref, start, end):
        start, end = to_native(start), to_native(end)
        fraction, err = C.c_float(), C.create_string_buffer(512)
        self.check(self.lib.wa_corridor_raycast(prepared, start_ref, fp(start), fp(end),
                   C.byref(fraction), err, len(err)), err)
        return fraction.value

    def debug(self, blob):
        data = C.cast(C.c_char_p(blob), BYTE)
        err = C.create_string_buffer(512)
        n = self.check(self.lib.wa_debug(data, len(blob), None, 0, err, len(err)), err)
        if n > 2000000:
            raise NavigationError("debug_geometry_limit")
        out = np.empty((n, 3, 3), dtype=np.float32)
        self.check(self.lib.wa_debug(data, len(blob), fp(out), n, err, len(err)), err)
        return from_native(out).astype(float)
