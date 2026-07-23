"""Public structured errors raised by the Parsl backend."""

from __future__ import annotations

from typing import Any

from bioimageflow.engine import WorkerTaskError


class ParslTaskError(WorkerTaskError):
    """A remote Parsl task failure with stable correlation metadata."""

    def __init__(
        self,
        message: str | None = None,
        *,
        scoped_node_name: str = "",
        tool_origin: Any = None,
        executor_label: str = "",
        task_id: str = "",
        invocation_id: str = "",
        cache_attempt_id: str | None = None,
        task_retry: int = 0,
        row_position: int | tuple[int, int] | None = None,
        original_type: str = "",
        original_message: str = "",
        remote_traceback: str | None = None,
    ) -> None:
        self.scoped_node_name = scoped_node_name
        self.tool_origin = tool_origin
        self.executor_label = executor_label
        self.task_id = task_id
        self.invocation_id = invocation_id
        self.cache_attempt_id = cache_attempt_id
        self.task_retry = task_retry
        self.row_position = row_position
        self.original_type = original_type
        self.original_message = original_message
        self.remote_traceback = remote_traceback
        first_position = (
            -1
            if row_position is None
            else row_position[0]
            if isinstance(row_position, tuple)
            else row_position
        )
        self.failure_order_key: tuple[int, int, str] = (
            0,
            first_position,
            task_id,
        )

        if message is None:
            correlation = (
                f"task={task_id!r}, invocation={invocation_id!r}, "
                f"attempt={cache_attempt_id!r}, retry={task_retry}"
            )
            location = (
                "whole-node batch"
                if row_position is None
                else f"row position {row_position!r}"
            )
            message = (
                f"Parsl task failed for node {scoped_node_name!r} "
                f"({location}; {correlation}) on executor {executor_label!r}. "
                f"{original_type}: {original_message}"
            )
            if remote_traceback:
                message = f"{message}\nRemote traceback:\n{remote_traceback}"

        super().__init__(
            message,
            node_name=scoped_node_name,
            tool_class=repr(tool_origin),
            environment_name=executor_label,
            row_index=None if row_position is None else str(row_position),
            task_traceback=remote_traceback,
        )


__all__ = ["ParslTaskError"]
