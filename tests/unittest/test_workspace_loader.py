"""Tests for workspace instruction loader snapshot semantics."""

from pathlib import Path

from src.everbot.infra.workspace import WorkspaceLoader


def test_workspace_loader_load_reads_known_instruction_files(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("agents", encoding="utf-8")
    (tmp_path / "SKILLS.md").write_text("skills", encoding="utf-8")
    (tmp_path / "USER.md").write_text("user", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("memory", encoding="utf-8")
    (tmp_path / "HEARTBEAT.md").write_text("heartbeat", encoding="utf-8")

    loader = WorkspaceLoader(tmp_path)
    instructions = loader.load()

    assert instructions.agents_md == "agents"
    assert instructions.skills_md == "skills"
    assert instructions.user_md == "user"
    assert instructions.memory_md == "memory"
    assert instructions.heartbeat_md == "heartbeat"


def test_workspace_loader_retries_when_snapshot_changes(monkeypatch, tmp_path: Path):
    loader = WorkspaceLoader(tmp_path)
    file_states = [
        {"AGENTS.md": (1, 10)},
        {"AGENTS.md": (2, 12)},
        {"AGENTS.md": (2, 12)},
        {"AGENTS.md": (2, 12)},
    ]
    call_count = {"read": 0}

    def fake_capture():
        return file_states.pop(0)

    def fake_read(_filename: str):
        call_count["read"] += 1
        return "content"

    monkeypatch.setattr(loader, "_capture_file_stats", fake_capture)
    monkeypatch.setattr(loader, "_read_file", fake_read)

    instructions = loader.load()

    assert instructions.agents_md == "content"
    # At least two read rounds are expected: first unstable, second stable.
    assert call_count["read"] >= len(loader.INSTRUCTION_FILES) * 2
