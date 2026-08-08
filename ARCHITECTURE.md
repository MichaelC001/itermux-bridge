# 架构与设计评估

## 一、当前状态评估(对照真 tmux)

### 已经足够健壮的部分

| 方面 | 状态 | 依据 |
|---|---|---|
| imsg 协议编解码 | ✅ 可靠 | 对照 tmux 3.6b/3.7b 源码实现,含 `IMSG_FD_MARK` 等易错点;真实 tmux 二进制端到端验证 |
| 握手 / fd 传递 | ✅ 可靠 | SCM_RIGHTS 双 fd、`MSG_EXIT` 4 字节状态、命令客户端不发 `MSG_READY` —— 均有回归测试 |
| tty 接管 | ✅ 正确 | 复刻 `tty_start_tty()` 的 termios 标志,detach 时完整还原(含关鼠标上报) |
| 渲染正确性 | ✅ 良好 | 宽字符按 cell 计宽、`style_at` 按 cell 索引、同步更新消除闪烁、光标不反复隐藏(IME 稳定) |
| session/window/pane 映射 | ✅ 正确 | 索引 vs ID 分离、per-window 索引、ID 持久化 |

### 与真 tmux 的**语义差距**(设计取舍,非 bug)

1. **不是多路复用器,是"视图桥"**
   真 tmux 拥有 PTY,进程生命周期由它管理。本项目只是把 iTerm2 已有的会话**投影**出去 —— iTerm2 退出则一切消失。这是设计目标决定的,不是缺陷。

2. **客户端 resize 不重排布局**
   `on_resize` 只记录不动作。真 tmux 会把所有 pane 重新排版以适配最小客户端。这里刻意不改 iTerm2 的真实窗口(会干扰用户),代价是客户端比 iTerm2 窗口小时内容被裁切。

3. **多客户端共享同一 iTerm2 会话,但各自独立视图**
   每个 peer 有独立的 `scroll_offset` / `copy` 状态(隔离正确),但没有真 tmux 的"多客户端共享同一 window 的 attach 语义"。

### 明确缺失的常用命令

已实现:`attach` `ls` `list-windows` `list-panes` `send-keys` `display-message` `kill-server`

**缺失且常用**:
- `detach-client` / `kill-session` / `kill-window`
- `select-window` / `next-window` / `previous-window`(Ctrl-B n/p 切 window)
- `rename-window` / `rename-session`
- `resize-pane`
- `split-window` 的 `-h/-v` 参数解析(目前只有 prefix 绑定)
- `has-session`(脚本常用,判断是否存在)

## 二、当前的架构问题

```
iterm_backend.py  880 行 / 34 个方法  ← 单一类混杂 6 种职责
```

它同时承担:

1. Gateway 回调分发(`on_attach` / `on_input` / `on_mouse` / …)
2. 鼠标语义(选择、滚动、转发决策)
3. copy-mode 键盘状态机
4. prefix 命令执行(zoom / split / 导航)
5. 屏幕轮询与变化检测
6. iTerm2 API 适配(取内容、取历史、菜单项)

**耦合代价**:改鼠标逻辑要读 880 行;copy-mode 的 bug 反复出现在渲染、轮询、输入三处交叉点;iTerm2 API 细节渗透到状态机里。

**已经做对的**:除 `iterm_backend.py` 外,几乎所有模块**不依赖 iterm2 SDK**,协议层天然可复用。

## 三、拆分方案

按 **"tmux 主体功能" × "复用程度"** 两个维度切:

```
┌─ 第 1 层:tmux 协议(与 iTerm2 完全无关,可独立成库)────────────┐
│  protocol.py      消息类型常量                                  │
│  imsg_codec.py    帧编解码                                      │
│  gateway.py       Unix socket 监听 / accept                     │
│  peer.py          单客户端状态机(握手→attach→detach)          │
│  tty.py           客户端 tty 接管                               │
└─────────────────────────────────────────────────────────────────┘
                          ↓ 依赖
┌─ 第 2 层:终端语义(纯逻辑,不碰任何后端)──────────────────────┐
│  ansi.py          样式 → ANSI 编码 / 屏幕合成                   │
│  layout.py        分屏树 → 屏幕矩形                             │
│  mouse.py         SGR 鼠标序列解析                              │
│  copymode.py      选择区几何 / 文本提取 / OSC 52                │
│  keys.py    【新】prefix 绑定表 + repeat 窗口(从 peer.py 抽出)│
└─────────────────────────────────────────────────────────────────┘
                          ↓ 依赖
┌─ 第 3 层:会话模型(定义"tmux 概念",后端无关)────────────────┐
│  mapper.py        session/window/pane ↔ 后端对象 的 ID 映射     │
│  commands.py      tmux 命令解析与分发                           │
│  backend.py 【新】抽象接口:后端需要提供什么(取内容/发键/分屏)│
└─────────────────────────────────────────────────────────────────┘
                          ↓ 实现
┌─ 第 4 层:iTerm2 适配(唯一依赖 iterm2 SDK 的地方)──────────────┐
│  iterm/api.py     【拆】iTerm2 API 封装:取屏幕/历史/菜单/分屏  │
│  iterm/session.py 【拆】会话与 tab 查找、邻接 pane 计算         │
└─────────────────────────────────────────────────────────────────┘
                          ↓ 组装
┌─ 第 5 层:交互编排(把上面几层粘起来)──────────────────────────┐
│  view.py     【新】屏幕轮询 + 变化检测 + 重绘调度(_pump/_paint)│
│  input.py    【新】输入路由:键盘/鼠标/copy-mode 分派           │
│  actions.py  【新】prefix 动作执行(zoom/split/导航/翻页)      │
└─────────────────────────────────────────────────────────────────┘
```

### 拆分后 `iterm_backend.py` 的去向

| 原方法 | 去处 | 理由 |
|---|---|---|
| `_pump_screen` `_signature` `_paint*` `_history` | **view.py** | 渲染调度是独立关注点,bug 高发区 |
| `on_input` `on_mouse` `on_copy_key` `_handle_mouse` `_mouse_select` `_copy_key` `_esc_timeout` | **input.py** | 输入路由 + copy-mode 键盘状态机 |
| `_prefix` `_zoom` `_neighbour` `_page` `_move_v` | **actions.py** | prefix 动作执行 |
| `_screen_text` `_tab_of` `_bounds_at` `_pane_bounds` | **iterm/session.py** | iTerm2 对象导航 |
| `_send` `_send_raw` + 菜单调用 | **iterm/api.py** | SDK 封装 |

### 关键收益:`backend.py` 抽象接口

定义后端契约后,tmux 协议层就与 iTerm2 解耦:

```python
class Backend(Protocol):
    async def screen(self, pane_id) -> ScreenContents: ...
    async def history(self, pane_id, rows, offset) -> ScreenContents: ...
    async def send_text(self, pane_id, text: str) -> None: ...
    async def split(self, pane_id, vertical: bool) -> str: ...
    async def zoom(self, pane_id) -> None: ...
    def layout(self, window_id) -> SplitTree: ...
```

这样能:
- **单元测试不需要 iTerm2** —— 现在 `test_copymode` / `test_mouse` 里那些手搓的 `_Peer` / `ITermBackend.__new__` 假对象可以换成一个正经的 `FakeBackend`
- 未来接别的后端(比如真 tmux passthrough、或 Terminal.app)只写一个适配层

## 四、建议的边界情况加固(按优先级)

1. **`imsg_codec` 抗恶意输入**:超长 `len`、永不补全的截断帧会让 `_buf` 无限增长。应设上限并断开。
2. **`create_task` 的异常吞噬**:多处 `self.loop.create_task(...)` 没有异常处理,任务里抛异常会静默丢失,peer 状态可能半死不活。
3. **pane 中途消失**:`get_session_by_id` 返回 None 时多数路径只是 `return`,客户端会看到画面冻结而非明确提示。
4. **客户端小于 iTerm2 窗口**:目前裁切。至少应在状态栏提示尺寸不匹配。
5. **补齐 `has-session` / `detach-client` / `select-window`**:脚本化使用的常见依赖。
