# 上游 Go 版

上游 v2.1.0（`d26fcb98ea0f15b73ef19ac753bacf0344ce33b2`）完整放在 `upstream-go/`。现有 Python 后端、Next.js 前端和默认 Docker 部署继续从仓库根目录运行；两套服务可以同时启动。Go 版包含上游的创作台、语音合成与转写、图片参考修复及对话缓存修复。

## 本机预览

安装 Go 1.26 或更新版本。从仓库根目录运行：

```sh
export CHATGPT2API_GO_AUTH_KEY='替换为预览管理员密钥'
sh scripts/run_upstream_go.sh
```

浏览器打开 `http://127.0.0.1:3002`，用上述密钥登录。可用 `CHATGPT2API_GO_PORT` 更换端口。预览脚本只监听本机，使用独立的 `upstream-go/data/` 和 `upstream-go/config.yaml`。Go 版开机后及每分钟保留最新 10,000 条日志，仍按上游规则删除过期日志。

## 导入旧号池

1. 若旧版使用 JSON 存储，在 Go 后台「账号」→「导入账号」→「导入账号 JSON 文件」中选择旧版 `data/accounts.json` 的副本。也可导入旧后台导出的 Codex JSON；旧版的导出功能只包含持有完整凭据的账号。
2. 用户 API 密钥在 Go 后台重新设置。旧版的图片、对话、日志、任务、用户密钥规则和配置需单独处理。旧版数据库不要用作 Go 版的 `DATABASE_URL`，因为数据库结构不同。
3. 导入会触发账号刷新。两边同时使用同一个账号时，可能会争用会轮换的刷新令牌；联调建议使用单独的测试账号。

Go 版接口地址为 `http://127.0.0.1:3002/v1`。语音合成使用 `POST /v1/audio/speech`（JSON，`input`、`voice`），转写使用 `POST /v1/audio/transcriptions`（multipart，`file`）。两项均需配置可用 ChatGPT 网页账号。空号池的 `/v1/models` 返回空列表。

保留在 Python 版的能力（例如新前端、账号生图检测，以及原有的用户密钥限制）没有迁入 Go 版。切换生产服务前需要先核对这些能力与数据是否仍要沿用。
