# Athena Agent

Athena Agent is a from-scratch MVP for a self-evolving enterprise developer assistant. The first phase focuses on a runnable Agent loop rather than a framework wrapper: LLM gateway, prompt assembly, decorator-based tools, ReAct execution, short-term memory, CLI, and vector-store abstraction.

## Phase 1 Scope

- Python 3.11+ package scaffold.
- LiteLLM-backed LLM gateway with a provider-neutral interface.
- Prompt assembly from system prompt, working memory, tools, scratchpad, and user query.
- Decorator-based tool registry and invocation.
- ReAct loop with Thought, Action, Observation, and final-answer handling.
- Token-aware working memory with MVP pruning.
- Typer CLI entrypoint.
- VectorStore protocol with in-memory fallback and Milvus adapter boundary.

## Quick Start

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
athena chat "Echo hello with a tool if useful"
```

Set your model credentials according to the LiteLLM provider you choose. The default model can be overridden with `ATHENA_LLM_MODEL`.

## Development Checks

```powershell
pytest
```

## Design Notes

Athena intentionally avoids LangChain and LlamaIndex in core logic. The agent loop, prompt assembly, tool registry, memory, and vector-store interfaces are implemented directly so each moving part is explainable in interviews and extensible in later phases.
