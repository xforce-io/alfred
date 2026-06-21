"""TDD #91 件3:node_bin 必须显式钉死(绝对路径)。

daemon(launchd)与交互 shell 的 PATH 不同,裸 ``node`` 会解析到不同 node →
原生模块 ABI 漂移(2026-06-21 demo_agent 崩因)。doctor 据此校验:
- 绝对路径且可执行 → OK;
- 绝对路径但不存在 → ERROR;
- 非绝对路径(走 PATH)→ 交互 WARN / service 模式 ERROR,并报告当前解析到的绝对路径;
- 非绝对路径且 PATH 里找不到 → ERROR。
"""
import sys

from src.everbot.cli.milkie_preflight import check_node_bin
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
