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


class NavigationError(ValueError):
    """A specific, non-executable navigation failure."""


def source_id() -> str:
    digest = hashlib.sha256((REVISION + sys.platform + platform.machine()).encode())
    for p in sorted(SOURCE.rglob("*")):
        if p.suffix in {".cpp", ".h"}:
            digest.update(p.relative_to(SOURCE).as_posix().encode())
            digest.update(p.read_bytes())
    return digest.hexdigest()


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
        self.lib.wa_query.argtypes = [BYTE, C.c_int, FLOAT, FLOAT, C.c_float, C.c_float,
                                     C.c_float, FLOAT, C.c_int, FLOAT, C.c_int, C.c_char_p, C.c_int]
        self.lib.wa_debug.argtypes = [BYTE, C.c_int, FLOAT, C.c_int, C.c_char_p, C.c_int]

    @staticmethod
    def check(result, error):
        if result < 0:
            raise NavigationError(error.value.decode("utf-8", errors="replace"))
        return result

    def build(self, vertices, triangles, config):
        vertices = to_native(vertices)
        triangles = np.ascontiguousarray(triangles, dtype=np.int32)
        cfg = floats(config)
        output, size, err = BYTE(), C.c_int(), C.create_string_buffer(512)
        count = self.check(self.lib.wa_build(fp(vertices), len(vertices), triangles.ctypes.data_as(INT),
                          len(triangles), fp(cfg), C.byref(output), C.byref(size), err, len(err)), err)
        try:
            return C.string_at(output, size.value), count
        finally:
            self.lib.wa_free(output)

    def query(self, blob, start, goal, horizontal_snap, vertical_snap, spacing, boxes):
        data = (C.c_ubyte * len(blob)).from_buffer_copy(blob)
        start, goal = to_native(start), to_native(goal)
        # Transform both box corners, then reorder after the axis reflection.
        transformed = to_native(np.asarray(boxes, dtype=float).reshape(-1, 2, 3))
        boxes = floats(np.stack((transformed.min(axis=1), transformed.max(axis=1)), axis=1))
        out, err = np.empty((32768, 3), dtype=np.float32), C.create_string_buffer(512)
        n = self.check(self.lib.wa_query(data, len(blob), fp(start), fp(goal), horizontal_snap,
                       vertical_snap, spacing, fp(boxes), len(boxes), fp(out), len(out), err, len(err)), err)
        return from_native(out[:n]).astype(float)

    def debug(self, blob):
        data = (C.c_ubyte * len(blob)).from_buffer_copy(blob)
        err = C.create_string_buffer(512)
        n = self.check(self.lib.wa_debug(data, len(blob), None, 0, err, len(err)), err)
        if n > 2000000:
            raise NavigationError("debug_geometry_limit")
        out = np.empty((n, 3, 3), dtype=np.float32)
        self.check(self.lib.wa_debug(data, len(blob), fp(out), n, err, len(err)), err)
        return from_native(out).astype(float)
