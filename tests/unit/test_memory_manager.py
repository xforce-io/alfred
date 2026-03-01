"""Tests for MemoryManager — end-to-end with mocked LLM."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.everbot.core.memory.manager import MemoryManager
from src.everbot.core.memory.models import MemoryEntry
from src.everbot.core.memory.store import MemoryStore


def _seed_memory(md_path: Path, entries_data: list[dict]) -> None:
    """Write seed entries directly to MEMORY.md."""
    store = MemoryStore(md_path)
    entries = [MemoryEntry.from_dict(d) for d in entries_data]
    store.save(entries)


def _mock_llm_response(new_memories: list, reinforced_ids: list) -> str:
    return json.dumps({
        "new_memories": new_memories,
        "reinforced_ids": reinforced_ids,
    })


class TestProcessSessionEnd:
    """End-to-end memory lifecycle with mocked LLM."""

    @pytest.mark.asyncio
    async def test_extracts_new_memories(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        ctx = MagicMock()
        config = MagicMock()
        config.fast_llm = "test-model"
        ctx.get_config.return_value = config

        llm_response = _mock_llm_response(
            new_memories=[
                {"content": "用户喜欢 Python", "category": "preference", "importance": "high"},
                {"content": "使用 VS Code", "category": "fact", "importance": "medium"},
            ],
            reinforced_ids=[],
        )

        with patch("src.everbot.core.memory.extractor.MemoryExtractor._call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            mm = MemoryManager(md, ctx)
            messages = [
                {"role": "user", "content": "我喜欢 Python"},
                {"role": "assistant", "content": "好的"},
            ]
            stats = await mm.process_session_end(messages, "session_001")

        assert stats["new_count"] == 2
        assert stats["total"] == 2

        # Verify persisted
        entries = MemoryStore(md).load()
        assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_reinforces_existing_memories(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        _seed_memory(md, [
            {"id": "aaa111", "content": "用户喜欢简洁代码", "category": "preference", "score": 0.7, "activation_count": 3},
        ])

        ctx = MagicMock()
        config = MagicMock()
        config.fast_llm = "test-model"
        ctx.get_config.return_value = config

        llm_response = _mock_llm_response(
            new_memories=[],
            reinforced_ids=["aaa111"],
        )

        with patch("src.everbot.core.memory.extractor.MemoryExtractor._call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            mm = MemoryManager(md, ctx)
            stats = await mm.process_session_end(
                [{"role": "user", "content": "简洁最重要"}], "session_002"
            )

        assert stats["updated_count"] == 1
        entries = MemoryStore(md).load()
        reinforced = next(e for e in entries if e.id == "aaa111")
        assert reinforced.score > 0.7
        assert reinforced.activation_count == 4

    @pytest.mark.asyncio
    async def test_no_context_skips_extraction(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        mm = MemoryManager(md, context=None)
        stats = await mm.process_session_end(
            [{"role": "user", "content": "hello"}], "s1"
        )
        assert stats["new_count"] == 0


class TestIncrementalExtraction:
    """Verify that consecutive save_session calls only extract from new messages."""

    @pytest.mark.asyncio
    async def test_second_call_only_sends_new_messages_to_llm(self, tmp_path: Path):
        """Bug repro: full history is sent every time, causing LLM to re-extract
        from already-processed messages and create near-duplicate entries."""
        md = tmp_path / "MEMORY.md"
        ctx = MagicMock()
        config = MagicMock()
        config.fast_llm = "test-model"
        ctx.get_config.return_value = config

        # -- Round 1: 4 messages --
        round1_messages = [
            {"role": "user", "content": "我是拜仁球迷"},
            {"role": "assistant", "content": "好的，我知道了"},
            {"role": "user", "content": "帮我查下最新转会新闻"},
            {"role": "assistant", "content": "迪亚斯从利物浦转会拜仁"},
        ]
        round1_response = _mock_llm_response(
            new_memories=[
                {"content": "用户是拜仁慕尼黑球迷", "category": "preference", "importance": "high"},
            ],
            reinforced_ids=[],
        )

        captured_prompts = []

        async def _capture_llm(self_extractor, prompt):
            captured_prompts.append(prompt)
            if len(captured_prompts) == 1:
                return round1_response
            return round2_response

        with patch("src.everbot.core.memory.extractor.MemoryExtractor._call_llm", new=_capture_llm):
            mm = MemoryManager(md, ctx)
            await mm.process_session_end(round1_messages, "session_001")

        entries_after_r1 = MemoryStore(md).load()
        assert len(entries_after_r1) == 1

        # -- Round 2: same 4 messages + 2 new ones (cumulative history) --
        round2_messages = round1_messages + [
            {"role": "user", "content": "凯恩这赛季进了多少球"},
            {"role": "assistant", "content": "凯恩目前打进26球"},
        ]
        round2_response = _mock_llm_response(
            new_memories=[
                {"content": "用户关注球员进球数据", "category": "preference", "importance": "medium"},
            ],
            reinforced_ids=[entries_after_r1[0].id],
        )

        with patch("src.everbot.core.memory.extractor.MemoryExtractor._call_llm", new=_capture_llm):
            mm = MemoryManager(md, ctx)
            await mm.process_session_end(round2_messages, "session_001")

        # The LLM should only receive the 2 NEW messages, not all 6
        assert len(captured_prompts) == 2
        second_prompt = captured_prompts[1]
        assert "我是拜仁球迷" not in second_prompt, \
            "Round-1 messages should NOT appear in round-2 LLM prompt"
        assert "凯恩这赛季进了多少球" in second_prompt, \
            "Round-2 new messages SHOULD appear in LLM prompt"

        # Should have 2 entries total, not duplicates
        final_entries = MemoryStore(md).load()
        assert len(final_entries) == 2

    @pytest.mark.asyncio
    async def test_first_call_processes_all_messages(self, tmp_path: Path):
        """On first run (no prior extraction), all messages should be sent."""
        md = tmp_path / "MEMORY.md"
        ctx = MagicMock()
        config = MagicMock()
        config.fast_llm = "test-model"
        ctx.get_config.return_value = config

        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        response = _mock_llm_response(new_memories=[], reinforced_ids=[])
        captured_prompts = []

        async def _capture_llm(self_extractor, prompt):
            captured_prompts.append(prompt)
            return response

        with patch("src.everbot.core.memory.extractor.MemoryExtractor._call_llm", new=_capture_llm):
            mm = MemoryManager(md, ctx)
            await mm.process_session_end(messages, "s1")

        assert len(captured_prompts) == 1
        assert "你好" in captured_prompts[0]


class TestGetPromptMemories:
    """Prompt injection formatting."""

    def test_returns_formatted_text(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        _seed_memory(md, [
            {"id": "a1", "content": "喜欢 Python", "category": "preference", "score": 0.9},
            {"id": "a2", "content": "用 VS Code", "category": "fact", "score": 0.7},
            {"id": "a3", "content": "旧信息", "category": "fact", "score": 0.3},  # Below 0.5
        ])

        mm = MemoryManager(md)
        result = mm.get_prompt_memories()
        assert "喜欢 Python" in result
        assert "用 VS Code" in result
        assert "旧信息" not in result  # score < 0.5

    def test_respects_top_k(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        # Use distinct content per entry so greedy dedup doesn't collapse them
        topics = [
            "用户喜欢使用 Python 编程语言进行开发",
            "用户在工作中频繁使用 Docker 容器技术",
            "用户关注美股价值投资和宏观经济数据",
            "用户偏好使用 VS Code 作为主力编辑器",
            "用户习惯用 Git 进行版本控制和分支管理",
            "用户对 AI 论文发现和分析有明确需求",
            "用户重视代码审查流程和工程质量",
            "用户在本地环境维护多个自动化技能模块",
            "用户对信息冗余敏感，偏好简洁输出",
            "用户关注地缘政治事件和全球宏观风险",
        ]
        entries = [
            {"id": f"e{i}", "content": topics[i % len(topics)] + f" ({i})", "category": "fact", "score": 0.9 - i * 0.01}
            for i in range(30)
        ]
        _seed_memory(md, entries)

        mm = MemoryManager(md)
        result = mm.get_prompt_memories(top_k=5)
        # Should contain at most 5 entries
        assert result.count("- [") == 5

    def test_empty_memory_returns_empty(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        mm = MemoryManager(md)
        assert mm.get_prompt_memories() == ""

    def test_all_low_score_returns_empty(self, tmp_path: Path):
        md = tmp_path / "MEMORY.md"
        _seed_memory(md, [
            {"id": "lo", "content": "低分记忆", "category": "fact", "score": 0.3},
        ])
        mm = MemoryManager(md)
        assert mm.get_prompt_memories() == ""
