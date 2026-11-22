# 从 Python 版迁移

Go 版使用新的数据库。先备份旧数据目录和 `config.json`，从旧版复制 `accounts.json` 和 `auth_keys.json` 到本机专用目录。若旧版使用数据库或 Git 存储，先导出这两份 JSON 快照；也可以从旧版备份提取 `snapshots/accounts.json` 和 `snapshots/auth_keys.json`（取决于备份选项）。旧版数据库不能直接给 Go 使用。

在 Go 版仓库根目录，先检查快照：

```sh
go run . migrate-legacy -accounts /path/to/accounts.json -keys /path/to/auth_keys.json
```

没有用户密钥时省略 `-keys`，只迁移密钥时省略 `-accounts`。检查命令不打开目标数据库。设置好 Go 管理员密钥和目标数据库后，加 `-apply` 写入：

```sh
go run . migrate-legacy \
  -accounts /path/to/accounts.json -keys /path/to/auth_keys.json -apply
```

迁移不会请求 ChatGPT。已在目标库存在的账号或密钥会让本次导入整体回滚。建议在正式启动 Go 服务前迁移，导入后检查账号和常用 API。旧用户密钥仍用原密钥调用，模型白名单、限流和到期时间随密钥保留；Go 不会显示旧密钥明文。旧管理员密钥需通过新配置设置。

兼容 `gpt-image-2`、`gpt-image-2.5` 和 `codex-gpt-image-2`。前两个走 Web，2K/4K 仍需显式选择 Codex 模型。后台账号详情可发一张测试图检查所选账号的生图能力，检测会消耗一次额度；旧版批量检测记录不导入。

旧图库、对话、任务、日志、代理配置和旧前端的本地偏好不在此迁移范围内，旧文件仍留在备份中。上线前若需要继续访问这些历史数据，先单独核对。Go 版日志按上游保留天数清理，另在启动时及每分钟只保留最新 10,000 条。
