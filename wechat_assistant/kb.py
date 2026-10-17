# -*- coding: utf-8 -*-
"""本地知识库：导入用户自己的资料，生成回复前在本机检索相关片段。

════════════════════════════════════════════════════════════════════
为什么需要

    微信里对方问的那句话，答案常常在**我自己的资料**里（价目表、售后条款、参数表），
    而不在聊天记录里。没有这个模块，模型只能按常识编 —— 编出来的数字是灾难。

    ⚠️ 这**不是**"让模型会检索"：模型没有检索能力，它只是被动读我们塞进去的东西。
       检索在这里**全是本地字符串匹配**，一次网络请求都不多花（`ai.build_prompts`
       的 `extra_context` 就是它进来的口子）。

三件事，别混淆

    ① 导入（一次性）：读文本 → 切片 → 算二字组 → 写库。**不调模型、不联网**。
    ② 检索（每次生成，约 0.2ms）：问法 → 二字组 OR → FTS5 bm25 排序 → 取前几片。
    ③ 拼接：把命中的片渲染成 `【资料 N】来源：…`，交给 `extra_context`。

════════════════════════════════════════════════════════════════════
⚠️⚠️ 四个**实测**出来的硬约束（改之前先看 `RAG调研.md`）

1. **中文不能用 `tokenize='unicode61'` 直接索引原文**：中文没空格，整段话会被当成
   **一个 token**，2 字查询实测 **94% 返回 0 条**，而且**不报错**。
   → 所以每片存两份：`text`（原文，进 prompt）+ `grams`（重叠二字组，进索引）。

2. **`tokenize='trigram'` 也不行**：它要求查询 ≥3 字，而中文关键词最常见的长度
   就是 2 个字（`报销`/`A2`/`顺丰`），实测 2 字查询 **100% 返回 0 条**。

3. **查询必须用「二字组 OR」，不能用整句 phrase**：口语问法里必然带着资料里没有的
   语气词（`多少钱`/`怎么办`），phrase 要求逐字连续 → 一条都查不到（实测 0/16）。
   换 OR 后 38%，有「常见问法」时 94%。

4. **检索门槛是"相对首位"的比例，不能是绝对分数**：`bm25()` 的分数随查询长度和
   语料规模变化。实测 `min_ratio=0.6` 能砍掉一半噪声片、命中一点不掉（94%→94%）。

════════════════════════════════════════════════════════════════════
其他约定

    · **单库**（2026-10-17 用户定）：全局一份资料，会话级只有一个开关，
      不做多库/跨库 —— 那会引入"同一件事的多个版本同时进 prompt"。
    · **只收 `.md` / `.txt`（UTF-8）**：PDF/docx 的抽取质量（尤其表格）还没做，
      宁可明确报错，也不要"导进去看着成功、其实抽坏了"。
    · **原文件删了不影响已导入内容**：存的是抽出来的文本，`path` 只用来提示。
    · 写入口只有 `add_document` / `remove_document` 两个，**都在导入时同步完成**
      （不挂到记录器每拍里，也不做后台线程）。
"""
import hashlib
import os
import sqlite3
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import paths
from .prompts import KB_GUARD

__all__ = [
    "KBError", "KnowledgeBase",
    "bigrams", "sanitize_query", "build_query", "chunk_text",
    "read_text_file", "CRUMB_MAX",
]

#: 面包屑（"这份文件的哪一节"）最长多少字。
#: ⚠️ 实测（`RAG调研.md` §5.5）：直接把小节标题原样搬进去，
#:    出处那一行能有 40+ 字，白占 prompt 预算 —— 它是给模型交代来源的，不是给它读标题。
CRUMB_MAX = 20

#: 单片上限。实测 150/300/400/800 字对召回只差 5pp，取中间的。
DEFAULT_CHUNK_CHARS = 400

#: 一次导入最多多少片（防止有人拖进 100MB 的手册把索引撑爆）
MAX_CHUNKS = 200000

#: 检索时先取多少条候选（给"相对门槛剪枝"和"总字数额度"留余量）。
#: ⚠️ 别取 3：剪枝后可能一条不剩 —— 症状是"明明有资料却查不到"。
FETCH_N = 12

#: 库结构的版本（`PRAGMA user_version`）。改了表结构就 +1 并在 `_migrate` 里加一段。
#:   1 = 2026-10-17 加 `grp`（分组）
#:   2 = 2026-10-17 加 `kb_groups`（能存**空组**：先建组、再往里加文件）
#:   3 = 2026-10-17 **把小节标题（面包屑）也拼进索引** —— 用户实测踩到：
#:       问「分布式训练为什么要并行」，而那句正是**小节标题**，标题不进索引 → 查不到
SCHEMA_VERSION = 3

#: 空串（"不分到任何组"）在界面上的显示名。⚠️ 用户要求叫「默认」而不是「未分组」。
NO_GROUP_LABEL = "默认"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,   -- 文件显示名（只是名字，不是路径）
    path        TEXT,                      -- 只用于"重新导入"的提示，失效不算错
    grp         TEXT    DEFAULT '',        -- 分类（**只影响显示和批量操作，不参与检索**）
    sha256      TEXT    NOT NULL,          -- 内容指纹：没变就跳过
    chunks      INTEGER DEFAULT 0,
    chars       INTEGER DEFAULT 0,
    added_at    TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL,
    ord         INTEGER NOT NULL,          -- 第几片（拼上下文时保证阅读顺序）
    crumb       TEXT    DEFAULT '',        -- 来自哪个小节（**已经截到 CRUMB_MAX**）
    text        TEXT    NOT NULL,          -- 原文：进 prompt 的就是它
    grams       TEXT    NOT NULL           -- 重叠二字组：进 FTS 索引的就是它
);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_doc ON kb_chunks(doc_id, ord);

--: 分组的**实体表**（2026-10-17 用户要求"先建组、再往组里加文件"）。
--:
--: ⚠️ 为什么需要它：`kb_docs.grp` 只是"某份文件属于哪个组"的字符串，
--:    **空组存不下来** —— 刚建好、还没加文件的组会直接消失，用户看不到自己建的组。
--: ⚠️ 组名仍然是**字符串**（不改成 id）：改名就是一条 UPDATE，比维护外键简单，
--:    而且"分组只是标签"这个心智更直白。重名靠主键兜住。
CREATE TABLE IF NOT EXISTS kb_groups (
    name        TEXT    PRIMARY KEY,
    ord         INTEGER DEFAULT 0,
    created_at  TEXT    NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS kb_index USING fts5(
    grams,
    content='kb_chunks',
    content_rowid='id',
    tokenize='unicode61'
);
"""


class KBError(Exception):
    """导入阶段的错误（编码不对 / 没有文本 / 太大）。**界面上要显示出来**。"""


# ---------------------------------------------------------------- 纯函数（好测）

def bigrams(text: str, n: int = 2) -> str:
    """把一段中文切成**重叠 n 字组**，用空格连起来交给 unicode61。

        「我们明天开会」 -> 「我们 们明 明天 天开 开会」

    ⚠️ 必须是**重叠**的：检索时把问法也切成二字组并用 OR 连起来，
       靠"相邻二字组首尾相接"才能表示"这几个字是挨着的"。
    """
    t = "".join((text or "").replace("*", "").split())
    if len(t) < n:
        return t
    return " ".join(t[i:i + n] for i in range(len(t) - n + 1))


#: 会破坏 FTS5 查询语法的字符（**只作说明**，实际用白名单清洗，见 `sanitize_query`）。
#:
#: ⚠️⚠️ 实测两条血案（`RAG调研.md` §3.5）：
#:   · 问法里带一个有 `"` → FTS5 抛 `unterminated string`（**不是返回 0 条**）；
#:   · 清洗后为空（`**` / `→ ` / `）·`）→ 抛 `fts5: syntax error near ""`。
#: 而调用方一定会把检索包在 try/except 里（项目铁律 #2），于是症状变成
#: 「检索静默永不生效」—— 单测全绿、提示词里永远没有【参考资料】。
#: 结论：**不认识的字符根本不要进查询**，空串就返回 ""，由调用方跳过检索。


def sanitize_query(q: str) -> str:
    """只留中英文和数字（其余当分隔符），去掉会破坏 FTS5 语法的字符。"""
    out = []
    for ch in (q or ""):
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
    return "".join(out).strip()


def build_query(query: str, max_terms: int = 12) -> str:
    """问法 → FTS5 查询表达式（二字组 OR）。空串表示"这次不检索"。

    ⚠️ **用 OR，不用整句 phrase**（理由见模块头第 3 条）。
    ⚠️ 项数上限 12：长问法全进去没有增益（bm25 只看 idf），还会拖慢。
    """
    q = sanitize_query(query)
    if not q:
        return ""
    grams = [g for g in bigrams(q).split() if g.strip()][:max_terms]
    if not grams:
        return ""
    return " OR ".join('"%s"' % g.replace('"', '""') for g in grams)


def chunk_text(text: str, max_chars: int = DEFAULT_CHUNK_CHARS
               ) -> List[Tuple[str, str]]:
    """切文本 → [(面包屑, 这一片的原文)]。

    规则（和 `RAG调研.md` §5.2 一致）：

      · **按标题切**：标题行（`#` 开头）一定给它前面收尾，标题文字成为后文的面包屑；
      · **按长度攒**：累计到 `max_chars` 才切，**短段落不各占一片**；
      · **空行是"首选切点"**：攒到六成以上又遇到空行就切（切口落在段落边界更自然）；
      · **表格整块不拆**：`|` 开头的连续行另起一片、且不因长度打断（切开就没法读了）。

    ⚠️⚠️ **"短段落不各占一片"是实测补的**（2026-10-17）：
        第一版是"见到空行就切"，结果一份 2,258 字的笔记被切成 **24 片**
        （平均 94 字/片）—— 而 markdown 里每个段落、每个列表项后面都有空行。
        碎片化有两个坏处：① 同一节的内容散在多片里，模型只看到一半；
        ② 索引体积和检索噪声都变大。修完是 7 片。
    """
    chunks: List[Tuple[str, str]] = []
    crumb = ""
    buf: List[str] = []
    size = 0
    in_table = False

    def flush() -> None:
        nonlocal buf, size, in_table
        body = "\n".join(buf).strip()
        if body:
            chunks.append((crumb, body))
        buf = []
        size = 0
        in_table = False

    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            # 空行只是"段落边界"：攒够六成才在这儿切，短段落并进去
            if size >= max_chars * 0.6:
                flush()
            in_table = False
            continue
        if s.startswith("#"):
            flush()
            crumb = s.lstrip("#").strip()[:CRUMB_MAX]
            continue
        is_table = s.startswith("|")
        if is_table and not in_table and buf:
            # 表格另起一片 —— 保证整张表在同一片里
            flush()
        elif not is_table and buf and size + len(s) > max_chars:
            flush()
        buf.append(s)
        size += len(s)
        in_table = is_table
    flush()
    return chunks


def read_text_file(path: str) -> str:
    """读纯文本资料。**只认 UTF-8**（含 BOM）。

    ⚠️ 不猜 GBK：中文资料编码猜错会变成乱码进库，**而且再也洗不掉**
       （库里存的是乱码文本）。宁可报错让用户另存一次。
    """
    for enc in ("utf-8-sig", "utf-8"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
        except OSError as e:
            raise KBError("读不了这个文件：%s" % e)
    raise KBError("不是 UTF-8 编码（请另存为 UTF-8 再导入）")


# ---------------------------------------------------------------- 多格式读取
#
# ⚠️ 三种格式的坑完全不同（2026-10-17 加，用户要求支持 pdf / word）：
#
#   · **PDF**：有可能是扫描件（没有文字层）→ 抽出来是空。这里走**两级**：
#       先读文字层；整页读不到再**渲染成图交给现有 OCR 引擎**（`ocr.py`，
#       `rapidocr` 本来就在包里，不新增依赖）。OCR 很慢（约 0.4s/页），
#       所以只在"这页真的没字"时才付这个代价。
#   · **DOCX**：`python-docx` 的 `paragraphs` **不含表格**，表格要单独遍历
#       （行业资料里表格最多）；而且它**不保证文档顺序**（表格会被排到最后），
#       所以表格里的内容在 prompt 里可能离它上下文有点远 —— 已知取舍。
#   · 两种都会**按页/按标题造出 `#` 小节**，这样切片天然拿到面包屑
#       （「第 3 页」「第三章」），检索结果里的"出处"才有意义。
#
# ⚠️ 依赖是**懒加载**的：没装 pymupdf / python-docx 时只有对应格式报错，
#    不影响 md/txt（也不会在启动时把这两个库拖进内存）。

#: 支持导入的扩展名（小写）。`ALL_EXTS` 给界面做文件对话框过滤用。
TEXT_EXTS = (".md", ".txt")
PDF_EXTS = (".pdf",)
DOCX_EXTS = (".docx",)
ALL_EXTS = TEXT_EXTS + PDF_EXTS + DOCX_EXTS

#: 扫描件 PDF 渲染成图时放大几倍（2 倍 ≈ 144dpi；实测 OCR 够用，再大只是变慢）
PDF_OCR_ZOOM = 2.0


def _ocr_image(arr, logger=None) -> str:
    """把一张图交给现有 OCR 引擎，按行拼回文本。"""
    from . import ocr as ocr_mod         # 懒加载：不导扫描件就不加载 onnxruntime

    blocks = ocr_mod.get_engine(logger=logger).recognize(arr, min_score=0.5)
    if not blocks:
        return ""
    blocks.sort(key=lambda b: (b.get("y1", 0), b.get("x1", 0)))
    return "\n".join(b.get("text", "") for b in blocks if b.get("text"))


def read_pdf(path: str, ocr_scanned: bool = False,
             logger=None) -> Tuple[str, str]:
    """读 PDF → `(text, note)`。`note` 非空表示有页没读到文字（要告诉用户）。

    **默认不 OCR**（`ocr_scanned=False`，2026-10-17 用户要求）：
    有文字层的 PDF 用 `get_text()` **直接抽**（这就是"读取工具"，快、不联网、
    不加载 OCR 引擎）；**扫描件没有文字层，任何抽取工具都抽不出字** ——
    要么用户在外面转成文字，要么显式打开 `kb.ocr_pdf`。

    ⚠️ 有文字层的页**永远不走 OCR**（快得多，也不会把排版认错）。
    """
    try:
        import fitz                        # PyMuPDF
    except Exception:
        raise KBError("不支持 PDF：没装 PyMuPDF。装一下就行（pip install pymupdf）")

    try:
        doc = fitz.open(path)
    except Exception as e:
        raise KBError("这个 PDF 打不开（可能加密或已损坏）：%s" % e)

    parts: List[str] = []
    no_text: List[int] = []
    ocr_pages = 0
    try:
        for i, page in enumerate(doc, 1):
            raw = page.get_text("text") or ""
            # ⚠️ 中文 PDF 抽出来常带 **不换行空格 `\xa0`**（实测微软雅黑导出的就是）。
            #    它会让界面上显示成"型号 参数"这种怪东西，先统一成普通空格。
            text = raw.replace("\xa0", " ").strip()
            if not text and ocr_scanned:
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(PDF_OCR_ZOOM,
                                                             PDF_OCR_ZOOM))
                    import numpy as np
                    arr = np.frombuffer(pix.samples, dtype=np.uint8)
                    arr = arr.reshape(pix.height, pix.width, pix.n)
                    if pix.n > 3:
                        arr = arr[:, :, :3]
                    text = _ocr_image(arr, logger=logger).strip()
                    if text:
                        ocr_pages += 1
                except Exception as e:
                    if logger:
                        logger("[KB] 第 %d 页 OCR 失败（跳过这页）: %s: %s"
                               % (i, type(e).__name__, e))
                    text = ""
            if not text:
                no_text.append(i)
                continue
            parts.append("## 第 %d 页\n%s" % (i, text))
    finally:
        doc.close()

    if not parts:
        raise KBError("这份 PDF 里抽不出文字（扫描件没有文字层）。"
                      "可以先用别的工具转成文字再导入，或在配置里打开 OCR")
    note = ""
    if no_text:
        note = "有 %d 页没有文字层（第 %s 页）%s" % (
            len(no_text),
            "、".join(str(x) for x in no_text[:5]) + ("…" if len(no_text) > 5 else ""),
            ("，已用 OCR 认出 %d 页" % ocr_pages) if ocr_pages
            else "，这些页没导入（扫描件要转成文字）")
    return "\n\n".join(parts), note


def read_docx(path: str) -> Tuple[str, str]:
    """读 Word（`.docx`）→ `(text, note)`。段落 + 表格。

    ⚠️ **不支持老的 `.doc`**（二进制格式，`python-docx` 读不了）——
        明确报错并告诉用户怎么转，别让他对着"导入失败"猜。
    """
    try:
        import docx
    except Exception:
        raise KBError("不支持 Word：没装 python-docx。装一下就行（pip install python-docx）")

    try:
        d = docx.Document(path)
    except Exception as e:
        raise KBError("这个 Word 打不开（可能不是 .docx，或者已损坏）：%s" % e)

    parts: List[str] = []
    for p in d.paragraphs:
        t = (p.text or "").strip()
        if not t:
            continue
        # 标题段落 → markdown 标题，切片的"面包屑"就有了
        try:
            style = (p.style.name or "").lower()
        except Exception:
            style = ""
        if style.startswith("heading"):
            parts.append("## " + t)
        else:
            parts.append(t)

    n_tables = 0
    for t in d.tables:
        rows = []
        for row in t.rows:
            cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            n_tables += 1
            parts.append("\n".join(rows))

    text = "\n".join(parts).strip()
    if not text:
        raise KBError("这个 Word 里没有文字（可能全是图片或文本框）")
    note = ""
    if n_tables:
        note = "含 %d 张表格，已按行拼成文本（表格会排在正文后面）" % n_tables
    return text, note


def read_document(path: str, ocr_scanned: bool = False,
                  logger=None) -> Tuple[str, str]:
    """按扩展名分发 → `(text, note)`。不认识的格式抛 `KBError`（说清怎么办）。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXTS:
        return read_text_file(path), ""
    if ext in PDF_EXTS:
        return read_pdf(path, ocr_scanned=ocr_scanned, logger=logger)
    if ext in DOCX_EXTS:
        return read_docx(path)
    if ext == ".doc":
        raise KBError("不支持老的 .doc 格式，请用 Word 另存为 .docx 或导出 PDF")
    raise KBError("不支持的格式（%s）：只收 Markdown / 纯文本 / PDF / Word(.docx)" % ext)



def grams_of(crumb: str, text: str) -> str:
    """一片进 FTS 的文本 = **小节标题的二字组 + 正文的二字组**。

    ⚠️⚠️ **标题必须进索引**（2026-10-17 用户实测）：
        问「分布式训练为什么要并行」时，那句正是**小节标题**，而正文里只写
        「单卡放不下是常态…」—— 标题不进索引就**完全查不到**（实测命中了别的文件）。
        `RAG调研.md` §3.6 那条"面包屑加了不提分"是**用正文词做查询**量出来的，
        没覆盖"用户拿标题里的词来问"这种情况 —— 那次结论范围写窄了。

    ⚠️ 两段分开拼接（各自 bigram 再用空格连），**不要先把标题和正文粘起来**：
        粘起来会在接缝处生出 "行单"/"行单卡" 这种跨边界的二字组，凭空多出噪声。
    """
    parts = []
    c = bigrams(crumb or "")
    if c:
        parts.append(c)
    t = bigrams(text or "")
    if t:
        parts.append(t)
    return " ".join(parts)


def _sha256(text: str) -> str:
    """按**内容**算指纹（不按文件字节：换行符差异不该触发重建）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 知识库

class KnowledgeBase:
    """本地知识库（单库）。线程安全：每次操作自己开关连接。"""

    def __init__(self, path: Optional[str] = None,
                 chunk_chars: int = DEFAULT_CHUNK_CHARS,
                 ocr_pdf: bool = False,
                 logger: Callable[[str], None] = None):
        self.path = os.path.abspath(path or paths.resolve("state", "kb.db"))
        self.chunk_chars = int(chunk_chars)
        #: 扫描件 PDF 要不要用 OCR 兜底。**默认关**（2026-10-17 用户要求）：
        #: 有文字层的 PDF 根本不需要它；扫描件没有文字层，任何抽取工具都抽不出字，
        #: 想让程序硬认才打开这个开关（慢，约 0.4s/页）。
        self.ocr_pdf = bool(ocr_pdf)
        self._log = logger or (lambda msg: None)
        self._lock = threading.Lock()
        self._ensure_schema()

    # -- 基础设施 ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        paths.ensure_parent(self.path)
        con = sqlite3.connect(self.path, timeout=5.0)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _ensure_schema(self) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.executescript(SCHEMA)
                con.executescript(FTS_SCHEMA)
                self._migrate(con)
                con.commit()
            finally:
                con.close()

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        """把老库升级到当前结构（**老数据一条都不能丢**）。

        版本记在 `PRAGMA user_version`（SQLite 自带，不用自己建表）：
            0 —— 最早版（没有 `grp`）
            1 —— 有 `grp`、但分组没有实体表（空组建不出来）
            2 —— 当前：多了 `kb_groups`

        ⚠️ 用 `ALTER TABLE ADD COLUMN` 而不是"建新表再复制"：老库里可能有几千片，
           重建要动 kb_chunks 和 FTS 索引，出错面大得多。
        ⚠️ 老资料一律落 **默认组（''）**，界面上显示成「默认」。
        ⚠️ 升级到 2 时要把**已有的组名灌进 `kb_groups`** —— 不然老库的组在
           `list_groups()` 里只能从文档里推出来，而"文档全被删了的组"会凭空消失。
        """
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        if ver >= SCHEMA_VERSION:
            return
        cols = {r[1] for r in con.execute("PRAGMA table_info(kb_docs)").fetchall()}
        if "grp" not in cols:
            con.execute("ALTER TABLE kb_docs ADD COLUMN grp TEXT DEFAULT ''")
        # 表本身由 `SCHEMA` 的 CREATE IF NOT EXISTS 建好了；这里只补数据
        try:
            con.execute(
                "insert or ignore into kb_groups(name, ord, created_at)"
                " select distinct grp, 0, ? from kb_docs where grp <> ''",
                (time.strftime("%Y-%m-%d %H:%M:%S"),))
        except sqlite3.Error:
            # 极端情况（表还没建出来）：迁移不该因此失败，下次打开再补
            pass
        if ver < 3:
            # ⚠️ 3：把**小节标题**也拼进索引。老片必须重算 `grams`，否则
            #    "拿标题里的词来问"在这些老资料上永远查不到（用户实测踩到）。
            #    ⚠️ **不用重新读原文件**：`text` 和 `crumb` 都在库里，就地重算。
            for cid, crumb, text in con.execute(
                    "select id, crumb, text from kb_chunks").fetchall():
                con.execute("update kb_chunks set grams=? where id=?",
                            (grams_of(crumb or "", text or ""), cid))
            # 外部内容表：索引整块重建（FTS5 自带命令）
            con.execute("insert into kb_index(kb_index) values('rebuild')")
        con.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)

    # -- 导入 / 删除 ---------------------------------------------------

    def add_document(self, path: str, grp: Optional[str] = None) -> Dict:
        """导入一份资料。返回 {name, chunks, chars, skipped, replaced, grp}。

        ⚠️ 同名文件：内容没变 → `skipped=True`（不重建）；内容变了 → 覆盖重建。
        ⚠️ 失败抛 `KBError`（**界面上要显示**，别只打日志）。
        ⚠️ `grp=None`（默认）= **沿用这份资料原来的分组**（新资料落「默认」组）。
           这条是给「全部重新导入」用的：改个文件不该把它踢回「默认」组。
        """
        name = os.path.basename(path)
        text, note = read_document(path, ocr_scanned=self.ocr_pdf, logger=self._log)
        digest = _sha256(text)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")

        chunks = chunk_text(text, self.chunk_chars)
        if not chunks:
            raise KBError("这个文件里没有可用的文字（可能是空的，或者只有格式）")
        if len(chunks) > MAX_CHUNKS:
            raise KBError("文件太大（%d 片，上限 %d）" % (len(chunks), MAX_CHUNKS))
        n_chars = sum(len(t) for _, t in chunks)

        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    "select id, sha256, chunks, grp from kb_docs where name=?", (name,)
                ).fetchone()
                old_grp = (row[3] if row else "") or ""
                use_grp = old_grp if grp is None else (grp or "")
                if row and row[1] == digest:
                    if use_grp != old_grp:
                        # 内容没变、只改了分组：只 UPDATE 那一列，别重建片
                        con.execute("update kb_docs set grp=? where id=?",
                                    (use_grp, row[0]))
                        con.commit()
                    return {"name": name, "chunks": row[2] or 0, "grp": use_grp,
                            "chars": n_chars, "skipped": True, "replaced": False,
                            "note": note}
                old_id = row[0] if row else None
                if old_id:
                    con.execute("delete from kb_index where rowid in "
                                "(select id from kb_chunks where doc_id=?)", (old_id,))
                    con.execute("delete from kb_chunks where doc_id=?", (old_id,))
                    con.execute("delete from kb_docs where id=?", (old_id,))
                if use_grp:
                    # 组名登记进实体表：否则"文档被删光之后组就消失了"
                    con.execute("insert or ignore into kb_groups(name, ord, created_at)"
                                " values (?, 0, ?)", (use_grp, stamp))
                cur = con.execute(
                    "insert into kb_docs(name, path, grp, sha256, chunks, chars,"
                    " added_at, updated_at) values (?,?,?,?,?,?,?,?)",
                    (name, os.path.abspath(path), use_grp, digest, len(chunks),
                     n_chars, stamp, stamp))
                doc_id = cur.lastrowid
                for i, (crumb, body) in enumerate(chunks):
                    c = con.execute(
                        "insert into kb_chunks(doc_id, ord, crumb, text, grams)"
                        " values (?,?,?,?,?)",
                        (doc_id, i, crumb, body, grams_of(crumb, body)))
                    con.execute("insert into kb_index(rowid, grams) values (?,?)",
                                (c.lastrowid, grams_of(crumb, body)))
                con.commit()
                self._log("[KB] 导入《%s》：%d 片 / %d 千字%s%s"
                          % (name, len(chunks), max(1, n_chars // 1000),
                             ("（分组：%s）" % use_grp) if use_grp else "",
                             "（覆盖旧版）" if old_id else ""))
                if note:
                    self._log("[KB] 《%s》：%s" % (name, note))
                return {"name": name, "chunks": len(chunks), "chars": n_chars,
                        "grp": use_grp, "skipped": False, "replaced": bool(old_id),
                        "note": note}
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()

    def import_tree(self, root: str, grp: Optional[str] = None) -> Dict:
        """整目录导入。

        `grp="X"` → **整个文件夹变成一个新的组**，组名就是文件夹名（界面走这条）
        `grp=None` → 老行为：子目录名当组名（CLI / 测试用）

        返回 `{ok, skipped, groups, errors: [(文件名, 原因)]}`。

        ⚠️ 2026-10-17 用户改的口径：「添加文件夹」= **整个文件夹就是一个组**，
           不再按子目录拆。选 `D:\\资料\\AI` 就得到组「AI」，里面所有文件
           （含子目录里的）都归它 —— 想分两个组就选两次不同文件夹。
        ⚠️ 文件夹名如果正好是保留名（「默认」），归到默认组（`grp=''`），不报错。
        ⚠️ **空文件夹也会建出那个组**（用户建了就该看得见）。
        ⚠️ 顺序：**先按路径排序**再导（结果可复现，日志也好对）。
        """
        root = os.path.abspath(root)
        files = []
        ignored: Dict[str, int] = {}
        for dirpath, _dirs, names in os.walk(root):
            for fn in names:
                ext = os.path.splitext(fn)[1].lower()
                if ext in ALL_EXTS:
                    files.append(os.path.join(dirpath, fn))
                elif ext:
                    # ⚠️ 不支持的格式要**数出来**（2026-10-17 用户要求"文件夹里的其他格式
                    #    也要读"）：静默跳过的话，用户会以为整个文件夹都导进来了。
                    ignored[ext] = ignored.get(ext, 0) + 1
        files.sort()

        def _one(grp_name: str) -> str:
            g = (grp_name or "").strip()
            return "" if g == NO_GROUP_LABEL else g

        groups = set()
        if grp is not None:
            g = _one(grp)
            if g:
                try:
                    self.create_group(g)     # 空文件夹也要看得见
                except KBError:
                    pass                     # 已存在 -> 往里加就是了
                groups.add(g)

        ok = skipped = 0
        errors: List[Tuple[str, str]] = []
        notes: List[Tuple[str, str]] = []
        for p in files:
            if grp is not None:
                g = _one(grp)
            else:
                rel = os.path.relpath(os.path.dirname(p), root)
                g = "" if rel in (".", "") else rel.replace("\\", "/")
            try:
                r = self.add_document(p, grp=g)
                if r.get("skipped"):
                    skipped += 1
                else:
                    ok += 1
                if r.get("note"):
                    notes.append((os.path.basename(p), r["note"]))
                groups.add(g)
            except Exception as e:
                errors.append((os.path.basename(p), str(e)))
        return {"ok": ok, "skipped": skipped, "groups": sorted(x for x in groups if x),
                "errors": errors, "notes": notes, "ignored": ignored}

    def set_group(self, name: str, grp: str) -> bool:
        """改一份资料的分组（**只影响显示**，不重建片、不动索引）。"""
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute("update kb_docs set grp=? where name=?",
                                  ((grp or "").strip(), name))
                con.commit()
                return cur.rowcount > 0
            finally:
                con.close()

    def remove_group(self, grp: str) -> int:
        """删掉一整组。返回删掉的资料份数。"""
        names = [d["name"] for d in self.list_documents() if d["grp"] == (grp or "")]
        for n in names:
            self.remove_document(n)
        return len(names)

    def remove_document(self, name: str) -> int:
        """删掉一份资料（连带它的片和索引）。返回删掉的片数。"""
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("select id from kb_docs where name=?",
                                  (name,)).fetchone()
                if not row:
                    return 0
                doc_id = row[0]
                n = con.execute("select count(*) from kb_chunks where doc_id=?",
                                (doc_id,)).fetchone()[0]
                con.execute("delete from kb_index where rowid in "
                            "(select id from kb_chunks where doc_id=?)", (doc_id,))
                con.execute("delete from kb_chunks where doc_id=?", (doc_id,))
                con.execute("delete from kb_docs where id=?", (doc_id,))
                con.commit()
                self._log("[KB] 移除《%s》：%d 片" % (name, n))
                return n
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()

    # -- 查询状态 ------------------------------------------------------

    def list_documents(self) -> List[Dict]:
        """已导入的资料列表（给设置界面用）。按「分组 → 文件名」排好序。

        ⚠️ 排序在这里做（不是界面里）：分组要按名字排、**「默认」永远排最后**，
            两个地方各写一遍必然漂。
        """
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    "select name, path, grp, chunks, chars, added_at, updated_at"
                    " from kb_docs").fetchall()
            finally:
                con.close()
        out = []
        for name, path, grp, chunks, chars, added, updated in rows:
            out.append({"name": name, "path": path or "", "grp": grp or "",
                        "chunks": chunks or 0,
                        "chars": chars or 0, "added_at": added or "",
                        "updated_at": updated or "",
                        "path_exists": bool(path) and os.path.exists(path)})
        # 默认组（''）排最后：`(grp == "", grp, name)` —— False < True，正好
        out.sort(key=lambda d: (d["grp"] == "", d["grp"], d["name"]))
        return out

    def groups(self) -> List[Dict]:
        """分组的列表（含**空组**），按「有名字的按名字、「默认」最后」排好。

        每项：`{grp, label, docs, chunks, chars, names}` —— `grp == ''` 那一项是「默认」。

        ⚠️ 组名来源是**两处合并**：`kb_groups` 表（能存空组）+ 文档里出现过的 `grp`。
            只看文档的话，刚建好还没加文件的组会消失（用户说"我明明建了"）；
            只看表的话，老库里没有登记过的组会漏。
        """
        names: List[str] = []
        with self._lock:
            con = self._connect()
            try:
                for (n,) in con.execute(
                        "select name from kb_groups order by ord, name").fetchall():
                    if n and n not in names:
                        names.append(n)
            finally:
                con.close()
        docs = self.list_documents()
        for d in docs:
            if d["grp"] and d["grp"] not in names:
                names.append(d["grp"])
        stats: Dict[str, Dict] = {}
        for d in docs:
            item = stats.setdefault(d["grp"], {"docs": 0, "chunks": 0, "chars": 0,
                                               "names": []})
            item["docs"] += 1
            item["chunks"] += d["chunks"]
            item["chars"] += d["chars"]
            item["names"].append(d["name"])
        out: List[Dict] = []
        for g in sorted(names):
            s = stats.get(g, {"docs": 0, "chunks": 0, "chars": 0, "names": []})
            out.append({"grp": g, "label": g, "docs": s["docs"],
                        "chunks": s["chunks"], "chars": s["chars"],
                        "names": s["names"]})
        # ⚠️ **「默认」组永远出现**（哪怕它是空的）：顶部的「添加文件…」就是往这里加，
        #    它要是被藏起来，用户加完文件会发现"凭空冒出一个组"。
        s = stats.get("", {"docs": 0, "chunks": 0, "chars": 0, "names": []})
        out.append({"grp": "", "label": NO_GROUP_LABEL, "docs": s["docs"],
                    "chunks": s["chunks"], "chars": s["chars"],
                    "names": s["names"]})
        return out

    def create_group(self, name: str) -> str:
        """新建一个**空组**（`kb_docs.grp` 是字符串，空组只能记在这张表里）。

        返回实际存进去的组名；名字非法（空 / 「默认」/ 已存在）抛 `KBError`。
        """
        name = (name or "").strip()
        if not name:
            raise KBError("组名不能为空")
        if name == NO_GROUP_LABEL:
            raise KBError("「%s」是保留名字，换一个" % NO_GROUP_LABEL)
        if len(name) > 40:
            raise KBError("组名太长（最多 40 字）")
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    "insert or ignore into kb_groups(name, ord, created_at)"
                    " values (?, 0, ?)",
                    (name, time.strftime("%Y-%m-%d %H:%M:%S")))
                con.commit()
                if cur.rowcount == 0:
                    raise KBError("已经有一个叫「%s」的组了" % name)
            finally:
                con.close()
        self._log("[KB] 新建分组：%s" % name)
        return name

    def rename_group(self, old: str, new: str) -> int:
        """改组名（连组里的资料一起改）。返回改了几份资料。"""
        old, new = (old or "").strip(), (new or "").strip()
        if not new:
            raise KBError("组名不能为空")
        if new == NO_GROUP_LABEL:
            raise KBError("「%s」是保留名字，换一个" % NO_GROUP_LABEL)
        if old == new:
            return 0
        with self._lock:
            con = self._connect()
            try:
                if con.execute("select 1 from kb_groups where name=?", (new,)).fetchone():
                    raise KBError("已经有一个叫「%s」的组了" % new)
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                con.execute("insert or ignore into kb_groups(name, ord, created_at)"
                            " values (?, 0, ?)", (new, stamp))
                cur = con.execute("update kb_docs set grp=? where grp=?", (new, old))
                con.execute("delete from kb_groups where name=?", (old,))
                con.commit()
                n = cur.rowcount
            finally:
                con.close()
        return n

    def delete_group(self, name: str, with_docs: bool = True) -> int:
        """删掉一个组。`with_docs=True` 连组里的资料一起删（返回删了几份）。

        ⚠️ 组里还有资料、又不想删资料时，调 `delete_group(name, with_docs=False)`：
           资料会落回「默认」组（**不丢**），组名消失。
        """
        name = (name or "").strip()
        names = [d["name"] for d in self.list_documents() if d["grp"] == name]
        if with_docs:
            for n in names:
                self.remove_document(n)
        else:
            with self._lock:
                con = self._connect()
                try:
                    con.execute("update kb_docs set grp='' where grp=?", (name,))
                    con.commit()
                finally:
                    con.close()
        with self._lock:
            con = self._connect()
            try:
                con.execute("delete from kb_groups where name=?", (name,))
                con.commit()
            finally:
                con.close()
        self._log("[KB] 删除分组《%s》%s" % (name, "（连同 %d 份资料）" % len(names)
                                            if with_docs else "（资料落回「默认」）"))
        return len(names) if with_docs else 0

    def summary(self) -> Dict:
        """「已导入 N 份 · 共 M 片 · K 千字」——界面和日志都用这一份，别各算一份。"""
        with self._lock:
            con = self._connect()
            try:
                docs, chunks, chars = con.execute(
                    "select count(*), coalesce(sum(chunks),0), coalesce(sum(chars),0)"
                    " from kb_docs").fetchone()
            finally:
                con.close()
        return {"docs": docs or 0, "chunks": chunks or 0, "chars": chars or 0}

    def dump(self, name: Optional[str] = None, limit_chars: int = 8000) -> str:
        """把各片的原文拼成一段文本（给设置里的「查看抽出的内容」用）。

        ⚠️ 为什么需要它：抽文本**抽坏了不会报错** —— 扫描件抽成空、表格抽成一列乱序，
        导进去"看着成功"、检索却永远查不到。用户在预览里一眼能看出不对，
        否则只能对着"明明导了却查不到"瞎猜。
        """
        sql = ("select d.name, c.crumb, c.text from kb_chunks c"
               " join kb_docs d on d.id = c.doc_id")
        args: tuple = ()
        if name:
            sql += " where d.name = ?"
            args = (name,)
        sql += " order by d.name, c.ord"
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(sql, args).fetchall()
            finally:
                con.close()
        out: List[str] = []
        used = 0
        cur = None
        for doc, crumb, text in rows:
            if doc != cur:
                cur = doc
                block = "\n＝＝＝ 《%s》 ＝＝＝\n" % doc
                out.append(block)
                used += len(block)
            seg = ""
            if crumb:
                seg += "· [%s]\n" % crumb
            seg += (text or "").strip() + "\n"
            if used + len(seg) > limit_chars:
                out.append("……（后面还有，先只看这么多）\n")
                break
            out.append(seg)
            used += len(seg)
        return "".join(out).strip()

    # -- 检索 ----------------------------------------------------------

    def search(self, query: str, top_k: int = 3, min_ratio: float = 0.6,
               fetch: int = FETCH_N) -> List[Dict]:
        """检索 → [{doc, crumb, text, score}]，按相关度排序。

        ⚠️ **任何异常都返回 []（打一行日志）**：检索坏了不许让生成失败。
        ⚠️ `min_ratio`：只保留分数不差于"首位 × 0.6"的片（bm25 是负数，越小越相关）。
        """
        expr = build_query(query)
        if not expr:
            return []
        try:
            with self._lock:
                con = self._connect()
                try:
                    rows = con.execute(
                        "select d.name, c.crumb, c.text, bm25(kb_index)"
                        " from kb_index"
                        " join kb_chunks c on c.id = kb_index.rowid"
                        " join kb_docs   d on d.id = c.doc_id"
                        " where kb_index match ?"
                        " order by bm25(kb_index) limit ?", (expr, int(fetch))
                    ).fetchall()
                finally:
                    con.close()
        except sqlite3.Error as e:
            self._log("[KB] 检索失败（当作没查到，不影响生成）: %s" % e)
            return []

        if not rows:
            return []
        top = rows[0][3]
        cut = (top * float(min_ratio)) if top < 0 else None
        out: List[Dict] = []
        for name, crumb, text, score in rows:
            if cut is not None and score > cut:
                continue
            out.append({"doc": name, "crumb": crumb or "",
                        "text": text or "", "score": score})
            if len(out) >= max(1, int(top_k)):
                break
        return out

    def render(self, query: str, top_k: int = 3, min_ratio: float = 0.6,
               max_chars: int = 600, per_chunk: int = 300) -> str:
        """检索并渲染成 `extra_context`（**不含**【参考资料】标题，那由 ai 拼）。

        返回 "" = 这次没有可用资料（调用方 `extra_context=""` 时整段都不出现）。
        """
        hits = self.search(query, top_k=top_k, min_ratio=min_ratio)
        if not hits:
            return ""
        parts: List[str] = []
        used = 0
        for i, h in enumerate(hits, 1):
            body = h["text"].strip()
            if len(body) > per_chunk:
                body = body[:per_chunk] + "…（这节还有内容，已截断）"
            src = "《%s》" % h["doc"]
            if h["crumb"]:
                src += " " + h["crumb"]
            seg = "【资料 %d】来源：%s\n%s" % (i, src, body)
            if parts and used + len(seg) > max_chars:
                break
            parts.append(seg)
            used += len(seg)
        parts.append(KB_GUARD)
        return "\n\n".join(parts)
