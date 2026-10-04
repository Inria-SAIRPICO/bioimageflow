"""Public exact-record admission preserves integrity and captured authority."""

from pathlib import Path
import json

import pandas as pd
import pytest

from bioimageflow.cache import dataframe_publish
from bioimageflow.cache.selection import selected_result
from bioimageflow.storage import CacheCorruptionError, Storage


def _publish(root: Path):
    frame = pd.DataFrame({"integer": pd.Series([2**60 + 1], dtype="int64"), "text": ["001"]})
    binding = dataframe_publish(root, "source", "signature", frame, run_id="run_" + "0" * 32,
                                engine="direct", tool_identity="test:source")
    return frame, binding


def test_exact_admission_reads_parquet_once_and_detaches_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected, published = _publish(tmp_path)
    storage = Storage(tmp_path)
    reads = []
    original = pd.read_parquet

    def read(path, *args, **kwargs):
        reads.append(Path(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", read)
    binding = selected_result(storage, published.result_key, published.record_id)
    assert reads == [published.record_dir / "dataframe.parquet"]
    pd.testing.assert_frame_equal(binding.dataframe, expected)
    assert binding.record_dir == published.record_dir
    mutation = binding.manifest
    mutation.row_relation["output_domain"] = "foreign"
    assert binding.manifest.row_relation["output_domain"] != "foreign"
    (storage.result_dir(binding.result_key) / "current.json").unlink()
    manifest, value, address = storage.load_record(binding.result_key, binding.record_id)
    assert manifest.record_id == binding.record_id and address == binding.record_dir
    pd.testing.assert_frame_equal(value, expected)


def test_exact_admission_refuses_corrupt_parquet(tmp_path: Path) -> None:
    _, binding = _publish(tmp_path)
    (binding.record_dir / "dataframe.parquet").write_bytes(b"corrupt")
    with pytest.raises(CacheCorruptionError, match="transport digest"):
        Storage(tmp_path).load_record(binding.result_key, binding.record_id)


@pytest.mark.parametrize("mutation", ["obsolete-schema", "missing-relation", "foreign-output-row", "malformed-collective"])
def test_exact_admission_refuses_unowned_relation(tmp_path: Path, mutation: str) -> None:
    _, binding = _publish(tmp_path)
    path = binding.record_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    if mutation == "obsolete-schema":
        manifest["schema"] = "bioimageflow.cache.record.v1"
    elif mutation == "missing-relation":
        del manifest["row_relation"]
    elif mutation == "foreign-output-row":
        manifest["row_relation"]["groups"][0]["output_indices"] = ["foreign"]
    else:
        manifest["row_relation"]["row_consumption"] = "collective"
        manifest["row_relation"]["groups"] = []
    path.write_text(json.dumps(manifest))
    with pytest.raises(CacheCorruptionError):
        Storage(tmp_path).load_record(binding.result_key, binding.record_id)
