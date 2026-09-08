#!/usr/bin/env python3
"""Scan Voice Memos and Downloads for XXX-YYMMDD items; recommend existing Kairo topics."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

STEM_RE = re.compile(r"^(.+)-(\d{6})$")
STEM8_RE = re.compile(r"^(.+)-(\d{8})$")
DEFAULT_VOICE_RE = re.compile(r"^新录音")
AUDIO_EXT = {".m4a", ".wav", ".mp3", ".aac", ".caf"}
SEEN_FILE = Path(".kairo") / "kairo-ingest-seen.json"
NOTIFIED_FILE = Path(".kairo") / "kairo-ingest-notified.json"
SILENT_TOKEN = "NO_USER_MESSAGE"
DEFAULT_VOICE_DIR = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.com.apple.VoiceMemos.shared"
    / "Recordings"
)


def kairo_bin() -> str:
    return os.environ.get("KAIRO_REAL_BIN") or str(Path.home() / ".local/bin/kairo")


def parse_stem(stem: str) -> dict | None:
    stem = (stem or "").strip()
    if not stem or DEFAULT_VOICE_RE.match(stem):
        return None
    matched = STEM_RE.match(stem)
    if not matched:
        return None
    xxx, yymmdd = matched.group(1).strip(), matched.group(2)
    if not xxx or DEFAULT_VOICE_RE.match(xxx):
        return None
    occurred = _occurred(yymmdd)
    if not occurred:
        return None
    return {
        "xxx": xxx,
        "title": f"{xxx}-{yymmdd}",
        "yymmdd": yymmdd,
        "occurred": occurred,
    }


def canonical_title(stem: str) -> str | None:
    """Map XXX-YYMMDD / XXX_YYMMDD / XXX-YYYYMMDD onto XXX-YYMMDD."""
    text = (stem or "").strip().replace("_", "-")
    if not text:
        return None
    matched8 = STEM8_RE.match(text)
    if matched8:
        ymd = matched8.group(2)
        parsed = parse_stem(f"{matched8.group(1)}-{ymd[2:]}")
        return parsed["title"] if parsed else None
    parsed = parse_stem(text)
    return parsed["title"] if parsed else None


def _occurred(yymmdd: str) -> str | None:
    year = 2000 + int(yymmdd[:2])
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    try:
        dt.date(year, month, day)
    except ValueError:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def recommend_topic(xxx: str, topics: list[dict]) -> dict:
    xxx = (xxx or "").strip()
    exact: list[str] = []
    for topic in topics:
        slug = topic["slug"]
        label = topic.get("topic") or slug
        if xxx == slug or xxx == label:
            exact.append(slug)
    if len(exact) == 1:
        return {"status": "matched", "topic": exact[0], "candidates": exact}
    if len(exact) > 1:
        return {"status": "unspecified", "topic": None, "candidates": exact}

    scored: list[tuple[int, str]] = []
    for topic in topics:
        slug = topic["slug"]
        label = topic.get("topic") or slug
        lengths: list[int] = []
        if slug and slug in xxx:
            lengths.append(len(slug))
        if label and label != slug and label in xxx:
            lengths.append(len(label))
        if lengths:
            scored.append((max(lengths), slug))
    if not scored:
        return {"status": "unspecified", "topic": None, "candidates": []}
    best = max(length for length, _ in scored)
    winners = sorted({slug for length, slug in scored if length == best})
    if len(winners) == 1:
        return {"status": "matched", "topic": winners[0], "candidates": winners}
    return {"status": "unspecified", "topic": None, "candidates": winners}


_MIN_HINT_FRAG = 2
_HINT_STOP = frozenset(
    {"讨论", "沟通", "例会", "准备", "部分", "计划", "逻辑", "产品", "系统"}
)


def fuzzy_topic_hint(xxx: str, topics: list[dict]) -> tuple[str | None, list[str]]:
    """Best existing-topic guess when slug is not a substring of XXX.

    Score = longest fragment of a topic name that appears in XXX; ties go
    to the earlier occurrence in XXX. Never invents a new topic.
    """
    xxx = (xxx or "").strip()
    scored: list[tuple[int, int, str]] = []
    for topic in topics:
        slug = topic["slug"]
        names = {slug, topic.get("topic") or slug}
        best: tuple[int, int, str] | None = None
        for name in names:
            if not name:
                continue
            for length in range(len(name), _MIN_HINT_FRAG - 1, -1):
                hit = False
                for start in range(len(name) - length + 1):
                    frag = name[start : start + length]
                    if frag in _HINT_STOP:
                        continue
                    pos = xxx.find(frag)
                    if pos >= 0:
                        cand = (length, -pos, slug)
                        if best is None or cand[:2] > best[:2]:
                            best = cand
                        hit = True
                        break
                if hit:
                    break
        if best is not None:
            scored.append(best)
    if not scored:
        return None, []
    scored.sort(reverse=True)
    best_len = scored[0][0]
    winners: list[str] = []
    seen: set[str] = set()
    for length, _negpos, slug in scored:
        if length != best_len:
            continue
        if slug not in seen:
            winners.append(slug)
            seen.add(slug)
    return winners[0], winners


def collect_downloads(folder: Path) -> list[dict]:
    rows: list[dict] = []
    if not folder.is_dir():
        return rows
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        parsed = parse_stem(path.stem)
        if not parsed:
            continue
        rows.append(
            {
                **parsed,
                "path": str(path.resolve()),
                "source": "downloads",
                "copy": False,
            }
        )
    return rows


def collect_voice_memos(recordings_dir: Path) -> list[dict]:
    rows: list[dict] = []
    if not recordings_dir.is_dir():
        return rows
    for label, rel in _voice_memo_rows(recordings_dir):
        parsed = parse_stem(label)
        if not parsed:
            continue
        path = recordings_dir / rel
        if not path.is_file():
            continue
        rows.append(
            {
                **parsed,
                "path": str(path.resolve()),
                "source": "voice-memo",
                "copy": True,
            }
        )
    return rows


def _voice_memo_rows(recordings_dir: Path) -> list[tuple[str, str]]:
    db = recordings_dir / "CloudRecordings.db"
    if not db.is_file():
        return []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for name in ("CloudRecordings.db", "CloudRecordings.db-wal", "CloudRecordings.db-shm"):
            src = recordings_dir / name
            if src.exists():
                shutil.copy2(src, tmp_path / name)
        conn = sqlite3.connect(str(tmp_path / "CloudRecordings.db"))
        try:
            cur = conn.execute(
                "SELECT COALESCE(ZCUSTOMLABELFORSORTING, ZENCRYPTEDTITLE, ''), "
                "COALESCE(ZPATH, '') FROM ZCLOUDRECORDING"
            )
            out: list[tuple[str, str]] = []
            for label, rel in cur.fetchall():
                label = (label or "").strip()
                rel = (rel or "").strip()
                if label and rel:
                    out.append((label, rel))
            return out
        finally:
            conn.close()


def _add_title(titles: set[str], raw: str) -> None:
    raw = (raw or "").strip()
    if not raw:
        return
    titles.add(raw)
    canon = canonical_title(raw)
    if canon:
        titles.add(canon)


def load_seen_ledger(root: Path) -> tuple[set[str], set[str], set[str]]:
    titles: set[str] = set()
    basenames: set[str] = set()
    paths: set[str] = set()
    seen_path = root / SEEN_FILE
    if not seen_path.is_file():
        return titles, basenames, paths
    try:
        data = json.loads(seen_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return titles, basenames, paths
    for title in data.get("titles") or []:
        _add_title(titles, str(title))
    for name in data.get("basenames") or []:
        if name:
            basenames.add(str(name))
    for path in data.get("paths") or []:
        if path:
            paths.add(str(path))
    return titles, basenames, paths


def load_existing(root: Path) -> tuple[set[str], set[str], set[str]]:
    titles: set[str] = set()
    basenames: set[str] = set()
    paths: set[str] = set()
    if not root.is_dir():
        return titles, basenames, paths
    for manifest in root.glob("*/references/*/manifest.yaml"):
        text = manifest.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("title:"):
                _add_title(titles, line.split(":", 1)[1].strip().strip("'\""))
            stripped = line.strip()
            if "location:" in stripped:
                loc = stripped.split("location:", 1)[1].strip()
                if loc:
                    basenames.add(Path(loc).name)
                    paths.add(loc)
    led_titles, led_bases, led_paths = load_seen_ledger(root)
    titles |= led_titles
    basenames |= led_bases
    paths |= led_paths
    return titles, basenames, paths


def merge_items(
    rows: list[dict],
    topics: list[dict],
    existing_titles: set[str],
    existing_basenames: set[str],
    existing_paths: set[str] | None = None,
) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["title"], []).append(row)

    known_titles = set(existing_titles)
    for raw in existing_titles:
        canon = canonical_title(raw)
        if canon:
            known_titles.add(canon)
    known_paths = set(existing_paths or ())

    items: list[dict] = []
    for title, forms in grouped.items():
        rec = recommend_topic(forms[0]["xxx"], topics)
        hint, hint_candidates = fuzzy_topic_hint(forms[0]["xxx"], topics)
        if rec["status"] == "matched":
            topic_hint = rec["topic"]
            hint_candidates = rec["candidates"]
        else:
            topic_hint = hint
            if rec["candidates"] and not hint_candidates:
                hint_candidates = rec["candidates"]
        names = {Path(form["path"]).name for form in forms}
        form_paths = {form["path"] for form in forms}
        already = (
            title in known_titles
            or bool(names & existing_basenames)
            or bool(form_paths & known_paths)
        )
        forms_sorted = sorted(
            forms,
            key=lambda form: (
                0 if Path(form["path"]).suffix.lower() in AUDIO_EXT else 1,
                0 if form["source"] == "voice-memo" else 1,
            ),
        )
        items.append(
            {
                "title": title,
                "xxx": forms[0]["xxx"],
                "occurred": forms[0]["occurred"],
                "topic_status": rec["status"],
                "topic": rec["topic"],
                "candidates": rec["candidates"],
                "topic_hint": topic_hint,
                "hint_candidates": hint_candidates,
                "action": "skip" if already else "add",
                "skip_reason": "already-ingested" if already else None,
                "forms": [
                    {
                        "path": form["path"],
                        "source": form["source"],
                        "copy": bool(form["copy"]),
                    }
                    for form in forms_sorted
                ],
            }
        )
    items.sort(key=lambda item: (item["occurred"], item["title"]))
    return items


def list_topics(root: Path, binary: str) -> list[dict]:
    result = subprocess.run(
        [binary, "list", "--json", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "kairo list failed")
    data = json.loads(result.stdout)
    if isinstance(data, list):
        return data
    return data.get("topics") or data.get("workspaces") or []


def scan(
    root: Path,
    downloads: Path,
    recordings: Path,
    binary: str | None = None,
) -> dict:
    binary = binary or kairo_bin()
    topics = list_topics(root, binary)
    titles, basenames, paths = load_existing(root)
    rows = collect_downloads(downloads) + collect_voice_memos(recordings)
    return {
        "root": str(root),
        "topics": [{"slug": t["slug"], "topic": t.get("topic") or t["slug"]} for t in topics],
        "items": merge_items(rows, topics, titles, basenames, paths),
    }


def load_notified(root: Path) -> set[str]:
    path = root / NOTIFIED_FILE
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    titles = data.get("titles") if isinstance(data, dict) else data
    return {str(t) for t in (titles or []) if t}


def save_notified(root: Path, titles: set[str]) -> None:
    path = root / NOTIFIED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"titles": sorted(titles)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def new_import_items(items: list[dict], notified: set[str] | None = None) -> list[dict]:
    """Items that still need import (not yet in Kairo). ``notified`` is ignored."""
    del notified
    return [item for item in items if item.get("action") == "add"]


def format_import_report(items: list[dict]) -> str:
    """User-facing message: only items that need import, newest first."""
    items = sorted(
        items,
        key=lambda item: (item.get("occurred") or "", item.get("title") or ""),
        reverse=True,
    )
    matched = [i for i in items if i.get("topic")]
    pending = [i for i in items if not i.get("topic")]
    lines = ["kairo-ingest 待导入"]
    if matched:
        lines.append("将自动入库:")
        for item in matched:
            lines.append(f"- {item['title']} → {item['topic']}")
    if pending:
        lines.append("待指定（请确认推荐 Topic 或改选/跳过）:")
        for index, item in enumerate(pending, 1):
            hint = item.get("topic_hint") or "无"
            alts = [
                c
                for c in (item.get("hint_candidates") or [])
                if c and c != item.get("topic_hint")
            ]
            extra = f" 备选={','.join(alts)}" if alts else ""
            lines.append(f"{index}. {item['title']}  推荐={hint}{extra}")
    return "\n".join(lines)


def format_scan_report(payload: dict) -> str:
    items = payload.get("items") or []
    matched = [i for i in items if i.get("action") == "add" and i.get("topic")]
    pending = [i for i in items if i.get("action") == "add" and not i.get("topic")]
    skipped = [i for i in items if i.get("action") == "skip"]
    lines = [
        "kairo-ingest 扫描",
        f"- 已匹配可入库: {len(matched)} 条",
        f"- 已跳过已入库: {len(skipped)} 条",
        f"- 待指定: {len(pending)} 条（不自动 add，下面每条都有推荐 Topic）",
    ]
    if matched:
        lines.append("已匹配:")
        for item in matched:
            lines.append(f"- {item['title']} → {item['topic']}")
    if pending:
        pending = sorted(
            pending,
            key=lambda item: (item.get("occurred") or "", item.get("title") or ""),
            reverse=True,
        )
        lines.append("待指定（新→旧；推荐来自已有 Topic，请确认或改选/跳过）:")
        for index, item in enumerate(pending, 1):
            hint = item.get("topic_hint") or "无"
            alts = [
                c
                for c in (item.get("hint_candidates") or [])
                if c and c != item.get("topic_hint")
            ]
            extra = f" 备选={','.join(alts)}" if alts else ""
            lines.append(f"{index}. {item['title']}  推荐={hint}{extra}")
    if not matched and not pending:
        lines.append("无待处理条目")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan XXX-YYMMDD materials for Kairo ingest")
    parser.add_argument("--root", default=str(Path.home() / "kairo"))
    parser.add_argument("--downloads", default=str(Path.home() / "Downloads"))
    parser.add_argument("--voice-memos", default=str(DEFAULT_VOICE_DIR))
    parser.add_argument("--kairo-bin", default=kairo_bin())
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument(
        "--only-new",
        action="store_true",
        help="Only items that need import and were not previously notified",
    )
    parser.add_argument(
        "--mark-notified",
        action="store_true",
        help="Record listed pending titles so later --only-new stays silent",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser()
    try:
        payload = scan(
            root,
            Path(args.downloads).expanduser(),
            Path(args.voice_memos).expanduser(),
            args.kairo_bin,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    items = payload.get("items") or []
    if args.only_new:
        notified = load_notified(root)
        items = new_import_items(items, notified)
        payload = {**payload, "items": items}
        if args.mark_notified:
            pending_titles = {i["title"] for i in items if not i.get("topic")}
            save_notified(root, notified | pending_titles)
        if not items:
            sys.stdout.write(SILENT_TOKEN + "\n")
            return 0
        sys.stdout.write(format_import_report(items) + "\n")
        return 0
    if args.format == "text":
        sys.stdout.write(format_scan_report(payload) + "\n")
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
