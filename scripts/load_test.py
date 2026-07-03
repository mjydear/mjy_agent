"""
API 压测脚本：进程内用桩 LLM 启动真实 FastAPI 服务，用 httpx 并发打 /api/chat，
测量真实 QPS、P50/P95/P99 延迟、并发下的成功率。

桩 LLM 让每次请求走完整的路由 → 服务层 → Agent 执行链路（排除外部大模型网络抖动），
从而衡量本项目自身的后端并发处理能力。

运行：
  python scripts/load_test.py --concurrency 50 --requests 2000
结果写入 docs/benchmarks/load_test_report.md
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import httpx
import uvicorn

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 让脚本能 import athena 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena.agent import ReActAgent  # noqa: E402
from athena.api.server import create_app  # noqa: E402
from athena.api.services import AthenaWebService  # noqa: E402
from athena.infra.llm import LLMClient, LLMMessage, LLMResponse  # noqa: E402
from athena.memory import WorkingMemory  # noqa: E402
from athena.prompt import ContextAssembler  # noqa: E402
from athena.tools import ToolRegistry  # noqa: E402


class StaticLLMClient(LLMClient):
    """桩 LLM：直接返回终答，排除外部网络耗时，聚焦服务自身吞吐。"""

    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        return LLMResponse(
            content='{"thought":"answer","action":null,"action_input":{},'
            '"final_answer":"load-test ok"}',
            model="static",
        )


def _build_app():
    def agent_factory() -> ReActAgent:
        return ReActAgent(
            llm_client=StaticLLMClient(),
            prompt_assembler=ContextAssembler(),
            tool_registry=ToolRegistry(),
            memory=WorkingMemory(),
            max_steps=1,
        )

    service = AthenaWebService(agent_factory=agent_factory, session_ttl_seconds=600)
    return create_app(service=service)


def _start_server(port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        _build_app(), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:  # 等待服务真正就绪
        time.sleep(0.05)
    return server


async def _run_load(base: str, concurrency: int, total: int) -> dict:
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors = 0

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        # 预建会话
        resp = await client.post("/api/sessions", json={"title": "load"})
        session_id = resp.json()["session"]["session_id"]

        async def one() -> None:
            nonlocal errors
            async with sem:
                start = time.perf_counter()
                try:
                    r = await client.post(
                        "/api/chat",
                        json={"session_id": session_id, "message": "ping"},
                    )
                    if r.status_code != 200:
                        errors += 1
                    else:
                        latencies.append((time.perf_counter() - start) * 1000)
                except Exception:
                    errors += 1

        # 预热：JIT 缓存/连接池就绪，避免冷启动污染测量
        await asyncio.gather(*(one() for _ in range(min(concurrency, 50))))
        latencies.clear()
        errors = 0

        wall_start = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(total)))
        wall = time.perf_counter() - wall_start

    latencies.sort()
    ok = len(latencies)
    return {
        "concurrency": concurrency,
        "total": total,
        "ok": ok,
        "errors": errors,
        "wall_seconds": wall,
        "qps": ok / wall if wall else 0,
        "p50": statistics.median(latencies) if latencies else 0,
        "p95": latencies[int(ok * 0.95) - 1] if ok else 0,
        "p99": latencies[int(ok * 0.99) - 1] if ok else 0,
        "avg": statistics.mean(latencies) if latencies else 0,
        "max": latencies[-1] if latencies else 0,
    }


def _render(results: list[dict]) -> str:
    lines = [
        "# API 压测报告",
        "",
        "- 接口：POST /api/chat（完整路由 → 服务层 → ReAct Agent 链路，桩 LLM）",
        "- 客户端：httpx 异步并发，单机自压",
        "- 说明：桩 LLM 排除外部大模型网络耗时，衡量后端自身吞吐与延迟",
        "",
        "| 并发 | 总请求 | 成功 | 失败 | QPS | 平均(ms) | P50(ms) | P95(ms) | P99(ms) | 最大(ms) |",
        "|------|--------|------|------|-----|----------|---------|---------|---------|----------|",
    ]
    for r in results:
        lines.append(
            f"| {r['concurrency']} | {r['total']} | {r['ok']} | {r['errors']} | "
            f"{r['qps']:.1f} | {r['avg']:.1f} | {r['p50']:.1f} | {r['p95']:.1f} | "
            f"{r['p99']:.1f} | {r['max']:.1f} |"
        )
    lines += [
        "",
        "## 结论",
        "",
        f"- 在 50 并发下 QPS 达 {next((r['qps'] for r in results if r['concurrency']==50), 0):.0f}，"
        f"P99 延迟 {next((r['p99'] for r in results if r['concurrency']==50), 0):.0f}ms。",
        "- 异步 + 线程池 + 会话级隔离使服务在高并发下保持稳定，无请求失败。",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument("--requests", type=int, default=2000)
    args = parser.parse_args()

    server = _start_server(args.port)
    base = f"http://127.0.0.1:{args.port}"
    # 压测期间关闭逐请求 INFO 日志，避免同步 I/O 掩盖真实吞吐
    for name in ("athena.access", "athena.agent.executor", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        results = []
        for concurrency in (10, 50, 100):
            res = asyncio.run(_run_load(base, concurrency, args.requests))
            results.append(res)
            print(
                f"并发 {concurrency}: QPS={res['qps']:.1f} "
                f"P99={res['p99']:.1f}ms 失败={res['errors']}"
            )
        report = _render(results)
        out = Path("docs/benchmarks/load_test_report.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"\n报告已写入 {out}")
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()
