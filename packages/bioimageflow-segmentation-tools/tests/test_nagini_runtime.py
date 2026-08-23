"""Optional pinned NAGINI-3D API acceptance test."""

import inspect

import pytest


pytestmark = [pytest.mark.package_tools, pytest.mark.complete]


def test_real_nagini_runtime_exposes_adapter_api() -> None:
    model_module = pytest.importorskip("nagini3D.models.model")
    sampler_module = pytest.importorskip("nagini3D.models.tools.snake_sampler")

    constructor = inspect.signature(model_module.Nagini3D)
    inference = inspect.signature(model_module.Nagini3D.inference)
    curvature = inspect.signature(sampler_module.SnakeSmoothSampler)

    assert {"unet_cfg", "P", "M1", "M2", "device"} <= set(constructor.parameters)
    assert {"proba_th", "nb_tiles", "nms_th", "anisotropy", "grad_fn"} <= set(
        inference.parameters
    )
    assert {"P", "M1", "M2", "device"} <= set(curvature.parameters)
