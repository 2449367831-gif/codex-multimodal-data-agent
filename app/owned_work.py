"""Keep synchronous work owned until its thread has actually finished."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar


P = ParamSpec("P")
T = TypeVar("T")


async def drain_task(task: asyncio.Task[object]) -> None:
    """Drain a cancelled operation even if shutdown cancels its owner again."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        task.exception()


async def run_owned_thread(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # Cancelling to_thread only cancels its awaiter. Leases held by callers
        # must survive until the actual database/model operation exits.
        await drain_task(work)
        raise
