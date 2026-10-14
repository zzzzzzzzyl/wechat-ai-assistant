# -*- coding: utf-8 -*-
"""对比「UIA 后端」和「OCR 后端」在**非聊天页面**上分别读到什么。

    python probes/probe_uia_vs_nonchat.py

起因（用户 2026-10-11）：「那原来 uia 树是如何拦截这些的？」

猜想（写在代码里、可以验证的）：**UIA 是靠"控件类型"天然过滤的** ——
它读的是「聊天标题控件」「消息控件」，而不是"像素"。
公众号 / 微信支付 这类页面**根本没有那些控件**，于是：

    · UIA：`read_chat_title()` 返回 ""  -> `Recorder.tick` 直接「没会话名」跳过
           -> **一个字节都不读、不报任何错**（看起来就是"当前没有打开的会话"）
    · OCR：所有东西都是像素 -> 文章卡片看起来就是"内容" -> 被读成消息

也就是说这不是"UIA 按名字拦了它们"，而是 **UIA 看不到它们**。
这个差别决定了 OCR 后端必须**显式**加一层过滤（按名字挡、或判断"是不是聊天"）。

⚠️ 纯只读：UIA 只做 FindAll、OCR 只做 PrintWindow，都不碰微信状态。
⚠️ 本机 UIA 是可用的（实测那个 2×2 交叉实验里"我的机器 + 我的号"✅）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi as _dpi  # noqa: E402

_dpi.enable()

from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.reader import WeChatReader, list_windows, pick_window  # noqa: E402
from wechat_assistant.uia_worker import UiaWorker  # noqa: E402


def main():
    cfg = load_config()
    win = pick_window(list_windows(cfg), cfg)
    if win is None:
        print("挑不出微信主窗口")
        return 1
    hwnd = win["hwnd"]
    print("微信窗口 hwnd=0x%X" % hwnd)

    worker = UiaWorker(timeout=float(cfg.get("runtime.uia_timeout", 20.0)),
                       logger=lambda m: None)
    try:
        rd = WeChatReader(cfg, worker, logger=lambda m: None)

        n = rd.uia_health(hwnd)
        print("\n== UIA 体检 ==")
        print("   后代控件总数 = %d（%s）"
              % (n, "正常，控件树在" if n >= 20
                 else "**几乎是空的** —— 这台机器/这个号读不到 UIA"))

        title = rd.read_chat_title(hwnd)
        print("\n== UIA 读出来的 ==")
        print("   read_chat_title() = %r" % title)
        snap = rd.read_chat(hwnd)
        if snap is None:
            print("   read_chat()       = None")
        else:
            print("   read_chat(): 标题=%r 群聊=%s 消息 %d 条 时间分隔 %d 个"
                  % (snap.title, snap.is_group, len(snap.messages),
                     len(snap.markers)))
            for m in snap.messages[:5]:
                print("      [%s] %r" % ("我" if m.is_self else "对方",
                                         (m.text or "")[:40]))

        # 关键判据：**消息类**控件在不在（这是真正的拦截点）
        id_title = cfg.get("wechat.node.chat_title", "current_chat_name_label")
        cls_msg = cfg.get("wechat.cls.msg_text", "mmui::ChatTextItemView")
        cls_ref = cfg.get("wechat.cls.msg_bubble_ref", "mmui::ChatBubbleReferItemView")
        cls_sess = cfg.get("wechat.cls.session_cell", "mmui::ChatSessionCell")
        counts = rd.count_controls_by_class(hwnd, [cls_msg, cls_ref, cls_sess])
        print("\n== 为什么 UIA 读不到（关键判据）==")
        print("   标题控件要求 AutomationId 含 %r  -> 这个页面上%s"
              % (id_title, "**有**（所以标题读得到）" if title else "没有"))
        print("   几个「只有聊天里才有」的控件各有多少个：")
        for k, v in counts.items():
            note = "  ← **没有它就没有消息**" if k == cls_msg and v == 0 else ""
            print("      %-36s %s 个%s" % (k, v, note))

        print("\n== 结论 ==")
        nmsg = len(snap.messages) if snap else 0
        if title and nmsg == 0 and counts.get(cls_msg) == 0:
            print("   ⭐ **拦截点是「消息控件」，不是标题**：")
            print("      · 标题 UIA 照样读得到（%r）" % title)
            print("        -> 所以公众号**仍然会**出现在会话列表里（它确实是个会话名）")
            print("      · 但 %s 在这个页面上**一个都没有**" % cls_msg)
            print("        -> `read_chat()` 返回 **0 条消息**")
            print("        -> `Recorder.tick` 里 `if not snap.messages: return 0`")
            print("           直接跳过 —— **一个字节都不写库，也不报错**")
            print("      · 所以「UIA 怎么拦住公众号的」答案是：**靠控件类型**。")
            print("        非聊天页面上没有聊天控件，于是天然读不出东西 ——")
            print("        这个过滤是**免费的、隐含的**，代码里一行都没写。")
            print("   ⚠️ 而 OCR 后端**所有东西都是像素**，没有这层结构 ——")
            print("      文章卡片看起来就是内容。所以必须**显式**补上这层过滤")
            print("      （→ 已加 `sessions.disabled_contains` 按名字挡）。")
        elif title:
            print("   标题读到了 %r，消息 %d 条，%s = %s 个 —— 和预期不同，另找原因。"
                  % (title, nmsg, cls_msg, counts.get(cls_msg)))
        else:
            print("   UIA 标题为空 -> `Recorder.tick` 按「没会话名」直接跳过。")
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
