"""面向终端用户的标准文案(#92 件A)。

内部异常(SidecarStartError 等富诊断)绝不透传给用户 —— channel 边界统一回这些
友好文案,富诊断只进日志。集中一处,保证 telegram/web 口径一致。
"""

# agent/sidecar 起不来或不可用时对用户的统一提示。
AGENT_UNAVAILABLE = "助手暂时不可用,已记录诊断,请稍后重试。"
