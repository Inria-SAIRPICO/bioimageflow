"""Command-line output export tests."""

from pathlib import Path

from bioimageflow import cli


def test_export_outputs_command_defaults_to_latest_copy(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    exported = tmp_path / "outputs" / "latest" / "node" / "dataframe.json"
    calls = []

    def fake_export(storage_path, *, destination, replace, mode, scope, run_id):
        calls.append((storage_path, destination, replace, mode, scope, run_id))
        return [exported]

    monkeypatch.setattr(cli, "export_outputs", fake_export)

    assert cli.main(["export-outputs", str(tmp_path)]) == 0
    assert calls == [(tmp_path, None, False, "copy", "latest", None)]
    assert capsys.readouterr().out == f"{exported}\n"


def test_export_outputs_command_forwards_destination_and_replacement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "shared-results"
    calls = []

    def fake_export(storage_path, *, destination, replace, mode, scope, run_id):
        calls.append((storage_path, destination, replace, mode, scope, run_id))
        return []

    monkeypatch.setattr(cli, "export_outputs", fake_export)

    assert (
        cli.main(
            [
                "export-outputs",
                str(tmp_path),
                "--destination",
                str(destination),
                "--replace",
                "--mode",
                "hardlink",
                "--scope",
                "both",
                "--run-id",
                "run_selected",
            ]
        )
        == 0
    )
    assert calls == [(tmp_path, destination, True, "hardlink", "both", "run_selected")]
