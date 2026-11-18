"""Read-only local CASC access. Never downloads assets or masks encrypted data."""
from __future__ import annotations

import ctypes as C
import csv
import hashlib
from pathlib import Path

from .backend import NavigationError

CASC_COMMIT = "6b889ec8f421cbc9baccec296c582362ab917887"
EXPORT_COMMIT = "81cc25e4c500d2153c503b381eba82a906f52ba8"


def client_build(root, product="wow"):
    rows = csv.DictReader((Path(root)/".build.info").read_text().splitlines(), delimiter="|")
    matches = [{k.split("!")[0]: v for k, v in row.items()} for row in rows]
    matches = [r for r in matches if r.get("Product") == product and r.get("Active") == "1"]
    if len(matches) != 1:
        raise NavigationError("casc_product_build_ambiguous")
    return matches[0]


class LocalCASC:
    def __init__(self, root, library, *, product="wow", cache=None):
        self.root = Path(root).resolve()
        self.build = client_build(self.root, product)
        self.cache = Path(cache) / self.build["Build Key"] if cache else None
        self.records = {}
        self.lib = C.CDLL(str(Path(library).resolve()))
        for name, args, result in (
            ("CascOpenStorage", [C.c_char_p, C.c_uint32, C.POINTER(C.c_void_p)], C.c_bool),
            ("CascOpenFile", [C.c_void_p, C.c_void_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_void_p)], C.c_bool),
            ("CascGetFileSize64", [C.c_void_p, C.POINTER(C.c_uint64)], C.c_bool),
            ("CascReadFile", [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)], C.c_bool),
            ("CascCloseFile", [C.c_void_p], C.c_bool),
            ("CascCloseStorage", [C.c_void_p], C.c_bool),
        ):
            fn = getattr(self.lib, name); fn.argtypes = args; fn.restype = result
        self.handle = C.c_void_p()
        # Product selection matters when retail and another client share Data/.
        if not self.lib.CascOpenStorage(f"{self.root}*{product}".encode(), 0x40, C.byref(self.handle)):
            raise NavigationError("casc_open_local_storage_failed")

    def read(self, file_id):
        numeric = isinstance(file_id, int)
        if numeric and not 0 < file_id < 2**32:
            raise NavigationError("invalid_file_data_id")
        key = str(file_id)
        cached = self.cache / hashlib.sha256(key.encode()).hexdigest() if self.cache else None
        checksum = cached.with_suffix(".sha256") if cached else None
        if cached and cached.exists() and checksum.exists():
            data = cached.read_bytes()
            if hashlib.sha256(data).hexdigest() != checksum.read_text().strip():
                raise NavigationError(f"casc_cache_checksum_mismatch:{file_id}")
        else:
            handle = C.c_void_p()
            name = C.c_void_p(file_id) if numeric else C.cast(C.c_char_p(key.encode()), C.c_void_p)
            if not self.lib.CascOpenFile(self.handle, name, 0x40, (3 if numeric else 0) | 0x10, C.byref(handle)):
                raise NavigationError(f"casc_missing_local_file:{file_id}")
            try:
                size = C.c_uint64()
                if not self.lib.CascGetFileSize64(handle, C.byref(size)) or size.value > 256*1024*1024:
                    raise NavigationError(f"casc_invalid_file_size:{file_id}")
                buf, actual = C.create_string_buffer(size.value), C.c_uint32()
                if not self.lib.CascReadFile(handle, buf, size.value, C.byref(actual)) or actual.value != size.value:
                    raise NavigationError(f"casc_unreadable_or_encrypted_file:{file_id}")
                data = buf.raw
            finally:
                self.lib.CascCloseFile(handle)
            if cached:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(data)
                checksum.write_text(hashlib.sha256(data).hexdigest())
        self.records[key] = {"file": file_id, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        return data

    def close(self):
        if self.handle:
            self.lib.CascCloseStorage(self.handle)
            self.handle = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
