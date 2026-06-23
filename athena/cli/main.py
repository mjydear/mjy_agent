"""Typer CLI entrypoint for Athena Agent."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from athena.agent import ReActAgent
from athena.config import load_settings
from athena.exceptions import AthenaError
from athena.infra.llm import LLMClientFactory
from athena.logging import configure_logging
from athena.memory import WorkingMemory
from athena.prompt import ContextAssembler
from athena.tools import ToolRegistry
from athena.tools.builtin.basic import register_basic_tools

app = typer.Typer(help="Athena Agent command line interface.")


def build_agent(config_path: Path | None = None) -> ReActAgent:
    """Build an Athena ReAct agent from configuration.

    Args:
        config_path: Optional path to config YAML.

    Returns:
        Configured ReAct agent instance.
    """
    settings = load_settings(config_path)
    configure_logging(settings.logging.level)
    registry = ToolRegistry()
    register_basic_tools(registry)
    llm_client = LLMClientFactory.create(
        provider=settings.llm.provider,
        model=settings.llm.model,
        temperature=settings.llm.temperature,
        max_tokens=settings.llm.max_tokens,
    )
    return ReActAgent(
        llm_client=llm_client,
        prompt_assembler=ContextAssembler(),
        tool_registry=registry,
        memory=WorkingMemory(max_tokens=settings.memory.working_max_tokens),
        max_steps=settings.agent.max_steps,
    )


@app.command()
def chat(
    query: str = typer.Argument(..., help="Single-turn user query."),
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to config.yaml."),
) -> None:
    """Run a single Athena chat request."""
    try:
        agent = build_agent(config)
        response = asyncio.run(agent.run(query))
        typer.echo(response.answer)
    except AthenaError as exc:
        typer.secho(f"Athena error [{exc.code}]: {exc.message}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


@app.command()
def start() -> None:
    """Start an interactive MVP session."""
    try:
        agent = build_agent(None)
    except AthenaError as exc:
        typer.secho(f"Athena error [{exc.code}]: {exc.message}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo("Athena Agent MVP session. Type 'exit' to quit.")
    while True:
        query = typer.prompt("You")
        if query.strip().lower() in {"exit", "quit"}:
            break
        try:
            response = asyncio.run(agent.run(query))
            typer.echo(f"Athena: {response.answer}")
        except AthenaError as exc:
            typer.secho(f"Athena error [{exc.code}]: {exc.message}", fg=typer.colors.RED)


def main() -> None:
    """Run the Typer application."""
    app()


if __name__ == "__main__":
    main()
