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

from .textfilter import is_placeholder

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

    ⚠️ **"整块都要回"不等于"每一行都要回"**（2026-10-17）：这一块里可能混着
       `[图片]`、引用卡片、以及我自己刚说的话。真正要回应的是
       `pick_reply_anchor()` 找出来的那条 —— 用 `render_current_block()` 渲染，
       会在它前面标 `>> `。
    """
    msgs = [m for m in snap.messages if (m.text or "").strip()]
    if not msgs:
        return [], [], "无消息"
    return list(msgs), [], "当前屏幕的全部对话"


#: 这些 kind 不是"谁说的话" —— 不该被当成待回复的对话行
NONTEXT_KINDS = ("image", "emoji", "refer", "file", "link")


def is_dialogue(item) -> bool:
    """这条算不算「谁说的话」（可以当回复锚点、可以进历史）。

    ⚠️ 判 `kind` 而不是只判 text：图片/引用/卡片在 prompt 里也是
       `[图片]` 这种文字，但**回它没意义**（见 `render_current_block`）。
    ⚠️ 还要判**纯占位**（2026-10-17）：UIA 路径的动画表情就是一条
       `kind=text`、文本为 `动画表情`/`[动画表情]` 的消息 —— 只判 kind
       会让锚点落在表情上、模型被要求"回复一个表情"。
    """
    kind = getattr(item, "kind", "text") or "text"
    if kind in NONTEXT_KINDS:
        return False
    text = (getattr(item, "text", "") or "").strip()
    if not text:
        return False
    if is_placeholder(text):
        return False
    return True


def pick_reply_anchor(block) -> Optional[int]:
    """最后一条「**别人**说的正文」—— 这次真正要回应的那条。

    为什么不是"最后一条"（2026-10-17，用户反馈）：
      · 末尾常跟着 `[图片]`、`嗯`、`好的`，或者我自己刚补的一句；
      · 提示词第 6 条也写着「我:」不是待回复内容 —— 拿最后一条会自相矛盾；
      · 实测（`probes/diag_context_focus.py`）当前屏常常只有 2-5 条、一半是图片，
        模型只能抓住最后那几个字。

    找不到（全是「我」/全是图片）返回 None —— 由调用方提示"可以接着自己的话说"，
    **不要**伪造成"有锚点"。
    """
    for i in range(len(block or []) - 1, -1, -1):
        it = block[i]
        if not is_dialogue(it):
            continue
        if getattr(it, "is_self", None) is False:
            return i
    return None


def render_current_block(block, anchor_index: Optional[int] = None):
    """渲染 prompt 里的「当前屏幕」那一段，返回 (对话文本, 非文字说明, 锚点下标)。

    ```
    我: 在忙啊
    >> 李四: 你昨天为什么不回我消息        ← 要回应的就是这条
    李四: 算了 你爱怎么想怎么想吧
    李四: [图片]
    ```

    ⚠️ 锚点用 **`>> ` 前缀**表达，而不是改文字 —— 模型看得见，用户点【看提示词】
       也看得见；改文字会让"原文"和"喂进去的"对不上。

    ⚠️ **占位消息（图片 / 动画表情 / 文件…）保留在对话行里**（2026-10-17）：
       模型得知道"对方发的是一张图 / 一个表情"，否则它会以为对方没说话；
       但 `is_dialogue()` 排除了它们，所以**锚点不会落在占位上**。
       ⚠️ 视觉模型那句「对应上面第 N 个 `[图片]`」依赖 `[图片]` 文本在场 ——
          **别在这里把它们挪走或删掉**（历史里才过滤，见 `run_watch`）。

    第二个返回值现在恒为空串（留给将来做更细的分区），调用方按 `if nont:` 用即可。
    """
    block = list(block or [])
    if anchor_index is None:
        anchor_index = pick_reply_anchor(block)
    lines = []
    for i, it in enumerate(block):
        text = (getattr(it, "text", "") or "").strip()
        if not text:
            continue
        who = getattr(it, "who", "?")
        prefix = ">> " if (anchor_index is not None and i == anchor_index) else ""
        lines.append("%s%s: %s" % (prefix, who, text))
    return "\n".join(lines), "", anchor_index



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


def filter_history(items):
    """历史里丢掉**纯占位**消息（图片 / 动画表情 / 文件 / 转账…），当前屏不动。

    为什么只在历史里丢（2026-10-17 用户要求）：
      · 历史里的 `[图片]` / `[动画表情]` 只是噪声，占条数、占字数；
      · 而且**历史图片根本取不到像素**（`screens` 不存 rect，见已知边界 #11）——
        留着它只会让模型以为"对方发了张图，去回它"，而它其实什么都没看到。

    ⚠️ 当前屏**不能**用这个函数：那里必须保留（模型得知道对方发的是图/表情），
       见 `render_current_block`。
    """
    out = []
    for h in items or []:
        text = (h.get("text") or "").strip()
        if text and is_placeholder(text):
            continue
        out.append(h)
    return out


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
