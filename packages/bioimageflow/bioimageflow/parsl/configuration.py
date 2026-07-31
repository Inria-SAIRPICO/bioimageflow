"""Trusted ParslEngine construction from persistent config references."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from .types import ExecutorBinding


def engine_from_config_ref(
    engine_type: Any,
    reference: Any,
    *,
    executor_bindings: Mapping[str, ExecutorBinding],
    trusted_factories: Collection[str],
    engine_options: dict[str, Any],
) -> Any:
    from bioimageflow.integration import validate_parsl_config_ref
    from bioimageflow.launcher.configuration import build_parsl_config
    from bioimageflow.launcher.types import ParslConfigRef

    if type(reference) is not ParslConfigRef:
        raise TypeError("reference must be ParslConfigRef.")
    report = validate_parsl_config_ref(
        reference,
        executor_bindings=executor_bindings,
        trusted_factories=trusted_factories,
    )
    if not report.valid:
        raise ValueError(
            "Invalid Parsl configuration: "
            + "; ".join(item.message for item in report.diagnostics)
        )
    return engine_type(
        parsl_config=build_parsl_config(
            reference,
            trusted_factories=trusted_factories,
        ),
        executor_bindings=executor_bindings,
        **engine_options,
    )
