from pathlib import Path

import pandas as pd

from bioimageflow.launcher.return_routes import ReturnProviderRoute
from bioimageflow.launcher.returns import (
    load_public_return,
    persist_public_return,
)


RUN_ID = "run_1234567812344abc923456789abcdef0"


def test_external_path_return_rehydrates_from_string_extension_dtype(
    tmp_path: Path,
) -> None:
    control = tmp_path / "launcher" / "v1" / "runs" / RUN_ID
    control.mkdir(parents=True)
    external = (tmp_path / "external.tif").resolve()
    value = pd.DataFrame(
        {
            "path": pd.Series(
                [external.as_posix()],
                dtype="string[pyarrow]",
                index=["row"],
            )
        }
    )

    persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        value,
        outcomes=(),
        root_outputs=[{"port_id": "output-image", "name": "path"}],
        provider_routes=(
            ReturnProviderRoute(
                mapping_key=None,
                public_column="path",
                node_key="source",
                provider_column="path",
                result_key=None,
                record_id=None,
                transient_invocation_id=None,
                owned=False,
                shared_array=False,
            ),
        ),
    )
    loaded = load_public_return(control, tmp_path, RUN_ID)

    assert loaded["path"].dtype == object
    assert loaded.at["row", "path"] == external
