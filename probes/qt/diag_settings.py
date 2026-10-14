# -*- coding: utf-8 -*-
"""Qt 版设置窗口 / 会话面板体检。

    python -X utf8 probes/qt/diag_settings.py

方向跟 `probes/diag_settings_ui.py`（Tk 版）一致，但**只做 Qt 能做的**：
量结构、跑交互、查落盘。外观类的东西（抗锯齿、透明色键）在
`QT_QPA_PLATFORM=offscreen` 下测不出来，那类断言这里一条都不写 ——
不写"看起来在测、其实恒真"的假检查。

⚠️ **绝不碰仓库里的 `state/settings.json`**：
   `SettingsStore` 的路径由构造参数决定（见 `settings.py`），
   这里全部指到临时目录，并且在开头/结尾比对仓库 `state/` 的 mtime
   （有一条检查项专门守着这件事）。
"""

import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from PySide6 import QtCore, QtWidgets                     # noqa: E402

from wechat_assistant import paths, theme                 # noqa: E402
from wechat_assistant.qt_ui import base                   # noqa: E402
from wechat_assistant.qt_ui.settings_ui import (          # noqa: E402
    MIN_H, MIN_W, SHELL_RADIUS, SettingsWindow)
from wechat_assistant.qt_ui.session_ui import (           # noqa: E402
    SessionPanel, count_blacklisted, merge_map, session_rows)
from wechat_assistant.settings import GROUPS, SPEC, SettingsStore  # noqa: E402

PASS = 0
FAIL = []
FAILED_NAMES = []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [通过] %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAILED_NAMES.append(name)
        FAIL.append(name)
        print("  [失败] %s%s" % (name, ("  " + detail) if detail else ""))


def pump(app, ms=120):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def safe(fn):
    """跑一段会碰界面的东西：抛了就当失败，别把整份体检带崩。"""
    try:
        fn()
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------- 临时环境

# 一份最小 config.yaml。⚠️ `default_mode` 必须带引号 —— YAML 会把裸写的
# `on` 解析成布尔 True，那是最隐蔽的一个坑（见 CLAUDE.md）。
CONFIG_YAML = """\
ai:
  base_url: ""
  model: "deepseek-flash"
persona:
  default: "全局人设"
  default_relationship: "朋友"
  per_session: {}
  relationships: {}
sessions:
  default_mode: "on"
  disabled: []
ui:
  theme: "dark"
  width: 340
  radius: 10
"""


def make_store(tmp):
    """把三层配置全部指到临时目录。

    ⚠️ `SettingsStore` 的三个路径都能从构造参数传（`settings.py` 里就是这么设计的），
        所以这里**不需要 monkeypatch**，也就不会出现"漏改一处就写到真 state/"。
    """
    cfg_path = os.path.join(tmp, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(CONFIG_YAML)
    return SettingsStore(
        config_path=cfg_path,
        settings_path=os.path.join(tmp, "settings.json"),
        secrets_path=os.path.join(tmp, "secrets.yaml"),
    )


FAKE_SESSIONS = {
    "张三": {"display_name": "张三", "mode": "on", "is_new": False},
    "张三.工作": {"display_name": "张三.工作", "mode": "on", "is_new": False},
    "李四": {"display_name": "李四", "mode": "off", "is_new": True},
    "某群": {"display_name": "某群", "mode": "observe", "is_new": False},
}


def state_stamp():
    """仓库 `state/` 目录的时间戳（用来证明体检没往里写东西）。"""
    d = paths.state_dir()
    try:
        return os.path.getmtime(d), sorted(os.listdir(d))
    except Exception:
        return None, None


def code_only(path):
    """读一个 .py，**去掉注释和字符串**之后返回（用来查"有没有照搬 Tk 代码"）。

    为什么要这么绕：迁移后的文件里**故意**在注释/docstring 里写着
    "Tk 的 overrideredirect + 色键在这里不用了"。直接文本搜含那一串的会
    把说明文字当成遗留代码 —— 那是假阳性，比不查更糟。
    """
    import io
    import tokenize

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except Exception:
        return src
    return "\n".join(out)


# ---------------------------------------------------------------- 体检

def main():
    app = base.init_app([])
    print("Qt %s / 缩放 %.2f / 基准像素 px(10)=%d"
          % (QtCore.qVersion(), base.scale(), base.px(10)))

    before = state_stamp()
    tmp = tempfile.mkdtemp(prefix="wai_qtsettings_")
    try:
        return run(app, tmp, before)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(app, tmp, before):
    store = make_store(tmp)
    cfg = store.load()
    modes = []
    saved = []

    win = SettingsWindow(
        cfg, store, base.Style(cfg, 0, name="dark"),
        logger=lambda m: None,
        on_saved=lambda: saved.append(1),
        session_source=lambda: FAKE_SESSIONS,
        on_mode_change=lambda k, m: modes.append((k, m)),
    )
    win.show_dialogs = False          # 模态框会卡住无人操作的体检

    print("\n== 纯逻辑（与 Tk 版逐字一致，签名/行为都不能变）==")
    # ⚠️ 这一条比"跑几个用例"更硬：**直接比对两个版本的函数源码**。
    #    `tests/test_session_ui.py` 里几十条用例测的就是这三个纯函数，
    #    迁移时只要多一个空格、少一行 docstring 都会被它抓出来。
    import inspect

    from wechat_assistant import session_ui as tk_session_ui
    from wechat_assistant.qt_ui import session_ui as qt_session_ui

    for fname in ("session_rows", "merge_map", "count_blacklisted"):
        a = inspect.getsource(getattr(tk_session_ui, fname)).strip()
        b = inspect.getsource(getattr(qt_session_ui, fname)).strip()
        check("纯函数 %s() 与 Tk 版逐字一致" % fname, a == b,
              "" if a == b else "长度 %d vs %d" % (len(a), len(b)))

    rows = session_rows(FAKE_SESSIONS, {"孤儿": "配置里留着人设"}, {"孤儿": "同事"},
                        ["李四"])
    keys = [r["key"] for r in rows]
    check("session_rows 取并集（配置里的孤儿会话也列出来）",
          "孤儿" in keys, repr(keys))
    check("session_rows 标出黑名单", 
          [r for r in rows if r["key"] == "李四"][0]["blacklisted"] is True)
    check("session_rows 标出 known=False",
          [r for r in rows if r["key"] == "孤儿"][0]["known"] is False)
    check("session_rows 排序稳定",
          keys == [r["key"] for r in session_rows(FAKE_SESSIONS,
                                                  {"孤儿": "x"}, {"孤儿": "y"},
                                                  ["李四"])])
    check("merge_map 空值 = 删键", merge_map({"A": "1"}, "A", "  ") == {})
    check("merge_map 不改原字典",
          (lambda b: (merge_map(b, "B", "2"), b == {"A": "1"})[1])({"A": "1"}))
    check("count_blacklisted 数得对", count_blacklisted(store.get) == 0,
          str(count_blacklisted(store.get)))

    # ------------------------------------------------------------ 窗口本体
    print("\n== 窗口本体 ==")
    ok_open, err_open = safe(lambda: win.open())
    pump(app, 200)
    check("open() 不抛异常", ok_open, err_open)
    check("窗口能建出来", win.win is not None, repr(win.win))
    check(".win 属性保留（panel.py 靠它判死活）", win.is_open() is True)
    check("无边框（FramelessWindowHint）",
          bool(win.win.windowFlags() & QtCore.Qt.FramelessWindowHint))
    check("Panel 的圆角半径 = theme.SHELL_RADIUS",
          int(win.win.radius) == base.px(theme.SHELL_RADIUS),
          "%s vs %s" % (win.win.radius, base.px(theme.SHELL_RADIUS)))
    check("SHELL_RADIUS 常量跟 theme 一致", SHELL_RADIUS == theme.SHELL_RADIUS,
          "%s vs %s" % (SHELL_RADIUS, theme.SHELL_RADIUS))
    check("外框线宽 = theme.shell_border_width",
          int(win.win.border_w) == base.px(
              theme.shell_border_width(win.style.theme_name)),
          "%s" % win.win.border_w)
    check("窗口最小尺寸 = px(MIN_W) x px(MIN_H)",
          (win.win.minimumWidth(), win.win.minimumHeight())
          == (base.px(MIN_W), base.px(MIN_H)),
          "%sx%s vs %sx%s" % (win.win.minimumWidth(), win.win.minimumHeight(),
                              base.px(MIN_W), base.px(MIN_H)))
    check("内容区页头给的是**当前分组名**（不是又写一条「设置」）",
          win.desc_title.text() == "AI 接口", repr(win.desc_title.text()))

    # ------------------------------------------------------------ 分组切换
    print("\n== 分组切换 ==")
    gids = [g[0] for g in GROUPS]
    check("左侧七组按钮都建出来了",
          len(win._nav_btns) == len(gids) and set(win._nav_btns) == set(gids),
          repr(sorted(win._nav_btns)))

    switched = True
    for gid in gids:
        ok_g, err_g = safe(lambda g=gid: win._switch(g))
        pump(app, 60)
        if not ok_g or win._group != gid:
            switched = False
            print("      切到 %r 失败: %s" % (gid, err_g))
    check("每一组都能切过去（含「会话」自定义面板）", switched)
    check("切到最后一组后页头描述跟着变",
          win.desc_title.text() == dict((g[0], g[1]) for g in GROUPS)[win._group],
          repr(win.desc_title.text()))

    # 选中态：选中的那颗胶囊是 primary
    win._switch("ui")
    pump(app, 60)
    check("选中的分组按钮是选中外观（primary）",
          win._nav_btns["ui"].variant == "primary"
          and win._nav_btns["ai"].variant != "primary",
          repr((win._nav_btns["ui"].variant, win._nav_btns["ai"].variant)))

    # ------------------------------------------------------------ SPEC 表单
    print("\n== 表单按 SPEC 生成 ==")
    per_group_ok = True
    details = []
    for gid in gids:
        if gid == "session":
            continue
        win._switch(gid)
        pump(app, 60)
        want = [s for s in SPEC if s.group == gid]
        got = len(win._fields)
        if got != len(want) or got <= 0:
            per_group_ok = False
            details.append("%s: %d != %d" % (gid, got, len(want)))
    check("每个分组的行数与该组的 SPEC 项数一致（且 > 0）", per_group_ok,
          "; ".join(details))
    check("总共生成了 SPEC 的全部项（除「会话」组）",
          len([s for s in SPEC if s.group != "session"]) > 0)

    win._switch("ai")
    pump(app, 60)
    check("API Key 那行额外挂了「测试连接」", win.test_result is not None)
    check("提示文字里没有 markdown（** 会原样显示）",
          not any("**" in (s.label or "") or "**" in (s.help or "")
                  for s in SPEC))

    # 多行输入框是真的多行（Tk 版这里是 5px 高，见 _TextAreaField 的说明）
    win._switch("persona")
    pump(app, 60)
    f = win._fields.get("persona.default")
    check("全局人设是多行框，而且高度 > 40px（不是 Tk 版的 5px）",
          f is not None and f.widget.height() > base.px(40),
          "h=%s" % (f.widget.height() if f else None))

    # ------------------------------------------------------------ 取值 / 保存
    print("\n== 取值与保存 ==")
    win._switch("ui")
    pump(app, 60)
    win._fields["ui.width"].box.set("380")
    win._fields["ui.radius"].box.set("12")
    vals = win._collect()
    check("_collect() 反映改动的值",
          vals.get("ui.width") == "380" and vals.get("ui.radius") == "12",
          repr({k: vals[k] for k in ("ui.width", "ui.radius") if k in vals}))
    check("_collect() 覆盖当前分组的所有项",
          set(vals) == {s.key for s in SPEC if s.group == "ui"}, repr(sorted(vals)))

    ok_save, err_save = safe(win._save)
    pump(app, 120)
    check("_save() 不抛异常", ok_save, err_save)
    check("保存后配置里读得到新值",
          store.load().get("ui.width") == 380
          and store.load().get("ui.radius") == 12,
          repr((store.load().get("ui.width"), store.load().get("ui.radius"))))
    check("落盘在临时目录（不是仓库 state/）",
          os.path.exists(os.path.join(tmp, "settings.json"))
          and paths.settings_path() not in (store.settings_path,),
          store.settings_path)
    check("on_saved 被回调", bool(saved), str(len(saved)))
    check("状态行给出「已保存」", "已保存" in (win.status.text() or ""),
          repr(win.status.text()))
    check("保存后重新渲染（没有残留的待提交项）",
          win._session_stage["personas"] == {}, repr(win._session_stage))

    # 来源标记：改过的项要能看出来源
    win._switch("ui")
    pump(app, 60)
    check("改过的项 source = user", store.source("ui.width") == "user",
          store.source("ui.width"))
    ok_reset, err_reset = safe(lambda: win._reset_one("ui.width"))
    pump(app, 80)
    check("恢复默认不抛异常", ok_reset, err_reset)
    check("恢复默认后读出来是出厂默认 340",
          store.load().get("ui.width") == 340, repr(store.load().get("ui.width")))

    # ------------------------------------------------------------ 会话面板
    print("\n== 会话分组 ==")
    win._switch("session")
    pump(app, 120)
    check("会话面板建出来了", win._session_panel is not None)
    check("默认选中了第一个会话", win._session_panel.selected is not None,
          repr(win._session_panel.selected))
    check("四个会话都建了行", len(win._session_panel._row_widgets) == 4,
          repr(sorted(win._session_panel._row_widgets)))
    check("编辑器三个部件齐全",
          set(win._session_panel._ed or {}) == {"mode", "persona", "rel"},
          repr(sorted(win._session_panel._ed or {})))
    check("提示文字登记进 WrapGroup（同名 API 还在）",
          len(win._session_panel._wrap_labels) > 0,
          str(len(win._session_panel._wrap_labels)))
    check("会话面板拿得到合理宽度（> px(120)）",
          win._session_panel._wrap_len() >= base.px(120),
          str(win._session_panel._wrap_len()))

    # 切会话不丢改动
    p = win._session_panel
    p._select("李四")
    pump(app, 60)
    p._ed["persona"].set("李四专属人设")
    p._ed["rel"].set("同事")
    p._ed["mode"]._pick("on")
    p._select("张三")
    pump(app, 60)
    check("切会话后上一个会话的改动仍在（暂存）",
          p.collect()["personas"].get("李四") == "李四专属人设",
          repr(p.collect()["personas"]))
    check("切过去后编辑框是新的会话（不串）",
          p._ed["persona"].get() == "", repr(p._ed["persona"].get()))
    p._select("李四")
    pump(app, 60)
    check("切回来回填的是暂存值",
          p._ed["persona"].get() == "李四专属人设",
          repr(p._ed["persona"].get()))

    # ⚠️ 切分组「暂存不丢」：这是最容易被重建吃掉的一条
    win._switch("ai")          # 切走（会 stash + 销毁面板）
    pump(app, 80)
    check("切走后暂存还在", win._session_stage["personas"].get("李四") == "李四专属人设",
          repr(win._session_stage["personas"]))
    win._save()                # 在别的分组点保存
    pump(app, 120)
    per = store.get("persona.per_session", {}) or {}
    rel = store.get("persona.relationships", {}) or {}
    check("切分组后再保存，改动不丢", per.get("李四") == "李四专属人设", repr(per))
    check("关系也一起写进去了", rel.get("李四") == "同事", repr(rel))
    check("模式走的是另一个存储（回调被调用）", ("李四", "on") in modes, repr(modes))

    # 会话名带句点（点分路径会切坏 —— CLAUDE.md 记着的坑）
    win._switch("session")
    pump(app, 120)
    win._session_panel._select("张三.工作")
    pump(app, 60)
    win._session_panel._ed["persona"].set("工作人格")
    win._save()
    pump(app, 120)
    per = store.get("persona.per_session", {}) or {}
    check("会话名含句点也能正确落盘", per.get("张三.工作") == "工作人格", repr(per))
    check("含句点的名字没有被切成嵌套",
          isinstance(per.get("张三"), str) or "张三" not in per, repr(per))

    # 留空 = 删掉那个键
    win._switch("session")
    pump(app, 120)
    win._session_panel._select("李四")
    pump(app, 60)
    win._session_panel._ed["persona"].set("")
    win._session_panel._ed["rel"].set("")
    win._save()
    pump(app, 120)
    per = store.get("persona.per_session", {}) or {}
    check("留空 = 删掉那个键（恢复用全局）", "李四" not in per, repr(per))
    check("别的会话不受影响", per.get("张三.工作") == "工作人格", repr(per))

    # 黑名单会话要打警告
    store.set("sessions.disabled", ["张三"])
    win._switch("session")
    pump(app, 120)
    win._session_panel._select("张三")
    pump(app, 60)
    texts = [l.text() for l in win._session_panel._wrap_labels]
    check("黑名单会话显示红字警告", any("黑名单" in t for t in texts),
          repr([t[:16] for t in texts]))
    store.set("sessions.disabled", [])

    # 恢复全部默认（show_dialogs=False 时不弹框，否则会卡住无人操作的体检）
    ok_ra, err_ra = safe(win._reset_all)
    pump(app, 150)
    check("恢复全部默认不抛异常（不弹模态框）", ok_ra, err_ra)
    check("恢复后覆盖层被清空", store.overrides() == {}, repr(store.overrides()))
    check("恢复全部默认会清掉会话级人设",
          not (store.get("persona.per_session", {}) or {}),
          repr(store.get("persona.per_session", {})))
    check("恢复后暂存也清了（不会把刚清掉的值又填回去）",
          win._session_stage["personas"] == {}, repr(win._session_stage))

    # ------------------------------------------------------------ 测试连接
    print("\n== 测试连接（空地址，不发网络请求）==")
    win._switch("ai")
    pump(app, 60)
    win._fields["ai.base_url"].box.set("")
    win._fields["ai.api_key"].field.set("")
    ok_t, err_t = safe(win._test)
    check("_test() 能起线程不抛", ok_t, err_t)
    pump(app, 600)          # 等子线程的结果经 Signal 回到 UI 线程
    check("子线程的结果回到了界面上（空地址 -> 明确报错，不是静默）",
          bool(win.test_result.text()) and "空" in win.test_result.text(),
          repr(win.test_result.text()))

    # ------------------------------------------------------------ set_style
    print("\n== 切样式（panel.py 保存后会调）==")
    win._switch("ui")
    pump(app, 60)
    lite = base.Style(cfg, 0, name="light")
    ok_st, err_st = safe(lambda: win.set_style(lite))
    pump(app, 120)
    check("set_style() 不抛异常", ok_st, err_st)
    check("换样式后窗口还在、样式真的换了",
          win.win is not None and win.style.theme_name == "light",
          repr(win.style.theme_name))
    check("换样式后分组保持不变", win._group == "ui", repr(win._group))
    check("换样式后表单重建了（行数仍然对）",
          len(win._fields) == len([s for s in SPEC if s.group == "ui"]),
          str(len(win._fields)))
    check("浅色下圆角半径仍然是 theme.SHELL_RADIUS",
          int(win.win.radius) == base.px(theme.SHELL_RADIUS),
          str(win.win.radius))

    # ------------------------------------------------------------ 关窗
    print("\n== 关窗 ==")
    ok_c, err_c = safe(win.close)
    pump(app, 120)
    check("close() 不抛异常", ok_c, err_c)
    check("关窗后 .win 回到 None（Tk 版的判活方式）", win.win is None)
    check("关窗后 is_open() 是 False", win.is_open() is False)
    ok_re, err_re = safe(lambda: win.open("session"))
    pump(app, 200)
    check("关掉之后还能重新打开", ok_re and win.win is not None, err_re)
    check("重开时指定分组生效", win._group == "session", repr(win._group))
    check("重开后会话面板又建出来了", win._session_panel is not None)
    safe(win.close)

    # ------------------------------------------------------------ 几何
    #
    # ⚠️ 全是 Qt 独有的坑，Tk 版一个都碰不到：
    #    · 重建界面时旧内容走 deleteLater()，**删除被推迟**，
    #      而 Qt 会把还没销毁的旧布局算进窗口的最小尺寸 ——
    #      `panel.py` 就是在折叠态上被这个顶住过（折叠成 56x56 却卡在 168x270）。
    #    · 内容最小宽度大于窗口宽度时，Qt 不报错，只是把右边**裁掉**。
    print("\n== 几何（重建后窗口还能不能缩回去）==")
    win2 = SettingsWindow(cfg, make_store(tmp), base.Style(cfg, 0, name="dark"),
                          logger=lambda m: None,
                          session_source=lambda: FAKE_SESSIONS)
    win2.show_dialogs = False
    win2.open()
    pump(app, 200)
    lmins = []
    for _ in range(3):
        win2._switch("session")
        pump(app, 80)
        win2._switch("ai")
        pump(app, 80)
        lmins.append(win2.win.layout().minimumSize().width())
    check("反复切分组后布局最小宽度不膨胀（deleteLater 的旧控件被算进去的坑）",
          max(lmins) <= base.px(MIN_W), "最小值 %s / 窗口最小 %d"
          % (lmins, base.px(MIN_W)))

    win2._switch("ui")
    pump(app, 80)
    win2.win.resize(base.px(MIN_W), base.px(MIN_H))
    pump(app, 120)
    check("能缩到最小尺寸 px(MIN_W) x px(MIN_H)",
          (win2.win.width(), win2.win.height())
          == (base.px(MIN_W), base.px(MIN_H)),
          "%dx%d" % (win2.win.width(), win2.win.height()))
    check("最小尺寸下右侧表单区没被挤没",
          win2.scroll.viewport().width() > base.px(200),
          str(win2.scroll.viewport().width()))
    check("左侧分组栏宽度 = px(NAV_W)",
          win2._nav_btns["ai"].parentWidget().width() == base.px(150),
          str(win2._nav_btns["ai"].parentWidget().width()))

    win2._switch("session")
    pump(app, 150)
    panel2 = win2._session_panel
    check("最小尺寸下会话面板还在、列表没被压没",
          panel2 is not None and panel2.list_area.width() == base.px(176),
          str(panel2 and panel2.list_area.width()))
    check("最小尺寸下编辑区仍有可用宽度（> px(120)）",
          panel2.edit_area.viewport().width() > base.px(120),
          str(panel2.edit_area.viewport().width()))
    safe(win2.close)

    # ------------------------------------------------------------ 没有照搬 Tk 补丁
    print("\n== 没有照搬 Tk 专属补丁 ==")
    # ⚠️ 只查**代码**，不查注释和 docstring：那两份文件的注释里**故意**写着
    #    "Tk 的 overrideredirect / 色键 #FF00FE 在这里不用了"，
    #    拿纯文本搜会把说明文字当成遗留代码（第一次跑就是这么误报的）。
    for fname in ("settings_ui.py", "session_ui.py"):
        code = code_only(os.path.join(ROOT, "wechat_assistant", "qt_ui", fname))
        bad = [m for m in ("winchrome", "overrideredirect", "#FF00FE",
                           "transparentcolor", "messagebox", "tkinter",
                           "winfo_", "Toplevel") if m in code]
        check("%s 的**代码**里没有 Tk/DWM 的痕迹" % fname, not bad, repr(bad))
    check("这条路径上没 import winchrome",
          "wechat_assistant.winchrome" not in sys.modules)

    # ------------------------------------------------------------ 端到端接线
    #
    # ⚠️ 这一段才是真正验"对外 API 逐字一致"的：不自己按签名 new 一个，
    #    而是**走 panel.py 的 open_settings()** —— 那边是按
    #    `SettingsWindow(cfg, store, style, logger=..., on_saved=...,
    #     session_source=..., on_mode_change=..., preselect=...)` 调的，
    #    参数错一个、少一个，这里就建不出窗口（而且它自己会 try/except 吞掉，
    #    所以只能靠 `_settings_win is not None` 来判）。
    print("\n== 端到端：panel.open_settings() 真的调得通 ==")
    from wechat_assistant.qt_ui.panel import FloatingAssistant

    ui = FloatingAssistant(cfg, logger=lambda m: None,
                           session_source=lambda: FAKE_SESSIONS,
                           on_mode_change=lambda k, m: None)
    pump(app, 150)
    ui._session_title = "张三"           # 假装当前打开的是「张三」
    ui.open_settings(group="session")
    pump(app, 250)
    sw = ui._settings_win
    check("panel.open_settings() 建出了设置窗口", sw is not None)
    if sw is not None:
        check("会话表回调透传进去了", sw._session_source is ui._session_source)
        check("模式回调透传进去了", sw._on_mode_change is ui._on_mode_change)
        check("当前会话被当成预选", sw._preselect == "张三", repr(sw._preselect))
        check("打开时直接落在「会话」分组", sw._group == "session", repr(sw._group))
        check("面板真的停在预选的那个会话上",
              sw._session_panel is not None
              and sw._session_panel.selected == "张三",
              repr(sw._session_panel and sw._session_panel.selected))
        ok_ss, err_ss = safe(lambda: sw.set_style(ui.style))
        pump(app, 150)
        check("panel 侧调 set_style() 不抛异常", ok_ss, err_ss)
        check("set_style 之后窗口还在", sw.win is not None)
        # 再点一次「设置」应该复用同一个窗口（不是又开一扇）
        same = ui._settings_win is sw
        ui.open_settings(group="ai")
        pump(app, 150)
        check("重复打开复用同一个窗口实例", same and ui._settings_win is sw)
        check("复用时也能切到指定分组", sw._group == "ai", repr(sw._group))
        safe(sw.close)
    safe(ui.close)
    pump(app, 100)
    check("悬浮窗关闭不报错", True)

    # ------------------------------------------------------------ 隔离
    print("\n== 没有污染仓库 ==")
    after = state_stamp()
    check("仓库 state/ 目录没被动过（mtime + 文件列表都不变）",
          before == after, "%r -> %r" % (before, after))
    check("体检用的 store 路径确实在临时目录里",
          os.path.abspath(store.settings_path).startswith(os.path.abspath(tmp)),
          store.settings_path)

    # ------------------------------------------------------------ 汇总
    print("\n" + "=" * 66)
    if FAIL:
        print("失败项原文：")
        for n in FAILED_NAMES:
            print("  · %s" % n)
    print("通过 %d / 失败 %d" % (PASS, len(FAIL)))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
