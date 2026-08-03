"""Immutable PSI/J pre-launch script preparation and installation."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal


MAX_PRE_LAUNCH_BYTES = 64 * 1024
PRE_LAUNCH_RELATIVE_PATH = "bootstrap/psij-pre-launch.sh"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _validate_digest(value: object, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("expected_digest must be a lowercase SHA-256 digest.")
    return value


def _validate_script_bytes(value: bytes) -> None:
    if not value:
        raise ValueError("A pre-launch script must not be empty.")
    if len(value) > MAX_PRE_LAUNCH_BYTES:
        raise ValueError("A pre-launch script must not exceed 64 KiB.")
    if b"\x00" in value:
        raise ValueError("A pre-launch script must not contain NUL bytes.")
    try:
        value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("A pre-launch script must contain valid UTF-8.") from error


def _cluster_path(value: object) -> PurePosixPath:
    if not isinstance(value, (str, PurePosixPath)):
        raise TypeError("Cluster pre-launch paths must be POSIX path-like values.")
    encoded = str(value)
    path = PurePosixPath(encoded)
    if (
        not encoded
        or any(character in encoded for character in ("\x00", "\n", "\r"))
        or not path.is_absolute()
        or encoded.startswith("//")
        or str(path) != encoded
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ValueError(
            "Cluster pre-launch paths must be normalized absolute POSIX paths."
        )
    return path


@dataclass(frozen=True, slots=True, repr=False)
class PreLaunchScript:
    """Transient source for one PSI/J orchestrator pre-launch script."""

    _kind: Literal["text", "local_file", "cluster_file"]
    _content: bytes | None = field(default=None, repr=False)
    _path: Path | PurePosixPath | None = field(default=None, repr=False)
    _expected_digest: str | None = field(default=None, repr=False)
    _expected_size: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._kind == "text":
            if (
                type(self._content) is not bytes
                or self._path is not None
                or self._expected_digest is not None
                or self._expected_size is not None
            ):
                raise ValueError("Invalid text pre-launch source.")
            _validate_script_bytes(self._content)
            return
        if self._kind == "local_file":
            if (
                not isinstance(self._path, Path)
                or self._content is not None
                or ((self._expected_digest is None) != (self._expected_size is None))
            ):
                raise ValueError("Invalid local pre-launch source.")
            if self._expected_digest is not None:
                _validate_digest(self._expected_digest)
                if (
                    type(self._expected_size) is not int
                    or self._expected_size <= 0
                    or self._expected_size > MAX_PRE_LAUNCH_BYTES
                ):
                    raise ValueError("Invalid local pre-launch source size.")
            return
        if self._kind == "cluster_file":
            if (
                not isinstance(self._path, PurePosixPath)
                or self._content is not None
                or self._expected_size is not None
            ):
                raise ValueError("Invalid cluster pre-launch source.")
            _cluster_path(self._path)
            _validate_digest(self._expected_digest, nullable=True)
            return
        raise ValueError("Unknown pre-launch source kind.")

    def __repr__(self) -> str:
        return f"PreLaunchScript(source_kind={self.source_kind!r})"

    @property
    def source_kind(self) -> Literal["text", "local_file", "cluster_file"]:
        return self._kind

    @classmethod
    def from_text(cls, text: str) -> "PreLaunchScript":
        """Create an uploaded source from inline UTF-8 shell text."""
        if type(text) is not str:
            raise TypeError("PreLaunchScript.from_text() requires a string.")
        try:
            content = text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise ValueError("A pre-launch script must contain valid UTF-8.") from error
        _validate_script_bytes(content)
        return cls("text", _content=content)

    @classmethod
    def from_local_file(cls, path: Path) -> "PreLaunchScript":
        """Create an uploaded source snapshotted from a local file."""
        if not isinstance(path, Path):
            raise TypeError("PreLaunchScript.from_local_file() requires a Path.")
        return cls("local_file", _path=path)

    @classmethod
    def from_cluster_file(
        cls,
        path: PurePosixPath | str,
        *,
        expected_digest: str | None = None,
    ) -> "PreLaunchScript":
        """Create a cluster-file source with an optional SHA-256 pin."""
        return cls(
            "cluster_file",
            _path=_cluster_path(path),
            _expected_digest=_validate_digest(expected_digest, nullable=True),
        )

    @classmethod
    def _from_uploaded_file(
        cls,
        path: Path,
        *,
        expected_digest: str,
        expected_size: int,
    ) -> "PreLaunchScript":
        if type(expected_size) is not int or expected_size <= 0:
            raise ValueError("Uploaded pre-launch size must be positive.")
        return cls(
            "local_file",
            _path=path,
            _expected_digest=_validate_digest(expected_digest),
            _expected_size=expected_size,
        )


@dataclass(frozen=True, slots=True)
class PreparedPreLaunch:
    source_kind: Literal["uploaded", "cluster_file"]
    source_path: str | None
    expected_digest: str | None
    path: Path = field(repr=False)
    size: int
    digest: str

    def persisted(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_path": self.source_path,
            "expected_digest": self.expected_digest,
            "artifact": {
                "path": PRE_LAUNCH_RELATIVE_PATH,
                "size": self.size,
                "digest": self.digest,
            },
        }


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )


def _copy_script(
    source: Path,
    destination: Path,
    *,
    expected_digest: str | None = None,
    expected_size: int | None = None,
) -> tuple[int, str]:
    before = source.stat(follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("A pre-launch source must be a regular non-symlink file.")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(source, flags)
    content = bytearray()
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("A pre-launch source must be a regular file.")
        while len(content) <= MAX_PRE_LAUNCH_BYTES:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_PRE_LAUNCH_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = source.stat(follow_symlinks=False)
    if (
        _identity(before) != _identity(opened)
        or _identity(before) != _identity(after)
        or _identity(before) != _identity(final)
    ):
        raise ValueError("A pre-launch source changed while it was read.")
    encoded = bytes(content)
    _validate_script_bytes(encoded)
    digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    if expected_digest is not None and digest != expected_digest:
        raise ValueError("A pre-launch source does not match its expected digest.")
    if expected_size is not None and len(encoded) != expected_size:
        raise ValueError("A pre-launch source does not match its expected size.")
    _write_script(destination, encoded)
    return len(encoded), digest


def _write_script(destination: Path, content: bytes) -> None:
    _validate_script_bytes(content)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise ValueError("The pre-launch destination directory is unsafe.")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.fchmod(descriptor, 0o400)
    finally:
        os.close(descriptor)
    _sync_directory(destination.parent)


@contextmanager
def prepare_pre_launch(
    script: PreLaunchScript | None,
) -> Iterator[PreparedPreLaunch | None]:
    if script is None:
        yield None
        return
    if type(script) is not PreLaunchScript:
        raise TypeError("pre_launch must be a PreLaunchScript or None.")
    temporary = tempfile.TemporaryDirectory(prefix="bioimageflow-pre-launch-")
    destination = Path(temporary.name) / "psij-pre-launch.sh"
    try:
        if script._kind == "text":
            assert script._content is not None
            _write_script(destination, script._content)
            size = len(script._content)
            digest = f"sha256:{hashlib.sha256(script._content).hexdigest()}"
            source_kind: Literal["uploaded", "cluster_file"] = "uploaded"
            source_path = None
            expected_digest = None
        else:
            assert script._path is not None
            size, digest = _copy_script(
                Path(script._path),
                destination,
                expected_digest=script._expected_digest,
                expected_size=script._expected_size,
            )
            source_kind = (
                "cluster_file" if script._kind == "cluster_file" else "uploaded"
            )
            source_path = str(script._path) if script._kind == "cluster_file" else None
            expected_digest = (
                script._expected_digest if script._kind == "cluster_file" else None
            )
        yield PreparedPreLaunch(
            source_kind=source_kind,
            source_path=source_path,
            expected_digest=expected_digest,
            path=destination,
            size=size,
            digest=digest,
        )
    finally:
        temporary.cleanup()


def install_prepared_pre_launch(
    prepared: PreparedPreLaunch | None,
    candidate: Path,
) -> dict[str, Any] | None:
    if prepared is None:
        return None
    destination = candidate / Path(*PurePosixPath(PRE_LAUNCH_RELATIVE_PATH).parts)
    size, digest = _copy_script(
        prepared.path,
        destination,
        expected_digest=prepared.digest,
        expected_size=prepared.size,
    )
    if size != prepared.size or digest != prepared.digest:
        raise ValueError("The installed pre-launch script changed during preparation.")
    return prepared.persisted()


def stage_bundle_pre_launch(
    script: PreLaunchScript | None,
    root: Path,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], ...]]:
    if script is None:
        return None, ()
    if type(script) is not PreLaunchScript:
        raise TypeError("pre_launch must be a PreLaunchScript or None.")
    if script._kind == "cluster_file":
        assert script._path is not None
        source_path = str(script._path)
        return (
            {
                "source_kind": "cluster_file",
                "source_path": source_path,
                "expected_digest": script._expected_digest,
                "expected_size": None,
            },
            (
                {
                    "kind": "cluster_pre_launch",
                    "path": source_path,
                    "expected_digest": script._expected_digest,
                },
            ),
        )
    with prepare_pre_launch(script) as prepared:
        assert prepared is not None
        destination = root / Path(*PurePosixPath(PRE_LAUNCH_RELATIVE_PATH).parts)
        size, digest = _copy_script(
            prepared.path,
            destination,
            expected_digest=prepared.digest,
            expected_size=prepared.size,
        )
    return (
        {
            "source_kind": "uploaded",
            "source_path": PRE_LAUNCH_RELATIVE_PATH,
            "expected_digest": digest,
            "expected_size": size,
        },
        (),
    )


def pre_launch_from_bundle_request(
    value: Any,
    object_root: Path,
) -> PreLaunchScript | None:
    if value is None:
        return None
    fields = {"source_kind", "source_path", "expected_digest", "expected_size"}
    if type(value) is not dict or set(value) != fields:
        raise ValueError("The pre-launch bundle descriptor is invalid.")
    kind = value["source_kind"]
    if kind == "cluster_file":
        if value["expected_size"] is not None:
            raise ValueError("Cluster pre-launch sources cannot declare a size.")
        return PreLaunchScript.from_cluster_file(
            value["source_path"],
            expected_digest=value["expected_digest"],
        )
    if kind != "uploaded":
        raise ValueError("The pre-launch source kind is invalid.")
    if value["source_path"] != PRE_LAUNCH_RELATIVE_PATH:
        raise ValueError("The uploaded pre-launch path is invalid.")
    relative = PurePosixPath(value["source_path"])
    return PreLaunchScript._from_uploaded_file(
        object_root / Path(*relative.parts),
        expected_digest=value["expected_digest"],
        expected_size=value["expected_size"],
    )


__all__ = ["PreLaunchScript"]
