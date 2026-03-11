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


def test_build_system_prompt_includes_memory(tmp_path: Path):
    """MEMORY.md content should be injected into system prompt."""
    (tmp_path / "SOUL.md").write_text("I am an agent", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Memory\n\nUser prefers cm", encoding="utf-8")

    loader = WorkspaceLoader(tmp_path)
    prompt = loader.build_system_prompt()

    assert "# 记忆" in prompt
    assert "User prefers cm" in prompt


def test_build_system_prompt_truncates_large_memory(tmp_path: Path):
    """Memory exceeding MAX_MEMORY_PROMPT_CHARS gets truncated."""
    (tmp_path / "SOUL.md").write_text("I am an agent", encoding="utf-8")
    large_memory = "x" * (WorkspaceLoader.MAX_MEMORY_PROMPT_CHARS + 500)
    (tmp_path / "MEMORY.md").write_text(large_memory, encoding="utf-8")

    loader = WorkspaceLoader(tmp_path)
    prompt = loader.build_system_prompt()

    assert "记忆 (截断)" in prompt
    assert "更多记忆请读取 MEMORY.md" in prompt


def test_build_system_prompt_no_memory_when_empty(tmp_path: Path):
    """Empty MEMORY.md should not produce a memory section."""
    (tmp_path / "SOUL.md").write_text("I am an agent", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("", encoding="utf-8")

    loader = WorkspaceLoader(tmp_path)
    prompt = loader.build_system_prompt()

    assert "记忆" not in prompt
