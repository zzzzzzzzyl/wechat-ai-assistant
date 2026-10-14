# -*- coding: utf-8 -*-
"""上下文组装：决定「这次要回复什么」和「给 AI 看哪些历史」。

两种取法：

  ① 默认（鼠标不在消息上）
     回复**可见的最新的那条**。前提是列表滚到了底部 —— 否则可见的最后一条
     只是中间某条，回它就是回一条早就回过的旧消息（见 ChatSnapshot.at_bottom）。

  ② 鼠标停在某条消息上
     以那条为**锚点**，往上合并「连续的同一个人说的消息」当成一个
     **回复目标块**。因为真实聊天里要回的往往不是一句话，而是一段：

         对方: 你昨天为什么不回我消息   ┐
         对方: 你每次都这样            ├─ 目标块（三句连着，算一组）
         对方: 算了 你爱怎么想怎么想吧  ┘  ← 鼠标停在这
         我:   在忙啊                  ← 中间夹了自己的话，块到此断开

     锚点本身就是最强的降噪手段：它把「用户想回应什么」直接告诉程序了，
     不用去猜。
"""

from typing import List, Optional, Tuple

CURSOR_UNKNOWN = None


def find_anchor(snap, point: Optional[Tuple[int, int]]) -> Optional[int]:
    """返回鼠标所指消息在 snap.messages 里的下标；没指到任何一条则返回 None。"""
    if not point:
        return None
    x, y = point
    # 从后往前找：消息行在竖直方向可能重叠（虚拟列表），取最靠下的那条更符合直觉
    for i in range(len(snap.messages) - 1, -1, -1):
        r = snap.messages[i].rect
        if not r:
            continue
        if r[0] <= x <= r[2] and r[1] <= y <= r[3]:
            return i
    return None


def collect_target_block(snap, anchor_index: int, max_block: int = 8):
    """从锚点往上合并连续的同一个人发的消息。

    返回 (目标块, 目标块之前的历史消息, 判定说明)。
    """
    msgs = snap.messages
    if not (0 <= anchor_index < len(msgs)):
        return [], list(msgs), "锚点无效"

    anchor = msgs[anchor_index]
    block = [anchor]
    i = anchor_index - 1

    # 只有「明确知道是哪一方」才合并；未判定（None）时不猜，单独成块
    if anchor.is_self is not None:
        while i >= 0 and len(block) < max_block:
            prev = msgs[i]
            if prev.is_self != anchor.is_self:
                break
            block.insert(0, prev)
            i -= 1

    history = msgs[:i + 1]
    return block, history, "鼠标锚点"


def collect_visible_block(snap):
    """把**当前屏幕上的全部对话**当成回复目标 —— 不再用鼠标锚点。

    返回 (目标块, 历史消息, 说明)。

    ⚠️ 为什么放弃鼠标锚点：
      · 它要求"先把鼠标移到那句话上、再按热键"，平白多一个动作；
      · 鼠标位置**完全不可靠** —— 实测它抓到过我们自己的「看提示词」窗口里的
        文字（`第 0 条「========== system（883 字）」`），把提示词全文当成了
        对方的消息，一次调用贵 3 倍。
      · 用户看的就是屏幕上这些，屏幕内容本身就是天然的"当前上下文"。

    块就是可见的全部消息，所以返回的 history 是**空的** ——
    更早的内容由历史库补上（那边再做时间切段/概要）。

    ⚠️ **不再因为“最后一条是自己发的”而返回 None**（2026-10-13 用户要求）：
       用户原话「如果最后一条如果是自己发的，也不要拦截，毕竟也可以跟着自己的话说」。
       场景很实在 —— 自己刚说完一句，想**接着补一句**，或者想让 AI
       顺着自己的话往下接。硬拦着会让人以为功能坏了
       （**屏幕上明明有对话**）。
    ⚠️ 返回形状仍然是“统一 list 或 None”（下面那条历史原因）。
    以前这里返回过单个对象而调用处按 list 用，实机上报过
    `AttributeError: 'list' object has no attribute 'text'`。
    """
    msgs = [m for m in snap.messages if (m.text or "").strip()]
    if not msgs:
        return [], [], "无消息"
    return list(msgs), [], "当前屏幕的全部对话"


def collect_default_block(snap):
    """默认取法：可见的最后一条作为目标，它之前的作为历史。

    返回 (目标块, 历史消息, 说明)。**目标块统一是 list**（可能为 None 表示
    「最后一条是自己发的，不需要回」），和 collect_target_block 保持同样的形状 ——
    之前这里返回的是单个对象，调用处却按 list 用，实机上报过
    `AttributeError: 'list' object has no attribute 'text'`。
    """
    msgs = [m for m in snap.messages if (m.text or "").strip()]
    if not msgs:
        return [], [], "无消息"
    last = msgs[-1]
    # ⚠️ 同 `collect_visible_block`：**最后一条是自己发的也照常生成**
    #    （2026-10-13 用户要求 —— 可以“跟着自己的话说”）。
    return [last], msgs[:-1], "可见的最后一条"


def block_to_text(block: List) -> str:
    """把目标块渲染成一段可读文本（给 AI 用，也给悬浮窗显示用）。"""
    lines = []
    for m in block or []:
        who = getattr(m, "who", "?")
        text = (getattr(m, "text", "") or "").strip()
        if text:
            lines.append("%s: %s" % (who, text))
    return "\n".join(lines)


def block_to_dicts(block: List) -> List[dict]:
    """目标块 -> [{'who':..,'text':..}]，便于传给 AI 模块。"""
    out = []
    for m in block or []:
        text = (getattr(m, "text", "") or "").strip()
        if text:
            out.append({"who": getattr(m, "who", "?"), "text": text})
    return out


def history_to_dicts(messages: List) -> List[dict]:
    """历史消息 -> [{'who':..,'text':..}]。"""
    out = []
    for m in messages or []:
        text = (getattr(m, "text", "") or "").strip()
        if text:
            out.append({"who": getattr(m, "who", "?"), "text": text})
    return out


def merge_with_store(visible_history: List[dict], stored_history: List[dict],
                     limit: int) -> List[dict]:
    """把「窗口里可见的历史」和「库里存的历史」拼起来，取最近 limit 条。

    为什么要拼：
        库里可能有几十条（之前触发时攒下的），但**可能缺最近这几条**
        （如果上次触发到现在又聊了几句）；而窗口里正好有最新的几条。
        所以用可见的作为尾部，库里更早的作为前缀，按内容去掉重叠部分。
        注意必须**先做合并再取最近 limit 条** —— 反过来会把库里的都丢掉。
    """
    if limit <= 0:
        return list(visible_history or [])

    vis = list(visible_history or [])
    st = list(stored_history or [])

    # 在库里找可见历史的第一条，作为重叠点：库中该条之后的都不要（要用可见的版本）
    overlap = 0
    if vis:
        head = vis[0]
        for idx in range(len(st) - 1, -1, -1):
            if st[idx].get("text") == head.get("text") and \
                    st[idx].get("who") == head.get("who"):
                overlap = idx
                break
        merged = st[:overlap] + vis
    else:
        merged = st

    return merged[-limit:] if len(merged) > limit else merged
