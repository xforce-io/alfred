"""#193 P0f — setup/CI must not require dolphin SDK (milkie-only runtime)."""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_bin_setup_has_no_dolphin_requirement():
    setup = (_ROOT / "bin" / "setup").read_text(encoding="utf-8")
    assert "import dolphin" not in setup
    assert "missing_dolphin" not in setup
    assert re.search(r"(?i)requires dolphin", setup) is None
    assert "pip install -e /path/to/dolphin" not in setup


def test_ci_workflow_does_not_install_dolphin():
    wf = (_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "Install Dolphin SDK" not in wf
    assert "dolphin.git" not in wf
    assert re.search(r"(?i)pip install.*dolphin", wf) is None
