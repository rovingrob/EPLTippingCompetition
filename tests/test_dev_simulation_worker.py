from __future__ import annotations

import asyncio

from epl_tipping import main


def test_simulation_worker_poll_seconds_uses_default_and_minimum(monkeypatch) -> None:
    monkeypatch.delenv("TIPPING_SIMULATION_WORKER_POLL_SECONDS", raising=False)
    assert main.simulation_worker_poll_seconds() == 1.0

    monkeypatch.setenv("TIPPING_SIMULATION_WORKER_POLL_SECONDS", "invalid")
    assert main.simulation_worker_poll_seconds() == 1.0

    monkeypatch.setenv("TIPPING_SIMULATION_WORKER_POLL_SECONDS", "0")
    assert main.simulation_worker_poll_seconds() == 0.1


def test_simulation_worker_loop_drains_queue_until_idle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TIPPING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TIPPING_SIMULATION_WORKER_POLL_SECONDS", "0.1")
    responses = [{"status": "completed"}, {"status": "completed"}, None]
    idle = asyncio.Event()

    async def fake_process_next(*args, **kwargs):
        response = responses.pop(0)
        if response is None:
            idle.set()
        return response

    monkeypatch.setattr(main, "process_next_simulation", fake_process_next)

    async def exercise() -> None:
        worker = asyncio.create_task(main.simulation_worker_loop())
        await asyncio.wait_for(idle.wait(), timeout=1)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())
    assert responses == []


def test_app_lifespan_starts_and_stops_enabled_worker(monkeypatch) -> None:
    monkeypatch.setenv("TIPPING_DEV_SIMULATION_WORKER", "true")
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def fake_worker() -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    monkeypatch.setattr(main, "simulation_worker_loop", fake_worker)

    async def exercise() -> None:
        async with main.app_lifespan(main.app):
            await asyncio.wait_for(started.wait(), timeout=1)
        assert stopped.is_set()

    asyncio.run(exercise())
