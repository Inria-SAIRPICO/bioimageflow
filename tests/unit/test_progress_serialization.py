"""Progress callback serialization under concurrent engine emission."""

from __future__ import annotations

from threading import Event, Lock, Thread
from types import SimpleNamespace

from bioimageflow import DefaultEngine


def test_progress_callback_invocations_are_serialized() -> None:
    first_entered = Event()
    release_first = Event()
    second_started = Event()
    second_entered = Event()
    state_lock = Lock()
    callback_count = 0
    active_callbacks = 0
    maximum_active = 0

    def callback(_event: object) -> None:
        nonlocal callback_count, active_callbacks, maximum_active
        with state_lock:
            callback_count += 1
            ordinal = callback_count
            active_callbacks += 1
            maximum_active = max(maximum_active, active_callbacks)
        if ordinal == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        with state_lock:
            active_callbacks -= 1

    engine = DefaultEngine(use_wetlands=False)
    workflow = SimpleNamespace(on_progress=callback)
    first = Thread(
        target=engine._emit_progress,
        args=(workflow, "first", "started"),
    )

    def emit_second() -> None:
        second_started.set()
        engine._emit_progress(workflow, "second", "started")

    second = Thread(target=emit_second)
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    assert second_started.wait(timeout=2)
    assert not second_entered.wait(timeout=0.1)

    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()
    assert maximum_active == 1
