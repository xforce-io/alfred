"""增量 Server-Sent Events 解析。

milkie serve 的 ``text/event-stream`` 每帧形如::

    event: <name>
    data: <json>

帧之间用空行(``\\n\\n``)分隔。网络分包会把单帧甚至单行 JSON 拆断,故采用
带缓冲的增量解析:``feed()`` 每次喂入一段 chunk,返回本次凑齐的完整帧。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

Frame = Tuple[str, str]  # (event_name, data_json_str)


class SSEParser:
    """Stateful incremental SSE frame parser."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> List[Frame]:
        """Append ``chunk`` and return every complete frame now available."""
        self._buf += chunk
        out: List[Frame] = []
        while "\n\n" in self._buf:
            raw, self._buf = self._buf.split("\n\n", 1)
            frame = self._parse_frame(raw)
            if frame is not None:
                out.append(frame)
        return out

    @staticmethod
    def _parse_frame(raw: str) -> Optional[Frame]:
        event = "message"
        data_lines: List[str] = []
        for line in raw.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if not data_lines:
            return None
        return (event, "\n".join(data_lines))
