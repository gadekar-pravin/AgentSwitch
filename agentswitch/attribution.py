"""Pool threads do not inherit context; workers must enter ``node_context`` inside callables."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT_NODE: ContextVar[str | None] = ContextVar("current_graph_node", default=None)


def current_node() -> str | None:
    """Return the graph node attributed to the current execution context."""
    return _CURRENT_NODE.get()


@contextmanager
def node_context(node_id: str | None) -> Iterator[None]:
    """Temporarily attribute MCP calls to ``node_id`` in this context."""
    token = _CURRENT_NODE.set(node_id)
    try:
        yield
    finally:
        _CURRENT_NODE.reset(token)
