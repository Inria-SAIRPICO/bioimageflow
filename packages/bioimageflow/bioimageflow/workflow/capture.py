"""Semantic definition values, independent of execution/resource handles."""
from typing import Any

import pandas as pd
from bioimageflow_core.defaults import snapshot_value


def capture_value(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        result = value.copy(deep=True)
        for column in range(len(result.columns)):
            if pd.api.types.is_object_dtype(result.dtypes.iloc[column]):
                for row in range(len(result)):
                    result.iat[row, column] = snapshot_value(result.iat[row, column])
        result.attrs = snapshot_value(value.attrs)
        return result
    return snapshot_value(value)


def capture_model(model: Any) -> Any:
    """Detach declaration mappings without changing the executable tool class."""
    if model is None:
        return None
    from bioimageflow_core import IOModel
    from bioimageflow.dataframe_tool import Passthrough
    namespace = {
        "__module__": model.__module__,
        "__annotations__": dict(model._get_all_annotations()),
        **model.capture_defaults(),
    }
    base = Passthrough if issubclass(model, Passthrough) else IOModel
    return type(model.__name__, (base,), namespace)
