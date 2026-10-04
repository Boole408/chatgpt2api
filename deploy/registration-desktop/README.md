# 注册浏览器的人工接管桌面

给 Docker 后端提供可见 Chromium 桌面，供管理员手动完成安全验证。
遇到安全验证时，批次暂停并保留原浏览器会话；验证后在任务页点击“已完成验证，继续”。

在项目目录启动（生产 Compose 中的后端服务名须为 `app`）：

```sh
export REGISTRATION_DESKTOP_BASE_IMAGE=$(docker inspect chatgpt2api-free --format '{{.Config.Image}}')
docker compose -f compose.production.yml -f deploy/registration-desktop/compose.yml build registration-desktop
docker compose -f compose.production.yml -f deploy/registration-desktop/compose.yml up -d --no-build
```

接码设置的浏览器选择 **Chromium**。Roxy 的本地 API 地址指向运行后端的主机。

在自己电脑建立 SSH 隧道：

```sh
ssh -N -L 127.0.0.1:16080:127.0.0.1:16080 root@YOUR_SERVER
```

保持 SSH 连接，打开 `http://127.0.0.1:16080/vnc.html?autoconnect=1&resize=scale`。
这里是可交互桌面，任务诊断截图仅供查看。

noVNC 端口仅绑定服务器 loopback，VNC 端口仅在容器内部 loopback 监听。
SSH 负责访问认证；不要将这些端口暴露到公网或挂到未认证的反向代理。
任何能进入该桌面的人均可操作当前登录会话。

以后重启或升级时，继续传入两份 Compose 文件，以保留桌面环境和 DISPLAY 设置。
启动时先等待桌面健康，再创建注册任务。只运行一个后端实例。

后端与 Xvfb 必须共享 IPC（`app.ipc: shareable` 和
`registration-desktop.ipc: service:app`）。只共享 X11 socket 时，复杂页面可能无法
绘制，导致截图超时以及 Playwright 点击持续等待按钮稳定。

## 通过 HTTPS 域名访问

也可以在已有 HTTPS Nginx 的 server 块中包含 `nginx-location.conf`。
先创建独立的桌面登录密码文件 `/etc/nginx/.htpasswd-registration-desktop`，
仅允许 root 和 Nginx 工作进程用户读取；不要复用 SSH 或项目管理员密码。
运行 `nginx -t` 后再 reload。

访问 `https://YOUR_DOMAIN/registration-desktop/vnc.html?autoconnect=1&resize=scale&path=registration-desktop/websockify`。
确认未登录访问返回 401，登录后页面和 WebSocket 均可用。
密码不应放入 URL；原始 Docker 端口仍只绑定 loopback。
