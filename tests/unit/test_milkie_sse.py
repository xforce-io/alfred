"""TDD: 增量 SSE 解析(milkie serve 的 text/event-stream)。

milkie serve 每帧形如 ``event: <name>\\ndata: <json>\\n\\n``。解析器必须能
处理:一次喂入多帧、单帧跨多次 chunk(网络分包)、无 data 的注释/keepalive
帧、缺省 event 名、多行 data。
"""
from everbot.core.agent.provider.milkie.sse import SSEParser


def test_single_complete_frame():
    p = SSEParser()
    frames = p.feed('event: message_delta\ndata: {"text": "hi"}\n\n')
    assert frames == [("message_delta", '{"text": "hi"}')]


def test_two_frames_in_one_chunk():
    p = SSEParser()
    frames = p.feed(
        'event: message_delta\ndata: {"text":"a"}\n\n'
        'event: agent.run.completed\ndata: {"status":"completed"}\n\n'
    )
    assert frames == [
        ("message_delta", '{"text":"a"}'),
        ("agent.run.completed", '{"status":"completed"}'),
    ]


def test_frame_split_across_chunks():
    """单帧被网络拆成两次 chunk 也必须正确拼回。"""
    p = SSEParser()
    assert p.feed("event: message_delta\nda") == []
    assert p.feed('ta: {"text":"x"}\n\n') == [("message_delta", '{"text":"x"}')]


def test_data_byte_split_across_chunks():
    """连 data 的 JSON 都可能被拆断。"""
    p = SSEParser()
    assert p.feed('event: message_delta\ndata: {"te') == []
    assert p.feed('xt":"曹"}\n\n') == [("message_delta", '{"text":"曹"}')]


def test_frame_without_data_is_ignored():
    """注释/keepalive(无 data 行)→ 不产出帧。"""
    p = SSEParser()
    assert p.feed(": keepalive\n\n") == []


def test_event_defaults_to_message_when_only_data():
    p = SSEParser()
    assert p.feed('data: {"x":1}\n\n') == [("message", '{"x":1}')]


def test_multiline_data_joined_with_newline():
    p = SSEParser()
    frames = p.feed("data: line1\ndata: line2\n\n")
    assert frames == [("message", "line1\nline2")]


def test_partial_trailing_frame_not_emitted():
    """没有以空行结尾的尾部数据必须留在缓冲,不提前产出。"""
    p = SSEParser()
    assert p.feed('event: message_delta\ndata: {"text":"y"}\n') == []
