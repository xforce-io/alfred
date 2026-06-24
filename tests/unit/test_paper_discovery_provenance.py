"""#127/#124 P3 — paper-discovery L2(a) 合规守卫。

paper-discovery 的报告天生逐篇内联来源链接(arXiv/PDF/HF/GitHub),不像灰犀牛
那样在聚合时丢 url。本测试锁住这个性质,防将来被改回"只剩标题没链接"。
"""
import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "paper-discovery" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gr = _load("generate_report")


def test_paper_entry_inlines_source_links():
    paper = {
        "title": "Attention Is All You Need 2",
        "arxiv_url": "https://arxiv.org/abs/2606.00001",
        "pdf_url": "https://arxiv.org/pdf/2606.00001",
        "github_repo": "https://github.com/x/y",
        "ai_summary_zh": "摘要",
        "heat_level": 3,
    }
    entry = gr.format_paper_entry(paper, 1)
    # 逐篇可溯源:报告条目必须带真实来源链接
    assert "https://arxiv.org/abs/2606.00001" in entry
    assert "https://arxiv.org/pdf/2606.00001" in entry
    assert "https://github.com/x/y" in entry


def test_report_carries_links_for_all_papers():
    papers = [
        {"title": "P1", "arxiv_url": "https://arxiv.org/abs/1", "heat_level": 1},
        {"title": "P2", "arxiv_url": "https://arxiv.org/abs/2", "heat_level": 1},
    ]
    report = gr.generate_formatted_report(papers)
    assert "https://arxiv.org/abs/1" in report
    assert "https://arxiv.org/abs/2" in report
