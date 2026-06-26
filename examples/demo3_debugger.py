"""Demo 3 - execution trace, token statistics, and step debugger."""

from __future__ import annotations

from athena.learning.tracer import TraceEvent, Tracer
from athena.observability.debugger import DebuggerCommand, StepDebugger


def main() -> None:
    """Run the debugger and observability demo."""
    tracer = Tracer(max_events=20)
    run_id = "demo3-run-001"
    token_usage = {
        "prompt_tokens": 1280,
        "completion_tokens": 240,
        "total_tokens": 1520,
    }
    for name, detail in (
        ("agent.step", "Plan code analysis"),
        ("tool.call", "parse_code_outline"),
        ("agent.step", "Draft unit test"),
        ("agent.final", "Return test draft"),
    ):
        tracer.record(TraceEvent(name, run_id, payload={"detail": detail}))

    debugger = StepDebugger()
    debugger.add_breakpoint("tool.call")
    should_pause = debugger.should_pause("tool.call")
    debugger.apply(DebuggerCommand("pause"))

    print("# Demo 3: Debugger and Trace")
    print(f"Run id: {run_id}")
    print(f"Token usage: {token_usage}")
    print(f"Breakpoint hit: {should_pause}, paused={debugger.paused}")
    print("\n## Trace Events")
    for event in tracer.by_run(run_id):
        print(f"- {event.name}: {event.payload['detail']}")


if __name__ == "__main__":
    main()
