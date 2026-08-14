# 尚未实现的 tmux 基本操作

对照 tmux 3.7b 的默认 prefix 绑定表(`tmux list-keys -T prefix`)与常用命令逐项核对。

## 已实现

| 绑定 | 命令 | 说明 |
|---|---|---|
| `d` | `detach-client` | 分离 |
| `z` | `resize-pane -Z` | 放大/还原 pane |
| `o` | `select-pane -t :.+` | 下一个 pane |
| `←↑↓→` / `hjk` | `select-pane -LDUR` | 方向选 pane(含 `bind -r` 连按) |
| `"` / `%` | `split-window` / `-h` | 分屏 |
| `x` | `kill-pane` | 关闭 pane |
| `n` / `p` | `next-window` / `previous-window` | 切 window |
| `[` | `copy-mode` | 进入复制模式 |
| `PgUp`/`PgDn`、`u`/`e`、`C-u`/`C-d` | — | 翻 scrollback |
| `m` | `set -g mouse` | 鼠标开关 |
| `C-b` | `send-prefix` | 发送字面前缀 |

命令:`attach` `ls` `list-windows` `list-panes` `send-keys` `has-session`
`new-session`(`-d` / `-s <name>`)`detach-client`
`select-window`/`next-window`/`previous-window` `display-message`

**`new-session` 曾经是个 bug**:它被放在 `ATTACH_CMDS` 里,所以既不创建
也不报错,只是静默 attach 到一个已有的 pane。现在它真的会新建一个 iTerm2
window(`Window.async_create`)。注意与 `kill-*` 的**不对称**是有意的:
创建是用户显式请求的,销毁则会毁掉桥并不拥有的终端。

---

## 一、本轮已补上 ✅

| 绑定 | 操作 | 实机验证 |
|---|---|---|
| `c` | `new-window`(新建 iTerm2 tab 并跟随) | ✅ 8→9 个 tab |
| `0`–`9` | 按序号跳 window | ✅ |
| `l` | `last-window`(回到上一个 window) | ✅ |
| `;` | `last-pane`(上一个 pane) | ✅ 状态已记录 |
| `C-←↑↓→` | `resize-pane`(可连按) | ✅ 15→17 行 |

**注意 `l` 的语义变更**:之前它是 vi 风格的"选右边 pane",现在改回
tmux 的 `last-window`。向右选 pane 请用**箭头键**(`Ctrl-B →`)。
这是有意对齐 tmux,避免老用户的肌肉记忆出错。

`resize-pane` 的实现是设置 `Session.preferred_size` 再 `async_update_layout()`。
实测生效,但**回弹不精确**(15→17→14):iTerm2 的布局约束会重新分配剩余空间,
不像 tmux 那样精确可逆。属于后端差异,不是 bug。

### 仍未做

**`,` — `rename-window`**:iTerm2 API 齐备(`async_set_name`),但需要
"命令提示行"输入 UI(见 §三)。当前按键会提示从外部命令行改。

**`!` — `break-pane`**:iTerm2 没有"把 session 移到新 tab"的 API,
无法忠实实现。当前按键会明确说明不支持。

**`space` — `next-layout` / `M-1`…`M-5` — 预设布局**:`async_update_layout`
理论可行,但要自己算 even-horizontal / tiled 等布局的几何。工作量中等,
收益一般。

---

## 二、可以实现但语义要想清楚

### `&` — `kill-window` / `kill-session`
**当前刻意拒绝**:桥不拥有 iTerm2 终端的生命周期,杀掉会毁掉用户真实的
工作,所以改成 detach 并说明。这是设计取舍,不是遗漏。

### `t` — 时钟、`?` — `list-keys`、`~` — `show-messages`
纯 UI 类,可以做但价值低。`?` 列快捷键对新用户有帮助。

### `(` `)` `L` — `switch-client`(切换 client 挂载的 session)
本项目里"session = iTerm2 window",切 session 约等于切 window。
可以映射,但语义会和 tmux 有微妙差异。

---

## 三、结构性缺失(不是单个命令)

### 1. 命令提示行(`:` — `command-prompt`)
tmux 的 `Ctrl-B :` 打开命令行,是 `rename-window`、`find-window`、
`move-window` 等一大批操作的入口。**没有它,那些命令只能从外部
`tmux -S ... <cmd>` 调用,不能在 attach 状态下用。**

实现需要:在客户端屏幕底部画一行输入区、自己处理按键编辑、回车后
走现有的 `commands.dispatch`。这是补齐一大片功能的**杠杆点**。

### 2. paste buffer(`]` `#` `=` `-`)
tmux 有自己的剪贴板缓冲区栈。本项目的 copy-mode 直接走 **OSC 52 进系统
剪贴板**(见 README),这是刻意的选择 —— 桥内部的 buffer 无处可粘。
所以 buffer 系列命令不打算实现。

### 3. `choose-*` 交互式选择器(`s` `w` `D` `=`)
`Ctrl-B s` 选 session、`Ctrl-B w` 选 window 都是常用操作,但需要一整套
可交互的列表 UI(上下选择、回车确认)。属于较大工程。

### 4. `.tmux.conf` 解析
自定义绑定、`set -g` 选项。当前所有绑定硬编码在 `keys.py`。
做的话应该先支持 `bind-key` 和少量 `set-option`。

---

## 四、明确不打算做

| 项 | 原因 |
|---|---|
| control mode (`-CC`) | 目标是让**标准 tmux 客户端**能连,不是给上层程序接管渲染 |
| `respawn-pane` / `respawn-window` | 桥不拥有进程生命周期 |
| `display-menu` / `customize-mode` | 重度 UI,与桥的定位不符 |
| 跨机器 session 迁移 | tmux 生态边缘功能 |

---

## 剩余优先级

1. **命令提示行 `Ctrl-B :`** — 杠杆最大,一次打开一大片命令
   (`rename-window`、`find-window`、`move-window`…)
2. `choose-session` / `choose-window`(`Ctrl-B s` / `w`)— 工程量大,视需要
3. `.tmux.conf` 解析 — 让绑定可自定义
4. 预设布局(`space`、`M-1`…`M-5`)— 需自己算几何
