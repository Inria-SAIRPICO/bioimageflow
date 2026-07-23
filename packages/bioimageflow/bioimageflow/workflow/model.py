"""Public workflow model assembled from focused behavior units."""

from __future__ import annotations

from .common import MISSING
from .interfaces import _InterfacesMixin
from .inspection import _InspectionMixin
from .invalidation import _InvalidationMixin
from .runtime import _RuntimeMixin
from .serialization import _SerializationMixin
from .loading import _LoadingMixin
from .materialization import _MaterializationMixin


class Workflow(
    _InterfacesMixin,
    _InspectionMixin,
    _InvalidationMixin,
    _RuntimeMixin,
    _SerializationMixin,
    _LoadingMixin,
    _MaterializationMixin,
):
    """Holds the DAG and provides configuration for execution."""

    MISSING = MISSING
