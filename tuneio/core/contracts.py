"""Narrow integration contracts; adapters own HTTP and persistence semantics."""
from typing import Iterator, Protocol


class MetricSink(Protocol):
    def record(self, method: str, route: str, status: int, duration_ms: float, cache: str) -> None: ...


class ObservationStore(Protocol):
    """Bounded event history; persistence is a later adapter, not a core import."""
    maxlen: int
    def append(self, observation: dict) -> None: ...
    def __iter__(self) -> Iterator[dict]: ...
    def __len__(self) -> int: ...
