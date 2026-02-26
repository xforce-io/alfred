"""Tests exposing memory system deduplication and quality issues.

These tests document real-world problems observed in production:
- demo_agent's MEMORY.md accumulated 5+ near-identical entries about "dolphin 长期记忆系统"
- When user asked to "review alfred project", agent went to dolphin because MEMORY.md
  was dominated by dolphin paths and contained zero alfred path references
- The memory system has no content-level dedup — it relies entirely on LLM to avoid
  duplicates, which fails in practice (especially across sessions)

Each xfail test describes the DESIRED behavior that is currently NOT implemented.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.everbot.core.memory.manager import MemoryManager
from src.everbot.core.memory.merger import MemoryMerger
from src.everbot.core.memory.models import MemoryEntry
from src.everbot.core.memory.store import MemoryStore


def _seed_memory(md_path: Path, entries_data: list[dict]) -> None:
    store = MemoryStore(md_path)
    entries = [MemoryEntry.from_dict(d) for d in entries_data]
    store.save(entries)


def _mock_llm_response(new_memories: list, reinforced_ids: list) -> str:
    return json.dumps({
        "new_memories": new_memories,
        "reinforced_ids": reinforced_ids,
    })


class TestMergerDedup:
    """Merger should reject semantically duplicate new entries."""

    @pytest.mark.xfail(reason="Merger has no content-level dedup — blindly adds all new entries")
    def test_merger_rejects_near_duplicate_content(self):
        """Real-world scenario: LLM returns a rephrased version of an existing memory.

        Production evidence (demo_agent MEMORY.md):
          - [workflow] 知识网络与长期记忆的设计文档位于 .../long_term_memory_design.md
          - [workflow] 知识网络与长期记忆系统的核心设计文档位于 .../long_term_memory_design.md
          - [workflow] 智能体长期记忆与知识压缩系统的核心设计文档位于 .../long_term_memory_design.md
        All three say essentially the same thing with slightly different wording.
        """
        merger = MemoryMerger()
        existing = [
            MemoryEntry(
                id="aaa111",
                content="知识网络与长期记忆的设计文档位于 /path/to/long_term_memory_design.md",
                category="workflow",
                score=0.8,
                created_at="2026-02-10T00:00:00+00:00",
                last_activated="2026-02-10T00:00:00+00:00",
                activation_count=3,
                source_session="s1",
            ),
        ]

        # LLM returns a rephrased near-duplicate
        new_extractions = [
            {
                "content": "知识网络与长期记忆系统的核心设计文档位于 /path/to/long_term_memory_design.md",
                "category": "workflow",
                "importance": "medium",
            },
        ]

        result = merger.merge(
            existing=existing,
            new_extractions=new_extractions,
            reinforcements=[],
            source_session="s2",
        )

        # DESIRED: should reinforce existing rather than add a near-duplicate
        assert result.new_count == 0, (
            "Near-duplicate content should be detected and merged, not added as new"
        )
        assert len(result.entries) == 1, (
            f"Expected 1 entry (reinforced), got {len(result.entries)}"
        )

    @pytest.mark.xfail(reason="Merger has no content-level dedup — blindly adds all new entries")
    def test_merger_deduplicates_within_single_extraction_batch(self):
        """LLM sometimes returns multiple entries that are near-identical in one batch."""
        merger = MemoryMerger()

        # Two semantically identical entries in a single extraction batch
        new_extractions = [
            {
                "content": "用户关注美股价值投资，特别是科技巨头如Meta",
                "category": "preference",
                "importance": "high",
            },
            {
                "content": "用户关注美股价值投资，特别是科技巨头如Meta的财务指标",
                "category": "preference",
                "importance": "high",
            },
        ]

        result = merger.merge(
            existing=[],
            new_extractions=new_extractions,
            reinforcements=[],
        )

        # DESIRED: deduplicate within the batch
        assert result.new_count == 1, (
            "Near-identical entries within a single batch should be deduplicated"
        )


class TestPromptMemoryQuality:
    """get_prompt_memories should produce high-quality, diverse output."""

    @pytest.mark.xfail(reason="get_prompt_memories has no diversity/dedup filtering")
    def test_prompt_memories_dedup_near_identical_entries(self, tmp_path: Path):
        """Real-world reproduction: demo_agent's MEMORY.md had 5 near-identical
        [workflow] entries all pointing to the same long_term_memory_design.md,
        wasting 5 of the top-20 prompt slots with redundant information."""
        md = tmp_path / "MEMORY.md"

        # Reproduce the actual demo_agent MEMORY.md content
        _seed_memory(md, [
            {"id": "w1", "content": "知识网络与长期记忆的设计文档位于 /path/to/long_term_memory_design.md，包含世界模型、经验知识和检索机制等核心内容。",
             "category": "workflow", "score": 0.8},
            {"id": "w2", "content": "知识网络与长期记忆系统的核心设计文档位于 /path/to/long_term_memory_design.md，包含世界模型、经验知识和检索机制等关键内容。",
             "category": "workflow", "score": 0.78},
            {"id": "w3", "content": "智能体长期记忆与知识压缩系统的核心设计文档位于 /path/to/long_term_memory_design.md，包含世界模型、经验知识和检索机制等关键内容。",
             "category": "workflow", "score": 0.76},
            {"id": "f1", "content": "智能体长期记忆系统由记忆提取器、LLM知识抽象、版本化存储和知识检索器构成，支持构建动态世界模型。",
             "category": "fact", "score": 0.75},
            {"id": "f2", "content": "智能体长期记忆系统通过记忆提取器、LLM知识抽象、版本化存储和知识检索器构成，支持构建动态世界模型并实现自适应学习。",
             "category": "fact", "score": 0.74},
            {"id": "f3", "content": "知识网络的核心功能包括从对话历史中自动总结、压缩和抽象出有价值的知识，并将其结构化存储以供后续调用。",
             "category": "fact", "score": 0.73},
            {"id": "f4", "content": "知识网络的核心功能是从对话历史中自动总结、压缩和抽象出有价值的知识，并将其结构化存储以供后续调用。",
             "category": "fact", "score": 0.72},
            # The actually-useful diverse memories
            {"id": "p1", "content": "用户关注投资信号和宏观流动性",
             "category": "preference", "score": 0.7},
            {"id": "p2", "content": "用户关注美股价值投资，特别是科技巨头如Meta",
             "category": "preference", "score": 0.69},
        ])

        mm = MemoryManager(md)
        result = mm.get_prompt_memories(top_k=5)

        # Count unique semantic themes in the output
        # Currently: all 5 slots wasted on dolphin/长期记忆 variations
        # DESIRED: diverse selection, at most 1-2 entries per semantic cluster
        lines = [l for l in result.split("\n") if l.startswith("- [")]
        path_refs = sum(1 for l in lines if "long_term_memory_design.md" in l)
        assert path_refs <= 1, (
            f"Expected at most 1 entry referencing the same doc path, "
            f"got {path_refs} near-duplicates consuming top-k slots"
        )

    @pytest.mark.xfail(reason="get_prompt_memories has no diversity/dedup filtering")
    def test_prompt_memories_ensure_category_diversity(self, tmp_path: Path):
        """If all top-scoring memories are the same category, we lose signal diversity.

        Real-world: demo_agent had ~15 [fact]/[workflow] entries about dolphin internals
        but only 2 [preference] entries about user interests, yet the preferences
        were arguably more useful for serving the user.
        """
        md = tmp_path / "MEMORY.md"

        entries = []
        # 15 high-scoring fact entries about the same topic
        for i in range(15):
            entries.append({
                "id": f"f{i:02d}",
                "content": f"长期记忆系统的第{i+1}个技术细节",
                "category": "fact",
                "score": 0.9 - i * 0.01,
            })
        # 3 lower-scoring but important preference entries
        entries.extend([
            {"id": "p1", "content": "用户希望每日获取投资信号报告", "category": "preference", "score": 0.7},
            {"id": "p2", "content": "用户喜欢简洁的代码风格", "category": "preference", "score": 0.68},
            {"id": "p3", "content": "用户是拜仁球迷", "category": "preference", "score": 0.65},
        ])
        _seed_memory(md, entries)

        mm = MemoryManager(md)
        result = mm.get_prompt_memories(top_k=10)
        lines = [l for l in result.split("\n") if l.startswith("- [")]

        # DESIRED: at least some preference entries should appear in top-10
        preference_count = sum(1 for l in lines if "[preference]" in l)
        assert preference_count >= 1, (
            "Prompt memories should include diverse categories; "
            "all top-10 slots were consumed by [fact] entries"
        )


class TestEndToEndDuplicateAccumulation:
    """Simulate the real-world scenario where duplicates accumulate across sessions."""

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="No cross-session dedup: LLM keeps extracting rephrased versions")
    async def test_multi_session_duplicate_accumulation(self, tmp_path: Path):
        """Reproduce the exact production bug: user discusses the same topic
        across 3 sessions, and each session's LLM extraction adds a slightly
        different phrasing of the same memory.

        This is the root cause of demo_agent having 5+ duplicate entries.
        The LLM prompt says "avoid duplicates" and passes existing memories,
        but in practice the LLM still returns rephrased near-duplicates.
        """
        md = tmp_path / "MEMORY.md"
        ctx = MagicMock()
        config = MagicMock()
        config.fast_llm = "test-model"
        ctx.get_config.return_value = config

        # Session 1: first discussion about memory system
        s1_response = _mock_llm_response(
            new_memories=[{
                "content": "知识网络与长期记忆的设计文档位于 /path/to/doc.md",
                "category": "workflow",
                "importance": "high",
            }],
            reinforced_ids=[],
        )

        with patch(
            "src.everbot.core.memory.extractor.MemoryExtractor._call_llm",
            new_callable=AsyncMock,
            return_value=s1_response,
        ):
            mm = MemoryManager(md, ctx)
            await mm.process_session_end(
                [{"role": "user", "content": "看看记忆系统设计文档"}],
                "session_1",
            )

        entries_s1 = MemoryStore(md).load()
        assert len(entries_s1) == 1
        original_id = entries_s1[0].id

        # Session 2: LLM returns a rephrased version instead of reinforcing
        # (This is what actually happens in production)
        s2_response = _mock_llm_response(
            new_memories=[{
                "content": "知识网络与长期记忆系统的核心设计文档位于 /path/to/doc.md",
                "category": "workflow",
                "importance": "medium",
            }],
            reinforced_ids=[],  # LLM failed to recognize it as a reinforcement
        )

        with patch(
            "src.everbot.core.memory.extractor.MemoryExtractor._call_llm",
            new_callable=AsyncMock,
            return_value=s2_response,
        ):
            mm = MemoryManager(md, ctx)
            await mm.process_session_end(
                [{"role": "user", "content": "再看看那个文档"}],
                "session_2",
            )

        # Session 3: yet another rephrased version
        s3_response = _mock_llm_response(
            new_memories=[{
                "content": "智能体长期记忆与知识压缩系统的设计文档位于 /path/to/doc.md",
                "category": "workflow",
                "importance": "medium",
            }],
            reinforced_ids=[],
        )

        with patch(
            "src.everbot.core.memory.extractor.MemoryExtractor._call_llm",
            new_callable=AsyncMock,
            return_value=s3_response,
        ):
            mm = MemoryManager(md, ctx)
            await mm.process_session_end(
                [{"role": "user", "content": "知识压缩怎么做的"}],
                "session_3",
            )

        final_entries = MemoryStore(md).load()

        # DESIRED: all three should collapse into one reinforced entry
        assert len(final_entries) == 1, (
            f"Expected 1 entry (3 sessions reinforcing the same fact), "
            f"got {len(final_entries)} entries. Contents:\n"
            + "\n".join(f"  - {e.content}" for e in final_entries)
        )
        # The surviving entry should have been reinforced
        surviving = final_entries[0]
        assert surviving.activation_count >= 3, (
            "The single entry should have been reinforced across sessions"
        )


class TestMergerContentSimilarity:
    """Test that merger can detect content similarity (when implemented)."""

    @pytest.mark.xfail(reason="No similarity detection in merger")
    def test_high_token_overlap_detected_as_duplicate(self):
        """Two entries sharing >70% tokens about the same path should be considered duplicates."""
        merger = MemoryMerger()
        existing = [
            MemoryEntry(
                id="e1",
                content="用户关注投资信号的自动化推送，希望每日获取结构化市场分析报告，特别是美股价值投资相关数据",
                category="preference",
                score=0.8,
                created_at="2026-02-10T00:00:00+00:00",
                last_activated="2026-02-10T00:00:00+00:00",
                activation_count=2,
                source_session="s1",
            ),
        ]

        new_extractions = [
            {
                "content": "用户对自动化信息推送有明确需求，希望每日获取结构化市场分析报告，特别是美股价值投资相关数据",
                "category": "preference",
                "importance": "medium",
            },
        ]

        result = merger.merge(
            existing=existing,
            new_extractions=new_extractions,
            reinforcements=[],
        )

        # DESIRED: detected as duplicate, should reinforce instead of adding
        assert result.new_count == 0
        assert result.updated_count == 1
        assert len(result.entries) == 1
        assert result.entries[0].activation_count == 3  # reinforced
