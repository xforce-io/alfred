# 功能地图

| 文件 | 用户能看见什么 | 对上的验收 |
|---|---|---|
| [telegram-access.md](./telegram-access.md) | 谁能和 bot 说话；`/start <agent>` 绑定；执行失败时 bot 回什么 | #227 S1、S3、S5 |
| [web-api-access.md](./web-api-access.md) | Web API / WS 是否需要 key；跨站页面能否打进来；未知 agent 路由 | #227 S2、S5 |
| [sidecar-env.md](./sidecar-env.md) | 技能脚本能读到哪些环境变量；daemon 的 bot token 是否漏进 sidecar | #227 S4 |
| [doctor.md](./doctor.md) | `bin/everbot doctor` 对访问控制配置的提示 | #227 S6 |

未列入的 heartbeat / routine 调度 / memory / skills 安装：本 Issue 不对则不要 Drive，只在 Cleanup 时确认心跳仍在跑。
