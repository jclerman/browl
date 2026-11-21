"""Running reads concurrently."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def task_group() -> AsyncIterator[asyncio.TaskGroup]:
    """Yield a task group that raises the first error of its tasks, not an `ExceptionGroup`.

    Unlike `gather`, a task group cancels the other tasks when one fails.
    """
    try:
        async with asyncio.TaskGroup() as group:
            yield group
    except ExceptionGroup as errors:
        raise errors.exceptions[0]  # noqa: B904 -- the task's own error, with the group as its context
