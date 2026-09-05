"""Bounded asynchronous work scheduling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable


@dataclass(frozen=True)
class WorkItemResult:
    item_id: str
    success: bool
    value: Any = None
    error: str = ""


@dataclass(frozen=True)
class BoundedRunResult:
    items: list[WorkItemResult]
    peak_active: int

    @property
    def completed_count(self) -> int:
        return sum(item.success for item in self.items)

    @property
    def failed_count(self) -> int:
        return len(self.items) - self.completed_count


def _item_id(item: Any) -> str:
    return str(getattr(item, "course_id", item))


async def run_bounded(
    items: Iterable[Any],
    *,
    concurrency: int,
    handler: Callable[[Any], Awaitable[Any]],
) -> BoundedRunResult:
    """Run every item with a hard upper bound of fifteen active handlers."""
    if not 1 <= concurrency <= 15:
        raise ValueError("concurrency must be between 1 and 15")

    pending_items = list(items)
    if not pending_items:
        return BoundedRunResult(items=[], peak_active=0)

    queue: asyncio.Queue[Any] = asyncio.Queue()
    for item in pending_items:
        queue.put_nowait(item)

    active = 0
    peak_active = 0
    results: list[WorkItemResult] = []
    state_lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal active, peak_active
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            async with state_lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                value = await handler(item)
                result = WorkItemResult(
                    item_id=_item_id(item), success=True, value=value
                )
            except Exception as exc:
                result = WorkItemResult(
                    item_id=_item_id(item),
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                async with state_lock:
                    active -= 1
                queue.task_done()
            results.append(result)

    worker_count = min(concurrency, len(pending_items))
    await asyncio.gather(*(worker() for _ in range(worker_count)))
    return BoundedRunResult(items=results, peak_active=peak_active)
