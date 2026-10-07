"""Atlas wrapper keeps implicit CLI outputs out of the process cwd."""

import threading
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bioimageflow_spot_tools.atlas as atlas_module
from bioimageflow_spot_tools import AtlasSpotDetection
from bioimageflow_core import Arguments, ExecutionContext
from bioimageflow_core import external

import pytest

pytestmark = pytest.mark.package_tools


def test_atlas_reference_file_is_packaged():
    blobs_file = Path(atlas_module.__file__).parent / "data" / "blobs.txt"

    assert blobs_file.is_file()
    assert blobs_file.stat().st_size > 0


def _execution_context(run_dir: Path, row_name: str = "000000") -> ExecutionContext:
    return ExecutionContext(
        run_dir=run_dir,
        assets_dir=run_dir / "assets",
        work_dir=run_dir / "work",
        rows_dir=run_dir / "work" / "rows",
        row_dir=run_dir / "work" / "rows" / row_name,
        batch_dir=None,
        row_index=row_name,
    )


def test_atlas_runs_external_command_in_execution_row_dir(tmp_path, monkeypatch):
    calls = []

    def fake_run_staged(command, **kwargs):
        calls.append((command, kwargs))
        Path(kwargs["output_path"]).write_text("detections")

    monkeypatch.setattr(
        "bioimageflow_spot_tools.atlas.run_external_command_with_staged_output",
        fake_run_staged,
    )

    output_path = tmp_path / "assets" / "detections.tif"
    context = _execution_context(tmp_path)

    result = AtlasSpotDetection().process_row(
        Arguments(
            input_image=tmp_path / "input.tif",
            output_image=output_path,
            gaussian_std=None,
            p_value=None,
            area_lim=None,
            verbose=False,
        ),
        context=context,
    )

    assert Path(result.output_image) == output_path
    assert output_path.read_text() == "detections"
    assert calls
    assert calls[-1][0][0] == "atlas"
    assert calls[-1][1]["cwd"] == context.row_dir
    assert calls[-1][1]["output_path"] == output_path
    assert not (Path.cwd() / "LoG.tif").exists()


def test_atlas_blobsref_fallback_uses_shared_work_atlas_path(tmp_path, monkeypatch):
    calls = []

    def fake_run_staged(command, **kwargs):
        calls.append((command, kwargs))
        Path(kwargs["output_path"]).write_text(
            "reference" if command[0] == "blobsref" else "detections"
        )

    fake_package_file = tmp_path / "missing_package_data" / "atlas.py"
    fake_package_file.parent.mkdir()
    fake_package_file.write_text("")
    monkeypatch.setattr(atlas_module, "__file__", str(fake_package_file))
    monkeypatch.setattr(
        "bioimageflow_spot_tools.atlas.run_external_command_with_staged_output",
        fake_run_staged,
    )
    monkeypatch.chdir(tmp_path)

    relative_root = Path("relative_run")
    context = _execution_context(relative_root)

    AtlasSpotDetection().process_row(
        Arguments(
            input_image=tmp_path / "input.tif",
            output_image=tmp_path / "assets" / "detections.tif",
            gaussian_std=None,
            p_value=None,
            area_lim=None,
            verbose=False,
        ),
        context=context,
    )

    blobsref_call = calls[0]
    assert blobsref_call[0][0] == "blobsref"
    expected_blobs_path = (context.work_dir / "atlas" / "blobs.txt").resolve()
    assert Path(blobsref_call[0][2]) == expected_blobs_path
    assert blobsref_call[1]["output_path"] == expected_blobs_path
    assert Path(blobsref_call[1]["cwd"]) == expected_blobs_path.parent

    atlas_call = calls[1]
    assert atlas_call[0][0] == "atlas"
    assert atlas_call[0][2] == str(expected_blobs_path)
    assert atlas_call[1]["cwd"] == context.row_dir
    assert atlas_call[1]["output_path"] == tmp_path / "assets" / "detections.tif"


def test_atlas_blobsref_fallback_is_generated_once_for_parallel_rows(
    tmp_path, monkeypatch
):
    calls = []
    calls_lock = threading.Lock()

    def fake_run_staged(command, **kwargs):
        with calls_lock:
            calls.append((command, kwargs))
        if command[0] == "blobsref":
            time.sleep(0.05)
        Path(kwargs["output_path"]).write_text(
            "reference" if command[0] == "blobsref" else "detections"
        )

    fake_package_file = tmp_path / "missing_package_data" / "atlas.py"
    fake_package_file.parent.mkdir()
    fake_package_file.write_text("")
    monkeypatch.setattr(atlas_module, "__file__", str(fake_package_file))
    monkeypatch.setattr(
        "bioimageflow_spot_tools.atlas.run_external_command_with_staged_output",
        fake_run_staged,
    )

    def run_row(i: int) -> None:
        AtlasSpotDetection().process_row(
            Arguments(
                input_image=tmp_path / f"input_{i}.tif",
                output_image=tmp_path / "assets" / f"detections_{i}.tif",
                gaussian_std=None,
                p_value=None,
                area_lim=None,
                verbose=False,
            ),
            context=_execution_context(tmp_path, f"{i:06d}"),
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(run_row, range(4)))

    blobsref_calls = [call for call in calls if call[0][0] == "blobsref"]
    atlas_calls = [call for call in calls if call[0][0] == "atlas"]

    expected_blobs_path = tmp_path / "work" / "atlas" / "blobs.txt"
    assert len(blobsref_calls) == 1
    assert expected_blobs_path.read_text() == "reference"
    assert not expected_blobs_path.with_suffix(".txt.tmp").exists()
    assert not (expected_blobs_path.parent / ".blobsref.lock").exists()
    assert {call[0][2] for call in atlas_calls} == {str(expected_blobs_path)}
    assert {call[1]["cwd"] for call in atlas_calls} == {
        tmp_path / "work" / "rows" / f"{i:06d}" for i in range(4)
    }


@pytest.mark.parametrize("cleanup_failure", ["none", "ordinary", "readonly"])
def test_atlas_preparation_failure_cleans_owned_directory_without_masking_primary(
    tmp_path,
    monkeypatch,
    caplog,
    cleanup_failure,
):
    class ReadonlyError(RuntimeError):
        def __setattr__(self, _name, _value):
            raise AttributeError("read-only exception")

    primary = (
        ReadonlyError("preparation failed")
        if cleanup_failure == "readonly"
        else RuntimeError("preparation failed")
    )
    held = []
    real_temporary = atlas_module.tempfile.TemporaryDirectory
    real_mkdir = Path.mkdir
    real_cleanup = real_temporary.cleanup
    calls = []
    sentinel = tmp_path / "independent.txt"
    sentinel.write_text("unrelated")
    sentinel_inode = sentinel.stat().st_ino

    def temporary(*_args, **_kwargs):
        owner = real_temporary(prefix="atlas-owned-", dir=tmp_path)
        held.append(owner)
        return owner

    def mkdir(path, *args, **kwargs):
        if held and path == Path(held[0].name) / "work":
            raise primary
        return real_mkdir(path, *args, **kwargs)

    def cleanup(owner):
        if cleanup_failure != "none":
            raise OSError("contingent cleanup failure")
        return real_cleanup(owner)

    monkeypatch.setattr(atlas_module.tempfile, "TemporaryDirectory", temporary)
    monkeypatch.setattr(real_temporary, "cleanup", cleanup)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(
        atlas_module,
        "run_external_command_with_staged_output",
        lambda *args, **kwargs: calls.append(args),
    )
    with pytest.raises(RuntimeError) as error:
        AtlasSpotDetection().process_row(
            Arguments(
                input_image=tmp_path / "input.tif",
                output_image=tmp_path / "output.tif",
                gaussian_std=None,
                p_value=None,
                area_lim=None,
                verbose=False,
            )
        )
    assert error.value is primary
    assert calls == []
    assert not (tmp_path / "output.tif").exists()
    assert (
        sentinel.read_text() == "unrelated" and sentinel.stat().st_ino == sentinel_inode
    )
    owner = held[0]
    if cleanup_failure == "none":
        assert not Path(owner.name).exists()
    else:
        assert Path(owner.name).exists()
        assert owner.name in caplog.text
        if cleanup_failure == "ordinary":
            assert primary._atlas_cleanup_owner is owner
            assert primary.atlas_cleanup["pending_path"] == owner.name
        monkeypatch.setattr(real_temporary, "cleanup", real_cleanup)
        owner.cleanup()
        assert not Path(owner.name).exists()


def _fallback_call(tmp_path, monkeypatch):
    package = tmp_path / "missing-package" / "atlas.py"
    package.parent.mkdir()
    package.write_text("")
    monkeypatch.setattr(atlas_module, "__file__", str(package))
    return Arguments(
        input_image=tmp_path / "input.tif",
        output_image=tmp_path / "output.tif",
        gaussian_std=None,
        p_value=None,
        area_lim=None,
        verbose=False,
    ), _execution_context(tmp_path)


@pytest.mark.parametrize("late_owner", [False, True])
def test_atlas_reference_publication_preserves_foreign_files(
    monkeypatch, tmp_path, late_owner
):
    arguments, context = _fallback_call(tmp_path, monkeypatch)
    directory = context.work_dir / "atlas"
    directory.mkdir(parents=True)
    predictable = directory / "blobs.txt.tmp"
    predictable.write_text("foreign temporary")
    original_inode = predictable.stat().st_ino
    reference = directory / "blobs.txt"
    winner_inode = []
    calls = []

    def child(command, _kwargs):
        calls.append(command[0])
        Path(command[command.index("-o") + 1]).write_text("candidate")
        if command[0] == "blobsref" and late_owner:
            reference.write_text("late reference winner")
            winner_inode.append(reference.stat().st_ino)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(external, "_run_subprocess", child)
    if late_owner:
        with pytest.raises(FileExistsError):
            AtlasSpotDetection().process_row(arguments, context=context)
        assert reference.read_text() == "late reference winner"
        assert reference.stat().st_ino == winner_inode[0]
        assert calls == ["blobsref"]
        assert not arguments.output_image.exists()
    else:
        output = AtlasSpotDetection().process_row(arguments, context=context)
        assert reference.read_text() == "candidate"
        assert Path(output.output_image).read_text() == "candidate"
        assert calls == ["blobsref", "atlas"]
    assert predictable.read_text() == "foreign temporary"
    assert predictable.stat().st_ino == original_inode
    assert sorted(path.name for path in directory.iterdir()) == [
        "blobs.txt",
        "blobs.txt.tmp",
    ]


@pytest.mark.parametrize(
    "published,removed,substituted",
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
        (False, False, True),
    ],
)
def test_atlas_reference_lock_cleanup_preserves_primary_and_reports_publication(
    monkeypatch, tmp_path, published, removed, substituted
):
    arguments, context = _fallback_call(tmp_path, monkeypatch)
    lock = context.work_dir / "atlas" / ".blobsref.lock"
    reference = lock.parent / "blobs.txt"
    primary = RuntimeError("child failed")
    cleanup_error = OSError("contingent lock cleanup failed")
    original_rmdir = Path.rmdir
    displaced_lock = lock.parent / ".original-blobsref.lock"
    replacement_inode = []

    def child(command, _kwargs):
        if substituted:
            lock.rename(displaced_lock)
            lock.mkdir()
            (lock / "sentinel.txt").write_text("independent lock")
            replacement_inode.append(lock.stat().st_ino)
        if not published:
            raise primary
        Path(command[command.index("-o") + 1]).write_text("accepted reference")
        return subprocess.CompletedProcess(command, 0)

    def rmdir(path):
        if path == lock:
            if removed:
                original_rmdir(path)
            raise cleanup_error
        return original_rmdir(path)

    monkeypatch.setattr(external, "_run_subprocess", child)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    with pytest.raises((RuntimeError, OSError)) as error:
        AtlasSpotDetection().process_row(arguments, context=context)
    expected = cleanup_error if published else primary
    assert error.value is expected
    facts = expected.atlas_reference_cleanup
    assert facts["pending_lock"] == (None if removed else str(lock))
    assert facts["published_reference"] == (str(reference) if published else None)
    assert lock.is_dir() == (not removed)
    assert not arguments.output_image.exists()
    if published:
        assert reference.read_text() == "accepted reference"
    else:
        assert not reference.exists()
    monkeypatch.setattr(Path, "rmdir", original_rmdir)
    if removed:
        assert not hasattr(expected, "_atlas_reference_cleanup_owner")
    elif substituted:
        assert displaced_lock.is_dir()
        assert lock.stat().st_ino == replacement_inode[0]
        assert (lock / "sentinel.txt").read_text() == "independent lock"
        with pytest.raises(RuntimeError, match="identity"):
            expected._atlas_reference_cleanup_owner.close()
        assert lock.stat().st_ino == replacement_inode[0]
        assert (lock / "sentinel.txt").read_text() == "independent lock"
        original_rmdir(displaced_lock)
        return
    else:
        expected._atlas_reference_cleanup_owner.close()
    assert not lock.exists()
