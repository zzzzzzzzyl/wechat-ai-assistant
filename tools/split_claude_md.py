# -*- coding: utf-8 -*-
"""把 `CLAUDE.md` 里"太长、平时不看"的整节搬到独立文件，主文件只留指针。

    python tools/split_claude_md.py --dry-run   # 只看会怎么切（默认）
    python tools/split_claude_md.py --write     # 真的切

为什么要做（2026-10-11）

    `CLAUDE.md` 涨到 **71KB**，而**工作区说明的预算是 65536 字节** ——
    超出的部分会被**截断**，也就是说文件**尾部的内容模型根本读不到**。
    这不是"文档有点长"，是"文档的一部分对 AI 不可见"，而且**不报错**。
    它已经超了两轮（67185 / 69131 / 71008），每次都是我往里加东西才发现。

⚠️ **绝不手抄**：这里是 20KB 级别的文本，手抄必丢东西。
   脚本按**标记行**精确切，切出来的内容**一个字都不改**地写进目标文件。
⚠️ 搬走之后在 `CLAUDE.md` 原位留一段**摘要 + 指针** ——
   最要紧的几条规则仍然要能一眼看到（不然等于把知识藏起来了）。
⚠️ 目标文件是**追加**的（`记录与存储设计.md` 本来就有内容），
   所以会带一个清晰的祖先标题，并且**幂等**：已经搬过的节再跑一次不会重复写。
"""

import argparse
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "CLAUDE.md")

#: 每项：(主文件里的起始标记行, 目标文件, 目标文件里的标题, 提前停止标记或 None, 原位留的摘要)
#: ⚠️ `stop_before` 用来"这一节只搬到某处为止" —— 比如「打包」那节后面紧跟着
#:    全局的 `## Git` 规则，不能一起搬走（不然主文件里就没有 commit 规范了）。
MOVES = [
    (
        "## 读取后端：UIA 读不到时改走「截屏 + OCR」",
        "OCR后端.md",
        "## OCR 后端（从 CLAUDE.md 搬来的详细规则 · 2026-10-11）",
        None,
        """## 读取后端：UIA 读不到时改走「截屏 + OCR」

> ⚠️ 这一节原来有 **24.5KB**（占全文 38%），是 `CLAUDE.md` 逼近 64KB 上限的主因，
> 2026-10-11 搬到 **`OCR后端.md`**。下面留**"别再踩"的规则**，详细取证和推理看那个文件。

`reader.backend = auto | uia | ocr`，入口是 **`ocr_reader.make_reader()`**。
**别在别处直接 `WeChatReader(...)`** —— 那样降级逻辑会被绕过去。

- **判据是「控件树几乎是空的」**（`reader.uia_health()`：正常 160 / 读不到 2，门槛 20），
  不是「标题为空」—— 用户停在设置页时标题同样是空的，但控件树是满的。
- ⚠️⚠️ **降级判定挂在 `read_chat_title` 上，不能挂在 `read_chat` 上** ——
  `Recorder.tick()` 是「标题为空就整拍跳过」，挂 `read_chat` 上等于没做。
- **切换是单向永久的**（想重试就重启）。`find_window()` 两个后端共用。

### OCR 后端的三个已知损失（别当成 bug 重新排查）

1. **上下文只有当前一屏**（替你滚动微信 = 踩红线）。
2. **分不出「图片消息」和「文本消息」**，也没有 `[图片]` 占位符。
3. **不提供会话列表和输入框内容**（`read_sessions` 返回 `[]`、
   `read_input_text` 返回 `""`）。

### ⚠️⚠️ 四条最容易静默失效的（都实测翻过车）

1. **标题必须和正文用同一个左边界** —— `_title_left()`：`_left_accepted`（正文刚采纳的）
   → 自己检测一次（按位图宽度缓存）→ 配置兜底。
   ⚠️ 窄窗口上配置值（465）比真边界（430）**偏右 35 像素**，够吃掉第一个字；
   ⚠️ 探针模式下 `_left_accepted` 可能**一直是 None**，所以必须能自己检测。
2. **`_bg()` 必须自己转 int32** —— uint8 时 `q * 1024` 在 NumPy 2.x 抛
   `OverflowError`，被 `except` 吞掉后**恒返回 None**（静默降级）。
   ⚠️ 隐藏约定（"调用方记得转 int16"）**没人会记得**，修在函数内部。
3. **`read_chat` 必须自己填 `snap.title`** —— 留空 = 生成建议时静默返回
   「当前没有打开的会话」。⚠️ 这条坑只在 OCR 后端存在。
4. **`sender.resolve()` 对非 UIA 快照必须直接返回**（坐标系不是一回事，
   而且会**覆盖**掉已有的正确判定）。守卫写在 `resolve()` **入口**，不是调用点。

⚠️ 另外：几何不对时**要说出来**（`OcrReader._geom_bad`），
不能和"没打开会话"长成一个样。

### 几何：**检测优先，配置兜底**（第四十六轮把顺序反过来了）

- ⚠️ **「导航栏 + 会话列表」宽度不是常数**：1542 宽窗口分界 465、1206 宽是 **430**。
  所以 `chat_left` 只是**兜底值**。
- 安全性靠两条：① 检测**认不出来就返回 None**；② 认出来的值**还得真能认出气泡**才算数。
- ⚠️ `tol` 别调大（聊天区 28 / 会话列表 45，差 51 —— tol=60 就把两边判成同色）；
  底色**要用众数**，别拿最右几列平均（那儿常是滚动条）。
- 排查时想严格按配置来：`ocr_read.auto_geometry: false`。

### 上下文：默认走「探针」（空闲约 3.4% 一个核）

- ⭐ **探针 = 抓图 + 缩略图哈希，不做 OCR**（约 34ms）；变了才真读一屏（约 380ms）。
- ⚠️⚠️ **探针里绝对不能有像素分析**（第一版 132ms，比 UIA 还贵）。
  ⚠️ **它不报错、单测也全绿，唯一症状是日志里那个毫秒数** —— 所以有 AST 守门测试。
- ⚠️ **"变了没"必须带容差**（`probe_changed`），不能写 `prev != cur`（±2 噪声就误判）。
- ⚠️ **探针只覆盖「标题栏 + 聊天区」**，不含会话列表和输入框
  （免得你自己打字就触发一次真读）。
- ⚠️ **探针第一次只建立基准、不读**；`probe()` 返回 None（抓图失败）**当没变**处理。
- ⚠️ **探针必须走产品代码**（`rd.read_chat()`），别自己再拼一遍几何 ——
  ⚠️ 而且它**必须 `dpi.enable()`**（判据是它打印的 `DPI 缩放` 那一行）。
- ⚠️ **只读标题那一档用 `ocr_read.title_interval`（3s）**，不是 `record.interval`。

**详细内容（切消息行为什么只能按气泡连通域、群聊昵称三条判据、
发送者判定的坐标系问题、时间分隔符、量 OCR 耗时别把引擎加载算进去、
图片消息 OCR 的入口）**：见 `OCR后端.md`。
""",
    ),
    (
        "# 存储",
        "记录与存储设计.md",
        "## 存储（从 CLAUDE.md 搬来的规则与结论 · 2026-10-11）",
        None,
        """# 存储

**两个库，分工不同**：`screens.db`（屏快照）管**上下文的顺序**，
`history.db`（消息表）管**口吻样本 / AI 自我污染标记 / 清空**。

⚠️ 两者都**不是存档**，只是"最近上下文的窗口"（40 屏 / 500 条自动淘汰）。
⚠️ **`history.db` 靠 `Recorder` 连续记录，不要退回"触发时才存"**（会顺序错乱 + 重复存两份）。
⚠️ `session_key` = **会话标题原文** → 重名会话会串（别名映射只做了 emoji 变体合并）。

### ⚠️⚠️ 写库前的三道闸（2026-10-13 补，之前一道都没有）

用户报「翻记录时经常出现乱记录」。实测：库里有 **15** 个 `session_key`，
而 `state.json` 里只登记了 **2** 个 —— 多出来的 `X`/`+`/`①` 是 OCR 碎片，
`简历` / `示例脏标题` 是 **OCR 抓错了画面**（别的窗口、我们自己的面板
被读成了消息）。根因：**两道人墙只挡 `state.json`，不挡这两个库**。

1. **垃圾标题**（`is_junk_title`）→ **连正文都不读、更不写库**（挡 `X`/`+`/`①`）。
2. **只给"确认过的会话"写库**（`registry.is_registered`）→ 挡只出现过一次的脏标题。
   ⚠️ 前提是**登记每拍数**：`discover()` 原来只在 `_on_session` 里调，而那个回调
   **只在标题变了那拍**发 → 一共只数一拍（**除非 10 秒内切走再切回，谁都登记不上**）。
   现在登记与这道闸都在 `Recorder._register_session()`。
   ⚠️ OCR 探针模式下标题**只在"屏幕变了"的拍才读**，所以"未确认"要留记号
   （`_pending_title`）让**下一拍再读一次标题**，否则新会话永远确认不了、头一屏白丢。
3. **切会话竞态**（`_session_stable`：正文读完**再读一次标题**，两次不同 → 这一拍不写）。

⚠️ **判不出"在不在底部"就宁可不写**（`ChatSnapshot.bottom_known`）：
截屏 + OCR 后端的 `at_bottom` **恒为真**（实测 **97/97 屏**），而 `save_new_only`
在 `cut=-1` 时要靠它裁决 → 等于永远"全写"（实测 `简历` 一秒写 **26** 条别的窗口的画面）。
OCR 后端设 `bottom_known=False` → `cut=-1` 一律不写。
⚠️ **只影响 `cut=-1`**：重叠点找得到时照旧"只写新的"。

**完整的去重规则 / 缝合算法 / 时间分隔切段 / 指纹与重叠点**：
见 `记录与存储设计.md` 的「存储」一节。
""",
    ),
    (
        "# 打包（对外分发）",
        "打包与分发.md",
        "## 打包（从 CLAUDE.md 搬来的规则与结论 · 2026-10-11）",
        "## Git",       # ⚠️ Git 规则是全局的，**不搬**（不然主文件里就没有 commit 规范了）
        """# 打包（对外分发）

**一条命令链**：`build_exe.bat` → `python packaging\\make_zip.py` →
`python packaging\\verify_zip.py`。产物 `dist\\微信AI助手.zip`（≈119MB）。

⚠️ **`contents_directory='.'` 不能删**（spec 里），否则 `config.yaml` 不在 exe 旁边，
程序当"配置不存在"跑成默认值**且不报错**。
⚠️ **路径必须全走 `paths.py`**（打包后 `dirname(__file__)` 指向临时解压目录）。
⚠️ **`console=False` 时 `sys.stdout` 是 `None`**，而日志全靠 `print()` ——
`run_watch._attach_frozen_log()` 是**启动前提**，不是锦上添花。
⚠️ **验收标准是 `packaging/verify_zip.py`**（在日志里找只在真跑到那步才打印的词），
**不是"窗口出现了"**。

**完整规则（两个环境假象、清运行数据、体积构成）**：见 `打包与分发.md`。
""",
    ),
]


def find_section(lines, marker, stop_before=None):
    """找到起始行。返回 (start, end)。

    end 取**先到**的那个：
      · 下一个**同级或更高级**的标题（`#` 数量 <= marker 的）
      · `stop_before` 指定的标记行（用来"这一节只搬到某处为止"）
    """
    level = 0
    for ch in marker:
        if ch == "#":
            level += 1
        else:
            break
    start = None
    for i, l in enumerate(lines):
        if l.startswith(marker) and (start is None):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        l = lines[j]
        # ⚠️ `stop_before` 优先判：不然会像第一版那样把后面的 `## Git`
        #    一起搬走 —— 而 Git 规则是**全局**的，不属于"打包"那一节。
        if stop_before and l.startswith(stop_before):
            end = j
            break
        if not l.startswith("#"):
            continue
        n = 0
        for ch in l:
            if ch == "#":
                n += 1
            else:
                break
        # 同级或更高级的标题 -> 这一节到头了
        if n <= level and not l.startswith(marker):
            end = j
            break
    return start, end


def tidy_headings(path):
    """确保每个标题行（第 0 列的 `#`）**前面有一个空行**。

    ⚠️ 为什么需要：脚本是把整节替换成一段摘要的，于是摘要的最后一行
        和下一节的标题会**直接贴在一起**（`...见 xxx.md`。 紧跟着 `# 下一节`）。
        markdown 标题前面没有空行时**渲染不出来**，而且读起来像一句话被截断。

    ⚠️ 只认**第 0 列**的 `#` —— 代码块里的注释都是缩进的（`  # ...`），
        不会被误加空行。
    """
    lines = open(path, encoding="utf-8").read().splitlines()
    out = []
    added = 0
    for l in lines:
        if l.startswith("#") and out and out[-1].strip():
            out.append("")
            added += 1
        out.append(l)
    # 连续 3 个以上空行压成 1 个
    squeezed = []
    for l in out:
        if l.strip() == "" and squeezed and squeezed[-1].strip() == "":
            continue
        squeezed.append(l)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(squeezed) + "\n")
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="真的写（默认只 dry-run）")
    args = ap.parse_args()

    lines = open(MAIN, encoding="utf-8").read().splitlines()
    before = os.path.getsize(MAIN)
    print("CLAUDE.md 现在 %s 字节（预算 65536）%s"
          % (format(before, ","), "  ⚠️ 超了！" if before > 65536 else ""))

    plan = []
    for marker, target, title, stop_before, summary in MOVES:
        got = find_section(lines, marker, stop_before)
        if got is None:
            print("⚠️ 找不到标记: %r" % marker)
            continue
        s, e = got
        body = lines[s:e]
        plan.append((marker, s, e, target, title, summary, body))
        print("\n· %-52s 行 %d~%d（%d 行, %d 字节）"
              % (marker[:52], s + 1, e, len(body),
                 len("\n".join(body).encode("utf-8"))))
        print("    -> %s%s" % (target,
                              "（到 %s 为止，不搬它）" % stop_before
                              if stop_before else ""))

    moved_bytes = sum(len("\n".join(p[6]).encode("utf-8")) for p in plan)
    print("\n合计搬走 %s 字节" % format(moved_bytes, ","))

    if not args.write:
        print("\n（这是 dry-run，加 --write 才动文件）")
        return 0

    # 从后往前替换，避免行号错位
    for marker, s, e, target, title, summary, body in sorted(
            plan, key=lambda p: -p[1]):
        tp = os.path.join(ROOT, target)
        old = open(tp, encoding="utf-8").read() if os.path.exists(tp) else ""
        if title in old:
            print("· %s 里已经有这一节了，跳过（幂等）" % target)
        else:
            sep = "" if old.endswith("\n\n") or not old else "\n\n"
            with open(tp, "w", encoding="utf-8") as f:
                f.write(old + sep + title + "\n\n" + "\n".join(body) + "\n")
            print("· 写进 %s（%s -> %s 字节）"
                  % (target, format(len(old.encode('utf-8')), ","),
                     format(os.path.getsize(tp), ",")))
        lines[s:e] = summary.splitlines()

    with open(MAIN, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    added = tidy_headings(MAIN)
    if added:
        print("\n· 规整排版：给 %d 个标题补了前置空行" % added)
    after = os.path.getsize(MAIN)
    print("\nCLAUDE.md: %s -> %s 字节（省下 %s）"
          % (format(before, ","), format(after, ","),
             format(before - after, ",")))
    print("预算 65536 -> %s" % ("✅ 回到预算内" if after <= 65536
                                else "⚠️ 还是超（还得再搬）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
