"""Static contract checks for the standalone Runtime Console module."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_console_module_exposes_the_mount_seam_and_runtime_contract() -> None:
    script = (ROOT / "athena/web/static/runtime-console.js").read_text(encoding="utf-8")

    assert "export function mountRuntimeConsole(host, options)" in script
    assert "options?.api" in script
    assert 'const RUNTIME_ROOT = "/api/runtime/tasks"' in script
    for endpoint in ("/run", "/cancel", "/input", "/events", "/evidence", "/context", "/usage"):
        assert endpoint in script
    assert "PUBLIC_EVENT_FIELD" in script
    assert "thought|reasoning|chain" in script
    assert "destroy:" in script


def test_runtime_console_styles_cover_console_and_narrow_screen_layout() -> None:
    stylesheet = (ROOT / "athena/web/static/styles/runtime-console.css").read_text(encoding="utf-8")

    for marker in (
        ".runtime-console__grid",
        ".runtime-console__timeline",
        ".runtime-console__inspector",
        ".runtime-console__composer",
        ".runtime-console__status--success",
        "@media (max-width: 760px)",
    ):
        assert marker in stylesheet
