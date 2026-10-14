# -*- coding: utf-8 -*-
"""只读分析：某个会话的「对话方式」到底能从现有数据里得出什么。

目的
----
回答「有没有办法分析并记录这个会话的对话方式」。做法是把历史库
(state/history.db) 里的消息跑一遍统计，看哪些特征**现在就能拿到**、
哪些**缺数据拿不到**。

⚠️ 只读：不写库、不改配置、不调 AI、不碰微信。
用法： python probes/analyze_style.py [会话名]
     不带参数则分析库里消息最多的那个会话。
"""

import os
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "state", "history.db")

# 语气词/口头语（判断「这人平时怎么说话」很有效）
MOOD_WORDS = ["啊", "吧", "呢", "吗", "嘛", "哦", "噢", "呀", "哈", "嘿",
              "嗯", "诶", "唉", "哎", "呜", "鸭", "滴", "咯", "啦", "呗", "嗷"]
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF"
    "\u2190-\u21FF\u2B00-\u2BFF\uFE0F]")
# 常见称呼/爱称，用来看「这个会话里怎么互相称呼」
CALL_RE = re.compile(r"(猪猪|笨蛋|傻瓜|宝贝|亲爱的|老婆|老公|哥哥|姐姐|弟弟|妹妹|"
                     r"老板|老师|同学|大佬|兄弟|集美|家人们)")


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def pct(a, b):
    return "%.0f%%" % (100.0 * a / b) if b else "—"


def main():
    if not os.path.exists(DB):
        print("历史库不存在: %s" % DB)
        return 1

    want = sys.argv[1] if len(sys.argv) > 1 else None
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    rows = list(conn.execute("select * from messages order by id"))
    if not rows:
        print("历史库还是空的 —— 先去微信里按几次 Ctrl+Alt+Q 攒点数据")
        return 0

    # ---- 选会话
    by_sess = Counter(r["session_key"] for r in rows)
    if want and want not in by_sess:
        print("库里没有会话 %r；现有：%s" % (want, list(by_sess)))
        return 1
    sess = want or by_sess.most_common(1)[0][0]
    msgs = [r for r in rows if r["session_key"] == sess]

    section("会话 %r —— 共 %d 条" % (sess, len(msgs)))

    me = [r for r in msgs if r["is_self"] == 1]
    other = [r for r in msgs if r["is_self"] == 0]
    unknown = [r for r in msgs if r["is_self"] is None]

    # ============================================================ 1. 发言占比
    section("1. 谁在说话（发言占比）")
    print("  我  : %d 条  (%s)" % (len(me), pct(len(me), len(me) + len(other))))
    print("  对方: %d 条  (%s)" % (len(other), pct(len(other), len(me) + len(other))))
    if unknown:
        print("  未判定: %d 条（发送者没判出来，不计入占比）" % len(unknown))

    # ============================================================ 2. 长度
    section("2. 说话长短（最重要的口吻指标之一）")
    for tag, grp in (("我", me), ("对方", other)):
        if not grp:
            continue
        lens = [len(r["content"]) for r in grp]
        lens_sorted = sorted(lens)
        mid = lens_sorted[len(lens_sorted) // 2]
        short = sum(1 for x in lens if x <= 5)
        print("  %s  : 平均 %.1f 字  中位 %d 字  最长 %d 字"
              % (tag, sum(lens) / len(lens), mid, max(lens)))
        print("        5 字以内的短消息占 %s（%d/%d）"
              % (pct(short, len(lens)), short, len(lens)))

    # ============================================================ 3. 标点/表情
    section("3. 标点与表情习惯")
    for tag, grp in (("我", me), ("对方", other)):
        if not grp:
            continue
        n = len(grp)
        end_dot = sum(1 for r in grp if r["content"].rstrip().endswith(("。", ".")))
        has_emoji = sum(1 for r in grp if EMOJI_RE.search(r["content"]))
        has_quest = sum(1 for r in grp if "?" in r["content"] or "？" in r["content"])
        has_excl = sum(1 for r in grp if "!" in r["content"] or "！" in r["content"])
        has_tilde = sum(1 for r in grp if "~" in r["content"] or "～" in r["content"])
        print("  %s  : 句尾句号 %s | 带表情 %s | 问句 %s | 感叹 %s | 波浪号 %s"
              % (tag, pct(end_dot, n), pct(has_emoji, n),
                 pct(has_quest, n), pct(has_excl, n), pct(has_tilde, n)))

    # ============================================================ 4. 语气词
    section("4. 语气词使用（「怎么说话」的直接证据）")
    for tag, grp in (("我", me), ("对方", other)):
        if not grp:
            continue
        c = Counter()
        for r in grp:
            for w in MOOD_WORDS:
                if w in r["content"]:
                    c[w] += 1
        top = "、".join("%s×%d" % (w, k) for w, k in c.most_common(8))
        print("  %s  : %s" % (tag, top or "（没用到常见语气词）"))

    # ============================================================ 5. 称呼
    section("5. 互相怎么称呼")
    for tag, grp in (("我", me), ("对方", other)):
        if not grp:
            continue
        c = Counter()
        for r in grp:
            for m in CALL_RE.findall(r["content"]):
                c[m] += 1
        top = "、".join("%s×%d" % (w, k) for w, k in c.most_common(6))
        print("  %s  : %s" % (tag, top or "（没出现常见称呼词）"))

    # ============================================================ 6. 连发
    section("6. 连发习惯（一次发几条）")
    runs = {"我": [], "对方": []}
    cur_who, cur_n = None, 0
    for r in msgs:
        who = "我" if r["is_self"] == 1 else ("对方" if r["is_self"] == 0 else None)
        if who is None:
            continue
        if who == cur_who:
            cur_n += 1
        else:
            if cur_who:
                runs[cur_who].append(cur_n)
            cur_who, cur_n = who, 1
    if cur_who:
        runs[cur_who].append(cur_n)
    for tag in ("我", "对方"):
        seq = runs[tag]
        if not seq:
            continue
        multi = sum(1 for x in seq if x > 1)
        print("  %s  : 共 %d 次发言，其中连发(>1条) %d 次，最长连发 %d 条，平均一次 %.1f 条"
              % (tag, len(seq), multi, max(seq), sum(seq) / len(seq)))

    # ============================================================ 7. 消息类型
    section("7. 消息类型分布")
    tc = Counter(r["msg_type"] or "(未标注)" for r in msgs)
    print("  " + " | ".join("%s×%d" % (k, v) for k, v in tc.most_common()))

    # ============================================================ 8. 数据缺口
    section("8. ⚠️ 现有数据拿不到的东西（决定了能做到什么程度）")
    stamps = set(r["created_at"] for r in msgs)
    print("  · 消息**实际发送时间**：拿不到。")
    print("    created_at 是「我们读到它的时刻」——现在 %d 条消息只落在 %d 个时间戳上，"
          % (len(msgs), len(stamps)))
    print("    同一批读取的挤在同一秒，所以算不出「回复间隔」「谁先回」这类节奏指标。")
    print("  · 语音/图片内容：只有占位（[图片] 等），不含实际内容。")
    print("  · 未读/已读、对方在线状态：UIA 不提供。")
    print()
    print("  要补上发送时间，有两条路（都还没做）：")
    print("    a) 聊天窗口里其实**显示**了时间分隔（实测 PrintWindow 截图里有 '21:30'），")
    print("       可从 UIA 找时间元素或用 OCR 提取，再和消息行对齐；")
    print("    b) 轮询更密（例如 watch 模式 1s 一次），用「首次出现时刻」近似发送时间。")

    # ============================================================ 9. 给 AI 的画像
    section("9. 这些统计可以直接拼成一段「会话画像」喂给 AI")
    if me and other:
        me_len = sum(len(r["content"]) for r in me) / len(me)
        ot_len = sum(len(r["content"]) for r in other) / len(other)
        me_emoji = sum(1 for r in me if EMOJI_RE.search(r["content"])) / len(me)
        ot_emoji = sum(1 for r in other if EMOJI_RE.search(r["content"])) / len(other)
        print("  示例（自动生成的，可人工改）：")
        print("    「这是个%s聊天。我说话平均 %.0f 字，对方平均 %.0f 字；"
              % ("你在多说" if len(me) > len(other) * 1.3 else
                 ("对方在多说" if len(other) > len(me) * 1.3 else "一来一回"),
                 me_len, ot_len))
        print("      我用表情的比例 %.0f%%，对方 %.0f%%。」"
              % (me_emoji * 100, ot_emoji * 100))

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
