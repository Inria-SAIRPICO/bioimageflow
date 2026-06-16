"""Console logging configuration for BioImageFlow hosts."""

from __future__ import annotations

import logging
import sys

_BIOIMAGEFLOW_CONSOLE_HANDLER = "_bioimageflow_console_handler"


class _MaxLevelFilter(logging.Filter):
    """Allow records up to and including ``max_level``."""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def _create_split_stream_handlers(
    fmt: str,
    level: int,
) -> tuple[logging.StreamHandler, logging.StreamHandler]:
    formatter = logging.Formatter(fmt, datefmt="%H:%M:%S")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(_MaxLevelFilter(logging.INFO))
    stdout_handler.setFormatter(formatter)
    setattr(stdout_handler, _BIOIMAGEFLOW_CONSOLE_HANDLER, True)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(max(level, logging.WARNING))
    stderr_handler.setFormatter(formatter)
    setattr(stderr_handler, _BIOIMAGEFLOW_CONSOLE_HANDLER, True)

    return stdout_handler, stderr_handler


def _managed_console_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [
        handler
        for handler in logger.handlers
        if getattr(handler, _BIOIMAGEFLOW_CONSOLE_HANDLER, False)
    ]


def _set_handler_levels(
    handlers: tuple[logging.Handler, logging.Handler],
    level: int,
) -> None:
    stdout_handler, stderr_handler = handlers
    stdout_handler.setLevel(logging.DEBUG)
    stderr_handler.setLevel(max(level, logging.WARNING))


def configure_logging(
    level: int = logging.INFO,
    fmt: str = "%(asctime)s [%(name)s] %(message)s",
    wetlands_fmt: str | None = None,
    force: bool = False,
) -> tuple[logging.Handler, logging.Handler]:
    """Enable BioImageFlow and Wetlands split-stream console logging.

    BioImageFlow DEBUG/INFO records are routed to stdout, while WARNING and
    higher records are routed to stderr. Repeated calls reuse BioImageFlow-owned
    console handlers unless ``force=True`` is provided.
    """

    import wetlands.logger

    bioimageflow_logger = logging.getLogger("bioimageflow")
    managed_handlers = _managed_console_handlers(bioimageflow_logger)

    if force or len(managed_handlers) != 2:
        for handler in managed_handlers:
            bioimageflow_logger.removeHandler(handler)
            handler.close()
        handlers = _create_split_stream_handlers(fmt=fmt, level=level)
        for handler in handlers:
            bioimageflow_logger.addHandler(handler)
        bioimageflow_logger.propagate = False
    else:
        handlers = (
            managed_handlers[0],
            managed_handlers[1],
        )
        _set_handler_levels(handlers, level)

    bioimageflow_logger.setLevel(level)
    wetlands.logger.enable_console_logging(level=level, fmt=wetlands_fmt or fmt)
    return handlers
