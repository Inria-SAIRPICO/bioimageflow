"""Credential isolation, cleanup and restored logging without a network."""

import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import traceback

import pytest

from bioimageflow_core import ExecutionContext
from bioimageflow_omero_tools.connection import KEYRING_SERVICE, OmeroToolError, omero_session


pytestmark = pytest.mark.package_tools


@pytest.fixture
def context(tmp_path):
    return ExecutionContext(
        run_dir=tmp_path, assets_dir=tmp_path / "assets", work_dir=tmp_path / "work",
        rows_dir=tmp_path / "work/rows", row_dir=tmp_path / "work/rows/0", row_index="0",
    )


@pytest.fixture
def fake_sdk(monkeypatch):
    state = SimpleNamespace(lookups=[], connections=[], password="test-only-secret", fail_connect=False, fail_close=False)
    keyring = ModuleType("keyring")
    def password(service, key):
        state.lookups.append((service, key))
        return state.password
    keyring.get_password = password
    package = ModuleType("omero")
    package.__path__ = []
    gateway = ModuleType("omero.gateway")
    class Gateway:
        def __init__(self, username, password, **kwargs):
            self.username = username
            self.password = password
            self.kwargs = kwargs
            self.closed = False
            state.connections.append(self)

        def connect(self):
            logging.getLogger("omero.gateway").warning("test-only-session-uuid %s", self.password)
            if state.fail_connect:
                raise RuntimeError("test-only-session-uuid " + self.password)
            return True

        def close(self):
            self.closed = True
            assert Path(self.kwargs["extra_config"][0]).is_file()
            if state.fail_close:
                raise RuntimeError("test-only-session-uuid " + self.password)

    gateway.BlitzGateway = Gateway
    for name, module in {"keyring": keyring, "omero": package, "omero.gateway": gateway}.items():
        monkeypatch.setitem(sys.modules, name, module)
    return state


def connection():
    return SimpleNamespace(host="server.invalid", port=4064, username="scientist")


def test_shared_keyring_contract_and_scoped_secret_free_ice_config(fake_sdk, context, caplog):
    with omero_session(connection(), context) as session:
        assert session.password == "test-only-secret"
        config = Path(session.kwargs["extra_config"][0])
        text = config.read_text()
        assert "ConnectTimeout=30000" in text and "Timeout=60000" in text
        assert "test-only-secret" not in text
        assert session.kwargs["secure"] is True
        assert "test-only-session-uuid" not in caplog.text
    assert fake_sdk.lookups == [(KEYRING_SERVICE, "server.invalid:4064:scientist")]
    assert fake_sdk.connections[0].closed
    assert not config.exists()
    logging.getLogger("omero.gateway").warning("restored outside transfer")
    assert "restored outside transfer" in caplog.text


def test_absent_keyring_credential_never_opens_connection(fake_sdk, context):
    fake_sdk.password = None
    with pytest.raises(OmeroToolError, match="No OMERO password"):
        with omero_session(connection(), context):
            pytest.fail("No session should be yielded")
    assert fake_sdk.connections == []


@pytest.mark.parametrize("failure", ["connect", "body", "close"])
def test_sdk_exception_and_session_uuid_are_not_forwarded(fake_sdk, context, caplog, failure):
    fake_sdk.fail_connect = failure == "connect"
    fake_sdk.fail_close = failure == "close"
    with pytest.raises(OmeroToolError) as caught:
        with omero_session(connection(), context):
            if failure == "body":
                raise RuntimeError("test-only-session-uuid test-only-secret")
    diagnostic = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert "test-only-session-uuid" not in diagnostic + caplog.text
    assert "test-only-secret" not in diagnostic + caplog.text
    assert fake_sdk.connections[0].closed
    assert not list(context.row_dir.glob("*.ice.config"))


def test_overlapping_session_filters_restore_only_their_own_scope(fake_sdk, context, caplog):
    logger = logging.getLogger("omero.gateway")
    original = tuple(logger.filters)
    with omero_session(connection(), context):
        outer_filters = tuple(logger.filters)
        with omero_session(connection(), context):
            assert len(logger.filters) == len(original) + 2
        assert tuple(logger.filters) == outer_filters
        logger.warning("test-only-session-uuid")
    assert tuple(logger.filters) == original
    assert "test-only-session-uuid" not in caplog.text


def test_primary_validation_error_survives_cleanup_failure(fake_sdk, context, caplog):
    fake_sdk.fail_close = True
    with pytest.raises(OmeroToolError, match="Primary safe diagnostic"):
        with omero_session(connection(), context):
            raise OmeroToolError("Primary safe diagnostic")
    assert "OMERO session cleanup also failed" in caplog.text
    assert "test-only-secret" not in caplog.text
