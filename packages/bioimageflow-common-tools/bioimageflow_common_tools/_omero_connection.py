"""Transient, local-keyring-backed OMERO sessions; no platform imports."""

from contextlib import contextmanager
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterator

from bioimageflow_core import ExecutionContext


KEYRING_SERVICE = "bioimageflow-omero"


class OmeroToolError(RuntimeError):
    """A fixed, non-secret transfer or validation diagnostic."""


def positive_id(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise OmeroToolError(f"{label} must be a positive integer.")
    return value


def connection_fields(arguments: Any) -> tuple[str, int, str]:
    host, port, username = arguments.host, arguments.port, arguments.username
    if not isinstance(host, str) or not host.strip():
        raise OmeroToolError("OMERO host is required.")
    if not isinstance(username, str) or not username.strip():
        raise OmeroToolError("OMERO username is required.")
    if type(port) is not int or not 1 <= port <= 65535:
        raise OmeroToolError("OMERO port must be between 1 and 65535.")
    return host.strip(), port, username.strip()


def runtime_scratch(context: ExecutionContext | None) -> Path:
    if not isinstance(context, ExecutionContext):
        raise OmeroToolError("OMERO transfer requires an execution context.")
    scratch = context.row_dir or context.batch_dir
    assert scratch is not None
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


class _QuietSDK(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # OMERO.py logs live session UUIDs, including at INFO. Its exception
        # strings are also outside this package's non-secret error contract.
        return not record.name.startswith(("omero", "keyring", "Ice"))


@contextmanager
def omero_session(arguments: Any, context: ExecutionContext | None) -> Iterator[Any]:
    host, port, username = connection_fields(arguments)
    scratch = runtime_scratch(context)
    quiet = _QuietSDK()
    sdk_loggers = [logging.getLogger(name) for name in ("omero", "omero.gateway", "omero.clients", "keyring")]
    handlers = {handler for logger in [logging.getLogger(), *sdk_loggers] for handler in logger.handlers}
    if logging.lastResort is not None:
        handlers.add(logging.lastResort)
    for target in [*sdk_loggers, *handlers]:
        target.addFilter(quiet)
    connection = None
    primary = False
    try:
        try:
            import keyring  # pyright: ignore[reportMissingImports]
            from omero.gateway import BlitzGateway  # pyright: ignore[reportMissingImports]

            password = keyring.get_password(KEYRING_SERVICE, f"{host}:{port}:{username}")
            if password is None:
                raise OmeroToolError("No OMERO password is stored for this connection.")
            # Configuration contains no credential and exists only in owned
            # runtime scratch. These are supported Ice 3.6 transport bounds.
            with NamedTemporaryFile(mode="w", suffix=".ice.config", dir=scratch) as config:
                config.write("Ice.Override.ConnectTimeout=30000\nIce.Override.Timeout=60000\n")
                config.flush()
                try:
                    connection = BlitzGateway(
                        username, password, host=host, port=port, secure=True,
                        extra_config=[config.name], useragent="BioImageFlow OMERO tools",
                    )
                    if not connection.connect():
                        raise OmeroToolError("OMERO connection failed; verify the server and stored credential.")
                    yield connection
                except BaseException:
                    primary = True
                    raise
                finally:
                    if connection is not None:
                        try:
                            connection.close()
                        except Exception:
                            if not primary:
                                raise OmeroToolError("OMERO session cleanup failed.") from None
                            logging.getLogger(__name__).warning("OMERO session cleanup also failed.")
        except OmeroToolError:
            raise
        except Exception:
            raise OmeroToolError("OMERO transfer failed; verify access and server availability.") from None
    finally:
        for target in [*sdk_loggers, *handlers]:
            target.removeFilter(quiet)
