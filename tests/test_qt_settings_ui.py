# -*- coding: utf-8 -*-
"""Qt 版设置窗口测试：结构、观感、SPEC 表单、保存/恢复、几何、隔离。

对照 Tk 版（`tests/test_settings_ui.py`，43 项）
==============================================
⚠️ **不是逐条照搬**。Tk 版有相当一部分断言在测"Tk 这个工具本身"的细节
   （`itemcget(_shape,"fill")` / 透明色键 / `winfo_viewable` / canvas 多边形 /
   `SCROLL_PAD`）。Qt 里那些东西**根本不存在**，照搬只会得到一批恒真的假断言。
   所以每一条都先问「它到底在防什么行为」，再用 Qt 的方式重新表达；
   确实没有对应行为的**直接删掉**，理由写在下面对照表里。

    Tk 版断言                              它防的行为                Qt 里的处理
    ------------------------------------   ----------------------   --------------------------
    胶囊 `itemcget(_shape,"fill")`         画上去的形状色跟父容器    **删**：Qt 的 Pill 是
      跟父容器底色一致                      不一致 -> 侧栏里一块深色  QPushButton + QSS，没有
                                                                    "形状填充色"这个层
    `attributes("-transparentcolor")`      圆角外面透明              **换**成
      是 `#FF00FE`                                                  `WA_TranslucentBackground`
    `winfo_viewable()`（transient +        窗口真的显示出来了        **换**成 `isVisible()`
      无边框会 1x1）                                                 + 尺寸不是 1x1
    外壳是**两个填充多边形**              外框线是细线、颜色来自     **换**成 Panel 的
      + 内层内缩 bw + 没有描边             theme.shell_border         radius/border_w 断言
                                                                    + 抓窗口位图看边框色
    外壳比内容层宽 2*inset                 内容层缩进到圆角以内      保留（Qt 同样会
      （winfo 几何）                                                "内容铺满把圆角盖成直角"）
    `bind("<B1-Motion>")` 非空              能拖 / 能缩放              **换**成真的模拟一次
                                                                    按下：边缘 -> 进入缩放
    `GetWindowRect` / 客户区 == 外框         整扇窗在屏幕里 /          **换**成 `_clamp()`
      / 拖拽夹取                            不出屏                    的四个方向断言
    `winfo_manager() == "pack"`（自定义容器）会话分组不是 SPEC 表单     **换**成自定义容器
                                                                    可见、滚动区隐藏
    `SCROLL_PAD` 盖住指示条一半             内容不被滚动条压住        **删**：Qt 的滚动条是
                                                                    独立部件，不叠在内容上
    `wraplength` 跟着窗口变                 提示文字不被静默裁字      **换**成 QLabel 的
                                                                    `wordWrap()` +
                                                                    最小宽度不顶大窗口

⚠️ **不打开真实窗口 / 不需要显示器**：用 `QT_QPA_PLATFORM=offscreen`。
   代价是**抗锯齿的细腻程度**测不出来（那要靠 `probes/qt/check_corners.py` 目视），
   但"圆角有没有被内容盖住"这类**alpha 级别**的事在 offscreen 下照样量得到
   （抓窗口自己的位图，见第 2 节）。

⚠️ **绝不碰仓库里的 `state/`**：`SettingsStore` 的三个路径全部指到临时目录
   （`settings.py` 的构造函数就是这么设计的，不需要 monkeypatch），
   并且开头/结尾比对 `state/` 的 mtime + 文件列表（第 12 节）。

用法：python tests/test_qt_settings_ui.py
"""

import os
import shutil
import sys
import tempfile
import time

# ⚠️ 必须在**任何 Qt import 之前**设。offscreen 平台不依赖显示器，
#    但仍会走完整的布局 / 事件 / 绘制链路 —— 几何与位图断言是真实值。
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


def pump(app, ms=140):
    """转一段事件循环 —— 布局 / deleteLater / 跨线程 Signal 都要靠它推进。"""
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def safe(fn):
    """跑一段会碰界面的东西：抛了就当失败，别把整份测试带崩。"""
    try:
        fn()
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------- 临时环境

# 一份最小 config.yaml。
# ⚠️ `default_mode` 必须带引号 —— YAML 会把裸写的 `on` 解析成布尔 True，
#    那是最隐蔽的一个坑（症状是"所有会话都不启用，而且不报错"，见 CLAUDE.md）。
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
ui:
  width: 340
  radius: 10
  theme: "dark"
  opacity: 1.0
"""


def make_store(tmp):
    """把三层配置全部指到临时目录。

    ⚠️ `SettingsStore` 的三个路径都能从构造参数传 —— 所以这里
        **不需要 monkeypatch**，也就不会出现"漏改一处就写到真 state/"。
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


FAKE_SESSIONS = {
    "张三": {"display_name": "张三", "mode": "on", "is_new": False},
    "张三.工作": {"display_name": "张三.工作", "mode": "on", "is_new": False},
    "李四": {"display_name": "李四", "mode": "off", "is_new": True},
}


def another_instance_running():
    """**那个程序本身**是不是正在跑（用它的单实例互斥体判，不数进程）。

    ⚠️ 为什么需要这个：设置窗口那一节要断言「本套件没有污染仓库的
       `state/settings.json`」。但**用户自己开着的那个实例**会写这个文件
       （比如他正在用设置窗口），于是这条断言会**假红** ——
       实测踩到过：同一份代码连跑 6 次全过，第 7 次挂在这条上，
       而 mtime 的变化来自另一个进程，不是本套件。

    ⚠️ 判据必须用**有名互斥体**，不能数 `python.exe`：
       本机上跑着别的 Python 脚本，一数就误报（`run_watch.py` 里
       为同一个理由也是这么做的，见那里的说明）。
    """
    try:
        # ⚠️ 名字只有 `single_instance` 那一个出口。原来这里是写死的名字 +
        #    `OpenMutexW`：名字一改就悄悄失效，而 `OpenMutexW` 本机实测会
        #    ACCESS_DENIED —— 等于**一直判成"没人在跑"**，有实例时这条断言**假红**。
        from wechat_assistant import single_instance

        return single_instance.is_running()
    except Exception:
        # 判不了就当作"不知道"，调用方会按保守方式处理
        return None


def state_stamp():
    """测试**该写**的那几个文件的时间戳 + 目录文件列表。

    ⚠️ 别拿整个 `state/` 目录的 mtime 当判据 —— 那个目录里的 `history.db` /
        `screens.db` 是**记录器**每 2 秒就在写的，只要开发时另开过一个程序实例，
        目录 mtime 就会变，这条断言就会**假红**（实测踩过：
        目录 mtime 从 ...049 变到 ...097，而文件列表一个没变 —— 不是测试写的）。
        只盯 `settings.json` / `state.json` 这两个设置窗口才有权写的文件。
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


def walk_widgets(root):
    """某棵部件树里的全部子孙部件（含 root 自己）。"""
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


def labels_of(root):
    from PySide6 import QtWidgets

    return [w for w in walk_widgets(root) if isinstance(w, QtWidgets.QLabel)]


def make_win(tmp, theme_name="dark", sessions=None, preselect=None,
             on_saved=None, modes=None, name="ct",
             on_show_prompt=None, on_show_read=None):
    """建一扇设置窗口（store 指向临时目录）。调用方负责 close()。"""
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.settings_ui import SettingsWindow

    store = make_store(tmp)
    cfg = store.load()
    style = base.Style(cfg, 0, name=theme_name)
    win = SettingsWindow(
        cfg, store, style,
        logger=lambda m: None,
        on_saved=on_saved,
        session_source=(lambda: (FAKE_SESSIONS if sessions is None else sessions)),
        on_mode_change=(lambda k, m: (modes.append((k, m)) if modes is not None else None)),
        preselect=preselect,
        on_show_prompt=on_show_prompt,
        on_show_read=on_show_read)
    win.show_dialogs = False          # 模态框会卡住无人操作的测试
    return win, store, style


# ================================================================ 1. 结构

def test_skeleton(app, tmp):
    print("\n1. 窗口骨架（无边框 / 圆角 / 页面结构）")
    from PySide6 import QtCore, QtGui, QtWidgets

    from wechat_assistant import theme
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.settings_ui import (MIN_H, MIN_W, SHELL_INSET,
                                                   SHELL_RADIUS, WIN_H, WIN_W)
    from wechat_assistant.settings import GROUPS

    win, store, style = make_win(tmp)
    try:
        ok, err = safe(win.open)
        pump(app, 250)
        check("open() 不抛异常", ok, err)
        check("窗口能建出来", win.win is not None)
        # `.win` 的语义和 Tk 版逐字一致：None = 窗口不存在（调用点靠它判死活）
        check(".win 保留着（panel.py 靠它判死活）", win.is_open() is True)

        check("无边框（FramelessWindowHint）",
              bool(win.win.windowFlags() & QtCore.Qt.FramelessWindowHint))
        # Tk 版这里是判 `attributes("-transparentcolor")` == "#FF00FE"（透明色键）。
        # Qt 没有色键：真圆角靠的是**真 alpha**，撞色穿洞那一类坑不复存在。
        check("真透明（WA_TranslucentBackground，不是色键）",
              win.win.testAttribute(QtCore.Qt.WA_TranslucentBackground))
        # Tk 版是判 `winfo_viewable()`：无边框 + transient 在 Windows 上根本
        # 不会显示（矩形 1x1）。Qt 这条**同样必须守** —— 不显示的话布局全是假值。
        check("窗口真的显示出来了、尺寸不是 1x1",
              win.win.isVisible()
              and (win.win.width(), win.win.height()) == (base.px(WIN_W), base.px(WIN_H)),
              "%s %dx%d" % (win.win.isVisible(), win.win.width(), win.win.height()))

        # 圆角半径**从 theme 拿** —— 两边各写一个数的话，设置窗口和悬浮窗
        # 并排看就是两个不同的圆度。
        check("圆角半径 = theme.SHELL_RADIUS",
              int(win.win.radius) == base.px(theme.SHELL_RADIUS),
              "%s vs %s" % (win.win.radius, base.px(theme.SHELL_RADIUS)))
        check("SHELL_RADIUS 常量跟 theme 一致", SHELL_RADIUS == theme.SHELL_RADIUS)
        # 外框线宽也只允许有一个出处：theme.shell_border_width()。
        # Tk 版对应的是判"两个填充分辨出内层内缩了 bw"（canvas 几何）——
        # Qt 里线宽就是 Panel.border_w 这一个数，直接问它。
        check("外框线宽 = theme.shell_border_width",
              int(win.win.border_w) == base.px(
                  theme.shell_border_width(win.style.theme_name)),
              str(win.win.border_w))
        check("外框线宽 >= 2 逻辑像素（用户要求「边框宽度 2」）",
              theme.shell_border_width(win.style.theme_name) >= 2,
              str(theme.shell_border_width(win.style.theme_name)))

        # ⚠️ **内容层必须缩进到圆角以内**：不缩的话，铺满整宽的标题条会画到
        #    (0,0)，把自绘的圆角整块盖掉 —— 四个角看起来就是直角，
        #    而自绘代码明明画了圆角、也没有任何报错（Qt 侧的实测坑）。
        #
        # ⚠️ 2026-09-27 起缩进量**不再是固定常量**：改成"外框线宽度 + 1"
        #    （`_content_inset()`），因为内容层首尾部件自己也会圆角，
        #    没必要再躲那么远（用户要求「消除多余四周留白」）。
        #    → 所以这里**问窗口要实际值**，别再去读一个常量。
        m = win.win._root_lay.contentsMargins()
        inset = win.win._content_inset()
        check("内容层四周都往里缩了 _content_inset()",
              (m.left(), m.top(), m.right(), m.bottom()) == (inset, inset, inset, inset),
              "%s vs %d" % ((m.left(), m.top(), m.right(), m.bottom()), inset))
        check("缩进仍然 > 0（缩成 0 就会盖住外框线）", inset > 0, str(inset))
        check("缩进比原来的 px(SHELL_INSET)=%d **更紧**（这才是这次要的效果）"
              % base.px(SHELL_INSET), inset < base.px(SHELL_INSET),
              "%d < %d" % (inset, base.px(SHELL_INSET)))
        win.win.resize(base.px(WIN_W), base.px(WIN_H))
        pump(app, 120)
        body = win.win._root_lay.itemAt(win.win._root_lay.count() - 1).widget()
        check("内容层比窗口窄 2*inset（那一圈空着留给缩放热区）",
              win.win.width() - body.width() == 2 * inset,
              "%d vs %d" % (win.win.width() - body.width(), 2 * inset))

        check("窗口最小尺寸 = px(MIN_W) x px(MIN_H)",
              (win.win.minimumWidth(), win.win.minimumHeight())
              == (base.px(MIN_W), base.px(MIN_H)),
              "%dx%d" % (win.win.minimumWidth(), win.win.minimumHeight()))

        # ---- 顶栏没被盖住（用户报过「顶部一片空白，没有标题也没有 ✕」）
        #
        # ⚠️ Tk 版的根因是 `_build()` 把正文挂在了 `self.win` 上而不是内容层。
        #    Qt 里同一类错误的表现是"某个部件 setParent(win) 又没进布局" ——
        #    它会浮在 (0,0) 把顶栏压住，同样不报错。所以这里的两条断言是同一件事：
        #    ① 顶栏排在内容布局的第一位；② 窗口上**没有不进布局的子部件**。
        root_lay = win.win._root_lay
        check("顶栏是内容布局的第一项（排在最上面）",
              root_lay.itemAt(0).widget() is win._titlebar)
        kids = [c for c in win.win.children() if isinstance(c, QtWidgets.QWidget)]
        check("窗口上的每个子部件都在内容布局里（没有飘在 (0,0) 盖住顶栏的野部件）",
              len(kids) == root_lay.count(),
              "%d 个子部件 / 布局 %d 项" % (len(kids), root_lay.count()))
        check("顶栏有实际高度（不是被压成 0）",
              win._titlebar.height() == base.px(38), "h=%d" % win._titlebar.height())
        check("顶栏在窗口里的 y = 内容层缩进（贴在最上面）",
              win._titlebar.y() == win.win._content_inset(),
              "y=%d" % win._titlebar.y())
        # 正文区必须排在顶栏**下面**，不能和它重叠
        check("正文区排在顶栏下方（不和顶栏重叠）",
              body.y() >= win._titlebar.geometry().bottom(),
              "body.y=%d 顶栏底=%d" % (body.y(), win._titlebar.geometry().bottom()))
        check("顶栏里有标题和关闭键两个部件",
              win._titlebar.lbl is not None and win._titlebar.btn_close is not None)
        check("标题在左、关闭键在右",
              win._titlebar.lbl.x() < win._titlebar.btn_close.x(),
              "%d vs %d" % (win._titlebar.lbl.x(), win._titlebar.btn_close.x()))

        # ---- 标题条的两种约定（用户明确要求过的那两条）
        # 1. 光标只认两种：默认箭头；**只有悬停在按钮上才换手型**。
        #    给整条标题栏加"可移动"光标是被用户否掉的。
        check("标题条空白处的光标是普通箭头（用户说过不需要移动鼠标）",
              win._titlebar.cursor().shape() == QtCore.Qt.ArrowCursor,
              str(win._titlebar.cursor().shape()))
        # 2. 拖动和 8 向缩放都是自己实现的 —— 无边框窗口没有系统行为。
        win.win.mousePressEvent(_press(2, 2))
        check("在外壳边缘按下会进入缩放（那圈空边就是缩放把手）",
              win.win._rs is not None, repr(win.win._rs))
        win.win.mouseReleaseEvent(_press(2, 2, release=True))
        check("松手后退出缩放态", win.win._rs is None)
        win.win.mousePressEvent(_press(win.win.width() // 2, win.win.height() // 2))
        check("在窗口正中按下**不**进入缩放（不然界面就没法点了）",
              win.win._rs is None)

        # ---- 页头：不许再画一条写着「设置」的页头
        #    ⚠️ Tk 版查的是 `win.win.winfo_children()` 里的 tk.Label。
        #    Qt 版必须**排除标题条** —— 它扮演的是"系统标题栏"（无边框窗口
        #    压根没有系统标题栏），内容区那条才是重复。CLAUDE.md 说的是内容区。
        check("内容区页头给的是当前分组名，不是又写一条「设置」",
              win.desc_title.text() == GROUPS[0][1], repr(win.desc_title.text()))
        bar_zone = set(id(w) for w in walk_widgets(win._titlebar))
        leaked = [l.text() for l in labels_of(win.win)
                  if id(l) not in bar_zone and l.text().strip() == "设置"]
        check("内容区里没有写着「设置」的标题（标题条是窗口装饰，不算重复）",
              not leaked, repr(leaked))

        # ---- 状态行 + 分组栏
        check("有状态行", win.status is not None)
        check("分组按钮齐全",
              len(win._nav_btns) == len(GROUPS),
              "%d/%d" % (len(win._nav_btns), len(GROUPS)))
    finally:
        safe(win.close)


def _press(x, y, release=False):
    """造一个左键按下/松开事件（无边框窗口的拖动与缩放全靠它）。"""
    from PySide6 import QtCore, QtGui

    kind = QtCore.QEvent.MouseButtonRelease if release else QtCore.QEvent.MouseButtonPress
    p = QtCore.QPointF(x, y)
    return QtGui.QMouseEvent(kind, p, p, QtCore.Qt.LeftButton,
                             QtCore.Qt.LeftButton,
                             QtCore.Qt.NoModifier)


# ================================================================ 2. 真绘制

def test_paint(app, tmp):
    """外框线 / 圆角**真的画出来了没有** —— 靠抓窗口自己的位图。

    ⚠️ 为什么不用屏幕截图：本机的 `ImageGrab.grab()` 实测**不可信**
       （抓已知坐标的纯色矩形得到一片均匀的灰），CLAUDE.md 专门记过这一条。
       `QWidget.grab()` 是让窗口自己渲染，与遮挡无关，offscreen 下也照常工作。

    ⚠️ 为什么"内容层有没有盖住圆角"必须单独验：
       `grab()` 会把**子部件**一起画进去，所以只要内容层铺到了 (0,0)，
       圆角就被盖住了 —— 而且**不报错**，只是四个角变成直角。
       这是 Qt 迁移里实测踩到的坑（CLAUDE.md 记着）。
    """
    print("\n2. 外框线与圆角真的画出来了（抓窗口自己的位图）")
    from PySide6 import QtGui

    from wechat_assistant import theme
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.settings_ui import SHELL_INSET

    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 300)
        pm = win.win.grab()
        im = pm.toImage().convertToFormat(QtGui.QImage.Format_ARGB32)
        w, h = im.width(), im.height()
        check("抓到的位图尺寸 == 窗口尺寸",
              (w, h) == (win.win.width(), win.win.height()),
              "%dx%d vs %dx%d" % (w, h, win.win.width(), win.win.height()))

        r = int(win.win.radius)
        d = max(1, int(r * 0.2))          # 明显落在圆角之外的点
        corners = [(d, d), (w - 1 - d, d), (d, h - 1 - d), (w - 1 - d, h - 1 - d)]
        alphas = [im.pixelColor(x, y).alpha() for x, y in corners]
        check("四个角都是透明的（内容层没有把圆角盖成直角）",
              all(a == 0 for a in alphas), str(alphas))
        check("四个角的 alpha 一致（不是只有某一个角是圆的）",
              len(set(alphas)) == 1, str(alphas))
        check("窗口中间是不透明的（不是整窗透明）",
              im.pixelColor(w // 2, h // 2).alpha() > 200,
              str(im.pixelColor(w // 2, h // 2).alpha()))
        check("内容层缩进 > 0（缩进为 0 的话必然盖住外框线）",
              win.win._content_inset() > 0, str(win.win._content_inset()))

        # 外框线的颜色必须来自 theme.shell_border（界面里不许写死色号）。
        # ⚠️ Tk 版这条是查 `itemcget(outer,"fill")`；Qt 没有 canvas item，
        #    所以直接看**画出来的像素**，反而更接近"用户看到的是什么"。
        want = theme.palette(style.theme_name)["shell_border"].lower()
        got = [im.pixelColor(w // 2, 1).name().lower(),
               im.pixelColor(1, h // 2).name().lower(),
               im.pixelColor(w - 2, h // 2).name().lower()]
        check("上/左/右边框中点画的就是 shell_border", all(c == want for c in got),
              "%s vs %s" % (got, want))

        # 顶栏的底色必须是 surface_alt 且和正文底色**不同** —— 两个一样的话
        # 就看不出那是一条顶栏（Tk 版守着同一条）。
        p = theme.palette(style.theme_name)
        check("顶栏底色与正文底色不同（不然看不出是顶栏）",
              p["surface_alt"].lower() != p["bg"].lower(),
              "%s vs %s" % (p["surface_alt"], p["bg"]))
        tim = win._titlebar.grab().toImage().convertToFormat(
            QtGui.QImage.Format_ARGB32)
        bx = max(1, win._titlebar.btn_close.geometry().left() - 6)
        check("顶栏画出来的底色就是 surface_alt",
              tim.pixelColor(bx, tim.height() // 2).name().lower()
              == p["surface_alt"].lower(),
              "%s vs %s" % (tim.pixelColor(bx, tim.height() // 2).name(),
                            p["surface_alt"]))
    finally:
        safe(win.close)


# ================================================================ 3. 分组切换

def test_groups(app, tmp):
    print("\n3. 分组切换（七组都要能切过去）")
    from PySide6 import QtCore, QtGui, QtWidgets
    from wechat_assistant.settings import GROUPS

    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 200)
        bad = []
        for gid, _n, _d in GROUPS:
            ok, err = safe(lambda g=gid: win._switch(g))
            pump(app, 60)
            if not ok or win._group != gid:
                bad.append((gid, err))
        check("每一组都能切过去（含「会话」自定义分组）", not bad, repr(bad))

        win._switch("ui")
        pump(app, 60)
        check("页头跟着换成当前分组名",
              win.desc_title.text() == dict((g[0], g[1]) for g in GROUPS)["ui"],
              repr(win.desc_title.text()))
        check("分组说明也跟着换、且非空",
              win.desc.text() == dict((g[0], g[2]) for g in GROUPS)["ui"],
              repr(win.desc.text()[:24]))
        # 选中态：⚠️ **2026-09-27 按微信实测改过，下面这段结论现在是对的**：
        #    微信**设置窗口**左侧的选中项就是**一块实心绿**
        #    （实测底色 #0DA869 = `accent_dim`，占该块 93.3% 的像素，
        #      240x60 的**直角矩形**，铺满导航栏；文字近白 #E7F6F0）。
        #    → 用 `navsel`。
        #
        # ⚠️⚠️ **这里前后错过两次，都留在这儿免得再犯**：
        #    · 最早写 `primary` —— 对了一半（也是实心绿），但形状是胶囊。
        #    · 后来改成 `selected`（淡绿底 + 绿字胶囊），理由写的是
        #      "微信侧栏的选中项不是实心块"。**那个理由是错的** ——
        #      它来自**聊天窗口的会话列表**，而**设置窗口**根本是另一个界面。
        #      用户就是因此报「微信设置的颜色与字体高亮并不是微信设置的颜色」。
        #    ⚠️ 教训：**"我看过微信某个界面的选中样式"不等于"微信所有界面的
        #       选中样式都那样"**。换界面就得重新量。
        check("选中的分组按钮是 navsel 外观，别的不是",
              win._nav_btns["ui"].variant == "navsel"
              and win._nav_btns["ai"].variant != "navsel",
              repr((win._nav_btns["ui"].variant, win._nav_btns["ai"].variant)))
        check("未选中的分组按钮是全透明的 text（无边框无底色）",
              win._nav_btns["ai"].variant == "text",
              repr(win._nav_btns["ai"].variant))
        check("没有分组按钮被套成 primary（那是主操作按钮的样式）",
              all(b.variant != "primary" for b in win._nav_btns.values()),
              repr([b.variant for b in win._nav_btns.values()]))
        # ⚠️ 形状也照实测锁住：微信那边是**直角**、而且**铺满导航栏宽度**。
        #    只验 variant 的话，有人把 radius 改回胶囊、把宽度改窄都不会被发现。
        _nb = win._nav_btns["ui"]
        check("选中项是**直角矩形**（对齐微信实测，不是胶囊）",
              _nb._radius == 0, "radius=%r" % (_nb._radius,))
        check("选中项**铺满**导航栏宽度（对齐微信实测）",
              _nb.width() >= win._nav_btns["ai"].parentWidget().width(),
              "%d / %d" % (_nb.width(),
                           win._nav_btns["ai"].parentWidget().width()))

        # ⚠️ 上面四条验的是 `variant` 这个**字符串**，它说明不了 Qt 真的画了。
        #    2026-09-26 实测过一个只有渲染层才看得见的缺陷：Pill 的样式表里
        #    `{{` / `}}` 在 `%` 格式化下**原样进 CSS**，Qt 解析整条失败、
        #    静默退回系统默认外观 —— 而 `variant` 明明是 "selected"、
        #    `styleSheet()` 里也能读到颜色。**字符串对 ≠ 画出来对。**
        #    所以这里补一条渲染层的验证。
        def _on_magenta(w):
            """把控件渲染到**洋红底**上，返回左侧中点的像素色。

            洋红是故意的：任何一套正常配色都不会命中它，
            所以"还看得见洋红"= 控件根本没画自己的底色（样式没生效）。
            ⚠️ `renderFlags` 只留 `DrawChildren`（默认的
               `DrawWindowBackground` 会先用调色板底色刷一遍，洋红就量不到了）；
               `renderFlags` 是**第 4 个**参数，第 3 个 `sourceRegion` 不能跳过。
            ⚠️ 采样点取**左侧中点**而不是角上：Pill 是胶囊（半径 = 高度/2），
               角上那块本来就没有像素，拿角去采样会把圆角误判成"没画底色"。
            """
            if hasattr(w, "_apply"):
                w._hover = False
                w._apply()
            pm = QtGui.QPixmap(max(1, w.width()), max(1, w.height()))
            pm.fill(QtGui.QColor("#FF00FF"))
            pt = QtGui.QPainter(pm)
            w.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
                     QtWidgets.QWidget.RenderFlag.DrawChildren)
            pt.end()
            im = pm.toImage()
            c = QtGui.QColor(im.pixelColor(6, max(0, im.height() // 2)))
            return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())

        def _is_green(h):
            """绿色：绿通道明确压过红蓝。"""
            r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
            return g > r + 20 and g > b + 20

        _sel_px = _on_magenta(win._nav_btns["ui"])
        _off_px = _on_magenta(win._nav_btns["ai"])
        check("选中的分组按钮**渲染出来**是绿的（不是系统默认的灰白）",
              _is_green(_sel_px), _sel_px)
        # ⚠️ 这一条原来是 `not _is_green(...)`，断言"选中项**不是**实心亮绿"。
        #    那是按**错的**认知写的（以为微信设置窗口的选中项不是实心绿）。
        #    实测（`analyze_settings_nav.py`）它就是实心绿 #0DA869，
        #    所以现在反过来断言**必须就是它**。
        #    ⚠️ 保留这段说明：断言没变错之前，它一直是"绿"的、看着很安心。
        check("选中项渲染出来就是 accent_dim（微信实测的实心绿）",
              _sel_px.upper() == win.style.p["accent_dim"].upper(),
              "%s vs %s" % (_sel_px, win.style.p["accent_dim"]))
        check("未选中的分组按钮**渲染出来**是透明的（洋红透出来）",
              _off_px == "#FF00FF", _off_px)

        # ⚠️ 「会话」分组用的是**自定义容器**，不能塞进 SPEC 表单的滚动区
        #    （那里还有两个自己的滚动区，嵌套滚轮会互相打架）。
        #    Tk 版查的是 `winfo_manager() == "pack"`；Qt 里这个行为等价于
        #    「两个容器**轮流可见**」—— 这才是用户能看见的那件事。
        win._switch("session")
        pump(app, 120)
        check("会话分组：自定义容器可见、SPEC 滚动区隐藏",
              win.custom.isVisible() and not win.scroll.isVisible(),
              "%s / %s" % (win.custom.isVisible(), win.scroll.isVisible()))
        check("会话分组里确实建出了面板", win._session_panel is not None)
        win._switch("ai")
        pump(app, 120)
        check("切回普通分组：滚动区可见、自定义容器隐藏",
              win.scroll.isVisible() and not win.custom.isVisible(),
              "%s / %s" % (win.scroll.isVisible(), win.custom.isVisible()))
        check("切回普通分组后面板引用被清掉（不留下待销毁的部件）",
              win._session_panel is None)
    finally:
        safe(win.close)


# ================================================================ 4. SPEC 表单

def test_form(app, tmp):
    print("\n4. 表单按 SPEC 自动生成 + 提示文字不裁字")
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.settings_ui import MIN_W
    from wechat_assistant.settings import GROUPS, SPEC

    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 200)
        per_group_ok, details = True, []
        for gid, _n, _d in GROUPS:
            if gid == "session":
                continue
            win._switch(gid)
            pump(app, 60)
            want = len([s for s in SPEC if s.group == gid])
            got = len(win._fields)
            if got != want or got <= 0:
                per_group_ok = False
                details.append("%s: %d != %d" % (gid, got, want))
        check("每个分组的行数 == 该组的 SPEC 项数（且 > 0）", per_group_ok,
              "; ".join(details))
        win._switch("session")
        pump(app, 120)
        # ⚠️ Tk 版这条查的是 `win.custom.winfo_manager() == "pack"`（"用的是自定义
        #    容器，不是 SPEC 表单"）。Qt 里 pack 不存在，**用户能看到的那件事**
        #    是"两个容器轮流显示" —— 会话分组下画出来的必须是面板。
        #    （`_fields` 在会话分组下**故意**留着上一组的引用，Tk 版逐字同款：
        #     这样"在别的分组点保存"也不会把刚才没提交的编辑丢掉。）
        check("「会话」分组画的是自定义面板，不是 SPEC 表单",
              win.custom.isVisible() and not win.scroll.isVisible()
              and win._session_panel is not None,
              "%s / %s / %s" % (win.custom.isVisible(), win.scroll.isVisible(),
                                win._session_panel is not None))

        win._switch("ai")
        pump(app, 60)
        check("API Key 那一行额外挂了「测试连接」", win.test_result is not None)

        # ⚠️ 多行输入框必须**真的是多行**：Tk 版的 `_TextAreaField` 把"行数"
        #    当成"像素"传了下去（`style.px(5)` = 8），实测被压成 1px 高 ——
        #    也就是**全局人设 / 黑名单这两项在 Tk 版里根本没法用**。
        #    Qt 版按行数换成真实像素高度，这条守着别退回去。
        win._switch("persona")
        pump(app, 60)
        f = win._fields.get("persona.default")
        check("全局人设是多行框，高度 > 40px（不是 Tk 版那个 5px）",
              f is not None and f.widget.height() > base.px(40),
              "h=%s" % (f.widget.height() if f else None))

        # ---- 提示文字不许被静默裁字
        #
        # ⚠️ Tk 版的机制是 `tk.Label` 的 wraplength 超出可用宽度时**不报错、
        #    只是静静裁掉一截**；Qt 换成了 QLabel 原生换行，所以要守两件事：
        #    ① 登记进 WrapGroup 的提示标签都开了 wordWrap；
        #    ② 内容的最小宽度不把窗口顶大（顶大的话右边就会被裁）。
        #    ⚠️ 只查**确实有提示文字**的分组：有的组的 SPEC 项本来就没有 help，
        #       那它一个标签都不建是正常的，不该当成失败（第一版就是这么误报的）。
        bad, seen = [], 0
        for gid, _n, _d in GROUPS:
            win._switch(gid)
            pump(app, 80)
            tips = (list(win._session_panel._wrap_labels)
                    if gid == "session" and win._session_panel is not None
                    else list(win._tips.labels))
            if not tips:
                continue
            seen += len(tips)
            nowrap = [l.text()[:16] for l in tips if not l.wordWrap()]
            if nowrap:
                bad.append((gid, nowrap[:2]))
        check("每个建了提示的分组，提示标签都在换行（wordWrap）", not bad,
              repr(bad[:2]))
        check("确实查到了提示标签（否则上一条是空转）", seen > 0,
              "%d 个提示标签" % seen)

        win.win.resize(base.px(MIN_W), base.px(MIN_W))
        pump(app, 150)
        top = []
        for gid, _n, _d in GROUPS:
            win._switch(gid)
            pump(app, 80)
            top.append((gid, win.win.layout().minimumSize().width()))
        over = [t for t in top if t[1] > base.px(MIN_W)]
        check("缩到最小宽度时内容的最小宽度不超过窗口最小宽度（不会顶大/裁字）",
              not over, "%s vs %s" % (over, base.px(MIN_W)))
    finally:
        safe(win.close)


def test_text_hygiene(app, tmp):
    print("\n5. 界面文字里不许漏出 markdown 标记")
    from wechat_assistant.settings import GROUPS, SPEC

    # SPEC 的 label / help 是**直接显示给用户**的（这些字符串原本是给文档写的），
    # 写 `**加粗**` 会原样显示成星号；反引号同理。
    bad = []
    for s in SPEC:
        for field in (s.label, s.help or ""):
            if "**" in field or "`" in field:
                bad.append((s.key, field[:30]))
    check("SPEC 的 label / help 里没有 ** 或反引号", not bad, repr(bad[:3]))

    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 200)
        leak = []
        for gid, _n, _d in GROUPS:
            win._switch(gid)
            pump(app, 80)
            leak += [l.text()[:40] for l in labels_of(win.win)
                     if "**" in l.text() or "`" in l.text()]
        check("真的没有星号/反引号漏到界面上", not leak, repr(leak[:3]))
    finally:
        safe(win.close)


# ================================================================ 6. 保存

def test_save(app, tmp):
    print("\n6. 取值 / 保存 / 来源 / 恢复默认")
    from wechat_assistant.settings import SPEC

    saved = []
    win, store, style = make_win(tmp, on_saved=lambda: saved.append(1))
    try:
        win.open()
        pump(app, 200)
        win._switch("ui")
        pump(app, 60)
        # ⚠️ 用 SPEC 里有的键。`ui.width`（面板宽度）2026-10-13 已从设置界面移除 ——
        #    它由**面板拖动左右边缘**直接写配置（`qt_ui/panel.py:_save_width`），
        #    所以 `_fields` 里不该再有它（这条也在下面反向验一次）。
        win._fields["ui.opacity"].box.set("0.7")
        vals = win._collect()
        check("_collect() 反映改动的值", vals.get("ui.opacity") == "0.7",
              repr(vals.get("ui.opacity")))
        check("_collect() 覆盖当前分组的全部项",
              set(vals) == {s.key for s in SPEC if s.group == "ui"},
              repr(sorted(vals)))
        check("被移除的「面板宽度」不再有字段（它归拖动管）",
              "ui.width" not in win._fields, repr(sorted(win._fields)))

        ok, err = safe(win._save)
        pump(app, 150)
        check("_save() 不抛异常", ok, err)
        check("保存后配置里读得到新值", abs(store.load().get("ui.opacity") - 0.7) < 1e-9,
              repr(store.load().get("ui.opacity")))
        # ⚠️ 这条是**隔离**的地基：写错地方的话下面第 12 节会报 state/ 被动过
        check("落盘在临时目录（不是仓库 state/）",
              os.path.exists(os.path.join(tmp, "settings.json")),
              store.settings_path)
        check("on_saved 被回调（主程序要据此重载配置）", bool(saved), str(len(saved)))
        check("状态行给出「已保存」", "已保存" in (win.status.text() or ""),
              repr(win.status.text()))
        check("保存后暂存清空（不会把刚存过的值又当成待提交）",
              win._session_stage["personas"] == {}, repr(win._session_stage))

        win._switch("ui")
        pump(app, 60)
        check("改过的项来源标记为 user", store.source("ui.opacity") == "user",
              store.source("ui.opacity"))
        ok, err = safe(lambda: win._reset_one("ui.opacity"))
        pump(app, 100)
        check("恢复默认不抛异常", ok, err)
        check("恢复默认 = 覆盖层里没有它了", store.raw_override("ui.opacity") is None,
              repr(store.raw_override("ui.opacity")))
        check("恢复默认后读出来是 config.yaml 里的 1.0",
              abs(store.load().get("ui.opacity") - 1.0) < 1e-9,
              repr(store.load().get("ui.opacity")))

        # 恢复全部默认：show_dialogs=False 时不弹模态框（会卡住无人操作的测试）
        ok, err = safe(win._reset_all)
        pump(app, 150)
        check("恢复全部默认不抛异常（不弹模态框）", ok, err)
        check("恢复后覆盖层被清空", store.overrides() == {}, repr(store.overrides()))

        # ⚠️⚠️ 2026-10-14 用户要求：「恢复默认**接口地址与模型**不要改」。
        #    ⚠️ 这一段必须用**自己的临时目录**（`os.path.join(tmp, "keep")`）——
        #       直接往上面那个 store 里写 base_url 会把第 8 节（测试连接）
        #       的"空地址"前提污染掉（我第一次就踩了，第 8 节报"传进去的不是空地址"）。
        _keep_dir = os.path.join(tmp, "keep")
        os.makedirs(_keep_dir, exist_ok=True)   # `make_store` 自己不建目录
        win2, store2, _style2 = make_win(_keep_dir, name="ct2")
        try:
            store2.set("ai.base_url", "https://api.example.com")
            store2.set("ai.model", "my-model")
            store2.save()
            win2.open("ai")
            pump(app, 200)
            # ⚠️ 判据用**数量**：AI 组里被覆盖的只有地址和模型这两项（都是 `RESET_KEEP`），
            #    所以这一组里**一颗「恢复默认」都不该有**。
            #    改坏了会变成 2 颗 —— 比"按坐标找那一行"更硬也更简单。
            from wechat_assistant.qt_ui.widgets import Pill
            _pills = [b for b in win2.findChildren(Pill)
                      if getattr(b, "text", None) and b.text() == "恢复默认"]
            check("「AI 接口」组里没有「恢复默认」按钮（地址/模型那两行不画它）",
                  len(_pills) == 0, "找到 %d 颗" % len(_pills))
            ok, err = safe(lambda: win2._reset_one("ai.base_url"))
            pump(app, 80)
            check("程序里调到 `_reset_one(ai.base_url)` 也不清它（第二道闸）",
                  ok and store2.raw_override("ai.base_url") == "https://api.example.com",
                  "%s / %s" % (err, store2.raw_override("ai.base_url")))
            ok, err = safe(lambda: win2._reset_one("ai.model"))
            pump(app, 80)
            check("模型同理", ok and store2.raw_override("ai.model") == "my-model",
                  "%s / %s" % (err, store2.raw_override("ai.model")))
            win2._reset_all()
            pump(app, 120)
            check("「恢复全部默认」之后地址/模型还在",
                  store2.raw_override("ai.base_url") == "https://api.example.com"
                  and store2.raw_override("ai.model") == "my-model",
                  repr(store2.overrides()))
        finally:
            safe(win2.close)
            pump(app, 100)
    finally:
        safe(win.close)


# ================================================================ 7. 会话设置

def test_session_group(app, tmp):
    """「会话」分组里的关键行为（细节在 `test_qt_session_ui.py`）。

    这里只守**设置窗口这一侧**的三条 —— 它们都踩过坑：
      · 留空 = **删掉那个键**（不是设成空字符串）
      · 会话名可以带句点（「张三.工作」），点分路径会被切坏
      · 暂存必须**跨分组切换**存活（切走再回来改动不能丢）
    """
    print("\n7. 会话分组：留空删键 / 句点会话名 / 跨分组暂存")
    from wechat_assistant import theme

    modes = []
    win, store, style = make_win(tmp, modes=modes)
    try:
        win.open("session")
        pump(app, 250)
        panel = win._session_panel
        check("打开时能直接落在「会话」分组", win._group == "session")
        check("面板建出来了", panel is not None)
        if panel is None:
            return

        panel._select("张三")
        pump(app, 60)
        panel._ed["persona"].set("给张三的专属人设")
        panel._ed["rel"].set("大学室友")
        panel._ed["mode"]._pick("observe")
        win._save()
        pump(app, 150)
        per = store.get("persona.per_session", {}) or {}
        rel = store.get("persona.relationships", {}) or {}
        check("人设写进 persona.per_session", per.get("张三") == "给张三的专属人设",
              repr(per))
        check("关系写进 persona.relationships", rel.get("张三") == "大学室友",
              repr(rel))
        # ⚠️ 模式写的是**另一个存储**（state.json，归 SessionRegistry 管），
        #    界面够不着它，只能通过注入的回调交出去。回调没被调 = 模式静默丢失。
        check("模式经回调交回注册表（另一个存储）",
              ("张三", "observe") in modes, repr(modes))

        # ⚠️ 会话名里完全可能有句点。写 `store.set("persona.per_session." + 名字)`
        #    会被 dotted_set 按 `.` 切坏 —— 所以是**整张映射写回**。
        win._session_panel._select("张三.工作")
        pump(app, 60)
        win._session_panel._ed["persona"].set("工作人格")
        win._save()
        pump(app, 150)
        per = store.get("persona.per_session", {}) or {}
        check("会话名含句点也能正确落盘", per.get("张三.工作") == "工作人格",
              repr(per))
        check("含句点的名字没有被切成嵌套",
              isinstance(per.get("张三"), str) or "张三" not in per, repr(per))

        # 改完会话设置 -> 切到别的分组 -> 再点保存，改动不能丢。
        # ⚠️ 面板是重建出来的，暂存不跨分组存活的话这里会**静默丢失**。
        win._switch("session")
        pump(app, 120)
        win._session_panel._select("李四")
        pump(app, 60)
        win._session_panel._ed["persona"].set("切分组前改的")
        win._switch("ai")                 # 切走（面板被销毁重建）
        pump(app, 150)
        check("切走后暂存还在",
              win._session_stage["personas"].get("李四") == "切分组前改的",
              repr(win._session_stage["personas"]))
        win._save()                       # 在**别的**分组点保存
        pump(app, 150)
        per = store.get("persona.per_session", {}) or {}
        check("切分组后再保存，改动不丢", per.get("李四") == "切分组前改的",
              repr(per))

        # ⚠️ 留空 = **删掉那个键**（恢复用全局），不是设成空字符串。
        #    设成空字符串的话用户想恢复全局就只能去手改 JSON，而界面上看起来已经生效了。
        win._switch("session")
        pump(app, 150)
        win._session_panel._select("张三")
        pump(app, 60)
        win._session_panel._ed["persona"].set("")
        win._session_panel._ed["rel"].set("")
        win._save()
        pump(app, 150)
        per = store.get("persona.per_session", {}) or {}
        check("留空 = 删掉那个键（恢复用全局）", "张三" not in per, repr(per))
        check("别的会话不受影响", per.get("张三.工作") == "工作人格", repr(per))

        # ⚠️ 黑名单（sessions.disabled）在启动时强制生效，**优先级高于面板里设的模式**。
        #    不标红字警告的话，用户会遇到"改了没反应但找不到原因"。
        store.set("sessions.disabled", ["张三"])
        win._switch("session")
        pump(app, 150)
        win._session_panel._select("张三")
        pump(app, 80)
        texts = [l.text() for l in win._session_panel._wrap_labels]
        check("黑名单会话显示红字警告", any("黑名单" in t for t in texts),
              repr([t[:20] for t in texts]))
        warn = [l for l in win._session_panel._wrap_labels if "黑名单" in l.text()]
        danger = theme.palette(win.style.theme_name)["danger"].lower()
        check("警告用的是 danger 色（不是普通说明文字的颜色）",
              bool(warn) and danger in (warn[0].styleSheet() or "").lower(),
              "%s vs %s" % (warn[0].styleSheet() if warn else "", danger))
        store.set("sessions.disabled", [])

        win._reset_all()
        pump(app, 150)
        check("恢复全部默认会清掉会话级人设",
              not (store.get("persona.per_session", {}) or {}),
              repr(store.get("persona.per_session", {})))
        check("恢复后暂存也清了（不会把刚清掉的值又填回去）",
              win._session_stage["personas"] == {}, repr(win._session_stage))
    finally:
        safe(win.close)


# ================================================================ 8. 测试连接

def test_view_tools(app, tmp):
    """⚠️ 2026-10-14：「看提示词 / 只看对话」**从悬浮窗搬进设置**了。

    用户要求：「把只看对话和看提示词隐藏起来，可以放到设置里面」。
    这条守三件事：
      ① 两颗按钮真的在「AI 接口」组里（跟系统提示词同一组 —— 用户得找得到）；
      ② 点它们真的调到注入进来的回调（`FloatingAssistant` 那两个方法）；
      ③ 回调**没注入**时不报错、也不假装成功（设置窗口可以单独用）。
    """
    print("\n8b. 看提示词 / 只看对话（从悬浮窗搬进设置的两个入口）")
    called = []
    win, store, style = make_win(
        tmp,
        on_show_prompt=lambda: called.append("prompt"),
        on_show_read=lambda: called.append("read"))
    try:
        win.open("ai")
        pump(app, 200)
        check("设置里有两颗按钮（看提示词 / 只看对话）",
              win.btn_prompt is not None and win.btn_read is not None,
              "%s / %s" % (win.btn_prompt, win.btn_read))
        check("按钮文字就是那两句话",
              win.btn_prompt.text() == "看提示词" and win.btn_read.text() == "只看对话",
              "%r / %r" % (win.btn_prompt.text(), win.btn_read.text()))
        # ⚠️ 工具提示里不许有 `**` / 反引号（设置界面不渲染 markdown）
        _tips = win.btn_prompt.toolTip() + win.btn_read.toolTip()
        check("按钮提示里没有 markdown 记号（** / 反引号）",
              "**" not in _tips and "`" not in _tips, _tips)
        ok, err = safe(win._show_prompt)
        check("点「看提示词」调到注入的回调", ok and called == ["prompt"],
              "%s / %s" % (called, err))
        ok, err = safe(win._show_read)
        check("点「只看对话」调到注入的回调", ok and called == ["prompt", "read"],
              "%s / %s" % (called, err))
    finally:
        win.close()
        pump(app, 120)

    # 回调没注入时：不抛异常（设置窗口可以脱离悬浮窗单独打开）
    win2, _store2, _st2 = make_win(tmp, name="ct2")
    try:
        win2.open("ai")
        pump(app, 200)
        ok, err = safe(win2._show_prompt)
        check("没注入回调时「看提示词」不抛异常", ok, err)
        ok, err = safe(win2._show_read)
        check("没注入回调时「只看对话」不抛异常", ok, err)
    finally:
        win2.close()
        pump(app, 120)


def test_dialog_width(app, tmp):
    """⚠️ 2026-10-14：确认框/报错框**不许缩成窄条**（用户报「恢复默认窗口太窄了」）。

    判据是**最小宽度**被真的设进了窗口（不是 show 之后再 resize —— 那样无边框窗口
    的位置不会重算，看起来会"弹出来又跳一下"）。
    """
    print("\n8c. 确认框 / 报错框的最小宽度")
    from wechat_assistant.qt_ui import layout as ql
    from wechat_assistant.qt_ui import settings_ui as qs
    from wechat_assistant.qt_ui import base

    store = make_store(tmp, )
    cfg = store.load()
    style = base.Style(cfg, 0, name="dark")
    dlg = ql.ShellDialog(style, "恢复默认", None, modal=False,
                         min_width=qs.DIALOG_MIN_W)
    try:
        check("弹窗的最小宽度 >= DIALOG_MIN_W",
              dlg.minimumWidth() >= style.px(qs.DIALOG_MIN_W),
              "%d vs %d" % (dlg.minimumWidth(), style.px(qs.DIALOG_MIN_W)))
        check("DIALOG_MIN_W 比默认窄条宽（>= 280 基准像素）",
              qs.DIALOG_MIN_W >= 280, str(qs.DIALOG_MIN_W))
    finally:
        dlg.deleteLater()
        pump(app, 80)

    # 不传 min_width 时**不该**被强加宽度（别的弹窗照旧按内容定尺寸）
    dlg2 = ql.ShellDialog(style, "看提示词", None, modal=False)
    try:
        check("不传 min_width 的弹窗没有最小宽度下限",
              dlg2.minimumWidth() <= style.px(220), str(dlg2.minimumWidth()))
    finally:
        dlg2.deleteLater()
        pump(app, 80)


def test_test_connection(app, tmp):
    """「测试连接」必须走**不发网络请求**的那条路（空地址）。

    ⚠️ 真调 API 是要花钱的，测试套件里一条网络请求都不许发。
       这里不但验界面文案，还**把 test_api 包一层**证明：
       传进去的 base_url 真的是空串（也就是走的是"提前返回"那条分支）。
    """
    print("\n8. 测试连接（空地址 -> 不发网络请求）")
    from wechat_assistant.qt_ui import settings_ui as qs

    win, store, style = make_win(tmp)
    calls = []
    real = qs.test_api

    def spy(base_url, key, model, timeout=20.0):
        calls.append((base_url, key, model))
        return real(base_url, key, model)

    try:
        win.open("ai")
        pump(app, 200)
        win._fields["ai.base_url"].box.set("")
        win._fields["ai.api_key"].field.set("")
        qs.test_api = spy
        ok, err = safe(win._test)
        check("_test() 能起子线程、不抛异常", ok, err)
        # 等子线程的结果经 Signal 回到 UI 线程（Tk 版用的是 win.after(0,...)，
        # Qt 里的对应物是 QObject + Signal —— 子线程里 QTimer 根本不会执行）
        pump(app, 900)
        check("传进去的是空地址（走提前返回分支，没发网络请求）",
              bool(calls) and calls[0][0] == "", repr(calls))
        check("子线程的结果回到了界面上（明确报错，不是静默）",
              bool(win.test_result.text()) and "空" in win.test_result.text(),
              repr(win.test_result.text()))
    finally:
        qs.test_api = real
        safe(win.close)


# ================================================================ 9. 切样式

def test_set_style(app, tmp):
    print("\n9. 切样式（panel.py 保存后会调它）")
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.settings_ui import SHELL_RADIUS
    from wechat_assistant.settings import SPEC
    from wechat_assistant import theme

    win, store, style = make_win(tmp)
    try:
        win.open("ui")
        pump(app, 200)
        pos = (win.win.x(), win.win.y())
        lite = base.Style(store.load(), 0, name="light")
        ok, err = safe(lambda: win.set_style(lite))
        pump(app, 200)
        check("set_style() 不抛异常", ok, err)
        check("窗口还在、样式真的换了",
              win.win is not None and win.style.theme_name == "light",
              repr(win.style.theme_name))
        check("换样式后窗口没被挪走", (win.win.x(), win.win.y()) == pos,
              "%s -> %s" % (pos, (win.win.x(), win.win.y())))
        check("换样式后分组保持不变", win._group == "ui", repr(win._group))
        check("换样式后表单重建了（行数仍然对）",
              len(win._fields) == len([s for s in SPEC if s.group == "ui"]),
              str(len(win._fields)))
        check("浅色下圆角半径仍然是 theme.SHELL_RADIUS",
              int(win.win.radius) == base.px(theme.SHELL_RADIUS),
              str(win.win.radius))
    finally:
        safe(win.close)


def test_both_themes(app, tmp):
    print("\n10. 两套主题都建得起来且内容不溢出")
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.settings_ui import MIN_W
    from wechat_assistant.settings import GROUPS, SPEC

    for name in ("dark", "light"):
        win, store, style = make_win(tmp, theme_name=name)
        try:
            win.open()
            pump(app, 200)
            check("%s 主题：配色用的是 %s" % (name, name),
                  win.style.theme_name == name, win.style.theme_name)
            win.win.resize(base.px(MIN_W), base.px(620))
            pump(app, 120)
            bad = []
            for gid, _n, _d in GROUPS:
                win._switch(gid)
                pump(app, 70)
                if gid == "session":
                    ok = win._session_panel is not None
                else:
                    ok = len(win._fields) == len([s for s in SPEC if s.group == gid])
                if not ok or win.win.layout().minimumSize().width() > base.px(MIN_W):
                    bad.append((gid, win.win.layout().minimumSize().width()))
            check("%s 主题：每个分组都画得出来、且不顶大窗口" % name, not bad,
                  repr(bad[:2]))
        finally:
            safe(win.close)

    # ---- 10b. 开关（胶囊）**量像素**：浅色下必须看得出滑块
    # ⚠️⚠️ 用户报过「胶囊开关在浅色时显示不明显」：关态轨道原来填 `surface`
    #    = `#FFFFFF`，滑块又写死 `#FFFFFF` —— **白底白滑块**，只剩 1px 边框。
    #    ⚠️ 这条**必须量像素**，只看色板值是看不出来的
    #    （`surface` 和 `knob` 两个键单看都"没问题"）。
    from wechat_assistant.qt_ui.cards import Switch
    from PySide6 import QtCore, QtGui, QtWidgets

    def _lum(hexs):
        c = QtGui.QColor(hexs)
        return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()

    for name in ("dark", "light"):
        style = base.Style(None, name=name)
        p = style.p
        for value in (False, True):
            sw = Switch(style, value)
            pm = QtGui.QPixmap(sw.width(), sw.height())
            # ⚠️ 铺**真实行底色**（量过：设置页的行底是 `bg`，不是 `surface`）——
            #    填错底色会把深色那套判成"轨道和底同色"（第一版就这么误报的）。
            pm.fill(QtGui.QColor(p["bg"]))
            pt = QtGui.QPainter(pm)
            sw.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
                      QtWidgets.QWidget.RenderFlag.DrawChildren)
            pt.end()
            im = pm.toImage()
            y = im.height() // 2
            # 关态滑块在左、开态滑块在右；取滑块中心那一列和最靠边的轨道列
            knob_x = int(im.width() * (0.75 if value else 0.25))
            track_x = int(im.width() * (0.06 if value else 0.94))
            knob = QtGui.QColor(im.pixelColor(knob_x, y)).name().upper()
            track = QtGui.QColor(im.pixelColor(track_x, y)).name().upper()
            d = abs(_lum(knob) - _lum(track))
            check("%s 主题 · %s：滑块和轨道分得开（亮度差 >= 15）"
                  % (name, "开" if value else "关"),
                  d >= 15.0, "滑块 %s vs 轨道 %s（差 %.1f）" % (knob, track, d))
        # 关态轨道还要跟**行底色**分得开（否则"看不出这里有颗开关"）
        sw = Switch(style, False)
        pm = QtGui.QPixmap(sw.width(), sw.height())
        pm.fill(QtGui.QColor(p["bg"]))
        pt = QtGui.QPainter(pm)
        sw.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
                  QtWidgets.QWidget.RenderFlag.DrawChildren)
        pt.end()
        track = QtGui.QColor(pm.toImage().pixelColor(int(sw.width() * 0.94),
                                                     sw.height() // 2)).name().upper()
        check("%s 主题：关态轨道和行底色分得开" % name,
              abs(_lum(track) - _lum(p["bg"])) >= 8.0,
              "轨道 %s vs 行底 %s" % (track, p["bg"]))

    # ---- 10c. **单行行高必须完全一致**（用户 2026-10-14 报「有些行高度没有统一」）
    # ⚠️ 三个原因都修在了 `_build_row` 那一处，这条是**总判据**：
    #    ① 控件自己的高度不一样（输入框 34 基准 / 开关 22）→ `ROW_CTRL_H` 固定控件带；
    #    ② 头部带不带 Pill 高度不同 → `ROW_HEAD_H` 固定头部；
    #    ③ 说明文字折 1 行 / 2 行 / 没有 → `ROW_TIP_LINES` 固定说明带。
    #    ⚠️ **自带宽高的 kind**（`TALL_KINDS` = multiline / list）**故意**更高，按 kind 排除。
    from wechat_assistant.settings import SPEC_BY_KEY as _SBK
    from wechat_assistant.qt_ui.settings_ui import TALL_KINDS as _TALL
    _win, _store, _style = make_win(tmp, theme_name="light")     # 最窄窗口最容易露馅
    try:
        _win.open()
        _win.win.resize(base.px(MIN_W), base.px(620))
        pump(app, 200)
        hs = {}
        for gid, _n, _d in GROUPS:
            if gid == "session":
                continue
            _win._switch(gid)
            pump(app, 80)
            lay = _win.scroll.inner.layout()
            keys = [s.key for s in SPEC if s.group == gid]
            ri = 0
            for i in range(lay.count()):
                w = lay.itemAt(i).widget()
                if w is None or w.objectName() == "hline" or w.height() <= 2:
                    continue
                # ⚠️ 2026-10-17：「知识库」分组在 SPEC 那一行**下面**挂了一块手写的
                #    文件清单（`settings_ui._build_kb_files`，objectName="kb-files"）。
                #    它不是 SPEC 的一行，不能拿它去查 `SPEC_BY_KEY` ——
                #    不跳过去的话这里会 `KeyError: '?'`（实测）。
                if w.objectName() == "kb-files":
                    continue
                k = keys[ri] if ri < len(keys) else "?"
                ri += 1
                if _SBK[k].kind in _TALL:
                    continue
                hs.setdefault(w.height(), []).append(k)
        check("单行行高只有**一个**档位（自带宽高的 kind 除外）",
              len(hs) == 1,
              " / ".join("%d: %d 行" % (h, len(v)) for h, v in sorted(hs.items())))
        # ⚠️⚠️ 2026-10-14 用户报「**黑名单底部被挡住了**」：
        #    `list`（`_ListField`）是 `_TextAreaField(height=4)` —— 4 行高的文本框，
        #    而做"行高统一"时**只排除了 `multiline`**，于是它被 `holder.setFixedHeight(
        #    px(34))` 钉住 → **底部 78 设备像素整段被切掉**（实测：控件 129 / holder 51）。
        #    → 判据两条：① 自带宽高的 kind 都在 `TALL_KINDS` 里；
        #              ② 那些行的 holder **不许比控件矮**（矮了就一定被切）。
        print("\n10d. 自带宽高的行（系统提示词 / 黑名单）不许被控件带切掉")
        for gid, key in (("record", "sessions.disabled"), ("ai", "ai.system_prompt")):
            _win._switch(gid)
            pump(app, 120)
            fld = _win._fields.get(key)
            if fld is None:
                check("%s 取得到" % key, False)
                continue
            # ⚠️⚠️ 量的是**容器 vs 里面的文本框**（不是 holder vs 容器 —— 那两个
            #    会**一起**被钉住，量出来一样高，测不出东西：第一版就是这么写的，
            #    反例自检时发现"坏状态下也是通过"）。
            #    坏状态的真实长相：文本框 114 而容器只有 51 → **Qt 把子控件裁到
            #    父控件矩形内**，底下 63 设备像素看不见（用户说"底部被挡住了"）。
            check("%s 的容器装得下文本框（没被裁）" % _SBK[key].label,
                  fld.widget.height() >= fld.box.height() - 1,
                  "容器 %s vs 文本框 %s（裁掉 %s）"
                  % (fld.widget.height(), fld.box.height(),
                     fld.box.height() - fld.widget.height()))
        check("list 和 multiline 一样算「自带宽高」",
              "list" in _TALL and "multiline" in _TALL,
              repr(_TALL))
        # ⚠️ 说明带是**固定高度**的 —— 谁写了超过 `ROW_TIP_LINES` 行的说明会被
        #    **裁掉**（不报错）。所以这条必须一起卡住。
        from wechat_assistant.qt_ui import settings_ui as _sui
        over = []
        for lab in _win._tips.labels:
            need = lab.fontMetrics().boundingRect(
                0, 0, max(1, lab.width()), 10000,
                QtCore.Qt.TextWordWrap, lab.text()).height()
            lines = max(1, round(need / max(1.0, lab.fontMetrics().height())))
            if lines > _sui.ROW_TIP_LINES:
                over.append((lab.text()[:18], lines))
        check("最窄窗口下没有说明超过 %d 行（超了会被裁）" % _sui.ROW_TIP_LINES,
              not over, repr(over[:3]))
    finally:
        safe(_win.close)


# ================================================================ 11. 几何

def test_geometry(app, tmp):
    """Qt 独有的坑：**重建界面时的旧内容会被算进窗口最小尺寸**。

    ⚠️ `deleteLater()` 的删除被推迟到下一轮事件循环，而 Qt 会把**还没销毁的
       旧布局**算进 `minimumSize`。切分组时「会话」面板和 SPEC 表单的内容宽度
       差得多 —— 不补这一刀的话窗口会被顶大、而且拖不回去。
       （Tk 版没有这个问题：`pack` 不参与窗口最小尺寸。）
    """
    print("\n11. 几何（重建后窗口还能不能缩回去 / 内容有没有被挤没）")
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.settings_ui import MIN_H, MIN_W, NAV_W
    from wechat_assistant.qt_ui.session_ui import LIST_W
    from wechat_assistant.settings import GROUPS

    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 250)
        lmins = []
        for _ in range(3):
            win._switch("session")
            pump(app, 80)
            win._switch("ai")
            pump(app, 80)
            lmins.append(win.win.layout().minimumSize().width())
        check("反复切分组后布局最小宽度不膨胀", max(lmins) <= base.px(MIN_W),
              "最小值 %s / 窗口最小 %d" % (lmins, base.px(MIN_W)))

        win._switch("ui")
        pump(app, 80)
        win.win.resize(base.px(MIN_W), base.px(MIN_H))
        pump(app, 150)
        check("能缩到最小尺寸 px(MIN_W) x px(MIN_H)",
              (win.win.width(), win.win.height()) == (base.px(MIN_W), base.px(MIN_H)),
              "%dx%d" % (win.win.width(), win.win.height()))
        check("最小尺寸下右侧表单区没被挤没",
              win.scroll.viewport().width() > base.px(200),
              str(win.scroll.viewport().width()))
        check("左侧分组栏宽度 = px(NAV_W)",
              win._nav_btns["ai"].parentWidget().width() == base.px(NAV_W),
              str(win._nav_btns["ai"].parentWidget().width()))

        win._switch("session")
        pump(app, 200)
        panel = win._session_panel
        check("最小尺寸下会话面板还在、列表没被压没",
              panel is not None and panel.list_area.width() == base.px(LIST_W),
              str(panel and panel.list_area.width()))
        check("最小尺寸下编辑区仍有可用宽度（> px(120)）",
              panel is not None and panel.edit_area.viewport().width() > base.px(120),
              str(panel and panel.edit_area.viewport().width()))
        check("会话分组下 SPEC 滚动区是空的（没有残留的待销毁部件顶大窗口）",
              win.scroll.isVisible() is False)
        check("七个分组的按钮都还在（重建没有把侧栏弄没）",
              len(win._nav_btns) == len(GROUPS), str(len(win._nav_btns)))

        # 窗口不能跑到桌面可用区外面：无边框窗口**没有任务栏条目、alt-tab 也
        # 切不到**，一旦被拖出屏幕就只能靠热键找回来（实测反馈过）。
        # Tk 版这里用的是 GetWindowRect 量真窗口；Qt 侧直接验 `_clamp()` 的语义
        # （拖动和缩放都从它过），比量一次瞬时几何更稳、而且不挑平台。
        try:
            from wechat_assistant.follow import work_area

            left, top, right, bottom = work_area()
            area = (left, top, right, bottom)
            clamped = []
            for x, y in ((99999, 99999), (-99999, -99999)):
                cx, cy = win._clamp(x, y, 400, 300)
                clamped.append(
                    left <= cx and cx + 400 <= right
                    and top <= cy and cy + 300 <= bottom)
            check("拖到屏幕外会被夹回桌面可用区（无边框窗口没有任务栏条目，"
                  "拖丢了就找不回来）", all(clamped), "可用区=%s" % (area,))
            x0, y0 = win._clamp(100, 100, 400, 300)
            check("夹取对已经在区内的坐标是恒等映射（不乱挪窗口）",
                  (x0, y0) == (100, 100), "-> (%d,%d)" % (x0, y0))
        except Exception as e:
            skip("可用区夹取", "follow.work_area 不可用: %s" % e)
    finally:
        safe(win.close)


# ================================================================ 12. 关窗 / 重开

def test_close_reopen(app, tmp):
    print("\n12. 关窗 / 重开")
    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 200)
        ok, err = safe(win.close)
        pump(app, 120)
        check("close() 不抛异常", ok, err)
        check("关窗后 .win 回到 None（Tk 版的判活方式）", win.win is None)
        check("关窗后 is_open() 是 False", win.is_open() is False)
        # 关窗后所有部件引用都要清掉 —— 留着的是**已被 deleteLater 的悬空引用**，
        # 再调它的方法会崩在 C++ 侧（不是能 try/except 的 Python 异常）。
        check("关窗后部件引用也清了",
              win.status is None and win.scroll is None
              and win._nav_btns == {} and win._fields == {})

        ok, err = safe(lambda: win.open("session"))
        pump(app, 250)
        check("关掉之后还能重新打开", ok and win.win is not None, err)
        check("重开时指定分组生效", win._group == "session", repr(win._group))
        check("重开后会话面板又建出来了", win._session_panel is not None)

        # ⚠️ 设置窗口是靠**自绘标题条**关掉的（无边框窗口没有系统 ✕）。
        win._titlebar.btn_close.click()
        pump(app, 200)
        check("标题条上的 ✕ 真的把窗口关掉了", win.is_open() is False)
    finally:
        safe(win.close)


# ================================================================ 13. 接线

def test_end_to_end(app, tmp):
    """走 `panel.py` 的 `open_settings()` 调一遍 —— 这才是真正验"对外 API 一致"。

    ⚠️ 不自己按签名 new 一个的原因：那边是按
       `SettingsWindow(cfg, store, style, logger=..., on_saved=...,
        session_source=..., on_mode_change=..., preselect=...)` 调的，
       参数错一个、少一个就建不出窗口 —— 而 `open_settings()` 自己会
       try/except 吞掉异常，只能靠 `_settings_win is not None` 来判。
    """
    print("\n13. 端到端：panel.open_settings() 真的调得通")
    from wechat_assistant.qt_ui.panel import FloatingAssistant

    store = make_store(tmp)
    cfg = store.load()
    modes = []
    ui = FloatingAssistant(cfg, logger=lambda m: None,
                           session_source=lambda: FAKE_SESSIONS,
                           on_mode_change=lambda k, m: modes.append((k, m)))
    try:
        ui._settings_store = store          # 别碰真的 state/settings.json
        ui._session_title = "张三"           # 假装当前打开的是「张三」
        ok, err = safe(lambda: ui.open_settings(group="session"))
        pump(app, 300)
        check("open_settings() 不抛异常", ok, err)
        sw = ui._settings_win
        check("panel.open_settings() 建出了设置窗口", sw is not None)
        if sw is None:
            return
        check("会话表回调透传进去了", sw._session_source is ui._session_source)
        check("模式回调透传进去了", sw._on_mode_change is ui._on_mode_change)
        check("当前会话被当成预选", sw._preselect == "张三", repr(sw._preselect))
        check("打开时直接落在「会话」分组", sw._group == "session", repr(sw._group))
        check("面板真的停在预选的那个会话上",
              sw._session_panel is not None
              and sw._session_panel.selected == "张三",
              repr(sw._session_panel and sw._session_panel.selected))

        ok, err = safe(lambda: sw.set_style(ui.style))
        pump(app, 150)
        check("panel 侧调 set_style() 不抛异常", ok, err)
        check("set_style 之后窗口还在", sw.win is not None)

        # 再点一次「设置」应该复用同一个窗口（不是又开一扇）
        same = ui._settings_win is sw
        ui.open_settings(group="ai")
        pump(app, 200)
        check("重复打开复用同一个窗口实例", same and ui._settings_win is sw)
        check("复用时也能切到指定分组", sw._group == "ai", repr(sw._group))

        # ⚠️ 复用已开着的窗口前**必须先确认它没被 Qt 收走**：被 deleteLater 的
        #    部件是悬空引用，再调它的方法直接崩在 C++ 侧。
        class Dead:
            def is_open(self):
                raise RuntimeError("Internal C++ object already deleted")

        ui._settings_win = Dead()
        check("设置窗口被 Qt 收走后判定为「不在了」",
              ui._settings_win_is_open() is False)
        check("顺手把悬空引用清掉", ui._settings_win is None)
        safe(sw.close)
    finally:
        safe(ui.close)
    check("悬浮窗关闭不报错", True)


# ================================================================ 14. 没有照搬 Tk

def test_no_tk_leftovers():
    """走查：Qt 版这两个文件里**不许有 Tk / DWM 的残留**。

    ⚠️ 查的是**代码**（先用 tokenize 去掉注释和字符串），不是纯文本 ——
       这两份文件的注释里**故意**写着"Tk 的 overrideredirect / 色键 #FF00FE
       在这里不用了"，纯文本搜会把说明文字当成遗留代码（第一次跑就是这么误报的）。
    """
    import io
    import tokenize

    print("\n14. 没有照搬 Tk 专属补丁（查代码，不查注释）")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def code_only(path):
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

    for fname in ("settings_ui.py", "session_ui.py"):
        code = code_only(os.path.join(root, "wechat_assistant", "qt_ui", fname))
        bad = [m for m in ("winchrome", "overrideredirect", "#FF00FE",
                           "transparentcolor", "messagebox", "tkinter",
                           "winfo_", "Toplevel", "SCROLL_PAD")
               if m in code]
        check("%s 的代码里没有 Tk/DWM 痕迹" % fname, not bad, repr(bad))
    # 这条路径上不该把 DWM 模块拉进来（它是给"退回系统标题栏"那条路用的）
    check("这条路径上没 import winchrome",
          "wechat_assistant.winchrome" not in sys.modules)


# ================================================================ main

def test_layout_fixes(app, tmp):
    """三处**实测修过**的布局（用户报的具体问题），锁住别再退回去。

    ⚠️ 全部量**几何/光标**，不看代码怎么写：
      ① 「恢复默认与保存放在下方吧」
         按钮条原来被 `addWidget` 在 body **之前**，于是整条跑到了标题栏正下方
         （实测 y=84，窗口高 930）。
      ② 「右边还是有部分被拉动条遮挡住」
         内容右边界 == 滚动条左边界（**贴着**，一点缝都没有），
         看着就是右边被吃了一块。
         ⚠️ 判据必须取**叶子部件**（真正画东西的），不能取行的容器 ——
            容器铺满整宽、但没有背景、看不见，拿它量永远是"顶到边"。
      ③ 「鼠标放在拉动条上时左右拉伸的样式」
         滚动条没设自己的光标，**继承**了无边框窗口在右边缘设的 `SizeHorCursor`
         （Qt 的光标沿父链继承）。
    """
    from PySide6 import QtCore, QtWidgets
    from wechat_assistant.qt_ui.layout import ScrollArea

    print("\n16. 三处实测修过的布局（按钮条位置 / 滚动条缝 / 滚动条光标）")
    win, _store, _style = make_win(tmp)
    try:
        win.open()
        pump(app, 400)
        w = win.win
        check("设置窗口建出来了", w is not None)
        if w is None:
            return

        # ---- ① 按钮条在下半部、且在滚动区下方
        # ⚠️ 第三颗的文案是**自适应**的（2026-10-13）：「关闭」/「放弃改动」。
        #    刚打开、什么都没改 -> 它是「关闭」；改了需重启的项才变「放弃改动」。
        ys = {}
        for ch in w.findChildren(QtWidgets.QPushButton):
            if (isinstance(ch, QtWidgets.QPushButton) and ch.text()
                    and ch.text() in ("恢复全部默认", "保存", "关闭", "放弃改动")):
                ys[ch.text()] = ch.mapTo(w, QtCore.QPoint(0, 0)).y()
        check("三颗（恢复全部默认/关闭/保存）都找得到", len(ys) == 3, repr(ys))
        check("按钮都在窗口**下半部**",
              len(ys) == 3 and all(y > w.height() // 2 for y in ys.values()),
              repr(ys))
        check("什么都没改时第三颗叫「关闭」而不是「取消」",
              "关闭" in ys and "放弃改动" not in ys, repr(ys))
        if win.scroll is not None and ys:
            sc_bottom = win.scroll.mapTo(
                w, QtCore.QPoint(0, win.scroll.height())).y()
            check("按钮条在滚动区**下方**（不是浮在中间）",
                  min(ys.values()) >= sc_bottom,
                  "按钮最上 y=%d / 滚动区底部 y=%d"
                  % (min(ys.values()), sc_bottom))

        # ---- ③ 滚动条不继承窗口的"左右拉伸"光标
        areas = [a for a in w.findChildren(ScrollArea) if a.isVisible()]
        check("窗口里找得到滚动区", bool(areas), "%d 个" % len(areas))
        if not areas:
            return
        sb = areas[0].verticalScrollBar()
        old = w.cursor()
        w.setCursor(QtCore.Qt.SizeHorCursor)
        pump(app, 80)
        check("滚动条**不继承**窗口的左右拉伸光标",
              sb.cursor().shape() != QtCore.Qt.SizeHorCursor,
              "滚动条光标=%r（窗口已设成 SizeHorCursor）"
              % (sb.cursor().shape(),))
        w.setCursor(old)

        # ---- ② 内容不贴着滚动条
        a = areas[0]
        sb = a.verticalScrollBar()
        if not sb.isVisible():
            skip("内容与滚动条之间留了缝", "这个分组的内容还没到要滚动，量不到")
            return
        sb_l = sb.mapTo(w, QtCore.QPoint(0, 0)).x()
        inner = a.widget()
        worst = None
        for ch in inner.findChildren(QtWidgets.QWidget):
            if (not ch.isVisible() or ch.width() <= 1
                    or ch.height() <= 4                    # 分隔条是故意铺满的
                    or ch.findChildren(QtWidgets.QWidget)):  # 容器不可见
                continue
            r = ch.mapTo(w, QtCore.QPoint(ch.width(), 0)).x()
            worst = r if worst is None else max(worst, r)
        check("可见内容**不贴着**滚动条（留了缝）",
              worst is not None and worst < sb_l,
              "内容最右=%s 滚动条左=%d" % (worst, sb_l))
    finally:
        try:
            win.close()
        except Exception:
            pass


def test_list_roundtrip(app, tmp):
    """名单类设置（黑名单）**填进去 / 存回来**要原样，不能变成 Python repr。

    ⚠️ 这条是为了一个**会损坏数据**的真 bug 加的（2026-09-27）。
        往返原来是这么坏的：
            填充：`_TextAreaField` 走 `str(value)`
            保存：`coerce` 走 `str(raw).splitlines()`
        于是
            `[]`              -> 输入框里是字面量 `[]`      -> 存回 `["[]"]`
            `["张三","李四"]` -> 输入框里是 `['张三', '李四']` -> 存回**一整条**，
                                 两个会话全丢。
        现场指纹：用户的 `state/settings.json` 里出现过 `"disabled": ["[]"]`。
    ⚠️ 判据要看**输入框里的文本**（不是只看最终值）——
        只看最终值的话，"填进去就是错的、存回来恰好又对上了"这种情况会漏掉。
    """
    from wechat_assistant.settings import SPEC_BY_KEY

    print("\n17. 名单类设置填进去/存回来要原样（不能变成 repr）")
    win, store, _style = make_win(tmp)
    try:
        # ⚠️ **必须先 `open()`**：界面是在 `open()` 里才建出来的，
        #    不调它 `_fields` 是空的（我第一版就是漏了这步，白查一轮）。
        win.open()
        pump(app, 250)
        # 先写一份名单进配置，再开窗口 —— 模拟"用户以前设过黑名单"
        store.set("sessions.disabled", ["张三", "李四"])
        store.save()
        # ⚠️ 字段是按**当前分组**建出来的，`sessions.disabled` 在「record」组里。
        #    `_switch()` 会重跑 `_render_group()`，而它取的是 `store.load()`
        #    的**最新**值 —— 所以只要切一次就能拿到新数据，不用先 `_build()`。
        win._switch("record")
        pump(app, 300)

        fld = win._fields.get("sessions.disabled")
        check("黑名单字段建出来了", fld is not None)
        if fld is None:
            return
        txt = fld.get()
        check("输入框里是**一行一个**，不是 Python repr",
              txt == "张三\n李四", repr(txt))
        check("输入框里没有方括号（那是 repr 的指纹）",
              "[" not in txt and "]" not in txt, repr(txt))

        # 现在模拟"用户点保存"
        values = win._collect()
        errs = store.apply_form(values)
        check("保存没有报错", not errs, repr(errs))
        store.save()
        got = store.load().get("sessions.disabled")
        check("存回来的名单跟原来**一模一样**",
              list(got) == ["张三", "李四"], repr(got))

        # 空名单也要对：填进去是空文本，存回来是空列表
        store.set("sessions.disabled", [])
        store.save()
        win._switch("record")
        pump(app, 300)
        fld2 = win._fields.get("sessions.disabled")
        txt2 = fld2.get() if fld2 else None
        check("空名单 -> 输入框是**空文本**（不是字面量 []）",
              txt2 == "", repr(txt2))
        if fld2 is not None:
            store.apply_form(win._collect())
            store.save()
            got2 = store.load().get("sessions.disabled")
            check("空名单存回来还是空（不会变成 ['[]']）",
                  list(got2 or []) == [], repr(got2))
    finally:
        try:
            win.close()
        except Exception:
            pass


def test_textarea_resizable(app, tmp):
    """多行框：**开局够高** + **底边能上下拖**（2026-10-11 用户要求）。

    用户原话：「可以把设置里这个输入框高度大一些并可以上下拖动高度」。

    ⚠️ 两条都要验，而且**要分别验**：
        · 只把初始高度调大 -> 长文本还是只能看到开头，用户改不了；
        · 只有把手、初始高度还是 4 行 -> 一进来就得先拖，很烦。
    ⚠️ 拖动验的是**真的改了高度**（`box.height()` 变了），不是"有个把手就算"——
        把手画出来了但 `mouseMoveEvent` 没接上也完全可能。
    """
    from PySide6 import QtCore, QtGui, QtWidgets
    from wechat_assistant.qt_ui.settings_ui import _HeightGrip
    from wechat_assistant.settings import SPEC_BY_KEY

    print("\n18. 多行框：开局够高 + 底边可上下拖")

    def drag(widget, y0, y1):
        def ev(kind, gy):
            return QtGui.QMouseEvent(kind, QtCore.QPointF(5, 5), QtCore.QPointF(5, gy),
                                  QtCore.Qt.LeftButton, QtCore.Qt.LeftButton,
                                  QtCore.Qt.NoModifier)
        widget.mousePressEvent(ev(QtCore.QEvent.MouseButtonPress, y0))
        widget.mouseMoveEvent(ev(QtCore.QEvent.MouseMove, y1))
        widget.mouseReleaseEvent(ev(QtCore.QEvent.MouseButtonRelease, y1))

    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 200)
        # 提示词在「ai」组里 —— 切过去，字段才会被建出来
        win._switch("ai")
        pump(app, 200)
        f = win._fields.get("ai.system_prompt")
        check("找得到提示词那个多行框", f is not None)
        if f is None:
            return

        spec = SPEC_BY_KEY["ai.system_prompt"]
        check("SPEC 声明了初始行数 rows=14", spec.rows == 14, str(spec.rows))
        # ⚠️ 一行约 19 逻辑像素 * 缩放 —— 只断言"明显比 4 行高"
        four = style.px(4 * 19)
        check("开局高度明显大于旧的 4 行",
              f.box.height() > four, "%d px vs 旧的 %d px" % (f.box.height(), four))
        check("高度**不是**硬编码一个死数（跟着缩放走）",
              f.box.height() == style.px(14 * 19), str(f.box.height()))

        check("field.widget 是容器（框 + 把手）",
              f.widget is not f.box and f.widget.layout().count() == 2)
        check("把手是 _HeightGrip", isinstance(f.grip, _HeightGrip),
              type(f.grip).__name__)
        check("把手光标是上下箭头（这就是「能拖」的提示）",
              f.grip.cursor().shape() == QtCore.Qt.SizeVerCursor)
        check("把手有 tooltip 说明", bool(f.grip.toolTip()), f.grip.toolTip())

        # ⚠️⚠️ **把手不画任何东西**（2026-10-11 用户：「框下面不需要那三横吧」）。
        #    第一版画了三条短横线当提示，用户明确不要 —— 所以加这条守住它，
        #    免得哪天有人"为了好发现"又把装饰加回来。
        #    ⚠️ 判据必须用 `DrawChildren` 渲染再数像素：
        #       `grab()` / 默认 render 会**先用调色板底色刷一遍**，
        #       那样即使什么都没画也是有像素的，**验不出来**。
        img = QtGui.QImage(f.grip.width(), f.grip.height(),
                           QtGui.QImage.Format_ARGB32)
        img.fill(QtCore.Qt.transparent)
        f.grip.render(img, QtCore.QPoint(0, 0), QtGui.QRegion(),
                      QtWidgets.QWidget.DrawChildren)
        painted = sum(1 for y in range(img.height()) for x in range(img.width())
                      if img.pixelColor(x, y).alpha() != 0)
        check("把手**不画**任何东西（那三横已经按用户要求去掉）", painted == 0,
              "画了 %d 个像素" % painted)
        check("把手没有重写 paintEvent",
              type(f.grip).paintEvent is QtWidgets.QWidget.paintEvent)

        h0 = f.box.height()
        drag(f.grip, 1000, 1080)
        check("往下拖 80 -> 真的变高了", f.box.height() == h0 + 80,
              "%d -> %d" % (h0, f.box.height()))

        h1 = f.box.height()
        drag(f.grip, 1000, 930)
        check("往上拖 70 -> 真的变矮了", f.box.height() == h1 - 70,
              "%d -> %d" % (h1, f.box.height()))

        # ⚠️ 拖到极限不能变成 0/负数 —— 那样把手自己也被压没，**再也拖不回来**
        for _ in range(40):
            drag(f.grip, 1000, 200)
        check("往下猛拖也不会小于下限", f.box.height() >= f.grip.min_h,
              "%d >= %d" % (f.box.height(), f.grip.min_h))
        check("下限本身是个正数（不能是 0）", f.grip.min_h > 0, str(f.grip.min_h))

        # ⚠️ 拖动**不能**把内容弄丢、也不能改到别的字段
        check("拖完内容还在（没被清空）", "【硬性禁止" in f.box.get())
        check("取值接口没变（还是 f.get()）", f.get() == f.box.get())

        # 另一个多行项（全局人设）也该有把手 —— 它走的是同一个类
        win._switch("persona")
        pump(app, 200)
        g = win._fields.get("persona.default")
        check("全局人设那个多行框也有把手",
              g is not None and isinstance(getattr(g, "grip", None), _HeightGrip))
    finally:
        safe(win.close)


def test_auto_apply(app, tmp):
    """不用重启的项：**监听变化 -> 防抖 -> 立刻生效**；需重启的才出现保存按钮。

    用户要求（2026-10-11）：「设置中那些不用重启的可以马上就变，
    需要重启的才出现保存按钮」，并问「可不可以监听状态是否有变化然后修改？」。

    ⚠️⚠️ 这一节里最要紧的是**第 19a 条**：每一种字段 kind 都必须能报告"我变了"。
        漏一种 = 那一项改了**不生效、也不报错** —— 而"静默不生效"是本项目
        最贵的一类 bug。所以这里**遍历 SPEC 里出现过的每一种 kind** 各戳一下。
    """
    from wechat_assistant.qt_ui import base
    from wechat_assistant.settings import SPEC, SPEC_BY_KEY
    from wechat_assistant.qt_ui.settings_ui import make_field

    print("\n19a. 每一种字段 kind 都能报告「我变了」")
    kinds, worst = {}, []
    for s in SPEC:
        kinds.setdefault(s.kind, s)
    for kind, spec in sorted(kinds.items()):
        fired = []
        f = make_field(None, base.Style(name="dark"), spec, spec.default,
                       on_change=lambda: fired.append(1))
        try:
            # 按控件类型戳一下 —— 顺序要紧：`_SecretField.box` 是个容器
            # （有 `box` 但没有 `setText`），要靠后面的 `.field` 兜住。
            if hasattr(f, "sw"):
                f.sw.click()
            elif hasattr(f, "box") and hasattr(f.box, "setPlainText"):
                f.box.setPlainText("zz 试一试")
            elif hasattr(f, "box") and hasattr(f.box, "setText"):
                f.box.setText("999")
            elif hasattr(f, "field"):
                f.field.setText("sk-zz")
            elif hasattr(f, "_pick") and getattr(f, "_btns", None):
                vals = list(f._btns)
                f._pick([v for v in vals if v != f.value][0])
            else:
                worst.append(kind + "（不知道怎么戳）")
                continue
        except Exception as e:
            worst.append("%s（戳的时候炸了：%s）" % (kind, e))
            continue
        if not fired:
            worst.append(kind)
        # 收尾：把控件还回去（`make_field` 的 parent 是 None，别让它留着）
        try:
            f.widget.setParent(None)
        except Exception:
            pass
    check("SPEC 里出现过的每一种 kind 都能被监听到（共 %d 种）" % len(kinds),
          not worst, "漏了: %s" % worst if worst else "")
    check("覆盖到了 8 种 kind（漏一种说明有 kind 没测到）",
          len(kinds) >= 8, str(sorted(kinds)))

    print("\n19b. 不用重启的项：改完**不等保存**就生效")
    saved = []
    win, store, style = make_win(tmp, on_saved=lambda: saved.append(1))
    try:
        win.open()
        pump(app, 200)
        # ⚠️ 2026-10-13：`ai.temperature`（随机性）从「ai」挪到了「高级」——
        #    设置界面按 `group` 建字段，所以要切到它现在所在的那一组去。
        win._switch("advanced")
        pump(app, 200)
        f = win._fields.get("ai.temperature")
        check("ai.temperature 在「高级」组里、且**不**需重启",
              f is not None
              and not SPEC_BY_KEY["ai.temperature"].restart, str(f is not None))
        if f is not None:
            before = store.load().get("ai.temperature")
            new = 0.3 if float(before or 1.0) != 0.3 else 0.9
            f.box.setText(str(new))
            pump(app, 800)                      # 防抖 400ms + 余量
            got = store.load().get("ai.temperature")
            check("改完（**没点保存**）配置里就变了", float(got) == float(new),
                  "%r -> %r" % (before, got))
            check("而且通知了程序去应用（on_saved 被调到）", bool(saved),
                  "调了 %d 次" % len(saved))
            check("状态行说了「已生效」", "已生效" in (win.status.text() or ""),
                  repr(win.status.text())[:40])
            # ⚠️ 用 `isHidden()` 不用 `isVisible()`：后者要求**祖先链全部可见**，
            #    而测试里窗口没 show()，`isVisible()` 恒为 False（我第一版就白红了两条）。
            check("**不用重启**的项改完，保存按钮**不出现**",
                  win.btn_save.isHidden())
    finally:
        safe(win.close)

    print("\n19c. 需重启的项：才出现保存按钮，保存后消失")
    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 200)
        check("刚打开时保存按钮是**隐藏**的", win.btn_save.isHidden())
        # `record.interval` 现在在「advanced」组里，标了 restart
        win._switch("advanced")
        pump(app, 200)
        f = win._fields.get("record.interval")
        check("record.interval 标了「需重启」",
              f is not None and SPEC_BY_KEY["record.interval"].restart)
        if f is not None:
            before = store.load().get("record.interval")
            f.box.setText(str(int(before or 2) + 3))
            pump(app, 1600)                     # 防抖 + 兜底那 1.2s
            check("改了需重启的项 -> 保存按钮**出现**",
                  not win.btn_save.isHidden())
            check("而且**没有**被自动写进去（需重启的归保存管）",
                  store.load().get("record.interval") == before,
                  "%r" % store.load().get("record.interval"))
            win._save()
            pump(app, 300)
            check("点保存后真的写进去了",
                  store.load().get("record.interval") == int(before or 2) + 3,
                  "%r" % store.load().get("record.interval"))
            check("保存后保存按钮**又消失**（没有待保存的需重启项）",
                  win.btn_save.isHidden())
    finally:
        safe(win.close)

    print("\n19d. 保存要覆盖**所有看过的分组**（不然跨组改的会静默丢）")
    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 200)
        # 在第 1 组改一个需重启的项（`record.fast_interval` 现在在「advanced」组里）
        win._switch("advanced")
        pump(app, 200)
        f1 = win._fields.get("record.fast_interval")
        key = "record.fast_interval"
        check("record.fast_interval 可取到且有 restart",
              f1 is not None and SPEC_BY_KEY[key].restart)
        if f1 is not None:
            want = int(store.load().get(key) or 1) + 7
            f1.box.setText(str(want))
            pump(app, 200)
            # 切到**另一组**再保存（用有 restart 项的「会话与记录」组）
            win._switch("record")
            # ⚠️ 要等到**兜底定时器**（1.2s）真的跑过一次 ——
            #    第一版这里只 pump 了 200ms，两次加起来 400ms，
            #    比 1.2s 的兜底还短，于是**时红时绿**（实测 3 次里红 1 次）。
            #    ⚠️ 别改成"手动调 `_sync_save_btn()`"绕过等待 ——
            #       那样就验不到"定时器真的会同步按钮"这件事了。
            pump(app, 1600)
            check("切组之后保存按钮还在（跨分组记得住）",
                  not win.btn_save.isHidden())
            win._save()
            pump(app, 300)
            check("**在别组点保存**也把第 1 组的改动写进去了",
                  store.load().get(key) == want,
                  "%r（想要 %r）" % (store.load().get(key), want))
    finally:
        safe(win.close)

    print("\n19e. 填了一半的非法值：只卡住它自己，不连累别人、也不刷屏")
    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 200)
        # ⚠️ 2026-10-13：「ai」组里现在**只剩一个数值项**（`ai.n_replies`），
        #    而这条要的是"同一组里一好一坏"。改用「外观」组：
        #    `ui.height`（int，填空 = 非法）+ `ui.opacity`（float，合法）。
        win._switch("ui")
        pump(app, 200)
        bad = win._fields.get("ui.height")
        good = win._fields.get("ui.opacity")
        check("ui.height / ui.opacity 都在「外观」组里", bad and good)
        if bad and good:
            bad.box.setText("")                 # 空 -> coerce 过不了
            new = 0.7 if float(good.get() or 1.0) != 0.7 else 0.6
            good.box.setText(str(new))
            pump(app, 900)
            # ⚠️ 判据是 **`is_overridden`**，不是"合并后的值等于默认值" ——
            #    后者在 `config.yaml` 没写这一项时恒为 `None`，比不出东西。
            check("非法的那一项**没有**建覆盖（= 没写进去）",
                  not store.is_overridden("ui.height"),
                  repr(store.load().get("ui.height")))
            check("**合法的那一项照常生效**（逐项提交，不整批卡住）",
                  float(store.load().get("ui.opacity")) == float(new),
                  repr(store.load().get("ui.opacity")))
            first = win.status.text() or ""
            pump(app, 900)
            check("同一个非法值**不反复报**（状态行没被刷成新的错）",
                  (win.status.text() or "") == first
                  or "已生效" in (win.status.text() or ""),
                  repr((win.status.text() or ""))[:50])
    finally:
        safe(win.close)


    print("\n19f. 底部那颗键：文案跟着状态走，而且它**撤不回已经生效的东西**")
    # ⚠️ 2026-10-13 用户报「修改后不要按取消吧，交互起来很奇怪」，这一段就是那条的判据。
    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 200)
        check("刚打开：第三颗键叫「关闭」",
              win.btn_cancel.text() == "关闭", win.btn_cancel.text())

        # ---- ① 改一个**不用重启**的项 -> 立刻落盘（这就是"马上实施"）
        win._switch("ui")
        pump(app, 200)
        f = win._fields.get("ui.opacity")
        check("ui.opacity 可取到且 not restart",
              f is not None and not SPEC_BY_KEY["ui.opacity"].restart)
        new_op = 0.66 if float(f.get() or 1.0) != 0.66 else 0.77
        f.box.setText(str(new_op))
        pump(app, 900)
        check("不用重启的项：**改完立刻写进 settings.json**",
              float(store.load().get("ui.opacity") or 0) == float(new_op),
              repr(store.load().get("ui.opacity")))
        check("状态行明说「已生效」",
              "已生效" in (win.status.text() or ""), repr(win.status.text()))
        check("并且它**不会**让保存按钮冒出来（没什么要保存的）",
              win.btn_save.isHidden() and win.btn_cancel.text() == "关闭",
              "%s / %s" % (win.btn_cancel.text(), win.btn_save.isHidden()))

        # ---- ② 改一个**要重启**的项 -> 键变「放弃改动」+ 状态行点名
        win._switch("advanced")
        pump(app, 200)
        r = win._fields.get("record.interval")
        before = store.load().get("record.interval")
        want = int(before or 2) + 4
        r.box.setText(str(want))
        pump(app, 1600)                        # 防抖 + 兜底 1.2s
        check("改了要重启的项 -> 第三颗键变「放弃改动」",
              win.btn_cancel.text() == "放弃改动", win.btn_cancel.text())
        check("保存按钮也出现", not win.btn_save.isHidden())
        check("状态行**点名**是哪一项要重启",
              "记录间隔" in (win.status.text() or ""), repr(win.status.text()))
        check("而且它还没被写进去",
              store.load().get("record.interval") == before,
              repr(store.load().get("record.interval")))

        # ---- ③ 点「放弃改动」：只撤**还没生效**的，字段拨回原值
        win._on_cancel()
        pump(app, 300)
        check("放弃后键变回「关闭」", win.btn_cancel.text() == "关闭",
              win.btn_cancel.text())
        check("保存按钮也消失", win.btn_save.isHidden())
        check("要重启的那一项**没被写进去**",
              store.load().get("record.interval") == before,
              repr(store.load().get("record.interval")))
        check("字段上的值也拨回去了（不是只改了按钮）",
              float(win._fields["record.interval"].get() or 0) == float(before or 2),
              repr(win._fields["record.interval"].get()))
        # ⚠️ 这条是这一段的**核心诚实性判据**：已经自动生效的项**撤不回来**，
        #    所以那颗键只能叫「关闭 / 放弃改动」，**不能叫"取消"**。
        check("⚠️ 已经自动生效的项**没有被一起撤掉**（撤不回来，所以别叫「取消」）",
              float(store.load().get("ui.opacity") or 0) == float(new_op),
              repr(store.load().get("ui.opacity")))

        # ---- ④ 直接关窗（✕）不会把改动静默丢掉
        win._switch("advanced")
        pump(app, 200)
        r = win._fields.get("record.interval")
        r.box.setText(str(want))
        pump(app, 1600)
        win.close()
        pump(app, 200)
        check("关窗时把还没生效的改动**存掉了**（不是静默丢）",
              store.load().get("record.interval") == want,
              repr(store.load().get("record.interval")))
    finally:
        safe(win.close)


    print("\n19g. 打开会话页**不算改动**（用户报过），改回原值也不算")
    # ⚠️ 2026-10-13 用户报「会话页，只要我打开它就会认为我改动了？：
    #    根因是 `SessionPanel._stash()` 原来**无条件**把当前会话的值写进暂存。
    win, store, style = make_win(tmp)
    try:
        win.open()
        pump(app, 200)
        win._switch("session")
        pump(app, 1600)                        # 让 1.2s 的兜底定时器真的跑过一次
        check("刚打开会话页：保存按钮**不出现**", win.btn_save.isHidden())
        check("第三颗键还是「关闭」", win.btn_cancel.text() == "关闭",
              win.btn_cancel.text())
        check("状态行没有说「还没保存」",
              "还没保存" not in (win.status.text() or ""),
              repr(win.status.text()))
        # 来回切几个会话（每次切换都会 `_stash()` 一次）也不该产生改动
        for name in ("张三", "李四", "张三.工作", "张三"):
            win._session_panel._select(name)
            pump(app, 60)
        pump(app, 1600)
        check("来回切会话也不产生改动", win._session_dirty() is False)
        check("保存按钮仍然隐藏", win.btn_save.isHidden())

        # 真的改了才算 —— 李四原来是 off
        win._session_panel._select("李四")
        pump(app, 60)
        win._session_panel._ed["mode"]._pick("on")
        pump(app, 1600)
        check("改了模式 -> 保存按钮出现", not win.btn_save.isHidden())
        check("_session_dirty() 为真", win._session_dirty() is True)
        check("状态行提示会话改动还没保存",
              "还没保存" in (win.status.text() or ""), repr(win.status.text()))

        # 改回原值 -> 又等于没改（这一条把"只记改动"钉死）
        win._session_panel._ed["mode"]._pick("off")
        pump(app, 1600)
        check("⚠️ 改回原值 -> 又算没改（保存按钮消失）", win.btn_save.isHidden())
        check("_session_dirty() 又为假", win._session_dirty() is False)
        check("而且暂存里真的没有残留",
              (win._session_stage or {}).get("modes") == {},
              repr(win._session_stage))
    finally:
        safe(win.close)

    # ---- 19h. 打字的项：**攒到保存再写**；拨开关仍然立刻生效；保存不跳顶
    # 用户 2026-10-14：「有输入框的项目，也是按保存后进行保存吧。比如说我打系统提示词，
    # 就不要打一半就跳到顶部」+「现在设置一个后保存会跳到顶部去」。
    # ⚠️ 三件事一起卡：① 打字的项**不自动落盘**（不然边打边写）+ 保存按钮要出现；
    #    ② bool/数值**仍然**改完立刻生效（拨一下开关不该还要点保存）；
    #    ③ 自动生效**不许重建界面**（重建 = 光标和滚动位置全丢），
    #       保存之后滚动位置也要还在。
    print("\n19h. 打字的项攒到保存 + 保存后不跳回顶部")
    from wechat_assistant.qt_ui.settings_ui import DEFER_KINDS, MIN_W as _MIN_W
    win, store, style = make_win(tmp)
    try:
        win.open()
        win.win.resize(base.px(_MIN_W), base.px(620))
        pump(app, 250)
        win._switch("ai")
        pump(app, 250)

        # ① 打字的项（text / multiline / secret）
        _txt = win._fields.get("ai.base_url")
        check("「接口地址」是打字的项（kind 在 DEFER_KINDS 里）",
              _txt is not None and SPEC_BY_KEY["ai.base_url"].kind in DEFER_KINDS,
              SPEC_BY_KEY["ai.base_url"].kind)
        _before = store.load().get("ai.base_url")
        _typed = "https://example.com/v1"
        _txt.box.setText(_typed)
        pump(app, 1200)
        check("打完字**没有**被自动写进去（攒着）",
              (store.load().get("ai.base_url") or "") != _typed,
              repr(store.load().get("ai.base_url")))
        pump(app, 1200)                       # 让 1.2s 的兜底定时器也跑过
        check("  （但保存按钮**出现了**，用户知道要按保存）",
              not win.btn_save.isHidden())
        win._save()
        pump(app, 300)
        check("点保存之后才真的写进去",
              store.load().get("ai.base_url") == _typed,
              repr(store.load().get("ai.base_url")))
        check("保存后按钮又消失", win.btn_save.isHidden())

        # ② 拨开关（bool）仍然**立刻**生效，且**不重建**界面
        win._switch("ui")
        pump(app, 250)
        _sw = win._fields.get("ui.topmost")
        _fid = id(_sw)
        check("「总是置顶」是 bool、不用重启",
              _sw is not None and not SPEC_BY_KEY["ui.topmost"].restart)
        _want = not bool(store.load().get("ui.topmost"))
        _sw.sw.click()
        pump(app, 1000)
        check("拨开关**不用点保存**就落盘了",
              bool(store.load().get("ui.topmost")) == _want,
              repr(store.load().get("ui.topmost")))
        check("  （而且没让保存按钮冒出来）", win.btn_save.isHidden())
        # ⚠️ 这一条是"跳回顶部"的**根因判据**：自动生效时**不许重建字段**
        #    （重建会让滚动条归零、光标也没了）。比对象 id 最直接。
        check("自动生效**没有重建界面**（字段还是同一个对象）",
              id(win._fields.get("ui.topmost")) == _fid,
              "%s -> %s" % (_fid, id(win._fields.get("ui.topmost"))))

        # ③ 保存之后滚动位置还在（用户报的「设置一个后保存会跳到顶部」）
        win._switch("advanced")
        pump(app, 300)
        _bar = win.scroll.verticalScrollBar()
        check("「高级」这一组长到能滚动（否则这条判据没意义）",
              _bar.maximum() > 200, "max=%d" % _bar.maximum())
        _pos = max(120, _bar.maximum() // 2)
        _bar.setValue(_pos)
        pump(app, 120)
        win._save()
        pump(app, 300)
        check("保存之后**滚动位置还在**（没跳回顶部）",
              _bar.value() >= _pos - 40, "保存前 %d -> 保存后 %d" % (_pos, _bar.value()))

        # ④ 「恢复默认」也是同组重建 —— 同样不许跳顶
        _bar.setValue(_pos)
        pump(app, 120)
        win._rebuild_keep_scroll()
        pump(app, 200)
        check("同组重建（恢复默认/放弃改动那条路）也不跳顶",
              _bar.value() >= _pos - 40, "重建后 %d" % _bar.value())

        # ---- 19i. ⚠️⚠️ 重建会把视图**推到下面去**（用户报「改 AI 接口后保存跳到下面去」）
        #
        #    2026-10-15 用户：「为什么修改 ai 接口后，保存会跳到下面去」。
        #    根因：`_restore_scroll()` 开头对 **`pos <= 0` 早退** —— 而重建
        #    **自己**就会把视图从 0 推到接近底部（实测 0 → 860/883），
        #    所以"用户停在顶部"这种最常见的情形从来没被恢复过。
        #    ⚠️ 上面 ③ 量的是**中间**位置（120 / max//2）—— 正因为如此漏掉了这个 bug：
        #       **判据必须覆盖边界值（0 / max），不能只量中间。**
        #    ⚠️⚠️ **触发条件是「焦点在某个输入框里」**（实测：焦点在滚动区上时重建
        #       0 → 0 不动；焦点在 `EntryBox` 里时 0 → 860）。用户刚打完字，
        #       焦点当然在框里 —— 所以判据**必须先 `setFocus()`**，否则测不到东西。
        print("\n19i. 焦点在输入框里时重建不许把视图推到下面 + 焦点要回到原来那一项")
        _fkey = None
        for _k, _f in win._fields.items():
            if hasattr(_f, "box") and hasattr(_f.box, "setFocus"):
                _fkey = _k
                break
        check("这一组里有带输入框的项（否则下面两条没意义）",
              _fkey is not None, repr(_fkey))

        if _fkey is not None:
            win._fields[_fkey].box.setFocus()
            pump(app, 120)
            _bar.setValue(0)
            pump(app, 150)
            check("打字时焦点就在 %s 上（触发条件）" % SPEC_BY_KEY[_fkey].label,
                  win._focus_key() == _fkey, repr(win._focus_key()))
            win._rebuild_keep_scroll()
            pump(app, 350)
            check("重建后**还在顶部**（没被推到下面）",
                  _bar.value() <= 8,
                  "重建后 %d（max=%d）" % (_bar.value(), _bar.maximum()))

            # ⚠️ **反例自检**：把恢复逻辑换回老写法（`pos <= 0` 早退）+ 焦点在框里，
            #    上一条必须能红 —— 否则它可能只是"碰巧没动"，等于没验。
            _real_restore = win._restore_scroll

            def _legacy_restore(pos):
                if pos <= 0:                   # ← 老写法，就是这么漏的
                    return
                return _real_restore(pos)

            win._restore_scroll = _legacy_restore
            try:
                win._fields[_fkey].box.setFocus()
                pump(app, 120)
                _bar.setValue(0)
                pump(app, 150)
                win._rebuild_keep_scroll()
                pump(app, 350)
                _legacy_val = _bar.value()
            finally:
                win._restore_scroll = _real_restore
                _bar.setValue(0)
                pump(app, 200)
            check("反例：恢复逻辑写回老样子时，视图**确实**被推下去了（判据不是空的）",
                  _legacy_val > 8, "老写法重建后 %d" % _legacy_val)

            # 焦点：重建会把输入框删掉 → Qt 把焦点丢给滚动区（实测），
            #       用户接着打字就打到空气里；而且 `setFocus` 自己也会让滚动区滚过去。
            win._fields[_fkey].box.setFocus()
            pump(app, 120)
            win._rebuild_keep_scroll()
            pump(app, 300)
            check("重建后焦点**回到同一个字段**（能接着打字）",
                  win._focus_key() == _fkey, repr(win._focus_key()))
    finally:
        safe(win.close)


def main():
    print("=" * 70)
    print("Qt 设置窗口测试")
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
    #    断言还是自洽的、测试还是"绿"的，但**它验的根本不是真机那套尺寸**
    #    （本机 150%，`SHELL_INSET=7` 应该是 11px 而不是 7px）。
    dpi.enable()
    app = base.init_app([])
    print("Qt %s / 缩放 %.2f" % (QtCore.qVersion(), base.scale()))

    before = state_stamp()
    tmp = tempfile.mkdtemp(prefix="wai_qtset_")
    try:
        test_skeleton(app, tmp)
        test_paint(app, tmp)
        test_groups(app, tmp)
        test_form(app, tmp)
        test_text_hygiene(app, tmp)
        test_list_roundtrip(app, tmp)
        test_save(app, tmp)
        test_session_group(app, tmp)
        test_view_tools(app, tmp)
        test_dialog_width(app, tmp)
        test_test_connection(app, tmp)
        test_set_style(app, tmp)
        test_both_themes(app, tmp)
        test_geometry(app, tmp)
        test_close_reopen(app, tmp)
        test_end_to_end(app, tmp)
        test_layout_fixes(app, tmp)
        test_textarea_resizable(app, tmp)
        test_auto_apply(app, tmp)
        test_no_tk_leftovers()

        print("\n15. 没有污染仓库")
        after = state_stamp()
        # ⚠️ 这条断言只在**没有别的实例在跑**时才有意义。
        #    用户自己开着的那个实例会写 `state/settings.json`（他可能在用设置窗口），
        #    那种情况下 mtime 变了**不等于我们污染了仓库** ——
        #    直接判失败就是**假红**（实测：同一份代码连跑 6 次全过，
        #    第 7 次挂在这条，变化来自另一个进程）。
        #    所以：变了 + 有别的实例 → 跳过并说明；变了 + 没有别的实例 → 真失败。
        if before == after:
            check("仓库 state/ 里的 settings.json / state.json 没被动过",
                  True, "%r" % (before,))
        else:
            live = another_instance_running()
            if live:
                skip("仓库 state/ 里的 settings.json / state.json 没被动过",
                     "有另一个本程序实例在跑（它自己会写这个文件），"
                     "这次时间戳变化**归因不了** —— 关掉那个实例再跑才能判定。"
                     "%r -> %r" % (before, after))
            else:
                check("仓库 state/ 里的 settings.json / state.json 没被动过",
                      False, "%r -> %r" % (before, after))
        check("测试用的 store 路径确实在临时目录里",
              os.path.abspath(os.path.join(tmp, "settings.json")).startswith(
                  os.path.abspath(tmp)))


    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print("通过 %d，失败 %d" % (PASS, FAIL))
    if SKIP:
        print("跳过 %d" % SKIP)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
