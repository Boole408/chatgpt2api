# 部署

复制 `config.example.yaml` 为 `config.yaml`，设置 `api-key`，或通过环境变量 `CHATGPT2API_AUTH_KEY` 配置管理员密钥。

```sh
docker compose up -d --build
```

面板默认在 `http://localhost:3000`，API 前缀为 `/v1`。编排只启动一个 Go 应用，界面由同一个进程提供。默认把 `./data-go` 挂载到容器的 `/app/data`；备份 `config.yaml` 和该目录。设置 `CHATGPT2API_HOST_PORT` 可换端口，设置 `CHATGPT2API_DATA_DIR` 可换宿主数据目录。

访问密钥的可复制副本会加密存储。备份或迁移数据库时，请同时保留数据目录中的 `access-keys.aes`；使用外部数据库时也需要备份此文件。

已有本仓库发布的 Go 镜像时，可以把 `CHATGPT2API_IMAGE` 设为完整镜像引用，再执行：

```sh
docker compose pull app
docker compose up -d --no-build
```

默认使用 SQLite。使用新的 PostgreSQL 或 MySQL 数据库时，Compose 读 `CHATGPT2API_DATABASE_URL`，直接运行 Go 时读 `DATABASE_URL`。切换旧版前先看 [迁移说明](migrate-python.md)。

## 直接运行

安装项目 `go.mod` 指定版本的 Go。在仓库根目录运行 `BIND_ADDR=127.0.0.1 go run .`。直接运行时默认数据目录也是 `./data-go`，可用 `CHATGPT2API_STORAGE_DIR` 修改；监听端口由 `PORT` 指定。

## WARP 代理

需要 WARP、Privoxy 与 FlareSolverr 时，用额外的 Compose 文件：

```sh
docker compose -f docker-compose.yml -f docker-compose.warp.yml up -d --build
```

在后台设置或 `config.yaml` 里启用 `proxy_runtime.enabled`。普通部署无需启动代理组件。
