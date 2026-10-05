"""Exclusive publication preserves a concurrently acquired public return."""

import pandas as pd

RUN_ID = "run_1234567812344abc923456789abcdef0"


def test_launcher_return_preserves_late_empty_winner(tmp_path, monkeypatch):
    from bioimageflow.launcher import returns

    control = tmp_path / 'control'
    control.mkdir()
    target = control / 'return'
    original = returns._validate_return_tree
    winner = {}

    def validate(candidate, manifest, **kwargs):
        original(candidate, manifest, **kwargs)
        target.mkdir()
        winner['inode'] = target.stat().st_ino

    monkeypatch.setattr(returns, '_validate_return_tree', validate)
    error = None
    try:
        returns.persist_public_return(control, tmp_path / 'store', RUN_ID,
                                      pd.DataFrame({'value': [4]}), outcomes=())
    except (FileExistsError, returns.LauncherProtocolError, returns.WorkflowRunResultUnavailableError) as exc:
        error = exc
    observed = {'error': str(error), 'inode': target.stat().st_ino,
                'members': sorted(p.name for p in target.iterdir())}
    assert observed['inode'] == winner['inode'], observed
    assert observed['members'] == [], observed
    assert error is not None, observed

    assert not list(control.glob('.return.*.tmp'))

