"""Object stores for trajectories and content addressed blobs (DESIGN.md section 6.11).

The filesystem store backs development and tests; S3 or GCS implement the same
protocol in production. Steps are separate objects, so writing never needs an
append operation the cloud stores lack.
"""

import hashlib
import re
import threading
from pathlib import Path
from typing import Protocol

KEY_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9_.]+")


class ObjectStore(Protocol):
    def put_bytes(self, key: str, data: bytes) -> None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def delete(self, key: str) -> None: ...


class BlobIntegrityError(ValueError):
    """A blob's bytes no longer hash to its key."""


def validate_key(key: str) -> str:
    segments = key.split("/")
    if not segments or any(segment in ("", ".", "..") or not KEY_SEGMENT_PATTERN.fullmatch(segment)
                           for segment in segments):
        raise ValueError(f"invalid object key {key!r}")
    return key


class InMemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.lock = threading.Lock()

    def put_bytes(self, key: str, data: bytes) -> None:
        with self.lock:
            self.objects[validate_key(key)] = bytes(data)

    def get_bytes(self, key: str) -> bytes:
        with self.lock:
            if key not in self.objects:
                raise KeyError(key)
            return self.objects[key]

    def exists(self, key: str) -> bool:
        with self.lock:
            return key in self.objects

    def list_keys(self, prefix: str) -> list[str]:
        with self.lock:
            return sorted(key for key in self.objects if key.startswith(prefix))

    def delete(self, key: str) -> None:
        with self.lock:
            self.objects.pop(key, None)


class FilesystemObjectStore:
    def __init__(self, root_directory: Path) -> None:
        self.root_directory = root_directory.resolve()
        self.root_directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        return self.root_directory / validate_key(key)

    def put_bytes(self, key: str, data: bytes) -> None:
        object_path = self.path_for(key)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = object_path.with_name(f"{object_path.name}.partial")
        temporary_path.write_bytes(data)
        temporary_path.replace(object_path)

    def get_bytes(self, key: str) -> bytes:
        object_path = self.path_for(key)
        if not object_path.is_file():
            raise KeyError(key)
        return object_path.read_bytes()

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(
            object_path.relative_to(self.root_directory).as_posix()
            for object_path in self.root_directory.rglob("*")
            if object_path.is_file()
            and not object_path.name.endswith(".partial")
            and object_path.relative_to(self.root_directory).as_posix().startswith(prefix)
        )

    def delete(self, key: str) -> None:
        self.path_for(key).unlink(missing_ok=True)


def blob_key(digest: str) -> str:
    hex_digest = digest.removeprefix("sha256:")
    return f"blobs/{hex_digest[:2]}/{hex_digest}"


def put_blob(object_store: ObjectStore, data: bytes) -> str:
    """Store bytes under their sha256; identical screenshots and states are stored once."""
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    key = blob_key(digest)
    if not object_store.exists(key):
        object_store.put_bytes(key, data)
    return digest


def get_blob(object_store: ObjectStore, digest: str) -> bytes:
    data = object_store.get_bytes(blob_key(digest))
    if f"sha256:{hashlib.sha256(data).hexdigest()}" != digest:
        raise BlobIntegrityError(f"blob {digest} is corrupted")
    return data
