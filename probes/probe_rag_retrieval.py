# -*- coding: utf-8 -*-
"""RAG 检索方案的**只读实测**：中文在 FTS5 上到底哪种索引方式能查回来。

为什么需要它
------------
`需求与设计文档.md` §5.5 的 P2 计划是「先 FTS5，后向量」，原话是
「Python 内置 sqlite3 直接支持 FTS5，**零额外依赖**，中文可用 bigram 分词」。
但**"可以直接用"和"查得回来"是两件事**，而这个项目最怕的失败方式是静默：
建表成功、插入成功、查询不报错，只是**永远返回 0 条**。

四种索引方式
------------
- `uni`      : FTS5 默认 `unicode61`。中文没空格，**整句 = 一个 token**（对照组）
- `trigram`  : SQLite 3.34+ 内置，按三字滑窗建索引 → **查询必须 ≥ 3 个字**
- `bigram`   : 自己把正文切成「重叠二字组」再交给 unicode61（零依赖、可查 2 字）
- `jieba`    : 先分词再交给 unicode61（效果取决于查询是不是"整词"）

两类查询（**必须分开看，结论完全不同**）
--------------------------------------
- `word`：从正文里取一个**完整词**当查询（模拟用户输入"报销""打包"这类关键词）
- `sub` ：从正文里随机截一段（模拟"记不清原话，只记得几个字"）

⚠️ 只看 `sub` 会**低估 jieba**（词边界被切断，分词器必然查不到）；
   只看 `word` 会**高估 trigram**（整词本来就是连续子串）。两个都要跑。

怎么跑
------
    python probes\\probe_rag_retrieval.py            # 全部
    python probes\\probe_rag_retrieval.py --chat     # 只跑聊天库
    python probes\\probe_rag_retrieval.py --docs     # 只跑文档库

⚠️ **只打印数字，绝不打印消息正文**（聊天库里有真实昵称和对话）。
"""

import argparse
import glob
import os
import random
import sqlite3
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(BASE, "state")


# ------------------------------------------------------------------ 索引构造

def bigrams(text, n=2):
    """把一段中文切成**重叠 n 字组**，用空格连起来交给 unicode61。

    「我们明天下午开会」 -> 「我们 们明 明天 天下 下午 午开 开会」
    ⚠️ 必须是**重叠**的：查询侧要拼成 phrase，靠"相邻两个字组首尾相接"
      才能只命中连续出现的原文（`明天 天下 下午` 才算命中「明天下午」）。
    """
    t = "".join(text.split())
    if len(t) < n:
        return t
    return " ".join(t[i:i + n] for i in range(len(t) - n + 1))


def setup(con, kind):
    con.execute("drop table if exists t_%s" % kind)
    if kind == "trigram":
        con.execute("create virtual table t_%s using fts5(body, tokenize='trigram')" % kind)
    else:
        con.execute("create virtual table t_%s using fts5(body)" % kind)
    return "t_%s" % kind


def index_body(kind, text):
    if kind == "bigram":
        return bigrams(text)
    if kind == "jieba":
        import jieba
        return " ".join(w for w in jieba.cut(text) if w.strip())
    return text


def sanitize(q):
    """去掉会破坏 FTS5 查询语法的字符。

    ⚠️⚠️ **别指望"转义就行"**（实测踩过）：把 `"` 换成 `""` 之后，
        `bigrams()` 会在两个引号中间**切出新的引号 token**，
        拼出来的 phrase 照样是坏的。正确做法是**这两个字符根本不进查询**：
        FTS5 的 unicode61 分词本来就不索引标点，删掉不损失召回。
    """
    out = []
    for ch in (q or ""):
        if ch in '"*(){}[]:^-+':
            continue
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
    return "".join(out).strip()


def make_query(kind, q):
    """把用户输入的一小段话变成该索引方式下能命中的查询串。"""
    q = sanitize(q)
    if kind == "bigram":
        return '"%s"' % bigrams(q)
    if kind == "jieba":
        import jieba
        words = [w for w in jieba.cut(q) if w.strip()]
        return " ".join('"%s"' % w for w in words)
    return '"%s"' % q


def build(kind, rows, dbpath):
    """**写到真实文件**再量体积（`:memory:` 上没有 dbstat，量不出索引大小）。"""
    if os.path.exists(dbpath):
        os.remove(dbpath)
    con = sqlite3.connect(dbpath)
    t = setup(con, kind)
    t0 = time.time()
    con.executemany("insert into t_%s(rowid, body) values (?,?)" % kind,
                    [(rid, index_body(kind, text)) for rid, text in rows])
    con.commit()
    dt = (time.time() - t0) * 1000
    con.close()
    return t, dt, os.path.getsize(dbpath)


# ------------------------------------------------------------------ 语料

def chat_corpus():
    p = os.path.join(STATE, "history.db")
    if not os.path.exists(p):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
    rows = con.execute("select id, content from messages where length(content) >= 4").fetchall()
    con.close()
    out = []
    for rid, text in rows:
        text = (text or "").strip()
        if text in ("[图片]", "[动画表情]", "[文件]", "[链接]", ""):
            continue
        if len(text) < 4:
            continue
        out.append((rid, text[:500]))
    return out


def doc_chunks(max_chars=400):
    """把项目自己的 md 当"导入的知识库"：按段落切片，模拟 kb_chunks。"""
    files = sorted(glob.glob(os.path.join(BASE, "*.md")))
    chunks = []
    for f in files:
        try:
            raw = open(f, encoding="utf-8").read()
        except Exception:
            continue
        paras, buf = [], ""
        for line in raw.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith(">") or s.startswith("|"):
                if buf.strip():
                    paras.append(buf.strip())
                buf = ""
                continue
            buf += s + "\n"
            if len(buf) >= max_chars:
                paras.append(buf.strip())
                buf = ""
        if buf.strip():
            paras.append(buf.strip())
        for p in paras:
            p = " ".join(p.split())
            if len(p) >= 40:
                chunks.append((os.path.basename(f), p))
    return chunks


# ------------------------------------------------------------------ 查询生成

def queries_from(rows, mode, n_query, seed):
    """生成 (源 rowid, 查询串, 词长) 三元组。"""
    import jieba
    rnd = random.Random(seed)
    out = []
    for rid, text in rows:
        if len(out) >= n_query:
            break
        if mode == "word":
            words = [w for w in jieba.cut(text) if len(w.strip()) >= 2 and w.count(" ") == 0]
            if not words:
                continue
            w = rnd.choice(words)
            # 只保留像"词"的（不含标点/数字乱码）
            if all("\u4e00" <= ch <= "\u9fff" for ch in w):
                out.append((rid, w, len(w)))
        else:
            for qlen in (2, 3, 4):
                if len(text) >= qlen:
                    i = rnd.randrange(0, len(text) - qlen + 1)
                    q = text[i:i + qlen]
                    if q.strip():
                        out.append((rid, q, qlen))
    return out


# ------------------------------------------------------------------ 实验主体

def run_bench(title, rows, queries, kinds, tmpdir, topk=5):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    print("%-9s %-6s %-7s %-12s %-12s %-11s %s"
          % ("索引", "词长", "查询数", "首位命中", "前5命中", "0 结果", "单次查询"))
    print("-" * 78)
    for kind in kinds:
        t, dt, size = build(kind, rows, os.path.join(tmpdir, "kb_%s.db" % kind))
        con = sqlite3.connect(os.path.join(tmpdir, "kb_%s.db" % kind))
        lat = []
        # buckets[词长] = [总查询数, 首位命中, 0 结果, 前5命中, 报错]
        buckets = {}
        for rid, q, ql in queries:
            b = buckets.setdefault(ql, [0, 0, 0, 0, 0])
            mq = make_query(kind, q)
            # ⚠️ **清洗后为空必须直接跳过**：把 `''` 丢给 MATCH 不是返回 0 条，
            #    而是抛 `fts5: syntax error near ""`。实测 `--docs --sub` 里
            #    570 次查询是纯标点（`**` / `→ ` / `）·`）。
            if not mq.strip('" '):
                b[4] += 1
                continue
            try:
                t0 = time.time()
                res = [r[0] for r in con.execute(
                    "select rowid from %s where %s match ? order by rank limit %d"
                    % (t, t, topk), (mq,))]
                lat.append((time.time() - t0) * 1000)
            except Exception:
                b[4] += 1
                b[0] += 1
                continue
            b[0] += 1
            if not res:
                b[2] += 1
            else:
                if res[0] == rid:
                    b[1] += 1
                if rid in res[:topk]:
                    b[3] += 1
        for ql in sorted(buckets):
            total, hit1, empty, hit5, err = buckets[ql]
            print("%-9s %-6d %-7d %-12s %-12s %-11s %.2f ms"
                  % (kind, ql, total,
                     "%d(%.0f%%)" % (hit1, 100.0 * hit1 / total) if total else "-",
                     "%d(%.0f%%)" % (hit5, 100.0 * hit5 / total) if total else "-",
                     "%d(%.0f%%)" % (empty, 100.0 * empty / total) if total else "-",
                     sum(lat) / len(lat) if lat else 0))
            if err:
                print("          ⚠️ 其中 %d 次查询清洗后为空，已跳过（不是 0 结果）" % err)
        print("          └ 建索引 %.0f ms / 库文件 %.1f KB" % (dt, size / 1024.0))
        con.close()


def run_bruteforce(rows, queries, topk=5):
    """零索引基线：Python 直接 `in` 扫一遍全部切片，返回命中的前几条。

    用来回答「到底需不需要索引」—— 个人知识库通常只有几百到几千片。
    """
    print("\n" + "=" * 78)
    print("基线：无索引暴力扫描（python `in`）")
    print("=" * 78)
    for scale in (1, 10, 100):
        corpus = rows * scale
        lat, hit1, hit5, empty, total = [], 0, 0, 0, 0
        for rid, q, ql in queries:
            t0 = time.time()
            hits = [i for i, (_, text) in enumerate(corpus) if q in text][:topk]
            lat.append((time.time() - t0) * 1000)
            total += 1
            if not hits:
                empty += 1
            elif corpus[hits[0]][0] == rid:
                hit1 += 1
            if any(corpus[h][0] == rid for h in hits):
                hit5 += 1
        print("%6d 片：首位 %d/%d (%.0f%%)  前5 %d/%d (%.0f%%)  0结果 %d  "
              "平均 %.1f ms"
              % (len(corpus), hit1, total, 100.0 * hit1 / total,
                 hit5, total, 100.0 * hit5 / total, empty, sum(lat) / len(lat)))


def run_vector_cost(dim=512, n=10):
    """向量检索的**算力成本**（不含模型本身）：numpy 暴力余弦 + topk。"""
    try:
        import numpy as np
    except Exception:
        return
    print("\n" + "=" * 78)
    print("基线：向量检索的纯算力成本（%d 维，numpy 暴力余弦，不含模型推理）" % dim)
    print("=" * 78)
    rnd = np.random.default_rng(0)
    for scale in (2000, 20000, 200000):
        mat = rnd.standard_normal((scale, dim)).astype(np.float32)
        mat /= np.linalg.norm(mat, axis=1, keepdims=True)
        lat = []
        for _ in range(n):
            q = rnd.standard_normal(dim).astype(np.float32)
            q /= np.linalg.norm(q)
            t0 = time.time()
            _ = np.argpartition(mat @ q, -5)[-5:]
            lat.append((time.time() - t0) * 1000)
        print("%7d 条 × %d 维（%.1f MB 内存）：平均 %.1f ms"
              % (scale, dim, scale * dim * 4 / 1024.0 / 1024.0,
                 sum(lat) / len(lat)))


def run_chunk_sizes(kinds=("bigram",), n_query=500):
    """切片粒度 + "面包屑前缀"对召回的实测（决定 `kb.chunk_size` 定多少）。

    ⚠️ 这一节是为了**别拍脑袋定 400** —— 需求文档里那个 400/50 是估的。
    """
    print("\n" + "=" * 78)
    print("实验三：切片粒度 / 面包屑前缀（bigram 索引，查询=word）")
    print("=" * 78)
    print("%-26s %-7s %-10s %-10s %s" % ("切片", "片数", "首位命中", "前5命中", "建索引"))
    print("-" * 78)
    tmpdir = tempfile.mkdtemp(prefix="ragprobe_")
    try:
        for max_chars in (150, 300, 400, 800):
            for crumb in (False, True):
                chunks = doc_chunks(max_chars)
                rows = []
                for i, (name, text) in enumerate(chunks):
                    body = ("%s %s" % (name, text)) if crumb else text
                    rows.append((i + 1, body))
                qs = queries_from([(i + 1, c[1]) for i, c in enumerate(chunks)],
                                  "word", n_query, seed=11)
                for kind in kinds:
                    t, dt, size = build(kind, rows, os.path.join(tmpdir, "c_%s.db" % kind))
                    con = sqlite3.connect(os.path.join(tmpdir, "c_%s.db" % kind))
                    hit1 = hit5 = empty = total = 0
                    for rid, q, ql in qs:
                        mq = make_query(kind, q)
                        if not mq.strip('" '):
                            continue
                        total += 1
                        res = [r[0] for r in con.execute(
                            "select rowid from %s where %s match ? order by rank limit 5"
                            % (t, t), (mq,))]
                        if not res:
                            empty += 1
                        elif res[0] == rid:
                            hit1 += 1
                        if rid in res[:5]:
                            hit5 += 1
                    con.close()
                    print("%-26s %-7d %-10s %-10s %.0f ms"
                          % ("%d 字%s" % (max_chars, " +文件名前缀" if crumb else ""),
                             len(rows),
                             "%d(%.0f%%)" % (hit1, 100.0 * hit1 / total),
                             "%d(%.0f%%)" % (hit5, 100.0 * hit5 / total), dt))
    finally:
        for f in glob.glob(os.path.join(tmpdir, "*")):
            try:
                os.remove(f)
            except Exception:
                pass
        try:
            os.rmdir(tmpdir)
        except Exception:
            pass


def filter_by_df(rows, queries, lo=2, hi=20):
    """只留「在 lo~hi 个切片里出现过」的查询词。

    ⚠️ 为什么需要：随机取词会抽到大量高频虚词（`可以` / `这个` / `我们`），
    它们出现在几百片里，**任何检索器都不可能把"来源那一片"排进前 3** ——
    于是绝对数字天然偏低，看起来像"关键词检索不能用"。
    真实用户搜的是"小红书号""报销""碱水包"这种词，它们的 DF 很小。
    """
    cache = {}
    out = []
    for rid, q, ql in queries:
        if q not in cache:
            cache[q] = sum(1 for _, text in rows if q in text)
        if lo <= cache[q] <= hi:
            out.append((rid, q, ql))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", action="store_true")
    ap.add_argument("--docs", action="store_true")
    ap.add_argument("--chunk", action="store_true")
    ap.add_argument("--kinds", default="uni,trigram,bigram,jieba")
    ap.add_argument("--n", type=int, default=600, help="每类查询最多多少条")
    a = ap.parse_args()
    kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
    if not (a.chat or a.docs or a.chunk):
        a.chat = a.docs = a.chunk = True

    tmpdir = tempfile.mkdtemp(prefix="ragprobe_")
    try:
        if a.chat:
            rows = chat_corpus()
            if rows:
                for mode in ("word", "sub"):
                    qs = queries_from(rows, mode, a.n, seed=7)
                    run_bench("实验一 · 聊天消息（%d 条，avg %.1f 字） · 查询=%-4s"
                              % (len(rows), sum(len(t) for _, t in rows) / len(rows), mode),
                              rows, qs, kinds, tmpdir)
                qs = queries_from(rows, "word", a.n, seed=7)
                run_bruteforce(rows, qs)
            else:
                print("聊天库没有可用数据，跳过实验一")
        if a.docs:
            chunks = doc_chunks()
            rows = [(i + 1, c[1]) for i, c in enumerate(chunks)]
            for mode in ("word", "sub"):
                qs = queries_from(rows, mode, a.n, seed=11)
                run_bench("实验二 · 长文档切片（%d 片，%d 个 md） · 查询=%-4s"
                          % (len(rows), len(set(c[0] for c in chunks)), mode),
                          rows, qs, kinds, tmpdir)
            qs = queries_from(rows, "word", a.n, seed=11)
            run_bruteforce(rows, qs)
            rare = filter_by_df(rows, qs, 2, 20)
            if rare:
                run_bench("实验二b · 长文档切片 · 查询=**稀有词**（DF 2~20，"
                          "更接近真实搜索）",
                          rows, rare, ["uni", "trigram", "bigram", "jieba"], tmpdir)
        if a.chunk:
            run_chunk_sizes()
        run_vector_cost()
    finally:
        for f in glob.glob(os.path.join(tmpdir, "*")):
            try:
                os.remove(f)
            except Exception:
                pass
        try:
            os.rmdir(tmpdir)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
