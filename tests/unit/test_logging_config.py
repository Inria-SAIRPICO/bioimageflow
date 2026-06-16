from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from bioimageflow import Workflow, configure_logging
from bioimageflow.engine import DefaultEngine


@pytest.fixture(autouse=True)
def _restore_loggers() -> Iterator[None]:
    loggers = [logging.getLogger("bioimageflow"), logging.getLogger("wetlands")]
    saved = {
        logger.name: (logger.handlers[:], logger.level, logger.propagate)
        for logger in loggers
    }
    for logger in loggers:
        logger.handlers[:] = []
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
    yield
    for logger in loggers:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        handlers, level, propagate = saved[logger.name]
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _bioimageflow_managed_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger("bioimageflow").handlers
        if getattr(handler, "_bioimageflow_console_handler", False)
    ]


def test_configure_logging_routes_info_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(fmt="%(levelname)s:%(message)s")

    logging.getLogger("bioimageflow").info("ready")

    captured = capsys.readouterr()
    assert "INFO:ready" in captured.out
    assert captured.err == ""


def test_configure_logging_routes_warning_and_error_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(fmt="%(levelname)s:%(message)s")

    logger = logging.getLogger("bioimageflow")
    logger.warning("careful")
    logger.error("failed")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "WARNING:careful" in captured.err
    assert "ERROR:failed" in captured.err


def test_configure_logging_repeated_calls_do_not_duplicate_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(fmt="%(message)s")
    configure_logging(fmt="%(message)s")

    logging.getLogger("bioimageflow").info("once")

    captured = capsys.readouterr()
    assert captured.out.count("once") == 1
    assert len(_bioimageflow_managed_handlers()) == 2


def test_configure_logging_repeated_calls_update_handler_levels(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level=logging.ERROR, fmt="%(levelname)s:%(message)s")
    configure_logging(level=logging.DEBUG, fmt="%(levelname)s:%(message)s")

    logger = logging.getLogger("bioimageflow")
    logger.debug("details")
    logger.warning("careful")

    captured = capsys.readouterr()
    assert "DEBUG:details" in captured.out
    assert "WARNING:careful" in captured.err


def test_configure_logging_force_replaces_managed_handlers() -> None:
    first = configure_logging(fmt="%(message)s")

    second = configure_logging(fmt="%(message)s", force=True)

    assert first != second
    assert all(handler not in logging.getLogger("bioimageflow").handlers for handler in first)
    assert list(second) == _bioimageflow_managed_handlers()


def test_configure_logging_force_preserves_unrelated_user_handlers() -> None:
    logger = logging.getLogger("bioimageflow")
    user_handler = logging.NullHandler()
    logger.addHandler(user_handler)
    old_managed = logging.NullHandler()
    old_managed._bioimageflow_console_handler = True  # type: ignore[attr-defined]
    logger.addHandler(old_managed)

    configure_logging(force=True)

    assert user_handler in logger.handlers
    assert old_managed not in logger.handlers
    assert len(_bioimageflow_managed_handlers()) == 2


def test_configure_logging_delegates_wetlands_console_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, str]] = []

    def fake_enable_console_logging(*, level: int, fmt: str) -> None:
        calls.append((level, fmt))

    monkeypatch.setattr(
        "wetlands.logger.enable_console_logging",
        fake_enable_console_logging,
    )

    configure_logging(level=logging.DEBUG, fmt="bio:%(message)s", wetlands_fmt="wet:%(message)s")

    assert calls == [(logging.DEBUG, "wet:%(message)s")]


def test_default_engine_and_workflow_construction_do_not_add_console_handlers() -> None:
    DefaultEngine(use_wetlands=False)
    Workflow()

    assert logging.getLogger("bioimageflow").handlers == []
    assert logging.getLogger("wetlands").handlers == []
