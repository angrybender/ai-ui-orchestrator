from __future__ import annotations

import asyncio
import json

from starlette.concurrency import run_in_threadpool

import agent_store
import board_store

POLL_INTERVAL = 0.5
HEARTBEAT_POLLS = 30


async def task_events(request):
    previous = None
    idle_polls = 0
    while not await request.is_disconnected():
        tasks = await run_in_threadpool(board_store.list_tasks)
        recovery_runs = await run_in_threadpool(agent_store.recovery_candidates)
        payload = json.dumps({"tasks": tasks, "recovery_runs": recovery_runs}, ensure_ascii=False)
        if payload != previous:
            yield f"event: board\ndata: {payload}\n\n"
            previous = payload
            idle_polls = 0
        else:
            idle_polls += 1
            if idle_polls >= HEARTBEAT_POLLS:
                yield ": keepalive\n\n"
                idle_polls = 0
        await asyncio.sleep(POLL_INTERVAL)
