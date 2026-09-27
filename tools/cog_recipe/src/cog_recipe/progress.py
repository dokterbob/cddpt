"""A tiny, dependency-free progress-reporting hook.

The core pipeline never imports `tqdm` or `typer` -- it only calls an
optional callback with a typed event. The CLI layer supplies a callback that
drives `tqdm`; a future `cddpt` integration, or a GUI, can supply its own
without touching the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One reported step of a pipeline stage.

    ``stage`` is a short machine-readable label (e.g. ``"scan"``,
    ``"base-write"``, ``"overviews"``, ``"cog-encode"``, ``"validate"``,
    ``"convert"``). ``current``/``total`` describe progress *within* that
    stage (``total`` may be 0 if unknown, e.g. before a scan completes).
    ``message`` is a short human-readable detail (e.g. a file name).
    """

    stage: str
    current: int
    total: int
    message: str = ""


class ProgressCallback(Protocol):
    """Callable invoked with each :class:`ProgressEvent`."""

    def __call__(self, event: ProgressEvent) -> None: ...


def report(callback: ProgressCallback | None, event: ProgressEvent) -> None:
    """Call ``callback(event)`` if a callback was supplied."""

    if callback is not None:
        callback(event)
