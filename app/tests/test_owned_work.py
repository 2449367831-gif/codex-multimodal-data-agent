import asyncio
from contextvars import ContextVar
import threading
import unittest

from app.owned_work import run_owned_thread


class OwnedWorkTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_kwargs_and_context_are_preserved(self):
        identity = ContextVar("identity", default="absent")
        token = identity.set("request-owner")
        try:
            result = await run_owned_thread(lambda *, value: (value, identity.get()), value=7)
            self.assertEqual((7, "request-owner"), result)
        finally:
            identity.reset(token)

    async def test_exception_is_not_replaced(self):
        def fail():
            raise ValueError("synthetic")

        with self.assertRaisesRegex(ValueError, "synthetic"):
            await run_owned_thread(fail)

    async def test_repeated_cancellation_does_not_release_ownership_early(self):
        started, release = threading.Event(), threading.Event()
        finished = False

        def work():
            started.set()
            if not release.wait(5):
                raise AssertionError("test worker was not released")
            raise ValueError("late synthetic failure")

        async def owner():
            nonlocal finished
            try:
                await run_owned_thread(work)
            finally:
                finished = True

        task = asyncio.create_task(owner())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            self.assertFalse(finished)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished)
