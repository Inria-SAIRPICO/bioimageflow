"""Optional real LapTrack runtime acceptance test."""

from pathlib import Path

import pytest

from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import LapTrackLink


pytestmark = [pytest.mark.package_tools, pytest.mark.complete]


def test_real_laptrack_links_two_deterministic_tracks() -> None:
    pytest.importorskip("laptrack")
    source = Path("synthetic_labels.tif")
    rows = [
        Arguments(
            source_label_image=source,
            frame=frame,
            label=label,
            y=y,
            x=x,
            area=9,
            max_link_distance=3.0,
            gap_closing_distance=None,
            gap_closing_max_frames=2,
            division_distance=None,
        )
        for frame, label, y, x in [
            (0, 1, 2.0, 2.0),
            (0, 2, 10.0, 10.0),
            (1, 1, 2.0, 3.0),
            (1, 2, 10.0, 11.0),
        ]
    ]

    outputs = [group[0] for group in LapTrackLink().process_batch(rows)]

    assert [output.track_id for output in outputs] == [1, 2, 1, 2]
    assert {output.track_count for output in outputs} == {2}
    assert {output.generation for output in outputs} == {0}
    assert {output.parent_track_id for output in outputs} == {None}
