"""Immutable setup-script value used by managed cluster configuration."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal

from ._common import DIGEST_RE, exact_dict, normalized_cluster_path


def _path(value: object, *, field_name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"{field_name} must be path-like.")
    return Path(value).expanduser().absolute()


@dataclass(frozen=True, slots=True, repr=False)
class SetupScript:
    """One exact site setup script, snapshotted before remote contact."""

    SCHEMA: ClassVar[str] = "bioimageflow.setup_script.v1"
    MAX_BYTES: ClassVar[int] = 64 * 1024

    source_kind: Literal["text", "local_file", "cluster_file"]
    size: int | None
    digest: str
    cluster_path: PurePosixPath | None = None
    _content: bytes | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.source_kind not in {"text", "local_file", "cluster_file"}:
            raise ValueError("Unknown setup-script source kind.")
        if type(self.digest) is not str or DIGEST_RE.fullmatch(self.digest) is None:
            raise ValueError("Setup script digest must be a lowercase SHA-256 digest.")
        if self.source_kind == "cluster_file":
            if self._content is not None or self.cluster_path is None:
                raise ValueError(
                    "A cluster setup script requires only its pinned path."
                )
            if self.size is not None and (
                type(self.size) is not int or not 0 < self.size <= self.MAX_BYTES
            ):
                raise ValueError(
                    "A verified cluster setup script size must be in [1, 65536] bytes."
                )
            normalized_cluster_path(self.cluster_path, field="cluster_path")
        else:
            if type(self.size) is not int or not 0 < self.size <= self.MAX_BYTES:
                raise ValueError("Setup script size must be in [1, 65536] bytes.")
            if self.cluster_path is not None:
                raise ValueError("An uploaded setup script cannot name a cluster path.")
            if self._content is not None:
                self._validate_bytes(self._content)
                if (
                    len(self._content) != self.size
                    or self._digest(self._content) != self.digest
                ):
                    raise ValueError("Setup script bytes do not match their manifest.")

    def __repr__(self) -> str:
        return (
            f"SetupScript(source_kind={self.source_kind!r}, size={self.size}, "
            f"digest={self.digest!r})"
        )

    @staticmethod
    def _digest(content: bytes) -> str:
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    @classmethod
    def _validate_bytes(cls, content: bytes) -> None:
        if not content or len(content) > cls.MAX_BYTES:
            raise ValueError("Setup script must contain between 1 byte and 64 KiB.")
        if b"\x00" in content:
            raise ValueError("Setup script must not contain NUL bytes.")
        try:
            content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("Setup script must be valid UTF-8.") from exc

    @classmethod
    def from_text(cls, text: str) -> "SetupScript":
        if type(text) is not str:
            raise TypeError("SetupScript.from_text() requires a string.")
        content = text.encode("utf-8", errors="strict")
        cls._validate_bytes(content)
        return cls("text", len(content), cls._digest(content), _content=content)

    @classmethod
    def from_file(cls, path: str | Path) -> "SetupScript":
        source = _path(path, field_name="path")
        before = source.stat(follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ValueError(
                "SetupScript.from_file() requires a regular non-symlink file."
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        try:
            opened = os.fstat(descriptor)
            content = bytearray()
            while len(content) <= cls.MAX_BYTES:
                chunk = os.read(
                    descriptor, min(64 * 1024, cls.MAX_BYTES + 1 - len(content))
                )
                if not chunk:
                    break
                content.extend(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        final = source.stat(follow_symlinks=False)
        identities = {
            (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_size,
                value.st_mtime_ns,
            )
            for value in (before, opened, after, final)
        }
        if len(identities) != 1:
            raise ValueError("Setup script changed while it was snapshotted.")
        encoded = bytes(content)
        cls._validate_bytes(encoded)
        return cls("local_file", len(encoded), cls._digest(encoded), _content=encoded)

    @classmethod
    def from_cluster_file(
        cls,
        path: str | PurePosixPath,
        *,
        sha256: str,
    ) -> "SetupScript":
        normalized = normalized_cluster_path(path, field="path")
        if type(sha256) is not str or DIGEST_RE.fullmatch(sha256) is None:
            raise ValueError("sha256 must be a lowercase 'sha256:<hex>' digest.")
        # Exact bytes and therefore size are learned during remote verification.
        return cls("cluster_file", None, sha256, cluster_path=normalized)

    @property
    def content(self) -> bytes:
        if self._content is None:
            raise RuntimeError("local-state-unavailable")
        return self._content

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "source_kind": self.source_kind,
            "size": self.size,
            "digest": self.digest,
            "cluster_path": None
            if self.cluster_path is None
            else str(self.cluster_path),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SetupScript":
        data = exact_dict(
            value,
            {"schema", "source_kind", "size", "digest", "cluster_path"},
            cls.__name__,
        )
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported SetupScript schema.")
        return cls(
            source_kind=data["source_kind"],
            size=data["size"],
            digest=data["digest"],
            cluster_path=(
                None
                if data["cluster_path"] is None
                else PurePosixPath(data["cluster_path"])
            ),
        )


__all__ = ["SetupScript"]
