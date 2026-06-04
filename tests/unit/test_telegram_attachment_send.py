"""telegram channel 附件投递编排单测(#38 telegram 原生化)。

验证 _send_attachment_directives 的编排:验证文件 → 选 sendDocument/sendPhoto →
photo 失败降级 document → 缺文件不崩。真实 Telegram HTTP 由现成 dolphin 代码负责,
此处 monkeypatch 发送辅助只验编排。
"""
import pytest

from src.everbot.channels.attachment_directives import AttachmentDirective
from src.everbot.channels.telegram_channel import TelegramChannel


def _channel():
    ch = TelegramChannel.__new__(TelegramChannel)  # 跳过重构造,只需 _bot_token
    ch._bot_token = "TESTTOKEN"
    return ch


async def test_sends_file_and_photo_via_correct_api(tmp_path, monkeypatch):
    f = tmp_path / "doc.txt"; f.write_text("hi", encoding="utf-8")
    img = tmp_path / "pic.png"; img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)

    calls = []
    import src.everbot.channels.telegram_skillkit as tsk

    async def _doc(self, chat_id, file_path, caption=""):
        calls.append(("doc", chat_id, file_path, caption)); return {"ok": True}

    async def _photo(self, chat_id, file_path, caption=""):
        calls.append(("photo", chat_id, file_path, caption)); return {"ok": True}

    monkeypatch.setattr(tsk.TelegramSkillkit, "_send_document", _doc)
    monkeypatch.setattr(tsk.TelegramSkillkit, "_send_photo_api", _photo)

    ch = _channel()
    res = await ch._send_attachment_directives("chat42", [
        AttachmentDirective("file", str(f), "报告"),
        AttachmentDirective("photo", str(img), ""),
    ])
    kinds = [c[0] for c in calls]
    assert kinds == ["doc", "photo"]
    assert calls[0][1] == "chat42" and calls[0][3] == "报告"
    assert all(ok for _, ok in res)


async def test_photo_failure_falls_back_to_document(tmp_path, monkeypatch):
    img = tmp_path / "big.png"; img.write_bytes(b"\x89PNG" + b"0" * 80)
    seq = []
    import src.everbot.channels.telegram_skillkit as tsk

    async def _photo(self, chat_id, file_path, caption=""):
        seq.append("photo"); return {"ok": False, "description": "too big"}

    async def _doc(self, chat_id, file_path, caption=""):
        seq.append("doc"); return {"ok": True}

    monkeypatch.setattr(tsk.TelegramSkillkit, "_send_photo_api", _photo)
    monkeypatch.setattr(tsk.TelegramSkillkit, "_send_document", _doc)

    ch = _channel()
    res = await ch._send_attachment_directives("c", [AttachmentDirective("photo", str(img), "")])
    assert seq == ["photo", "doc"]   # 先 photo 失败,降级 document
    assert res == [(str(img), True)]


async def test_missing_file_does_not_crash(tmp_path):
    ch = _channel()
    res = await ch._send_attachment_directives("c", [AttachmentDirective("file", "/no/such/file", "")])
    assert res == [("/no/such/file", False)]
