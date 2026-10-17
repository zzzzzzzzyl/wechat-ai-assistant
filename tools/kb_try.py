# -*- coding: utf-8 -*-
"""拿**你自己的专业资料**试一下"能不能查到"—— 不装产品、不改产品、不改配置。

用途
----
`RAG调研.md` §11.2 说"能不能答对"取决于三件事，其中两件（答案在不在资料里、
问法用词有没有交集）**半天就能验完，不用写一行产品代码**。这个脚本就是那半天要用的：

    1. 把你的资料（md / txt / pdf / docx）丢进一个目录
    2. 把"对方可能怎么问"写成一行一条
    3. 看每条问法能不能把对的段落搜出来

怎么用
------
    python tools\\kb_try.py --docs 资料目录 --queries 问题.txt
    python tools\\kb_try.py --docs a.pdf b.docx --queries q.txt --topk 3
    python tools\\kb_try.py --docs 资料目录 --queries q.txt --export out\\kb_md

参数
----
    --docs      资料目录或文件（.md/.txt/.pdf/.docx，可多个）
    --queries   每行一条问法（**用你真实会遇到的问法**，不要抄资料里的原句）
    --topk      每条问法看前几条（默认 3，和产品一致）
    --chunk     切片长度（默认 400 字）
    --export    把"抽出来的文本 + 切片结果"导出成 md，方便人工修
    --all       把每条问法的前 3 条**全文**打出来（默认只打 80 字预览）

判据（照 `RAG调研.md` §11.2）
------------------------------
- **0 结果的条数**：一条都没有 → 说明"资料里没有你的说法"，去写「常见问法」；
- **首位是不是对的段落**：不对 → 看是不是切片把一条问答切开了；
- 20 条里能对上 16 条（80%）再去做导入器和界面。

⚠️ 只读：本脚本不写任何文件（除非给 `--export`），不联网，不碰微信。
"""

import argparse
import glob
import os
import re
import sqlite3
import sys

# ⚠️ 仓库根进 sys.path：切片的规则**只留一份**，直接用产品里的实现
#    （第一版这里抄了一份 `chunk_text`，结果产品修了 bug、工具还是老的 ——
#     实测那会儿两个实现给出的片数差了 3 倍）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wechat_assistant.kb import (bigrams, chunk_text,   # noqa: E402
                                 sanitize_query)

# ------------------------------------------------------------------ 取文本

def read_txt(path):
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return open(path, encoding=enc).read(), enc
        except UnicodeDecodeError:
            continue
    raise RuntimeError("读不了（不是 UTF-8 / GBK）")


def read_pdf(path):
    """返回 (text, 备注)。⚠️ 表格/多栏会抽坏，扫描件会得到空串。"""
    try:
        import fitz  # PyMuPDF
    except Exception:
        return "", "缺 PyMuPDF，跳过"
    doc = fitz.open(path)
    pages = []
    for i, page in enumerate(doc):
        pages.append(page.get_text("text"))
    doc.close()
    text = "\n".join(pages)
    note = ""
    if not text.strip():
        note = "⚠️ 抽出空文本（大概是扫描件，需要 OCR）"
    return text, note


def read_docx(path):
    """段落 + 表格。⚠️ 文本框/图片里的字抽不到。"""
    try:
        import docx
    except Exception:
        return "", "缺 python-docx，跳过"
    d = docx.Document(path)
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    # ⚠️ 表格**不在 paragraphs 里**，要单独遍历（行业资料里表格最多）
    for t in d.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts), ""


def extract(path):
    ext = os.path.splitext(path)[1].lower()
    note = ""
    if ext in (".md", ".txt"):
        text, enc = read_txt(path)
        if enc != "utf-8":
            note = "⚠️ 按 %s 读的（不是 UTF-8）" % enc
    elif ext == ".pdf":
        text, note = read_pdf(path)
    elif ext == ".docx":
        text, note = read_docx(path)
    else:
        return "", "不支持的格式（只收 md/txt/pdf/docx）"
    return text, note


# ------------------------------------------------------------------ 切片
#
# ⚠️ `chunk_text` 从 `wechat_assistant.kb` **导入**（见文件头的 import）：
#    规则只能有一份。第一版这里抄了一份，结果产品里修了"短段落各占一片"的 bug、
#    工具还是旧的 —— 实测同一份 2,258 字的笔记，产品切 7 片、工具切 24 片。


# ------------------------------------------------------------------ 检索

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


def build_index(rows):
    """rows: [(doc_name, crumb, text)]；返回 (con, table)。"""
    con = sqlite3.connect(":memory:")
    con.execute("create table docs(id integer primary key, doc text, crumb text, text text)")
    con.execute("create virtual table t using fts5(grams, tokenize='unicode61')")
    for i, (doc, crumb, text) in enumerate(rows, 1):
        con.execute("insert into docs(id, doc, crumb, text) values (?,?,?,?)",
                    (i, doc, crumb, text))
        con.execute("insert into t(rowid, grams) values (?,?)", (i, bigrams(text)))
    return con


def search(con, q, topk=3):
    """⚠️ 查询用**二字组 OR**，不用整句 phrase —— 实测差很多（RAG调研.md §5.4）。

    原因：口语问法里必然带着资料里不会出现的词（`多少钱`/`怎么办`/`能吗`）。
    整句 phrase 要求**逐字连续**，语气词一出现就 0 命中；
    OR 只要有一个二字组命中就算候选，再靠 bm25（idf 加权）把对的排前面。

    实测（16 条小知识库，top3 命中）：
        关键词问法    phrase 94%  -> OR 100%
        口语问法      phrase  0%  -> OR  38%
        口语+常见问法  phrase 62%  -> OR  94%
    """
    q = sanitize(q)
    if not q:
        return [], "清洗后为空"
    grams = [g for g in bigrams(q).split() if g.strip()]
    if not grams:
        return [], "清洗后为空"
    # 限制 OR 项数：太长会退化，也不是越长越好（bm25 只看 idf，冗余项没增益）
    grams = grams[:12]
    expr = " OR ".join('"%s"' % g.replace('"', '""') for g in grams)
    try:
        res = con.execute(
            "select d.doc, d.crumb, d.text, bm25(t) from t join docs d on d.id = t.rowid "
            "where t match ? order by bm25(t) limit ?", (expr, topk)).fetchall()
    except Exception as e:
        return [], "查询报错: %s" % e
    return res, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", nargs="+", required=True)
    ap.add_argument("--queries", required=True)
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--chunk", type=int, default=400)
    ap.add_argument("--export", default="")
    ap.add_argument("--all", action="store_true", help="打出前 3 条全文")
    a = ap.parse_args()

    # ---- 收文件
    qpath = os.path.abspath(a.queries)
    files = []
    for d in a.docs:
        if os.path.isdir(d):
            for ext in ("md", "txt", "pdf", "docx"):
                files += glob.glob(os.path.join(d, "**", "*." + ext), recursive=True)
        else:
            files.append(d)
    # ⚠️ **把问题清单自己排除掉**：`--queries` 常常就放在资料目录里（.txt），
    #    不排除的话那份清单会被当成资料，然后**每条问法都命中它自己**（实测踩过），
    #    看起来"命中率 100%"，其实全是假的。
    files = sorted(set(f for f in files if os.path.abspath(f) != qpath))
    if not files:
        print("没找到文件：%s" % " ".join(a.docs))
        return 2

    # ---- 抽文本 + 切片
    rows, report = [], []
    for f in files:
        text, note = extract(f)
        if not text.strip():
            report.append((os.path.basename(f), 0, note or "抽不出文本"))
            continue
        ch = chunk_text(text, a.chunk)
        for crumb, body in ch:
            rows.append((os.path.basename(f), crumb, body))
        report.append((os.path.basename(f), len(ch), note))

    print("=" * 78)
    print("资料 %d 个文件 → %d 片（每片 ≤%d 字）" % (len(files), len(rows), a.chunk))
    print("=" * 78)
    for name, n, note in report:
        print("  %-34s %4d 片   %s" % (name[:34], n, note))
    if not rows:
        print("\n没有可用文本 —— 全是扫描件的话需要 OCR（产品的 rapidocr 能做）。")
        return 3

    con = build_index(rows)

    # ---- 逐条问
    # ⚠️ 问法清单常常是从 markdown 里粘贴的（`- 问题` / `1. 问题`）——
    #    不剥掉前缀的话，`- ` 会跟着进查询（清洗后没影响召回，但打印出来很脏，
    #    而且"0 结果"的统计会跟着变）。
    def _clean_query(line: str) -> str:
        s = line.strip()
        s = re.sub(r"^[-*+]\s+", "", s)
        s = re.sub(r"^\d+[.)、]\s*", "", s)
        return s.strip()

    qs = [_clean_query(l) for l in
          open(a.queries, encoding="utf-8").read().splitlines()
          if l.strip() and not l.lstrip().startswith("#")]
    qs = [q for q in qs if q]
    print("\n" + "=" * 78)
    print("问法 %d 条，每条看前 %d 条" % (len(qs), a.topk))
    print("=" * 78)
    empty = 0
    for q in qs:
        hits, err = search(con, q, a.topk)
        if err:
            empty += 1
            print("\n✗ %s\n    %s" % (q, err))
            continue
        if not hits:
            empty += 1
            print("\n✗ %s\n    0 结果（资料里没有这个说法 → 去写「常见问法」）" % q)
            continue
        print("\n• %s" % q)
        for k, (doc, crumb, text, score) in enumerate(hits, 1):
            head = ("《%s》%s " % (doc, ("› " + crumb) if crumb else "")).strip()
            body = text if a.all else (text[:80] + ("…" if len(text) > 80 else ""))
            print("   %d) %s\n      %s" % (k, head, body.replace("\n", " / ")))

    n = len(qs)
    print("\n" + "-" * 78)
    print("0 结果的 %d/%d 条（%.0f%%）" % (empty, n, 100.0 * empty / max(1, n)))
    print("判据：20 条问法里能对上 16 条（80%）再去做导入器和界面；")
    print("      0 结果多 = 资料里没有用户的说法，先写「常见问法」（实测写了 100%、没写 0%）。")

    # ---- 导出（人工修资料用）
    if a.export:
        os.makedirs(a.export, exist_ok=True)
        by_doc = {}
        for doc, crumb, text in rows:
            by_doc.setdefault(doc, []).append((crumb, text))
        for doc, items in by_doc.items():
            out = os.path.join(a.export, os.path.splitext(doc)[0] + ".md")
            with open(out, "w", encoding="utf-8") as f:
                f.write("# %s（从原文件抽出、已切片；**人工改这一份**再导入）\n\n" % doc)
                for crumb, text in items:
                    if crumb:
                        f.write("## %s\n\n" % crumb)
                    f.write(text + "\n\n")
            print("  导出 %s（%d 片）" % (out, len(items)))
    con.close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
