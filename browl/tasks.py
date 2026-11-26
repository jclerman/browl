"""Running reads concurrently."""

import asyncio
from collections.abc import AsyncIterator, Awaitable
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


async def caught[T, E: Exception](awaitable: Awaitable[T], *errors: type[E]) -> T | E:
    """Return what an awaitable returns, or the error of these types it raises.

    In a task group, the error then doesn't cancel the other tasks, and the caller can log it only if they succeed.
    """
    try:
        return await awaitable
    except errors as error:
        return error
