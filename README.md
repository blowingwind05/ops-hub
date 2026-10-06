# Ops Hub · 服务器运维中枢

以一台机器为入口，在浏览器中使用本机和远程 SSH 终端、管理多台服务器的文件。

## 安装与运行

需要 Linux、Python 3.12 和系统 OpenSSH 客户端。远程文件管理还需要目标服务器启用 SFTP。

在项目目录中创建独立环境、安装依赖并复制配置示例：

```bash
cd ops-hub
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
cp config.example.toml config.toml
```

按部署环境修改 `config.toml`，然后启动：

```bash
.venv/bin/python server.py
```

默认访问 `http://127.0.0.1:8088`。也可以通过 `--config /path/to/config.toml` 指定配置文件；配置修改后重启服务生效。`config.toml` 不纳入版本控制。

## 配置

| 配置项 | 用途 |
| --- | --- |
| `server.listen` | 监听 IP 列表 |
| `server.port` | 网页与终端服务端口 |
| `access.allowed_hosts` | 允许访问的 IP 或主机名，不带端口 |
| `access.allowed_clients` | 允许连接的客户端 IP 或 CIDR 网段 |
| `terminal.directory` | 终端起始目录和本机文件面板的默认目录 |
| `terminal.ssh_config` | 服务器列表及 SSH 连接使用的配置文件 |
| `files.upload_limit_mib` | 单个上传文件的大小上限（MiB），默认 `0` 表示不限大小 |

允许远程访问时，同时配置监听地址、访问主机名和客户端范围。路径支持 `~`，相对路径以配置文件所在目录为基准。完整示例见 [config.example.toml](config.example.toml)。

## 使用

左侧导航切换“终端”和“文件”，顶栏机器名称用于选择本机或远程服务器。服务器列表读取 SSH 配置中的明确 `Host` 别名；修改 SSH 配置后点击菜单中的“刷新”。

### 终端

点击“新建终端”创建当前机器的会话，通过会话列表切换或关闭。切换机器和工作区时会话继续运行；刷新或关闭页面会结束该页面的终端。

远程终端沿用 SSH 配置中的用户名、密钥、端口和跳板机，密码及首次连接的主机指纹确认在终端中完成。

### 文件

- 点击文件夹或地址栏目录层级浏览，点击地址栏编辑图标输入绝对路径或 `~/` 路径。
- 支持搜索文件名、显示隐藏文件、新建、重命名、移动、上传下载和文本编辑。
- 将列表中的文件或文件夹拖入文件夹或地址栏目录层级即可移动；从电脑拖入文件即可上传，目前不支持拖入文件夹。
- 文本编辑支持 UTF-8 文件和 Ctrl/Cmd+S 保存，大小上限为 2 MiB；上传默认不限大小，可通过配置设置上限。
- 删除的文件和编辑前的备份进入对应机器的回收站，可恢复、彻底删除或一键清空。恢复和上传不会覆盖同名文件。

远程文件管理使用 SSH 密钥或 SSH agent 认证。首次连接请先在终端核验主机指纹；远程文本保存需要 SFTP 服务支持 OpenSSH 原子重命名扩展。文件访问权限与运行服务的本机用户或 SSH 登录用户一致。

右上角可切换深色与浅色模式。

## 仓库结构

```text
ops-hub/
├── server.py              # HTTP 服务、访问控制和终端会话
├── configuration.py       # 配置读取与校验
├── file_common.py         # 文件服务的共用规则与辅助函数
├── file_manager.py        # 文件 API 路由与本机文件操作
├── remote_files.py        # OpenSSH / SFTP 连接与远程文件操作
├── config.example.toml    # 配置示例
├── requirements.txt       # 直接依赖
├── requirements.lock      # 固定依赖版本
├── static/                # 页面、样式、脚本和第三方资源
├── design/themes/         # 配色记录
└── test_*.py              # 后端测试
```

## 开发验证

```bash
.venv/bin/python -m pip check
.venv/bin/python -m unittest -v
```

远程文件测试需要本机安装 `/usr/lib/openssh/sftp-server`，未安装时跳过。

第三方前端资源的许可证保存在 `static/vendor/`。
