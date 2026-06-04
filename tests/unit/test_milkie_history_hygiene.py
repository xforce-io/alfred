"""A4:milkie 历史出口(_milkie_messages_to_history)的数据卫生覆盖(#38)。

milkie /session/history 翻译出的 history 若含 orphan tool(无配对 assistant tool_use)或
空 assistant(无 content 无 tool_calls),下游(记忆抽取/历史压缩/重启)送给 LLM 会 400。
故对 milkie 历史出口套用与 dolphin 保存路径同一套卫生(empty-assistant 过滤 + orphan-tool 修复)。
"""
from src.everbot.core.agent.provider.milkie.provider import _milkie_messages_to_history


def _user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant_text(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _assistant_tooluse(tid, name="run_command", inp=None):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp or {}}]}


def _tool_result(tid, text):
    return {"role": "tool", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}


def test_filters_empty_assistant():
    msgs = [_user("hi"), _assistant_text(""), _assistant_text("real answer")]
    out = _milkie_messages_to_history(msgs)
    roles_contents = [(m["role"], m.get("content")) for m in out]
    # 空 assistant 被剔除,真实回复保留
    assert ("assistant", "") not in roles_contents
    assert ("assistant", "real answer") in roles_contents


def test_heals_orphan_tool_message():
    # tool_result 的 tool_use_id 没有配对的 assistant tool_use → orphan
    msgs = [_user("do x"), _tool_result("ghost", "orphan tool output")]
    out = _milkie_messages_to_history(msgs)
    # 没有残留的 orphan tool 消息
    assert not any(m["role"] == "tool" and m.get("tool_call_id") == "ghost" for m in out)
    # orphan 内容被并入 user 上下文(不丢)
    assert any(m["role"] == "user" and "orphan tool output" in (m.get("content") or "") for m in out)


def test_keeps_valid_tool_chain():
    msgs = [_user("run it"), _assistant_tooluse("t1"), _tool_result("t1", "exit 0")]
    out = _milkie_messages_to_history(msgs)
    # 配对的 tool chain 完整保留
    assert any(m["role"] == "assistant" and m.get("tool_calls") for m in out)
    assert any(m["role"] == "tool" and m.get("tool_call_id") == "t1" for m in out)


def test_clean_history_unchanged():
    msgs = [_user("hi"), _assistant_text("hello")]
    out = _milkie_messages_to_history(msgs)
    assert out == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
