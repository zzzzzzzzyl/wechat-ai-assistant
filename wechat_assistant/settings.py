# -*- coding: utf-8 -*-
"""分层配置：读写用户设置，同时**不破坏 `config.yaml` 里的中文注释**。

为什么不能直接改写 config.yaml
------------------------------
`config.yaml` 里每一段都在解释"这个值为什么是这个数"（OCR 的线程数、
滚动容差为什么是 4、YAML 的 on/off 陷阱……）。这份注释是这个项目的教学价值所在。
而 YAML 的写回（`yaml.dump`）**会把注释全部抹掉**，还会打乱键序。

所以改成三层：

    config.yaml          出厂默认 + 中文注释        【程序永不写入】
          ↓ 被覆盖
    state/settings.json  用户在设置界面改的值       【只写这里】
          ↓ 被覆盖
    secrets.yaml         API Key                  【只做行级替换，保注释】

三个好处：
  1. `config.yaml` 的注释永远在；
  2. **"恢复默认"是真的能做到** —— 删掉 settings.json 里的那一项，默认值就透出来；
  3. 以后升级换了 `config.yaml` 的默认值，你的覆盖仍然生效，不会被钉死在旧值上。

`state/settings.json` 放在 `state/` 下，是因为那个目录**已经在 .gitignore 里**
（见 paths.settings_path 的说明），不用再动忽略规则。
"""

import json
import os
import shutil
from typing import Any, Dict, List, Optional

import yaml

from . import paths
from . import prompts
from .config import Config, _deep_merge

__all__ = ["Setting", "SPEC", "GROUPS", "SettingsStore", "test_api",
           "dotted_get", "dotted_set", "dotted_del"]


# ---------------------------------------------------------------- 点号路径工具

def dotted_get(data: dict, dotted: str, default=None):
    """按 "a.b.c" 取值。"""
    cur = data
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def dotted_set(data: dict, dotted: str, value) -> None:
    """按 "a.b.c" 赋值（中间层不存在就建出来）。"""
    keys = dotted.split(".")
    cur = data
    for key in keys[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[keys[-1]] = value


def dotted_del(data: dict, dotted: str) -> bool:
    """按 "a.b.c" 删除。**父层因此变空也一并清掉** —— 否则 settings.json 里
    会留下一堆 `{"ui": {}}` 这样的空壳，"恢复默认"看起来没生效。"""
    keys = dotted.split(".")
    stack = []
    cur = data
    for key in keys[:-1]:
        if not isinstance(cur, dict) or key not in cur:
            return False
        stack.append((cur, key))
        cur = cur[key]
    if not isinstance(cur, dict) or keys[-1] not in cur:
        return False
    del cur[keys[-1]]
    for parent, key in reversed(stack):
        if isinstance(parent.get(key), dict) and not parent[key]:
            del parent[key]
        else:
            break
    return True


# ---------------------------------------------------------------- 设置项声明表
#
# 设置界面**由这张表自动生成**，不另外写一遍控件。
# 好处：加一个设置项 = 在这里加一行，界面自动出现，取值/写回/恢复默认都不用碰。

class Setting:
    """一个可配置项。

    属性：
        key      点号路径，例如 "ai.model"
        group    分组 id（见 GROUPS）
        label    界面上显示的名字
        kind     text | secret | int | float | bool | choice | multiline | list
        default  没有覆盖时显示什么（仅用于界面提示，真正的默认值在 config.yaml）
        choices  kind="choice" 时的候选项，[(值, 显示名), ...]
        lo/hi    kind 为 int/float 时的范围
        unit     单位后缀，例如 "秒"
        help     一行说明
        restart  True 表示改完要重启程序才生效（界面会标出来）
        secret   True 表示写到 secrets.yaml 而不是 settings.json
        rows     kind="multiline" 时的**默认行数**（不填用 5 行）
                 ⚠️ 只是**初始高度** —— 那个框底下有一条把手，用户可以上下拖。
                 所以这里填"大概够看几行"，别为了塞下全文填一个巨大的数。
    """

    __slots__ = ("key", "group", "label", "kind", "default", "choices",
                 "lo", "hi", "unit", "help", "restart", "secret", "rows")

    def __init__(self, key, group, label, kind="text", default=None, choices=None,
                 lo=None, hi=None, unit="", help="", restart=False, secret=False,
                 rows=None):
        self.key = key
        self.group = group
        self.label = label
        self.kind = kind
        self.default = default
        self.choices = choices or []
        self.lo = lo
        self.hi = hi
        self.unit = unit
        self.help = help
        self.restart = restart
        self.secret = secret
        #: multiline 的**初始行数**（None = 用界面里的默认 5）。
        #: ⚠️ 只是初始高度 —— 那个框底下有把手可以上下拖。
        self.rows = rows

    def coerce(self, raw):
        """把界面来的原始值转成配置里该有的类型。转不了就抛 ValueError（界面会提示）。"""
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on", "是", "开")
        if self.kind == "int":
            v = int(float(str(raw).strip()))
            return self._clamp(v)
        if self.kind == "float":
            return self._clamp(float(str(raw).strip()))
        if self.kind == "list":
            if isinstance(raw, list):
                return [str(x) for x in raw]
            # 界面里是一行一个；顺手去空白、去重、保序
            out = []
            for line in str(raw).splitlines():
                s = line.strip()
                if s and s not in out:
                    out.append(s)
            return out
        if self.kind == "secret":
            return str(raw).strip()
        return str(raw).strip()

    def _clamp(self, v):
        if self.lo is not None and v < self.lo:
            raise ValueError("不能小于 %s" % self.lo)
        if self.hi is not None and v > self.hi:
            raise ValueError("不能大于 %s" % self.hi)
        return v

    def format(self, value) -> str:
        """把配置里的值转成界面上的文本。"""
        if value is None:
            return ""
        if self.kind == "bool":
            return "开" if value else "关"
        if self.kind == "list":
            return "\n".join(str(x) for x in (value or []))
        return str(value)


GROUPS = [
    # ⚠️ 「AI 接口」这一组**只放用户真会动的**（2026-10-11 用户要求简化：
    #    「AI项中下面很多都不用出现吧」）。原来 10 项，最长输出 / 超时 /
    #    输入单价 / 输出单价**挪到「高级」**了 —— 那四项是"设完就不动"的调参，
    #    摆在常用项中间只会让人以为必须逐个填。
    #    ⚠️ `ai.base_url` **留在这里**：换服务商时只改「模型」不改接口地址
    #       会得到很难查的报错，它和「模型」是同一层的东西。
    ("ai",       "AI 接口",    "接口、模型、提示词、生成条数。"),
    ("persona",  "人设与口吻", "你是谁、跟谁说话、照着谁的口吻写。"),
    ("session",  "会话",       "每个会话单独设模式、人设和关系。关掉的会话完全不读不记。"),
    ("ui",       "外观",       "配色、窗口怎么跟微信配合。宽度直接拖面板左右边缘。"),
    ("behavior", "行为",       "热键，以及给模型带多少条聊天记录。"),
    ("record",   "会话与记录", "记录哪些会话、怎么读、要不要把图片也发给模型。"),
    # ⚠️ 2026-10-13：常用组里"调完就不动"的项（数值调参、采样频率、诊断开关、
    #    自动触发源）全都堆到这里。**这一组不设上限**。
    ("advanced", "高级",       "生成参数、读取频率、触发方式、文件位置。"
                              "平时不用动，改错会影响运行。"),
]


#: 「恢复默认」时**不动的项**（2026-10-14 用户要求「恢复默认接口地址与模型不要改」）。
#:
#: 理由：这三样是**用户自己填的"用哪家服务"**（地址 / 模型名 / Key），
#: 清掉之后得重新翻服务商文档、重新填一遍；而「恢复默认」的语义是
#: 「把**调参**退回出厂」，不是「把配置抹掉」。
#: ⚠️ 加进这个元组就等于**同时**改了三个地方（别再各处手写一遍）：
#:    ① `SettingsStore.reset_all()` 会留着它们；
#:    ② 单行那颗「恢复默认」**不再画在**这两行上（`settings_ui._build_row`）；
#:    ③ 确认框里那句"不会动"的说明（用 `RESET_KEEP_LABEL` 拼）。
#: ⚠️ `ai.api_key` 本来就在密钥层（`_secrets`，不走 `_overrides`），
#:    这里列出来是为了**让文案和代码同源** —— 以后加第三个"用户的凭据类"项时，
#:    只改这一处就够了。
RESET_KEEP = ("ai.base_url", "ai.model", "ai.api_key")

#: 确认框里那句「…不会动」的说明（和 `RESET_KEEP` 同源，别再手写第二份）。
RESET_KEEP_LABEL = "接口地址 / 模型 / API Key"


SPEC: List[Setting] = [
    # ---- AI 接口 ----
    # ⚠️ 这一项的行内说明**是空的，别再加回去**（2026-09-27 用户要求）：
    #    原来写的是"写到 secrets.yaml（不进 Git）。界面默认打码显示。"，
    #    那是**开发者的备忘**，不是给用户看的说明 —— 用户要的是"填进去就能用"。
    #    密钥落在哪个文件、进不进 Git，是打包和仓库层面的事，不该占用界面。
    Setting("ai.api_key", "ai", "API Key", "secret", secret=True),
    # ⚠️⚠️ **接口地址与模型的出厂值是空的**（2026-10-13 用户要求：「默认为空吧」）。
    #    原来预填 DeepSeek 的值，等于替用户选好了服务商 —— 换一家时只填 Key 会
    #    **静默打到 DeepSeek 上**（旧代码里还有硬编码兜底，见 `ai.py` 开头）。
    #    留空 = 没配置：`AIClient.available` 为假、`missing` 会说清缺哪几项。
    #    DeepSeek 那两行只当**例子**写在 help 里。
    Setting("ai.base_url", "ai", "接口地址", "text", default="",
            # ⚠️ help 里**不要再复述 label**（"服务商的接口地址"）——
            #    `tools/audit_ui_text.py` 第②段就是查这个，2026-10-14 报过 1 处。
            help="例如 https://api.deepseek.com。"
                 "照服务商文档填，留空没法调用。"),
    Setting("ai.model", "ai", "模型", "text", default="",
            help="服务商给的模型名，例如 deepseek-flash。"
                 "填错会在生成时报错，留空没法调用。"),
    # ⚠️ 2026-10-14：这一项**从「会话与记录」挪到「AI 接口」**（用户要求
    #    「把分析图片放在 AI 栏里面」）—— 它决定的是"发给模型的请求里带不带图"，
    #    是 AI 侧的开关，跟记录本身没关系。`group` 是独立字段，挪它只改这一处。
    # ⚠️ 同一天**又往前挪了一格**（用户要求「改到视觉模型前面，名称叫识别图片」）：
    #    SPEC 里的**先后**就是界面上的先后（`settings_ui` 按 SPEC 顺序铺），
    #    所以"挪位置"= 把这段 Setting 搬到 `ai.vision_model` 前面（就在下面几行）。
    Setting("ocr_read.attach_image", "ai", "识别图片", "bool",
            default=False,
            help="把聊天里的图片发给模型识别（需要模型支持看图）。开启后可能会消耗更多费用"),
    Setting("ai.vision_model", "ai", "视觉模型", "text", default="",
            help="留空 = 用上面那个模型。填了就只在识别图片时用它"),
    Setting("ai.system_prompt", "ai", "系统提示词（整段可改）", "multiline",
            default=prompts.DEFAULT_SYSTEM_TEMPLATE,
            # ⚠️ 开局就要看得见十几行（这段有一整个屏幕长）。
            #    它只是**初始高度** —— 框底下有一条把手，用户可以自己上下拖。
            rows=14,
            # ⚠️ 这段不许出现 `**` / 反引号 —— 设置界面不渲染 markdown，
            #    会原样漏出去（test_qt_settings_ui 有一节卡着这条）。
            # ⚠️ 2026-10-13：原来这里还有一句"⭐ 可以整段重写"，**和标题里的
            #    「（整段可改）」重复**，删了（用户要求：多余的提示不要）。
                 # ⚠️ 2026-10-14：「看提示词」从悬浮窗搬到了**这个窗口里**
                 #    （就挂在这一项下面），所以文案要指向"下面那颗"。
                 ),
    Setting("ai.n_replies", "ai", "生成几条", "int", default=3, lo=1, hi=8, unit="条"),
    Setting("ai.max_tokens", "advanced", "最长输出", "int", default=2000, lo=64, hi=8192,
            unit="token",
            help="一次最多输出多少。模型的「思考」也算在里面 —— 调小了可能光想不答，"
                 "正文一个字都出不来。"),
    Setting("ai.timeout", "advanced", "超时", "int", default=60, lo=5, hi=300, unit="秒",
            help="等模型回话的最长时间。网络慢或模型排队时才需要调大。"),
    # ⚠️ 2026-10-13：「随机性」从常用组挪进「高级」。
    #    代码**真的在用**它（`ai.py:93` 读、`ai.py:192` 随请求发出去），不是死变量；
    #    但默认 1.0 = 模型自己的默认值，等于「没设」——
    #    而改了它**看不出效果**（只影响措辞的飘不飘），属于"调完就不动"的那一类。
    #    判据同 `test_settings.py` 第 8 节：数值调参一律放「高级」。
    Setting("ai.temperature", "advanced", "随机性", "float", default=1.0, lo=0.0, hi=2.0,
            help="调低更稳、调高更活。0 附近容易每次都写出一样的句子。"),

    # ---- 人设与口吻 ----
    Setting("persona.default", "persona", "全局人设", "multiline",
            default="你帮「我」写微信回复。写的是「我」要说的话，用第一人称，直接能发出去。",
            help="写「你是谁、跟谁说话」。反 AI 腔的规则在「AI 接口 → 系统提示词」；"
                 "想给某个会话单独写，去「会话」分组。"),
    Setting("persona.default_relationship", "persona", "默认关系", "text", default="朋友",
            help="影响称呼和语气。可以按会话单独覆盖（在「会话」分组里）。"),
    Setting("style.enabled", "persona", "口吻样本", "bool", default=True,
            help="把「我」最近发过的短消息喂给模型照着模仿。"),
    Setting("style.max_samples", "advanced", "样本条数", "int", default=12, lo=0, hi=50,
            unit="条"),
    Setting("style.max_length", "advanced", "样本长度上限", "int", default=60, lo=10, hi=500,
            unit="字", help="超过这个字数的消息不算口吻样本（长段落不代表日常说话）。"),

    # ---- 外观 ----
    Setting("ui.theme", "ui", "主题", "choice", default="auto",
            choices=[("auto", "跟随微信"), ("dark", "深色"), ("light", "浅色")],
            help="默认直接看微信渲染出来是什么颜色，系统设置只作兜底。"),
    # ⚠️ 2026-10-14 **删掉「无边框窗口」**（`ui.frameless`，用户要求「删除无边框窗口吧」）：
    #    · 关掉它只是退回**系统标题栏**（多一条丑边），而这个程序的面板是**贴边跟随**的、
    #      设置窗口也是自绘外壳 —— 关掉之后和整个界面不是一套；
    #    · 它还有一条**重启级**的坑：`panel.py:770` 是建窗口时读它设 `FramelessWindowHint`，
    #      ⚠️ 运行期改窗口 flag 会**销毁并重建原生窗口**（用户眼里"窗口闪一下"），
    #      所以这一项**没标 `restart` 但它其实要重启才真的生效** —— 界面在骗人。
    #    ⚠️ 键和读取代码都留着（`config.yaml` / `panel.py`），值恒为默认的 `True`。
    # ⚠️ 2026-10-13 删掉 `ui.rounded`（圆角开关）——**产品代码里一处都没读它**，
    #    是个"设置了没有任何效果"的假开关。
    # ⚠️ 2026-10-14 又删掉 `ui.radius`（圆角半径，用户要求「先把界面删掉吧这几个」）：
    #    · 它**只在悬浮窗上生效**（`panel.py` 两处读它给 `Panel(radius=)`），
    #      而设置窗口 / 看提示词 / 只看对话 / 确认框都固定用 `theme.SHELL_RADIUS`(10) ——
    #      名字叫"圆角半径"其实只管一个窗口；
    #    · 代码是 `int(cfg.get(...) or SHELL_RADIUS)` → **填 0 等于没填**
    #      （想调成直角做不到），范围里的 `lo=0` 是骗人的；
    #    · 默认 10 **正好等于**那个常量，所以删掉**行为完全不变**。
    #    ⚠️ 键和读取代码都留着（`config.yaml` / `panel.py`）——"删的是设置项，不是功能"。
    Setting("ui.attach", "ui", "跟随微信", "choice", default="dock",
            choices=[("dock", "贴边跟随"), ("float", "固定悬浮"), ("off", "不跟随")],
            help="贴边 = 自动站到微信旁边，位置和高度都跟着微信。"
                 "固定悬浮 = 记住你拖的位置，跟着微信平移，不会被拉回边上。"),
    Setting("ui.dock_side", "ui", "贴哪边", "choice", default="auto",
            choices=[("auto", "自动（右→左→角落）"), ("right", "固定右侧"), ("left", "固定左侧")]),
    Setting("ui.follow_minimize", "ui", "微信最小化时一起收起", "bool", default=True),
    # ⚠️⚠️ 2026-10-13 **删掉「面板宽度」这一项**（用户提的）。
    #    它本来就在面板上直接调：拖**左/右边缘**改宽度，松手就
    #    `_save_width()` 写回同一个 `ui.width` 键，状态栏还会说"已记住"
    #    （`qt_ui/panel.py:1542~1576`）。摆在设置里是**同一个东西两处可改**，
    #    而且设置里那个数（逻辑像素）和拖动出来的值容易让人以为哪个没生效。
    #    ⚠️ **配置键 `ui.width` 保留**（面板和跟随器都要读它），
    #       `SettingsStore.set()` 不校验 SPEC，所以拖动保存这条路不受影响。
    #    范围由下面两项限定：`ui.min_width` / `ui.width_max`。
    Setting("ui.min_width", "advanced", "缩窄下限", "int", default=200, lo=120, hi=400,
            unit="px",
            help="空间不够时面板会自动缩窄到这个宽度；连它都放不下就不再跟随。"
                 "也是拖宽度时的下限。"),
    Setting("ui.width_max", "advanced", "宽度上限", "int", default=640, lo=240, hi=1200,
            unit="px",
            help="拖面板左右边缘调宽度时的上限。"),
    Setting("ui.follow_interval", "advanced", "跟随检查间隔", "float", default=0.016,
            lo=0.008, hi=2.0, unit="秒",
            help="多久看一眼微信的位置。调大省电，但拖微信时面板会一跳一跳地跟。"),
    Setting("ui.height", "ui", "面板高度", "int", default=0, lo=0, hi=2000, unit="px",
            help="0 = 跟随微信窗口的高度（推荐）。填正数则钉死这个高度。"
                 "微信没开时才退回「按回复内容自适应」。"),
    Setting("ui.opacity", "ui", "不透明度", "float", default=1.0, lo=0.5, hi=1.0),
    Setting("ui.topmost", "ui", "总是置顶", "bool", default=False,
            help="只在上面选了「不跟随」时才起作用：那种情况下没人把面板抬到微信上面，"
                 "一被别的窗口盖住就找不回来。跟随中会自动不生效（切到别的程序时"
                 "不该还压在最上面）。"),

    # ---- 行为 ----
    # ⚠️ 2026-10-13 这一组从 9 项砍到 2 项（用户要求「方便用户操作」）。
    #    判据沿用第 8 节那条：**"调完就不动"的数值/调参项一律进「高级」**。
    #    留在常用组的是"你真会动的"：热键、喂几条给模型，
    #    外加 2026-10-14 用户点名要放进来的「鼠标定位」（见下面那行）。
    #
    # ⚠️⚠️ 另两个"自动触发源"**已经不在界面上**了：
    #    `trigger.on_window_focus`（切到微信时触发）、`trigger.on_session_switch`
    #    （切换会话时触发）—— 2026-10-14 用户要求「先把界面删掉吧这几个」。
    #    开着会在你切窗口/切会话时**自己调 AI 花钱**，所以一直是默认关；
    #    ⚠️ **代码路径全部留着**（`trigger.py` / `run_watch.py` 照旧 `.get(..., False)`），
    #    值恒为假 = 行为和以前一模一样；想恢复就把两行 `Setting(...)` 加回来。
    Setting("trigger.on_hotkey", "behavior", "全局热键", "text", default="Ctrl+Alt+Q",
            restart=True, help="程序不在前台也能响应。改完需要重启程序。"),
    Setting("runtime.max_messages", "behavior", "喂给 AI 的消息条数", "int", default=20,
            lo=2, hi=200, unit="条",
            help="带多少条聊天记录给模型看。给得越多越贵，一般 20 条够用。"),
    # ⚠️ 2026-10-14：这一项**原来在「高级」**（按"默认关、别开就别摆出来"的判据），
    #    用户要求「把鼠标定位放在行为里面」→ 挪进常用组。
    #    ⚠️ 它的 help 原来写着"建议一直关着"，挪过来之后那句话和位置自相矛盾，
    #    改成把**两种行为**讲清楚；默认仍然是关。
    Setting("reply.use_cursor_anchor", "behavior", "用鼠标位置定位回复目标", "bool",
            default=False,
            help="只把鼠标停着的那一条当回复目标（要先把鼠标放上去再按热键）。"),

    # ---- 会话与记录 ----
    Setting("record.enabled", "record", "静默记录", "bool", default=True, restart=True,
            help="每隔几秒读一次当前会话存起来，给模型当上下文。不调 AI、不花钱、不提醒。"),
    # ⚠️ 全项目只有这一处的默认值是 **choice 而不是 bool**，因为它是三态：
    #    要不要用「截屏 + OCR」这条备用路。
    Setting("reader.backend", "record", "读取方式", "choice", default="auto", restart=True,
            choices=[("auto", "自动"), ("uia", "只用微信控件"), ("ocr", "只用截图认字")],
            help="自动：先读微信自己的控件，读不到就改用截图认字。"
                 "截图认字只能看到当前一屏，也认不出图片。"),
    # ⚠️ 2026-10-14 **删掉「按需读屏」和「一直读屏」**（用户要求「按照默认的来就可」）：
    #    · `ocr_read.probe`（默认 **true**）—— 每秒轻量探一次、真变了才仔细读，
    #      这是**推荐**的那一档，本来也不该有人关它（关了 = 你翻过的屏一条都不留）；
    #    · `ocr_read.record`（默认 **false**）—— 每拍都真读，实测贵 5 倍
    #      （`OCR后端.md`：5.2% → 25% 一个核），是给排查用的。
    #    ⚠️ **两个键和读取代码全部保留**（`recorder.py` / `ocr_reader.py` 照旧
    #       `.get(..., 默认)`），所以删掉之后行为就**钉在默认值**上：
    #       探针开着 + 不连续真读 = 和现在的观感完全一样。
    #    ⚠️ 想恢复就把这两行 `Setting(...)` 加回来（键还在 `config.yaml` 里）。
    Setting("sessions.default_mode", "record", "新会话默认模式", "choice", default="on",
            restart=True,
            choices=[("on", "开（记录 + 可生成）"), ("observe", "只记录不生成"), ("off", "关")],
            help="默认「开」：你打开的每个会话都会被记录。"
                 "不想被记的写进黑名单（在下面）；单个会话的开关在「会话」分组。"),
    Setting("sessions.disabled", "record", "黑名单（一行一个）", "list", restart=True,
            help="写进来的会话不读、不记录、不生成。敏感会话放这里。"
                 "⚠️ 启动时生效，优先于「会话」分组里选的模式。"),

    # ---- 高级 ----
    # ⚠️ 2026-10-13：常用组里那些"调完就不动"的项都堆到这一组（用户要求"方便操作"）。
    #    这一组**不设条数上限** —— 它就是专门放杂项的地方。
    Setting("state.path", "advanced", "运行数据文件", "text", default="state/state.json",
            restart=True),
    Setting("history.path", "advanced", "聊天记录文件", "text", default="state/history.db",
            restart=True),
    # ⚠️ 2026-10-13 **删掉「日志级别」**：产品代码里没有任何地方读它
    #    （日志走 `print` + 打包版的重定向文件），选它不会有任何效果。
    #    `config.yaml` 里也一起删了。
    # ---- 触发源与防抖 ----
    # ⚠️ 2026-10-14 **删掉「切到微信时触发」「切换会话时触发」**（用户要求
    #    「先把界面删掉吧这几个」）：两个都是"自动触发源"，开着会在你切窗口 /
    #    切会话时**自己调 AI 生成**（花 token、把刚生成的内容顶掉），
    #    所以一直是默认关 —— 既然没人该打开，就别摆在界面上。
    #    ⚠️ `trigger.py` / `run_watch.py` **照旧读它们**（纯 `.get(..., False)`），
    #    值恒为假 = 行为与之前完全一致；想恢复只需要把这两行 `Setting(...)` 加回来。
    Setting("trigger.min_interval", "advanced", "两次触发最小间隔", "float", default=3.0,
            lo=0.0, hi=120.0, unit="秒", help="两次触发至少隔这么久，撞得太近的会被忽略。"),
    Setting("trigger.cooldown_after_copy", "advanced", "复制后冷静期", "float", default=20.0,
            lo=0.0, hi=600.0, unit="秒",
            help="这段时间内不自动触发（你正在去微信粘贴的路上）。"),
    # ⚠️ `reply.use_cursor_anchor`（「用鼠标位置定位回复目标」）原来在这块，
    #    2026-10-14 用户要求「把鼠标定位放在行为里面」→ 挪到上面「行为」那一组了。
    # ---- 上下文的闸（从「行为」挪来）----
    Setting("runtime.max_chars", "advanced", "上下文总长上限", "int", default=4000,
            lo=0, hi=100000, unit="字",
            help="0 = 不限制。带的历史超过这个长度，就从最旧的开始丢。"),
    Setting("runtime.max_msg_chars", "advanced", "单条消息截断长度", "int", default=500,
            lo=0, hi=10000, unit="字",
            help="0 = 不限制。单条太长的（比如粘贴进来一大段）会被截断，"
                 "免得一条就把上下文挤满。"),
    # ---- 记录与采样频率（从「会话与记录」挪来）----
    Setting("record.interval", "advanced", "记录间隔", "float", default=1.0, lo=0.5, hi=60.0,
            unit="秒", restart=True,
            help="多久读一次聊天内容。别低于 0.5 秒。"),
    Setting("record.fast_interval", "advanced", "快速档间隔", "float", default=0.2,
            lo=0.0, hi=10.0, unit="秒", restart=True,
            help="翻记录、来新消息时用这个更快的间隔，安静下来自动回到上面的间隔。"
                 "填 0 = 关掉。"),
    Setting("record.fast_hold", "advanced", "快速档保持", "float", default=3.0,
            lo=0.0, hi=60.0, unit="秒", restart=True,
            help="内容变过之后，保持快读多久再回落。"),
    Setting("ocr_read.probe_interval", "advanced", "看一眼的间隔（安静时）", "float",
            default=1.0, lo=0.25, hi=30.0, unit="秒", restart=True,
            help="安静时多久看一眼屏幕。调小更灵敏、更费电。"),
    Setting("ocr_read.probe_fast", "advanced", "看一眼的间隔（内容在变时）", "float",
            default=0.5, lo=0.5, hi=5.0, unit="秒", restart=True,
            help="翻记录、来新消息时多久看一眼。别低于 0.5 秒，否则来不及读完。"),
    # ⚠️ 2026-10-13 **删掉「OCR 模式记录间隔」**：产品代码里没有消费者
    #    （OCR 连续记录那条路读的是 `record.interval`），选它不会有任何效果。
    Setting("history.max_per_session", "advanced", "每会话最多保留", "int", default=500,
            lo=20, hi=10000, unit="条", restart=True, help="超出后自动删最旧的。"),
]

SPEC_BY_KEY: Dict[str, Setting] = {s.key: s for s in SPEC}


# ---------------------------------------------------------------- 测试连接

def test_api(base_url: str, api_key: str, model: str, timeout: float = 20.0):
    """发一次**最小**请求验证 Key / 地址 / 模型是否可用。

    返回 (是否成功, 说明)。**不抛异常** —— 界面上要的是能直接显示的一句话。

    为什么值得做：填完 Key 不验证的话，要等真正生成回复时才发现填错了，
    而那时错误信息混在业务流程里，不容易看出是 Key 的问题。

    成本：`max_tokens=1`，一次请求约几十 token，基本不花钱。
    """
    import json
    import urllib.error
    import urllib.request

    base_url = (base_url or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    model = (model or "").strip()
    if not base_url:
        return False, "接口地址是空的"
    if not api_key:
        return False, "API Key 是空的"
    if not model:
        return False, "模型名是空的"

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, "连接成功，Key 和模型都可用"
            return False, "服务返回 HTTP %s" % resp.status
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
            detail = (payload.get("error") or {}).get("message") or ""
        except Exception:
            pass
        if e.code == 401:
            return False, "API Key 无效（401）" + ("：" + detail if detail else "")
        if e.code == 404:
            return False, "地址或模型名不对（404）" + ("：" + detail if detail else "")
        if e.code == 402:
            return False, "余额不足（402）"
        return False, "HTTP %s %s" % (e.code, detail)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------- 存储

class SettingsStore:
    """三层配置的读写。

    用法：
        store = SettingsStore()
        cfg   = store.load()               # 合并后的 Config（给主程序用）
        store.set("ui.width", 380)         # 写进 settings.json
        store.save()
    """

    def __init__(self, config_path: Optional[str] = None,
                 secrets_path: Optional[str] = None,
                 settings_path: Optional[str] = None,
                 logger=None):
        self.config_path = config_path or paths.config_path()
        self.secrets_path = secrets_path or paths.secrets_path()
        self.settings_path = settings_path or paths.settings_path()
        self._log = logger or (lambda m: None)
        self._overrides: Dict[str, Any] = {}
        self._secrets: Dict[str, Any] = {}
        self._load_file_overrides()

    # ---------------------------------------------------------- 读

    def _load_file_overrides(self):
        self._overrides = self._read_json(self.settings_path)
        # secrets 只用来判断"这个键被覆盖了吗"，值本身已经在 load() 里合并过了
        self._secrets = self._read_yaml(self.secrets_path)

    @staticmethod
    def _read_json(path: str) -> dict:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _read_yaml(path: str) -> dict:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def load(self) -> Config:
        """合并三层，返回 Config。**这是主程序唯一该用的入口。**"""
        data: Dict[str, Any] = {}
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                raise RuntimeError("config.yaml 根节点必须是映射")
        else:
            self._log("配置文件不存在: %s（将只用用户设置）" % self.config_path)

        _deep_merge(data, self._overrides)     # 用户设置覆盖出厂默认
        _deep_merge(data, self._secrets)       # 密钥最后覆盖（跟改动前的行为一致）
        return Config(data, self.config_path)

    def get(self, dotted: str, default=None):
        """取某个键**当前生效**的值（合并结果）。"""
        cur = self.load().get(dotted, default)
        return cur

    def source(self, dotted: str) -> str:
        """这个键的值是从哪一层来的：default / user / secret。

        界面用它显示「已修改 · 恢复默认」—— 用户改了 config.yaml 却发现没反应时，
        这一行就是答案。
        """
        if dotted_get(self._secrets, dotted) is not None:
            return "secret"
        if dotted_get(self._overrides, dotted) is not None:
            return "user"
        return "default"

    def raw_override(self, dotted: str):
        """用户在设置里写过的原始值（没写过返回 None）。"""
        return dotted_get(self._overrides, dotted)

    def is_overridden(self, dotted: str) -> bool:
        return self.source(dotted) != "default"

    def overrides(self) -> dict:
        """当前所有用户覆盖（副本，改它不影响内部状态）。"""
        return json.loads(json.dumps(self._overrides))

    # ---------------------------------------------------------- 写

    def set(self, dotted: str, value):
        """设置一个值。`None` 等于「恢复默认」（删掉覆盖）。

        ⚠️ 只改内存，要落盘得调 save()。
        """
        if value is None:
            dotted_del(self._overrides, dotted)
        else:
            dotted_set(self._overrides, dotted, value)
        if dotted in SPEC_BY_KEY and SPEC_BY_KEY[dotted].secret:
            # 密钥不走 settings.json（见模块开头），这里只是保持内存一致
            pass

    def unset(self, dotted: str):
        """"恢复默认"：删掉这一层的覆盖，config.yaml 的值就透出来了。"""
        self.set(dotted, None)

    def reset_all(self):
        """清空所有用户覆盖（全部恢复默认）。

        ⚠️⚠️ **`RESET_KEEP` 里那几样不动**（2026-10-14 用户要求
        「恢复默认接口地址与模型不要改」）—— 它们是"用哪家服务"的选择，
        清掉之后得重新翻文档、重新填；而「恢复默认」的语义是
        "把**调参**退回出厂"，不是"把配置抹掉"。
        """
        keep: Dict[str, Any] = {}
        for _k in RESET_KEEP:
            _v = dotted_get(self._overrides, _k)
            if _v is not None:
                keep[_k] = _v
        self._overrides = {}
        for _k, _v in keep.items():
            dotted_set(self._overrides, _k, _v)

    def apply_form(self, values: Dict[str, Any], only_changes: bool = True) -> List[str]:
        """把界面提交的一整张表单写进内存。返回错误说明列表（空 = 全部成功）。

        **不会半途落盘**：先全部校验通过才写，避免"改了一半"。

        ⚠️ `only_changes=True`（默认）会**跳过"跟出厂默认一样"的项**，
        不给它建覆盖。这一点很重要：

            设置界面一提交就是**整组**的值。如果无脑全写，
            `settings.json` 会堆满用户根本没碰过的项，
            而**分层配置的意义正是"没改过的跟着 config.yaml 走"** ——
            被这么一写，以后 `config.yaml` 改了默认值也透不出来了
            （实测踩过：一提交就写进去 14 个 ui 项，其中 13 个用户没碰过）。

        用户在界面上把某个值改回默认值，效果也是"删掉覆盖"（`set` 传 None）。
        """
        errors: List[str] = []
        staged: List[tuple] = []
        for key, raw in values.items():
            spec = SPEC_BY_KEY.get(key)
            if spec is None:
                continue
            try:
                staged.append((key, spec.coerce(raw)))
            except Exception as e:
                errors.append("%s：%s" % (spec.label, e))
        if errors:
            return errors

        cfg = self.load() if only_changes else None
        for key, val in staged:
            if only_changes and not self.is_overridden(key):
                # "当前生效值" = config.yaml 里的值，**没有就用 SPEC 默认值兜底**
                # （后者常发生在 config.yaml 里没写这一项的时候）。
                try:
                    cur = cfg.get(key)
                    if cur is None:
                        cur = SPEC_BY_KEY[key].default
                    if cur == val:
                        continue          # 没改过，不用建覆盖
                except Exception:
                    pass
            self.set(key, val)
        return []

    # ---------------------------------------------------------- 落盘

    def save(self) -> None:
        """把覆盖写进 settings.json（原子写 + 备份）。

        ⚠️ **不用 PowerShell 的 Set-Content**（会写成 UTF-16）；这里用 Python 写 UTF-8。
        """
        paths.ensure_parent(self.settings_path)
        if os.path.exists(self.settings_path):
            try:
                shutil.copy2(self.settings_path, self.settings_path + ".bak")
            except Exception:
                pass
        self._atomic_write(self.settings_path,
                           json.dumps(self._overrides, ensure_ascii=False, indent=2) + "\n")
        self._log("设置已保存: %s" % self.settings_path)

    def set_secret(self, dotted: str, value: str) -> None:
        """把密钥写进 secrets.yaml —— **行级替换，保住文件里的注释**。

        不能 `yaml.dump`：那会把注释和键序都抹掉。
        """
        parts = dotted.split(".")
        if len(parts) != 2:
            raise ValueError("密钥的键必须是两段，例如 ai.api_key")
        section, key = parts
        value = (value or "").strip()

        lines: List[str] = []
        if os.path.exists(self.secrets_path):
            with open(self.secrets_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()

        new_lines = self._patch_yaml_scalar(lines, section, key, value)
        paths.ensure_parent(self.secrets_path)
        if os.path.exists(self.secrets_path):
            try:
                shutil.copy2(self.secrets_path, self.secrets_path + ".bak")
            except Exception:
                pass
        self._atomic_write(self.secrets_path, "\n".join(new_lines).rstrip("\n") + "\n")
        dotted_set(self._secrets, dotted, value)
        self._log("密钥已写入 %s（第 %s 节）" % (os.path.basename(self.secrets_path), section))

    @staticmethod
    def _patch_yaml_scalar(lines: List[str], section: str, key: str, value: str) -> List[str]:
        """在 YAML 文本里改一个「节.键」的标量值，其它行原样保留。

        找不到就追加。value 一律用双引号包起来（密钥里可能有 `:`、`#`、空格，
        不加引号会被 YAML 解析坏）。
        """
        quoted = '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
        out = list(lines)
        sec_start = None
        sec_end = len(out)

        for i, line in enumerate(out):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not line[:1].isspace():          # 顶格 = 顶级键
                name, _, rest = line.partition(":")
                if name.strip() == section and not rest.strip():
                    sec_start = i
                    continue
                if sec_start is not None:       # 到了下一个顶级键，本节结束
                    sec_end = i
                    break
        if sec_start is None:                   # 整个节都没有
            if out and out[-1].strip():
                out.append("")
            out.append("%s:" % section)
            out.append("  %s: %s" % (key, quoted))
            return out

        for i in range(sec_start + 1, sec_end):
            stripped = out[i].strip()
            if not stripped or stripped.startswith("#"):
                continue
            name = stripped.split(":", 1)[0].strip()
            if name == key:
                out[i] = "  %s: %s" % (key, quoted)
                return out

        # 节在、键不在 -> 插在节的最后（跳过尾部空行，插在节内容紧后面）
        insert_at = sec_end
        while insert_at > sec_start + 1 and not out[insert_at - 1].strip():
            insert_at -= 1
        out.insert(insert_at, "  %s: %s" % (key, quoted))
        return out

    @staticmethod
    def _atomic_write(path: str, text: str, encoding: str = "utf-8") -> None:
        """先写临时文件再替换 —— 中途断电也不会留下半个坏文件。"""
        tmp = path + ".tmp"
        with open(tmp, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
