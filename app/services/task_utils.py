"""Helpers for fire-and-forget asyncio tasks.

The event loop only keeps weak references to tasks, so a task created with
``asyncio.create_task`` and never referenced can be garbage collected
mid-flight. ``spawn`` keeps a strong reference until completion and
``shutdown`` drains everything on app exit.
"""

import asyncio
from typing import Any, Coroutine

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Schedule a coroutine and keep a strong reference to its task."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def shutdown() -> None:
    """Cancel and await all outstanding background tasks."""
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()