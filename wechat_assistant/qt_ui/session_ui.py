# -*- coding: utf-8 -*-
"""会话级设置面板（Qt 版）—— 给**单个会话**单独设模式、人设、关系。

这是 `wechat_assistant/session_ui.py`（Tk 版，466 行）的 Qt 迁移。
**纯函数一个字节都没改**（`session_rows` / `merge_map` / `count_blacklisted`
连同 docstring 原样搬过来），`tests/test_session_ui.py` 里那些直接测它们的用例
照旧能过。`SessionPanel` 的对外语义（`collect()` / `clear_stage()` / `dirty()` /
`selected` / `_stash()`）也保持一致 —— **只是不再需要 `pack()`**，
Qt 里由调用方把它塞进布局。

为什么单独一个模块
------------------
设置窗口（`settings_ui.py`）是**按 SPEC 表平铺**的：一项就是一个值，
一行画完。而会话级设置是**二维**的（会话 × 属性），塞不进那个模型。
所以这里自己画，设置窗口在「会话」分组下直接把它摆出来。

它能配什么、为什么只能是这些
----------------------------
| 项 | 落在哪 | 什么时候生效 |
|---|---|---|
| 模式 on/observe/off | `state/state.json`（`SessionRegistry`）| **保存后立刻**，不用重启 |
| 单独人设 | `state/settings.json` 的 `persona.per_session` | 保存后立刻（`run_watch` 会重载 cfg）|
| 关系 | 同上，`persona.relationships` | 保存后立刻 |

⚠️ **模式不在 settings.json 里**，它归 `SessionRegistry` 管（`state.json`）。
这是有意的：记录器每 2 秒都在问 `registry.mode_of()`，模式必须是内存里的活状态。
所以这个面板**跨界**写两个存储，保存时要两边都落盘 —— 见 `SettingsWindow._save()`。

⚠️ **和 `sessions.disabled` 黑名单的关系**：黑名单在**启动时**由
`SessionRegistry._apply_bootstrap()` 强制执行，优先级高于这里设的模式。
也就是说：一个会话如果在 config 黑名单里，你在面板里给它开「开」，
**下次启动还是会被强制关掉**。面板会在这类会话上标出来，
免得你遇到「改了没反应」却找不到原因。

留空 = 用全局
-------------
人设/关系留空表示"删掉这个会话的覆盖，跟全局走"，而不是"设成空字符串"。
这一点很重要：不这样做的话，用户想恢复全局人设就只能去手改 JSON。

Tk 专属补丁在这里**没有对应物**（不是漏了，是本来就不需要）
--------------------------------------------------------
    Tk 的做法                                     Qt 的替代
    -------------------------------------------   ------------------------------
    `_paint/_hover` 逐个控件 `configure(bg=...)`   一个自定义部件在 `paintEvent`
      （Frame + Label + Canvas **三个**底色都要跟）   里画一次底，子部件是透明的
    `dot` 用 `tk.Canvas.create_oval`               `paintEvent` 画椭圆（抗锯齿白送）
    `<Button-1>` / `<Enter>` / `<Leave>` 逐个绑     部件自带 `mousePressEvent` /
      （子控件不继承父的绑定，漏一个就点不动）        `enterEvent` / `leaveEvent`
    `wraplength` 手算（超宽静默裁字）               `QLabel.setWordWrap(True)`
    `ScrollArea(bar_width=5)` 自绘浮层指示条        `QScrollArea` 的原生滚动条
"""

from typing import Callable, Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .base import Style, hand_cursor, qc
from .inputs import EntryBox, InputBox
from .layout import ScrollArea, WrapGroup
from .widgets import Pill

__all__ = ["MODE_CHOICES", "MODE_LABEL", "session_rows", "merge_map",
           "count_blacklisted", "SessionPanel"]

LIST_W = 176
ROW_H = 30

# 模式选项：值 -> 显示文字。顺序就是面板里从左到右的顺序。
MODE_CHOICES = [("on", "开"), ("observe", "只记录"), ("off", "关")]
MODE_LABEL = dict(MODE_CHOICES)

MODE_HINT = {
    "on": "记录上下文，并且可以生成建议。",
    "observe": "只记录上下文，不生成建议。想留着回看但不想被打扰时用。",
    "off": "完全不读、不记录、不生成。敏感会话（银行、工作群）用这个。",
}


# ================================================================ 纯逻辑（可脱开界面测）
#
# ⚠️ 下面三个函数是**从 Tk 版逐字搬过来的**，连 docstring 都没动。
#    `tests/test_session_ui.py` 有几十条用例直接调它们，
#    签名和行为都不能变 —— 迁移时改这里等于改业务逻辑。

def _seen_key(v) -> str:
    """把「最后一次看到这个会话」的时间规范化成**可比较的字符串**。

    ⚠️ 只接受 `"YYYY-MM-DD HH:MM:SS"` 这种**定宽**格式 —— 否则返回空串
        （＝"没有时间"，排到最后）。
        为什么要校验：排序用的是**字符串比较**（这个格式天生可比，不用解析），
        而字符串比较对**畸形值没有免疫力** —— 实测 `"坏数据" > "2026-09-27 ..."`，
        因为 `坏` 的码点比 `2` 大，于是一条脏数据会**冒到列表最前面**。
    ⚠️ 为什么不用 `time.strptime` 解析再排：多花时间、还要处理格式差异，
        而"定宽字符串直接比"本来就等价 —— 校验一次格式就够了。
    """
    if not v:
        return ""
    t = str(v)
    if len(t) != 19 or t[4] != "-" or t[7] != "-" or t[10] != " " \
            or t[13] != ":" or t[16] != ":":
        return ""
    return t


def session_rows(
    sessions: Optional[Dict[str, dict]],
    personas: Optional[Dict[str, str]],
    relationships: Optional[Dict[str, str]],
    disabled: Optional[List[str]],
    default_mode: str = "off",
    seen: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """把「注册表里的会话」和「各处配置」合成一行一条的视图。

    ⚠️ 取的是**并集**，不是只看注册表：配置里可能留着某个会话的人设，
    但那台微信还没打开过它（注册表里没有）。这种条目也要列出来 ——
    否则用户永远删不掉那个孤儿配置。

    返回的每行：
        key / name / mode / is_new / known / persona / relationship
        / blacklisted / custom / seen

    `seen`：`{会话key: "2026-09-27 20:40:52"}` —— **按时间排**的依据，见下面的排序说明。
    """
    sessions = sessions or {}
    personas = personas or {}
    relationships = relationships or {}
    disabled = [str(d) for d in (disabled or []) if d]
    dset = set(disabled)
    seen = seen or {}

    keys = set(sessions.keys()) | set(personas.keys()) | set(relationships.keys())
    rows = []
    for key in keys:
        s = sessions.get(key) or {}
        name = s.get("display_name") or key
        rows.append({
            "key": key,
            "name": name,
            "mode": s.get("mode", default_mode),
            "is_new": bool(s.get("is_new")),
            "known": key in sessions,          # 注册表里有没有（微信里出现过没有）
            "persona": (personas.get(key) or ""),
            "relationship": (relationships.get(key) or ""),
            "blacklisted": key in dset,
            "custom": bool(personas.get(key) or relationships.get(key)),
            # 两个来源取**较新的**那个：注册表的"刚打开过"、屏快照库的"最后记录过"。
            # ⚠️ 不能写成 `a or b` —— 那只是"a 没有就用 b"，不是取较新的
            #    （第一版就是这么写的，被 §1b 的用例抓出来了）。
            # ⚠️ `_seen_key` 会把**不像时间的东西**当成"没有时间"：
            #    字符串比较下畸形值可能比真时间还大而冒到最前面
            #    （`"坏数据" > "2026-..."`，因为 `坏` 的码点比 `2` 大）。
            "seen": max(_seen_key(s.get("last_seen_at")),
                        _seen_key(seen.get(key))),
        })

    # ---- 排序：**按时间，最近的在上面**
    #
    # ⚠️ 2026-09-27 改。原来按名字排（Unicode 码点，不是拼音），
    #    理由是"至少是稳定的" —— 稳定是够稳定，但跟"最近在聊什么"毫无关系，
    #    19 个会话里想找刚聊过的那个得一个个扫。用户要求改成按时间。
    #
    # ⚠️ **为什么不用微信左栏的顺序**：那是虚拟列表，UIA 只给**渲染出来的**
    #    那几项（实测 19 个只读得到 8 个），而且它按的是"会话活动时间"
    #    （你点开也会往上顶），跟列表里显示的那个时间**不是一回事** ——
    #    所以既不全、也不能靠 `time_text` 反推。
    #    现在用的是**本地记录的时间**：`screens.seen_at` 的每个会话最大值
    #    （占 13/19，够用），外加注册表里"刚刚打开过"的那个。
    #
    # ⚠️ **没有时间的排在最后**，而且那一段内部**仍按名字** ——
    #    不能把它们混进时间内，也不能让它们每次打开面板都乱换位置。
    # ⚠️ `seen` 是**定宽字符串**（`YYYY-MM-DD HH:MM:SS`），可以直接比大小，
    #    也**必须**这么比：换成 `time.strptime` 解析再比，遇到半截数据就抛异常，
    #    而且多花的时间纯属浪费。排序用降序（`reverse`），所以时间新的在前。
    # ⚠️ 两步排序，别合并成一步：
    #    第一步按名字 —— 给"没有时间"的那一段定死顺序（稳定，不会每次换位）；
    #    第二步按时间**降序** —— Python 的排序是稳定的，`seen` 为空串的
    #    在降序里**自动落到最后**，段内顺序保留第一步的名字序。
    rows.sort(key=lambda r: r["name"])
    if any(r["seen"] for r in rows):
        rows.sort(key=lambda r: r["seen"], reverse=True)
    return rows


def merge_map(current: Optional[dict], key: str, value: Optional[str]) -> Dict[str, str]:
    """在映射里设置/删除一个键，返回**新**字典（不改传入的那个）。

    值去空白后为空 = 删除该键（= 恢复用全局）。
    """
    out = dict(current or {})
    v = (value or "").strip()
    if v:
        out[key] = v
    else:
        out.pop(key, None)
    return out


def count_blacklisted(cfg_get: Callable[[str, object], object]) -> int:
    """黑名单条数（只用来在头部提示一行）。"""
    try:
        return len([x for x in (cfg_get("sessions.disabled", []) or []) if x])
    except Exception:
        return 0


# ================================================================ 列表里的一行

class _Dot(QtWidgets.QWidget):
    """状态点：绿=开、黄(次)=只记录、灰=关。

    Tk 版是一个 8x8 的 `tk.Canvas` 画 `create_oval` —— Canvas 不做抗锯齿，
        这个小圆点其实是有阶梯的。Qt 一行 `drawEllipse` + Antialiasing 就圆了。
    """

    def __init__(self, style: Style, mode: str, parent=None):
        super().__init__(parent)
        self.style = style
        self.setFixedSize(style.px(8), style.px(8))
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._color = style.p["border"]
        self.set_mode(mode)

    def set_mode(self, mode: str):
        p = self.style.p
        self._color = {"on": p["accent"], "observe": p["text_dim"]}.get(
            mode, p["border"])
        self.update()

    def paintEvent(self, ev):
        pt = QtGui.QPainter(self)
        pt.setRenderHint(QtGui.QPainter.Antialiasing, True)
        pt.setPen(QtCore.Qt.NoPen)
        pt.setBrush(qc(self._color))
        pt.drawEllipse(QtCore.QRectF(0.5, 0.5, self.width() - 1,
                                     self.height() - 1))


class _SessionRow(QtWidgets.QWidget):
    """左列的一个会话：名字 + 状态点，整行可点。

    ⚠️ Tk 版要**逐个控件**改底色（Frame / Label / Canvas 三份都要跟，
        漏掉那个 Canvas 就会在选中时留一块异色）—— 因为 Tk 的子控件不继承
        父容器的背景。Qt 这边整行的底在 `paintEvent` 里画一次，
        子控件全部透明，天然不会漏。
    """

    def __init__(self, panel, data: dict, parent=None):
        super().__init__(parent)
        st = panel.style
        self.panel = panel
        self.data = data
        self.key = data["key"]
        self._hover = False
        self._selected = False
        self.setFixedHeight(st.px(ROW_H))
        self.setCursor(hand_cursor())

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(st.px(8), 0, st.px(8), 0)
        lay.setSpacing(st.px(6))

        name = data["name"]
        if len(name) > 12:
            name = name[:11] + "…"
        self.label = QtWidgets.QLabel(name, self)
        self.label.setFont(st.font("label"))
        self.label.setStyleSheet("color:%s; background:transparent;" % st.p["text"])
        self.label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.label, 1)

        self.dot = _Dot(st, data["mode"], self)
        lay.addWidget(self.dot, 0)

        if data.get("blacklisted"):
            self.setToolTip("在黑名单里（启动时会被关掉）")

    # ---- 选中 / 悬停

    def set_selected(self, on: bool):
        if self._selected != bool(on):
            self._selected = bool(on)
            self.update()

    def set_hover(self, on: bool):
        if self._hover != bool(on):
            self._hover = bool(on)
            self.update()

    def enterEvent(self, ev):
        self.setCursor(hand_cursor())
        if not self._selected:
            self.set_hover(True)

    def leaveEvent(self, ev):
        self.set_hover(False)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self.panel._select(self.key)
            ev.accept()

    def paintEvent(self, ev):
        p = self.panel.style.p
        bg = p["surface_alt"] if (self._selected or self._hover) else p["bg"]
        pt = QtGui.QPainter(self)
        pt.fillRect(self.rect(), qc(bg))


# ================================================================ 模式选项组

class _ModeChoices(QtWidgets.QWidget):
    """三个模式胶囊。点一下选中，选中是绿的。

    不用 QComboBox：那是系统原生控件，风格跟这一套自绘的对不上，
    而且只有三个选项，平铺开一眼能看全。
    """

    def __init__(self, master, style: Style, value: str):
        super().__init__(master)
        self.style = style
        self.value = value if value in MODE_LABEL else MODE_CHOICES[0][0]
        self.on_change: Optional[Callable[[str], None]] = None
        self._btns = {}

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(style.px(6))
        for val, text in MODE_CHOICES:
            # ⚠️ 选中的模式用 `selected`（淡绿底 + 绿字），不是 `primary`。
            #    这一行是"开 / 观察 / 关"三个**选项**，不是主操作按钮；
            #    实心绿块会跟底部的「保存」抢视觉（同一个道理见 Pill 的类文档）。
            b = Pill(style, text, command=lambda v=val: self._pick(v),
                     variant="selected" if val == self.value else "ghost",
                     parent=self)
            lay.addWidget(b, 0)
            self._btns[val] = b
        lay.addStretch(1)

    def _pick(self, val: str):
        self.value = val
        for v, b in self._btns.items():
            b.set_variant("selected" if v == val else "ghost")
        if self.on_change:
            try:
                self.on_change(val)
            except Exception:
                pass

    def get(self) -> str:
        return self.value

    def set(self, val: str):
        if val in self._btns:
            self._pick(val)


# ================================================================ 面板

class SessionPanel(QtWidgets.QWidget):
    """左列会话 + 右列该会话的设置。

    用法（设置窗口里）：
        panel = SessionPanel(holder, style, store,
                             session_source=lambda: registry.sessions,
                             on_mode_change=lambda k, m: ...)
        holder_layout.addWidget(panel)
        panel.collect()      # 保存时取回全部改动
        panel.clear_stage()  # 保存/取消后清掉暂存

    ⚠️ 和 Tk 版一样，**暂存（`_stage_*`）是这个面板的命根子**：
        面板是重建出来的，切一次分组就换一个新实例。
        暂存不从外面传进来（`stage=`），用户改了东西再切分组就会**静默丢失**。
    """

    def __init__(self, master, style: Style, store, session_source=None,
                 on_mode_change: Optional[Callable[[str, str], None]] = None,
                 logger: Optional[Callable[[str], None]] = None,
                 preselect: Optional[str] = None,
                 stage: Optional[dict] = None,
                 seen_source: Optional[Callable[[], dict]] = None):
        super().__init__(master)
        self.style = style
        self.store = store
        self._source = session_source
        #: 给「按时间排」用的时间表 `{会话key: "YYYY-MM-DD HH:MM:SS"}`。
        #: ⚠️ 作为一个**单独的**回调注入，而不是塞进 `session_source` 的返回值里 ——
        #:    那个返回的是注册表本身（活的 dict），往里塞键会**污染状态文件**
        #:    （`SessionRegistry.save()` 会把 sessions 整个写盘）。
        self._seen_source = seen_source
        self._on_mode_change = on_mode_change
        self._log = logger or (lambda m: None)

        self._sel: Optional[str] = None
        self._ed = None
        self._row_widgets: Dict[str, object] = {}
        # 编辑器里那些提示文字。Tk 版要把它们登记进 WrapGroup 手算 wraplength
        # （写死会**静默裁字**，设置窗口能拖到 600px）。Qt 的 QLabel 自己换行，
        # 这里保留登记只为兼容同名 API（`_wrap_labels` / `_wrap_len`）。
        self._tips: Optional[WrapGroup] = None

        # 暂存：**跨分组切换存活**（见模块开头与 `settings_ui._stash_session`）。
        st0 = stage or {}
        self._stage_modes: Dict[str, str] = dict(st0.get("modes") or {})
        self._stage_persona: Dict[str, str] = dict(st0.get("personas") or {})
        self._stage_rel: Dict[str, str] = dict(st0.get("relationships") or {})

        self._rows_data: List[dict] = []
        self._keys: List[str] = []

        self.setStyleSheet("background:%s;" % style.p["bg"])
        self._build()

        if preselect and preselect in self._row_widgets:
            self._select(preselect)
        elif self._keys:
            self._select(self._keys[0])

    # ------------------------------------------------------------ 数据

    def _rows(self) -> List[dict]:
        try:
            sessions = self._source() if self._source else None
        except Exception as e:
            self._log("读会话表失败: %s: %s" % (type(e).__name__, e))
            sessions = None
        try:
            seen = self._seen_source() if self._seen_source else None
        except Exception as e:
            self._log("读会话时间失败: %s: %s" % (type(e).__name__, e))
            seen = None
        return session_rows(
            sessions,
            self.store.get("persona.per_session", {}) or {},
            self.store.get("persona.relationships", {}) or {},
            self.store.get("sessions.disabled", []) or [],
            self.store.get("sessions.default_mode", "off") or "off",
            seen=seen,
        )

    def _mode_of(self, key: str, fallback: str) -> str:
        """当前该显示的 mode：暂存优先。"""
        return self._stage_modes.get(key, fallback)

    def _text_of(self, kind: str, key: str, fallback: str) -> str:
        stage = self._stage_persona if kind == "persona" else self._stage_rel
        return stage.get(key, fallback)

    # ------------------------------------------------------------ 构建

    def _build(self):
        st = self.style
        p = st.p
        rows = self._rows()
        self._rows_data = rows
        self._keys = [r["key"] for r in rows]

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        head = QtWidgets.QWidget(self)
        hlay = QtWidgets.QHBoxLayout(head)
        hlay.setContentsMargins(st.px(14), st.px(4), st.px(14), 0)
        hlay.setSpacing(0)
        nbl = count_blacklisted(self.store.get)
        tip = "共 %d 个会话" % len(rows)
        if nbl:
            tip += " · 黑名单 %d 个（在「会话与记录」里改）" % nbl
        hlay.addWidget(_label(st, tip, font_name="small", color_key="text_faint"))
        hlay.addStretch(1)
        lay.addWidget(head, 0)

        body = QtWidgets.QWidget(self)
        blay = QtWidgets.QHBoxLayout(body)
        blay.setContentsMargins(st.px(8), st.px(4), st.px(8), st.px(4))
        blay.setSpacing(0)
        lay.addWidget(body, 1)

        # ---- 左：会话列表
        self.list_area = ScrollArea(st, "bg", body)
        self.list_area.setFixedWidth(st.px(LIST_W))
        blay.addWidget(self.list_area, 0)

        self._row_widgets = {}
        for r in rows:
            row = _SessionRow(self, r, self.list_area.inner)
            self.list_area.add(row)
            self._row_widgets[r["key"]] = {"frame": row, "label": row.label,
                                           "dot": row.dot, "data": r}

        # ---- 右：该会话的设置
        self.edit_area = ScrollArea(st, "bg", body)
        blay.addSpacing(st.px(10))
        blay.addWidget(self.edit_area, 1)
        # 换行由 QLabel 自己管，这个登记表只保留给 `_wrap_labels` / `_wrap_len`
        self._tips = WrapGroup(st, self.edit_area, inset=16)

    # ---- 给测试用的两个入口（在 Qt 里换行是 QLabel 自己的事，这里保留同名 API）
    @property
    def _wrap_labels(self):
        return self._tips.labels if self._tips else []

    def _wrap_len(self) -> int:
        return self._tips.width() if self._tips else self.style.px(360)

    # ------------------------------------------------------------ 选中态

    def _paint(self, key: str, on: bool):
        w = self._row_widgets.get(key)
        if not w:
            return
        w["frame"].set_selected(on)

    def _hover(self, key: str, on: bool):
        if key == self._sel:
            return
        w = self._row_widgets.get(key)
        if not w:
            return
        w["frame"].set_hover(on)

    def _select(self, key: str):
        if key == self._sel:
            return
        self._stash()                 # 先把上一个会话编辑框里的内容收好
        old = self._sel
        self._sel = key
        if old:
            self._paint(old, False)
        self._paint(key, True)
        self._render_editor()

    # ------------------------------------------------------------ 编辑器

    def _tip(self, lay, text, color_key="text_faint"):
        """建一个会随宽度自动换行的提示标签（换行交给 QLabel 自己）。

        ⚠️ Tk 版这里是 `self._tip(holder, ...).pack(...)` —— `.pack()` 那一下
            就是把标签放进容器。Qt 没有 pack，所以这里直接收一个**布局**，
            建完就加进去，调用点少一层。
        """
        lab = self._tips.add(self.edit_area, text, color_key=color_key)
        lay.addWidget(lab)
        return lab

    def _render_editor(self):
        st = self.style
        self.edit_area.clear()
        self._tips.clear()
        self._ed = None

        key = self._sel
        row = next((r for r in self._rows_data if r["key"] == key), None)

        box = QtWidgets.QWidget(self.edit_area.inner)
        lay = QtWidgets.QVBoxLayout(box)
        # ⚠️ **右边留一档**，别让内容贴着滚动条。
        #    跟设置窗口 SPEC 表单那边是同一个问题：几何上滚动条不压内容，
        #    但内容右边界 == 滚动条左边界 时，看着就是"右边被吃了一块"。
        #    左边保持 0（外面的 body 已经给了 8），只补右边。
        lay.setContentsMargins(0, 0, st.px(12), 0)
        lay.setSpacing(0)
        self.edit_area.add(box)

        if row is None:
            self._tip(lay, "左边还没有会话。微信里打开一个聊天，它就会出现在这里。")
            lay.addStretch(1)
            return

        # ---- 标题行
        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(0, st.px(2), 0, st.px(8))
        head.setSpacing(st.px(6))
        head.addWidget(_label(st, row["name"], font_name="body_bold",
                              color_key="text"))
        # ⚠️ 2026-10-13 **删掉绿色的「新」标记**（用户要求：「不需要显示绿色的新字」）。
        #    `row["is_new"]` 这个**数据**留着（注册表还在用），只是不再画在界面上 ——
        #    新会话本来就排在列表最上面（按时间排序），这个字只添乱。
        head.addStretch(1)
        lay.addLayout(head)

        # ---- 黑名单警告：必须显眼，否则就是"改了没反应"
        #
        # ⚠️ 2026-10-13 把「config 的 sessions.disabled」这种**配置键名**从文案里删了
        #    （用户要求：设置里的话要面向用户，别出现内部名字）。意思不变：
        #    黑名单是"启动时"生效的，所以在这里选「开」，下次启动照样被关掉。
        if row["blacklisted"]:
            self._tip(lay,
                      "⚠ 这个会话在黑名单里。黑名单在启动时生效 —— "
                      "你在这里选「开」，下次启动还是会被关掉。"
                      "要真正启用，先去「会话与记录」把它从黑名单里删掉。",
                      color_key="danger")
            lay.addSpacing(st.px(6))
        elif not row["known"]:
            self._tip(lay,
                      "这个会话只在配置里有记录，微信里还没出现过 —— 可能是改过名字。")
            lay.addSpacing(st.px(6))

        # ---- 模式
        lay.addWidget(_label(st, "模式", font_name="body", color_key="text"))
        mode_now = self._mode_of(key, row["mode"])
        mode_w = _ModeChoices(box, st, mode_now)
        lay.addSpacing(st.px(4))
        lay.addWidget(mode_w, 0)
        lay.addSpacing(st.px(3))
        mode_hint = self._tip(lay, MODE_HINT.get(mode_now, ""))
        lay.addSpacing(st.px(10))
        mode_w.on_change = lambda v: mode_hint.setText(MODE_HINT.get(v, ""))

        # ---- 单独人设
        lay.addWidget(_label(st, "单独人设", font_name="body", color_key="text"))
        box_persona = InputBox(st, height=st.px(96), parent=box)
        box_persona.set(self._text_of("persona", key, row["persona"]))
        lay.addSpacing(st.px(4))
        lay.addWidget(box_persona, 0)
        lay.addSpacing(st.px(3))
        self._tip(lay, "留空 = 用「人设与口吻」里的全局人设。")
        lay.addSpacing(st.px(10))

        # ---- 关系
        lay.addWidget(_label(st, "关系", font_name="body", color_key="text"))
        rel = EntryBox(st, text=self._text_of("rel", key, row["relationship"]),
                       parent=box)
        lay.addSpacing(st.px(4))
        lay.addWidget(rel, 0)
        lay.addSpacing(st.px(3))
        default_rel = self.store.get("persona.default_relationship", "") or ""
        self._tip(lay, "留空 = 用全局默认「%s」。影响称呼、语气和分寸。" % default_rel)
        lay.addSpacing(st.px(10))

        if row["custom"]:
            self._tip(lay, "这个会话已经有单独设置。把上面两个框清空即可恢复全局。",
                      color_key="accent")

        lay.addStretch(1)
        self._ed = {"mode": mode_w, "persona": box_persona, "rel": rel}

    # ------------------------------------------------------------ 暂存

    def _stash(self):
        """把当前编辑框的内容收进暂存。切会话/切分组/保存前都要调。

        ⚠️⚠️ **只记"真的改过的"**（2026-10-13 用户报「会话页，只要我打开它就会认为
            我改动了」）：
            原来是**无条件**把当前会话的模式/人设/关系写进暂存 ——
            于是**打开这一页就等于改了东西**：保存按钮冒出来、状态行说
            "会话面板的改动还没保存"，而用户一个字节都没动。
            现在拿**行里的原值**当基准：一样就把那个键从暂存里**删掉**
            （所以"改完又改回去"也等于没改）。
        ⚠️ 只记改动是**安全**的：`settings_ui._apply_session_maps()` 是往当前配置里
            **merge**（`new = dict(cur)` 再覆盖暂存里的键），
            `_apply_session_modes()` 也是逐个键写的 —— 都不要求暂存里是全量。
        """
        if self._sel is None or self._ed is None:
            return
        k = self._sel
        row = next((r for r in self._rows_data if r["key"] == k), None) or {}
        try:
            now = {"modes": self._ed["mode"].get(),
                   "personas": self._ed["persona"].get(),
                   "relationships": self._ed["rel"].get()}
            was = {"modes": row.get("mode", ""),
                   "personas": row.get("persona", ""),
                   "relationships": row.get("relationship", "")}
            for slot, stage in (("modes", self._stage_modes),
                                ("personas", self._stage_persona),
                                ("relationships", self._stage_rel)):
                if str(now[slot] or "") == str(was[slot] or ""):
                    stage.pop(k, None)          # 和原值一样（或改回来了）-> 不算改动
                else:
                    stage[k] = now[slot]
        except Exception as e:
            self._log("暂存会话设置失败: %s: %s" % (type(e).__name__, e))

    def collect(self) -> dict:
        """取回全部改动（当前编辑框 + 暂存）。"""
        self._stash()
        return {
            "modes": dict(self._stage_modes),
            "personas": dict(self._stage_persona),
            "relationships": dict(self._stage_rel),
        }

    def clear_stage(self):
        self._stage_modes = {}
        self._stage_persona = {}
        self._stage_rel = {}

    def dirty(self) -> bool:
        """有没有待保存的改动（只看模式；人设/关系一律回填，不区分）。"""
        return bool(self._stage_modes)

    @property
    def selected(self) -> Optional[str]:
        """当前选中的会话名。

        ⚠️ 和 Tk 版一样是**属性**（不是方法）—— Tk 那边就是
            `panel.selected`，调用点写的是属性访问，别改成方法。
        """
        return self._sel


# ---------------------------------------------------------------- 小工具

def _label(style: Style, text: str, font_name: str = "body",
           color_key: str = "text") -> QtWidgets.QLabel:
    """一行说明文字。

    等价 Tk 版的 `widgets.label(parent, style, text, ...)` —— Qt 里字号/颜色/换行
    都是原生属性，不需要自己算 `wraplength`（Tk 那边超宽会**静默裁字**）。
    """
    lab = QtWidgets.QLabel(text)
    lab.setFont(style.font(font_name))
    lab.setStyleSheet("color:%s; background:transparent;" % style.p[color_key])
    lab.setWordWrap(True)
    return lab
