# Isolated Routine 检查点与投递幂等（#143）

> 最后更新：2026-07-10  
> Issue: https://github.com/xforce-io/alfred/issues/143  
> 分支：`feat/143-routine-checkpoints`

## 目标

在不引入通用 DAG/workflow engine 的前提下，为 isolated routine 提供可选的
`fetch -> analyze -> deliver` 阶段检查点，并保证含糊投递失败后的重试不会产生重复通知。

## 执行身份与存储

- `execution_id = task_id + scheduled_for`，重试时间不改变身份。
- 每阶段原子写入 manifest：输入 hash、输出 artifact 引用/hash、完成时间、producing run id。
- manifest 位于 agent workspace 的 runtime 区，不写入版本化 skill 目录。
- retry 从第一个未完成或校验失败的阶段继续；输入变化使所有下游阶段失效。
- manifest 不保存 secret。

## 投递幂等

`delivery_key = execution_id + output_hash + destination`。投递前记录 `pending`，确认后记录
`delivered`。重试先查询 channel outbox；已投递 key 不再写用户消息、history 或 projection。

## Skill 契约与兼容

routine 可选声明 staged execution descriptor，指向阶段命令与 artifact。未声明的现有
free-form routine 继续走单 prompt 路径。首个端到端测试使用确定性 fixture；生产 Serenity
只有在 twitter-watch 已暴露稳定阶段 artifact 后才显式迁移。

## 测试计划

- **单元测试**：execution/delivery key、manifest 校验、原子状态迁移、下游失效和脱敏。
- **集成测试**：fetch 后崩溃从 analyze 恢复；输入变化触发下游失效；daemon 重启后状态仍在。
- **功能测试**：含糊 channel failure 重试后只有一条 history、projection 和用户可见消息。
- **端到端测试**：真 Alfred daemon + 真 Milkie sidecar + staged fixture；fetch 后模型失败，
  重启服务，再从 analyze 继续且只投递一次，核对 artifact hash 和 run 链接。
- **配置验证**：旧 routine JSON 不带 staged 字段仍沿用 legacy 路径。

## 非目标

不实现通用 DAG、分布式事务，也不自动迁移所有历史技能。
