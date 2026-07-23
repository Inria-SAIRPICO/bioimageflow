"""Engine-to-workflow event value objects."""

from dataclasses import dataclass


@dataclass
class ProgressEvent:
    """Progress event reported by an execution engine."""

    node_name: str
    status: str
    row: int = 0
    total_rows: int = 0
    message: str | None = None
    current: int | None = None
    maximum: int | None = None
    timestamp: float = 0.0
    result_key: str | None = None
    record_id: str | None = None
