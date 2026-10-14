# -*- coding: utf-8 -*-
"""屏快照存储 + 缝合算法的单元测试。

不依赖微信：直接构造「一屏」喂进去，看缝出来的顺序对不对。
运行：python tests/test_window.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.window import (DEDUP_LOOKBACK, ScreenStore, collapse_repeats,
                                     find_overlap, items_from_snapshot, signature,
                                     splice, stitch_frames, text_signature,
                                     to_prompt_dicts, who_of)
from wechat_assistant.reader import (ChatSnapshot, MessageItem, TimeMarker,
                                     is_time_text)

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  [通过] %s" % name)
    else:
        FAIL += 1
        print("  [失败] %s" % name)
        print("         期望: %r" % (want,))
        print("         实际: %r" % (got,))


def check_true(name, got, extra=""):
    global PASS, FAIL
    if got:
        PASS += 1
        print("  [通过] %s" % name)
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, extra))


def it(text, is_self=None, sender=None):
    return {"text": text, "is_self": is_self, "sender": sender, "kind": "text"}


def texts(items):
    return [x["text"] for x in items]


def new_store(keep=40):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return ScreenStore(path, keep=keep), path


# ------------------------------------------------------------------ 1. 基本写入

def test_basic(st):
    print("\n1. 基本写入与去重")
    st.append_screen("s1", [it("a"), it("b"), it("c")], at_bottom=True)
    check("写了 1 屏", st.count("s1"), 1)

    # 完全一样的一屏 -> 跳过
    wrote = st.append_screen("s1", [it("a"), it("b"), it("c")], at_bottom=True)
    check("一模一样的一屏不重复存", wrote, False)
    check("仍然只有 1 屏", st.count("s1"), 1)

    # 内容变了 -> 存
    wrote = st.append_screen("s1", [it("b"), it("c"), it("d")])
    check("内容变了就存", wrote, True)
    check("现在 2 屏", st.count("s1"), 2)

    check("空会话不写", st.append_screen("s2", []), False)
    check("空 key 不写", st.append_screen("", [it("a")]), False)


# ------------------------------------------------------------------ 2. 判定升级

def test_upgrade(st):
    print("\n2. 发送者判定升级（? -> 对方）要生效")
    st.append_screen("up", [it("在吗", None), it("在吗2", None)])
    st.append_screen("up", [it("在吗", False, "张三"), it("在吗2", False, "张三")])
    items, gap = st.stitch("up")
    check("缝出来还是 2 条", len(items), 2)
    check("用的是新判定（对方）", [who_of(x) for x in items], ["张三", "张三"])
    check("不是 ?", "?" not in [who_of(x) for x in items], True)
    check("没有缺口", gap, False)


# ------------------------------------------------------------------ 3. 底部来新消息

def test_new_at_bottom(st):
    print("\n3. 停在底部，来了新消息（屏幕整体上移）")
    st.append_screen("b1", [it("m1"), it("m2"), it("m3"), it("m4"), it("m5")])
    st.append_screen("b1", [it("m3"), it("m4"), it("m5"), it("m6"), it("m7")])
    items, gap = st.stitch("b1")
    check("顺序正确", texts(items), ["m1", "m2", "m3", "m4", "m5", "m6", "m7"])
    check("没有缺口", gap, False)
    check("没有重复", len(items), len(set(texts(items))))


# ------------------------------------------------------------------ 4. 往上翻历史

def test_scroll_up(st):
    print("\n4. 往上翻历史（应该前插，而不是接到库尾）")
    st.append_screen("u1", [it("m5"), it("m6"), it("m7"), it("m8"), it("m9")])
    st.append_screen("u1", [it("m1"), it("m2"), it("m3"), it("m4"), it("m5")])
    items, gap = st.stitch("u1")
    check("顺序正确（旧的在前）", texts(items),
          ["m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9"])
    check("没有缺口", gap, False)


# ------------------------------------------------------------------ 5. 完整滚动会话

def test_full_scroll(st):
    print("\n5. 模拟一次完整浏览：翻上去看历史，再翻回底部，然后来新消息")
    conv = ["m%02d" % i for i in range(1, 22)]
    # 时间顺序：先停在底部，再一路上翻，最后翻回底部 + 新消息
    st.append_screen("f1", [it(x) for x in conv[15:20]])   # m16..m20
    st.append_screen("f1", [it(x) for x in conv[13:18]])   # m14..m18
    st.append_screen("f1", [it(x) for x in conv[11:16]])   # m12..m16
    st.append_screen("f1", [it(x) for x in conv[9:14]])    # m10..m14
    st.append_screen("f1", [it(x) for x in conv[15:20]])   # 翻回底部 m16..m20
    st.append_screen("f1", [it(x) for x in conv[16:21]])   # 新消息 m17..m21
    items, gap = st.stitch("f1")
    check("缝出完整顺序", texts(items), conv[9:21])
    check("没有缺口", gap, False)


# ------------------------------------------------------------------ 6. 接不上

def test_no_overlap(st):
    print("\n6. 接不上的屏不要硬拼")
    st.append_screen("n1", [it("甲"), it("乙"), it("丙")])
    st.append_screen("n1", [it("完全无关1"), it("完全无关2"), it("完全无关3")])
    items, gap = st.stitch("n1")
    check("只保留最新那屏", texts(items), ["完全无关1", "完全无关2", "完全无关3"])
    check("标记了缺口", gap, True)


def test_min_overlap(st):
    print("\n7. 重合大小的边界")
    # 大幅滑动 -> 相邻两屏只重合 1 条，**必须能接上**（这是滑动补录的常见情况，
    # 早期把门槛设成 2 会把这批整批拒掉）
    st.append_screen("n0", [it("m5"), it("m6"), it("m7")])
    st.append_screen("n0", [it("m1"), it("m2"), it("m3"), it("m4"), it("m5")])
    items, gap = st.stitch("n0")
    check("只重合 1 条也能接上", texts(items),
          ["m1", "m2", "m3", "m4", "m5", "m6", "m7"])
    check("没有缺口", gap, False)

    # 单条重合碰上重复内容（「图片」满屏都是）-> 会被接上，但结果不变，
    # 不会把无关的 x1/x2 拼进来
    st.append_screen("n2", [it("图片"), it("x1"), it("x2")])
    st.append_screen("n2", [it("图片"), it("y1"), it("y2")])
    items, gap = st.stitch("n2")
    check("重复内容不会拼进无关条目", texts(items), ["图片", "y1", "y2"])

    # 两条重合时，follow 关系取**更新的那一屏**的说法
    st.append_screen("n3", [it("图片"), it("动画表情"), it("x1")])
    st.append_screen("n3", [it("图片"), it("动画表情"), it("y1")])
    items2, gap2 = st.stitch("n3")
    check("跟随更新的那一屏", texts(items2), ["图片", "动画表情", "y1"])


# ------------------------------------------------------------------ 8. 容量

def test_trim():
    print("\n8. 容量：每会话只留最近 K 屏")
    st, path = new_store(keep=3)
    for i in range(6):
        st.append_screen("t1", [it("m%d" % i)])
    check("只留 3 屏", st.count("t1"), 3)
    items, _ = st.stitch("t1")
    check("留下的是最新的", texts(items), ["m5"])
    st.close()
    os.unlink(path)


def test_max_items(st):
    print("\n9. max_items：只留最新的一段")
    st.append_screen("mi", [it("m%d" % i) for i in range(10)])
    items, _ = st.stitch("mi", max_items=4)
    check("只留最后 4 条", texts(items), ["m6", "m7", "m8", "m9"])


# ------------------------------------------------------------------ 10. 转换

def test_convert():
    print("\n10. 数据转换")
    check("who: 自己", who_of(it("x", True)), "我")
    check("who: 对方", who_of(it("x", False)), "对方")
    check("who: 对方带昵称", who_of(it("x", False, "李四")), "李四")
    check("who: 未判定", who_of(it("x", None)), "?")

    d = to_prompt_dicts([it("你好", True), it("", False), it("在吗", False, "张三")])
    check("转 prompt 结构", d, [{"who": "我", "text": "你好"},
                                {"who": "张三", "text": "在吗"}])

    s1 = signature([it("a", True), it("b", None)])
    s2 = signature([it("a", False), it("b", None)])
    check_true("签名含发送者判定（升级会被当成新的一屏）", s1 != s2)


def test_find_overlap():
    print("\n11. find_overlap / splice 边界")
    check("空列表返回 None", find_overlap([], [it("a")]), None)
    cur = [it("m1"), it("m2"), it("m3")]
    res = [it("m2"), it("m3"), it("m4")]
    m = find_overlap(cur, res)
    check("找到重合段 (1,0,2)", m, (1, 0, 2))
    check("拼接结果", texts(splice(cur, res, m)), ["m1", "m2", "m3", "m4"])


# ------------------------------------------------------------------ 时间分隔符

def tm(text, top=0):
    """造一个时间分隔条目（形状与 items_from_snapshot 产出的完全一致）。

    `date` 直接用 reader.TimeMarker 的真实判定，别在这里再写一套规则。
    """
    return {"text": text, "is_self": None, "sender": None,
            "kind": "time", "date": TimeMarker(text=text).is_date_level}


def test_time_text_regex():
    print("\n12. 时间分隔符的文本判定（reader.is_time_text）")
    for s in ("16:19", "0:05", "昨天", "昨天 23:28", "前天 09:00",
              "星期三", "9月20日", "2026年9月1日", "上午10:05"):
        check("认出 %r" % s, is_time_text(s), True)
    for s in ("我拍了拍 \"张三\" 变成了鲫鱼饼饼", "在吗", "嗯", "1234",
              "昨天怎么样", "16:19 你还在吗", ""):
        check("不误认 %r" % s, is_time_text(s), False)


def test_items_from_snapshot():
    print("\n13. 时间分隔符按竖直位置插进屏快照")
    snap = ChatSnapshot(title="t", is_group=False)
    snap.messages = [
        MessageItem(text="昨天的话", cls="mmui::ChatTextItemView", rect=(0, 10, 100, 90)),
        MessageItem(text="今天的话", cls="mmui::ChatTextItemView", rect=(0, 200, 100, 280)),
    ]
    snap.markers = [
        TimeMarker(text="昨天", rect=(0, 0, 100, 20)),
        TimeMarker(text="16:19", rect=(0, 150, 100, 180)),
    ]
    items = items_from_snapshot(snap)
    check("顺序 = 旧到新，分隔符插在正确位置", texts(items),
          ["昨天", "昨天的话", "16:19", "今天的话"])
    check("分隔符标了 kind=time", items[0].get("kind"), "time")
    check("「昨天」标了 date=True", items[0].get("date"), True)
    check("「16:19」标了 date=False", items[2].get("date"), False)
    check("消息没被标成 time", items[1].get("kind"), "text")

    check("时间分隔不进 prompt", to_prompt_dicts(items),
          [{"who": "?", "text": "昨天的话"}, {"who": "?", "text": "今天的话"}])
    check("who_of 对分隔符返回空串", who_of(items[0]), "")


def test_cut_at_date():
    print("\n14. 跨天的时间分隔 -> 切掉更早的内容")
    old = [it("前天的对话1"), it("前天的对话2"), tm("昨天"), it("昨天的话")]
    frames = [old + [tm("16:19"), it("今天的话1"), it("今天的话2")]]
    merged, gap = stitch_frames(frames, 60)
    # 规则是「从**最后一个跨天分隔**开始保留」——也就是最多保留最近那个日期组
    # （昨天）+ 今天。前天那部分被切掉，昨天留着（它还算"近期"）。
    check("切掉更早的日期组", texts(merged),
          ["昨天", "昨天的话", "16:19", "今天的话1", "今天的话2"])
    check("gap 不受切段影响", gap, False)

    # 多个跨天分隔 -> 只保留最后一个
    multi = [[tm("9月20日"), it("a"), tm("昨天"), it("b"), tm("16:19"), it("c")]]
    merged_m, _ = stitch_frames(multi, 60)
    check("多个跨天分隔只保留最后一个之后的", texts(merged_m),
          ["昨天", "b", "16:19", "c"])

    merged2, _ = stitch_frames(frames, 60, cut_at_date=False)
    check("关掉就不切", texts(merged2),
          ["前天的对话1", "前天的对话2", "昨天", "昨天的话",
           "16:19", "今天的话1", "今天的话2"])

    # 纯时间不切
    only_time = [[it("今天a"), tm("16:19"), it("今天b")]]
    merged3, _ = stitch_frames(only_time, 60)
    check("纯时间（同一天）不切", texts(merged3), ["今天a", "16:19", "今天b"])

    # 切点后面没消息就不切（否则会把整段切没）
    tail_marker = [[it("今天a"), it("今天b"), tm("昨天")]]
    merged4, _ = stitch_frames(tail_marker, 60)
    check("切点后面没消息就不切", texts(merged4), ["今天a", "今天b", "昨天"])


def test_time_item_as_anchor():
    print("\n15. 时间分隔符能当缝合的锚点")
    # 两屏唯一的重合就是那条时间分隔 —— 没有它这两屏接不上
    a = [it("m1"), it("m2"), tm("16:19"), it("m3")]
    b = [tm("16:19"), it("m3"), it("m4")]
    merged, gap = stitch_frames([b, a], 60)
    check("靠分隔符接上了", texts(merged), ["m1", "m2", "16:19", "m3", "m4"])
    check("没有缺口", gap, False)


def test_signature_with_time():
    print("\n16. 签名要把时间分隔算进去")
    s1 = signature([it("a"), tm("16:19")])
    s2 = signature([it("a")])
    check_true("多了一条分隔符 -> 签名不同", s1 != s2)


# ------------------------------------------------------------------ 去重

def test_dedup_window(st):
    print("\n17. 去重看的是最近 K 屏，不是只看上一屏")
    # A/B/A/B —— 来回翻的时候屏幕就是这个形状。
    # 只跟上一屏比的话每一屏都不同 -> 全存下来（实测 20 屏里 4 屏是纯重复）。
    A = [it("a1"), it("a2")]
    B = [it("b1"), it("b2")]
    for x in (A, B, A, B, A, B):
        st.append_screen("w1", x)
    check("A/B 来回 6 次只存 2 屏", st.count("w1"), 2)

    # 窗口是有限的：超出 lookback 的重复照存（不能无限回溯，那是查询成本）
    st.append_screen("w2", [it("x0")])
    for i in range(1, DEDUP_LOOKBACK + 1):
        st.append_screen("w2", [it("x%d" % i)])
    check("先存 %d 屏" % (DEDUP_LOOKBACK + 1), st.count("w2"), DEDUP_LOOKBACK + 1)
    wrote = st.append_screen("w2", [it("x0")])       # x0 已经被挤出窗口
    check_true("超出窗口的重复照存", wrote is True)
    check("所以多了一屏", st.count("w2"), DEDUP_LOOKBACK + 2)


def test_judgment_update(st):
    print("\n18. 判定抖动 -> 原地更新，不新增一屏")
    st.append_screen("j1", [it("在吗", None), it("在吗2", None)])
    check("先存了 1 屏", st.count("j1"), 1)

    u0 = st.dedup_stats["updated"]
    wrote = st.append_screen("j1", [it("在吗", False, "张三"), it("在吗2", False, "张三")])
    check_true("返回真（确实写入了）", wrote is True)
    check("但**没有**新增屏（原地更新）", st.count("j1"), 1)
    check("统计里记了 update", st.dedup_stats["updated"] - u0, 1)   # 统计是全程累计的

    items, _ = st.stitch("j1")
    check("判定取到了新的", [who_of(x) for x in items], ["张三", "张三"])
    check("内容还是那两条", texts(items), ["在吗", "在吗2"])

    # 再抖回去（判定又变成判不出来）-> 还是原地更新
    st.append_screen("j1", [it("在吗", None), it("在吗2", None)])
    check("再抖一次也不新增", st.count("j1"), 1)
    items2, _ = st.stitch("j1")
    check("判定跟着回去", [who_of(x) for x in items2], ["?", "?"])


def test_dedup_stats(st):
    print("\n19. 去重统计 + ⚠️ `stats` 必须仍然是**方法**")
    # ⚠️ 这条是防复发的：曾经在 __init__ 里写了一个 `self.stats = {...}` 计数器，
    #    把同名的 `stats()` 方法**覆盖**掉了。于是 run_watch 里那句
    #    `len(store.stats())` 抛 `'dict' object is not callable`，异常被
    #    except 吞掉 -> `screens = None` -> **屏快照一行都不写、消息表照写**
    #    （实测就这么静默瘫了 8 分钟才被看出来）。
    # ⚠️ 必须查**实例**上的 `stats`，不能查类：曾经在这里写了
    #    `getattr(type(st), "stats")`，而 bug 是**实例属性**覆盖方法 ——
    #    类上查到的仍然是那个方法，测试照样全绿，等于没防。
    check_true("实例上的 stats 仍是可调用的方法",
               callable(getattr(st, "stats", None)))
    try:
        r = st.stats()
        ok = isinstance(r, list)
    except Exception as e:
        ok, r = False, "%s: %s" % (type(e).__name__, e)
    check_true("stats() 返回列表", ok, repr(r))

    s0 = dict(st.dedup_stats)
    st.append_screen("s9", [it("a")])
    st.append_screen("s9", [it("a")])                 # skipped
    st.append_screen("s9", [it("a", True)])           # updated
    st.append_screen("s9", [it("b")])                 # written
    d = {k: st.dedup_stats[k] - s0[k] for k in s0}
    check("written 增加 2", d["written"], 2)
    check("skipped 增加 1", d["skipped"], 1)
    check("updated 增加 1", d["updated"], 1)
    check("屏数=2", st.count("s9"), 2)


def test_last_seen_map(st):
    """`last_seen_map()` —— 设置面板「会话按时间排」的时间来源。

    ⚠️ 返回的时间是**字符串**（`"2026-09-27 20:40:52"`）：这个格式定宽、
        天生可按字典序比大小，所以**不要**在别处解析成时间戳 ——
        解析要处理格式差异，而比较结果完全一样（见 `session_ui._seen_key`）。
    """
    print("\n20. 每会话的最后记录时间（给「按时间排」用）")
    st.append_screen("甲", [it("a")])
    st.append_screen("乙", [it("b")])
    st.append_screen("甲", [it("c")])          # 甲再来一屏 -> 它的时间应该更新
    m = st.last_seen_map()
    check_true("两个会话都在", set(m) >= {"甲", "乙"}, repr(sorted(m)))
    check_true("值是**字符串**（不是 float 时间戳）", isinstance(m["甲"], str),
               repr(m["甲"]))
    check_true("格式是定宽 `YYYY-MM-DD HH:MM:SS`（能直接比大小）",
               len(m["甲"]) == 19 and m["甲"][4] == "-" and m["甲"][10] == " ",
               repr(m["甲"]))
    # ⚠️ 同一会话有多屏时取的是**最大**（最新）的那个，不是最早那个
    check_true("同一会话取的是最新的那一屏的时间", m["甲"] >= m["乙"],
               "甲=%s 乙=%s" % (m["甲"], m["乙"]))
    # ⚠️ 字符串比较要和真实时间先后一致 —— 这是"不解析也能排对"的前提
    check_true("后写的会话时间 >= 先写的（字符串比较有效）", m["甲"] >= m["乙"],
               "%s vs %s" % (m["甲"], m["乙"]))
    check_true("没记录过的会话不在表里", "丙" not in m, repr(sorted(m)))
    check_true("返回的是 dict（空库也不抛）", isinstance(st.last_seen_map(), dict))


# ------------------------------------------------------------------ 折叠重复段

def test_collapse_basic():
    print("\n20. 折叠重复段：同一段连续内容出现两次")
    # 来回滚的典型产物：开头一份、结尾又一份
    seq = [it("图片"), it("没吃"), it("哇"), it("挖槽"), it("别的"),
           it("图片"), it("没吃"), it("哇"), it("挖槽")]
    out, dropped = collapse_repeats(seq)
    check("丢掉了后面那份", dropped, 4)
    check("保留第一次出现的那份", texts(out),
          ["图片", "没吃", "哇", "挖槽", "别的"])

    # 单条重复（不成段）不该动 —— 「图片」「哈哈哈哈」满屏都是
    seq2 = [it("图片"), it("a"), it("图片"), it("b"), it("图片")]
    out2, d2 = collapse_repeats(seq2)
    check("单条重复不动", d2, 0)
    check("原样返回", len(out2), 5)

    # 两连重复也不动（min_run=3）
    seq3 = [it("a"), it("b"), it("x"), it("a"), it("b")]
    out3, d3 = collapse_repeats(seq3)
    check("两连重复不动", d3, 0)

    # 三条连续重复才算
    seq4 = [it("a"), it("b"), it("c"), it("x"), it("a"), it("b"), it("c")]
    out4, d4 = collapse_repeats(seq4)
    check("三连重复折掉", d4, 3)
    check("留下前面那份", texts(out4), ["a", "b", "c", "x"])

    check("空输入", collapse_repeats([]), ([], 0))


def test_collapse_in_stitch(st):
    print("\n21. stitch_frames 里的折叠")
    # 真实形状：来回滚产生的两屏（内容高度重复）。这种情况拼接本来就正确，
    # 不该误折叠 —— 借它确认「不误伤」。
    new = [it("图片"), it("动画表情")]
    old = [it("哇"), it("挖槽"), it("太火爆了"), it("啦啦啦啦"), it("19:25"), it("图片")]
    merged, gap = stitch_frames([new, old], 60, True)
    check("两屏都拼上了，顺序对", texts(merged),
          ["哇", "挖槽", "太火爆了", "啦啦啦啦", "19:25", "图片", "动画表情"])
    check("没有误折叠", len(merged), 7)

    # 回调确实接上了（用一个必然含重复段的输入）
    got = []
    frames = [[it("A"), it("B"), it("C"), it("X"), it("A"), it("B"), it("C")]]
    out, _ = stitch_frames(frames, 60, True, on_collapse=got.append)
    check("回调回报丢了 3 条", got, [3])
    check("留下第一次那份", texts(out), ["A", "B", "C", "X"])


def test_no_scatter():
    """⚠️⚠️ 2026-10-14：**任何一屏都不许在拼接结果里被拆散**。

    这是用户报「滑动看历史时聊天记录乱拼接」时定下的硬判据。
    实测（用户那 87 张真实屏）：旧算法 **28% 的屏被拆散/错序**；
    改成"先验后接"之后降到 **1%**，代价是 23% 的屏宁可标缺口（`stitch_frames` 报 gap）。

    这里用两种最容易出错的形状当回归：
      ① 满屏重复短词（「图片」「哈哈」）—— 旧算法就是拿这些偶然撞上的单条当锚点；
      ② 同一屏内容重复出现（来回滚）—— 重复段会让"最靠后"的启发式选错位置。
    """
    print("\n22. ⚠️ 拼接不许把任何一屏拆散（乱拼接的回归判据）")

    def contig(seq, sub):
        a = [(x.get("text") or "").strip() for x in seq]
        b = [(x.get("text") or "").strip() for x in sub]
        if not b:
            return True
        for i in range(len(a) - len(b) + 1):
            if a[i:i + len(b)] == b:
                return True
        return False

    # ① 满屏重复短词
    f_new = [it("图片"), it("哈哈"), it("图片"), it("哈哈"), it("图片")]
    f_old = [it("哈哈"), it("图片"), it("哈哈"), it("开始drink")]
    merged, gap = stitch_frames([f_new, f_old], 60, False)
    check_true("最新那屏原样保留（不许被拆散）", contig(merged, f_new), texts(merged))
    if contig(merged, f_old):
        check_true("旧屏接上了就得是**连续**的", True)
    else:
        check_true("旧屏接不上就没接（宁可不接，也不许拆散）", gap, "gap=%s" % gap)
        check_true("  它的独占内容没有散落在结果中间",
                   "开始drink" not in texts(merged), texts(merged))

    # ② 同内容重复出现（来回滚的典型形状）
    a = [it("早"), it("吃的啥"), it("你猜"), it("吃的啥"), it("你猜"), it("哈哈")]
    b = [it("你猜"), it("吃的啥"), it("你猜"), it("哈哈"), it("行了")]
    merged2, gap2 = stitch_frames([a, b], 60, False)
    check_true("最新那屏原样保留", contig(merged2, a), texts(merged2))
    if not contig(merged2, b):
        check_true("旧屏接不上就报缺口", gap2, "gap=%s" % gap2)

    # ③ 正常的"往上翻一屏"必须仍然接得上（别因为加严就全都不接了）
    newest = [it("m4"), it("m5"), it("m6")]
    older = [it("m1"), it("m2"), it("m3"), it("m4")]
    merged3, gap3 = stitch_frames([newest, older], 60, False)
    check("正常翻历史仍然接得上", texts(merged3),
          ["m1", "m2", "m3", "m4", "m5", "m6"])
    check("没有缺口", gap3, False)


def main():
    print("=" * 70)
    print("屏快照存储 + 缝合算法 单元测试")
    print("=" * 70)

    st, path = new_store()
    try:
        test_basic(st)
        test_upgrade(st)
        test_new_at_bottom(st)
        test_scroll_up(st)
        test_full_scroll(st)
        test_no_overlap(st)
        test_min_overlap(st)
        test_trim()
        test_max_items(st)
        test_convert()
        test_find_overlap()
        test_time_text_regex()
        test_items_from_snapshot()
        test_cut_at_date()
        test_time_item_as_anchor()
        test_signature_with_time()
        test_dedup_window(st)
        test_judgment_update(st)
        test_dedup_stats(st)
        test_last_seen_map(st)
        test_collapse_basic()
        test_collapse_in_stitch(st)
        test_no_scatter()
    finally:
        st.close()
        try:
            os.unlink(path)
        except Exception:
            pass

    print()
    print("=" * 70)
    print("通过 %d，失败 %d" % (PASS, FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
