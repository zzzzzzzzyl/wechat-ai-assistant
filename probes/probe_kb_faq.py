# -*- coding: utf-8 -*-
"""本地知识库「怎么答」的实测：**关键词问法 vs 口语问法**，以及"问法别名"能补多少。

为什么需要它
------------
`RAG调研.md` 量的是"哪种索引能查回原文"。但真正决定效果的还有两件事，
这两件都跟索引技术无关：

1. **用户怎么问**（用资料里的词 vs 换一种说法）；
2. **资料怎么整理**（一条问答一片 vs 按 400 字机械切；要不要写"常见问法"）。

所以这里造一个小型的「个人微信/群聊助手」知识库（16 条：价目、售后、配送、群规、预约），
量三组数字：

- `关键词` 查询（用资料里的词，如 `定金` / `自提点`）
- `口语` 查询（真人问法，如 `钱能退吗` / `在哪儿拿货`）
- 同 `口语` 查询，但资料里**补了"常见问法"**（其中 10 条的别名写进去了、6 条没写）

判据看的是 **top3 里是否出现该条的答案关键短语**（不是"来源片是否排第一" ——
真实使用是把 3 片一起塞给模型，不是只看第一名）。

怎么跑
------
    python probes\\probe_kb_faq.py

⚠️ 语料是**手写的小样本**（16 条）。它测的是**分词与问法**的行为差异，
   不是"真实知识库的召回率"。真实评估集要按 `RAG调研.md` §7.4 用失败样本攒。
"""

import sqlite3
import sys

# ------------------------------------------------------------------ 知识库（手写）

# answer_key：答案里一个**短而有辨识度**的串，用来判"这一片有没有答到"
KB = [
    dict(topic="定金与尾款", key="30%",
         answer="定金30%，发货前付清尾款。买家自己取消订单，定金不退；"
                "如果是我方缺货取消，定金全额退。",
         aliases=["钱怎么付", "定金能退吗", "付款方式"]),
    dict(topic="退换货时效", key="48小时",
         answer="确认收货后48小时内可无理由退；7天内出现质量问题包退换，"
                "超过7天只能按维修处理。",
         aliases=["东西不想要了怎么退", "能不能退", "退货期限"]),
    dict(topic="退款到账时间", key="1-3个工作日",
         answer="退款按支付原路退回，微信支付一般1-3个工作日到账，"
                "超时未到请把订单号发我查。",
         aliases=["退款多久能到", "钱什么时候退回来"]),
    dict(topic="自提点", key="17点后",
         answer="自提点在**南门菜鸟驿站**，每天17点后取货，"
                "取货时报收件手机号后四位。",
         aliases=["在哪儿拿货", "哪里取货", "自提地址"]),
    dict(topic="配送范围与运费", key="99",
         answer="市区三环内满99元免运费；不满99或超出三环，每单加收15元冷链费。",
         aliases=["运费怎么算", "配送费多少", "包邮吗"]),
    dict(topic="整箱优惠", key="每箱减8元",
         answer="整箱拿满20箱每箱减8元，满50箱每箱减12元，优惠不叠加。",
         aliases=["多买能便宜吗", "批发价", "有没有优惠"]),
    dict(topic="发货时间", key="次日",
         answer="当天17点前下单当天发出；17点之后的订单次日发出。",
         aliases=["今天下单什么时候发", "什么时候发货"]),
    dict(topic="现货与预定", key="5-7天",
         answer="A2定制款有现货当天发；B1标准款需要预定，周期5-7天。",
         aliases=["那个款有货吗", "要等多久"]),
    dict(topic="发票", key="电子普票",
         answer="可以开电子普票，下单备注「要发票」并把邮箱发我，随货一起发到邮箱。",
         aliases=["能开发票不", "发票怎么开"]),
    dict(topic="快递丢件处理", key="补发",
         answer="丢件先报运单号，我这边48小时内补发或全额退款，不用你去找快递公司。",
         aliases=["快递不见了怎么办", "包裹丢了"]),
    dict(topic="群规-广告", key="移出",
         answer="群内禁止发布其他代购广告和拉人链接，发现一次提醒、两次移出。",
         aliases=["群里能发广告吗", "能不能拉人"]),
    dict(topic="群规-拼单接龙", key="接龙",
         answer="拼单请在晚8点前接龙，接龙后不可取消；接了不付款的名额自动顺延。",
         aliases=["拼单怎么报名", "怎么参加拼单"]),
    dict(topic="群规-发布频率", key="早9点",
         answer="每日上新和价格集中在早9点发布，其余时段只答问，不刷屏。",
         aliases=["一天发几次消息", "什么时候上新"]),
    dict(topic="付款方式限制", key="红包",
         answer="只支持微信转账和群收款，**不接受红包付款**（红包无法对账）。",
         aliases=["能用红包付款吗", "怎么付款"]),
    dict(topic="生鲜储存", key="冷藏",
         answer="到货后冷藏保存，建议3天内吃完；冷冻可放30天，解冻后别再冻回去。",
         aliases=["买回来怎么保存", "能放多久"]),
    dict(topic="客服在线时间", key="21点",
         answer="每天9点到21点在线，21点之后的消息次日9点统一回复。",
         aliases=["晚上能问吗", "几点下班", "客服时间"]),
]

# 口语问法：**只覆盖其中 10 条**（另外 6 条没有对应别名 —— 这是有意留的）
# key 指向 KB 的下标；covered=True 表示 KB 的 aliases 里写了这句
COLLOQUIAL = [
    (0, "钱怎么付", True),
    (1, "东西不想要了怎么退", True),
    (2, "退款多久能到", True),
    (3, "在哪儿拿货", True),
    (4, "运费怎么算", True),
    (5, "多买能便宜吗", True),
    (8, "能开发票不", True),
    (9, "快递不见了怎么办", True),
    (13, "能用红包付款吗", True),
    (15, "晚上能问吗", True),
    # ---- 下面这 6 条**没有任何别名**，模拟"用户换了你想不到的说法" ----
    (6, "我急着要，今天能走吗", False),
    (7, "那款我没等到货", False),
    (10, "发别的链接会被踢吗", False),
    (11, "报名之后能撤吗", False),
    (12, "群里消息太多了", False),
    (14, "放冰箱能放几天", False),
]

KEYWORD = [
    (0, "定金"), (1, "退换货"), (2, "退款到账"), (3, "自提点"), (4, "运费"),
    (5, "满20箱"), (6, "发货时间"), (7, "现货"), (8, "发票"), (9, "丢件"),
    (10, "广告"), (11, "接龙"), (12, "上新"), (13, "红包"), (14, "冷藏"),
    (15, "客服时间"),
]


# ------------------------------------------------------------------ 索引

def bigrams(text, n=2):
    t = "".join(text.replace("*", "").split())
    if len(t) < n:
        return t
    return " ".join(t[i:i + n] for i in range(len(t) - n + 1))


def sanitize(q):
    out = []
    for ch in (q or ""):
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
    return "".join(out).strip()


def build(use_alias, kind="bigram"):
    """建两个东西：**原文表**（判"有没有答到"用）+ FTS 索引（检索用）。

    ⚠️⚠️ 判"命中片里有没有答案"必须查**原文**。
        重叠二字组是**不可逆**的（`定金30%` -> `定金 金3 30 0%`，
        去掉空格得到 `定金金3300%`，原串还原不回来）——
        踩过一次：直接用 grams 判包含，命中率从 100% 被算成 44%。
        这正是 `RAG调研.md` §5.1 里 `text` / `grams` **要分两列存**的原因。
    """
    con = sqlite3.connect(":memory:")
    con.execute("create table docs(id integer primary key, text text)")
    tok = "'trigram'" if kind == "trigram" else "'unicode61'"
    con.execute("create virtual table t using fts5(body, tokenize=%s)" % tok)
    for i, e in enumerate(KB):
        text = e["topic"] + "。" + e["answer"]
        if use_alias:
            text = "常见问法：" + "、".join(e["aliases"]) + "。" + text
        body = bigrams(text) if kind == "bigram" else text
        con.execute("insert into docs(id, text) values (?,?)", (i + 1, text))
        con.execute("insert into t(rowid, body) values (?,?)", (i + 1, body))
    return con


def query(kind, q, mode="or"):
    """把问法变成 FTS 查询。

    ⚠️⚠️ `mode` 的选择**影响很大**（§10.8 实测）：
        - `phrase`：整句二字组必须**逐字连续**。口语问法里只要带一个资料里没有的
          语气词（`多少钱` / `怎么办` / `能吗`），就**直接 0 命中**；
        - `or`：任意二字组命中就算候选，靠 bm25（idf 加权）排序 —— **推荐**。
    """
    q = sanitize(q)
    if kind == "trigram":
        # ⚠️ trigram 索引**不能**再把查询切成二字组（那样永远是 0 命中）；
        #    它是按三字滑窗建的，查询原样进去、且必须 ≥3 字。
        return '"%s"' % q if len(q) >= 3 else ""
    grams = [g for g in bigrams(q).split() if g.strip()]
    if not grams:
        return ""
    if mode == "phrase":
        return '"%s"' % " ".join(grams)
    return " OR ".join('"%s"' % g for g in grams[:12])


def run(label, queries, kind="bigram", topk=3, use_alias=False, mode="or"):
    con = build(use_alias, kind)
    hits = {}
    rows = []
    for idx, q in queries:
        key = KB[idx]["key"]
        mq = query(kind, q, mode)
        try:
            res = [r[0] for r in con.execute(
                "select rowid from t where t match ? order by rank limit %d" % topk,
                (mq,))] if mq else []
        except Exception:
            res = []
        texts = []
        for rid in res:
            row = con.execute("select text from docs where id=?", (rid,)).fetchone()
            if row:
                texts.append(row[0])
        ok = any(key in t for t in texts)
        rows.append((q, ok, len(res)))
        hits[q] = ok
    con.close()
    n = len(rows)
    got = sum(1 for _, ok, _ in rows if ok)
    empty = sum(1 for _, _, k in rows if k == 0)
    print("%-34s 命中 %2d/%2d (%3.0f%%)   0结果 %d" % (label, got, n, 100.0 * got / n, empty))
    return rows


def main():
    print("=" * 78)
    print("小知识库（16 条，bigram 索引，取 top3 拼给模型 —— 与真实用法一致）")
    print("=" * 78)

    # ⚠️ 查询策略的影响**比我想的大**（第一次只跑了 phrase，结论偏悲观）：
    #    口语问法里必然带着资料里没有的语气词（`多少钱`/`怎么办`），
    #    整句 phrase 要求逐字连续 → 直接 0 命中。**所以两种都跑。**
    for mode in ("phrase", "or"):
        tag = "整句 phrase" if mode == "phrase" else "二字组 OR（推荐）"
        print("\n" + "#" * 78)
        print("# 查询策略：%s" % tag)
        print("#" * 78)

        print("\n【A 关键词问法】用资料里的词")
        run("关键词 16 条 [%s]" % mode, KEYWORD, mode=mode)

        print("\n【B 口语问法】真人怎么问（其中 6 条没有任何别名）")
        run("口语 16 条 · 无别名 [%s]" % mode,
            [(i, q) for i, q, _ in COLLOQUIAL], mode=mode)

        print("\n【C 口语问法 + 资料里写了「常见问法」】")
        rows_alias = run("口语 16 条 · 有别名 [%s]" % mode,
                         [(i, q) for i, q, _ in COLLOQUIAL], use_alias=True, mode=mode)

        covered = [(q, ok) for (q, ok, _), (_, _, c) in zip(rows_alias, COLLOQUIAL) if c]
        uncover = [(q, ok) for (q, ok, _), (_, _, c) in zip(rows_alias, COLLOQUIAL) if not c]
        print("  其中**写了别名**的 %d 条：命中 %d (%.0f%%)"
              % (len(covered), sum(1 for _, ok in covered if ok),
                 100.0 * sum(1 for _, ok in covered if ok) / max(1, len(covered))))
        print("  其中**没写别名**的 %d 条：命中 %d (%.0f%%)"
              % (len(uncover), sum(1 for _, ok in uncover if ok),
                 100.0 * sum(1 for _, ok in uncover if ok) / max(1, len(uncover))))

        print("\n【D 仍然失败的（口语 + 别名之后）】")
        for q, ok in uncover:
            if not ok:
                print("  ✗ %s" % q)

    print("\n【E 对照：trigram 索引（二字组 OR）】")
    run("关键词（trigram）", KEYWORD, kind="trigram", mode="or")
    run("口语 + 别名（trigram）", [(i, q) for i, q, _ in COLLOQUIAL],
        kind="trigram", use_alias=True, mode="or")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
