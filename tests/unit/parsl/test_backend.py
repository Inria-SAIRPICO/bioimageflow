"""Parsl backend adapter concurrency tests."""

from __future__ import annotations

from threading import Event, Thread

from bioimageflow.parsl.backend import ParslBackend
from bioimageflow.parsl.routing import RoutingPlan
from bioimageflow.parsl.types import ParslTaskPolicy


def test_concurrent_app_lookup_creates_one_app_per_executor_label() -> None:
    factory_entered = Event()
    release_factory = Event()
    app = object()
    created: list[str] = []
    results: list[object] = []

    def factory(**kwargs):
        created.append(kwargs["executors"][0])
        factory_entered.set()
        assert release_factory.wait(timeout=5)
        return app

    backend = ParslBackend(
        owner=object(),
        dfk=object(),
        routing=RoutingPlan(routes=()),
        task_policy=ParslTaskPolicy(),
        sequential=False,
        app_factory=factory,
    )

    first = Thread(target=lambda: results.append(backend._app("cpu")))
    second = Thread(target=lambda: results.append(backend._app("cpu")))
    first.start()
    assert factory_entered.wait(timeout=5)
    second.start()
    release_factory.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert created == ["cpu"]
    assert results == [app, app]
