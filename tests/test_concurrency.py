"""Async background and trace concurrency tests."""

from __future__ import annotations

import asyncio

import pytest

from athena.learning import CuratorDaemon, EventBus, TraceEvent, Tracer


@pytest.mark.asyncio
async def test_event_bus_handles_concurrent_trace_events() -> None:
    bus = EventBus()
    tracer = Tracer(max_events=100)
    bus.subscribe(tracer)

    await asyncio.gather(
        *(
            bus.publish(TraceEvent(name="step", run_id=str(index)))
            for index in range(20)
        )
    )

    assert len(tracer.events) == 20


@pytest.mark.asyncio
async def test_curator_daemon_starts_and_stops_without_blocking() -> None:
    tracer = Tracer(max_events=10)
    ran = asyncio.Event()

    async def job(_: Tracer) -> None:
        ran.set()

    daemon = CuratorDaemon(tracer, job=job, interval_seconds=0.05)
    await daemon.start()
    await asyncio.wait_for(ran.wait(), timeout=1)
    await daemon.stop()

    assert ran.is_set()
