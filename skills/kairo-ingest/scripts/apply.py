#!/usr/bin/env python3
"""Apply a confirmed kairo-ingest plan: add/title/attach then step. Never new/run."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REF_PLACEHOLDER = "{ref_id}"
SEEN_FILE = Path(".kairo") / "kairo-ingest-seen.json"


def kairo_bin() -> str:
    return os.environ.get("KAIRO_REAL_BIN") or str(Path.home() / ".local/bin/kairo")


def build_actions(plan: dict, *, step: bool = True) -> list[dict]:
    root = Path(plan["root"])
    actions: list[dict] = []
    stepped: list[str] = []
    for item in plan.get("items") or []:
        if item.get("action") == "skip":
            continue
        topic = item.get("topic")
        if not topic:
            continue
        if item.get("action") not in (None, "add"):
            continue
        forms = item.get("forms") or []
        if not forms:
            continue
        cwd = str(root / topic)
        primary, *rest = forms
        add_args = ["add", primary["path"]]
        if primary.get("copy"):
            add_args.append("--copy")
        add_args.extend(["--occurred", item["occurred"]])
        actions.append(
            {"kind": "add", "cwd": cwd, "args": add_args, "title": item["title"]}
        )
        actions.append(
            {
                "kind": "title",
                "cwd": cwd,
                "args": ["title", REF_PLACEHOLDER, item["title"]],
                "title": item["title"],
            }
        )
        for form in rest:
            attach_args = ["add", form["path"], "--to", REF_PLACEHOLDER]
            if form.get("copy"):
                attach_args.append("--copy")
            actions.append(
                {
                    "kind": "attach",
                    "cwd": cwd,
                    "args": attach_args,
                    "title": item["title"],
                }
            )
        if topic not in stepped:
            stepped.append(topic)
    if step:
        for topic in stepped:
            actions.append(
                {
                    "kind": "step",
                    "cwd": str(root / topic),
                    "args": ["step"],
                    "title": topic,
                }
            )
    return actions


def record_seen(root: Path, title: str, paths: list[str]) -> None:
    seen_path = root / SEEN_FILE
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"titles": [], "basenames": [], "paths": []}
    if seen_path.is_file():
        try:
            loaded = json.loads(seen_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            pass
    titles = {str(t) for t in (data.get("titles") or []) if t}
    if title:
        titles.add(title)
    basenames = {str(n) for n in (data.get("basenames") or []) if n}
    stored_paths = {str(p) for p in (data.get("paths") or []) if p}
    for path in paths:
        stored_paths.add(path)
        basenames.add(Path(path).name)
    payload = {
        "titles": sorted(titles),
        "basenames": sorted(basenames),
        "paths": sorted(stored_paths),
    }
    seen_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_added(stdout: str) -> str:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("added "):
            return line.split()[1].rstrip(",")
    raise RuntimeError(f"kairo add produced no ref id:\n{stdout}")


def _fill_ref(args: list[str], ref_id: str | None, *, dry_run: bool = False) -> list[str]:
    out: list[str] = []
    for part in args:
        if part == REF_PLACEHOLDER:
            if ref_id:
                out.append(ref_id)
            elif dry_run:
                out.append(REF_PLACEHOLDER)
            else:
                raise RuntimeError("ref id missing for title/attach")
        else:
            out.append(part)
    return out


def run_actions(
    actions: list[dict],
    *,
    binary: str,
    dry_run: bool,
    root: Path | None = None,
) -> dict:
    added: list[dict] = []
    stepped: list[dict] = []
    failed: list[dict] = []
    ref_by_title: dict[str, str] = {}
    for action in actions:
        args = _fill_ref(
            action["args"],
            ref_by_title.get(action["title"]),
            dry_run=dry_run,
        )
        record = {
            "kind": action["kind"],
            "cwd": action["cwd"],
            "args": args,
            "title": action["title"],
        }
        if dry_run:
            if action["kind"] == "add":
                added.append(record)
            elif action["kind"] == "step":
                stepped.append(record)
            continue
        result = subprocess.run(
            [binary, *args],
            cwd=action["cwd"],
            capture_output=True,
            text=True,
            check=False,
        )
        record["stdout"] = (result.stdout or "").strip()
        record["stderr"] = (result.stderr or "").strip()
        record["returncode"] = result.returncode
        if result.returncode != 0:
            failed.append(record)
            break
        if action["kind"] == "add":
            ref_id = parse_added(result.stdout)
            ref_by_title[action["title"]] = ref_id
            record["ref_id"] = ref_id
            added.append(record)
            if root is not None:
                record_seen(root, action["title"], [args[1]])
        elif action["kind"] == "attach":
            if root is not None:
                record_seen(root, action["title"], [args[1]])
        elif action["kind"] == "step":
            stepped.append(record)
    return {"added": added, "stepped": stepped, "failed": failed, "dry_run": dry_run}


def format_receipt(plan: dict, result: dict) -> str:
    lines = ["kairo-ingest 完成" if not result.get("dry_run") else "kairo-ingest 预览（未执行）"]
    pending = [
        item
        for item in plan.get("items") or []
        if item.get("action") != "skip" and not item.get("topic")
    ]
    skipped = [
        item["title"]
        for item in plan.get("items") or []
        if item.get("action") == "skip"
    ]
    for rec in result.get("added") or []:
        rid = rec.get("ref_id") or REF_PLACEHOLDER
        lines.append(f"- add {rec['title']} → {Path(rec['cwd']).name} ref {rid}")
    for rec in result.get("stepped") or []:
        extra = rec.get("stdout") or ""
        suffix = f" ({extra})" if extra else ""
        lines.append(f"- step {Path(rec['cwd']).name}{suffix}")
    if pending:
        for item in pending:
            hint = item.get("topic_hint") or "无"
            lines.append(f"- 待指定 {item['title']} 推荐={hint}")
    if skipped:
        lines.append(f"- 已跳过已入库 {len(skipped)} 条")
    for rec in result.get("failed") or []:
        err = rec.get("stderr") or rec.get("stdout") or "failed"
        lines.append(f"- 失败 {rec['kind']} {rec['title']}: {err}")
    if not result.get("added") and not result.get("stepped") and not result.get("failed"):
        lines.append("- 无已确认可执行条目")
    return "\n".join(lines)


def load_plan(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply a confirmed kairo-ingest plan")
    parser.add_argument("--plan", required=True, help="JSON plan from scan.py, after user edits")
    parser.add_argument("--kairo-bin", default=kairo_bin())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-step",
        action="store_true",
        help="Register refs only; skip kairo step (ASR/compose can exceed job turn timeout)",
    )
    args = parser.parse_args(argv)
    plan = load_plan(Path(args.plan).expanduser())
    actions = build_actions(plan, step=not args.no_step)
    result = run_actions(
        actions,
        binary=args.kairo_bin,
        dry_run=args.dry_run,
        root=None if args.dry_run else Path(plan["root"]),
    )
    receipt = format_receipt(plan, result)
    print(receipt)
    print("---")
    json.dump({"ok": not result["failed"], **result}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
