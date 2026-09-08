"""Unit tests for skills/kairo-ingest (XXX-YYMMDD scan + topic recommend)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path("skills/kairo-ingest/scripts")


def _load(name: str) -> ModuleType:
    path = (SCRIPTS / f"{name}.py").resolve()
    spec = importlib.util.spec_from_file_location(f"kairo_ingest_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scan():
    return _load("scan")


@pytest.fixture(scope="module")
def apply():
    return _load("apply")


TOPICS = [
    {"slug": "算法例会", "topic": "算法例会"},
    {"slug": "每周讨论", "topic": "每周讨论"},
    {"slug": "组织架构讨论", "topic": "组织架构讨论"},
    {"slug": "能源梳理", "topic": "能源梳理"},
    {"slug": "康医通", "topic": "康医通三智能体与营养处方落地"},
    {"slug": "康医通产品逻辑", "topic": "康医通产品逻辑"},
    {"slug": "ai-native", "topic": "ai-native"},
    {"slug": "流程质量", "topic": "流程质量"},
    {"slug": "未分类", "topic": "未分类"},
    {"slug": "医院机器人调度", "topic": "医院机器人调度"},
    {"slug": "机器人业务", "topic": "机器人业务"},
]


def test_parse_stem_accepts_xxx_yymmdd(scan):
    parsed = scan.parse_stem("算法例会-260904")
    assert parsed["xxx"] == "算法例会"
    assert parsed["title"] == "算法例会-260904"
    assert parsed["occurred"] == "2026-09-04"


def test_parse_stem_rejects_default_voice_titles(scan):
    assert scan.parse_stem("新录音") is None
    assert scan.parse_stem("新录音 12") is None
    assert scan.parse_stem("新录音副本") is None


def test_parse_stem_rejects_invalid_calendar(scan):
    assert scan.parse_stem("产品介绍-202306") is None  # month 23
    assert scan.parse_stem("例会-260231") is None


def test_parse_stem_keeps_hyphens_in_xxx(scan):
    parsed = scan.parse_stem("ai-native topic 讨论-260902")
    assert parsed["xxx"] == "ai-native topic 讨论"
    assert parsed["occurred"] == "2026-09-02"


def test_recommend_topic_exact_slug(scan):
    rec = scan.recommend_topic("算法例会", TOPICS)
    assert rec["status"] == "matched"
    assert rec["topic"] == "算法例会"


def test_recommend_topic_longest_substring(scan):
    rec = scan.recommend_topic("总体组织架构讨论", TOPICS)
    assert rec["status"] == "matched"
    assert rec["topic"] == "组织架构讨论"


def test_recommend_topic_exact_beats_shorter_prefix(scan):
    rec = scan.recommend_topic("康医通", TOPICS)
    assert rec["topic"] == "康医通"
    rec = scan.recommend_topic("康医通产品逻辑", TOPICS)
    assert rec["topic"] == "康医通产品逻辑"


def test_recommend_topic_unspecified_when_none(scan):
    rec = scan.recommend_topic("传奇沟通", TOPICS)
    assert rec["status"] == "unspecified"
    assert rec["topic"] is None


def test_recommend_topic_unspecified_when_tied(scan):
    rec = scan.recommend_topic(
        "组织架构",
        [
            {"slug": "组织", "topic": "组织"},
            {"slug": "架构", "topic": "架构"},
        ],
    )
    assert rec["status"] == "unspecified"
    assert rec["topic"] is None
    assert rec["candidates"] == ["架构", "组织"]


def test_recommend_topic_does_not_invent_new_topic(scan):
    rec = scan.recommend_topic("人员盘点任务", TOPICS)
    assert rec["topic"] is None
    assert rec["status"] == "unspecified"


def test_fuzzy_hint_energy_org_prefers_energy_topic(scan):
    hint, cands = scan.fuzzy_topic_hint("能源组织讨论", TOPICS)
    assert hint == "能源梳理"
    assert "能源梳理" in cands


def test_fuzzy_hint_platform_org_prefers_org_topic(scan):
    hint, cands = scan.fuzzy_topic_hint("中台组织讨论", TOPICS)
    assert hint == "组织架构讨论"


def test_fuzzy_hint_robot_prefers_hospital_dispatch(scan):
    hint, _cands = scan.fuzzy_topic_hint("机器人调度仿真与参数自学习讨论纪要", TOPICS)
    assert hint == "医院机器人调度"


def test_fuzzy_hint_none_when_no_overlap(scan):
    hint, cands = scan.fuzzy_topic_hint("传奇沟通", TOPICS)
    assert hint is None
    assert cands == []


def test_fuzzy_hint_ignores_generic_discussion_suffix(scan):
    hint, cands = scan.fuzzy_topic_hint("戴云讨论", TOPICS)
    assert hint is None
    assert cands == []
    hint, cands = scan.fuzzy_topic_hint("能源例会", TOPICS)
    assert hint == "能源梳理"
    assert "算法例会" not in cands


def test_auto_assign_topic_prefers_match_then_hint_then_fallback(scan):
    assert scan.auto_assign_topic({"topic": "算法例会", "topic_hint": "能源梳理"}) == "算法例会"
    assert scan.auto_assign_topic({"topic": None, "topic_hint": "能源梳理"}) == "能源梳理"
    assert scan.auto_assign_topic({"topic": None, "topic_hint": None}) == "未分类"


def test_assign_topics_fills_add_items_only(scan):
    items = scan.assign_topics(
        [
            {"title": "a", "action": "add", "topic": None, "topic_hint": "能源梳理"},
            {"title": "b", "action": "skip", "topic": None, "topic_hint": "能源梳理"},
            {"title": "c", "action": "add", "topic": None, "topic_hint": None},
        ]
    )
    assert items[0]["topic"] == "能源梳理"
    assert items[1]["topic"] is None
    assert items[2]["topic"] == "未分类"


def test_apply_no_step_omits_step_actions(apply):
    actions = apply.build_actions(
        {
            "root": "/kairo",
            "items": [
                {
                    "title": "算法例会-260904",
                    "occurred": "2026-09-04",
                    "topic": "算法例会",
                    "action": "add",
                    "forms": [{"path": "/tmp/b.m4a", "copy": True}],
                }
            ],
        },
        step=False,
    )
    assert [a["kind"] for a in actions] == ["add", "title"]


def test_merge_unspecified_carries_topic_hint(scan):
    items = scan.merge_items(
        [
            {
                "title": "能源组织讨论-260907",
                "xxx": "能源组织讨论",
                "occurred": "2026-09-07",
                "path": "/rec/20260907 112244.m4a",
                "source": "voice-memo",
                "copy": True,
            }
        ],
        TOPICS,
        existing_titles=set(),
        existing_basenames=set(),
    )
    assert items[0]["topic"] is None
    assert items[0]["action"] == "add"
    assert items[0]["topic_hint"] == "能源梳理"


def test_format_scan_report_includes_hint(scan):
    text = scan.format_scan_report(
        {
            "items": [
                {
                    "title": "能源组织讨论-260907",
                    "action": "add",
                    "topic": None,
                    "topic_hint": "能源梳理",
                    "hint_candidates": ["能源梳理", "组织架构讨论"],
                },
                {
                    "title": "算法例会-260904",
                    "action": "skip",
                    "topic": "算法例会",
                },
            ]
        }
    )
    assert "能源组织讨论-260907  推荐=能源梳理" in text
    assert "备选=组织架构讨论" in text
    assert "待指定: 1 条" in text


def test_new_import_items_keeps_unprocessed_even_if_notified(scan):
    items = [
        {"title": "旧待指定-260801", "action": "add", "topic": None},
        {"title": "将入库-260907", "action": "add", "topic": "能源梳理"},
        {"title": "已入库-260806", "action": "skip", "topic": "算法例会"},
    ]
    out = scan.new_import_items(items, notified={"旧待指定-260801"})
    titles = [i["title"] for i in out]
    assert titles == ["旧待指定-260801", "将入库-260907"]


def test_format_import_report_omits_skip_counts(scan):
    text = scan.format_import_report(
        [
            {
                "title": "能源组织讨论-260907",
                "occurred": "2026-09-07",
                "topic": None,
                "topic_hint": "能源梳理",
            }
        ]
    )
    assert "待导入" in text
    assert "能源组织讨论-260907  推荐=能源梳理" in text
    assert "已跳过" not in text


def test_new_import_items_since_days_keeps_yesterday_drops_old(scan):
    today = __import__("datetime").date(2026, 9, 8)
    items = [
        {"title": "中台组织讨论-260907", "occurred": "2026-09-07", "action": "add"},
        {"title": "能源例会-260901", "occurred": "2026-09-01", "action": "add"},
        {"title": "产品工厂务虚会-250926", "occurred": "2025-09-26", "action": "add"},
        {"title": "已入库-260907", "occurred": "2026-09-07", "action": "skip"},
    ]
    out = scan.new_import_items(items, since_days=1, today=today)
    assert [i["title"] for i in out] == ["中台组织讨论-260907"]


def test_new_import_items_since_hours_is_rolling_24h(scan):
    now = __import__("datetime").datetime(2026, 9, 8, 10, 0, 0)
    items = [
        {
            "title": "中台组织讨论-260907",
            "action": "add",
            "recorded_at": "2026-09-07T13:59:58",
        },
        {
            "title": "总体组织架构讨论-260907",
            "action": "add",
            "recorded_at": "2026-09-07T09:26:46",
        },
        {
            "title": "能源例会-260901",
            "action": "add",
            "recorded_at": "2026-09-01T09:01:52",
        },
    ]
    out = scan.new_import_items(items, since_hours=24, now=now)
    assert [i["title"] for i in out] == ["中台组织讨论-260907"]


def test_recorded_at_from_voice_name(scan):
    assert scan.recorded_at_from_voice_name("20260907 135958.m4a") == "2026-09-07T13:59:58"


def test_only_new_silent_only_when_nothing_to_import(scan):
    skipped = {
        "title": "算法例会-260904",
        "action": "skip",
        "topic": "算法例会",
    }
    assert scan.new_import_items([skipped], set()) == []
    pending = {
        "title": "能源组织讨论-260907",
        "action": "add",
        "topic": None,
        "topic_hint": "能源梳理",
    }
    assert scan.new_import_items([skipped, pending], {"能源组织讨论-260907"}) == [pending]


def test_format_scan_report_lists_newest_pending_first(scan):
    text = scan.format_scan_report(
        {
            "items": [
                {
                    "title": "产品工厂务虚会-250926",
                    "occurred": "2025-09-26",
                    "action": "add",
                    "topic": None,
                    "topic_hint": None,
                },
                {
                    "title": "中台组织讨论-260907",
                    "occurred": "2026-09-07",
                    "action": "add",
                    "topic": None,
                    "topic_hint": "组织架构讨论",
                },
                {
                    "title": "能源组织讨论-260907",
                    "occurred": "2026-09-07",
                    "action": "add",
                    "topic": None,
                    "topic_hint": "能源梳理",
                },
            ]
        }
    )
    pos_mid = text.find("中台组织讨论-260907")
    pos_energy = text.find("能源组织讨论-260907")
    pos_old = text.find("产品工厂务虚会-250926")
    assert 0 <= pos_energy < pos_old
    assert 0 <= pos_mid < pos_old


def test_merge_groups_same_title_and_prefers_audio(scan):
    items = scan.merge_items(
        [
            {
                "title": "流程质量-260902",
                "xxx": "流程质量",
                "occurred": "2026-09-02",
                "path": "/tmp/流程质量-260902.pdf",
                "source": "downloads",
                "copy": False,
            },
            {
                "title": "流程质量-260902",
                "xxx": "流程质量",
                "occurred": "2026-09-02",
                "path": "/tmp/20260902 090000.m4a",
                "source": "voice-memo",
                "copy": True,
            },
        ],
        TOPICS,
        existing_titles=set(),
        existing_basenames=set(),
    )
    assert len(items) == 1
    item = items[0]
    assert item["topic"] == "流程质量"
    assert item["action"] == "add"
    assert item["forms"][0]["source"] == "voice-memo"
    assert item["forms"][1]["source"] == "downloads"


def test_canonical_title_normalizes_yyyymmdd_and_underscore(scan):
    assert scan.canonical_title("胡博讨论-20260828") == "胡博讨论-260828"
    assert scan.canonical_title("研发流程讨论_260901") == "研发流程讨论-260901"
    assert scan.canonical_title("算法例会-260904") == "算法例会-260904"


def test_merge_skips_existing_title(scan):
    items = scan.merge_items(
        [
            {
                "title": "算法例会-260904",
                "xxx": "算法例会",
                "occurred": "2026-09-04",
                "path": "/tmp/20260904 090104.m4a",
                "source": "voice-memo",
                "copy": True,
            }
        ],
        TOPICS,
        existing_titles={"算法例会-260904"},
        existing_basenames=set(),
    )
    assert items[0]["action"] == "skip"
    assert items[0]["skip_reason"] == "already-ingested"


def test_merge_skips_when_kairo_title_uses_full_year(scan):
    items = scan.merge_items(
        [
            {
                "title": "胡博讨论-260828",
                "xxx": "胡博讨论",
                "occurred": "2026-08-28",
                "path": "/rec/20260828 170129.m4a",
                "source": "voice-memo",
                "copy": True,
            }
        ],
        TOPICS,
        existing_titles={"胡博讨论-20260828"},
        existing_basenames=set(),
    )
    assert items[0]["action"] == "skip"


def test_merge_skips_existing_source_path(scan):
    path = "/Users/xupeng/Downloads/戴云沟通-260903.m4a"
    items = scan.merge_items(
        [
            {
                "title": "戴云沟通-260903",
                "xxx": "戴云沟通",
                "occurred": "2026-09-03",
                "path": path,
                "source": "downloads",
                "copy": False,
            }
        ],
        TOPICS,
        existing_titles=set(),
        existing_basenames=set(),
        existing_paths={path},
    )
    assert items[0]["action"] == "skip"


def test_merge_skips_existing_source_basename(scan):
    items = scan.merge_items(
        [
            {
                "title": "每周讨论-260905",
                "xxx": "每周讨论",
                "occurred": "2026-09-05",
                "path": "/rec/20260905 142008.m4a",
                "source": "voice-memo",
                "copy": True,
            }
        ],
        TOPICS,
        existing_titles=set(),
        existing_basenames={"20260905 142008.m4a"},
    )
    assert items[0]["action"] == "skip"


def test_record_seen_is_picked_up_by_scan(scan, apply, tmp_path):
    apply.record_seen(tmp_path, "戴云沟通-260903", ["/tmp/戴云沟通-260903.m4a"])
    titles, bases, paths = scan.load_existing(tmp_path)
    assert "戴云沟通-260903" in titles
    assert "戴云沟通-260903.m4a" in bases
    assert "/tmp/戴云沟通-260903.m4a" in paths


def test_load_existing_unions_manifest_and_ledger(scan, tmp_path):
    ref = tmp_path / "算法例会" / "references" / "rid1"
    ref.mkdir(parents=True)
    (ref / "manifest.yaml").write_text(
        "title: 胡博讨论-20260828\nforms:\n- location: .kairo/uploads/a.m4a\n",
        encoding="utf-8",
    )
    ledger = tmp_path / ".kairo"
    ledger.mkdir()
    (ledger / "kairo-ingest-seen.json").write_text(
        json.dumps(
            {
                "titles": ["传奇沟通-260903"],
                "basenames": ["foo.m4a"],
                "paths": ["/tmp/foo.m4a"],
            }
        ),
        encoding="utf-8",
    )
    titles, bases, paths = scan.load_existing(tmp_path)
    assert "胡博讨论-20260828" in titles
    assert "胡博讨论-260828" in titles
    assert "传奇沟通-260903" in titles
    assert "a.m4a" in bases
    assert "foo.m4a" in bases
    assert "/tmp/foo.m4a" in paths


def test_collect_downloads_top_level_only(scan, tmp_path):
    (tmp_path / "戴云沟通-260903.m4a").write_bytes(b"x")
    nested = tmp_path / "subdir"
    nested.mkdir()
    (nested / "赵总沟通-260826.pdf").write_bytes(b"x")
    (tmp_path / "readme.txt").write_text("no")
    rows = scan.collect_downloads(tmp_path)
    titles = {r["title"] for r in rows}
    assert titles == {"戴云沟通-260903"}


def test_apply_skips_unspecified_topic(apply):
    actions = apply.build_actions(
        {
            "root": "/Users/xupeng/kairo",
            "items": [
                {
                    "title": "传奇沟通-260903",
                    "occurred": "2026-09-03",
                    "topic": None,
                    "action": "add",
                    "forms": [{"path": "/tmp/a.m4a", "copy": True}],
                }
            ],
        }
    )
    assert actions == []


def test_apply_builds_add_title_attach_and_one_step_per_topic(apply):
    actions = apply.build_actions(
        {
            "root": "/kairo",
            "items": [
                {
                    "title": "流程质量-260902",
                    "occurred": "2026-09-02",
                    "topic": "流程质量",
                    "action": "add",
                    "forms": [
                        {"path": "/tmp/a.m4a", "copy": True},
                        {"path": "/tmp/a.pdf", "copy": False},
                    ],
                },
                {
                    "title": "算法例会-260904",
                    "occurred": "2026-09-04",
                    "topic": "算法例会",
                    "action": "add",
                    "forms": [{"path": "/tmp/b.m4a", "copy": True}],
                },
            ],
        }
    )
    kinds = [a["kind"] for a in actions]
    assert kinds == [
        "add",
        "title",
        "attach",
        "add",
        "title",
        "step",
        "step",
    ]
    assert actions[0]["cwd"] == "/kairo/流程质量"
    assert actions[0]["args"][:2] == ["add", "/tmp/a.m4a"]
    assert "--copy" in actions[0]["args"]
    assert actions[0]["args"][-2:] == ["--occurred", "2026-09-02"]
    assert actions[1]["args"][0] == "title"
    assert actions[2]["args"][:3] == ["add", "/tmp/a.pdf", "--to"]
    assert "--copy" not in actions[2]["args"]
    step_cwds = [a["cwd"] for a in actions if a["kind"] == "step"]
    assert step_cwds == ["/kairo/流程质量", "/kairo/算法例会"]
    assert all(a["args"] == ["step"] for a in actions if a["kind"] == "step")


def test_format_receipt_lists_pending_and_failures(apply):
    text = apply.format_receipt(
        {
            "items": [
                {
                    "title": "传奇沟通-260903",
                    "topic": None,
                    "topic_hint": "团队管理",
                    "action": "add",
                },
                {"title": "算法例会-260904", "topic": "算法例会", "action": "skip"},
            ]
        },
        {
            "added": [
                {
                    "title": "流程质量-260902",
                    "cwd": "/kairo/流程质量",
                    "ref_id": "rid-1",
                }
            ],
            "stepped": [{"cwd": "/kairo/流程质量", "stdout": "stepped"}],
            "failed": [],
            "dry_run": False,
        },
    )
    assert text.startswith("kairo-ingest 完成")
    assert "流程质量-260902" in text
    assert "step 流程质量" in text
    assert "待指定 传奇沟通-260903 推荐=团队管理" in text
    assert "已跳过已入库 1 条" in text
    assert "已跳过: 算法例会-260904" not in text


def test_apply_dry_run_does_not_require_ref_id(apply):
    actions = apply.build_actions(
        {
            "root": "/kairo",
            "items": [
                {
                    "title": "算法例会-260904",
                    "occurred": "2026-09-04",
                    "topic": "算法例会",
                    "action": "add",
                    "forms": [{"path": "/tmp/b.m4a", "copy": True}],
                }
            ],
        }
    )
    result = apply.run_actions(actions, binary="/bin/false", dry_run=True)
    assert result["failed"] == []
    assert result["added"][0]["args"][0] == "add"
    assert result["stepped"][0]["args"] == ["step"]


def test_apply_never_emits_new_or_run(apply):
    actions = apply.build_actions(
        {
            "root": "/kairo",
            "items": [
                {
                    "title": "算法例会-260904",
                    "occurred": "2026-09-04",
                    "topic": "算法例会",
                    "action": "add",
                    "forms": [{"path": "/tmp/b.m4a", "copy": True}],
                }
            ],
        }
    )
    flat = [" ".join(a["args"]) for a in actions]
    assert all("new" not in s.split()[:1] for s in flat)
    assert all(s.split()[0] != "run" for s in flat)
    assert all("tag" not in s.split()[:1] for s in flat)
