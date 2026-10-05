"""Contained NPY backing and finite cross-process allocation reservations."""

import hashlib
import io
import json
import math
import mmap
import os
import re
import stat
import struct
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

TOKEN = re.compile(r"[a-zA-Z0-9_-]{1,96}\Z")


def token(value: Any) -> str:
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ValueError("Invalid shared allocation identity")
    return value


def identity(path: Path) -> list[int]:
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode):
        raise ValueError("Shared scope root must be a real directory")
    return [value.st_dev, value.st_ino]


def validate_scope_descriptor(value: Any) -> dict[str, Any]:
    """Validate admission metadata without opening, resolving or mapping any file."""
    keys = {"scope_id", "root", "root_identity", "owner_id", "owner_root",
            "owner_root_identity", "max_bytes", "max_header_bytes"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Invalid shared scope descriptor fields")
    result = dict(value)
    for key in ("scope_id", "owner_id"):
        token(result[key])
    for key in ("root", "owner_root"):
        if not isinstance(result[key], str) or not Path(result[key]).is_absolute():
            raise ValueError("Shared scope roots must be absolute")
        if ".." in Path(result[key]).parts:
            raise ValueError("Invalid shared scope path")
    for key in ("root_identity", "owner_root_identity"):
        pair = result[key]
        if not isinstance(pair, (tuple, list)) or len(pair) != 2 or any(
            type(item) is not int or item < 0 for item in pair
        ):
            raise ValueError("Invalid shared scope root identity")
        result[key] = list(pair)
    for key in ("max_bytes", "max_header_bytes"):
        if type(result[key]) is not int or result[key] <= 0:
            raise ValueError("Shared scope budgets must be finite positive integers")
    return result


def verify(descriptor: dict[str, Any]) -> None:
    for root, expected in (("root", "root_identity"), ("owner_root", "owner_root_identity")):
        if identity(Path(descriptor[root])) != descriptor[expected]:
            raise ValueError("Shared scope root identity changed")
    path = Path(descriptor["root"]) / ".scope.json"
    with _open(path, os.O_RDONLY, descriptor["root_identity"]) as handle:
        marker = json.load(handle)
    if marker != {"scope_id": descriptor["scope_id"], "owner_id": descriptor["owner_id"]}:
        raise ValueError("Shared scope marker mismatch")


@contextmanager
def _open(path: Path, flags: int, expected_parent: Any = None) -> Iterator[Any]:
    parent_fd = None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise ValueError("Shared backing must be a regular file, not a symlink")
    if os.open in os.supports_dir_fd:
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow)
        parent_stat = os.fstat(parent_fd)
        if expected_parent is not None and [parent_stat.st_dev, parent_stat.st_ino] != expected_parent:
            os.close(parent_fd)
            raise ValueError("Shared scope directory changed before admission")
    elif expected_parent is not None and identity(path.parent) != expected_parent:
        raise ValueError("Shared scope directory changed before admission")
    try:
        fd = os.open(path.name if parent_fd is not None else path, flags | nofollow,
                     0o600, dir_fd=parent_fd)
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    try:
        actual = os.fstat(fd)
        if not stat.S_ISREG(actual.st_mode):
            raise ValueError("Shared backing must be a regular file")
        if before is not None and (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Shared file identity changed during admission")
        with os.fdopen(fd, "r+b" if flags & os.O_RDWR else "rb") as handle:
            fd = -1
            yield handle
    finally:
        if fd != -1:
            os.close(fd)


@contextmanager
def quota(descriptor: dict[str, Any]) -> Iterator[dict[str, int]]:
    """One captured owner budget shared by all admitted task namespaces."""
    verify(descriptor)
    root = Path(descriptor["owner_root"])
    with _open(root / ".quota.lock", os.O_RDWR, descriptor["owner_root_identity"]) as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with _open(root / ".quota.json", os.O_RDONLY, descriptor["owner_root_identity"]) as handle:
                reservations = json.load(handle)
                if not isinstance(reservations, dict):
                    raise ValueError("Invalid owned allocation ledger")
                for key, size in reservations.items():
                    scope_id, name = key.split(":")
                    token(scope_id)
                    token(name)
                    if type(size) is not int or size <= 0:
                        raise ValueError("Invalid owned allocation reservation")
            original = dict(reservations)
            yield reservations
            if reservations == original:
                return
            temporary = root / ".quota.tmp"
            # A crashed writer may leave only this captured, exclusively owned
            # scratch record. The previously published ledger stays complete.
            if temporary.exists() or temporary.is_symlink():
                if not stat.S_ISREG(temporary.lstat().st_mode):
                    raise ValueError("Refusing substituted quota scratch file")
                temporary.unlink()
            with _open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL, descriptor["owner_root_identity"]) as handle:
                handle.write(json.dumps(reservations, sort_keys=True).encode())
                handle.flush()
            if identity(root) != descriptor["owner_root_identity"]:
                raise ValueError("Shared owner root changed during quota publication")
            os.replace(temporary, root / ".quota.json")
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def dtype(value: Any) -> Any:
    import numpy as np
    try:
        result = np.dtype(value)
    except TypeError:
        # NumPy's own structured dtype label is a literal, never executable code.
        import ast
        result = np.dtype(ast.literal_eval(value))
    if result.hasobject:
        raise ValueError("Shared memory arrays cannot contain Python objects.")
    return result


def create(descriptor: dict[str, Any], name: str, array: Any) -> None:
    import numpy as np
    dtype(array.dtype)
    token(name)
    header = io.BytesIO()
    np.lib.format.write_array_header_2_0(header, np.lib.format.header_data_from_array_1_0(array))
    if header.tell() - 12 > descriptor["max_header_bytes"]:
        raise ValueError("Shared array header exceeds admitted header budget")
    size = header.tell() + array.nbytes
    key = descriptor["scope_id"] + ":" + name
    with quota(descriptor) as allocations:
        if key in allocations:
            raise ValueError("Allocation name already owned")
        if sum(allocations.values()) + size > descriptor["max_bytes"]:
            raise ValueError("Shared array exceeds captured byte budget")
        allocations[key] = size
    path = Path(descriptor["root"]) / (name + ".npy")
    created = False
    try:
        verify(descriptor)
        with _open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, descriptor["root_identity"]) as handle:
            created = True
            np.lib.format.write_array(handle, array, version=(2, 0), allow_pickle=False)
            handle.flush()
            if os.fstat(handle.fileno()).st_size != size:
                raise ValueError("Shared allocation size does not match reservation")
    except BaseException as primary:
        try:
            if created:
                delete(descriptor, name)
            else:
                with quota(descriptor) as allocations:
                    allocations.pop(key, None)
        except BaseException as cleanup_error:
            # Retain the owned reservation and the original failed creation.
            raise primary from cleanup_error
        raise


def _sealed_marker(descriptor: dict[str, Any], name: str) -> Path:
    return Path(descriptor["root"]) / ("." + token(name) + ".sealed.json")


def _seal_record(descriptor: dict[str, Any], name: str) -> Any:
    try:
        with _open(_sealed_marker(descriptor, name), os.O_RDONLY,
                   descriptor["root_identity"]) as handle:
            metadata_budget = 6 * descriptor["max_header_bytes"] + 4096
            payload = handle.read(metadata_budget + 1)
            if len(payload) > metadata_budget:
                raise ValueError("Sealed backing marker exceeds its metadata budget")
            record = json.loads(payload)
            if (not isinstance(record, dict) or set(record) != {"identity", "sha256", "shape", "dtype"}
                    or not isinstance(record["identity"], list) or len(record["identity"]) != 3
                    or any(type(value) is not int or value < 0 for value in record["identity"])
                    or not isinstance(record["sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])
                    or not isinstance(record["shape"], list)
                    or any(type(n) is not int or n < 0 for n in record["shape"])
                    or not isinstance(record["dtype"], str)):
                raise ValueError("Invalid sealed backing identity")
            return record
    except FileNotFoundError:
        return None


def _require_sealed_identity(record: Any, value: Any) -> None:
    if record["identity"] != [value.st_dev, value.st_ino, value.st_size] or value.st_mode & 0o222:
        raise ValueError("Sealed backing identity or access policy changed")


def sealed(descriptor: dict[str, Any], ref: Any) -> bool:
    """Admit the persisted immutable backing identity, never a descriptor flag."""
    verify(descriptor)
    record = _seal_record(descriptor, ref.name)
    if record is None:
        return False
    if record["shape"] != list(ref.shape) or record["dtype"] != ref.dtype:
        raise ValueError("Sealed metadata does not match its reference")
    path = Path(descriptor["root"]) / (token(ref.name) + ".npy")
    with _open(path, os.O_RDONLY, descriptor["root_identity"]) as handle:
        _require_sealed_identity(record, os.fstat(handle.fileno()))
    return True


def seal(descriptor: dict[str, Any], ref: Any) -> None:
    """Publish read-only access for a newly independent controller allocation."""
    verify(descriptor)
    name = token(ref.name)
    path = Path(descriptor["root"]) / (name + ".npy")
    with _open(path, os.O_RDWR, descriptor["root_identity"]) as handle:
        value = os.fstat(handle.fileno())
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), 0o400)
        else:
            os.chmod(path, 0o400)
        with _open(_sealed_marker(descriptor, name), os.O_RDWR | os.O_CREAT | os.O_EXCL,
                   descriptor["root_identity"]) as marker:
            marker.write(json.dumps({"identity": [value.st_dev, value.st_ino, value.st_size],
                                     "sha256": digest.hexdigest(), "shape": list(ref.shape),
                                     "dtype": ref.dtype}).encode())
            marker.flush()


def content_identity(descriptor: dict[str, Any], ref: Any) -> dict[str, Any]:
    """Read an accepted digest, or preview current mutable bytes without publishing."""
    verify(descriptor)
    record = _seal_record(descriptor, ref.name)
    path = Path(descriptor["root"]) / (token(ref.name) + ".npy")
    with _open(path, os.O_RDONLY, descriptor["root_identity"]) as handle:
        if record is not None:
            _require_sealed_identity(record, os.fstat(handle.fileno()))
            if record["shape"] != list(ref.shape) or record["dtype"] != ref.dtype:
                raise ValueError("Sealed metadata does not match its reference")
            digest = record["sha256"]
        else:
            with quota(descriptor) as allocations:
                size = allocations.get(descriptor["scope_id"] + ":" + ref.name)
            _layout(handle, descriptor, ref, size)
            handle.seek(0)
            hasher = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
            digest = hasher.hexdigest()
    return {"shape": list(ref.shape), "dtype": ref.dtype, "sha256": digest}


def _layout(handle: Any, descriptor: dict[str, Any], ref: Any, reserved_size: Any) -> tuple[Any, ...]:
    import numpy as np
    expected_dtype = dtype(ref.dtype)
    if not isinstance(ref.shape, tuple) or any(type(n) is not int or n < 0 for n in ref.shape):
        raise ValueError("Invalid shared array shape")
    prefix = handle.read(12)
    if len(prefix) != 12 or prefix[:8] != b"\x93NUMPY\x02\x00":
        raise ValueError("Shared backing must use the current NPY2 format")
    header_size = struct.unpack("<I", prefix[8:])[0]
    if header_size > descriptor["max_header_bytes"]:
        raise ValueError("Shared array header exceeds admitted header budget")
    handle.seek(0)
    np.lib.format.read_magic(handle)
    shape, fortran, actual_dtype = np.lib.format.read_array_header_2_0(
        handle, max_header_size=descriptor["max_header_bytes"]
    )
    dtype(actual_dtype)
    offset = handle.tell()
    expected_size = offset + math.prod(shape) * actual_dtype.itemsize
    if shape != ref.shape or actual_dtype != expected_dtype:
        raise ValueError("Shared array header does not match its reference")
    if (expected_size != reserved_size or expected_size > descriptor["max_bytes"]
            or os.fstat(handle.fileno()).st_size != expected_size):
        raise ValueError("Shared backing size does not match admitted numeric layout")
    return shape, actual_dtype, offset, fortran


def map_array(descriptor: dict[str, Any], ref: Any, *, writable: Any = None) -> tuple[Any, mmap.mmap]:
    import numpy as np
    dtype(ref.dtype)
    token(ref.name)
    if not isinstance(ref.shape, tuple) or any(type(n) is not int or n < 0 for n in ref.shape):
        raise ValueError("Invalid shared array shape")
    verify(descriptor)
    with quota(descriptor) as allocations:
        reserved_size = allocations.get(descriptor["scope_id"] + ":" + ref.name)
    if reserved_size is None:
        raise ValueError("Shared backing has no owned allocation reservation")
    record = _seal_record(descriptor, ref.name)
    readonly = record is not None
    if readonly and writable is True:
        raise PermissionError("Accepted shared backing is read-only")
    path = Path(descriptor["root"]) / (ref.name + ".npy")
    with _open(path, os.O_RDONLY if readonly or writable is False else os.O_RDWR, descriptor["root_identity"]) as handle:
        if readonly:
            _require_sealed_identity(record, os.fstat(handle.fileno()))
        shape, actual_dtype, offset, fortran = _layout(handle, descriptor, ref, reserved_size)
        # Header admission and mapping use this same captured descriptor.
        mapping = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ if readonly or writable is False else mmap.ACCESS_WRITE)
    try:
        array = np.ndarray(shape, dtype=actual_dtype, buffer=mapping, offset=offset,
                           order="F" if fortran else "C")
        return array, mapping
    except BaseException:
        mapping.close()
        raise


def delete(descriptor: dict[str, Any], name: str) -> None:
    token(name)
    verify(descriptor)
    path = Path(descriptor["root"]) / (name + ".npy")
    try:
        value = path.lstat()
    except FileNotFoundError:
        value = None
    if value is not None:
        if not stat.S_ISREG(value.st_mode):
            raise ValueError("Refusing cleanup of substituted shared backing")
        # Windows cannot unlink a read-only file; only this owning cleanup may restore access.
        if not value.st_mode & 0o200:
            os.chmod(path, 0o600)
        path.unlink()
    marker = _sealed_marker(descriptor, name)
    if marker.exists() or marker.is_symlink():
        with _open(marker, os.O_RDONLY, descriptor["root_identity"]):
            pass
        marker.unlink()
    with quota(descriptor) as allocations:
        allocations.pop(descriptor["scope_id"] + ":" + name, None)
