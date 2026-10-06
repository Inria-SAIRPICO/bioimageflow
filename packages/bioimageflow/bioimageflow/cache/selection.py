"""One exact selected record accompanies the dataframe actually consumed."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import json
from pathlib import Path

import pandas as pd

from bioimageflow.record_shared_assets import RecordSharedAssets
from bioimageflow.storage import RecordManifest, Storage


@dataclass(frozen=True)
class SelectedResult:
    """Pinned record facts; dataframe mutability has a separate owner contract."""

    dataframe: pd.DataFrame
    result_key: str
    record_id: str
    record_dir: Path
    _manifest_json: str = field(repr=False)

    @property
    def manifest(self) -> RecordManifest:
        """Expose a fresh metadata projection without a writable authority alias."""
        return RecordManifest.from_dict(json.loads(self._manifest_json))


def selected_result(
    storage: Storage,
    result_key: str,
    record_id: str,
    *,
    path_columns: Iterable[str] = (),
    shared_array_columns: Iterable[str] = (),
    hydrate_assets: bool = False,
    accepted_shared_assets: RecordSharedAssets | None = None,
) -> SelectedResult:
    """Read an exact admitted record; never consult the current pointer."""
    load = storage.load_record if accepted_shared_assets is None else storage._load_record_with_shared_assets
    bindings = {} if accepted_shared_assets is None else {"accepted_shared_assets": accepted_shared_assets}
    manifest, dataframe, record_dir = load(
        result_key,
        record_id,
        path_columns=path_columns,
        shared_array_columns=shared_array_columns,
        hydrate_assets=hydrate_assets,
        **bindings,
    )
    return SelectedResult(
        dataframe=dataframe,
        result_key=result_key,
        record_id=record_id,
        record_dir=record_dir,
        _manifest_json=json.dumps(manifest.to_dict(), sort_keys=True),
    )
