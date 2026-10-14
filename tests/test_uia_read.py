# -*- coding: utf-8 -*-
"""UIA 取数体检：微信到底还能不能读到数据？

**两个互相独立的测试**，可以分开跑：

    测试一 · 会话列表（左侧栏）   reader.read_sessions
    测试二 · 聊天区（右侧）       reader.read_chat

为什么单独拆出来
----------------
主方案是 UIA，而 UIA 依赖**微信的控件树**。微信一升级，类名/automationId
就可能变，表现是「程序能启动、不报错，但什么都读不到」——
本项目的兜底全是 `except Exception: log(...)`，**不会崩，只会静默少干活**。

所以需要这么一个 10 秒能跑完的体检，回答一个问题：
**「现在这一刻，UIA 能不能拿到数据？」**
读不到时它会把**实际看到的类名**打出来，直接对着改 `config.yaml`。

⚠️ 这两个测试**只读**：不点微信、不写库、不发送任何消息。

用法：
    python tests/test_uia_read.py            # 两个都跑
    python tests/test_uia_read.py sessions   # 只跑测试一
    python tests/test_uia_read.py chat       # 只跑测试二
    python tests/test_uia_read.py sessions --dump    # 连类名清单一起打

退出码：0 = 全过；1 = 有失败（含「微信没开」）
"""

import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ⚠️ DPI 感知必须在任何窗口操作之前设，否则 UIA 给的 rect 和实际坐标对不上
#    （Windows 会给我们"逻辑坐标"，和物理像素差一个缩放比）。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from wechat_assistant.config import load_config          # noqa: E402
from wechat_assistant.reader import (SCOPE_DESCENDANTS,   # noqa: E402
                                     WeChatReader, is_time_text)
from wechat_assistant.uia_worker import UiaError, UiaWorker      # noqa: E402

PASS, FAIL = 0, 0
DUMP = "--dump" in sys.argv


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))
    return ok


def section(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def dump_class_names(reader, hwnd):
    """把微信窗口里**实际存在的控件类名**统计出来。

    这是给「微信升级之后读不到了」用的：拿这个清单直接对着改
    `config.yaml` 的 `wechat.cls` / `wechat.node`，不用猜。

    ⚠️ 用 `FindAll(TreeScope_Descendants, ...)`，**不是** `GetChildren()` 递归 ——
    后者在 Qt 界面上会极慢甚至栈溢出（见 CLAUDE.md 的硬性规则）。
    """
    from wechat_assistant.reader import SCOPE_DESCENDANTS  # noqa: F401

    def job(auto, uia_mod):
        el = auto.ControlFromHandle(hwnd).Element
        arr = el.FindAll(SCOPE_DESCENDANTS, uia_mod.CreateTrueCondition())
        seen = {}
        for i in range(arr.Length):
            try:
                e = arr.GetElement(i)
                cls = e.CurrentClassName or "(空)"
                aid = e.CurrentAutomationId or ""
            except Exception:
                continue
            key = (cls, aid)          # ⚠️ 存**完整** aid，别截断
            seen[key] = seen.get(key, 0) + 1
        return seen

    try:
        return reader.worker.call(job) or {}
    except Exception as e:
        print("     （类名清单取失败：%s: %s）" % (type(e).__name__, e))
        return {}


def print_classes(seen, top=20):
    if not seen:
        return
    # 有 automationId 的排在前面 —— 那些才是 `config.yaml` 里真正在用的锚点
    items = sorted(seen.items(),
                   key=lambda kv: (1 if kv[0][1] else 0, kv[1]), reverse=True)
    print("\n  —— 窗口里实际存在的控件（类名 / automationId / 个数）——")
    for (cls, aid), n in items[:top]:
        mark = "★" if aid else " "
        show = aid if len(aid) <= 46 else aid[:43] + "..."
        print("   %s %-42s %-48s x%d" % (mark, cls[:42], show or "(无 aid)", n))
    print("  ★ = 有 automationId，`config.yaml` 里配对的就是这些")


def check_config_anchors(cfg, seen, snap=None):
    """配置里写的锚点，实际到底找不找得到。

    这是整个体检里**最该先看**的一段：读不到东西时，原因八成就在这张表里 ——
    某项显示 `✗` 就说明微信改了名字，改 `config.yaml` 就行，不用动代码。

    ⚠️ 两条**不是问题**的 ✗，别被它们带偏：
      · `session_cell` 在**左栏**，本测试主要看右侧；
      · `titlebar_group` 只在**群聊**里存在 —— 单聊时它就是找不到，
        这时该看的是 `titlebar_single`。
    两项都会按当前会话类型自动跳过。
    """
    print("\n  —— 配置里的锚点 vs 实际情况 ——")
    cls_names = set(c for (c, _a) in seen)
    aids = set(a for (_c, a) in seen if a)

    # ⚠️ **匹配语义按 reader.py 的真实写法来，别想当然**：
    #    · `read_chat_title` 用的是**子串**（`id_title in aid`）——
    #      标题的 AutomationId 是一长串路径
    #      `content_view...current_chat_name_label`，**不是**精确等于配置值。
    #      我第一版按 `startswith` 比、还把 aid 截断到 28 字符，
    #      于是标题明明读到了却报 ✗ —— **测试自己错了**。
    #    · `read_chat` 找容器用**等值**（`aid == id_msglist`）、
    #      找消息项用**前缀**（`aid.startswith(id_msglist)`）。
    #    这里统一放宽成「等值 or 子串」，目的只是回答「这个锚点还能用吗」。
    def aid_hit(want):
        return any(a == want or want in a for a in aids)

    def cls_hit(want):
        return any(want == c or c.endswith(want) or want in c for c in cls_names)

    def suggest(want):
        """没找到时，从实际控件里挑几个**看起来像**的候选。"""
        parts = [p for p in want.replace("::", "_").split("_") if len(p) >= 3]
        cands = []
        for s in list(aids) + list(cls_names):
            if not s:
                continue
            low = s.lower()
            score = sum(1 for p in parts if p.lower() in low)
            if score > 0:
                cands.append((score, s))
        cands.sort(key=lambda x: -x[0])
        return [s for _sc, s in cands[:3]]

    is_group = bool(getattr(snap, "is_group", False)) if snap is not None else None
    rows = [
        ("wechat.node.chat_title", cfg.get("wechat.node.chat_title"), aid_hit, None),
        ("wechat.node.message_list", cfg.get("wechat.node.message_list"), aid_hit, None),
        ("wechat.node.session_list", cfg.get("wechat.node.session_list"), aid_hit, None),
        ("wechat.node.input_field", cfg.get("wechat.node.input_field"), aid_hit, None),
        ("wechat.cls.session_cell", cfg.get("wechat.cls.session_cell"), cls_hit,
         "左栏的会话项，右侧本来就看不着"),
        ("wechat.cls.msg_text", cfg.get("wechat.cls.msg_text"), cls_hit, None),
        ("wechat.cls.msg_bubble_ref", cfg.get("wechat.cls.msg_bubble_ref"), cls_hit, None),
        ("wechat.cls.titlebar_group", cfg.get("wechat.cls.titlebar_group"), cls_hit,
         None if is_group else "当前是**单聊**，群聊标题栏本来就不存在"),
        ("wechat.cls.titlebar_single", cfg.get("wechat.cls.titlebar_single"), cls_hit,
         "当前是**群聊**，单聊标题栏本来就不存在" if is_group else None),
    ]

    bad = []
    for key, val, probe, skip_reason in rows:
        if not val:
            print("   ?  %-28s %-38s （配置里没写）" % (key, ""))
            continue
        if skip_reason and not probe(val):
            print("   -  %-28s %-38s （跳过：%s）" % (key, val, skip_reason))
            continue
        hit = probe(val)
        print("   %s %-28s %-38s" % ("✓" if hit else "✗", key, val))
        if hit:
            continue
        cands = suggest(val)
        if cands:
            print("      ↳ 实际更接近的有：%s" % " / ".join(cands))
        bad.append((key, val))

    if bad:
        print()
        print("   ⚠️ 有 %d 项在窗口里**没看到**。先看上面的「↳ 实际更接近的有」。"
              % len(bad))
        print("      改 `config.yaml` 就行，**不用改代码** —— "
              "类名和 automationId 全集中在那个文件里就是为了这个。")
    return bad


# ==================================================================== 测试一

def test_sessions(reader, hwnd):
    """测试一：会话列表（左侧栏）能不能读到。

    这里读的是**左侧那一列会话**，它的一项 Name = 「会话名 + 最后一条预览 + 时间」
    三行拼在一起。它的用途是「要不要提醒」，**不是**对话本体。

    判据：
      · 至少读到 1 个会话
      · 每个会话都有非空的会话名
      · 大部分会话带预览或时间（个别空会话可以没有）
    """
    section("测试一 · 会话列表（左侧栏）  reader.read_sessions")
    log = lambda m: None
    cfg = load_config()

    t0 = time.perf_counter()
    try:
        sessions = reader.read_sessions(hwnd)
        ms = (time.perf_counter() - t0) * 1000
    except UiaError as e:
        check("读会话列表不超时/不报错", False, str(e))
        return
    except Exception as e:
        check("读会话列表不超时/不报错", False, "%s: %s" % (type(e).__name__, e))
        return

    if not check("读到会话列表（不报错）", True, "%.0f ms" % ms):
        return
    if not check("至少读到 1 个会话", len(sessions) > 0, "%d 个" % len(sessions)):
        print("     ⚠️ 列表为空 —— 先确认微信**左栏**不是空的（没登录 / 全被折叠）")
        return

    named = [s for s in sessions if (s.raw_title or "").strip()]
    check("每个会话都有名字", len(named) == len(sessions),
          "%d/%d 有名字" % (len(named), len(sessions)))

    with_preview = [s for s in sessions if (s.preview or "").strip()]
    with_time = [s for s in sessions if (s.time_text or "").strip()]
    # 空会话（从没聊过）本来就没有预览，所以不能要求 100%
    check("多数会话带预览", len(with_preview) >= max(1, len(sessions) // 2),
          "%d/%d" % (len(with_preview), len(sessions)))
    check("字段切分正确（名字里不该残留换行）",
          all("\n" not in (s.raw_title or "") for s in sessions))

    print("\n  前几个会话：")
    for s in sessions[:5]:
        print("     %-22s | %-24s | %s"
              % ((s.raw_title or "")[:22], (s.preview or "")[:24], s.time_text or ""))

    t0 = time.perf_counter()
    title = reader.read_chat_title(hwnd)
    ms2 = (time.perf_counter() - t0) * 1000
    check("读到当前聊天标题", bool(title), "%r，%.0f ms" % (title, ms2))

    # 轻量读取必须比全量快 —— 这是「轻量不读正文」这个设计意图的判据
    check("轻量读取足够快（< 150ms）", ms < 150, "%.0f ms" % ms)


# ==================================================================== 测试二

def test_chat(reader, hwnd):
    """测试二：聊天区（右侧）能不能读到。

    判据：
      · 有消息容器 rect（后面判「在不在底部」要用）
      · 至少读到 1 条消息，每条都有 rect
      · 消息的 rect 落在容器的竖直范围内
      · 类型能分类（text / refer / unknown）
      · `at_bottom` 判得出结论

    附带：时间分隔符数量、发送者的初始状态（这里是 None，
          真正的判定在 sender.py，要取像素，不在本测试范围内）。
    """
    section("测试二 · 聊天区（右侧）  reader.read_chat")
    bag = {}

    t0 = time.perf_counter()
    try:
        snap = reader.read_chat(hwnd)
        ms = (time.perf_counter() - t0) * 1000
    except UiaError as e:
        check("读聊天区不超时/不报错", False, str(e))
        return bag
    except Exception as e:
        check("读聊天区不超时/不报错", False, "%s: %s" % (type(e).__name__, e))
        return bag

    bag["snap"] = snap
    check("读聊天区（不报错）", True, "%.0f ms" % ms)
    if snap is None:
        check("拿到快照对象", False, "返回 None")
        return bag
    check("拿到快照对象", True)

    check("读到会话标题", bool(snap.title), "%r" % snap.title)
    check("识别出是不是群聊", isinstance(snap.is_group, bool),
          "群聊=%s" % snap.is_group)

    if not check("读到消息容器 rect", snap.container_rect is not None,
                 str(snap.container_rect)):
        print("     ⚠️ 没有容器 rect —— 「在不在底部」判不出来，"
              "多半是 `wechat.node.message_list` 的 automationId 变了")
        return bag

    msgs = snap.messages
    if not check("至少读到 1 条消息", len(msgs) > 0,
                 "%d 条" % len(msgs)):
        print("     ⚠️ 一条消息都没有 —— 换个有内容的会话再试；"
              "仍然为空就看下面的类名清单")
        return bag

    with_rect = [m for m in msgs if m.rect]
    check("每条消息都带 rect", len(with_rect) == len(msgs),
          "%d/%d" % (len(with_rect), len(msgs)))

    # 消息应当落在容器的竖直范围内
    top, bottom = snap.container_rect[1], snap.container_rect[3]
    inside = [m for m in with_rect
              if m.rect[3] > top - 200 and m.rect[1] < bottom + 200]
    check("消息 rect 落在容器范围内", len(inside) == len(with_rect),
          "%d/%d（容器 %d~%d）" % (len(inside), len(with_rect), top, bottom))

    kinds = {}
    for m in msgs:
        kinds[m.kind] = kinds.get(m.kind, 0) + 1
    check("消息类型能分类", "unknown" not in kinds or len(kinds) > 1,
          str(kinds))
    check("发送者初始为未判定（判定要取像素，不在这里做）",
          all(m.is_self is None for m in msgs))

    markers = snap.markers or []
    print("\n  读到 %d 条消息，%d 个时间分隔符" % (len(msgs), len(markers)))
    for m in msgs[:6]:
        print("     [%-7s] %s" % (m.kind, (m.text or "")[:48]))
    if markers:
        print("  时间分隔符：%s"
              % " / ".join("%s%s" % (mk.text, "(跨天)" if mk.is_date_level else "")
                           for mk in markers[:6]))

    # at_bottom 必须给得出结论（不抛异常）
    try:
        ab = snap.at_bottom()
        gap = snap.bottom_gap()
        check("at_bottom 判得出结论", isinstance(ab, bool),
              "在底部=%s，gap=%d" % (ab, gap))
    except Exception as e:
        check("at_bottom 判得出结论", False, "%s: %s" % (type(e).__name__, e))

    check("全量读取耗时可接受（< 300ms）", ms < 300, "%.0f ms" % ms)

    # 时间分隔符是可选的：微信连着聊时根本不插，所以这里只提示不断言
    if not check("（参考）时间分隔符能读到", True,
                 "%d 个" % len(markers)):
        pass
    if not markers:
        print("     · 这一屏没有时间分隔符。想验证这条：往下翻到有明显的"
              "「昨天/9月20日」的位置再跑一次")

    return bag


# ==================================================================== UIA 视图对比

# 「树是健康的」的下限。实测能读到的机器 160+ 个后代，读不到的只有 2 个。
TREE_HEALTHY_DESCENDANTS = 30


def view_counts(reader, hwnd):
    """三种 UIA 视图各有多少节点。

    ⚠️ 为什么要比这个（2026-09-27）
    有篇 CSDN 文章说微信「仅在检测到无障碍客户端时才会激活完整的
    **Control View** 暴露」。**它的解释站不住**（跨进程看不到对方引用了哪些 DLL，
    真正的 API 是 `UiaClientsAreListening()`），它给的 C# 代码也只是
    `FromHandle` + 遍历 —— 正是我们已经在做的。

    **但它点出了一个我们没验过的变量**：我们一直用
    `FindAll(Descendants, TrueCondition)`（默认视图），从没比过
    Raw / Control / Content 三种视图。

    实测（能读到的机器）：三种视图数字**完全一样**（166/167/167/167）。
    ⚠️ 那不能证明在读不到的机器上也一样 —— 所以这里**每次都打出来**，
    两边一比就知道。
    """
    def job(auto, uia_mod):
        el = auto.ControlFromHandle(hwnd).Element
        out = {}
        # ⚠️ 错误信息要带上**真正的原因**，不能只写 "err" ——
        #    第一版写死 "err"，结果在正常机器上算了半天也看不出为什么。
        try:
            out["FindAll(True)"] = el.FindAll(
                SCOPE_DESCENDANTS, uia_mod.CreateTrueCondition()).Length
        except Exception as e:
            out["FindAll(True)"] = "err(%s)" % type(e).__name__
        for name, attr in (("RawView", "RawViewWalker"),
                           ("ControlView", "ControlViewWalker"),
                           ("ContentView", "ContentViewWalker")):
            try:
                walker = getattr(uia_mod, attr)
                n, stack = 0, [el]
                while stack and n < 5000:
                    cur = stack.pop()
                    n += 1
                    try:
                        ch = walker.GetFirstChildElement(cur)
                    except Exception:
                        continue
                    while ch:
                        stack.append(ch)
                        try:
                            ch = walker.GetNextSiblingElement(ch)
                        except Exception:
                            break
                out[name] = n
            except Exception as e:
                out[name] = "err(%s)" % type(e).__name__
        # ⚠️ 这两个是 IUIAutomation 的**属性**，不是 CreateXxxViewCondition()
        #    方法（写错会直接报「找不到该属性」）
        for name, attr in (("ControlCond", "ControlViewCondition"),
                           ("ContentCond", "ContentViewCondition")):
            try:
                out[name] = el.FindAll(SCOPE_DESCENDANTS,
                                       getattr(uia_mod, attr)).Length
            except Exception as e:
                out[name] = "err(%s)" % type(e).__name__
        return out

    try:
        return reader.worker.call(job) or {}
    except Exception as e:
        print("     （视图对比取失败：%s: %s）" % (type(e).__name__, e))
        return {}


def print_views(vc, total):
    """打印视图对比并给结论。

    ⚠️ **判据要拿视图之间比，别拿绝对阈值比。**
       我第一版写「max(所有视图) >= 30 就判定某视图有内容」，
       结果在**正常**机器上也报「⚠️⚠️ 某个视图是有内容的」，完全指反。

    ⚠️ **还要处理「基准值取不到」的情况**：如果 `FindAll(True)` 那项是
       `err(...)`（非整数），拿它当基准就会一路走到最后的 else，
       在正常机器上打出「三种视图都是空的」—— **又是一个反向结论**。
       所以基准缺失时，退回「只看三个 walker 之间是否一致」。
    """
    if not vc:
        return
    print("\n  —— 三种 UIA 视图各有多少节点 ——")
    for k, v in vc.items():
        print("     %-16s = %s" % (k, v))
    nums = {k: v for k, v in vc.items() if isinstance(v, int)}
    if not nums:
        print("     （一个都没取到 —— UIA 这次完全没工作，先看上面的报错）")
        return
    base = nums.get("FindAll(True)")
    others = [v for k, v in nums.items() if k != "FindAll(True)"]
    best = max(nums.values())
    if others and base is not None and \
            best > max(base * 3, TREE_HEALTHY_DESCENDANTS):
        print("     ⚠️⚠️ **某个视图是有内容的**（%d，而 FindAll 只有 %d）—— 那就是它！"
              % (best, base))
        print("         把读取改成走那个视图即可。")
    elif best >= TREE_HEALTHY_DESCENDANTS:
        print("     ✓ 视图都有内容（最多 %d）—— **视图不是问题**。" % best)
    else:
        print("     → 视图**都是空的**（最多 %d）——微信确实没暴露控件树。" % best)


# ==================================================================== 主流程

def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = argv[0].lower() if argv else "both"
    if only not in ("both", "sessions", "chat"):
        print("用法：python tests/test_uia_read.py [sessions|chat] [--dump]")
        return 2

    print("=" * 74)
    print("UIA 取数体检（只读：不点微信、不写库、不发送任何消息）")
    print("=" * 74)

    cfg = load_config()
    try:
        worker = UiaWorker(cfg.get("runtime.uia_timeout", 20.0), logger=lambda m: None)
    except UiaError as e:
        print("\n❌ UIA 初始化失败：%s" % e)
        print("   UIA 是主方案 —— 这条不通，整个读取链路都不通。")
        print("   常见原因：uiautomation / comtypes 没装好；"
              "或当前是服务/无桌面会话（没有交互式桌面）。")
        return 1

    reader = WeChatReader(cfg, worker, logger=lambda m: None)
    win = reader.find_window()
    if not win:
        print("\n❌ 没找到微信主窗口 —— 后面两个测试都跑不了。")
        print("   确认：① 微信**已登录**  ② 主窗口没被关掉（最小化没关系，UIA 照样读）"
              "  ③ 进程名是 Weixin.exe")
        print("   期望的窗口类名特征：%s"
              % cfg.get("wechat.window_class_contains", "(未配置)"))
        worker.close()
        return 1

    print("\n找到微信窗口：hwnd=%s  pid=%s" % (hex(win["hwnd"]), win["pid"]))

    try:
        if only in ("both", "sessions"):
            test_sessions(reader, win["hwnd"])
        if only in ("both", "chat"):
            bag = test_chat(reader, win["hwnd"])
            seen = dump_class_names(reader, win["hwnd"])
            print_classes(seen)
            n_fa = 0
            for (c, _a), n in seen.items():
                n_fa += n
            print_views(view_counts(reader, win["hwnd"]), n_fa)
            check_config_anchors(cfg, seen, bag.get("snap"))
    finally:
        worker.close()

    print("\n" + "=" * 74)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    if FAIL:
        print()
        print("⚠️ **先看上面「三种 UIA 视图」那一段**，它已经分好类了：")
        print("   · 三个视图都空   -> 微信**没暴露控件树**。")
        print("      改 config.yaml / ANCHORS **没用**（没有东西可改）。")
        print("      实测确认这是**账号维度**的：同一台机器换个账号就好了。")
        print("      排查方向：微信是否登录 → 换账号交叉实验 → 见")
        print("      `E:\\Claude\\shared\\wechat-uia-check.py`（那个脚本会把环境打全）")
        print("   · 某个视图有内容 -> 那就是它，把读取改成走那个视图")
        print("   · 视图都正常     -> 那才可能是锚点变了，看「↳ 实际更接近的有」；")
        print("      但也要先确认「这一屏里本来就没有那个控件」")
        print("   · `python probes\\probe_capability.py` 可以看整棵树做复核")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
