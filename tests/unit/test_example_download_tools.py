"""Contract tests for self-contained example data downloaders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bioimageflow_core import Arguments, ExecutionContext
from example_workflows.fish_analysis.tools.download_images import (
    DownloadImages as FishDownloadImages,
)
from example_workflows.parameter_space_exploration.parameter_tools.download_images import (
    DownloadImages as ParametersDownloadImages,
)


class _Response:
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read(self) -> bytes:
        return b"image bytes"


def _context(tmp_path: Path) -> ExecutionContext:
    run_dir = tmp_path / "run"
    work_dir = run_dir / "work"
    rows_dir = work_dir / "rows"
    row_dir = rows_dir / "000000"
    return ExecutionContext(
        run_dir=run_dir,
        assets_dir=run_dir / "assets",
        work_dir=work_dir,
        rows_dir=rows_dir,
        row_dir=row_dir,
        row_index="000000",
    )


@pytest.mark.parametrize("tool_class", [FishDownloadImages, ParametersDownloadImages])
def test_downloaders_write_only_to_managed_assets(
    tool_class: type[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: _Response())
    context = _context(tmp_path)

    result = tool_class().process_row(
        Arguments(urls="https://example.test/sample.tif"),
        context=context,
    )

    assert len(result) == 1
    assert result[0].path == context.assets_dir / "sample.tif"
    assert result[0].path.read_bytes() == b"image bytes"


@pytest.mark.parametrize("tool_class", [FishDownloadImages, ParametersDownloadImages])
def test_downloaders_require_execution_context(tool_class: type[Any]) -> None:
    with pytest.raises(RuntimeError, match="execution context"):
        tool_class().process_row(Arguments(urls="https://example.test/sample.tif"))
