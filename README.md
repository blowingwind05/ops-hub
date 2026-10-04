# Ops Hub · 服务器运维中枢

以当前机器为管理入口，逐步统一多服务器状态监控、远程控制，以及 Hermes / Codex 辅助运维。当前已实现本机和 SSH 网页终端，以及本机文件管理；远程文件管理、服务器状态监控和智能运维入口为后续扩展方向。

左侧导航切换“终端”和“文件”工作区，切换面板时终端会话继续运行。顶栏的机器名称用于选择当前工作区的目标服务器。

## 本机与 SSH 终端

浏览器使用本地保存的 xterm.js 5.5.0 和 FitAddon 0.10.0，后端在项目独立的 `.venv` 环境中使用 aiohttp，将 WebSocket 输入输出接入 Linux PTY，直接运行当前用户的登录 shell。

点击顶栏的机器名称打开服务器下拉菜单，列表读取当前用户的 `~/.ssh/config` 中明确的 `Host` 别名，也支持 `Include` 文件和多个别名。通配符和排除模式不会作为服务器列出；修改配置后点击菜单中的“刷新”即可更新列表。选择机器时优先切换到它已有的可用会话，没有会话则新建终端。页面中的“新建终端”会为顶栏显示的机器创建额外会话；本机和多台远程服务器可以同时打开并切换。菜单支持方向键、Home、End 和 Esc，手机上同样可以点击机器名称选择。

远程终端使用系统 OpenSSH 客户端，通过 PTY 执行 `ssh -tt -- <Host 别名>`。用户名、端口、密钥、跳板机以及其他连接设置由 SSH 按原配置处理。密码、私钥口令和首次连接的主机指纹确认直接在终端中交互输入。侧栏“终端就绪”表示 SSH 客户端已启动，是否完成认证以终端输出为准。连接失败或退出时保留输出，不会转入本机 shell。

SSH 配置和认证由系统 OpenSSH 客户端处理，应用不另行存储 SSH 密码或密钥。

访问 `http://127.0.0.1:8088`。支持交互程序、窗口缩放、复制粘贴、中断命令及多个独立终端。

“新建终端”追加一个会话，右侧列表用于切换和单独关闭。切换会保留进程、输出和 shell 状态，后台终端继续运行。列表显示各终端当前前台程序名，如 `bash`、`htop`、`vim`，约每半秒更新一次。手机上列表显示为横向标签。页面最多保留 8 个会话，服务最多同时运行 8 个终端；关闭或刷新页面会结束该页面的所有终端。

右上角可切换深色或浅色模式，页面和所有终端同步配色。主题选择保存在当前浏览器中，切换主题不会重建终端会话。

## 本机文件管理

在左侧选择“文件”，通过路径栏、文件夹列表和上一级按钮浏览本机目录。支持文件名筛选、显示隐藏文件、查看大小、修改时间和权限，以及上传、下载、新建文件或文件夹、编辑文本、重命名和删除。访问范围和操作权限与运行服务的系统用户一致，路径栏支持绝对路径和 `~/`。

文本编辑支持不超过 2 MiB 的 UTF-8 普通文件，提供 Ctrl/Cmd+S 保存，保留 CRLF 文件的换行格式。保存前检查文件版本并创建备份；文件被其他程序修改时拒绝覆盖，未保存的编辑内容仍留在编辑器中。上传支持实时进度条、当前文件百分比和多文件队列状态，传输结束后等待服务器保存结果。单个上传文件限制为 256 MiB，上传、新建和重命名都不会覆盖已有的同名文件。

删除操作将文件或整个文件夹移入回收站；编辑前的版本也可在“回收站”中找到。恢复支持原路径或指定新路径，已有文件不会被覆盖。回收站保存于配置的终端起始目录下的 `.local/share/ops-hub/trash`，不自动清空。移入回收站需要源文件和回收站位于同一文件系统；跨文件系统时拒绝操作并保留源文件。

符号链接可重命名或移入回收站，链接到目录时可打开目标目录；编辑和下载仅支持普通文件。当前文件管理仅支持本机，选择远程服务器时显示尚未接入；远程终端仍可正常使用。

## 安装与运行

页面采用 VS Code 风格的中性灰背景与蓝色交互提示，浅色模式使用相近的柔和灰色层次，终端的 ANSI 色板参考 VS Code。原绿色方案记录在 `design/themes/green-original.json`，前一版灰蓝方案记录在 `design/themes/vscode-classic.json`，配色说明见 [design/themes/README.md](design/themes/README.md)。

左上角品牌标识和浏览器标签页图标共用 [static/ops-hub.svg](static/ops-hub.svg)，采用 [Lucide 的 network 图标](https://lucide.dev/icons/network)，以树形层级表达服务器管理关系。保留上游图形，仅将描边设为页面的蓝色；修改该 SVG 即可同时更新两处图标。上游原始 SVG 和 ISC 许可证保存在 `static/vendor/lucide/`，页面直接加载本地 SVG。

应用按配置文件监听地址，并验证客户端来源、请求的 Host 和 WebSocket Origin。命令以运行服务的系统用户身份执行。没有保存或录制终端输入。

运行环境：Linux、Python 3.12，以及用于远程连接的 OpenSSH 客户端。

在项目目录中创建虚拟环境并安装依赖：

```bash
cd ops-hub
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

`requirements.txt` 声明直接依赖，`requirements.lock` 固定已验证的完整依赖版本。

复制配置示例并按部署环境修改：

```bash
cp config.example.toml config.toml
```

`config.toml` 已加入 `.gitignore`，仓库仅保留通用的 `config.example.toml`。

运行：

```bash
.venv/bin/python server.py
```

默认读取项目目录中的 `config.toml`；文件不存在时使用本机访问的默认配置。也可以指定配置文件：

```bash
.venv/bin/python server.py --config /path/to/config.toml
```

| 配置项 | 用途 |
| --- | --- |
| `server.listen` | 监听 IP 列表，支持 IPv4、IPv6 和通配监听地址 |
| `server.port` | HTTP 和终端 WebSocket 共用的端口 |
| `access.allowed_hosts` | 浏览器访问时允许使用的 IP 或主机名，填写时不带端口 |
| `access.allowed_clients` | 允许连接的客户端 IP 或 CIDR 网段 |
| `terminal.directory` | 终端起始目录和文件面板的默认目录 |
| `terminal.ssh_config` | 用于发现和连接远程服务器的 SSH 配置文件 |

允许远程访问时，配置监听 IP、访问主机名和客户端范围。访问地址为 `http://<IP 或允许的主机名>:<端口>`，URL 中的 IPv6 地址需使用方括号。路径支持 `~`，相对路径以配置文件所在目录为基准。配置修改后重启服务生效。

验证环境和后端：

```bash
.venv/bin/python -m pip check
.venv/bin/python -m unittest -v test_config.py test_server.py test_files.py
```

前端 xterm.js 组件使用 MIT 许可，Lucide 图标附带 ISC / MIT 许可，组件和完整许可文本保存在 `static/vendor/`。
