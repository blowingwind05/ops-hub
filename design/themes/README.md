# Ops Hub 配色记录

当前页面采用 VS Code 风格的灰色层次和蓝色交互提示，同时提供深色与浅色模式。浅色模式改为相近的柔和灰色背景，减少顶栏、侧栏与终端的灰白反差。连接状态独立使用成功、等待、错误色，终端 ANSI 色板参考 VS Code。

参考来源：[VS Code 深色主题](https://github.com/microsoft/vscode/blob/main/extensions/theme-defaults/themes/dark_vs.json)、[浅色主题](https://github.com/microsoft/vscode/blob/main/extensions/theme-defaults/themes/light_vs.json)、[终端颜色定义](https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/terminal/common/terminalColorRegistry.ts)。页面按现有布局适配，不是完整复制 VS Code 界面。

## 原绿色方案

`green-original.json` 保存了修改前的完整配色：

- `saved_at`：保存时间，使用 Asia/Shanghai 时区。
- `ui`：深色、浅色页面的 CSS 变量。
- `terminal`：深色、浅色 xterm.js 主题。
- `stylesheet`：修改前的完整 `static/style.css` 内容，包含控件上的独立颜色。
- `favicon`：修改前的图标标签。

`vscode-classic.json` 使用相同字段，记录柔化之前的灰蓝配色。

恢复配色时，优先按 `ui` 中的颜色更新当前 CSS 变量，并将 `terminal` 替换到 `static/app.js` 中的 `themes` 对象；旧方案的 `--green` 对应当前的 `--accent`。`stylesheet` 和 `favicon` 保留作完整参考。页面结构发生变化后，请保留新控件的样式，不要直接用旧 CSS 覆盖整个文件。恢复配色不会改变浏览器中的深浅模式偏好。
