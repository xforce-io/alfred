"""Milkie agent provider (cross-process sidecar).

垂直切片 PoC:通过 ``milkie serve`` 子进程 + HTTP/SSE 驱动 milkie runtime,
把 milkie 原生事件适配成 alfred 的 :class:`TurnEvent`。详见 issue #86 与
xforce-io/alfred#32。
"""
