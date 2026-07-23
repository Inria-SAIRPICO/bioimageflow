"""Public storage repository assembled from focused behavior units."""

from .repository import _RepositoryMixin
from .run_views import _RunViewsMixin
from .output_views import _OutputViewsMixin


class Storage(_RepositoryMixin, _RunViewsMixin, _OutputViewsMixin):
    """Filesystem paths and guarded metadata updates for versioned storage."""
