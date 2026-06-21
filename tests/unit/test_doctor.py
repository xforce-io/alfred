"""Doctor report tests."""

from pathlib import Path
import sys
import tempfile

from src.everbot.cli.doctor import collect_doctor_report
from src.everbot.infra.user_data import UserDataManager


def test_doctor_includes_node_bin_check_ok_for_absolute(tmp_path):
    """#91 件3:doctor 报告 milkie node_bin;绝对可执行路径 → OK。"""
    home = tmp_path / ".alfred"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        f"everbot:\n  milkie:\n    node_bin: {sys.executable}\n", encoding="utf-8"
    )
    items = collect_doctor_report(project_root=tmp_path, alfred_home=home)
    node_items = [i for i in items if i.title == "milkie node_bin"]
    assert len(node_items) == 1
    assert node_items[0].level == "OK"
    assert sys.executable in node_items[0].details


def test_doctor_service_mode_errors_on_unpinned_node_bin(tmp_path):
    """#91 件3:service 模式下未钉死(裸 node)→ ERROR。"""
    home = tmp_path / ".alfred"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "everbot:\n  milkie:\n    node_bin: node\n", encoding="utf-8"
    )
    items = collect_doctor_report(
        project_root=tmp_path, alfred_home=home, service_mode=True
    )
    node_items = [i for i in items if i.title == "milkie node_bin"]
    assert node_items and node_items[0].level == "ERROR"


def test_doctor_reports_missing_config_and_agents_dir():
    """Doctor should warn when config/agents are missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        home = root / ".alfred"
        UserDataManager(alfred_home=home)
        # Intentionally do not create directories or config files.
        items = collect_doctor_report(project_root=root, alfred_home=home)
        levels = [i.level for i in items]
        assert "WARN" in levels


# #74:dolphin_has_system_skillkit 已随死配置 tool.enabled_tools 移除,
# 原两条钉住该函数的用例一并撤销。


# ── #74: doctor 同步改名(models.yaml 优先、dolphin.yaml 兜底、撤 skillkit 残留检查) ──


def test_doctor_resolves_models_yaml_first(tmp_path):
    from src.everbot.cli.doctor import resolve_model_config_source
    home = tmp_path / ".alfred"
    home.mkdir(parents=True)
    (home / "models.yaml").write_text("default: x\n", encoding="utf-8")
    (home / "dolphin.yaml").write_text("default: y\n", encoding="utf-8")
    ud = UserDataManager(alfred_home=home)
    label, path = resolve_model_config_source(ud, tmp_path)
    assert label == "alfred"
    assert path == home / "models.yaml"


def test_doctor_legacy_dolphin_still_resolves(tmp_path):
    from src.everbot.cli.doctor import resolve_model_config_source
    home = tmp_path / ".alfred"
    home.mkdir(parents=True)
    proj_cfg = tmp_path / "config"
    proj_cfg.mkdir(parents=True)
    (proj_cfg / "dolphin.yaml").write_text("default: y\n", encoding="utf-8")
    ud = UserDataManager(alfred_home=home)
    label, path = resolve_model_config_source(ud, tmp_path)
    assert label == "project"
    assert path == proj_cfg / "dolphin.yaml"


def test_doctor_report_drops_dead_skillkit_check(tmp_path):
    """tool.enabled_tools 已无 runtime 消费方,doctor 不应再为它产报告项。"""
    home = tmp_path / ".alfred"
    items = collect_doctor_report(project_root=tmp_path, alfred_home=home)
    assert not any("skillkit" in (i.title or "").lower() for i in items)
