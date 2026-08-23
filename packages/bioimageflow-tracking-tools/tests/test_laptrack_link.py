"""Fast LapTrack adapter contract tests."""

from pathlib import Path
import sys
import types

import pandas as pd
import pytest

from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import LapTrackLink


pytestmark = pytest.mark.package_tools


def test_laptrack_normalizes_division_lineage_and_restores_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor: dict[str, object] = {}

    class FakeLapTrack:
        def __init__(self, **kwargs: object) -> None:
            constructor.update(kwargs)

        def predict_dataframe(self, frame: pd.DataFrame, **_: object):
            tracked = frame.copy()
            by_label = {1: 0, 2: 1, 3: 2}
            tracked["track_id"] = [by_label[int(label)] for label in tracked["label"]]
            tracked["tree_id"] = 0
            splits = pd.DataFrame(
                [
                    {"parent_track_id": 0, "child_track_id": 1},
                    {"parent_track_id": 0, "child_track_id": 2},
                ]
            )
            return tracked, splits, pd.DataFrame()

    module = types.ModuleType("laptrack")
    module.LapTrack = FakeLapTrack  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "laptrack", module)
    source = Path("labels.tif")
    rows = [
        Arguments(
            source_label_image=source,
            frame=1,
            label=3,
            y=4.0,
            x=6.0,
            area=8,
            max_link_distance=10.0,
            gap_closing_distance=20.0,
            gap_closing_max_frames=2,
            division_distance=15.0,
        ),
        Arguments(
            source_label_image=source,
            frame=0,
            label=1,
            y=3.0,
            x=5.0,
            area=10,
            max_link_distance=10.0,
            gap_closing_distance=20.0,
            gap_closing_max_frames=2,
            division_distance=15.0,
        ),
        Arguments(
            source_label_image=source,
            frame=1,
            label=2,
            y=2.0,
            x=4.0,
            area=9,
            max_link_distance=10.0,
            gap_closing_distance=20.0,
            gap_closing_max_frames=2,
            division_distance=15.0,
        ),
    ]

    nested = LapTrackLink().process_batch(rows)
    outputs = [group[0] for group in nested]

    assert [output.label for output in outputs] == [3, 1, 2]
    assert [output.track_id for output in outputs] == [3, 1, 2]
    assert [output.parent_track_id for output in outputs] == [1, None, 1]
    assert [output.generation for output in outputs] == [1, 0, 1]
    assert {output.lineage_id for output in outputs} == {1}
    assert {output.division_count for output in outputs} == {1}
    assert constructor["cutoff"] == 100.0
    assert constructor["gap_closing_cutoff"] == 400.0
    assert constructor["splitting_cutoff"] == 225.0
    assert constructor["merging_cutoff"] is False


def test_laptrack_rejects_duplicate_object_identity() -> None:
    row = Arguments(
        source_label_image=Path("labels.tif"),
        frame=0,
        label=1,
        y=1.0,
        x=2.0,
        area=3,
        max_link_distance=10.0,
        gap_closing_distance=None,
        gap_closing_max_frames=2,
        division_distance=None,
    )
    with pytest.raises(ValueError, match="unique"):
        LapTrackLink().process_batch([row, row])
