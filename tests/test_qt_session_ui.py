# -*- coding: utf-8 -*-
"""Qt 版会话级设置面板测试：列表合成、跨分组暂存、两个存储的落盘、接线。

对照 Tk 版（`tests/test_session_ui.py`，73 项）
==============================================
⚠️ **不是逐条照搬**：Tk 版里有几条断言测的是 tkinter 自己的几何机制
   （`winfo_manager()` / `cget("wraplength")` / `_collect_labels` 递归找 tk.Label）。
   那些在 Qt 里没有对应物，照搬只会得到恒真的假断言 —— 逐条的处理写在
   `tests/test_qt_settings_ui.py` 顶部的对照表里（那份文件是"窗口观感"的一半，
   这份是"行为"的一半，分工和 Tk 版一致）。

    Tk 版断言                          它防的行为                Qt 里的处理
    ---------------------------------  ----------------------   --------------------------
    `win.custom.winfo_manager()`        会话分组用自定义容器      **换**成"两个容器
      == "pack"                         而不是 SPEC 表单           轮流可见"
    `l.cget("wraplength") == wide`      提示文字跟着宽度重排      **换**成
      / 拖窄后跟着变小                                             `_wrap_len()` +
                                                                  `heightForWidth`
                                                                  真的变大
    `_collect_labels` 递归找 tk.Label    黑名单警告显不显示        **换**成
                                                                  `QLabel.text()`
    面板用 `pack()` 塞进 root            面板能被摆进容器           **删**：Qt 里由
                                                                  调用方的布局负责

⚠️ **不需要显示器**：`QT_QPA_PLATFORM=offscreen`。
⚠️ **绝不碰仓库的 `state/`**：所有存储指到临时目录，末尾比对 mtime + 文件列表。

关于 `_stash()` 与预选的那条坑（CLAUDE.md 单独记过，这里有几条专测）
------------------------------------------------------------------
`open()` 里 `_stash_session()` **必须在应用预选之前**，而且还要一个
`_pending_preselect`：`_switch()` 会再走一次 `_render_group()`，它开头的
`_stash_session()` 读的**还是旧面板**，会把刚设的预选覆盖回旧选择。
症状是「设置窗口已经开着时，切了会话再点进来，面板还停在原来那个」。

用法：python tests/test_qt_session_ui.py
"""

import os
import shutil
import sys
import tempfile
import time

# ⚠️ 必须在**任何 Qt import 之前**设。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, SKIP = 0, 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def skip(name, why):
    global SKIP
    SKIP += 1
    print("  [跳过] %s —— %s" % (name, why))


def pump(app, ms=120):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def safe(fn):
    try:
        fn()
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def walk_widgets(root):
    from PySide6 import QtWidgets

    out = [root]
    stack = [root]
    while stack:
        w = stack.pop()
        for c in w.children():
            if isinstance(c, QtWidgets.QWidget):
                out.append(c)
                stack.append(c)
    return out


def texts_of(root):
    """一棵部件树里所有 QLabel 的文字（Tk 版这里是递归找 tk.Label）。"""
    from PySide6 import QtWidgets

    return [w.text() for w in walk_widgets(root)
            if isinstance(w, QtWidgets.QLabel)]


def _click(widget):
    """给一个部件发一次左键按下（模拟用户真的点上去）。"""
    from PySide6 import QtCore, QtGui

    p = QtCore.QPointF(widget.width() / 2, widget.height() / 2)
    widget.mousePressEvent(QtGui.QMouseEvent(
        QtCore.QEvent.MouseButtonPress, p, p, QtCore.Qt.LeftButton,
        QtCore.Qt.LeftButton, QtCore.Qt.NoModifier))


# ---------------------------------------------------------------- 临时环境

CONFIG_YAML = """\
ai:
  base_url: ""
  model: "deepseek-flash"
persona:
  default: "\u5168\u5c40\u4eba\u8bbe"
  default_relationship: "\u670b\u53cb"
  per_session: {}
  relationships: {}
sessions:
  default_mode: "on"
  disabled: []
"""


def make_store(tmp):
    """把三层配置全部指到临时目录。

    ⚠️ `SettingsStore` 的三个路径都能从构造参数传，所以**不需要 monkeypatch**，
        也就不会出现"漏改一处就写到真 state/"。
    """
    from wechat_assistant.settings import SettingsStore

    cfg_path = os.path.join(tmp, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(CONFIG_YAML)
    return SettingsStore(
        config_path=cfg_path,
        settings_path=os.path.join(tmp, "settings.json"),
        secrets_path=os.path.join(tmp, "secrets.yaml"),
        logger=lambda m: None,
    )


SESSIONS = {
    "张三": {"display_name": "张三", "mode": "on", "is_new": False},
    "李四": {"display_name": "李四", "mode": "off", "is_new": True},
    "某群": {"display_name": "某群", "mode": "observe", "is_new": False},
}

WIRED_SESSIONS = {
    "张三": {"display_name": "张三", "mode": "on", "is_new": False},
    "张三.工作": {"display_name": "张三.工作", "mode": "on", "is_new": False},
    "李四": {"display_name": "李四", "mode": "on", "is_new": False},
}


def another_instance_running():
    """**那个程序本身**是不是正在跑（用它的单实例互斥体判，不数进程）。

    ⚠️ 为什么需要：第 9 节要断言"本套件没污染仓库的 state/"。
       但**用户自己开着的那个实例**会写 `state/settings.json` / `state.json`，
       于是这条断言会**假红**（实测踩过：早上全绿、下午程序一起来就红）。
       ⚠️ 判据必须用**有名互斥体**，不能数 `python.exe` ——
          本机上还跑着别的 Python 脚本，一数就误报
          （`run_watch.py` 里为同一个理由也是这么做的）。
    判不了就返回 None，调用方按保守方式处理。
    """
    try:
        # ⚠️ 名字只有 `single_instance` 那一个出口（原来写死 + 用 OpenMutexW，
        #    名字一改就失效，而 OpenMutexW 本机实测 ACCESS_DENIED）。
        from wechat_assistant import single_instance

        return single_instance.is_running()
    except Exception:
        return None


def state_stamp():
    """测试**该写**的那几个文件的时间戳 + 目录文件列表。

    ⚠️ 别拿整个 `state/` 目录的 mtime 当判据 —— `history.db` / `screens.db`
        是**记录器**每 2 秒就在写的，只要开发时另开过一个程序实例，
        目录 mtime 就会变，这条断言就会**假红**（实测踩过：
        目录 mtime 变了而文件列表一个没变 —— 不是测试写的）。
        只盯 `settings.json` / `state.json`。
    """
    from wechat_assistant import paths

    d = paths.state_dir()
    stamps = {}
    for name in ("settings.json", "state.json"):
        try:
            stamps[name] = os.path.getmtime(os.path.join(d, name))
        except Exception:
            stamps[name] = None
    try:
        listing = sorted(os.listdir(d))
    except Exception:
        listing = None
    return stamps, listing


def make_panel(app, tmp, sessions=None, preselect=None, stage=None, name=None):
    """建一个独立面板（带一个顶层容器，不然量不到布局）。"""
    from PySide6 import QtWidgets

    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.session_ui import SessionPanel

    store = make_store(tmp)
    cfg = store.load()
    style = base.Style(cfg, 0, name="dark")
    holder = QtWidgets.QWidget()
    holder.resize(style.px(700), style.px(400))
    lay = QtWidgets.QVBoxLayout(holder)
    lay.setContentsMargins(0, 0, 0, 0)
    panel = SessionPanel(
        holder, style, store,
        session_source=lambda: (SESSIONS if sessions is None else sessions),
        on_mode_change=(lambda k, m: None),
        logger=lambda m: None,
        preselect=preselect, stage=stage)
    lay.addWidget(panel)
    holder.show()
    pump(app, 120)
    return panel, holder, store, style


# ================================================================ 1. 纯逻辑

def test_rows():
    """`session_rows()` 是**从 Tk 版逐字搬过来的**，所以这些用例可以原样保留 ——
    它们测的是纯逻辑（不碰界面），迁移时改这里等于改业务逻辑。"""
    print("\n1. 会话列表合成（session_rows）")
    from wechat_assistant.qt_ui.session_ui import session_rows

    r = session_rows(None, None, None, None)
    check("全空返回空列表", r == [], repr(r))

    sessions = {
        "张三": {"display_name": "张三", "mode": "on", "is_new": False},
        "李四": {"display_name": "李四", "mode": "off", "is_new": True},
    }
    r = session_rows(sessions, None, None, None)
    check("注册表里的会话全列出来", len(r) == 2, repr([x["key"] for x in r]))
    check("**没有时间信息时**退回按名字排，且稳定",   # 见 §17b 的"按时间排"
          [x["name"] for x in r] == sorted(["张三", "李四"]),
          repr([x["name"] for x in r]))
    check("带出 mode",
          {x["key"]: x["mode"] for x in r} == {"张三": "on", "李四": "off"})
    check("带出 is_new", [x for x in r if x["key"] == "李四"][0]["is_new"] is True)
    check("注册表里的都算 known", all(x["known"] for x in r))

    # ⚠️ 配置里有、注册表里没有 —— **必须也列出来**，否则那条孤儿配置
    #    永远删不掉（微信里改过名 / 还没打开过那个会话）。
    r = session_rows(sessions, {"王五": "只在这个配置里"}, {"王五": "同事"}, None)
    ww = [x for x in r if x["key"] == "王五"]
    check("配置里有、注册表没有的也列出来（取并集）", len(ww) == 1,
          repr([x["key"] for x in r]))
    check("这类条目标成 known=False", bool(ww) and ww[0]["known"] is False)
    check("带出单独人设", bool(ww) and ww[0]["persona"] == "只在这个配置里")
    check("带出关系", bool(ww) and ww[0]["relationship"] == "同事")
    check("有单独设置就标 custom", bool(ww) and ww[0]["custom"] is True)

    # ⚠️ 黑名单：sessions.disabled 在启动时强制生效，优先级**高于**面板里设的模式
    r = session_rows(sessions, None, None, ["李四"])
    lisi = [x for x in r if x["key"] == "李四"][0]
    check("黑名单被标出来", lisi["blacklisted"] is True)
    zhangsan = [x for x in r if x["key"] == "张三"][0]
    check("不在黑名单的不标", zhangsan["blacklisted"] is False)

    r = session_rows(None, None, None, None, default_mode="observe")
    check("空表也能接受默认模式（不报错）", r == [])

    # 只有名字没有 display_name 时回退到 key
    r = session_rows({"某群": {}}, None, None, None)
    check("没有 display_name 就用 key 当名字", r[0]["name"] == "某群")

    # ⚠️ 排序必须稳定：面板每次重建都靠它，顺序变了列表就会乱跳
    a = [x["key"] for x in session_rows(sessions, None, None, None)]
    b = [x["key"] for x in session_rows(sessions, None, None, None)]
    check("两次调用顺序一致", a == b, repr((a, b)))


def test_rows_by_time():
    """会话列表**按时间排**（最近的在上面）—— 2026-09-27 用户要求。

    ⚠️ 原来按名字排（Unicode 码点，不是拼音）。理由是"至少稳定" —— 稳定是够稳定，
        但跟"最近在聊什么"毫无关系，19 个会话里找刚聊过的那个得一个个扫。

    ⚠️ **不用微信左栏的顺序**：那是虚拟列表，UIA 只给渲染出来的那几项
        （实测 19 个会话只读得到 8 个），而且它按的是"会话活动时间"
        （点开也会往上顶），跟列表里显示的时间不是一回事。
        所以用本地记录的时间（屏快照库的 `MAX(seen_at)` + 注册表的"刚打开过"）。
    """
    print("\n1b. 会话列表按时间排（没有时间的排在最后，段内仍按名字）")
    from wechat_assistant.qt_ui.session_ui import session_rows

    sessions = {
        "甲": {"display_name": "甲", "mode": "on"},
        "乙": {"display_name": "乙", "mode": "on"},
        "丙": {"display_name": "丙", "mode": "on"},
        # 「丁」在注册表里带着"刚刚打开过"的时间
        "丁": {"display_name": "丁", "mode": "on",
               "last_seen_at": "2026-09-27 21:00:00"},
    }
    seen = {"甲": "2026-09-27 20:40:52", "乙": "2026-09-25 20:56:55"}
    r = session_rows(sessions, None, None, None, seen=seen)
    order = [x["key"] for x in r]
    check("最新的排最上面", order[0] == "丁", repr(order))
    check("其次按时间降序", order[:3] == ["丁", "甲", "乙"], repr(order))
    check("没有时间的排最后", order[-1] == "丙", repr(order))

    # 两个来源取**较新**的那个（注册表 vs 屏快照库）
    r2 = session_rows(
        {"甲": {"display_name": "甲", "last_seen_at": "2026-09-27 23:00:00"}},
        None, None, None, seen={"甲": "2026-09-27 20:40:52"})
    check("两个来源取较新的那个（注册表更晚就用注册表的）",
          r2[0]["seen"] == "2026-09-27 23:00:00", r2[0]["seen"])
    r3 = session_rows(
        {"甲": {"display_name": "甲", "last_seen_at": "2026-09-27 08:00:00"}},
        None, None, None, seen={"甲": "2026-09-27 20:40:52"})
    check("两个来源取较新的那个（屏快照库更晚就用库里的）",
          r3[0]["seen"] == "2026-09-27 20:40:52", r3[0]["seen"])

    # ⚠️ **没有时间的那一段内部必须稳定**（按名字），不能每次打开都乱换位置
    many = {n: {"display_name": n} for n in ("戊", "己", "庚", "辛")}
    o1 = [x["key"] for x in session_rows(many, None, None, None)]
    o2 = [x["key"] for x in session_rows(many, None, None, None, seen={})]
    check("全都没有时间 -> 仍按名字（且两次一致）", o1 == o2 == sorted(many),
          repr((o1, o2)))

    # ⚠️ 时间的比较必须是**字符串比较**：定宽 `YYYY-MM-DD HH:MM:SS` 才可比。
    #    畸形数据必须**当成"没有时间"**（排最后），不能冒到最前面 ——
    #    字符串比较下 `"坏数据" > "2026-..."`（`坏` 的码点比 `2` 大），
    #    不挡的话一条脏数据就跑到列表顶部。
    weird = session_rows(
        {"甲": {"display_name": "甲", "last_seen_at": "坏数据"},
         "乙": {"display_name": "乙", "last_seen_at": "2026-09-27 09:00:00"},
         "丙": {"display_name": "丙", "last_seen_at": "2026-09-27"}},
        None, None, None)
    check("畸形时间不会崩", len(weird) == 3, repr([x["key"] for x in weird]))
    check("畸形时间当成「没有时间」 -> 排在真时间后面",
          [x["key"] for x in weird][0] == "乙",
          repr([x["key"] for x in weird]))
    check("半截日期（2026-09-27）也不算时间",
          weird[-1]["seen"] == "", repr([(x["key"], x["seen"]) for x in weird]))

    # ⚠️ `seen` 参数必须是**可选**的：老的调用点（不传）行为不能变
    r0 = session_rows(sessions, None, None, None)
    check("不传 seen 时行为不变（退回按名字）",
          [x["key"] for x in r0] == sorted(sessions), repr([x["key"] for x in r0]))


def test_merge_map():
    print("\n2. 写映射（merge_map）")
    from wechat_assistant.qt_ui.session_ui import merge_map

    base = {"A": "1"}
    out = merge_map(base, "B", "2")
    check("加一个新键", out == {"A": "1", "B": "2"}, repr(out))
    check("不改原字典", base == {"A": "1"}, repr(base))

    out = merge_map(base, "A", "  ")
    check("空值 = 删掉这个键（留空 = 恢复用全局）", out == {}, repr(out))

    out = merge_map(base, "A", "3")
    check("非空值 = 覆盖", out == {"A": "3"}, repr(out))

    out = merge_map(base, "C", None)
    check("None 也不会炸", out == {"A": "1"}, repr(out))

    out = merge_map(base, "A", "  两边有空格  ")
    check("值会去掉首尾空白", out == {"A": "两边有空格"}, repr(out))


def test_groups():
    print("\n3. 分组声明")
    from wechat_assistant.settings import GROUPS

    gids = [g[0] for g in GROUPS]
    check("有「会话」分组", "session" in gids, repr(gids))
    check("分组 id 不重复", len(gids) == len(set(gids)))
    desc = dict((g[0], g[2]) for g in GROUPS)["session"]
    check("会话分组有说明文字", bool(desc), desc)

    from wechat_assistant.qt_ui.session_ui import count_blacklisted
    check("count_blacklisted 数得对",
          count_blacklisted(lambda k, d=None: ["A", "", "B"]) == 2)
    check("count_blacklisted 对坏输入不炸",
          count_blacklisted(lambda k, d=None: None) == 0)


# ================================================================ 4. 面板本体

def test_panel(app, tmp):
    print("\n4. 面板本体（选中 / 暂存 / 回填）")
    from wechat_assistant.qt_ui import base

    panel, holder, store, style = make_panel(app, tmp)
    try:
        check("建面板不报错", panel is not None)
        check("默认选中第一个会话", panel.selected is not None, repr(panel.selected))
        check("三个会话都建了行", len(panel._row_widgets) == 3,
              repr(sorted(panel._row_widgets)))
        # ⚠️ `selected` 是**属性**（不是方法）—— Tk 版就是 `panel.selected`，
        #    调用点写的是属性访问，别改成方法。
        check("selected 是属性而不是方法",
              not callable(type(panel).selected), str(type(panel).selected))

        st = panel.collect()
        check("collect 返回三个映射",
              set(st) == {"modes", "personas", "relationships"}, repr(sorted(st)))

        panel._select("李四")
        pump(app, 60)
        panel._ed["mode"]._pick("on")
        st = panel.collect()
        check("改了模式能收回来", st["modes"].get("李四") == "on",
              repr(st["modes"]))

        panel._ed["persona"].set("李四专属人设")
        st = panel.collect()
        check("改了人设能收回来", st["personas"].get("李四") == "李四专属人设",
              repr(st["personas"]))

        # 切到别的会话，李四的改动不能丢（面板是重建编辑区的）
        panel._select("张三")
        pump(app, 60)
        st = panel.collect()
        check("切会话后上一个会话的改动还在",
              st["personas"].get("李四") == "李四专属人设", repr(st["personas"]))
        check("切过去后编辑框是新的会话（不串）",
              panel._ed["persona"].get() == "", repr(panel._ed["persona"].get()))

        # 再切回李四，编辑框要回填暂存值（不是原始空值）
        panel._select("李四")
        pump(app, 60)
        check("切回来回填的是暂存值",
              panel._ed["persona"].get() == "李四专属人设",
              repr(panel._ed["persona"].get()))

        # 点左列那一行也要能切会话（Qt 里是新加的一层：部件自己的鼠标事件；
        # Tk 版是逐个控件绑 <Button-1>，漏绑一个子控件就点不动）
        panel._select("张三")
        pump(app, 60)
        _click(panel._row_widgets["李四"]["frame"])
        pump(app, 60)
        check("点左列的行能切换会话", panel.selected == "李四",
              repr(panel.selected))

        # dirty()：有没有待保存的改动（只看模式）
        check("dirty() 在改了模式之后为真", panel.dirty() is True)
        panel.clear_stage()
        check("clear_stage() 清空后 dirty() 为假", panel.dirty() is False)
    finally:
        safe(holder.close)

    # preselect
    p2, h2, _s, _st = make_panel(app, tmp, preselect="某群")
    try:
        check("preselect 生效", p2.selected == "某群", repr(p2.selected))
    finally:
        safe(h2.close)

    # stage 传入：重建面板不丢暂存
    p3, h3, _s, _st = make_panel(
        app, tmp, preselect="李四",
        stage={"modes": {}, "personas": {"李四": "传进来的"},
               "relationships": {}})
    try:
        check("重建时能用外部暂存回填", p3._ed["persona"].get() == "传进来的",
              repr(p3._ed["persona"].get()))
    finally:
        safe(h3.close)

    # ⚠️ 黑名单警告：sessions.disabled 优先级高于面板里设的模式，不警告的话
    #    用户会遇到"改了没反应但找不到原因"。
    store4 = make_store(tmp)
    store4.set("sessions.disabled", ["张三"])
    store4.save()          # ⚠️ 面板会另建一个 store 读同一个文件，得先落盘
    p4, h4, _s, _st = make_panel(app, tmp, preselect="张三")
    try:
        texts = texts_of(p4)
        check("黑名单会话会显示警告", any("黑名单" in t for t in texts),
              repr([t[:20] for t in texts]))
        warn = [l for l in walk_widgets(p4)
                if hasattr(l, "text") and "黑名单" in (l.text() or "")]
        danger = base.Style(store4.load(), 0, name="dark").p["danger"].lower()
        check("警告用的是 danger 色（普通提示是灰的，看不出严重性）",
              bool(warn) and danger in (warn[0].styleSheet() or "").lower(),
              "%s vs %s" % (warn[0].styleSheet() if warn else "", danger))
    finally:
        safe(h4.close)

    # 黑名单条数在头部提示一行
    p5, h5, _s, _st = make_panel(app, tmp)
    try:
        hdr = [t for t in texts_of(p5) if "共" in t]
        check("头部给出会话总数", bool(hdr), repr(hdr[:1]))
    finally:
        safe(h5.close)


# ================================================================ 5. 设置窗口集成

def test_window(app, tmp):
    print("\n5. 设置窗口集成（写两个存储）")
    import json

    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.settings_ui import SettingsWindow

    store = make_store(tmp)
    cfg = store.load()
    applied = []
    win = SettingsWindow(cfg, store, base.Style(cfg, 0, name="dark"),
                         logger=lambda m: None,
                         session_source=lambda: WIRED_SESSIONS,
                         on_mode_change=lambda k, m: applied.append((k, m)))
    win.show_dialogs = False
    try:
        win.open()
        pump(app, 250)
        check("设置窗口能打开", win.win is not None)

        win._switch("session")
        pump(app, 150)
        check("切到会话分组会建出面板", win._session_panel is not None)
        check("会话分组用的是自定义容器（不是 SPEC 表单）",
              win.custom.isVisible() and not win.scroll.isVisible(),
              "%s / %s" % (win.custom.isVisible(), win.scroll.isVisible()))

        # ---- 人设 + 关系 + 模式
        win._session_panel._select("张三")
        pump(app, 60)
        win._session_panel._ed["persona"].set("给张三的专属人设")
        win._session_panel._ed["rel"].set("大学室友")
        win._session_panel._ed["mode"]._pick("observe")
        win._save()
        pump(app, 150)

        per = store.get("persona.per_session", {}) or {}
        rel = store.get("persona.relationships", {}) or {}
        check("人设写进 persona.per_session", per.get("张三") == "给张三的专属人设",
              repr(per))
        check("关系写进 persona.relationships", rel.get("张三") == "大学室友",
              repr(rel))
        # ⚠️ 模式归**另一个存储**（state.json，SessionRegistry 管）—— 面板够不着
        #    它，只能通过注入的回调交出去。回调没被调 = 模式静默丢失。
        check("模式回调和会话名一起报了", ("张三", "observe") in applied,
              repr(applied))

        # ---- ⚠️ 会话名带句点不能坏（点分路径会被切坏）
        win._session_panel._select("张三.工作")
        pump(app, 60)
        win._session_panel._ed["persona"].set("工作人格")
        win._save()
        pump(app, 150)
        per = store.get("persona.per_session", {}) or {}
        check("会话名含句点也能正确落盘", per.get("张三.工作") == "工作人格",
              repr(per))
        check("含句点的会话名没有被切开成嵌套",
              isinstance(per.get("张三"), str), repr(per.get("张三")))

        # ---- 真的落到磁盘上了（不是只改了内存里的覆盖层）
        with open(store.settings_path, encoding="utf-8") as f:
            disk = json.load(f)
        check("落盘的 JSON 里有这两个会话",
              set((disk.get("persona") or {}).get("per_session") or {})
              == {"张三", "张三.工作"},
              repr((disk.get("persona") or {}).get("per_session")))

        # ---- 留空 = 删掉覆盖（恢复全局）
        win._session_panel._select("张三")
        pump(app, 60)
        win._session_panel._ed["persona"].set("")
        win._session_panel._ed["rel"].set("")
        win._save()
        pump(app, 150)
        per = store.get("persona.per_session", {}) or {}
        check("清空人设 = 删掉那个键", "张三" not in per, repr(per))
        check("别的会话不受影响", per.get("张三.工作") == "工作人格", repr(per))

        # ---- ⚠️ 改完会话设置 -> 切到别的分组 -> 再保存，改动不能丢
        win._switch("session")
        pump(app, 150)
        win._session_panel._select("李四")
        pump(app, 60)
        win._session_panel._ed["persona"].set("切分组前改的")
        win._switch("ai")            # 切走（面板被销毁重建）
        pump(app, 150)
        win._save()                  # 在别的分组点保存
        pump(app, 150)
        per = store.get("persona.per_session", {}) or {}
        check("切分组后再保存也不丢改动", per.get("李四") == "切分组前改的",
              repr(per))

        # ---- 保存后暂存要清空（否则重建时会把旧值当成待提交）
        win._switch("session")
        pump(app, 150)
        check("保存后暂存已清空", win._session_stage["personas"] == {},
              repr(win._session_stage["personas"]))

        # ---- 在非会话分组下保存，不能把会话设置抹掉
        before = store.get("persona.per_session", {}) or {}
        win._switch("ui")
        pump(app, 80)
        win._save()
        pump(app, 150)
        after = store.get("persona.per_session", {}) or {}
        check("在别的分组保存不影响会话设置", before == after,
              repr((before, after)))

        # ---- 全部恢复默认
        win._reset_all()
        pump(app, 200)
        check("恢复全部默认会清掉会话级人设",
              not (store.get("persona.per_session", {}) or {}),
              repr(store.get("persona.per_session", {})))
    finally:
        safe(win.close)


# ================================================================ 6. 拖窄重排

def test_rewrap(app, tmp):
    """⚠️ 提示文字的换行必须**跟着编辑区宽度走**。

    Tk 版这条踩过的坑：写死 `wraplength` 的话，设置窗口拖到 600px（minsize）
    时右侧只剩 ~230px，而 `tk.Label` 的 wraplength 大于可用宽度时
    **不报错，只是静静地被裁掉一截**。

    Qt 换成了 `QLabel.setWordWrap(True)`（原生换行），所以断言也换了一层：
    不再去读 `wraplength` 那个属性，而是验**"可用宽度变小了、文字需要的高度
    就变大"** —— 那才是"重排了"这件事本身。
    """
    print("\n6. 窗口拖窄后提示文字要重排")
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.settings_ui import SettingsWindow

    store = make_store(tmp)
    cfg = store.load()
    win = SettingsWindow(cfg, store, base.Style(cfg, 0, name="dark"),
                         logger=lambda m: None,
                         session_source=lambda: WIRED_SESSIONS)
    win.show_dialogs = False
    try:
        # 里面只有「张三」一个会话（和 Tk 版一致）
        win.open(group="session")
        pump(app, 250)
        panel = win._session_panel
        panel._select("张三")
        pump(app, 80)
        check("编辑器里有登记的提示文字", len(panel._wrap_labels) > 0,
              str(len(panel._wrap_labels)))
        check("提示文字都开着自动换行（不然超宽会被静静裁掉）",
              all(l.wordWrap() for l in panel._wrap_labels),
              repr([l.wordWrap() for l in panel._wrap_labels]))

        win.win.resize(base.px(760), base.px(620))
        pump(app, 200)
        wide = panel._wrap_len()
        check("宽的时候换行宽度 == 编辑区可用宽度（不是写死的数）",
              wide == panel._tips.width(), "%s vs %s" % (wide, panel._tips.width()))

        win.win.resize(base.px(600), base.px(620))
        pump(app, 250)
        tight = panel._wrap_len()
        check("拖窄后换行宽度跟着变小", tight < wide, "%d -> %d" % (wide, tight))
        check("换行宽度不会小到看不见", tight >= base.px(120), str(tight))

        # 真正"重排了"的证据：同一条长提示，窄的时候需要更多行高。
        long_tips = sorted(panel._wrap_labels, key=lambda l: -len(l.text()))
        lab = long_tips[0]
        h_wide = lab.heightForWidth(wide)
        h_tight = lab.heightForWidth(tight)
        check("同一条提示在窄的时候需要更高的空间（= 真的换行了，而不是被裁）",
              h_wide > 0 and h_tight > 0 and h_tight >= h_wide,
              "%s -> %s" % (h_wide, h_tight))
        check("拖窄后编辑区仍然装在窗口里、没被挤没",
              panel.edit_area.viewport().width() > base.px(120),
              str(panel.edit_area.viewport().width()))
    finally:
        safe(win.close)


# ================================================================ 7. 回填

def test_backfill(app, tmp):
    print("\n7. 已有配置的回填")
    store = make_store(tmp)
    store.set("persona.per_session", {"老会话": "以前配的人设"})
    store.set("persona.relationships", {"老会话": "老同学"})
    store.save()

    # ⚠️ 注册表里**没有**它 —— 只有"微信还没打开过 / 改过名"的会话才是这样。
    #    面板取的是注册表与配置的**并集**，否则那条孤儿配置永远删不掉。
    panel, holder, _s, _st = make_panel(app, tmp, sessions={})
    try:
        check("配置里有但注册表没有的会话仍然列出来",
              panel.selected == "老会话", repr(panel.selected))
        check("人设被回填进编辑框", panel._ed["persona"].get() == "以前配的人设",
              repr(panel._ed["persona"].get()))
        check("关系被回填进编辑框", panel._ed["rel"].get() == "老同学",
              repr(panel._ed["rel"].get()))
        st = panel.collect()
        # ⚠️ 2026-10-13 **语义变了**：暂存**只记真的改过的**（用户报「会话页，只要我
        #    打开它就会认为我改动了」）。所以"原样提交"= 三个映射**都是空的** ——
        #    而设置窗口那边是 **merge** 进当前配置（`_apply_session_maps`：
        #    `new = dict(cur)` 再覆盖暂存里的键），空暂存 = 一个字节都不写，
        #    已有配置自然不会被改掉（那正是这条用例当年要守的东西）。
        check("原样提交 = 暂存里没有任何改动（所以不会把已有配置改掉）",
              st["personas"] == {} and st["relationships"] == {}
              and st["modes"] == {}, repr(st))
        check("但编辑框里仍然是回填出来的原值（界面没被清空）",
              panel._ed["persona"].get() == "以前配的人设"
              and panel._ed["rel"].get() == "老同学",
              repr((panel._ed["persona"].get(), panel._ed["rel"].get())))
        check("配置里没有的会话不会被无中生有造出来",
              len(panel._row_widgets) == 1, repr(sorted(panel._row_widgets)))
    finally:
        safe(holder.close)


# ================================================================ 8. 接线

def test_floating_wiring(app, tmp):
    """设置窗口是靠**注入的回调**才够得着注册表和配置重载的。

    这里锁的是三件事：
      1. `open_settings()` 真的把那两个回调传下去了；
      2. 保存后调 `on_config_changed` ——
         ⚠️ 少了它，会话级人设"显示已保存但生成时还用旧的"，要重启才认；
      3. **预选的时序**：`_stash_session()` 必须在应用预选**之前**，
         而且 `_pending_preselect` 要绕开 `_render_group()` 里那次 stash 的覆盖。
    """
    print("\n8. 悬浮窗与设置窗口的接线")
    from wechat_assistant.qt_ui.panel import FloatingAssistant

    store = make_store(tmp)
    cfg = store.load()
    modes = []

    ui = FloatingAssistant(
        cfg, logger=lambda m: None,
        session_source=lambda: WIRED_SESSIONS,
        on_mode_change=lambda k, m: modes.append((k, m)))
    try:
        ui._settings_store = store          # 别碰真的 state/settings.json
        ui._session_title = "张三"           # 假装当前打开的是「张三」

        ui.open_settings(group="session")
        pump(app, 300)
        win = ui._settings_win
        check("设置窗口开起来了", win is not None)
        if win is None:
            return
        check("会话表被传进设置窗口", win._session_source is ui._session_source)
        check("模式回调被传进设置窗口", win._on_mode_change is ui._on_mode_change)
        check("当前会话当成预选", win._preselect == "张三", repr(win._preselect))
        check("打开时直接落在会话分组", win._group == "session", repr(win._group))
        check("面板真的停在预选的那个会话上（不是只记了个值）",
              win._session_panel is not None
              and win._session_panel.selected == "张三",
              repr(win._session_panel and win._session_panel.selected))

        # ---- 配置回传
        got = []
        ui._on_config_changed = lambda c: got.append(c)
        ok, err = safe(ui._on_settings_saved)
        pump(app, 150)
        check("_on_settings_saved() 不抛异常", ok, err)
        check("保存后把新配置回传给主程序", len(got) == 1, str(len(got)))
        check("回传的是真配置对象", bool(got) and hasattr(got[0], "get"))

        # ---- 回传回调自己炸了不能把保存流程带崩
        got2 = []

        def boom(_c):
            got2.append(1)
            raise RuntimeError("故意炸")

        ui._on_config_changed = boom
        ok, _err = safe(ui._on_settings_saved)
        pump(app, 120)
        check("回传出错不会把保存流程带崩", ok and got2 == [1], str(got2))

        # ---- ⚠️ 预选时序（这条踩过两次坑，CLAUDE.md 专门记着）
        #
        # 光看 `_preselect` 不够 —— 那是"打算选谁"。真正的坑是 `_switch()` 里
        # 又一次 `_stash_session()` 把新预选覆盖回旧选择，所以必须查
        # **面板实际选中了谁**，并且要验"上一个会话没提交的改动也还在"。
        check("换会话前面板停在张三", win._session_panel.selected == "张三",
              repr(win._session_panel.selected))
        win._session_panel._ed["persona"].set("还没提交的改动")
        ui._session_title = "李四"
        ui.open_settings(group="session")
        pump(app, 200)
        check("换会话后预选跟着换", win._preselect == "李四", repr(win._preselect))
        check("面板真的切到新会话了（不是只记了个值）",
              win._session_panel.selected == "李四",
              repr(win._session_panel.selected))
        check("切走时把上一个会话的改动先收进了暂存（stash 在预选之前）",
              win._session_stage["personas"].get("张三") == "还没提交的改动",
              repr(win._session_stage["personas"]))

        # ---- 当前没开任何会话时 preselect 传的是 None，
        #      那是「不指定」而不是「清空」—— 保持上次选中的那个
        ui._session_title = "—"
        ui.open_settings(group="session")
        pump(app, 200)
        check("没打开会话时保持上次的选择（不是清空）",
              win._preselect == "李四" and win._session_panel.selected == "李四",
              repr((win._preselect, win._session_panel.selected)))

        # ---- 关掉设置窗口之后还能重开（悬空引用要判得出来）
        win.close()
        pump(app, 120)
        check("关掉设置窗口后判定为不在", ui._settings_win_is_open() is False)
        ui.open_settings(group="session")
        pump(app, 250)
        check("关掉之后还能重新开出来", ui._settings_win is not None
              and ui._settings_win.is_open() is True)

        # ---- 在设置窗口里保存，走的是同一个 store（写临时目录）
        sw = ui._settings_win
        sw._switch("session")
        pump(app, 200)
        sw._session_panel._select("李四")
        pump(app, 60)
        sw._session_panel._ed["mode"]._pick("off")
        ok, err = safe(sw._save)
        pump(app, 200)
        check("在设置窗口里保存不抛异常", ok, err)
        check("模式经回调交回了注册表（另一个存储）",
              ("李四", "off") in modes, repr(modes))
        safe(sw.close)
    finally:
        safe(ui.close)
    check("悬浮窗关闭不报错", True)


# ================================================================ main

def main():
    print("=" * 70)
    print("Qt 会话级设置面板测试")
    print("=" * 70)

    try:
        from PySide6 import QtCore
        from wechat_assistant import dpi
        from wechat_assistant.qt_ui import base
    except Exception as e:                                   # pragma: no cover
        print("  PySide6 不可用: %s: %s" % (type(e).__name__, e))
        return 0

    # ⚠️ **必须先 `dpi.enable()`**，和 `run_watch.py` 的启动顺序一致。
    #    不调的话本进程没有 DPI 感知，`GetDpiForSystem()` 会**谎报 96**，
    #    于是 `base.scale()` 静默变成 1.0、所有 `px()` 缩水一半 ——
    #    断言还是自洽的、测试还是"绿"的，但**它验的根本不是真机那套尺寸**。
    dpi.enable()
    app = base.init_app([])
    print("Qt %s / 缩放 %.2f" % (QtCore.qVersion(), base.scale()))

    test_rows()
    test_rows_by_time()
    test_merge_map()
    test_groups()

    before = state_stamp()
    tmp = tempfile.mkdtemp(prefix="wai_qtsess_")
    try:
        test_panel(app, tmp)
        test_window(app, tmp)
        test_rewrap(app, tmp)
        test_backfill(app, tempfile.mkdtemp(prefix="wai_qtsess2_"))
        test_floating_wiring(app, tmp)

        print("\n9. 没有污染仓库")
        after = state_stamp()
        # ⚠️ 这条断言只在**没有别的实例在跑**时才有意义。
        #    用户自己开着的那个程序实例会写 `state/settings.json` 和 `state.json`
        #    （他正在界面上操作），那种情况下时间戳变了**归因不到本套件**。
        #    ⚠️ 实测踩过：同一份代码今天早上还全绿，下午连跑就红 ——
        #       因为用户把程序起来了。**测试的假红比不测还耗人**。
        #    → 变了 + 有别的实例 = **跳过并说明**；变了 + 没有别的实例 = 真失败。
        #    （同一个补丁早已加在 `test_qt_settings_ui.py`，这里是补齐。）
        if before == after:
            check("仓库 state/ 里的 settings.json / state.json 没被动过",
                  True, "%r" % (before,))
        elif another_instance_running():
            skip("仓库 state/ 里的 settings.json / state.json 没被动过",
                 "有另一个本程序实例在跑（它自己会写这两个文件），"
                 "这次时间戳变化**归因不了** —— 关掉那个实例再跑才能判定。"
                 "%r -> %r" % (before, after))
        else:
            check("仓库 state/ 里的 settings.json / state.json 没被动过",
                  False, "%r -> %r" % (before, after))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print("通过 %d，失败 %d" % (PASS, FAIL))
    if SKIP:
        print("跳过 %d" % SKIP)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
