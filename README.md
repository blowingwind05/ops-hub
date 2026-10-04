# Ops Hub · 服务器运维中枢

以当前机器为管理入口，逐步统一多服务器状态监控、远程控制，以及 Hermes / Codex 辅助运维。当前已实现本机和 SSH 网页终端；服务器状态监控和智能运维入口为后续扩展方向。

## 本机与 SSH 终端

浏览器使用本地保存的 xterm.js 5.5.0 和 FitAddon 0.10.0，后端在项目独立的 `.venv` 环境中使用 aiohttp，将 WebSocket 输入输出接入 Linux PTY，直接运行当前用户的登录 shell。

点击顶栏的机器名称打开服务器下拉菜单，列表读取当前用户的 `~/.ssh/config` 中明确的 `Host` 别名，也支持 `Include` 文件和多个别名。通配符和排除模式不会作为服务器列出；修改配置后点击菜单中的“刷新”即可更新列表。选择机器时优先切换到它已有的可用会话，没有会话则新建终端。页面中的“新建终端”会为顶栏显示的机器创建额外会话；本机和多台远程服务器可以同时打开并切换。菜单支持方向键、Home、End 和 Esc，手机上同样可以点击机器名称选择。

远程终端使用系统 OpenSSH 客户端，通过 PTY 执行 `ssh -tt -- <Host 别名>`。用户名、端口、密钥、跳板机以及其他连接设置由 SSH 按原配置处理。密码、私钥口令和首次连接的主机指纹确认直接在终端中交互输入。侧栏“终端就绪”表示 SSH 客户端已启动，是否完成认证以终端输出为准。连接失败或退出时保留输出，不会转入本机 shell。

SSH 连接由系统 OpenSSH 客户端处理，应用不保存密码，也不直接读取私钥内容。

访问 `http://127.0.0.1:8088`。支持交互程序、窗口缩放、复制粘贴、中断命令及多个独立终端。

“新建终端”追加一个会话，右侧列表用于切换和单独关闭。切换会保留进程、输出和 shell 状态，后台终端继续运行。列表显示各终端当前前台程序名，如 `bash`、`htop`、`vim`，约每半秒更新一次。手机上列表显示为横向标签。页面最多保留 8 个会话，服务最多同时运行 8 个终端；关闭或刷新页面会结束该页面的所有终端。

右上角可切换深色或浅色模式，页面和所有终端同步配色。主题选择保存在当前浏览器中，切换主题不会重建终端会话。

页面采用 VS Code 风格的中性灰背景与蓝色交互提示，浅色模式使用相近的柔和灰色层次，终端的 ANSI 色板参考 VS Code。原绿色方案记录在 `design/themes/green-original.json`，前一版灰蓝方案记录在 `design/themes/vscode-classic.json`，配色说明见 [design/themes/README.md](design/themes/README.md)。

左上角品牌标识和浏览器标签页图标共用 [static/ops-hub.svg](static/ops-hub.svg)，采用 [Lucide 的 network 图标](https://lucide.dev/icons/network)，以树形层级表达服务器管理关系。保留上游图形，仅将描边设为页面的蓝色；修改该 SVG 即可同时更新两处图标。上游原始 SVG 和 ISC 许可证保存在 `static/vendor/lucide/`，页面直接加载本地 SVG。

后端仅监听 `127.0.0.1`，并验证请求的 Host 和 WebSocket Origin。命令以运行服务的本机用户身份执行。没有保存或录制终端输入。

运行环境：Linux、Python 3.12，以及用于远程连接的 OpenSSH 客户端。

在项目目录中创建虚拟环境并安装依赖：

```bash
cd ops-hub
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

`requirements.txt` 声明直接依赖，`requirements.lock` 固定已验证的完整依赖版本。

可在 `server.py` 中配置终端起始目录 `HOME_DIR` 和监听端口 `PORT`。

运行：

```bash
.venv/bin/python server.py
```

验证环境和后端：

```bash
.venv/bin/python -m pip check
.venv/bin/python -m unittest -v test_server.py
```

前端 xterm.js 组件使用 MIT 许可，Lucide network 图标使用 ISC 许可，组件及许可保存在 `static/vendor/`。
