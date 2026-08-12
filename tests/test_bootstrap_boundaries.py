"""P4-04 bootstrap boundary regression tests."""

from __future__ import annotations

from pathlib import Path

from athena.bootstrap import build_agent as bootstrap_build_agent
from athena.bootstrap.agent_factory import build_agent as agent_factory_build_agent
from athena.cli.main import build_agent as cli_build_agent


def test_entrypoints_share_public_bootstrap_agent_factory() -> None:
    assert bootstrap_build_agent is agent_factory_build_agent
    assert cli_build_agent is bootstrap_build_agent


def test_api_layer_does_not_import_cli_composition_root() -> None:
    api_root = Path("athena/api")
    offenders = []
    for path in api_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "athena.cli.main import build_agent" in source:
            offenders.append(path.as_posix())
    assert offenders == []
