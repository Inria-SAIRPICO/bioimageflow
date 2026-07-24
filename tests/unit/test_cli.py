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

    def fake_export(storage_path, *, mode, scope, run_id):
        calls.append((storage_path, mode, scope, run_id))
        return [exported]

    monkeypatch.setattr(cli, "export_outputs", fake_export)

    assert cli.main(["export-outputs", str(tmp_path)]) == 0
    assert calls == [(tmp_path, "copy", "latest", None)]
    assert capsys.readouterr().out == f"{exported}\n"
