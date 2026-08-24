"""Required runtime-closure validation for locked uv deployments."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from typing import Any

from ._deployment_snapshot import _environment_failure


def _required_uv_runtime_identity(
    by_name: Mapping[str, list[Mapping[str, Any]]],
    reachable_names: set[str],
    *,
    scheduler: str,
) -> dict[str, str]:
    """Require one immutable selected candidate for every managed runtime package."""
    required = ("bioimageflow-core", "parsl", "psij-python")
    versions: dict[str, str] = {}
    for name in required:
        candidates = by_name.get(name, []) if name in reachable_names else []
        if len(candidates) != 1:
            raise _environment_failure(
                "environment-build-lock-incomplete",
                f"The selected uv closure requires exactly one {name!r} distribution.",
            )
        candidate = candidates[0]
        source = candidate.get("source")
        version = candidate.get("version")
        if (
            type(version) is not str
            or not isinstance(source, Mapping)
            or len(source) != 1
            or next(iter(source)) not in {"registry", "editable", "directory"}
        ):
            raise _environment_failure(
                "environment-build-lock-incomplete",
                f"The selected {name!r} distribution has no immutable wheel build path.",
            )
        versions[name] = version
    try:
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
        from packaging.version import Version

        requirements = importlib.metadata.requires("bioimageflow") or []
        compatibility = {
            canonicalize_name(requirement.name): requirement.specifier
            for value in requirements
            for requirement in (Requirement(value),)
            if canonicalize_name(requirement.name) in required
        }
        incompatible = [
            name
            for name, version in versions.items()
            if canonicalize_name(name) in compatibility
            and Version(version) not in compatibility[canonicalize_name(name)]
        ]
    except (importlib.metadata.PackageNotFoundError, ValueError) as exc:
        raise _environment_failure(
            "bioimageflow-version-conflict",
            "The running BioImageFlow runtime requirements cannot be verified.",
        ) from exc
    if incompatible:
        raise _environment_failure(
            "bioimageflow-version-conflict",
            "The selected uv runtime is incompatible with the running BioImageFlow version.",
        )
    return {
        "scheduler": scheduler,
        "distribution": "psij-python",
        "version": versions["psij-python"],
    }


__all__ = ["_required_uv_runtime_identity"]
