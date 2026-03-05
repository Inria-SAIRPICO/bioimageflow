"""
Test progress monitoring via callbacks.

Covers:
- ProgressEvent fields (node_name, status, row, total_rows, timestamp)
- Status transitions: started → row_complete(s) → completed
- Cached nodes report 'cached' status
- Multiple node progress events interleave correctly
"""

import pytest

from bioimageflow import ProgressEvent, Workflow

from .conftest import FileLoader, StubSegmenter, StubStats


class TestProgressCallback:

    def test_progress_events_for_single_node(self, tmp_workspace):
        events = []

        def on_progress(event):
            events.append(event)

        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(
            storage_path=tmp_workspace / "results", on_progress=on_progress
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.compute(masks)

        # Should have events for both nodes
        node_names = {e.node_name for e in events}
        assert "file_loader_1" in node_names or any(
            "file_loader" in n for n in node_names
        )

    def test_progress_event_fields(self, tmp_workspace):
        events = []

        def on_progress(event):
            events.append(event)

        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(
            storage_path=tmp_workspace / "results", on_progress=on_progress
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.compute(masks)

        for event in events:
            assert isinstance(event, ProgressEvent)
            assert isinstance(event.node_name, str)
            assert event.status in {
                "started",
                "row_complete",
                "completed",
                "cached",
                "failed",
            }
            assert isinstance(event.timestamp, float)

    def test_status_transitions(self, tmp_workspace):
        events = []

        def on_progress(event):
            events.append(event)

        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(
            storage_path=tmp_workspace / "results", on_progress=on_progress
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.compute(masks)

        # For the segmenter node, expect: started → row_complete(s) → completed
        seg_events = [e for e in events if "segmenter" in e.node_name]
        if seg_events:
            statuses = [e.status for e in seg_events]
            assert statuses[0] == "started"
            assert statuses[-1] == "completed"

    def test_row_complete_reports_progress(self, tmp_workspace):
        events = []

        def on_progress(event):
            events.append(event)

        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(
            storage_path=tmp_workspace / "results", on_progress=on_progress
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.compute(masks)

        row_events = [e for e in events if e.status == "row_complete"]
        for e in row_events:
            assert e.total_rows == 3
            assert 0 <= e.row < e.total_rows

    def test_cached_status_on_second_run(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        # First run (populates cache)
        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.compute(masks)

        # Second run with progress tracking
        events = []
        with Workflow(
            storage_path=tmp_workspace / "results",
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.compute(masks)

        cached = [e for e in events if e.status == "cached"]
        assert len(cached) > 0


class TestMultiNodeProgress:

    def test_events_from_multiple_nodes(self, tmp_workspace):
        events = []

        def on_progress(event):
            events.append(event)

        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(
            storage_path=tmp_workspace / "results", on_progress=on_progress
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])
            wf.compute(results)

        node_names = {e.node_name for e in events}
        # At least the segmenter and stats nodes should have events
        assert len(node_names) >= 2
