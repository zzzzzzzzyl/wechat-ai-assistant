# -*- coding: utf-8 -*-
"""知识库检索的**精度**实验：怎么挡住"弱相关也算命中"，又不误杀真命中。

    python probes\\probe_kb_precision.py

起因（2026-10-17 用户实测）：微信里在聊物理（合力/分力/静摩擦），当前要回的那句是
「怎么改变」，于是查询 = 「怎么改变」—— 3 个二字组（怎么 / 么改 / 改变）。
结果检索把**毕设选题报告**和 **Transformer 参数量** 塞进了【参考资料】，
命中数全是 **1/3**，而且匹配的是「怎么」这种哪都有的虚词。

要回答的问题
------------
能不能加一条**可解释**的门槛，把这类"只碰到一个虚词"的命中判成"没查到"，
同时不把真命中（问法和资料对得上）一起挡掉？

四种候选门槛
------------
- `base`      : 现状（只按 bm25 排序 + 相对首位的 0.6）
- `df`        : 查询时算每个二字组的**文档频率**，df 超过 5% 的当虚词丢掉，不参与检索
- `min2`      : 至少要命中 **2 个不同**的二字组（查询本身不足 2 个就按 1 个算）
- `cov`       : 命中覆盖率 ≥ 34%（命中数 / 查询二字组数）
- `df+min2`   : 上面两条一起
- `final`     : **产品里用的** = 覆盖率 0.3 + 「短查询命中≥2 / 长查询命中≥3」
  （实测：示例语料上"该中的 19/19、不该中的 1/10"；只用比例分不开
   「怎么改变」1/3 和「文档拆多长」3/9 —— 两者都是 0.33，得靠**绝对个数**）

怎么判好坏
----------
- **该挡的（BAD）**：资料里根本没有的题（物理受力、Docker、K8s…）——
  返回 ≥1 条就算**假命中**，越低越好。
- **不该挡的（GOOD）**：示例资料里确实有的题 —— 看 hit@3 掉了多少。

⚠️ 只打印数字和问法，**不打印资料正文**。
"""
import os
import re
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from wechat_assistant import kb as K          # noqa: E402

# ---------------------------------------------------------------- 语料

EXAMPLES = os.path.join(BASE, "examples", "kb")

#: 资料里**确实有**的问法（和 `examples/测试问题.txt` 的 A~D、G 组一致）
GOOD = [
    "注意力为什么要除以根号 d", "RoPE 位置编码有什么好处", "LoRA 的 rank 一般取多少",
    "混合精度训练每参数占多少字节", "流水线气泡率怎么算", "ZeRO-2 每参数多少字节",
    "七 B 的模型跑推理要多少显存", "显卡不够又想微调大模型怎么办",
    "文档拆多长一片比较好", "INT4 的 7B 权重占多大", "4090 能全参微调多大的模型",
    "32K 序列的 KV cache 多大", "学习率设多少比较合适", "切片要不要做重叠",
    "rebase 和 merge 该用哪个", "撤销已经 push 的提交用什么命令",
    "误删的分支还能找回来吗", "QLoRA 用的 NF4 到底是什么", "3D 并行的总卡数怎么算",
]

#: 资料里**根本没有**的题（含用户实测那条物理对话）
BAD = [
    "怎么改变",                                  # ← 用户实测：会被塞进毕设/Transformer
    "怎么改变 就有可能还有静摩擦 会",
    "合力的大小是不是会随着分力角度的改变而改变",
    "对抗训练怎么做", "怎么用 Docker 部署模型",
    "紫砂壶怎么开壶", "明天天气怎么样", "红烧肉要炖多久",
    "这个月业绩能完成吗", "怎么提高睡眠质量",
]

# ---------------------------------------------------------------- 门槛


def df_map(con, grams):
    """每个二字组在库里出现在多少片（查询时现算，**不需要建表维护**）。"""
    out = {}
    for g in grams:
        try:
            out[g] = con.execute(
                "select count(*) from kb_index where grams match ?",
                ('"%s"' % g.replace('"', '""'),)).fetchone()[0]
        except sqlite3.Error:
            out[g] = 0
    return out


def total_chunks(con):
    return con.execute("select count(*) from kb_chunks").fetchone()[0] or 1


def pick(con, query, strategy, fetch=24, top_k=3, min_ratio=0.6,
         df_ratio=0.05, cov_ratio=0.3):
    """按某种门槛检索 → [(doc, crumb, score)]（已经过剪枝）。"""
    q = K.sanitize_query(query)
    grams = [g for g in K.bigrams(q).split() if g.strip()][:12]
    if not grams:
        return []
    use = list(grams)
    if "df" in strategy:
        total = total_chunks(con)
        cut = max(2, int(total * df_ratio))
        dfs = df_map(con, grams)
        use = [g for g in grams if dfs.get(g, 0) <= cut]
        if not use:
            return []
    expr = " OR ".join('"%s"' % g for g in use)
    rows = con.execute(
        "select d.name, c.crumb, c.text, bm25(kb_index) s from kb_index"
        " join kb_chunks c on c.id = kb_index.rowid"
        " join kb_docs d on d.id = c.doc_id"
        " where kb_index match ? order by bm25(kb_index) limit ?",
        (expr, fetch)).fetchall()
    if not rows:
        return []
    top = rows[0][3]
    cut = top * min_ratio if top < 0 else None
    need = 1
    if "min2" in strategy:
        need = min(2, len(use))
    out = []
    for name, crumb, text, s in rows:
        if cut is not None and s > cut:
            continue
        flat = "".join((text or "").split()) + "".join((crumb or "").split())
        hit = sum(1 for g in use if g in flat)
        if hit < need:
            continue
        if "cov" in strategy and hit < max(1, int(len(use) * 0.34 + 0.999)):
            continue
        if strategy == "final":
            floor = 2 if len(use) < 6 else 3
            need2 = max(min(floor, len(use)),
                        int(-(-len(use) * cov_ratio // 1)))
            if hit < need2:
                continue
        out.append((name, crumb, s))
        if len(out) >= top_k:
            break
    return out


# ---------------------------------------------------------------- 跑


def build_examples(dbpath):
    kb = K.KnowledgeBase(dbpath, logger=lambda m: None)
    kb.import_tree(EXAMPLES, grp="示例")
    return kb


def run(label, con, strategy, want=None):
    """GOOD → hit@3；BAD → 假命中率。"""
    good_hit = 0
    for q in GOOD:
        hits = pick(con, q, strategy)
        if hits:
            good_hit += 1
    bad_hit = [q for q in BAD if pick(con, q, strategy)]
    print("%-12s GOOD 有结果 %2d/%2d    BAD 假命中 %2d/%2d   假命中问法: %r"
          % (strategy, good_hit, len(GOOD), len(bad_hit), len(BAD),
             bad_hit[:3]))


def main():
    d = tempfile.mkdtemp(prefix="kbprec_")
    kb = build_examples(os.path.join(d, "ex.db"))
    con = sqlite3.connect(kb.path)
    print("=" * 92)
    print("语料：examples/kb（%d 片）" % total_chunks(con))
    print("=" * 92)
    for s in ("base", "cov", "min2", "df+min2", "final"):
        run("examples", con, s)
    con.close()

    real = os.path.join(BASE, "state", "kb.db")
    if os.path.exists(real):
        con2 = sqlite3.connect("file:%s?mode=ro" % real.replace("\\", "/"), uri=True)
        print("\n" + "=" * 92)
        print("语料：你真实的知识库（%d 片）" % total_chunks(con2))
        print("⚠️ 这一段的 GOOD 列**别当召回看**：那 19 条问的是分布式/Git，而真实库里")
        print("   没有那些资料 —— 它们「有结果」本身就是假命中。只看 BAD 那一列。")
        print("=" * 92)
        for s in ("base", "final"):
            run("真实库", con2, s)
        con2.close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
