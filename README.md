# 微信 AI 聊天助手

Windows 上的本地回复草稿工具：读你**当前打开的那一屏**微信对话，按你的说话习惯
生成三条可以直接发出去的回复，显示在自己的小窗口里 —— 复制、粘贴、发送全部由你手动完成。

- **下载（Windows 10/11 64 位）**：[GitHub Releases](https://github.com/zzzzzzzzyl/wechat-ai-assistant/releases)
  → `wechat-ai-assistant-win64.zip`（约 105 MB，解压后 248 MB，不需要装 Python）
- **介绍页**：[zzzyl.xyz/projects/wxai](https://zzzyl.xyz/projects/wxai/)

**它不做什么**（这是这个项目的红线，也是它和"外挂"的区别）：

- ❌ 不模拟键鼠、不点击微信、不自动复制粘贴、**不自动发送任何消息**
- ❌ 不读写微信数据库、不注入进程、不 hook 网络、不破解加密
- ✅ 只读微信窗口**已经显示出来**的内容（Windows UI Automation）

技术路线与实测数据见下面的探测报告。

---

# 微信 4.0 聊天记录获取 —— 技术探测报告

> 探测日期：**2026-09-23**　｜　环境：Windows / Python 3.11.9 / PC 微信 4.0（`D:\Weixin\Weixin.exe`）
> 本文只记录**实测得到的事实**，不写推测。所有结论都可用 `probes/` 下的脚本重现。

---

## 0. 项目边界（务必先读）

本项目是**本地学习研究**用途，全部代码**只读**：

- ✅ 读取微信窗口已显示的聊天内容、会话列表
- ✅ 监听/统计消息变化
- ✅ 生成 AI 推荐回复并显示在独立窗口
- ❌ **不模拟键鼠、不点击微信、不自动复制粘贴、不自动发送任何消息**
- ❌ 不读写微信数据库文件、不注入进程、不 hook 网络

复制、粘贴、发送全部由用户手动完成。任何后续功能都不得越过这条线。

---

## 1. 环境事实（实测）

| 项目 | 值 |
|---|---|
| 微信版本 | **4.1.13.65**（文件版本；注册表 `HKCU:\Software\Tencent\Weixin` Version=4065598785） |
| 安装路径 | `D:\Weixin\Weixin.exe` |
| 主窗口类名 | **`Qt51514QWindowIcon`** → 微信 4.0 是 **Qt 5.15.14** 应用 |
| UIA 主窗口类 | `mmui::MainWindow`，Name = `Weixin` |
| 主进程 | 持有主窗口的 `Weixin.exe`（本机 pid 5436）；另有多个 `WeChatAppEx.exe` 小程序运行时进程 |
| Python | 3.11.9，已装 `uiautomation 2.0.29` / `pywin32 312` / `comtypes` / `rapidocr_onnxruntime` / `opencv` / `pillow` |
| 屏幕 | 2240 × 1400，单显示器 |

**重要**：网上绝大多数「微信 UIA 取聊天记录」教程针对的是 **3.9 的 `WeChatMainWndForPC`**，
在 4.0 上直接失效。本报告的控件类名（`mmui::*`）只适用于 4.x。

---

## 2. 核心结论

### 2.0 ⚠️ 重要前置结论：微信**不使用** Windows 系统 Toast 通知

原计划用 WinRT `UserNotificationListener` 监听系统通知来做提醒，**实测该方案不可行**。

证据（读 `%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db`）：

- 系统共注册 **344** 个通知来源（AUMID）；
- 里面**有** `QQ`、`BiliBiliPC`、`MSEdge` 等第三方应用 —— 说明这个数据库确实记录了应用通知；
- 但**没有任何** `WeChat` / `Weixin` 主程序的 AUMID，
  唯一的微信相关记录是 `WeixinShare_fv0hcek9q9tbw!WeixinShare`（微信的 UWP「分享」组件，与消息通知无关）；
- 通知表里的历史记录全部来自 Microsoft / Intel / 联想等，**没有一条微信通知**。

→ 微信 4.1.13.65 的消息提醒是**自己绘制的悬浮窗口**，不经过 Windows 通知中心，
因此 `UserNotificationListener` 永远收不到微信消息。

**替代方案（已实测可行）：轮询 UIA 会话列表**

| 维度 | 系统 Toast 监听（原方案） | UIA 轮询会话列表 |
|---|---|---|
| 可行性 | ❌ 微信根本不发 Toast | ✅ 单次约 40ms，实测能捕捉变化 |
| 权限 | 需用户授权通知访问 | 不需要任何特殊权限 |
| 内容 | 通知预览（可能被截断） | 会话名 + 完整预览 + 时间 |
| 窗口状态 | — | 最小化 / 隐藏 / 被遮挡都能读 |
| 覆盖范围 | 微信只在「当前不在该会话」时才提醒 | **所有会话**的最新状态都在视野内 |

→ 建议改用轮询；它比 Toast 方案拿到**更多**信息，且少一层权限依赖。

### 2.1 UIA 方案 —— ✅ 完全可行，且是首选

能稳定读到：

| 内容 | 控件类 / AutomationId | 示例 |
|---|---|---|
| 当前聊天标题 | `mmui::XTextView`，id 含 `current_chat_name_label` | `张三` |
| 消息列表容器 | `mmui::RecyclerListView`，id = `chat_message_list` | — |
| 消息条目 | `mmui::ChatTextItemView` / `mmui::ChatBubbleReferItemView`，Name = 消息正文 | `ohmygod`、`晚安`、`动画表情 [扇耳光]` |
| 会话列表项 | `mmui::ChatSessionCell`，id = `session_item_<会话名>`，Name = 「会话名\n预览\n时间」 | `张三\n[动画表情] 那个\n00:38\n` |
| 输入框 | `mmui::ChatInputField`，id = `chat_input_field`（可用 ValuePattern 读内容） | — |

**最强的一点（实测）**：窗口**最小化**甚至 `SW_HIDE` 完全隐藏时，UIA 依然能读到全部内容：

```
1.当前状态(被遮挡)   4 条消息   9 个会话   159 元素
2.最小化             4 条消息   9 个会话   160 元素
3.隐藏(SW_HIDE)      4 条消息   9 个会话   159 元素
```

→ UIA 与窗口显示状态**无关**，可实现真正的后台常驻，用户无需保持微信窗口可见。

### 2.1.1 能力清单（2026-09-23 复测，`probes/probe_capability.py`）

**✅ 能稳定拿到**

| 信息 | 来源 | 实测样例 |
|---|---|---|
| 会话列表（标题/预览/时间） | `mmui::ChatSessionCell` 的 Name（`\n` 分段） | `张三` / `好了` / `00:58` |
| 会话免打扰标记 | 同上，Name 末段为 `消息免打扰` | `示例24新生群` |
| 当前聊天标题 | `current_chat_name_label` | `张三` |
| 消息正文 | 消息项 Name | `猪猪`、`好了` |
| 消息类型 | 消息项控件类 | `ChatTextItemView`=文本、`ChatBubbleReferItemView`=图片/表情/引用 |
| 消息行位置与高度 | 消息项 `BoundingRectangle` | 高度 `[228, 153, 84, 84]` |
| 输入框内容 | `chat_input_field` + **ValuePattern** | 你此刻正在输入的字 |
| 导航栏与按钮 | `mmui::XTabBarItem` / `mmui::XButton` | `聊天记录`、`截图(Alt+A)`、`发送文件` |

**❌ 拿不到（已逐条验证）**

| 信息 | 原因 |
|---|---|
| 消息是对方还是自己发的 | 消息项 rect 是**整行**；`ItemStatus/ItemType/HelpText/AriaRole` 全空；**不支持 TextPattern** |
| 未读消息**条数** | `mmui::XBadge` 元素存在（导航栏 4 个，各 25×25），但**不含数字**，只能靠会话预览变化推断 |
| 消息级时间戳 | 消息项 Name 里没有时间；只有会话列表有时间 |
| 历史消息 | `mmui::RecyclerListView` 是虚拟列表，只返回已渲染的可见项 |
| 图片/表情的实际内容 | 只有 `[图片]`、`[动画表情]` 占位文本 |

### 2.1.2 性能（`probes/probe_poll.py` 实测，8 轮 @1s）

```
耗时: 最小 28 ms / 平均 40 ms / 最大 80 ms
元素 159 个，无随轮次上升的性能退化 => 无泄漏，可长期运行
期间成功捕捉到内容指纹变化与会话切换 => 可用于新消息检测
```

单次完整读取约 **40ms**，比 OCR（1~3s）快约两个数量级。轮询间隔 1 秒余量极大。

### 2.1.3 发送者区分 —— ✅ 已解决（`probes/probe_sender.py`）

**方法**：UIA 给每行精确 rect → 把微信窗口临时置顶 → 按行截图 → 用该行左右边缘估计背景色
→ 找出与背景差异大的「内容像素」（头像/气泡）→ 统计其在行内左半 / 右半的分布。

**实测结果**（深色主题，会话「张三」，`--thresh 45`）：

| # | 消息 | 左半占比 | 中心比 | 判定 |
|---|---|---|---|---|
| 1 | 动画表情 | 0.95 | 0.23 | 对方 |
| 2 | 谢谢你们 | 1.00 | 0.13 | 对方 |
| 3 | 示例消息三 | 1.00 | 0.20 | 对方 |
| 4 | 示例消息四 | **0.00** | 0.87 | **自己** |
| 5 | 示例消息二 | 1.00 | 0.22 | 对方 |
| 6 | 说要25号前 | 0.94 | 0.19 | 对方 |

区分度极大（对方 0.94~1.00，自己 0.00），判定阈值取 0.75 / 0.25 余量充足。

**交叉验证**：自己那条消息的行内主要颜色含 `(53,210,141)` —— 微信**绿色气泡**；
其余消息是 `(47,47,48)` —— 深色主题下对方的气泡色。

**⚠️ 一个必须记住的坑**：对方**头像**里也可能包含绿色像素
（实测「张三」的头像稳定贡献 1141 个绿像素 ≈ 0.0140，而自己气泡是 0.0751）。
所以**绿色不能当唯一判据**，只适合在左右分布不明确时兜底；主判据始终是左右分布。

**代价**（2026-09-23 更新）：像素分析**需要拿到消息行的像素**，但
**不再要求窗口可见** —— 改用 `PrintWindow(PW_RENDERFULLCONTENT)` 后，
窗口被遮挡、甚至不在前台都照样能判（实测 5/5）。
只有窗口**最小化**时才会放弃判定（那时 `PrintWindow` 只返回空白，用 `is_blank()` 拦掉），
该条消息的 `is_self` 留成 `None`（未知），不做猜测。
取图统一走 `wechat_assistant/capture.py`。

### 2.1.4 群聊 —— UIA 没有昵称，必须靠 OCR（`probe_group.py` / `probe_group_sender.py`）

**先说结论：UIA 在群聊里完全给不出「是谁发的」。**

单聊靠像素只能分「左=对方 / 右=自己」；但群聊左侧可能同时有多个人，必须知道**是谁**。
实测 UIA 树里根本不存在昵称：

- 群聊消息项的**直接子元素 = 0 个**（7 条消息全部为 0）；
- 每条消息行内**没有任何带名字的元素**；
- 全窗口 `TextControl` 只有 5 个：群名、群名、`(4)`、群描述、`发送`。

不过**群聊身份本身是可判定的**：

| 信息 | 来源 | 实测值 |
|---|---|---|
| 是否群聊 | 标题栏控件类 | `mmui::ChatTitleBarChatRoomView`（单聊为 `ChatTitleBarChatSingleView`） |
| 群名 / 成员数 | `current_chat_name_label` 附近 | `😝😜🤪😛😋` + `(4)` |
| 群描述 | `mmui::XTextView` | `示例代购群` |

**昵称只能靠 OCR**，`probe_group_sender.py` 实测成功：

| 消息正文（UIA） | 行高 | OCR 出的昵称 | 正文位置 |
|---|---|---|---|
| 多少钱啊 | 115 | `群友甲` | x=122 左 |
| 我也来a | 115 | `群友甲` | x=122 左 |
| **50半年** | **84** | **无** | x=783 **右** ← 自己发的 |
| 示例消息一 | 115 | `王五` | x=122 左 |
| 可以 | 115 | `群友甲` | x=122 左 |

两条很强的规律，可直接用于解析：

1. **行高本身就是信号**：有昵称的行高 **115**，没有昵称的行高 **84**
   —— 微信在别人的消息上方多画了一行昵称。
2. **昵称固定在行内左上**（x≈104, y≈15），正文在其下方（x≈122, y≈61）。

→ **群聊最终方案：UIA 取正文（精确）+ 行高判断有无昵称 + OCR 取昵称 + 像素判左右。**

### 2.2 ⚠️ 最容易踩的坑：必须用 FindAll，不能用 TreeWalker

这是本次探测**最关键的技术发现**：

```python
# ❌ 错：走 RawViewWalker 逐级遍历，在自绘列表处会断掉
children = control.GetChildren()      # uiautomation 内部 = GetFirstChildControl + GetNextSiblingControl

# ✅ 对：一次性 FindAll，能拿到全部后代
uia = core._AutomationClient.instance().IUIAutomation
arr = control.Element.FindAll(4, uia.CreateTrueCondition())   # 4 = TreeScope_Descendants
```

- 用 `GetChildren()` 递归：只看到 **78** 个元素，`ChatMessagePage` 和 `session_list` 下面**空无一物**，
  会让人误判「微信 4.0 不暴露消息，UIA 路线作废」。
- 用 `FindAll(Descendants)`：看到 **170** 个元素，消息文本、会话项全都在。

原因：Qt 的 UIA provider 为自绘虚拟列表实现了 `FindAll`，但**没有实现 walker 遍历**。

### 2.3 OCR 方案 —— ⚠️ 只能作补充，不能当主力

实测（`probes/test_capture.py`、`probes/probe_printwindow.py`、`probes/verify_printwindow_live.py`）：

| 方式 | 结果 |
|---|---|
| 屏幕截取 `ImageGrab` | 能截到微信内容，**但窗口被遮挡时截到的是遮挡物** |
| `PrintWindow(PW_RENDERFULLCONTENT=2)` | ✅ **完全可用，且不受遮挡影响** |
| `PrintWindow(PW_CLIENTONLY=1)` | 整幅空白，无效 |
| `PrintWindow(默认 flag=0)` | 全黑，无效 |

> **2026-09-23 更正**：本节早先记的结论是「`flag=2` 对 Qt 窗口**部分有效**，
> 被遮挡区域会混入屏幕像素」—— 这个结论**是错的**，已实测推翻。
> 在微信**非前台且聊天区被浏览器和悬浮窗同时盖住**的状态下，
> `flag=2` 抓到的内容与 UIA 读到的文本 **5/5 完全对上**（连最新一条都在，
> 说明是**实时**内容）；同一时刻屏幕截图的命中率是 **0/5**，
> 而且 OCR 出来的是**别的程序窗口的文字**。
> 两者都不是「混入」，而是各自截到了各自该截的东西。
>
> 这条更正很重要：发送者判定（依赖消息行像素）此前因为怕遮挡而
> 在窗口被盖住时**整体放弃**，导致 `is_self` 全为 `None`、
> 「同侧连续消息合并成回复目标块」静默失效。现在改用 `flag=2` 后不再有此问题。
> 唯一仍会失败的情况是窗口**最小化**（那时 `flag=2` 只拿到空白，用 `is_blank()` 拦掉）。

另外还有一个隐蔽陷阱：若微信窗口部分**超出屏幕**（实测时右边界 2262 > 屏幕宽 2240），
**屏幕截图**尺寸与实际可渲染区域不匹配，也会产生错乱内容
（`PrintWindow` 不受此影响，它按窗口自身的尺寸出图）。

RapidOCR 本身效果不错（中文识别准、带四点坐标框），
但把它架在**屏幕截图**上就会受遮挡和窗口位置影响太大；
架在 `PrintWindow` 上则稳定得多（`ocr.py` 仍是唯一允许创建 OCR 引擎的地方）。

### 2.4 方案对比

| 维度 | UIA | OCR |
|---|---|---|
| 消息文本 | ✅ 精确、零误差 | ⚠️ 可能识别错字 |
| 会话列表（含预览/时间） | ✅ 结构化 | ⚠️ 需要靠坐标切分 |
| 消息类型 | ✅ 靠控件类区分 | ⚠️ 只能靠文字与图标 |
| **抗遮挡 / 后台运行** | ✅ **完全不受影响** | ❌ 必须窗口可见且无遮挡 |
| 发送者左右区分 | ❌ UIA 单靠自己拿不到 → 需配合截图，见 2.1.3 | ✅ 气泡像素坐标可判左右 |
| 图片消息内文字 | ❌ 只有 `[图片]` 占位 | ⚠️ 可识别 |
| 群聊发送者昵称 | ❌ UIA 完全没有，见 2.1.4 | ✅ OCR 实测可识别 |
| 速度 | ✅ 极快（160 元素约 0.3s） | ❌ 慢（每次 1~3s） |

**建议架构：UIA 为主（取文本和结构化信息） + OCR 为辅（补气泡左右与图片内文字）**，
且 OCR 只在需要时按需触发。

---

## 3. 脚本清单

全部在 `probes/` 下，均可独立运行，且**只读**。

| 脚本 | 作用 |
|---|---|
| `probe_uia.py` | 列出顶层窗口、定位微信主窗口并 dump UIA 树（带子线程超时保护） |
| `probe_uia_deep.py` | 用 `FindAll(Descendants)` + MSAA 做深度验证（就是它推翻了初始误判） |
| `probe_chat.py` | **核心**：读取当前聊天消息、会话列表、输入框内容 |
| `probe_capability.py` | **能力总览**：一次列出窗口信息、控件分布、会话、消息、输入框、后台可读性 |
| `probe_poll.py` | 轮询性能与稳定性测试（验证实时监听可行性） |
| `probe_sender.py` | **发送者区分**：UIA 行 rect + 截图像素分析判定「对方/自己」 |
| `probe_group.py` | 群聊 UIA 结构探测（证明 UIA 无昵称） |
| `probe_group_sender.py` | **群聊发送者**：UIA 正文 + OCR 昵称 + 行高规律 |
| `probe_msg_attrs.py` | dump 消息项的全部 UIA 属性（用于找发送者线索） |
| `probe_textpattern.py` | 验证消息项是否支持 TextPattern（结论：不支持） |
| `probe_ocr.py` | 截图 + RapidOCR 识别，与 UIA 做对比 |
| `ocr_image.py` | 对任意图片跑 OCR，输出文本 + 坐标 |
| `test_capture.py` | 对比 PrintWindow / 屏幕截取的抗遮挡能力（带 OCR 自动判定） |
| `test_background.py` | 验证最小化/隐藏状态下 UIA 是否仍可读 |

运行（用系统 Python 即可，依赖已齐）：

```powershell
$env:PYTHONUTF8="1"
python probes\probe_chat.py        # 读聊天记录
python probes\test_background.py   # 验证后台可读性
```

> ⚠️ 若用 `>` 重定向输出到文件，Windows PowerShell 5.1 会写成 UTF-16 导致中文"乱码"。
> 脚本本身会把结果以 UTF-8 存到 `out/`，直接看那个文件即可。

## 4. 另一个坑：不要用 GetRootControl().GetChildren() 枚举顶层窗口

实测该调用在**本机 60 秒不返回**（`GetRootControl()` 本身很快，卡在 `GetChildren()`）。

原因：机器上同时存在 11 个 `WeChatAppEx.exe`（小程序运行时）等窗口，其中某个窗口的
UIA provider 无响应，就会把全量枚举挂死。

**正确做法**：用 `win32gui.EnumWindows` 枚举 HWND（秒回），再 `ControlFromHandle(hwnd)` 精确定位。
项目里所有脚本都已按此实现，并给 UIA 调用加了子线程超时保护。

---

## 5. 尚未验证 / 下一步

1. **历史消息加载** —— 消息容器是 `mmui::RecyclerListView`（虚拟列表），
   已确认只返回当前渲染的可见项。需要验证手动向上滚动后能否读到更多。
2. **新消息检测规则** —— `probe_poll.py` 已证明轮询（约 40ms/次）能捕捉内容指纹变化，
   但还需确定「变化 → 新消息」的具体判据（会话预览变化？消息条数变化？）。
3. **发送者区分的实时化** —— 见 2.1.3 / 2.1.4，目前需要窗口可见；
   需评估「只截取新出现的消息行」以降低成本与干扰。
4. **群聊昵称 OCR 的稳定性** —— 目前只在一个 4 人群、5 条消息上验证过；
   需要更多样本（长昵称、emoji 昵称、@提及、群成员改名片）确认鲁棒性。
5. **图片/表情消息处理** —— 目前只有 `[图片]`、`[动画表情]` 占位，需决定是否 OCR 图片内容。

---

## 6. 参考：一次真实抓取输出

```
【当前聊天】标题: '张三'
消息列表容器: class=mmui::RecyclerListView id=chat_message_list rect=(1353, 534, 2178, 1053)

消息项共 7 条:
   1. 'ohmygod'              class=mmui::ChatTextItemView
   2. '好美味的样子'           class=mmui::ChatTextItemView
   3. '不看了'                class=mmui::ChatTextItemView
   4. '你好好洗澡'             class=mmui::ChatTextItemView
   5. '晚安'                  class=mmui::ChatTextItemView
   6. '?'                    class=mmui::ChatTextItemView
   7. '动画表情 [扇耳光]'       class=mmui::ChatBubbleReferItemView

【会话列表】共 9 个会话
  张三                    | [动画表情] 那个                              | 00:38
  🈲示例23班乙群🈲        | 群友丙: [图片]                              | 昨天 22:22
  示例24新生群      | 群友乙: 请相关同学按时提交材料    | 昨天 21:40
  ...
```
