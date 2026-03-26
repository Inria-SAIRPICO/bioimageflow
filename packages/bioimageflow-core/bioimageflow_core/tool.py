"""Tool base classes — zero external dependencies."""

from enum import Enum
from typing import Any, ClassVar


class Category(str, Enum):
    """High-level functional category for a tool."""
    CONVERSION = "conversion"
    IMAGE_PROCESSING = "image_processing"
    SEGMENTATION = "segmentation"
    REGISTRATION = "registration"
    SPECTRAL_ANALYSIS = "spectral_analysis"
    TRACKING = "tracking"
    MEASUREMENT = "measurement"
    SPOT_DETECTION = "spot_detection"
    DECONVOLUTION = "deconvolution"
    RESTORATION = "restoration"
    COLOCALIZATION = "colocalization"
    STITCHING = "stitching"
    CLASSIFICATION = "classification"
    UTILITIES = "utilities"


class IOModel:
    """Lightweight declarative base for tool Inputs/Outputs."""

    @classmethod
    def _get_all_annotations(cls) -> dict[str, Any]:
        """Walk the MRO to collect annotations from all ancestor classes."""
        annotations: dict[str, Any] = {}
        for klass in reversed(cls.__mro__):
            annotations.update(getattr(klass, '__annotations__', {}))
        return annotations

    def __init__(self, **kwargs: Any) -> None:
        all_annotations = self._get_all_annotations()
        unknown = set(kwargs) - set(all_annotations)
        if unknown:
            raise TypeError(f"Unknown fields: {unknown}")
        for name in all_annotations:
            if name in kwargs:
                setattr(self, name, kwargs[name])
            elif hasattr(self.__class__, name):
                setattr(self, name, getattr(self.__class__, name))
            else:
                raise TypeError(f"Missing required field: '{name}'")

    def __repr__(self) -> str:
        fields = {k: getattr(self, k) for k in self._get_all_annotations()}
        return f"{self.__class__.__name__}({fields})"


class BaseTool:
    """
    Common base for all tools. Provides identity and Inputs.
    __call__ is NOT defined here — each subclass defines its own.
    """
    name: ClassVar[str]
    documentation: ClassVar[str] = ""
    category: ClassVar[Category | None] = None
    tags: ClassVar[list[str]] = []
    Inputs: ClassVar[type[IOModel]] = IOModel
    Outputs: ClassVar[type[IOModel] | None] = None

    def __init__(self) -> None:
        pass


class ProcessingTool(BaseTool):
    """Tool that processes data in an isolated Wetlands environment."""
    environment: ClassVar[Any]
    Outputs: ClassVar[type[IOModel] | None]
    resources: ClassVar[Any] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Only validate leaf concrete classes that define BOTH name and Outputs on themselves
        has_own_name = 'name' in cls.__dict__ and isinstance(cls.__dict__['name'], str)
        has_own_outputs = 'Outputs' in cls.__dict__
        if not has_own_name or not has_own_outputs:
            return
        # Check that at least one of process_row or process_batch is overridden
        has_process_row = cls.process_row is not ProcessingTool.process_row
        has_process_batch = cls.process_batch is not ProcessingTool.process_batch
        if not has_process_row and not has_process_batch:
            raise TypeError(
                f"{cls.__name__} must implement process_row or process_batch"
            )

    def __call__(self, *, name: str | None = None, **kwargs: Any) -> Any:
        """Create a graph node. No computation occurs."""
        try:
            from bioimageflow.node import Node
        except ImportError:
            raise RuntimeError(
                f"{type(self).__name__}.__call__() requires the bioimageflow "
                f"orchestrator package. This method is not available in worker "
                f"environments — use process_row/process_batch instead."
            )
        return Node(tool=self, kwargs=kwargs, name=name)

    def process_row(self, arguments: Any) -> Any:
        """Process a single row. Override in subclasses."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement process_row or process_batch."
        )

    def process_batch(self, arguments_list: list[Any]) -> Any:
        """Process all rows at once. Override for batch processing."""
        raise NotImplementedError
