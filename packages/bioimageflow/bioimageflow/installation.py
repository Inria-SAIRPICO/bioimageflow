"""Admission and exclusive publication of one versioned tool installation."""

from __future__ import annotations

import base64
import csv
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import importlib.machinery
import importlib.metadata
import os
import re
from pathlib import Path, PurePosixPath
import sys

from packaging.version import InvalidVersion, Version

from bioimageflow.worker_origins import _canonical_distribution, _distribution_imports


@dataclass(frozen=True)
class InstalledPackage:
    root: Path
    distribution: str
    version: str
    import_package: str
    member: Path


def validate_installation_selectors(import_package: str, version: str, distribution: str | None) -> None:
    if (type(import_package) is not str or not import_package.isidentifier()
            or type(version) is not str or not version or version in {'.', '..'}
            or '/' in version or '\\' in version
            or distribution is not None and (type(distribution) is not str
                or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", distribution) is None)):
        raise ValueError("Installation selectors must identify one package/version directory")
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ValueError("Installation version must be a valid PEP 440 token") from exc


def installation_target(store: Path, import_package: str, version: str) -> Path:
    """Resolve the selected parent before any installation filesystem effects."""
    root = store.resolve()
    parent = root / import_package
    try:
        parent.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Installation package parent escapes selected store: {parent}") from exc
    target = parent / version
    if target.is_symlink():
        raise ValueError(f"Installation target is a symbolic link: {target}")
    return target


def admit_installation(
    root: Path, import_package: str, version: str, *, distribution: str | None = None,
) -> InstalledPackage:
    """Validate the actual distribution and owned import member, without imports.

    A directory or a readiness flag is not installation evidence. The selected
    distribution's RECORD is admitted once, including hashes where declared.
    Initializer execution and dependency authority are separate loader gates.
    """
    validate_installation_selectors(import_package, version, distribution)
    label = f"{import_package}=={version} in {root}"
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Installation {label} is not an owned directory")
    root = root.resolve(strict=True)
    candidates = [dist for dist in importlib.metadata.distributions(path=[str(root)])
                  if import_package in _distribution_imports(dist)
                  and (distribution is None or _canonical_distribution(dist.metadata['Name'] or '')
                       == _canonical_distribution(distribution))]
    if len(candidates) != 1:
        raise ValueError(f"Installation {label} requires exactly one providing distribution")
    selected = candidates[0]
    name = selected.metadata["Name"]
    if not name or selected.version != version:
        raise ValueError(f"Installation {label} has mismatched distribution/version {name!r}=={selected.version!r}")
    record = selected.read_text("RECORD")
    if not record:
        raise ValueError(f"Installation {label} has no RECORD")
    records: set[Path] = set()
    for row in csv.reader(record.splitlines()):
        if len(row) != 3:
            raise ValueError(f"Installation {label} has malformed RECORD")
        relative, digest, size = row
        path = PurePosixPath(relative)
        if not relative or path.is_absolute() or '..' in path.parts or '\\' in relative:
            raise ValueError(f"Installation {label} has an escaping RECORD member {relative!r}")
        member = root.joinpath(*path.parts)
        try:
            resolved = member.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Installation {label} has an unavailable/outside RECORD member {relative!r}") from exc
        if resolved in records or not resolved.is_file():
            raise ValueError(f"Installation {label} has a duplicate/nonfile RECORD member {relative!r}")
        records.add(resolved)
        if size and (not size.isdecimal() or resolved.stat().st_size != int(size)):
            raise ValueError(f"Installation {label} has mismatched RECORD size for {relative!r}")
        if digest:
            algorithm, separator, expected = digest.partition('=')
            if not separator or algorithm not in {'sha256', 'sha384', 'sha512'}:
                raise ValueError(f"Installation {label} has unsupported RECORD digest for {relative!r}")
            value = hashlib.new(algorithm)
            with resolved.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    value.update(chunk)
            actual = base64.urlsafe_b64encode(value.digest()).decode().rstrip('=')
            if actual != expected:
                raise ValueError(f"Installation {label} has mismatched RECORD hash for {relative!r}")
    spec = importlib.machinery.PathFinder.find_spec(import_package, [str(root)])
    if spec is None or spec.origin is None or spec.loader is None:
        raise ValueError(f"Installation {label} has no concrete import member")
    member = Path(spec.origin).resolve(strict=True)
    try:
        member.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Installation {label} resolves outside its root") from exc
    metadata_files = [Path(str(selected.locate_file(path))).resolve(strict=True)
                      for path in selected.files or () if path.name == 'METADATA']
    if member not in records or len(metadata_files) != 1 or metadata_files[0] not in records:
        raise ValueError(f"Installation {label} does not record its import member and METADATA")
    return InstalledPackage(root, _canonical_distribution(name), selected.version, import_package, member)


def publish_installation(stage: Path, destination: Path) -> None:
    """Atomically publish without replacing any racing file or directory.

    Darwin RENAME_EXCL: apple-oss-distributions/xnu bsd/sys/stdio.h.
    Linux RENAME_NOREPLACE: linux include/uapi/linux/fs.h.
    Windows os.rename always refuses an existing destination (Python docs).
    Unsupported platforms/primitives fail rather than emulate check-then-rename.
    """
    if os.name == 'nt':
        os.rename(stage, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        operation = getattr(libc, 'renamex_np', None)
        if operation is None:
            raise NotImplementedError('Atomic exclusive installation publication unavailable')
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(os.fsencode(stage), os.fsencode(destination), 0x00000004)
    elif sys.platform.startswith('linux'):
        operation = getattr(libc, 'renameat2', None)
        if operation is None:
            raise NotImplementedError('Atomic exclusive installation publication unavailable')
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(-100, os.fsencode(stage), -100, os.fsencode(destination), 1)
    else:
        raise NotImplementedError('Atomic exclusive installation publication unavailable')
    if result:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(error, os.strerror(error), str(destination))
        raise OSError(error, os.strerror(error), str(destination))
