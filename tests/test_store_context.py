# -*- coding: utf-8 -*-
"""历史库 + 上下文组装 测试。

覆盖：
    - MessageStore：存/取/去重/容量裁剪/清空
    - find_anchor：鼠标位置 -> 消息下标
    - collect_target_block：往上合并连续的同一个人说的消息
    - merge_with_store：窗口可见历史 + 库里历史 的拼接
    - ChatSnapshot.at_bottom：滚动位置判定（见 test_scroll.py，这里做交叉验证）

用法：python tests/test_store_context.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.context import (block_to_dicts, block_to_text, collect_default_block,
                                      collect_target_block, collect_visible_block,
                                      find_anchor, merge_with_store)
from wechat_assistant.reader import ChatSnapshot, MessageItem
from wechat_assistant.store import MessageStore, make_fingerprint

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def msg(text, is_self=None, sender=None, y=None, h=84, x1=0, x2=500):
    rect = (x1, y, x2, y + h) if y is not None else None
    m = MessageItem(text=text, cls="mmui::ChatTextItemView", rect=rect,
                    sender_name=sender, is_self=is_self)
    return m


# ---------------------------------------------------------------- store

def test_store():
    print("\n1. 历史库 MessageStore")
    path = os.path.join(tempfile.gettempdir(), "test_hist_%d.db" % os.getpid())
    if os.path.exists(path):
        os.remove(path)
    st = MessageStore(path, max_per_session=5)

    msgs = [msg("你好", False), msg("在吗", True), msg("在的", False)]
    n = st.save_messages("张三", msgs)
    check("存入 3 条", n == 3, "新增 %d" % n)
    check("count 正确", st.count("张三") == 3)
    got = st.get_recent("张三", 10)
    check("按时间正序返回", [r["content"] for r in got] == ["你好", "在吗", "在的"],
          str([r["content"] for r in got]))
    check("is_self 字段正确", got[1]["is_self"] == 1 and got[0]["is_self"] == 0)

    n2 = st.save_messages("张三", msgs)
    check("重复消息不重复存", n2 == 0, "第二次新增 %d" % n2)

    check("会话之间互不干扰", st.count("李四") == 0)

    # 容量裁剪
    for i in range(10):
        st.save_messages("张三", [msg("追加%d" % i, False)])
    check("容量裁剪到 5 条", st.count("张三") == 5, "实际 %d" % st.count("张三"))
    check("保留的是最新的",
          st.get_recent("张三", 1)[0]["content"] == "追加9",
          st.get_recent("张三", 1)[0]["content"])

    last = st.last_row("张三")
    check("last_row 拿到最后一条", last and last["content"] == "追加9")

    st.clear("张三")
    check("clear 清空", st.count("张三") == 0)
    st.close()
    try:
        os.remove(path)
    except Exception:
        pass


def test_sender_upgrade():
    """⚠️ 2026-10-14：**发送者是"后补"的**，别让它变成重复行，但也别把两个人并成一条。

    实测（用户快速上翻历史的日志 + 库）：
      · UIA 的群昵称靠 `SenderResolver.resolve()` 逐拍 OCR 昵称那一小块，
        窗口最小化/遮挡那一拍读不出来 -> 先写成空（或「群友」），下一拍补上真名；
      · 而指纹含 `sender_name` -> 两次指纹不同 -> `UNIQUE(session_key, fingerprint)`
        去重失效 -> 同一句话存 3~4 份（库里 `'动画表情'` 3 份、sender 各不相同）。
    判据（用户 2026-10-14 选的）：**旧行没有名字 -> 升级；两个已知的不同人说同一句话
    -> 留两条**。
    """
    print("\n1b. 发送者后补：升级而不是重复，但不同的人算两条")
    path = os.path.join(tempfile.gettempdir(), "test_upg_%d.db" % os.getpid())
    if os.path.exists(path):
        os.remove(path)
    st = MessageStore(path, max_per_session=100)

    # ① 先记成"没读出发送者"（群聊里 OCR 没认出昵称的样子）
    n = st.save_messages("群", [msg("动画表情", False, sender="")])
    check("先写入 1 条（发送者未知）", n == 1 and st.count("群") == 1, "新增 %d" % n)

    # ② 下一拍补上了真名 -> 必须**升级那一条**，不是新增
    n = st.save_messages("群", [msg("动画表情", False, sender="李四")])
    rows = st.get_recent("群", 10)
    check("发送者补上 -> 升级，不新增", n == 0 and st.count("群") == 1,
          "新增 %d，库里 %d 条" % (n, st.count("群")))
    check("名字真的写进去了", rows and rows[0]["sender_name"] == "李四",
          repr(rows[0]["sender_name"] if rows else None))
    # ⚠️ 指纹必须**跟着一起更新**：不然这行的指纹还是"没名字"时的值，
    #    下一拍再读到同一条（这次带名字）时新指纹对不上 -> 又插一份（等于没修）。
    #    判据用"再存一次同样的" ：0 新增 = 指纹一致 ✓
    n = st.save_messages("群", [msg("动画表情", False, sender="李四")])
    check("指纹也跟着更新了（再存一次不新增）",
          n == 0 and st.count("群") == 1, "新增 %d，库里 %d 条" % (n, st.count("群")))

    # ③ 「群友」也算"还没读出名字" -> 同样升级
    st.save_messages("群2", [msg("在吗", False, sender="群友")])
    n = st.save_messages("群2", [msg("在吗", False, sender="王五")])
    rows2 = st.get_recent("群2", 10)
    check("「群友」-> 真名：同样升级", n == 0 and st.count("群2") == 1
          and rows2[0]["sender_name"] == "王五",
          "新增 %d / %s" % (n, rows2[0]["sender_name"] if rows2 else None))

    # ④ ⚠️ 两个**已知的**不同人说同一句话 -> 必须留两条（用户明确要求）
    st.save_messages("群3", [msg("图片", False, sender="李四")])
    n = st.save_messages("群3", [msg("图片", False, sender="王五")])
    rows3 = st.get_recent("群3", 10)
    check("不同的人说同一句话 -> **两条**", n == 1 and st.count("群3") == 2,
          "新增 %d，库里 %d 条" % (n, st.count("群3")))
    check("两条的发送者各是各的",
          sorted(r["sender_name"] for r in rows3) == ["李四", "王五"]
          if len(rows3) == 2 else False, str([r["sender_name"] for r in rows3]))

    # ⑤ 同一个**没名字**的发送者说了两次同样的内容 -> 指纹一样 -> 只留一条
    #    （这不是丢数据：没名字时本来分不出是两个人还是同一个人说了两遍；
    #     "两个人"要能分辨，前提是**名字已经读出来**——见 ④）
    st.save_messages("群4", [msg("哈哈", False, sender=""),
                             msg("哈哈", False, sender="")])
    check("都没名字时同内容只留一条（指纹相同）",
          st.count("群4") == 1, "库里 %d 条" % st.count("群4"))

    st.close()
    try:
        os.remove(path)
    except Exception:
        pass


def test_fingerprint():
    print("\n2. 指纹")
    a = make_fingerprint(True, None, "行")
    b = make_fingerprint(True, None, "行")
    c = make_fingerprint(False, None, "行")
    d = make_fingerprint(None, None, "行")
    check("同样内容同指纹", a == b)
    check("自己/别人 指纹不同", a != c)
    check("未判定与已判定不同", d != a and d != c)


# ---------------------------------------------------------------- context

def test_anchor():
    print("\n3. 鼠标锚点定位")
    snap = ChatSnapshot(container_rect=(0, 100, 500, 500), messages=[
        msg("第一条", False, y=100),
        msg("第二条", True, y=184),
        msg("第三条", False, y=268),
    ])
    check("指到第一条", find_anchor(snap, (250, 140)) == 0)
    check("指到第二条", find_anchor(snap, (250, 220)) == 1)
    check("指到第三条", find_anchor(snap, (250, 300)) == 2)
    check("指到空白处", find_anchor(snap, (250, 480)) is None)
    check("坐标为空", find_anchor(snap, None) is None)
    check("消息没 rect 时不崩",
          find_anchor(ChatSnapshot(messages=[msg("x")]), (10, 10)) is None)


def test_block():
    print("\n4. 连续消息块合并（要回的常常是一段而不是一句）")
    snap = ChatSnapshot(messages=[
        msg("早", True, y=100),                      # 我发的 —— 作为话题分隔
        msg("你昨天为什么不回我消息", False, y=184),
        msg("你每次都这样", False, y=268),
        msg("算了 你爱怎么想怎么想吧", False, y=352),
        msg("在忙啊", True, y=436),
        msg("手机静音了", True, y=520),
    ])
    block, hist, why = collect_target_block(snap, 3)
    check("锚点是最后一条对方消息时，往上合并同方的 3 条",
          [m.text for m in block] == ["你昨天为什么不回我消息", "你每次都这样",
                                      "算了 你爱怎么想怎么想吧"],
          str([m.text for m in block]))
    check("历史是目标块之前的部分", [m.text for m in hist] == ["早"],
          str([m.text for m in hist]))

    block2, hist2, _ = collect_target_block(snap, 5)
    check("锚点是自己发的，只合并自己连着的 2 条",
          [m.text for m in block2] == ["在忙啊", "手机静音了"],
          str([m.text for m in block2]))
    check("此时历史包含对方那一段",
          [m.text for m in hist2][-1] == "算了 你爱怎么想怎么想吧")

    # 未判定（None）时不猜，单独成块
    snap2 = ChatSnapshot(messages=[msg("A", None, y=100), msg("B", None, y=184)])
    b3, _, _ = collect_target_block(snap2, 1)
    check("未判定时不合并", len(b3) == 1, str([m.text for m in b3]))

    # 上限
    many = ChatSnapshot(messages=[msg("m%d" % i, False, y=100 + i * 84) for i in range(20)])
    b4, _, _ = collect_target_block(many, 19, max_block=5)
    check("受 target_max_block 限制", len(b4) == 5, str(len(b4)))

    check("block_to_text 可读", "算了" in block_to_text(block))
    check("block_to_dicts 结构正确",
          all(set(d) == {"who", "text"} for d in block_to_dicts(block)))


def test_self_last_not_blocked():
    """⚠️ 回归：**最后一条是自己发的，也照常生成**（2026-10-13 用户要求）。

    用户原话：「如果最后一条如果是自己发的，也不要拦截，毕竟也可以跟着自己的话」说
    —— 场景是自己刚说完一句、想**接着补一句**，或者让 AI 顺着自己的话往下接。

    以前 `collect_visible_block` / `collect_default_block` 遇到这种就返回 `None`
    （"没什么要回的"），于是按热键只会看到「最后一条是你自己发的，无需生成回复」，
    而**屏幕上明明有整段对话** —— 看起来就像功能坏了。
    """
    print("\n6b. 最后一条是自己发的：**不拦截**（照常生成）")
    snap = ChatSnapshot()
    snap.messages = [msg("在吗", is_self=False, y=0),
                     msg("在的", is_self=True, y=100),
                     msg("我马上到", is_self=True, y=200)]

    block, _hist, why = collect_visible_block(snap)
    check("collect_visible_block 不返回 None", block is not None, why)
    check("  整段都当目标（三条都在）",
          block is not None and len(block) == 3, str(len(block or [])))
    check("  说明里不再写「最后一条是自己发的」", why != "最后一条是自己发的", why)

    block2, _h2, _w2 = collect_default_block(snap)
    check("collect_default_block 也不拦截（照常取最后一条）",
          block2 is not None and len(block2) == 1, str(block2))

    # ⚠️ 另一头也要保住：**真的没有消息**时仍然返回空 —— 别把"没消息"也当"放行"，
    #    否则会拿一个空段去问模型。
    empty = ChatSnapshot()
    empty.messages = []
    b3, _h3, w3 = collect_visible_block(empty)
    check("空会话仍然返回空（不能把'没消息'放行）",
          b3 == [] and w3 == "无消息", "%r %s" % (b3, w3))

    blank = ChatSnapshot()
    blank.messages = [msg("   ", is_self=False, y=0)]
    b4, _h4, w4 = collect_visible_block(blank)
    check("只有空白文本 -> 也算无消息", b4 == [] and w4 == "无消息", w4)


def test_merge():
    print("\n5. 窗口可见历史 + 库里历史 的拼接")
    stored = [{"who": "我", "text": "库里1"}, {"who": "对方", "text": "库里2"},
              {"who": "我", "text": "库里3"}]
    visible = [{"who": "对方", "text": "库里3"}, {"who": "我", "text": "窗口4"}]
    # 可见的第一条是 "库里3"（who 不同，这里模拟同名不同人）
    merged = merge_with_store(visible, stored, limit=10)
    check("拼接结果非空", len(merged) > 0, str([m["text"] for m in merged]))
    check("窗口里的最新内容在尾部", merged[-1]["text"] == "窗口4")

    # 限制条数时保留最近的
    merged2 = merge_with_store(visible, stored, limit=2)
    check("limit 生效", len(merged2) == 2, str([m["text"] for m in merged2]))
    check("limit 时保留最新", merged2[-1]["text"] == "窗口4")

    check("空输入不崩", merge_with_store([], [], 5) == [])
    check("只有库里时可用",
          [m["text"] for m in merge_with_store([], stored, 10)] ==
          ["库里1", "库里2", "库里3"])


def test_ai_contamination():
    print("\n6. 防止 AI 自我污染：AI 建议过的消息不能当口吻样本")
    # 用户发现的链条：
    #   AI 生成建议 -> 用户复制粘贴发出去 -> 这条进了库 ->
    #   下一轮被当成「我平时说话的样子」-> **AI 开始模仿自己的输出**，
    #   几轮下来 AI 腔越来越重。
    # 对策：写入时给这些消息打 from_ai=1，取样口吻时排除掉。
    path = os.path.join(tempfile.gettempdir(), "test_ai_%d.db" % os.getpid())
    if os.path.exists(path):
        os.remove(path)
    st = MessageStore(path, max_per_session=100)

    ai_reply = "我把票看了 中秋尽量回"
    st.note_ai_suggestions([ai_reply, "想回 你别念叨了"])

    st.save_messages("张三", [
        msg("行", True),                 # 我自己随口说的 -> 应当算样本
        msg(ai_reply, True),             # AI 建议的原文被发出去 -> 不算样本
        msg("嗯嗯", True),
        msg("想回 你别念叨了", True),      # 另一条 AI 建议 -> 不算样本
        msg("你昨天说的那个", False),      # 别人发的 -> 本来就不算
    ])
    rows = st.get_recent("张三", 10)
    check("AI 建议的消息被打了标记",
          [r["from_ai"] for r in rows] == [0, 1, 0, 1, 0],
          str([r["from_ai"] for r in rows]))

    own = st.own_style_texts("张三", limit=10, max_len=60)
    check("口吻样本排除 AI 的", ai_reply not in own and "想回 你别念叨了" not in own,
          str(own))
    check("口吻样本保留自己真实说的", own == ["行", "嗯嗯"], str(own))

    # 它们**仍然在对话历史里**（那确实是用户发出去的真实消息，上下文要有）
    check("AI 建议的消息仍在历史里（只是不当样本）",
          ai_reply in [r["content"] for r in rows])

    # 没标记过的正常情况
    st.save_messages("李四", [msg("随便说说", True), msg("哈哈哈", True)])
    check("没有 AI 标记时会话不受影响",
          st.own_style_texts("李四", limit=10, max_len=60) == ["随便说说", "哈哈哈"],
          str(st.own_style_texts("李四", limit=10, max_len=60)))

    st.close()
    try:
        os.remove(path)
    except Exception:
        pass


def main():
    print("=" * 76)
    print("历史库 + 上下文组装 测试")
    print("=" * 76)
    test_store()
    test_sender_upgrade()
    test_fingerprint()
    test_anchor()
    test_block()
    test_self_last_not_blocked()
    test_merge()
    test_ai_contamination()
    print("\n" + "=" * 76)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
