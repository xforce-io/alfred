#!/usr/bin/env python3
"""Main loop orchestrator: send queries, evaluate answers, fix & regress.

Usage:
    python -m loop_test.runner [--config config.yaml] [--cases cases.yaml] [--case-id greet]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from . import agent_client, evaluator

logger = logging.getLogger(__name__)

# ANSI colors
_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_RESET = "\033[0m"

LOG_DIR = Path(__file__).parent / "log"


@dataclass
class Config:
    ws_url: str = "ws://localhost:8765/ws/chat/{agent_name}"
    agent_name: str = "demo_agent"
    api_key: str = ""
    claude_command: str = "claude"
    claude_flags: list[str] = field(default_factory=lambda: ["-p"])
    claude_timeout: float = 180.0
    alfred_home: str = "~/.alfred"
    project_root: str = "."
    max_iterations: int = 5
    everbot_bin: str = "bin/everbot"


@dataclass
class TestCase:
    id: str
    query: str
    expectation: str
    max_loops: Optional[int] = None


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    iterations: int
    last_reason: str


_CONFIG_DEFAULTS = Config()


class RunLogger:
    """Logs every step detail to loop_test/log/<run_id>/<case_id>.jsonl"""

    def __init__(self, run_id: str):
        self.run_dir = LOG_DIR / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, Path] = {}

    def _get_path(self, case_id: str) -> Path:
        if case_id not in self._files:
            self._files[case_id] = self.run_dir / f"{case_id}.jsonl"
        return self._files[case_id]

    def log(self, case_id: str, step: str, **data) -> None:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "case_id": case_id,
            "step": step,
            **data,
        }
        path = self._get_path(case_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def log_summary(self, results: list[CaseResult]) -> None:
        summary_path = self.run_dir / "summary.json"
        summary = {
            "timestamp": datetime.now().isoformat(),
            "results": [
                {
                    "case_id": r.case_id,
                    "passed": r.passed,
                    "iterations": r.iterations,
                    "last_reason": r.last_reason,
                }
                for r in results
            ],
        }
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)


async def restart_agent(cfg: Config) -> None:
    """Restart the EverBot daemon + web server between loop iterations."""
    import subprocess, os

    everbot_bin = Path(cfg.project_root).resolve() / cfg.everbot_bin
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    print(f"  Restarting agent ({everbot_bin})...")
    logger.info("Stopping EverBot...")
    subprocess.run(
        [str(everbot_bin), "stop"],
        capture_output=True, text=True, timeout=15, env=env,
    )
    await asyncio.sleep(1)

    logger.info("Starting EverBot...")
    subprocess.run(
        [str(everbot_bin), "start", "--background"],
        capture_output=True, text=True, timeout=15, env=env,
    )
    # Wait for web server to be ready
    await _wait_for_server(cfg, max_wait=15)


async def _wait_for_server(cfg: Config, max_wait: float = 15) -> None:
    """Wait until the web server responds."""
    import httpx
    from urllib.parse import urlparse

    parsed = urlparse(cfg.ws_url)
    base_url = f"http://{parsed.hostname}:{parsed.port}/docs"

    for _ in range(int(max_wait)):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(base_url, timeout=2)
                if resp.status_code == 200:
                    logger.info("Server is ready")
                    return
        except Exception:
            pass
        await asyncio.sleep(1)
    logger.warning("Server may not be ready after %.0fs", max_wait)


def load_config(path: Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    agent = raw.get("agent", {})
    claude = raw.get("claude", {})
    paths = raw.get("paths", {})
    loop = raw.get("loop", {})

    return Config(
        ws_url=agent.get("ws_url", _CONFIG_DEFAULTS.ws_url),
        agent_name=agent.get("agent_name", _CONFIG_DEFAULTS.agent_name),
        api_key=agent.get("api_key", _CONFIG_DEFAULTS.api_key),
        claude_command=claude.get("command", _CONFIG_DEFAULTS.claude_command),
        claude_flags=claude.get("flags", _CONFIG_DEFAULTS.claude_flags),
        claude_timeout=float(claude.get("timeout", _CONFIG_DEFAULTS.claude_timeout)),
        alfred_home=paths.get("alfred_home", _CONFIG_DEFAULTS.alfred_home),
        project_root=paths.get("project_root", _CONFIG_DEFAULTS.project_root),
        max_iterations=loop.get("max_iterations", _CONFIG_DEFAULTS.max_iterations),
        everbot_bin=agent.get("everbot_bin", _CONFIG_DEFAULTS.everbot_bin),
    )


def load_cases(path: Path) -> list[TestCase]:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    cases = []
    for item in raw.get("cases", []):
        cases.append(TestCase(
            id=item["id"],
            query=item["query"],
            expectation=item["expectation"],
            max_loops=item.get("max_loops"),
        ))
    return cases


async def run_case(cfg: Config, case: TestCase, rlog: RunLogger) -> CaseResult:
    """Run the test-optimize loop for a single test case."""
    max_loops = case.max_loops or cfg.max_iterations
    ws_url = cfg.ws_url.format(agent_name=cfg.agent_name)
    last_reason = ""

    print(f"\n{_CYAN}{_BOLD}=== Case: {case.id} ==={_RESET}")
    print(f"  Query: {case.query}")
    print(f"  Expectation: {case.expectation}")
    print(f"  Max loops: {max_loops}")

    rlog.log(case.id, "case_start", query=case.query, expectation=case.expectation, max_loops=max_loops)

    for iteration in range(1, max_loops + 1):
        print(f"\n  {_YELLOW}--- Iteration {iteration}/{max_loops} ---{_RESET}")
        rlog.log(case.id, "iteration_start", iteration=iteration)

        # Step 0: Restart agent between iterations
        if iteration > 1:
            try:
                await restart_agent(cfg)
                rlog.log(case.id, "agent_restart", status="ok")
            except Exception as exc:
                rlog.log(case.id, "agent_restart", status="error", error=str(exc))
                print(f"  {_RED}Restart failed: {exc}{_RESET}")

        # Step 1: Send query to agent
        print(f"  [1/4] Sending query to {cfg.agent_name}...")
        try:
            answer = await agent_client.send_new_and_query(
                ws_url, case.query,
                api_key=cfg.api_key,
                timeout=cfg.claude_timeout,
                agent_name=cfg.agent_name,
            )
        except Exception as exc:
            last_reason = f"Agent connection failed: {exc}"
            print(f"  {_RED}Agent error: {exc}{_RESET}")
            rlog.log(case.id, "agent_query", status="error", error=str(exc))
            continue

        print(f"  Answer ({len(answer)} chars): {answer[:150]}...")
        rlog.log(case.id, "agent_query", status="ok", query=case.query, answer=answer)

        # Step 2: Evaluate answer
        print(f"  [2/4] Evaluating answer with Claude Code...")
        result = evaluator.check_answer(
            case.query, answer, case.expectation,
            command=cfg.claude_command, flags=cfg.claude_flags, timeout=cfg.claude_timeout,
        )
        rlog.log(case.id, "check_answer", passed=result.passed, reason=result.reason,
                 input={"query": case.query, "answer": answer, "expectation": case.expectation},
                 output={"passed": result.passed, "reason": result.reason})

        if result.passed:
            print(f"  {_GREEN}PASS: {result.reason}{_RESET}")
            rlog.log(case.id, "iteration_end", iteration=iteration, result="pass")
            return CaseResult(
                case_id=case.id, passed=True,
                iterations=iteration, last_reason=result.reason,
            )

        last_reason = result.reason
        print(f"  {_RED}FAIL: {result.reason}{_RESET}")

        # Step 3: Analyze logs
        print(f"  [3/4] Analyzing logs at {cfg.alfred_home}...")
        analysis = evaluator.analyze_logs(
            cfg.alfred_home, cfg.agent_name,
            command=cfg.claude_command, flags=cfg.claude_flags, timeout=cfg.claude_timeout,
        )
        print(f"  Analysis: {analysis[:200]}...")
        rlog.log(case.id, "analyze_logs",
                 input={"alfred_home": cfg.alfred_home, "agent_name": cfg.agent_name},
                 output=analysis)

        # Step 4a: Suggest test cases
        print(f"  [4/4] Suggesting test cases & fixing...")
        suggestion = evaluator.suggest_testcases(
            analysis,
            command=cfg.claude_command, flags=cfg.claude_flags, timeout=cfg.claude_timeout,
        )
        print(f"  Suggestion: {suggestion[:200]}...")
        rlog.log(case.id, "suggest_testcases", input=analysis, output=suggestion)

        # Step 4b: Fix and regress
        fix_result = evaluator.fix_and_regress(
            suggestion,
            command=cfg.claude_command, flags=cfg.claude_flags, timeout=cfg.claude_timeout,
        )
        print(f"  Fix result: {fix_result[:200]}...")
        rlog.log(case.id, "fix_and_regress", input=suggestion, output=fix_result)

        rlog.log(case.id, "iteration_end", iteration=iteration, result="fail")

    rlog.log(case.id, "case_end", passed=False, iterations=max_loops, last_reason=last_reason)
    return CaseResult(
        case_id=case.id, passed=False,
        iterations=max_loops, last_reason=last_reason,
    )


def print_summary(results: list[CaseResult]) -> None:
    """Print a summary table of all case results."""
    print(f"\n{_BOLD}{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}{_RESET}")
    print(f"  {'Case ID':<20} {'Status':<10} {'Iters':<8} {'Reason'}")
    print(f"  {'-'*56}")

    for r in results:
        status = f"{_GREEN}PASS{_RESET}" if r.passed else f"{_RED}FAIL{_RESET}"
        reason = r.last_reason[:40] if r.last_reason else ""
        print(f"  {r.case_id:<20} {status:<19} {r.iterations:<8} {reason}")

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    print(f"\n  {passed}/{total} cases passed")

    if passed < total:
        print(f"  {_RED}Some cases failed.{_RESET}")
    else:
        print(f"  {_GREEN}All cases passed!{_RESET}")


async def main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    cases = load_cases(args.cases)

    if args.case_id:
        cases = [c for c in cases if c.id == args.case_id]
        if not cases:
            print(f"{_RED}No case found with id: {args.case_id}{_RESET}")
            return 1

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    rlog = RunLogger(run_id)

    print(f"{_BOLD}Loop Test Runner{_RESET}")
    print(f"  Config: {args.config}")
    print(f"  Cases: {args.cases} ({len(cases)} case(s))")
    print(f"  Agent: {cfg.agent_name} @ {cfg.ws_url.format(agent_name=cfg.agent_name)}")
    print(f"  Log dir: {rlog.run_dir}")

    results: list[CaseResult] = []
    for case in cases:
        result = await run_case(cfg, case, rlog)
        results.append(result)

    print_summary(results)
    rlog.log_summary(results)
    print(f"\n  Detailed logs: {rlog.run_dir}")
    return 0 if all(r.passed for r in results) else 1


def cli():
    parser = argparse.ArgumentParser(
        description="Automated test-optimize loop for Alfred agents",
    )
    default_dir = Path(__file__).parent

    parser.add_argument(
        "--config", type=Path,
        default=default_dir / "config.yaml",
        help="Path to config.yaml (default: loop_test/config.yaml)",
    )
    parser.add_argument(
        "--cases", type=Path,
        default=default_dir / "cases.yaml",
        help="Path to cases.yaml (default: loop_test/cases.yaml)",
    )
    parser.add_argument(
        "--case-id", type=str, default=None,
        help="Run only a specific case by id",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    exit_code = asyncio.run(main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    cli()
