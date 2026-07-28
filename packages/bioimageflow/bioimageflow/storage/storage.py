"""Public storage repository assembled from focused behavior units."""

from .repository import _RepositoryMixin
from .records import _ExactRecordsMixin
from .run_views import _RunViewsMixin
from .output_views import _OutputViewsMixin


class Storage(
    _RepositoryMixin,
    _ExactRecordsMixin,
    _RunViewsMixin,
    _OutputViewsMixin,
):
    """Filesystem paths and guarded metadata updates for versioned storage."""
