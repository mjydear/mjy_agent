"""Small CLI for running the local Agent Runtime."""

from __future__ import annotations

from pathlib import Path

import typer

from athena.application.runtime_task_service import RuntimeTaskService
from athena.config import load_settings
from athena.runtime import build_runtime

app = typer.Typer(help="Run the Athena ReAct Agent Runtime.")


@app.command()
def run(
    goal: str = typer.Argument(..., help="Task for the Runtime."),
    repository: Path = typer.Option(Path.cwd(), "--repository", "-r"),
    profile: str = typer.Option("standard", "--profile"),
) -> None:
    """Execute one inspectable Runtime task."""
    assembly = build_runtime(load_settings())
    service = RuntimeTaskService(
        assembly.runtime,
        assembly.store,
        backend=assembly.backend,
        decision_mode=assembly.decision_mode,
        memory_strategy=assembly.memory_strategy,
    )
    task = service.create(
        goal=goal,
        repository_path=str(repository),
        profile=profile,
    )
    result = service.run(str(task["id"]))
    typer.echo(result)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
