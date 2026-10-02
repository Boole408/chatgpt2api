<h1 align="center">ChatGPT2API</h1>


<p align="center">ChatGPT2API 主要是对 ChatGPT 官网相关能力进行逆向整理与封装，提供面向 ChatGPT 图片生成、图片编辑、多图组图编辑场景的 OpenAI 兼容图片 API / 代理，并集成在线画图、号池管理、多种账号导入方式与 Docker 自托管部署能力。</p>

> [!WARNING]
> 免责声明：
>
> 本项目涉及对 ChatGPT 官网文本生成、图片生成与图片编辑等相关接口的逆向研究，仅供个人学习、技术研究与非商业性技术交流使用。
>
> - 严禁将本项目用于任何商业用途、盈利性使用、批量操作、自动化滥用或规模化调用。
> - 严禁将本项目用于破坏市场秩序、恶意竞争、套利倒卖、二次售卖相关服务，以及任何违反 OpenAI 服务条款或当地法律法规的行为。
> - 严禁将本项目用于生成、传播或协助生成违法、暴力、色情、未成年人相关内容，或用于诈骗、欺诈、骚扰等非法或不当用途。
> - 使用者应自行承担全部风险，包括但不限于账号被限制、临时封禁或永久封禁以及因违规使用等所导致的法律责任。
> - 使用本项目即视为你已充分理解并同意本免责声明全部内容；如因滥用、违规或违法使用造成任何后果，均由使用者自行承担。
> - 本项目基于对 ChatGPT 官网相关能力的逆向研究实现，存在账号受限、临时封禁或永久封禁的风险。请勿使用你自己的重要账号、常用账号或高价值账号进行测试。


## 赞助商

<table>
  <tr>
    <td width="190" align="center">
      <a href="https://www.atlascloud.ai/zh?utm_source=github&utm_medium=link&utm_campaign=chatgpt2api"><img src="assets/atlascloud.svg" width="163" alt="Atlas Cloud"></a>
    </td>
    <td>
      <a href="https://www.atlascloud.ai/zh?utm_source=github&utm_medium=link&utm_campaign=chatgpt2api">Atlas Cloud</a> is a full-modal AI inference platform that gives developers a single AI API to access video generation, image generation, and LLM APIs. Instead of managing multiple vendor integrations, you connect once and get unified access to 300+ curated models across all modalities. Check out <a href="https://www.atlascloud.ai/console/coding-plan">Atlas Cloud's new coding plan promotion</a> for more budget-friendly API access.
    </td>
  </tr>
</table>

## 快速开始

### Docker 运行

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
docker compose up -d
```

启动前请先在 `config.json` 中设置 `auth-key`，也可以在 `docker-compose.yml` 中通过 `CHATGPT2API_AUTH_KEY` 覆盖。

- Web 面板：`http://localhost:3000`
- API 地址：`http://localhost:3000/v1`
- 数据目录：`./data`

### WARP / FlareSolverr 稳定代理部署

如果图片链路经常遇到 Cloudflare 拦截，可以启用附带的 WARP + Privoxy + FlareSolverr 方案：

```bash
cp .env.example .env
docker compose -f docker-compose.warp.yml up -d --build
```

该 compose 会启动：

- `warp-proxy`：提供 WARP SOCKS5 出口。
- `privoxy`：把 WARP SOCKS5 转成 HTTP 代理。
- `flaresolverr`：刷新 Cloudflare clearance。
- `init-config`：幂等写入 `proxy_runtime` 默认配置。
- `app`：启动 ChatGPT2API 主服务。

默认只让上游 OpenAI / ChatGPT 请求走稳定代理，账号邮箱、CPA 等辅助链路不会被强制接管。账号自身配置的代理优先级最高，其次是稳定代理运行时，再其次是显式代理和旧版全局代理。

可在 `.env` 中调整端口和代理运行时参数，也可在后台设置页的「稳定代理运行时」面板手动保存、测试代理和测试 clearance。

### 本地开发

启动后端：

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

启动前端：

```bash
cd chatgpt2api/web
bun install
bun run dev
```

后续更新新版本：

```bash
docker pull ghcr.io/basketikun/chatgpt2api:latest
docker-compose down
docker-compose up -d

```

### 存储后端配置

支持通过环境变量 `STORAGE_BACKEND` 切换存储方式：

- `json` - 本地 JSON 文件（默认）
- `sqlite` - 本地 SQLite 数据库
- `postgres` - 外部 PostgreSQL（需配置 `DATABASE_URL`）
- `git` - Git 私有仓库（需配置 `GIT_REPO_URL` 和 `GIT_TOKEN`）

示例：使用 PostgreSQL

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

## 功能

### 生图超时与诊断

管理后台的图片任务会记录 `stage_timings_ms`，服务日志中可检索 `image_task_timing`，用于区分账号等待、上游准备、生成流、结果轮询及下载所花的时间。`image_poll_timeout_secs` 控制单次生成流/轮询等待，`image_task_timeout_secs`（默认 360 秒）限制官网生图重试共用的总预算；网络请求仍可能使实际结束时间略晚于预算。若超时任务带有会话 ID 与原账号，工作台的「继续等待」会用原账号续查，不再次发起生图。旧版本已保存的任务没有账号关联，无法续查，需要重新生成。

建议先观察不同阶段的超时率，再调整 `image_account_concurrency`。每账号并发设为 1 可减少同一账号同时生图的压力，但高负载下排队时间可能增加；`image_parallel_generation` 保持启用时，不同账号仍可并行。关闭 `image_settle_enabled` 和 `image_check_before_hit_enabled` 可缩短已拿到图片 ID 的响应时间，但应关注缺图及下载失败率。

### 图片模型与尺寸

CPU 图片超分已关闭。`gpt-image-2` 与 `gpt-image-2.5` 的请求省略 `size` 或使用 1K 档尺寸时直接返回原图；2K/4K 请求明确返回 400，不会自动改走 Codex，也不会把低分辨率原图冒充高分辨率结果。显式选择 `codex-gpt-image-2` 时仍使用原有 Codex 高分辨率链路。

`gpt-image-2` 保持旧行为：上游对话模型由后台的 `default_upstream_model_name` 配置决定。`gpt-image-2.5` 是独立模型名，图片准备与生成请求均以 `gpt-image-2.5` 发送到上游；是否实际可用取决于上游账号的模型权限。历史超分失败任务的原图继续保留，但不再提供超分重试。

### API 兼容能力

- 兼容 `POST /v1/images/generations` 图片生成接口
- 兼容 `POST /v1/images/edits` 图片编辑接口
- 兼容面向图片场景的 `POST /v1/chat/completions`
- 兼容面向图片场景的 `POST /v1/responses`
- `GET /v1/models` 返回 `gpt-image-2`、`gpt-image-2.5`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、
  `gpt-5-mini`
- 支持通过 `n` 返回多张生成结果
- 支持生成可编辑 PPT 文件
- 支持生成可编辑 PSD 文件
- 支持 Codex 中的画图接口逆向，仅 `Plus` / `Team` / `Pro` 订阅可用，模型别名为 `codex-gpt-image-2`，如有需要可自行在其他场景映射回
  `gpt-image-2`，用于和官网画图区分；也就意味着同一账号会同时有官网和 Codex 两份生图额度

### 在线画图功能

- 内置在线画图工作台，支持生成、图片编辑与多图组图编辑
- 支持 `gpt-image-2`、`gpt-image-2.5`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、`gpt-5-mini` 模型选择
- 编辑模式支持参考图上传
- 前端支持多图生成交互
- 本地保存图片会话历史，支持回看、删除和清空
- 支持服务端缓存图片URL
- 图片生成进度追踪，超时后可继续等待
- 图片懒加载与滚动位置记忆，优化大量图片场景性能

### 号池管理功能

- 自动刷新账号邮箱、类型、额度和恢复时间（异步进度追踪）
- 轮询可用账号执行图片生成与图片编辑
- 遇到 Token 失效类错误时自动剔除无效 Token
- 定时检查限流账号并自动刷新
- 支持密码重新登录恢复异常账号，刷新后可自动重登
- 支持网页端配置全局 HTTP / HTTPS / SOCKS5 / SOCKS5H 代理
- 支持 WARP / FlareSolverr 稳定代理运行时
- 支持搜索、筛选、批量刷新、导出、手动编辑和清理账号
- 支持四种导入方式：本地 CPA JSON 文件导入、远程 CPA 服务器导入、`sub2api` 服务器导入、`access_token` 导入
- 支持在设置页配置 `sub2api` 服务器，筛选并批量导入其中的 OpenAI OAuth 账号

### 批量自动注册

号池页面点击“自动注册”，在“接码设置”保存 ccmtc 统一收件邮箱的四段凭据并测试取件，再粘贴邮箱列表或导入 TXT / JSON，查看预览后开始任务。

新邮箱先从 ChatGPT 登录/注册入口完成注册，核实浏览器会话的邮箱后复用该会话执行 OAuth；重试已注册项直接进入 OAuth。验证码支持单输入框与六格输入框，页面提供重发按钮时，验证码被拒绝会先请求重发再等待读取新邮件。

- 全批次串行执行，每个邮箱独立浏览器会话；人工验证等待期间也不会启动下一个邮箱。只使用发码后收到的新邮件，优先核对原始收件地址，选择最新验证码。验证码被拒绝后等待 5 秒再取件，不回退使用旧邮件；最多提交 3 次，验证码等待上限 180 秒，单账号自动流程上限 10 分钟（人工暂停时间另计）。
- 姓名使用随机英文名，年龄 26–60 岁；需要密码时使用接码设置中的注册密码。失败重试沿用原资料，已入池项不会重复导入。
- 关闭弹窗后任务保留；重新打开可查看进度、停止任务或重试失败项。macOS/Windows 桌面默认打开可见 Chromium；安全挑战保留原会话并暂停整个批次，最长等待 15 分钟。在任务进度点击“显示验证窗口”，自行完成挑战后点击“已完成验证，继续”；取消此账号会关闭该会话并继续下一项，停止任务会取消整个批次。手机验证和未知密码会单独提示人工处理。
- 凭据与任务分别保存在 `data/registration_settings.json` 和 `data/registration_jobs.json`，文件权限为 0600；密码和 token 不回显。部署时持久化并保护 `data`，使用 **单个 Uvicorn worker、单个服务实例**，避免统一邮箱被并发取码。重启后未完成任务标记为中断，需要手动重试。
- OAuth 回调校验 state，入池前核实 token 对应邮箱；资料刷新失败会保留已入池账号并提示再次刷新。
- 入池身份使用 OpenAI 的 OAuth userinfo 接口核对邮箱，账号资料/额度另行刷新。后端 HTTP 不自动沿用 macOS 系统代理；本地浏览器能够登录、后端兑换却提示地区受限时，请在项目设置中配置已有的全局代理，确保两者使用同一网络出口。
- OAuth 会话在注册成功后生成，已注册项重试直接授权；安全验证等待导致会话临近过期时重新生成匹配的 state/PKCE。已兑换凭证以私有字段保存在任务文件中，入池失败重试时先验证凭证再继续入池；凭证返回 401 时重新授权。已入池的刷新失败在号池列表中单独刷新。
- 阻断诊断提供移除 query 的 URL、脱敏标题、失败资源状态和脱敏截图；截图存储于私有 `data/registration_diagnostics`，仅管理员接口可读。

Linux/Docker 默认使用无头浏览器，无法通过普通网页远程操控保留的 Chromium。需要人工续接时，在服务所在机器配置桌面/VNC 和 `DISPLAY`，让用户访问该桌面；或者在本机桌面运行服务。`CHATGPT2API_REGISTRATION_HEADLESS=1` 可强制无头模式，此模式遇到挑战会明确报出无法续接，并关闭会话。

本地安装浏览器：

```bash
uv sync
uv run python -m playwright install chromium
# Linux 缺少系统依赖时使用：
uv run python -m playwright install --with-deps chromium
```

Docker 镜像已包含 Chromium 和运行依赖。外部页面可能变化，安全验证会阻止自动流程；本地浏览器测试通过不代表真实注册与入池已完成。

#### RoxyBrowser 驱动

在服务所在机器安装并登录 RoxyBrowser，启用本地 API。在“自动注册 → 接码设置”选择 RoxyBrowser，填写本机 API 地址（默认 `http://127.0.0.1:50000`）和 API Token，先保存，再点击“测试连接 / 读取工作区”，选择自己的工作区及项目并再次保存。

每个邮箱创建独立临时环境，通过 CDP 连接该环境的原有 context，注册与 OAuth 共用会话。结束后关闭并删除本次创建的环境；回收失败会停止批次，保留环境 ID，下次使用原 API/工作区配置重试回收。不会删除用户已有的其他环境。API Token 仅后端私有保存，页面不回显。安全验证仍需在保留的窗口中人工完成。

RoxyBrowser 必须与后端运行在同一机器，API 与 CDP 地址仅接受本机地址；容器中的 `127.0.0.1` 指向容器自身。Roxy 安装、登录、API 权限和实际注册验收需要单独完成。

### 实验性 / 规划中

- 详细状态说明见：[功能清单](./docs/feature-status.en.md)

## 效果展示

<table width="100%">
  <tr>
    <td width="50%"><img src="https://i.ibb.co/Jj8nfwwP/image.png" alt="image" border="0"></td>
    <td width="50%"><img src="https://i.ibb.co/pqf235v/image-edit.png" alt="image edit" border="0"></td>
  </tr>
  <tr>
    <td width="50%"><img src="https://i.ibb.co/tPcqtVfd/chery-studio.png" alt="chery studio" border="0"></td>
    <td width="50%"><img src="https://i.ibb.co/PsT9YHBV/account-pool.png" alt="account pool" border="0"></td>
  </tr>
  <tr>
    <td width="50%"><img src="https://i.ibb.co/rRWLG08q/new-api.png" alt="new api" border="0"></td>
  </tr>
</table>

## API

所有 AI 接口都需要请求头：

```http
Authorization: Bearer <auth-key>
```

<details>
<summary><code>GET /v1/models</code></summary>
<br>

返回当前暴露的图片模型列表。

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer <auth-key>"
```

<details>
<summary>说明</summary>
<br>

| 字段   | 说明                                                                                                         |
|:-----|:-----------------------------------------------------------------------------------------------------------|
| 返回模型 | `gpt-image-2`、`gpt-image-2.5`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、`gpt-5-mini` |
| 接入场景 | 可接入 Cherry Studio、New API 等上游或客户端                                                                          |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/generations</code></summary>
<br>

OpenAI 兼容图片生成接口，用于文生图。

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "一只漂浮在太空里的猫",
    "n": 1,
    "response_format": "b64_json"
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段                | 说明                                                 |
|:------------------|:---------------------------------------------------|
| `model`           | 图片模型，当前可用值以 `/v1/models` 返回结果为准，推荐使用 `gpt-image-2` |
| `prompt`          | 图片生成提示词                                            |
| `n`               | 生成数量，当前后端限制为 `1-4`                                 |
| `size`            | 图片尺寸；普通模型 2K/4K 请求返回 400，显式 Codex 模型可用高分辨率       |
| `response_format` | 当前请求模型中包含该字段，默认值为 `b64_json`                       |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/edits</code></summary>
<br>

OpenAI 兼容图片编辑接口，可上传图片文件，也可按官方 JSON 格式传入图片链接并生成编辑结果。

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer <auth-key>" \
  -F "model=gpt-image-2" \
  -F "prompt=把这张图改成赛博朋克夜景风格" \
  -F "n=1" \
  -F "image=@./input.png"
```

也可以直接传图片 URL：

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer <auth-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "把这张图改成赛博朋克夜景风格",
    "images": [
      {"image_url": "https://example.com/input.png"}
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段          | 说明                                            |
|:------------|:----------------------------------------------|
| `model`     | 图片模型， `gpt-image-2`                           |
| `prompt`    | 图片编辑提示词                                       |
| `n`         | 生成数量，当前后端限制为 `1-4`                            |
| `image`     | 需要编辑的图片文件，使用 multipart/form-data 上传           |
| `images`    | JSON 图片引用数组，支持 `{"image_url": "https://..."}` |
| `image_url` | 表单模式下也可直接传图片链接，支持重复字段传多张图                     |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/chat/completions</code></summary>
<br>

面向文本、网页搜索与图片场景的 Chat Completions 兼容接口，不是完整通用聊天代理。

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "messages": [
      {
        "role": "user",
        "content": "生成一张雨夜东京街头的赛博朋克猫"
      }
    ],
    "n": 1
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段                   | 说明                                                                           |
|:---------------------|:-----------------------------------------------------------------------------|
| `model`              | 文本、搜索或图片模型；搜索模型会触发网页搜索兼容逻辑                                                   |
| `messages`           | 消息数组，支持文本、搜索和图片请求内容                                                          |
| `n`                  | 图片生成数量，按当前实现解析为图片数量                                                          |
| `stream`             | 文本、搜索和图片场景均支持，仍在测试                                                           |
| `tools`              | 文本场景支持 `web_search` / `web_search_preview` / `web_search_preview_2025_03_11` |
| `web_search_options` | 传入时会触发网页搜索兼容逻辑                                                               |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/responses</code></summary>
<br>

面向文本、网页搜索和图片生成工具调用的 Responses API 兼容接口，不是完整通用 Responses API 代理。

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-5",
    "input": "生成一张未来感城市天际线图片",
    "tools": [
      {
        "type": "image_generation"
      }
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段       | 说明                                                                                      |
|:---------|:----------------------------------------------------------------------------------------|
| `model`  | 响应中会回显该模型字段，搜索和图片生成会走对应兼容逻辑                                                             |
| `input`  | 输入内容；搜索使用最后一条用户文本，图片生成需能解析出提示词                                                          |
| `tools`  | 支持 `image_generation`、`web_search`、`web_search_preview`、`web_search_preview_2025_03_11` |
| `stream` | 已实现，但仍在测试                                                                               |

<br>
</details>
</details>

## 社区支持

学 AI , 上 L 站：[LinuxDO](https://linux.do)

## Contributors

感谢所有为本项目做出贡献的开发者：

<a href="https://github.com/basketikun/chatgpt2api/graphs/contributors">
  <img alt="Contributors" src="https://contrib.rocks/image?repo=basketikun/chatgpt2api" />
</a>

## Star History

[![Star History Chart](https://api.star-history.com/chart?repos=basketikun/chatgpt2api&type=date&legend=top-left)](https://www.star-history.com/?repos=basketikun%2Fchatgpt2api&type=date&legend=top-left)
