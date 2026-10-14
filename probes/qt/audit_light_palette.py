# -*- coding: utf-8 -*-
"""浅色色板的**可读性/可分辨性**体检（量数字，不看观感）。

    python -X utf8 probes/qt/audit_light_palette.py

⚠️ 为什么单独写：浅色这几档灰是 2026-09-27 按线索调的（原来 `surface_alt`
   是 `#DCDCE1`，用户说"太深了"）。"调浅"这件事**有连带影响**，最容易漏的是：
     ① 底色变浅之后，**铺在它上面的东西看不出来了** —— 比如 `hover`
        原来是 `#F0F0F0`(240)，而新侧栏底是 `#EEEEF0`(239.4)，**只差 0.6**，
        悬停等于没有。这类"差值消失"是调浅时最典型的自伤。
     ② 描边类（`card_line`）调浅会让**卡片整个消失**，因为浅色下卡片是
        "白底铺在 #FAFAFA 上"，两者只差 5，全靠那条描边。
     ③ 文字压在新底色上的对比度会变（多数变好，但要确认）。
    ⚠️ 所以这个探针**不检查"色号等于多少"**，只检查**成对关系还成不成立**。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from wechat_assistant import theme                                # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print("  %s %-52s %s" % ("[通过]" if ok else "[失败]", name, detail))
    if not ok:
        FAIL.append(name)


def rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def lum(h):
    """感知亮度（0~255）。"""
    r, g, b = rgb(h)
    return 0.299 * r + 0.587 * g + 0.114 * b


def rel_lum(h):
    def f(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb(h)
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    la, lb = rel_lum(a), rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def main():
    p = theme.palette("light")

    print("== 浅色色板（%s）==" % theme.LIGHT)
    for k in ("bg", "surface", "surface_alt", "border", "shell_border",
              "card_line", "hover", "text", "text_dim", "text_faint", "accent"):
        print("   %-13s %s   亮度 %5.1f" % (k, p[k], lum(p[k])))

    # ⚠️ 判据用的是**感知亮度差**，不是一个绝对阈值。
    #    门槛 8 是量出来的：深色那套里"标题栏 vs 聊天背景"是 42 vs 30，
    #    差 12 —— 那个差在人眼里是明确分得开的。取 8 作为"分得开"的下限。
    print("\n== 1. 相邻两档灰要分得开（差 < 8 就等于同一块）==")
    pairs = [
        ("标题栏/侧栏(surface_alt) vs 正文底(bg)", "surface_alt", "bg"),
        ("悬停(hover) vs 正文底(bg)", "hover", "bg"),
        # ⚠️ 这条是调浅时最容易自伤的一条：hover 和 surface_alt 是
        #    "悬停的列表项"和"侧栏底色"的关系，两者贴在同一个区域里。
        #    原来的 hover(#F0F0F0) 改浅之后跟侧栏底色只差 0.6，等于没有悬停 ——
        #    这正是"只改一个键"会踩的坑。
        ("悬停(hover) vs 侧栏底(surface_alt)", "hover", "surface_alt"),
    ]
    for label, a, b in pairs:
        d = abs(lum(p[a]) - lum(p[b]))
        check(label, d >= 8.0, "亮度差 %.1f  (%s vs %s)" % (d, p[a], p[b]))

    # ⚠️ **故意不检查** "卡片(surface) vs 正文底(bg)"：两者只差 5.0，
    #    但那**是设计**，不是缺陷 —— 微信浅色下"聊天背景"和"对方气泡"本来就是
    #    同一个色（见 theme.py 顶部实测记录）。我们的卡片并排、没有头像，
    #    所以改用**白底 + `card_line` 描边**来区分，**靠的不是底色差**。
    #    所以下面第 3 节查"描边够不够明显"才是这条设计的正确判据。
    print("   （跳过：卡片 vs 正文底的底色差是**故意**很小的，见第 3 节的描边判据）")

    print("\n== 2. 外框线要比它周围的东西深（不然面板没有边）==")
    check("外框(shell_border) 明显深于正文底(bg)",
          lum(p["bg"]) - lum(p["shell_border"]) >= 20.0,
          "%.1f" % (lum(p["bg"]) - lum(p["shell_border"])))
    check("外框(shell_border) 不浅于分隔线(border)",
          lum(p["shell_border"]) <= lum(p["border"]),
          "外框 %.1f vs 分隔 %.1f" % (lum(p["shell_border"]), lum(p["border"])))

    print("\n== 3. 卡片描边必须还看得出（浅色下全靠它区分卡片）==")
    # 卡片是白底铺在 #FAFAFA 上，两者亮度只差 5 —— 描边是唯一的分界。
    check("卡片描边(card_line) 明显深于卡片底(surface)",
          lum(p["surface"]) - lum(p["card_line"]) >= 15.0,
          "%.1f" % (lum(p["surface"]) - lum(p["card_line"])))
    check("卡片描边(card_line) 明显深于正文底(bg)",
          lum(p["bg"]) - lum(p["card_line"]) >= 10.0,
          "%.1f" % (lum(p["bg"]) - lum(p["card_line"])))

    print("\n== 5. 开关（胶囊）要看得出来（2026-10-14 用户报「浅色下不明显」）==")
    # ⚠️ 原来关态轨道填 `surface`(#FFFFFF)、滑块写死 `#FFFFFF` —— 白底白滑块。
    #    这一节按两条量：轨道要跟页面底分得开，滑块要跟轨道分得开。
    for label, fg, bg, need in [
        ("关态滑块(switch_knob) vs 轨道(switch_off)", "switch_knob", "switch_off", 15.0),
        # ⚠️ 比的是**行底色 `bg`**，不是 `surface`：量过设置页的行底就是 `bg`
        #    （深色 #1E1E1F / 浅色 #FAFAFA）。拿 `surface` 比会把深色那套误报成
        #    "轨道和底同色"（深色的 `switch_off` 恰好等于 `surface`）。
        ("关态轨道(switch_off) vs 行底色(bg)", "switch_off", "bg", 8.0),
        ("开态滑块(switch_knob) vs 品牌绿(accent)", "switch_knob", "accent", 10.0),
    ]:
        d = abs(lum(p[fg]) - lum(p[bg]))
        check(label, d >= need, "亮度差 %.1f（要 >= %.0f）  %s vs %s"
              % (d, need, p[fg], p[bg]))

    print("\n== 4. 文字压在它真正的底色上要够读（WCAG >= 3.0）==")
    # ⚠️ 这里必须写**文字实际铺在哪个底上**，不是笼统地拿 bg 比。
    for label, fg, bg in [
        ("正文(surface) 上的主文字", "text", "surface"),
        ("正文底(bg) 上的主文字", "text", "bg"),
        ("正文底(bg) 上的次要文字(text_dim)", "text_dim", "bg"),
        ("侧栏(surface_alt) 上的次要文字(text_dim)", "text_dim", "surface_alt"),
    ]:
        r = contrast(p[fg], p[bg])
        check(label, r >= 3.0, "%.2f:1  (%s on %s)" % (r, p[fg], p[bg]))

    # ⚠️ 下面两条是**记录下来的已知取舍**，不是被悄悄放过的 bug。
    f = contrast(p["text_faint"], p["surface_alt"])
    print("   [注意] 侧栏(surface_alt) 上的浅文字(text_faint)  %.2f:1"
          % f)
    print("          —— 够不到 3.0，而且**在这个底色上根本够不到**：")
    print("             要 3.0 就得深到 #898989，那已经等于 text_dim(#888888)，")
    print("             两个语义键会合并成一个。")
    print("             `text_faint` 只用在**提示语/禁用态**（settings_ui 的脚注、")
    print("             session_ui 的 _tip、widgets.py 的禁用按钮文字），")
    print("             不用在「必须读清」的正文上。")
    print("             ℹ️ 这次把侧栏调浅其实**改善了**它：%s(#EEEEF0) 上 %.2f:1，"
          % (p["surface_alt"], f))
    print("                旧的 #DCDCE1 上只有 1.55:1。")
    print("             → 想把脚注做得更好读，应该**把那个标签换成 text_dim**，")
    print("               而不是把 text_faint 整体调深（那会牵动禁用态）。")

    g = contrast(p["accent_text"], p["accent"])
    print("   [注意] 主按钮：白字压绿  %.2f:1" % g)
    print("          —— **这是微信自己的样子**（微信绿按钮就是白字），")
    print("             我们是复刻配色不是做无障碍最优解，所以不做成失败。")
    print("             深色那套同样如此。要改就是「不按微信来」，是产品选择。")

    print("\n== 5. 浅色下不该出现「比底色还深的灰块」==")
    # 这就是用户报的那件事：一个"本该是浅灰"的面板比正文底深太多。
    # 深色那套里 标题栏(42) vs 聊天底(30) 差 12，所以浅色也按同量级看。
    d = lum(p["bg"]) - lum(p["surface_alt"])
    check("侧栏/标题栏 不比正文底深太多（<= 25）", d <= 25.0, "%.1f" % d)

    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
