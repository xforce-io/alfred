"""附件输出约定解析单测(#38 telegram 原生化)。"""
from src.everbot.channels.attachment_directives import (
    ATTACHMENT_INSTRUCTION,
    parse_attachment_directives,
)


def test_no_directive_returns_text_unchanged():
    text = "普通回复,没有任何标记。"
    cleaned, directives = parse_attachment_directives(text)
    assert cleaned == text
    assert directives == []


def test_parses_send_file_with_caption():
    text = "给你报告。\n<<<send_file: /tmp/report.pdf | 季度报告>>>\n请查收。"
    cleaned, directives = parse_attachment_directives(text)
    assert len(directives) == 1
    d = directives[0]
    assert d.kind == "file"
    assert d.path == "/tmp/report.pdf"
    assert d.caption == "季度报告"
    assert "report.pdf" not in cleaned  # 标记被剥离
    assert "给你报告" in cleaned and "请查收" in cleaned


def test_parses_send_photo_without_caption():
    text = "<<<send_photo: /tmp/a.png>>>"
    cleaned, directives = parse_attachment_directives(text)
    assert directives == [type(directives[0])("photo", "/tmp/a.png", "")]
    assert cleaned == ""  # 全是标记 → 清空


def test_multiple_directives():
    text = "a<<<send_file: /x | c1>>>b<<<send_photo: /y>>>c"
    cleaned, directives = parse_attachment_directives(text)
    assert [d.kind for d in directives] == ["file", "photo"]
    assert [d.path for d in directives] == ["/x", "/y"]
    assert cleaned == "abc"


def test_collapses_blank_lines_left_behind():
    text = "上文\n\n<<<send_file: /x>>>\n\n下文"
    cleaned, _ = parse_attachment_directives(text)
    assert "\n\n\n" not in cleaned
    assert "上文" in cleaned and "下文" in cleaned


def test_instruction_mentions_both_markers():
    assert "send_file" in ATTACHMENT_INSTRUCTION
    assert "send_photo" in ATTACHMENT_INSTRUCTION
