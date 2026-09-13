# `bin/everbot doctor` 访问控制项

CLI 入口，只读配置，不改状态。

## 用户入口（必须都走）

1. **最终配置**：`./bin/everbot doctor`。
2. **缺配置**：`ALFRED_HOME=/tmp/verify-doctor-<issue> ./bin/everbot doctor`，其中 `config.yaml` 是缺 `allowed_chat_ids`、`web.api_key` 为空、`env_passthrough` 引用未导出变量的样本。

## #227 S6

1. 最终配置：无 `Telegram access` / `Web api_key` / `env_passthrough` 的 ERROR/WARN 行。
2. 样本配置：
   - `Telegram access (default)` ERROR，提示 `allowed_chat_ids`；
   - `Web api_key` ERROR；若写 `${NOT_EXPORTED}` 则 details 含该变量名；
   - `env_passthrough (demo_agent)` WARN，列出缺失的变量名。
3. 样本里加 `allow_all: true` → `Telegram access` 变 WARN。

判定：输出原文存证。

## 不做

不把 doctor 做成自动修复。
