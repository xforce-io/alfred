# 统一 Routine 重试策略（#142）

> 最后更新：2026-07-10  
> Issue: https://github.com/xforce-io/alfred/issues/142  
> 分支：`feat/142-unified-routine-retry`

## 目标

Alfred 成为 routine 重试调度的唯一所有者。移除 isolated-agent 内部固定重试循环，
让 `Task.max_retry`、持久化状态、用户提示和实际调度共享同一个决策结果。

## 策略

- 初始执行不计入 retry；`max_retry` 表示初始执行之后最多重试次数。
- 结构化 provider 错误优先读取 `retryable`；旧错误字符串分类仅作兼容回退。
- 非 retryable 错误直接终止当前周期。
- 默认延迟保留 5 分钟、15 分钟；更大的 `max_retry` 使用封顶指数退避。
- jitter 可配置，clock/random 通过依赖注入保证测试确定性。

纯策略函数输出 `retry | terminal`、delay、attempt/max_retry、next_run_at 和错误元数据。
持久化和 `format_retry_hint` 必须消费同一个决策对象，禁止重复推导。

## 状态与兼容

持久化 attempt、`last_error_code`、retryable 和 next_run_at。旧 HEARTBEAT 记录缺少
新字段时按默认值加载；现有 `retry` 字段保持可读并迁移为权威 attempt 语义，不改变
既有 schedule。

## 测试计划

- **单元测试**：`max_retry=0/1/3/5`、可重试/终止错误、封顶退避和确定性 jitter。
- **集成测试**：任务保存再加载后，claim、hint 和调度仍使用同一 attempt。
- **功能测试**：isolated routine 首次 transient 失败、仅调度一次重试后成功；终止错误不重试。
- **端到端测试**：Alfred → 真 Milkie sidecar → 确定性模型 stub 先失败后成功；只产生
  两个 run attempt 和一次最终投递。
- **配置验证**：旧 HEARTBEAT JSON 无需迁移即可加载，daemon smoke 保持通过。

## 非目标

不实现 provider failover、Milkie 内部重试、stage checkpoint 或通用 workflow engine。
