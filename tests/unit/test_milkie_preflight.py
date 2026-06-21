"""TDD #91 件3:node_bin 必须显式钉死(绝对路径)。

daemon(launchd)与交互 shell 的 PATH 不同,裸 ``node`` 会解析到不同 node →
原生模块 ABI 漂移(2026-06-21 demo_agent 崩因)。doctor 据此校验:
- 绝对路径且可执行 → OK;
- 绝对路径但不存在 → ERROR;
- 非绝对路径(走 PATH)→ 交互 WARN / service 模式 ERROR,并报告当前解析到的绝对路径;
- 非绝对路径且 PATH 里找不到 → ERROR。
"""
import sys

from src.everbot.cli.milkie_preflight import check_node_bin, probe_native_deps
from src.everbot.cli.doctor import DoctorItem


def test_absolute_existing_node_bin_is_ok():
    # 用 sys.executable 充当"绝对路径且可执行的二进制"
    item = check_node_bin(sys.executable, service_mode=False)
    assert isinstance(item, DoctorItem)
    assert item.level == "OK"
    assert sys.executable in item.details


def test_absolute_missing_node_bin_is_error():
    item = check_node_bin("/no/such/path/node", service_mode=False)
    assert item.level == "ERROR"
    assert "/no/such/path/node" in item.details
    assert item.hint and "node_bin" in item.hint


def test_bare_node_bin_warns_in_interactive_mode(monkeypatch):
    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight.shutil.which", lambda n: "/usr/local/bin/node"
    )
    item = check_node_bin("node", service_mode=False)
    assert item.level == "WARN"
    assert "/usr/local/bin/node" in item.details   # 报告当前解析到的绝对路径
    assert item.hint and "node_bin" in item.hint    # 提示去 config 钉死


def test_bare_node_bin_errors_in_service_mode(monkeypatch):
    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight.shutil.which", lambda n: "/usr/local/bin/node"
    )
    # service 模式下即便能解析到,也必须 ERROR —— 根因(未钉死)没消掉。
    item = check_node_bin("node", service_mode=True)
    assert item.level == "ERROR"
    assert "/usr/local/bin/node" in item.details


def test_bare_node_bin_unresolvable_is_error(monkeypatch):
    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight.shutil.which", lambda n: None
    )
    item = check_node_bin("node", service_mode=False)
    assert item.level == "ERROR"
    assert item.hint and "node_bin" in item.hint


# --- #91 件2:native deps 探针(用 node_bin 实测 require('better-sqlite3'))-------
#
# 用 monkeypatch 替换 _run_probe(避免依赖真 node / 真 better-sqlite3),聚焦
# "退出码+stdout+stderr → DoctorItem" 的映射逻辑。

def _patch_probe(monkeypatch, rc, out, err):
    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight._run_probe",
        lambda node_bin, cwd: (rc, out, err),
    )


def test_probe_ok(monkeypatch, tmp_path):
    _patch_probe(monkeypatch, 0, "MILKIE_DEPS_ABI v23.11.0 131\nMILKIE_DEPS_OK\n", "")
    item = probe_native_deps("/opt/homebrew/bin/node", tmp_path)
    assert item.level == "OK"
    assert "131" in item.details  # ABI 进 details


def test_probe_abi_mismatch_hints_rebuild(monkeypatch, tmp_path):
    err = (
        "Error: The module 'better_sqlite3.node' was compiled against a different "
        "Node.js version using NODE_MODULE_VERSION 127. This version of Node.js "
        "requires NODE_MODULE_VERSION 131."
    )
    _patch_probe(monkeypatch, 1, "", err)
    item = probe_native_deps("/opt/homebrew/bin/node", tmp_path)
    assert item.level == "ERROR"
    assert "NODE_MODULE_VERSION" in item.details          # 真实 stderr 进诊断
    assert item.hint and "npm rebuild better-sqlite3" in item.hint


def test_probe_missing_module_hints_npm_ci(monkeypatch, tmp_path):
    _patch_probe(monkeypatch, 1, "", "Error: Cannot find module 'better-sqlite3'")
    item = probe_native_deps("/opt/homebrew/bin/node", tmp_path)
    assert item.level == "ERROR"
    assert item.hint and "npm ci" in item.hint


def test_probe_dylib_load_failure_is_error(monkeypatch, tmp_path):
    err = "dlopen(.../better_sqlite3.node): Library not loaded: ... image not found"
    _patch_probe(monkeypatch, 1, "", err)
    item = probe_native_deps("/opt/homebrew/bin/node", tmp_path)
    assert item.level == "ERROR"
    assert "image not found" in item.details


def test_probe_generic_nonzero_is_error(monkeypatch, tmp_path):
    _patch_probe(monkeypatch, 1, "", "some unexpected failure")
    item = probe_native_deps("/opt/homebrew/bin/node", tmp_path)
    assert item.level == "ERROR"


def test_probe_skipped_when_milkie_root_absent(tmp_path):
    # milkie 整个目录不存在(provider 未用 milkie)→ 跳过,不产 item。
    item = probe_native_deps("/opt/homebrew/bin/node", tmp_path / "no_such_milkie")
    assert item is None


def test_probe_runner_exception_is_error(monkeypatch, tmp_path):
    def _boom(node_bin, cwd):
        raise FileNotFoundError("node not found")

    monkeypatch.setattr("src.everbot.cli.milkie_preflight._run_probe", _boom)
    item = probe_native_deps("/opt/homebrew/bin/node", tmp_path)
    assert item.level == "ERROR"
    assert "node not found" in item.details


# --- #91 PR4:daemon boot preflight(解析 + 决策 + fail-fast)--------------------

from pathlib import Path as _Path  # noqa: E402

from src.everbot.cli.milkie_preflight import (  # noqa: E402
    resolve_node_bin_and_milkie_root,
    run_boot_preflight,
    enforce_boot_preflight,
)


def test_resolve_defaults_to_node_and_sibling_milkie(tmp_path):
    node_bin, milkie_root = resolve_node_bin_and_milkie_root({}, tmp_path / "alfred")
    assert node_bin == "node"
    assert milkie_root == (tmp_path / "milkie")  # project_root.parent / milkie


def test_resolve_reads_config_node_bin_and_dist_path():
    cfg = {
        "everbot": {
            "milkie": {
                "node_bin": "/opt/homebrew/bin/node",
                "dist_path": "/x/milkie/dist/cli/index.js",
            }
        }
    }
    node_bin, milkie_root = resolve_node_bin_and_milkie_root(cfg, _Path("/x/alfred"))
    assert node_bin == "/opt/homebrew/bin/node"
    assert milkie_root == _Path("/x/milkie")


def test_boot_preflight_fatal_on_native_deps_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight.probe_native_deps",
        lambda nb, mr: DoctorItem(level="ERROR", title="milkie native deps", details="ABI"),
    )
    findings, fatal = run_boot_preflight("/opt/homebrew/bin/node", tmp_path)
    assert fatal is True


def test_boot_preflight_not_fatal_when_unpinned_but_deps_ok(monkeypatch, tmp_path):
    # node_bin 裸名(WARN)但 deps 能加载 → 不致命(当前 node 可用,只是没钉死)。
    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight.shutil.which", lambda n: "/usr/bin/node"
    )
    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight.probe_native_deps",
        lambda nb, mr: DoctorItem(level="OK", title="milkie native deps", details="ok"),
    )
    findings, fatal = run_boot_preflight("node", tmp_path)
    assert fatal is False
    assert any(f.title == "milkie node_bin" and f.level == "WARN" for f in findings)


def test_boot_preflight_not_fatal_when_milkie_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight.probe_native_deps", lambda nb, mr: None
    )
    findings, fatal = run_boot_preflight("/opt/homebrew/bin/node", tmp_path)
    assert fatal is False


class _FakeLog:
    def __init__(self):
        self.calls = []

    def error(self, *a):
        self.calls.append(("error", a))

    def warning(self, *a):
        self.calls.append(("warning", a))

    def info(self, *a):
        self.calls.append(("info", a))


def test_enforce_boot_preflight_raises_on_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight.run_boot_preflight",
        lambda nb, mr: (
            [DoctorItem(level="ERROR", title="milkie native deps", details="ABI", hint="rebuild")],
            True,
        ),
    )
    log = _FakeLog()
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        enforce_boot_preflight({}, tmp_path / "alfred", log)
    # 致命诊断必须被 error 出来
    assert any(lvl == "error" for lvl, _ in log.calls)


def test_enforce_boot_preflight_returns_when_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight.run_boot_preflight",
        lambda nb, mr: (
            [DoctorItem(level="WARN", title="milkie node_bin", details="unpinned", hint="pin it")],
            False,
        ),
    )
    log = _FakeLog()
    enforce_boot_preflight({}, tmp_path / "alfred", log)  # must NOT raise
    assert any(lvl == "warning" for lvl, _ in log.calls)
