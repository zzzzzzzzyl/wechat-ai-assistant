# -*- coding: utf-8 -*-
"""贴边跟随的测试。

为什么值得测
------------
跟随器每一拍都在动**窗口位置**，出错的后果是"窗口乱跑"或者"卡住不动"，
而且它跟真实微信窗口耦合 —— 没有测试的话只能靠开微信手动试，很慢。

这里把「读微信窗口」那几个函数换成假的，于是**不需要微信在运行**也能验证：
  · 贴边方向的选择（右边放不下就走左边，两边都放不下就不动）
  · 垂直方向不会跑出屏幕
  · **值没变就不动窗口**（每一帧都调 geometry 会让界面抖，实测反馈过）
  · 用户自己拖过窗口之后，不再跟他抢位置
  · 微信最小化跟着收起、恢复跟着出现

另外还钉住一条红线：跟随器**只能读**微信窗口，不能动它。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant import follow  # noqa: E402

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


# ============================================================ 假的界面

class FakeStyle:
    def px(self, v):
        return int(v)


class FakeUI:
    """Qt 版悬浮窗的最小替身（**没有 `.root`**）。

    ⚠️ 这里原来有一个 `FakeRoot`（`winfo_*` / `after`），因为 `follow` 曾经
        两版通用、靠 `getattr(ui, "root", None)` 分流。2026-09-26 Tk 版删掉后
        `follow` 只剩 Qt 一条路，所以这个替身也照着 Qt 的形状来 ——
        **别再加回 `root`**：`follow` 已经不认识它了。
    """

    def __init__(self):
        self._x, self._y = 0, 0
        self._w, self._h = 340, 600
        self.style = FakeStyle()
        self.docked = []
        self.moves = []
        self.cleared = 0
        self.hidden = 0
        self.shown = 0
        self.visible = True
        self._user_moved = False
        self._geo_calls = 0

    # ---- 几何（Qt 形状：x()/y()/width()/height()）
    def x(self):
        self._geo_calls += 1
        return self._x

    def y(self):
        return self._y

    def width(self):
        return self._w

    def height(self):
        return self._h

    def set_docked(self, x, y, w, h):
        self.docked.append((x, y, w, h))
        self._x, self._y = x, y
        self._w, self._h = w, h

    def move_only(self, x, y):
        """固定悬浮模式用的"只挪位置"（对应真面板的 `Panel.move_only`）。

        ⚠️ 关键是它**不设 `_user_moved`** —— 真面板上写成 `move_clamped`
            就会变成"每一拍都重新定基准"，窗口永远不动（跟随静默失效）。
        """
        self.moves.append((x, y))
        self._x, self._y = x, y

    def clear_docked(self):
        self.cleared += 1

    def hide(self):
        self.hidden += 1
        self.visible = False

    def show(self):
        self.shown += 1
        self.visible = True

    def is_visible(self):
        return self.visible


_PATCHED = ("find_wechat_window", "window_box", "is_minimized", "work_area",
            "ui_native_handle")
# 导入时的原函数，用来在用例之间**还原**（见 `reset_patches`）
_PRISTINE = {name: getattr(follow, name) for name in _PATCHED}


def reset_patches():
    """把 `follow` 里被打桩的函数还原成原版。

    ⚠️ **必须有这一步**：`make_follower` 是往**模块属性**上打桩的，
        而用例之间是顺序执行的 —— 不清的话上一个用例的桩会漏到下一个用例。
        实测踩过：`test_zorder` 把 `ui_native_handle` 打成恒返回 99999，
        然后 `test_qt_seam` 里"拿不到句柄要返回 0"那条就拿到了 99999 而假红。
    """
    for name, fn in _PRISTINE.items():
        setattr(follow, name, fn)


def make_follower(cfg_extra=None, rect=(100, 100, 800, 700), work=(0, 0, 1920, 1040),
                  minimized=False):
    """装一个跟随器，把"读微信窗口"那几个函数换成假的。"""
    reset_patches()
    ui = FakeUI()
    cfg = {
        "ui.attach": "dock",
        "ui.dock_side": "auto",
        "ui.width": 340,
        "ui.height": 0,
        "ui.follow_minimize": True,
        "wechat.proc_name": "Weixin.exe",
        "wechat.window_class_contains": "QWindowIcon",
    }
    cfg.update(cfg_extra or {})

    class Cfg:
        def get(self, k, d=None):
            return cfg.get(k, d)

    follow.find_wechat_window = lambda c: 12345
    follow.window_box = lambda hwnd: rect
    follow.is_minimized = lambda hwnd: minimized
    follow.work_area = lambda: work
    # 假界面没有真的 HWND —— 层级那一步直接当"拿到了句柄"
    follow.ui_native_handle = lambda ui: 99999

    f = follow.WindowFollower(ui, Cfg(), logger=lambda m: None)
    return f, ui


# ============================================================ 1. 纯逻辑：算位置

def test_compute_dock():
    print("\n1. 贴边位置计算（纯逻辑，不碰窗口）")
    work = (0, 0, 1920, 1040)

    # 右边放得下
    got = follow.compute_dock(340, 600, (100, 100, 800, 700), work, "auto", 8)
    check("右边放得下就贴右边", got == (808, 100), repr(got))

    # 右边放不下 -> 走左边
    got = follow.compute_dock(340, 600, (1500, 100, 1900, 700), work, "auto", 8)
    check("右边放不下时走左边", got == (1152, 100), repr(got))

    # 两边都放不下
    got = follow.compute_dock(340, 600, (0, 0, 1920, 1040), work, "auto", 8)
    check("两边都放不下 -> None（不硬挤）", got is None, repr(got))

    # 固定右侧时右边放不下也不去左边
    got = follow.compute_dock(340, 600, (1500, 100, 1900, 700), work, "right", 8)
    check("固定右侧时不改去左边", got is None, repr(got))

    # 垂直方向：**只夹上边界**，底部**不再夹**（2026-10-11 用户改的决定）。
    #
    # ⚠️⚠️ 原来这里是 `min(wt, gb - panel_h)`，会在"面板底边顶到屏幕底"时
    #     把面板**钉住不再往下**。用户实测报「跟随微信时微信拖到下面它会被拦着」，
    #     并明确选了 **A：跟着一起越出屏幕（和微信完全同步）**。
    #     所以现在只保证"不跑到可用区顶上"，底部允许越出。
    #     ⚠️ 已知代价（用户接受）：**面板底部的复制/生成按钮会跟着藏到屏幕外**。
    #     ⚠️ 别把这条改回去 —— 那正是用户要的那个行为。
    got = follow.compute_dock(340, 600, (100, 900, 800, 1400), work, "auto", 8)
    check("微信往下拖 -> 面板跟着下去（不再被拦）", got == (808, 900), repr(got))
    check("  底边允许越出可用区（按钮会藏到屏幕外，已知并接受）",
          got is not None and got[1] + 600 > 1040, repr(got))
    got = follow.compute_dock(340, 600, (100, -50, 800, 500), work, "auto", 8)
    check("太高时向下夹住（只夹上边界）", got == (808, 0), repr(got))

    # 高度比屏幕还大时不应崩
    got = follow.compute_dock(340, 2000, (100, 100, 800, 700), work, "auto", 8)
    check("面板比屏幕高也不崩", got is not None, repr(got))


# ============================================================ 2. 跟随行为

def test_dock_gap():
    print("\n1b. 两侧各有多少空间可用（纯逻辑）")
    work = (0, 0, 1920, 1040)

    avail, side = follow.dock_gap((100, 100, 800, 700), work, "auto", 8)
    check("auto 取空间更大的一边", (avail, side) == (1112, "right"),
          "%s / %s" % (avail, side))

    avail, side = follow.dock_gap((100, 100, 800, 700), work, "left", 8)
    check("指定左边就报左边的空间", (avail, side) == (92, "left"), "%s / %s" % (avail, side))

    avail, side = follow.dock_gap((100, 100, 1900, 700), work, "auto", 8)
    check("右边挤不下时报左边", (avail, side) == (92, "left"), "%s / %s" % (avail, side))

    avail, _ = follow.dock_gap((0, 0, 1920, 1040), work, "auto", 8)
    check("两边都被占满时空间为 0", avail == 0, str(avail))

    # 用户实测过的真实数字：微信宽 1593、屏宽 2240
    avail, side = follow.dock_gap((358, 465, 1951, 1444), (0, 0, 2240, 1400), "auto", 8)
    check("真实场景下仍有空间（不能直接放弃跟随）", avail > 200, "%s / %s" % (avail, side))


def test_shrink_to_fit():
    """空间不够时应该**缩窄面板塞进去**，而不是干脆不跟随。

    实测踩过：微信宽 1593px + 屏宽 2240px，面板要 510px，两侧各剩 289/358px,
    旧逻辑直接放弃跟随 —— 用户看到的就是"完全不跟随"。
    """
    print("\n1c. 空间不够时自动缩窄")
    f, ui = make_follower(rect=(300, 100, 700, 700), work=(0, 0, 1000, 1040))
    f._apply()
    check("仍然跟随了（没放弃）", len(ui.docked) == 1, repr(ui.docked))
    if ui.docked:
        x, y, w, h = ui.docked[0]
        check("宽度被缩到可用宽度以内", w <= 292, "w=%d" % w)
        check("仍然贴边（没跑到屏幕外）", x + w <= 1000, "x+w=%d" % (x + w))


def test_no_room_at_all():
    print("\n1d. 连最小宽度都放不下时才放弃，并且说明原因")
    # 微信几乎占满整个桌面宽度 -> 两边都不足 min_width
    f, ui = make_follower(rect=(100, 100, 900, 700), work=(0, 0, 1000, 1040))
    f._apply()
    check("放弃跟随、不动窗口", len(ui.docked) == 0, repr(ui.docked))

    msgs = []
    ui.post_status = lambda t: msgs.append(t)
    f2, ui2 = make_follower(rect=(100, 100, 900, 700), work=(0, 0, 1000, 1040))
    f2.ui.post_status = lambda t: msgs.append(t)
    f2._apply()
    f2._apply()          # 再跑一拍：不该重复提示
    check("提示了用户原因", len(msgs) == 1, repr(msgs))
    check("提示内容说明了怎么办", msgs and "拖窄" in msgs[0], repr(msgs))


def test_deadband():
    """位置要**跟手**（差 1px 也跟），尺寸要**抗震**（给 2px 死区）。

    两者代价不同：
      · 改位置 -> `geometry("+x+y")`，不触发子树重排，很便宜
      · 改尺寸 -> 整棵控件树重排（卡片重新换行、圆角重画），很贵
    所以位置不能加死区（加了就"不跟手"），尺寸要加（微信 rect 有 1~2px 抖动）。
    """
    print("\n4b. 死区：位置跟手、尺寸抗震")
    f, ui = make_follower(rect=(100, 100, 800, 700))
    f._apply()
    n = len(ui.docked)
    check("第一次摆好了", n == 1, "%d" % n)

    # 只挪 1px -> 位置要跟
    follow.window_box = lambda h: (101, 101, 801, 701)
    f._apply()
    check("位置差 1px 也跟随（跟手）", len(ui.docked) == n + 1,
          "调用了 %d 次" % len(ui.docked))

    # 位置不动、高度只差 1px -> 尺寸死区吃掉（不重排）
    n = len(ui.docked)
    follow.window_box = lambda h: (101, 101, 801, 702)
    f._apply()
    check("高度只差 1px 不重排（死区生效）", len(ui.docked) == n,
          "调用了 %d 次" % len(ui.docked))

    # 高度差 6px -> 要跟
    follow.window_box = lambda h: (101, 101, 801, 707)
    f._apply()
    check("高度差得多仍然跟随", len(ui.docked) > n, "调用了 %d 次" % len(ui.docked))


def test_self_heal():
    """跟随器要能自愈：窗口被外部改过之后，不能永远不再纠正。

    实测踩过：**程序在微信最小化时启动**，之后即便把微信调出来，
    面板也一直停在启动那一瞬间的位置和高度（高 650，而微信是 896）。
    根因是 `_set()` 的去重（值没变就不动窗口）跟"窗口被别的东西改过"打架 ——
    `_last` 还记着旧值，于是永远不再下发。
    """
    print("\n4c. 自愈：窗口几何被外部改过之后能自己纠正")
    f, ui = make_follower(rect=(100, 100, 800, 700))
    f._apply()
    n = len(ui.docked)
    check("先摆好一次", n == 1, "%d" % n)

    # 模拟窗口被外部改过（主题重建 / 折叠展开 / 用户拖过）
    ui._x, ui._y = 999, 999
    f._verify()
    check("没到核对周期时不动作", len(ui.docked) == n, "%d" % len(ui.docked))
    check("记录还在（还没发现）", f._last is not None)

    # 到了核对周期：发现实际值跟记录不符 -> 清掉记录
    f._verify_count = f.VERIFY_EVERY - 1
    f._verify()
    check("到周期后发现不符，清掉记录", f._last is None)

    # 下一拍自然会重新摆
    f._apply()
    check("下一拍重新下发目标", len(ui.docked) > n, "%d" % len(ui.docked))
    check("位置回到微信旁边（不是 999）",
          ui.docked[-1][0] != 999 and ui.docked[-1][1] != 999, str(ui.docked[-1]))


def test_dock_right():
    print("\n2. 正常情况：贴到微信右边，高度与微信对齐")
    f, ui = make_follower(rect=(100, 100, 800, 700))
    f._apply()
    check("应用了一次位置", len(ui.docked) == 1, repr(ui.docked))
    if ui.docked:
        x, y, w, h = ui.docked[0]
        check("贴在微信右边", x == 808, str(x))
        check("顶端与微信对齐", y == 100, str(y))
        check("宽度来自配置", w == 340, str(w))
        check("**高度与微信一致**", h == 600, str(h))


def test_no_room():
    print("\n3. 微信占满整个桌面时完全不跟随（保持原样最不意外）")
    f, ui = make_follower(rect=(0, 0, 1920, 1040))
    f._apply()
    # 以前这里会"只对齐高度"，那会把面板拉成整屏那么高、飘在微信上面，很怪。
    # 现在连宽度都缩不到下限 -> 干脆什么都不做。
    check("既不挪位置也不改高度", len(ui.docked) == 0, repr(ui.docked))


def test_dedup():
    print("\n4. 值没变就不动窗口（这是「不闪」的关键）")
    f, ui = make_follower(rect=(100, 100, 800, 700))
    f._apply()
    n1 = len(ui.docked)
    f._apply()
    f._apply()
    check("连续三拍只动了一次窗口", len(ui.docked) == n1 == 1,
          "调用了 %d 次" % len(ui.docked))

    # 微信动了 -> 才跟着动
    follow.window_box = lambda hwnd: (100, 200, 800, 800)
    f._apply()
    check("微信移动后跟着动", len(ui.docked) == 2, "调用了 %d 次" % len(ui.docked))


def test_user_moved():
    """用户自己拖过窗口之后，跟随到底怎么办。

    ⚠️ **这一节在 2026-09-27 被用户推翻过一次，别按老印象改回去。**
       老写法是「用户拖过 -> 这一轮无条件不抢位置（`_apply_height()` 然后 return）」，
       测试当年锁的也是"微信挪了也不改位置"。

       用户实测反馈了两件事，其实是**同一个写法的两个侧面**：
         ① 「拖动后拖动微信窗口，他可能宽度会错误变化」
            —— `_apply_height()` 的 docstring 写的是"只把高度对齐微信"，
               但它内部是 `_panel_size()` + `_set(cur[0], cur[1], w, h)`，
               **把宽度一起写回去了**。于是"位置冻结、宽高还在变"：
               微信一挪，可用空隙变了 → 面板在原地被拉宽/压窄。
         ② 「拖动微信上边框高度，他反而是下边框变化」
            —— 拖微信上边框时微信**上边变、下边不动**，而这里位置被冻住、
               只有高度跟着变 → 面板的下边框往下跑。

       合起来用户看到的就是「跟随失效」。用户选的新规则：
       **拖动只影响你放下的那一下；微信窗口自己一动就恢复贴边跟随。**
    """
    print("\n5. 用户拖过窗口：微信没动就听用户的，微信一动就恢复跟随")

    # ---- ① 微信没动 -> 一个数都别写（位置、宽度、高度全不动）
    f, ui = make_follower(rect=(100, 100, 800, 700))
    f._apply()
    before = len(ui.docked)
    geo_before = (ui._x, ui._y, ui._w, ui._h)
    ui._user_moved = True
    f._apply()
    f._apply()
    check("微信没动时不再改位置", len(ui.docked) == before,
          "%d -> %d" % (before, len(ui.docked)))
    check("微信没动时几何一个数都没变",
          (ui._x, ui._y, ui._w, ui._h) == geo_before,
          "%s -> %s" % (geo_before, (ui._x, ui._y, ui._w, ui._h)))

    # ---- ② 微信挪了 -> **整体重新贴边**（位置和宽度一起变，不是只变宽度）
    # ⚠️ 这一条是冲着用户报的「拖动后拖动微信窗口，他可能宽度会错误变化」去的。
    #    老写法只调 `_apply_height()`，它会把算出来的**宽度**写回去、
    #    而位置是冻住的 —— 于是微信一挪，面板就在原地被拉宽/压窄。
    #    现在恢复跟随，宽度和位置必须**一起**重新算。
    follow.window_box = lambda hwnd: (100, 100, 1600, 700)   # 微信右边挪到 1600
    f._apply()
    check("微信挪位置后恢复跟随", len(ui.docked) > before,
          "%d -> %d" % (before, len(ui.docked)))
    check("恢复后用户那次拖动被清掉（不会下一拍又冻住）",
          ui._user_moved is False, str(ui._user_moved))
    check("贴到了新位置旁边（x 跟着微信右边走）", ui._x == 1608, str(ui._x))
    check("宽度**和位置一起**重算，不是留在原地被压窄",
          ui._w == 312, "w=%d x=%d" % (ui._w, ui._x))

    # ---- ③ 拖微信**上边框**（上边变、下边不动）-> 面板的上边也得跟着走
    # 这正是用户报的「拖上边框，变的却是面板下边框」。
    f2, ui2 = make_follower(rect=(100, 200, 800, 800))
    f2._apply()
    top0 = ui2._y
    ui2._user_moved = True
    f2._apply()                      # 先让跟随器记下"拖完时微信在哪"
    check("记住拖动时的微信矩形后不再跟随", ui2._y == top0, str(ui2._y))
    follow.window_box = lambda hwnd: (100, 120, 800, 800)   # 上边框往上拖 80px
    f2._apply()
    check("拖微信上边框后恢复跟随", ui2._user_moved is False, str(ui2._user_moved))
    check("面板**上边**跟着微信上边走（不是下边自己动）",
          ui2._y == 120, "面板上边 %d -> %d（微信上边 200 -> 120）" % (top0, ui2._y))
    check("高度也跟上了微信（800-120=680）", ui2._h == 680, str(ui2._h))


def test_minimize():
    print("\n6. 微信最小化时跟着收起；恢复时跟着出现")
    f, ui = make_follower(rect=(100, 100, 800, 700))
    follow.is_minimized = lambda hwnd: True
    f._apply()
    check("最小化 -> 面板隐藏", ui.hidden == 1, "hidden=%d" % ui.hidden)

    follow.is_minimized = lambda hwnd: False
    f._apply()
    check("恢复 -> 面板重现", ui.shown == 1, "shown=%d" % ui.shown)

    # 用户手动隐藏的不该被跟随器掀开
    f2, ui2 = make_follower(rect=(100, 100, 800, 700))
    ui2.visible = False
    f2._apply()
    check("用户自己藏起来的不被强行掀开", ui2.shown == 0, "shown=%d" % ui2.shown)


def test_minimize_then_handle_lost():
    """微信最小化期间**句柄失效**，恢复后面板仍要回来（用户报的回归）。

    ⚠️ 这条是 `test_minimize()` 覆盖不到的路径：那个用例里 `window_box()`
    一直返回有效矩形，于是每次都能走进最小化分支。

    真实情况是：微信最小化时窗口可能被重建、或缩到托盘让
    `IsWindowVisible` 变假 → `find_wechat_window()` 返回 0 →
    `window_box()` 返回 None → `_apply()` 在**最小化分支之前**就早退了。
    如果那个早退把 `_hid_by_us` 也清掉，就**再也没人负责把面板显示回来** ——
    症状正是「微信最小化再打开，悬浮窗没跟着出现」。
    """
    print("\n6b. 最小化期间句柄失效，恢复后仍然要显示回来")
    _real_find, _real_box = follow.find_wechat_window, follow.window_box
    f, ui = make_follower(rect=(100, 100, 800, 700))

    # 1) 正常最小化 -> 我们把它藏起来
    follow.is_minimized = lambda hwnd: True
    f._apply()
    check("最小化 -> 面板隐藏", ui.hidden == 1 and not ui.visible,
          "hidden=%d visible=%s" % (ui.hidden, ui.visible))

    # 2) 句柄失效：微信窗口在这一拍读不到（最小化到托盘 / 窗口重建）
    #
    # 这一拍就**立刻**把面板显示回来是对的：句柄读不到时我们已经无法判断微信
    # 到底是最小化还是关了，而「读不到」本身就是「面板不该继续藏着」的理由
    # （用户还要复制刚才的回复）。关键是**别把标记留在那儿不管**。
    follow.is_minimized = lambda hwnd: False
    follow.find_wechat_window = lambda c: 0
    follow.window_box = lambda hwnd: None
    f._apply()
    check(">>> 句柄丢失 -> 面板立刻显示回来", ui.shown == 1 and ui.visible,
          "shown=%d visible=%s" % (ui.shown, ui.visible))
    check("标记被消费掉，不会重复 show", f._hid_by_us is False,
          "_hid_by_us=%s" % f._hid_by_us)

    # 3) 微信恢复：句柄重新找得到，不该再触发一次 show
    follow.find_wechat_window = lambda c: 12345
    follow.window_box = lambda hwnd: (100, 100, 800, 700)
    f._apply()
    check("微信恢复后不再重复 show", ui.shown == 1,
          "shown=%d" % ui.shown)
    check("也不该反过来把面板藏起来", ui.hidden == 1,
          "hidden=%d" % ui.hidden)

    # 4) 用户自己藏的，走同一条早退路径也不该被掀开
    f2, ui2 = make_follower(rect=(100, 100, 800, 700))
    ui2.visible = False
    follow.find_wechat_window = lambda c: 0
    follow.window_box = lambda hwnd: None
    f2._apply()
    follow.find_wechat_window = lambda c: 12345
    follow.window_box = lambda hwnd: (100, 100, 800, 700)
    f2._apply()
    check("用户自己藏的，句柄丢失也不掀开", ui2.shown == 0,
          "shown=%d" % ui2.shown)

    # ⚠️ 上面的假窗口函数是**模块级**替换，会把后面的用例带偏，必须还原
    follow.find_wechat_window = _real_find
    follow.window_box = _real_box


def test_wechat_gone():
    print("\n7. 微信关掉时不要把面板藏起来")
    f, ui = make_follower(rect=(100, 100, 800, 700))
    f._apply()
    follow.find_wechat_window = lambda c: 0
    follow.window_box = lambda hwnd: None
    f._apply()
    check("微信没了也不隐藏面板（否则没有任务栏条目就找不回来）",
          ui.hidden == 0, "hidden=%d" % ui.hidden)
    check("把控制权还给界面（退回按内容自适应）", ui.cleared >= 1, "%d" % ui.cleared)


def test_fixed_height():
    print("\n8. ui.height 填了正数就用固定高度，不跟微信")
    f, ui = make_follower(rect=(100, 100, 800, 700), cfg_extra={"ui.height": 500})
    f._apply()
    check("用配置的高度而不是微信的高度", ui.docked and ui.docked[0][3] == 500,
          repr(ui.docked[0] if ui.docked else None))


def test_attach_off():
    print("\n9. attach 不是 dock 时完全不跟随")
    f, ui = make_follower(cfg_extra={"ui.attach": "off"})
    f._apply()
    check("不设位置", len(ui.docked) == 0, repr(ui.docked))
    check("把控制权还给界面", ui.cleared >= 1)


def test_attach_float():
    """固定悬浮（`ui.attach: float`）：记住你放的相对位置，跟着微信一起平移。

    用户原话（2026-09-27）：「固定悬浮模式就记住你放的那个相对位置，
    以后跟着微信一起移动」。

    ⚠️ 这条跟第 5 节（`dock` 下用户拖过之后怎么处理）是**两种行为**，别合并：
       · `dock`：位置是**算出来的**（贴边），微信一动就把你拉回边上；
       · `float`：位置是**你定的**，只记"相对微信左上角的偏移"，
                 之后按同一偏移平移，**不会被拉回边上**。
    """
    print("\n9b. 固定悬浮：记住你放的相对位置，跟着微信平移（不贴边）")

    # ---- ① 定基准：面板现在在哪儿，就跟微信差多少，以后一直保持这个差
    f, ui = make_follower(rect=(100, 100, 800, 700),
                          cfg_extra={"ui.attach": "float"})
    ui._x, ui._y = 1000, 40              # 用户把它放在这儿
    f._apply()
    check("float 完全不贴边（没走 dock 那条路）", ui.docked == [], repr(ui.docked))
    check("记下了相对微信的偏移", f._float_off == (900, -60), str(f._float_off))
    check("第一拍不动窗口（位置本来就是用户放的那个）",
          ui.moves == [], repr(ui.moves))
    check("交还了高度控制权（高度按内容自适应，不跟微信）",
          ui.cleared >= 1, "%d" % ui.cleared)

    # ---- ② 微信挪了 -> 面板按同一个偏移一起挪（关键：不贴回边上！）
    follow.window_box = lambda hwnd: (300, 300, 1000, 900)
    f._apply()
    check("跟着微信平移了（+200, +200）", ui.moves == [(1200, 240)], repr(ui.moves))
    check("**没有**被贴回微信边上（贴边的话 x 会是 1008）",
          ui._x == 1200, str(ui._x))
    check("偏移保持不变", f._float_off == (900, -60), str(f._float_off))

    # ---- ③ 微信再挪，还是同一个偏移；值没变就不重复下发
    before = len(ui.moves)
    f._apply()
    check("微信没动就不重复下发（去重）", len(ui.moves) == before,
          "%d -> %d" % (before, len(ui.moves)))

    # ---- ④ 用户又拖了一下 = 要改这个相对位置 -> 重定基准
    ui._x, ui._y = 500, 500
    ui._user_moved = True
    follow.window_box = lambda hwnd: (300, 300, 1000, 900)
    f._apply()
    check("拖过之后按新位置重定基准", f._float_off == (200, 200), str(f._float_off))
    check("重定基准后不再擅自挪窗口", ui.moves[-1] == (1200, 240),
          repr(ui.moves[-1]))
    check("用户拖动标记被消费掉了（不会每拍重定基准）",
          ui._user_moved is False, str(ui._user_moved))
    follow.window_box = lambda hwnd: (400, 300, 1100, 900)
    f._apply()
    check("之后再跟着微信走，用的还是新偏移", ui.moves[-1] == (600, 500),
          repr(ui.moves[-1]))

    # ---- ⑤ 切回贴边 -> 状态要清干净（两条路的基准不一样，不能带着走）
    reset_patches()
    f2, ui2 = make_follower(rect=(100, 100, 800, 700),
                            cfg_extra={"ui.attach": "float"})
    ui2._x, ui2._y = 1000, 40
    f2._apply()
    check("float 下攒了偏移", f2._float_off is not None)
    f2.cfg.get = (lambda orig: lambda k, d=None: (
        "dock" if k == "ui.attach" else orig(k, d)))(f2.cfg.get)
    f2._apply()
    check("切到 dock 后偏移被清掉（不会拿 float 的基准去贴边）",
          f2._float_off is None, str(f2._float_off))
    check("切到 dock 后**真的贴边了**", ui2.docked != [], repr(ui2.docked))
    check("贴到了微信右边（x=808）", ui2.docked[-1][0] == 808, repr(ui2.docked[-1]))


def test_use_config():
    """设置窗口保存后会**换一个新的配置对象**，跟随器必须跟着换。

    ⚠️ 这是"改了设置完全没反应"的根因：跟随器每一拍读的是**构造时传进来那个
        对象**，而 `run_watch.on_config_changed` 写的是 `cfg = new_cfg`
        —— 那是换了个新对象，不是原地改旧对象。
        不换的话「设置里把跟随微信从贴边改成固定悬浮」毫无效果，且不报错。
    """
    print("\n9c. 换配置对象：跟随器读的必须是**新**的那份")
    f, ui = make_follower(rect=(100, 100, 800, 700),
                          cfg_extra={"ui.attach": "dock", "ui.dock_side": "auto"})
    f._apply()
    check("起初按 auto 贴到右边", ui.docked[-1][0] == 808, repr(ui.docked[-1]))

    class Cfg2:
        def __init__(self, d):
            self._d = d

        def get(self, k, default=None):
            return self._d.get(k, default)

    f.use_config(Cfg2({"ui.attach": "float", "ui.dock_side": "left",
                       "ui.follow_minimize": False, "ui.follow_interval": 0.02}))
    check("换了 cfg 对象", isinstance(f.cfg, Cfg2))
    check("dock_side 也跟着刷新了（构造时缓存过）", f.side == "left", str(f.side))
    check("follow_minimize 也跟着刷新", f.follow_minimize is False)
    check("interval 也跟着刷新", abs(f.interval - 0.02) < 1e-9, str(f.interval))

    # 换成 float 之后**必须真的按 float 走**（而不是继续贴边）
    ui._x, ui._y = 1000, 40
    _docked_before = len(ui.docked)
    f._apply()
    check("use_config 之后真的切到了固定悬浮（没有再去贴边）",
          len(ui.docked) == _docked_before and f._float_off == (900, -60),
          "docked=%d float_off=%s" % (len(ui.docked), f._float_off))


def test_zorder():
    """层级：微信在前台时把面板插到它正上方。

    实测反馈过两个现象，都是这里的事：
      · "打开微信它就在底层" —— 面板不置顶，微信一升到前面就把它盖住了
      · "我打开其他页面他也弹出来" —— 不能简单设成总在最前，只在微信前台时才抬
    """
    print("\n11. 层级：插到微信正上方，但只在微信是前台时")
    import ctypes

    calls = []
    real = ctypes.windll.user32.SetWindowPos

    def fake(hwnd, insert_after, x, y, cx, cy, flags):
        calls.append((hwnd, insert_after, flags))
        return 1

    ctypes.windll.user32.SetWindowPos = fake
    try:
        # 微信不在前台 -> 不该动层级
        f, ui = make_follower(rect=(100, 100, 800, 700))
        follow.foreground_is = lambda h: False
        f._apply()
        check("微信不在前台时不碰层级", calls == [], repr(calls))

        # 微信在前台 -> 插到它正上方
        follow.foreground_is = lambda h: True
        f._fg_was = False
        f._z_done = False
        f._apply()
        check("微信在前台时抬了一次", len(calls) == 1, repr(calls))
        if calls:
            hwnd_self, insert_after, flags = calls[0]
            check("**动的是自己的窗口**（不是微信）", insert_after != hwnd_self,
                  "self=%s ref=%s" % (hwnd_self, insert_after))
            check("微信只作为插入位置（第二个参数）", insert_after == 12345,
                  str(insert_after))
            check("带 SWP_NOACTIVATE（不抢焦点）", flags & 0x0010, "flags=0x%X" % flags)
            check("不改尺寸也不移动", (flags & 0x0001) and (flags & 0x0002),
                  "flags=0x%X" % flags)

        # 同一轮里不该每拍都调
        before = len(calls)
        f._apply()
        check("同一轮不重复调", len(calls) == before, "%d -> %d" % (before, len(calls)))

        # 位置变了要重新抬（否则拖微信过程中会被压下去）
        f._z_done = False
        follow.window_box = lambda h: (100, 200, 800, 800)
        f._apply()
        check("位置变化后重新抬", len(calls) > before, "%d -> %d" % (before, len(calls)))
    finally:
        ctypes.windll.user32.SetWindowPos = real


def test_verify_no_spam():
    """`_verify()` 的同一对值**只报一次** —— 否则日志会刷屏。

    用户报的原话（日志里同一条消息每秒一行，而且**没做任何操作**）：
        面板几何被外部改过（(1636, 270, 406, 820) -> (1636, 270, 463, 820)），重新跟随

    真正的根因在 `Panel.set_docked()`（拿 `_size` 当判据，见那边的说明），
    但 `_verify()` 这边也得有护栏：万一窗口**真的**给不了那个尺寸，
    原来的写法会变成「清记录 → 重新下发 → 还是不符 → 再清」的**每秒无限循环**，
    而每次重摆都要整棵布局重排（最贵的那条路）。
    """
    print("\n9d. 自愈不会刷屏：同一对值只报一次")
    logs = []
    f, ui = make_follower(rect=(100, 100, 800, 700))
    f._log = logs.append
    f._apply()
    check("先正常摆好一次（_last 有值）", f._last is not None, str(f._last))

    # 让"实际几何"永远和记录对不上 —— 模拟"窗口就是给不了那个尺寸"
    real = follow.ui_rect
    follow.ui_rect = lambda u: (u._x, u._y, u._w + 57, u._h)
    try:
        for _ in range(f.VERIFY_EVERY * 5):
            f._apply()
            f._verify()
    finally:
        follow.ui_rect = real

    hits = [m for m in logs if "面板几何被外部改过" in m]
    check("确实报了一次（不是干脆不报了）", len(hits) >= 1, "%d 条" % len(hits))
    check("同一对值不会反复刷屏", len(hits) == 1,
          "%d 条：%s" % (len(hits), hits[:3]))
    # 对上了之后要能重新报（不能因为"报过一次"就永久闭嘴）
    f._apply()
    f._verify_count = f.VERIFY_EVERY - 1
    f._verify()
    check("持续不符 -> 仍然保持只报一次", len(
        [m for m in logs if "面板几何被外部改过" in m]) == 1,
        str(len([m for m in logs if "面板几何被外部改过" in m])))

    # ⚠️ "只报一次"不能变成"永久闭嘴"：对上了就要把标记清掉，下次不符还能报。
    #    （`ui_rect` 只在 `_verify()` 里被调，所以别在假函数里按"调用次数"计数
    #      —— 那样数的是**验证次数**，很容易跟外层循环次数对不上。）
    f2, ui2 = make_follower(rect=(100, 100, 800, 700))
    logs2 = []
    f2._log = logs2.append
    real2 = follow.ui_rect
    try:
        f2._apply()
        follow.ui_rect = lambda u: (u._x, u._y, u._w + 57, u._h)
        for _ in range(f2.VERIFY_EVERY * 2):
            f2._apply()
            f2._verify()
        n1 = len([m for m in logs2 if "面板几何被外部改过" in m])
        check("先制造一次不符并报掉", n1 == 1, "%d 条" % n1)
        check("标记记着那一对", f2._warned_pair is not None, str(f2._warned_pair))

        # 恢复正常几何 -> 应该判"对上了"，把标记清掉
        follow.ui_rect = lambda u: (u._x, u._y, u._w, u._h)
        for _ in range(f2.VERIFY_EVERY * 2):
            f2._apply()
            f2._verify()
        check("对上了 -> `_warned_pair` 被清掉（下次还能报）",
              f2._warned_pair is None, str(f2._warned_pair))
    finally:
        follow.ui_rect = real2



def test_raise_to_top():
    """`raise_to_top` —— 悬浮窗「唤醒」改成只改 z 序（2026-09-27）。

    起因是用户报的「为什么生成建议时窗口会闪两下？」：
    原来唤醒靠 `QWidget.setWindowFlag(WindowStaysOnTopHint, True)`
    再于 300ms 后设回去，而 Qt 的 `setWindowFlag()` 在窗口**可见**时
    会**销毁并重建原生窗口**（内部调 `setParent()`，部件先被隐藏、
    必须再 `show()`）—— 每调一次闪一下，两个来回就是闪两下，
    而且正好落在点【生成建议】/【只看对话】的那一刻
    （`run_watch.do_generate` 第一句就是 `ui.wake()`）。
    """
    print("\n13. 唤醒只改 z 序（不重建原生窗口，所以不闪）")
    import ctypes

    calls = []
    real = ctypes.windll.user32.SetWindowPos

    def fake(hwnd, insert_after, x, y, cx, cy, flags):
        calls.append((hwnd, insert_after, x, y, cx, cy, flags))
        return 1

    real_h = follow.ui_native_handle
    ctypes.windll.user32.SetWindowPos = fake
    try:
        follow.ui_native_handle = lambda ui: 777
        ui = FakeUI()
        ok = follow.raise_to_top(ui)
        check("调到了 SetWindowPos 并报成功", ok is True and len(calls) == 1,
              repr(calls))
        if calls:
            hwnd, after, x, y, cx, cy, flags = calls[0]
            # ⚠️ 2026-10-14：`follow.set_window_pos` 现在按 `wintypes.HWND`（= c_void_p）
            #    传参，所以这里拿到的可能是 `c_void_p(777)` 而不是 `777` ——
            #    **比之前先归一化成 int**（`int(c_void_p(777))` 也能用，但字符串形式不行）。
            def _as_int(v):
                try:
                    return int(v.value) if hasattr(v, "value") else int(v)
                except Exception:
                    return None

            check("动的是**我们自己的**窗口", _as_int(hwnd) == 777, str(hwnd))
            check("插到 z 序最上面（HWND_TOP = 0）", _as_int(after) == 0, str(after))
            check("带 SWP_NOACTIVATE（不然打字会打进面板）",
                  flags & 0x0010, "flags=0x%X" % flags)
            check("不移动也不改大小（只改层级）",
                  (flags & 0x0001) and (flags & 0x0002), "flags=0x%X" % flags)
            check("没有传坐标/尺寸（都不该被用上）",
                  (x, y, cx, cy) == (0, 0, 0, 0), repr((x, y, cx, cy)))
        # 拿不到真句柄时必须**静默放弃**，不能拿假句柄（offscreen 下恒为 0x1）
        # 去赌运行 —— 那是 `ui_native_handle` 里的既有约定。
        calls[:] = []
        follow.ui_native_handle = lambda ui: 0
        check("拿不到真句柄时返回 False 且不调用", follow.raise_to_top(ui) is False
              and calls == [], repr(calls))
    finally:
        ctypes.windll.user32.SetWindowPos = real
        follow.ui_native_handle = real_h


def test_no_redline():
    """跟随器只能**读**微信窗口 —— 这是本项目的红线。"""
    print("\n12. 红线：不移动 / 不置顶微信窗口")
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "wechat_assistant", "follow.py"), encoding="utf-8").read()
    code_lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
    code = "\n".join(code_lines)
    # 这些是真会改到**别人窗口**的调用，一个都不能有
    banned = ["MoveWindow", "SetForegroundWindow", "ShowWindow",
              "SendInput", "keybd_event", "mouse_event"]
    hit = [b for b in banned if b in code]
    check("没有会改到微信窗口的调用", not hit, repr(hit))
    check("用的是只读的 GetWindowRect", "GetWindowRect" in src)
    check("用的是只读的 IsIconic", "IsIconic" in src)
    check("用的是只读的 GetForegroundWindow", "GetForegroundWindow" in src)
    # SetWindowPos 是允许的，但**只允许动我们自己的窗口**，而且必须带
    # SWP_NOACTIVATE（行为验证见上面那条测试）。
    # ⚠️ 这里原来断言的是"整个文件里只出现 1 次调用" —— 那个数字是**当时**
    #    只有「插到微信正上方」一个用途时的快照。2026-09-27 加了
    #    `raise_to_top`（唤醒悬浮窗走 z 序，不再重建原生窗口，见 `_do_wake`），
    #    于是变成了 2 处。**与其把数字改成 2（下次再加一处又要改），
    #    不如直接钉住规则本身**：调用点必须在**白名单函数**里，
    #    且每个函数里都用了 `SWP_NOACTIVATE`。
    #    这样"有人在新函数里偷偷动别人的窗口"会被抓住，
    #    而"在允许的地方多写一处"不会误报。
    import ast as _ast

    # ⚠️ 2026-10-14：`SetWindowPos` 的调用点从"两个函数各写一遍"收进了中枢函数
    #    `set_window_pos()`（为了让 `argtypes` 只设一处 —— 少了它会**静默失败**，
    #    见那个函数的说明）。所以这里除了放行它，还**加了两条更硬的约束**：
    #      ① 中枢函数**只允许被**白名单里那几个调用（别的功能不许借道）；
    #      ② 它自己必须带 `SWP_NOACTIVATE`。
    allowed = {"set_window_pos", "raise_above", "raise_to_top"}
    sites = []

    def _walk(node, fn):
        for child in _ast.iter_child_nodes(node):
            if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                _walk(child, child.name)
                continue
            if isinstance(child, _ast.Call):
                f = child.func
                if (isinstance(f, _ast.Attribute) and f.attr == "SetWindowPos"):
                    sites.append(fn)
            _walk(child, fn)

    # ⚠️ 顶层这一层要**自己判**是不是函数 —— 直接 `_walk(每个顶层语句, None)`
    #    的话，函数体的第一层就是 fn=None，于是所有调用点都记成"不在函数里"，
    #    断言会因为**全都为 None** 而失败（第一次就是这么写的）。
    _mod = _ast.parse(src)
    for _n in _mod.body:
        if isinstance(_n, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            _walk(_n, _n.name)
        else:
            _walk(_n, None)
    check("SetWindowPos 只出现在白名单函数里",
          all(s in allowed for s in sites) and sites,
          "调用点所在函数: %s（允许: %s）" % (sorted(set(sites)), sorted(allowed)))
    check("每个白名单函数里都真的带 SWP_NOACTIVATE", "SWP_NOACTIVATE" in code)

    # ② **中枢函数只允许被白名单里那几个调用** ——
    #    否则"以后随便哪里想动 z 序就调它"会把红线悄悄放宽。
    callers = []

    def _walk2(node, fn):
        for child in _ast.iter_child_nodes(node):
            if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                _walk2(child, child.name)
                continue
            if isinstance(child, _ast.Call):
                f = child.func
                if isinstance(f, _ast.Name) and f.id == "set_window_pos":
                    callers.append(fn)
            _walk2(child, fn)

    for _n in _mod.body:
        if isinstance(_n, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            _walk2(_n, _n.name)
        else:
            _walk2(_n, None)
    _ok_callers = {"raise_above", "raise_to_top"}
    check("set_window_pos 只被 raise_above / raise_to_top 调用（别人不许借道）",
          set(callers) <= _ok_callers and callers,
          "调用者: %s（允许: %s）" % (sorted(set(callers)), sorted(_ok_callers)))
    # ⚠️ `raise_to_top` 的入参**只能有我们自己的界面对象** ——
    #    签名里一旦多出一个"参考窗口"参数，它就有了动别人窗口的能力，
    #    而它存在的理由只是"把自己提到普通窗口堆最上面"。
    _rt = [n for n in _mod.body
           if isinstance(n, _ast.FunctionDef) and n.name == "raise_to_top"]
    check("raise_to_top 只收一个参数（拿不到「别人的窗口」）",
          len(_rt) == 1 and len(_rt[0].args.args) == 1,
          repr([a.arg for a in _rt[0].args.args]) if _rt else "没找到这个函数")

    # ---- ⚠️⚠️ 置顶窗口**不许**被插到普通窗口上方（2026-10-14）
    # 用户报「保持置顶有时候会出错」。实测（.tmp/probe_opacity_topmost.py）：
    #   面板开着 `ui.topmost` 时，一次 `raise_above()` 之后 `WS_EX_TOPMOST`
    #   从 True 变成 **False** —— `SetWindowPos` 的规则是"把窗口插到非置顶窗口
    #   旁边会清掉它的置顶样式"，而 `hWndInsertAfter` 正是微信那个普通窗口。
    #   「有时候」= 取决于有没有切回过微信（那才会走 `_sync_zorder` → 这里）。
    # 判据：① 置顶时**一个 SetWindowPos 都不许发**；② 它要返回 True
    #       （= "已经到位"，让调用方记成"这拍做完了"，别每拍重试）。
    print("\n12b. 置顶窗口不许被插到普通窗口上方（会把置顶剥掉）")
    import ctypes as _ct

    calls = []
    real_sp = _ct.windll.user32.SetWindowPos
    real_top = follow.is_topmost
    try:
        _ct.windll.user32.SetWindowPos = lambda *a, **k: calls.append(a) or 1
        follow.is_topmost = lambda h: True
        ok = follow.raise_above(111, 222)
        check("置顶时：不发 SetWindowPos（一个都不发）", not calls, repr(calls))
        check("置顶时：返回 True（表示「已经到位」，别每拍重试）",
              ok is True, repr(ok))

        calls.clear()
        follow.is_topmost = lambda h: False
        ok2 = follow.raise_above(111, 222)
        check("不置顶时：照旧插到参考窗口上方（发一次）",
              len(calls) == 1, repr(calls))
        check("  而且是插到**参考窗口**那个 hwnd（不是 HWND_TOPMOST）",
              calls and calls[0][1] == 222, repr(calls))
        check("  仍然带 SWP_NOACTIVATE（不抢焦点）",
              calls and (calls[0][6] & 0x0010), repr(calls))
        check("  返回值仍为真", bool(ok2))
    finally:
        _ct.windll.user32.SetWindowPos = real_sp
        follow.is_topmost = real_top
    # 入参不合法时一律不动作（两个都要不炸）
    check("空句柄不动作、也不炸",
          follow.raise_above(0, 222) is False and follow.raise_above(111, 0) is False)



def test_qt_seam():
    """跟随器 x Qt 悬浮窗的接缝。

    `follow.py` 是**业务层**，它跟界面打交道只有两个口子：`ui_rect`（量几何）
    和 `ui_native_handle`（拿句柄）。**这条缝最容易在运行时炸而单测碰不到**，
    因为真跑起来要有微信，所以这里用桩把两侧都钉住。
    """
    print("\n13. 跟随器 x Qt 悬浮窗的接缝")

    # ⚠️ 先把上一个用例留下的桩清掉（见 `reset_patches` 的说明）
    reset_patches()

    class QtStyle:
        def px(self, v):
            return int(v)

    class QtUI:
        """Qt 面板的最小替身：只有 x/y/width/height（没有 `.root`）。"""

        def __init__(self):
            self.style = QtStyle()
            self.collapsed = False
            self._docked = False
            self._geo = [100, 120, 340, 600]

        def x(self):
            return self._geo[0]

        def y(self):
            return self._geo[1]

        def width(self):
            return self._geo[2]

        def height(self):
            return self._geo[3]

        def winId(self):
            return 0            # 拿不到真句柄 -> 必须安全地返回 0

        def windowHandle(self):
            return None

        def desired_width(self):
            return self.style.px(340)

        def desired_size(self):
            return self.style.px(340), None

        def min_height(self):
            return self.style.px(260)

        def set_docked(self, x, y, w, h):
            self._docked = True
            self._geo = [int(x), int(y), int(w), int(h)]

        def clear_docked(self):
            self._docked = False

        def is_visible(self):
            return True

        def show(self):
            pass

        def hide(self):
            pass

        def post_status(self, m):
            pass

    ui = QtUI()
    check("ui_rect 给出 x/y/w/h",
          follow.ui_rect(ui) == (100, 120, 340, 600), str(follow.ui_rect(ui)))
    # ⚠️ 拿不到真句柄时必须返回 0 —— 把 0x1 这种假句柄喂给 SetWindowPos
    #    是在赌运气（实测 offscreen 平台下 winId() 恒为 0x1）。
    check("拿不到真句柄时返回 0（不喂假句柄给 SetWindowPos）",
          follow.ui_native_handle(ui) == 0, str(follow.ui_native_handle(ui)))

    class Cfg:
        def get(self, path, default=None):
            return {"ui.attach": "dock"}.get(path, default)

    f = follow.WindowFollower(ui, Cfg(), logger=lambda m: None)
    f._last = None
    f._set(140, 160, 320, 640)
    check("跟随器能把 Qt 面板摆到指定位置",
          (ui.x(), ui.y(), ui.width(), ui.height()) == (140, 160, 320, 640),
          str((ui.x(), ui.y(), ui.width(), ui.height())))
    check("摆好之后 _docked=True", ui._docked is True)
    check("跟随器记住了这一组几何", f._last == (140, 160, 320, 640), str(f._last))

    # ⚠️ 尺寸没变就只挪位置：Tk 版靠这个避免整棵控件树重排，
    #    Qt 版靠它避免重跑布局（每张卡片重新换行 + 滚动条重算）。
    f._set(200, 220, 320, 640)
    check("尺寸不变时只挪位置", (ui.x(), ui.y()) == (200, 220),
          str((ui.x(), ui.y())))
    f._undock()
    check("解绑之后交还高度控制权", ui._docked is False)

    # Qt 分支的定时器：**必须被记住引用**，否则 QTimer 被 GC，
    # 定时器只响一次就没了（症状是"面板只跟一帧然后不动"）。
    try:
        import PySide6  # noqa: F401

        classic = f._job
        f.running = True
        f._schedule()
        check("Qt 分支的定时器被记住了引用（不会被 GC）",
              f._job is not None and f._job is not classic, str(type(f._job)))
        f.running = False
        f.stop()
    except ImportError:
        print("      （PySide6 不可用，跳过 Qt 定时器用例）")


    # ⚠️ `follow` 是从 `winchrome.top_level_hwnd` 借实现取顶层句柄的，而
    #    `winchrome` 是**纯 Tk 的外框补丁** —— 业务层被界面专属模块拽着，
    #    那条依赖必须断掉。2026-09-26 连 `winchrome.py` 本身也删了，
    #    所以这里改成钉两件事：文件不存在、follow 里也没有回头的引用。
    #
    # ⚠️ 判据要**解析 AST**，不能用"这一行里有 winchrome 又有 import" ——
    #    实测被注释里那句"这里刻意不 import `winchrome`"骗过一次。
    #    审计"代码里有没有引用 X"时，文本匹配会把解释性注释算成命中。
    import ast
    import io

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    check("winchrome.py 已经删掉了（Tk 版退休）",
          not os.path.exists(os.path.join(root_dir, "wechat_assistant",
                                          "winchrome.py")))
    src_path = os.path.join(root_dir, "wechat_assistant", "follow.py")
    src = io.open(src_path, encoding="utf-8").read()
    found = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if "winchrome" in a.name]
        elif isinstance(node, ast.ImportFrom):
            if "winchrome" in (node.module or ""):
                found.append(node.module)
    check("follow.py 的**代码**里不引用 winchrome", found == [], str(found))

    # ⚠️ 只钉"接口还在、对垃圾输入不抛异常"。句柄有效性由
    #    `probes/qt/diag_follow_qt.py` 在真窗口下验 ——
    #    在单测里建真窗口量句柄会受测试环境干扰（踩过假值 0x1869F）。
    check("ui_native_handle 对拿不到句柄的对象安全返回 0",
          follow.ui_native_handle(object()) == 0)


def main():
    test_compute_dock()
    test_dock_gap()
    test_shrink_to_fit()
    test_no_room_at_all()
    test_dock_right()
    test_no_room()
    test_dedup()
    test_deadband()
    test_self_heal()
    test_user_moved()
    test_minimize()
    test_minimize_then_handle_lost()
    test_wechat_gone()
    test_fixed_height()
    test_attach_off()
    test_attach_float()
    test_use_config()
    test_zorder()
    test_verify_no_spam()
    test_raise_to_top()
    test_no_redline()
    test_qt_seam()

    print("\n" + "=" * 60)
    print("通过 %d，失败 %d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
