# -*- coding: utf-8 -*-
"""本地知识库（`wechat_assistant/kb.py`）：切片 / 二字组 / 导入 / 检索 / 拼装。

    python tests/test_kb.py

起因（2026-10-17）：用户要给 wxai 加"本地知识库"。调研见 `RAG调研.md`，
这里锁的是**调研里量出来的那几条判据**，不是"能跑就行"：

  · 中文用 `unicode61` 直接索引原文 → 2 字查询实测 94% 查不到（所以存两份：text + grams）
  · `trigram` 2 字查询 100% 查不到（所以不用它）
  · 查询用**二字组 OR**，不用整句 phrase（口语问法下 phrase 是 0/16）
  · 检索门槛是**相对首位**的比例（0.6：砍一半噪声、命中不掉）

⚠️ 这个文件刻意**不依赖微信、不依赖 Qt、不联网**（纯函数 + 临时 sqlite），随时能跑。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import kb as kb_mod  # noqa: E402
from wechat_assistant.ai import AIClient  # noqa: E402
from wechat_assistant.prompts import KB_GUARD  # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


#: 一份小"行业资料"：两条问答 + 一张表 + 一段条款
DOC = """# 价目与条款

## 定金与尾款
定金30%，发货前付清尾款。买家自己取消订单，定金不退；我方缺货取消则全额退。
常见问法：钱怎么付、定金能退吗

## 退换货
确认收货后48小时内可无理由退；7天内质量问题包退换。
常见问法：东西不想要了怎么退、多久之内能退

## 型号参数
A2 定制款 耐温 -20℃~80℃，58元/份，有现货。
B1 标准款 耐温 -40℃~120℃，42元/份，预定5-7天。

| 项目 | 价格 | 备注 |
|---|---|---|
| 上门检测 | 50元 | 市区三环内 |
| 更换滤芯 | 120元 | 含滤芯 |
"""


def _tmpdir():
    return tempfile.mkdtemp(prefix="kb_test_")


def _write(dirname, name, text, encoding="utf-8"):
    p = os.path.join(dirname, name)
    with open(p, "w", encoding=encoding) as f:
        f.write(text)
    return p


# ==================================================================== 1. 纯函数


def test_bigrams():
    print("\n[1] 二字组")
    check("1a 重叠且无空格",
          kb_mod.bigrams("我们明天") == "我们 们明 明天",
          "-> %r" % kb_mod.bigrams("我们明天"))
    check("1b 去空白/星号（OCR 常带）",
          kb_mod.bigrams(" 明 天**见 ") == "明天 天见",
          "-> %r" % kb_mod.bigrams(" 明 天**见 "))
    check("1c 单字不炸", kb_mod.bigrams("开") == "开")


def test_sanitize():
    print("\n[2] 问法清洗（两条实测血案）")
    check("2a 引号被去掉（否则 FTS5 抛 unterminated string）",
          '"' not in kb_mod.sanitize_query('他说的"原话"是什么'),
          "-> %r" % kb_mod.sanitize_query('他说的"原话"是什么'))
    check("2b 纯标点清洗成空串（否则抛 syntax error near \"\"）",
          kb_mod.sanitize_query("**") == "" and kb_mod.sanitize_query("）·") == "")
    check("2c 中英数都保留",
          kb_mod.sanitize_query("A2 58元/份") == "A2 58元份",
          "-> %r" % kb_mod.sanitize_query("A2 58元/份"))


def test_build_query():
    print("\n[3] 查询构造：二字组 OR（不是整句 phrase）")
    q = kb_mod.build_query("A2 耐温多少")
    check("3a 是 OR 表达式", " OR " in q, "-> %r" % q)
    check("3b 每一项都是二字组", all(len(x.strip('"')) <= 3 for x in q.split(" OR ")),
          "-> %r" % q)
    check("3c 空输入返回空串（调用方据此跳过检索）",
          kb_mod.build_query("**") == "" and kb_mod.build_query("") == "")
    check("3d 项数有上限（长问法不无限膨胀）",
          kb_mod.build_query("一" * 100).count(" OR ") <= 11)


def test_chunk():
    print("\n[4] 切片")
    chunks = kb_mod.chunk_text(DOC, 400)
    check("4a 切出了多片", len(chunks) >= 3, "-> %d 片" % len(chunks))
    check("4b 面包屑来自标题", any(c == "型号参数" for c, _ in chunks),
          "-> %r" % [c for c, _ in chunks])
    check("4c 表格行没被拆散（同一片里两行都在）",
          any(("上门检测" in t and "更换滤芯" in t) for _, t in chunks))
    check("4d 单片不超上限（表格片除外）",
          all(len(t) <= 400 or "|" in t for _, t in chunks))
    check("4e 空文本切出空列表", kb_mod.chunk_text("   \n\n ") == [])
    # ⚠️⚠️ 2026-10-17 血案：第一版"见到空行就切"，于是 markdown 里
    #     **每个段落、每个列表项各占一片**（实测 2,258 字的笔记被切成 24 片，
    #      平均 94 字）。判据：6 个短段落（空行分隔）应该并成 1~2 片。
    paras = "\n\n".join("第 %d 段只有二十来个字用来测合并" % i for i in range(6))
    n = len(kb_mod.chunk_text(paras, 400))
    check("4f 短段落会被并起来（不是一段一片）", n <= 2, "-> %d 片" % n)


def test_read_encoding():
    print("\n[5] 读文件：只认 UTF-8")
    d = _tmpdir()
    p = _write(d, "gbk.txt", "中文", encoding="gbk")
    try:
        kb_mod.read_text_file(p)
        ok = False
    except kb_mod.KBError:
        ok = True
    check("5a GBK 文件明确报错（不猜编码，猜错就洗不掉）", ok)


# ==================================================================== 2. 库


def _make_kb(d, **kw):
    return kb_mod.KnowledgeBase(os.path.join(d, "kb.db"), **kw)


def test_import(d):
    print("\n[6] 导入")
    p = _write(d, "价目表.txt", DOC)
    kb = _make_kb(d)
    r = kb.add_document(p)
    check("6a 首次导入", r["chunks"] > 0 and not r["skipped"] and not r["replaced"],
          "-> %r" % r)
    check("6b summary 对得上",
          kb.summary()["docs"] == 1 and kb.summary()["chunks"] == r["chunks"],
          "-> %r" % kb.summary())

    r2 = kb.add_document(p)
    check("6c 同内容再导 → 跳过（不重建、不翻倍）",
          r2["skipped"] and kb.summary()["chunks"] == r["chunks"], "-> %r" % r2)

    new_text = DOC + "\n## 新增一节\n补充：节假日不发货。\n"
    _write(d, "价目表.txt", new_text)
    r3 = kb.add_document(p)
    check("6d 内容变了 → 覆盖（有 replaced、片数变了）",
          r3["replaced"] and not r3["skipped"], "-> %r" % r3)
    check("6e 覆盖后没有孤儿片",
          kb.summary()["chunks"] == sum(doc["chunks"] for doc in kb.list_documents()),
          "-> %r" % kb.summary())
    con = sqlite3.connect(kb.path)
    orphan = con.execute("select count(*) from kb_chunks where doc_id not in "
                         "(select id from kb_docs)").fetchone()[0]
    idx_orphan = con.execute("select count(*) from kb_index where rowid not in "
                             "(select id from kb_chunks)").fetchone()[0]
    con.close()
    check("6f 库里没有孤儿（chunk / 索引都对得上）",
          orphan == 0 and idx_orphan == 0, "-> %d / %d" % (orphan, idx_orphan))

    try:
        kb.add_document(_write(d, "空.txt", "   \n\n"))
        ok = False
    except kb_mod.KBError:
        ok = True
    check("6g 空文件报错（不静默导进 0 片）", ok)
    return kb


def test_search(kb):
    print("\n[7] 检索：2 字也能查、口语也能查")
    hits = kb.search("定金")
    check("7a 2 字关键词能查到（trigram 做不到这条）",
          bool(hits) and "定金" in hits[0]["text"], "-> %r" % [h["doc"] for h in hits])
    hits = kb.search("A2 耐温多少")
    check("7b 型号 + 自然语言查询", bool(hits) and "A2" in hits[0]["text"],
          "-> %r" % (hits[0]["text"][:20] if hits else ""))
    hits = kb.search("东西不想要了能退吗")
    check("7c 口语问法（靠「常见问法」里的同义说法或部分字重叠）",
          bool(hits), "-> %r" % [h["text"][:14] for h in hits])
    check("7d 0 结果返回空列表（不是异常）", kb.search("zzz不存在的东西qqq") == [])
    check("7e 纯标点问法返回空列表、不抛",
          kb.search("**") == [] and kb.search("）·") == [])
    check("7f 带引号的问法不抛",
          isinstance(kb.search('他说"定金不退"'), list))
    # ⚠️⚠️ 2026-10-17 用户实测：问「分布式训练为什么要并行」—— 这句正是**小节标题**，
    #    而原来标题**不进索引** → 命中了别的文件（看起来像"没查知识库"）。
    check("7h 拿**小节标题里的词**来问也能查到（标题也进了索引）",
          any("型号参数" in h["crumb"] for h in kb.search("型号参数是什么")),
          "-> %r" % [(h["doc"], h["crumb"]) for h in kb.search("型号参数是什么")])
    # ⚠️⚠️ 2026-10-17 用户实测：聊物理时当前要回的那句是「怎么改变」，
    #    查询只有 3 个二字组，每个命中都只撞 1 个（而且是「怎么」这种虚词）——
    #    【参考资料】被塞进了毕设选题报告和 Transformer 参数量。
    #    判据：这种问法必须**返回空**（宁可不带资料，也不要带错资料）。
    check("7i 只沾一个虚词的问法 → 返回空（不硬凑参考资料）",
          kb.search("怎么改变") == [], "-> %r" % kb.search("怎么改变"))
    check("7j 和资料毫无关系的问法 → 返回空",
          kb.search("合力的大小是不是会随着分力角度的改变而改变") == [])
    check("7g 相对门槛：首位很干净时噪声片被剪掉",
          len(kb.search("A2 耐温 -20℃", top_k=3, min_ratio=0.6)) <= 2,
          "-> %d 片" % len(kb.search("A2 耐温 -20℃", min_ratio=0.6)))


def test_render(kb):
    print("\n[8] 拼装")
    ctx = kb.render("A2 耐温多少")
    check("8a 带出处（文件名）", "《价目表.txt》" in ctx, "-> %r" % ctx[:40])
    check("8b 带防幻觉约束（KB_GUARD）", KB_GUARD in ctx)
    check("8c **不含**【参考资料】标题（那由 ai.build_prompts 加）",
          "【参考资料】" not in ctx)
    check("8d 空结果返回空串（连标题都不出现）", kb.render("zzz不存在") == "")
    ctx_long = kb.render("价目 定金 退换 型号 上门", max_chars=200)
    check("8e 总预算生效", len(ctx_long) <= 200 + len(KB_GUARD) + 40,
          "-> %d 字" % len(ctx_long))


def test_into_prompt(kb):
    print("\n[9] 接进 ai.build_prompts（这是它唯一的去处）")
    ctx = kb.render("A2 耐温多少")
    cfg = {"ai.api_key": "x", "ai.base_url": "https://example.com", "ai.model": "m",
           "ai.temperature": 1.0, "ai.max_tokens": 2000, "ai.n_replies": 3,
           "ai.vision_model": "", "runtime.max_messages": 20,
           "runtime.max_chars": 4000, "runtime.max_msg_chars": 500}
    a = AIClient(cfg)
    _sys, user = a.build_prompts("你帮「我」写微信回复。", [], ">> 客户: A2 耐温多少", 3,
                                 extra_context=ctx)
    check("9a 【参考资料】出现在 user 最前面、且在【对话历史】之前",
          user.startswith("【参考资料】") and
          user.index("【参考资料】") < user.index("【对话历史】"))
    check("9b 资料原文真的进了 prompt", "58元/份" in user)
    _sys2, user2 = a.build_prompts("你帮「我」写微信回复。", [], ">> 客户: 在吗", 3,
                                   extra_context="")
    check("9c 没检索到 → 整个【参考资料】段都不出现",
          "【参考资料】" not in user2)


def test_remove(kb):
    print("\n[10] 移除")
    n = kb.remove_document("价目表.txt")
    check("10a 删掉返回片数", n > 0, "-> %d" % n)
    check("10b 删完查不到", kb.search("定金") == [] and kb.summary()["docs"] == 0)
    check("10c 重复删不抛", kb.remove_document("价目表.txt") == 0)
    con = sqlite3.connect(kb.path)
    left = con.execute("select count(*) from kb_index").fetchone()[0]
    con.close()
    check("10d 索引里也没残留", left == 0, "-> %d" % left)


def test_paths():
    print("\n[11] 位置")
    from wechat_assistant import paths
    check("11a 默认落在 state/kb.db（和两个老库并列）",
          kb_mod.KnowledgeBase.__init__.__defaults__ is not None and
          paths.resolve("state", "kb.db").endswith(os.path.join("state", "kb.db")))


def test_reset_keeps_kb(d):
    """⚠️ 用户问「恢复默认不删库有吗」—— 这条把两件事一起锁住。"""
    print("\n[13] 恢复默认 / 会话默认")
    from wechat_assistant.sessions import KB_DEFAULT, SessionRegistry
    from wechat_assistant.settings import SettingsStore

    kb2 = kb_mod.KnowledgeBase(os.path.join(d, "kb2.db"))
    p = _write(d, "保留测试.txt",
               "# 小节\n确认收货后48小时内可退，这条要能活过恢复默认。\n")
    kb2.add_document(p)
    store = SettingsStore(os.path.join(d, "s.json"), logger=lambda m: None)
    store.set("kb.enabled", True)
    store.set("kb.top_k", 4)
    n_before = kb2.summary()["chunks"]
    store.reset_all()
    check("13a 恢复默认**不删**已导入的资料",
          kb2.summary()["chunks"] == n_before and n_before > 0,
          "-> %d 片" % kb2.summary()["chunks"])
    check("13b 恢复默认**不动**知识库开关（否则看起来像数据丢了）",
          store.raw_override("kb.enabled") is True, repr(store.overrides()))
    check("13c 知识库的**调参**照常被清掉", store.raw_override("kb.top_k") is None,
          repr(store.overrides()))
    check("13d 恢复之后仍然查得到", bool(kb2.search("48小时")),
          "-> %r" % [h["doc"] for h in kb2.search("48小时")])

    # ⚠️ **老配置迁移**（2026-10-17 用户实测）：会话级默认从 False 改成 True 之后，
    #    老 `state.json` 里写的是"旧默认"的 False —— 不迁移的话用户看到的就是
    #    "还是默认关闭"。用 `kb_by_user` 标记区分"用户手动关的"和"旧默认写的"。
    old_state = os.path.join(d, "old_state.json")
    with open(old_state, "w", encoding="utf-8") as f:
        json.dump({"sessions": {
            "老会话甲": {"mode": "on", "kb_enabled": False},
            "老会话乙": {"mode": "on", "kb_enabled": False, "kb_by_user": True},
            "老会话丙": {"mode": "on"},
        }, "sync": {}}, f, ensure_ascii=False)
    reg_old = SessionRegistry(old_state, logger=lambda m: None)
    check("13h 老配置里「旧默认写的 false」会被迁到新默认（开）",
          reg_old.kb_enabled("老会话甲") is True, "-> %r" % reg_old.kb_enabled("老会话甲"))
    check("13i **用户手动关掉的**不会被迁移覆盖",
          reg_old.kb_enabled("老会话乙") is False)
    check("13j 根本没这个字段的按新默认（开）", reg_old.kb_enabled("老会话丙") is True)

    reg = SessionRegistry(os.path.join(d, "state.json"), logger=lambda m: None)
    reg.set_kb_enabled("客户A", True)
    check("13e 会话级知识库**出厂默认打开**（`KB_DEFAULT`）", KB_DEFAULT is True,
          "-> %r" % (KB_DEFAULT,))
    check("13f 没写过的会话按默认算（老 state.json 不会突然全关）",
          reg.kb_enabled("没写过的会话") is True)
    reg.set_kb_enabled("客户B", False)
    check("13g 手动关掉之后保持关（默认值不会把它盖回来）",
          reg.kb_enabled("客户B") is False and reg.kb_enabled("客户A") is True)
    check("13k 手动设置会打上 kb_by_user 标记（以后改默认也不覆盖它）",
          (reg.sessions.get("客户B") or {}).get("kb_by_user") is True,
          "-> %r" % (reg.sessions.get("客户B") or {}))


def test_groups(d):
    """分组：**只影响显示和批量操作，不参与检索**（2026-10-17 用户要求）。"""
    print("\n[14] 分组")
    root = os.path.join(d, "资料树")
    for grp, name in (("AI", "a.md"), ("AI", "b.md"), ("工程", "c.md")):
        os.makedirs(os.path.join(root, grp), exist_ok=True)
        _write(os.path.join(root, grp), name,
               "# 一节\n%s 里的内容，提到 48 小时这个数字。\n" % name)
    _write(root, "零散.txt", "# 一节\n根目录下的文件。\n")

    kb3 = kb_mod.KnowledgeBase(os.path.join(d, "kb3.db"))
    r = kb3.import_tree(root)
    check("14a 整目录导入：子目录名 = 分组名",
          r["ok"] == 4 and r["groups"] == ["AI", "工程"], "-> %r" % r)
    gs = {g["grp"]: g for g in kb3.groups()}
    check("14b 分组统计对得上（默认组永远排最后）",
          gs.get("AI", {}).get("docs") == 2 and gs.get("工程", {}).get("docs") == 1
          and gs.get("", {}).get("label") == "默认"
          and [g["grp"] for g in kb3.groups()] == ["AI", "工程", ""],
          "-> %r" % [(g["grp"], g["docs"]) for g in kb3.groups()])

    r2 = kb3.import_tree(root)
    check("14c 同一棵树再导一次：全部跳过（不翻倍）",
          r2["skipped"] == 4 and r2["ok"] == 0, "-> %r" % r2)

    kb3.set_group("a.md", "工程")
    check("14d 改组只影响归属、不动片数",
          {x["name"]: x["grp"] for x in kb3.list_documents()}["a.md"] == "工程")
    n_chunks = kb3.summary()["chunks"]

    before = kb3.search("48 小时")
    kb3.set_group("a.md", "")
    check("14e **检索不看分组**（改组前后都能查到同一批）",
          len(before) == len(kb3.search("48 小时")) and bool(before),
          "-> %d / %d" % (len(before), len(kb3.search("48 小时"))))

    # 走到这里的状态：AI={b}、工程={c}、默认={a, 零散}（a 是 14d/14e 挪过去的）
    n = kb3.remove_group("工程")
    check("14f 整组删除", n == 1 and kb3.summary()["chunks"] < n_chunks,
          "-> 删了 %d 份，剩 %d 片" % (n, kb3.summary()["chunks"]))
    check("14g 删完不串到别的组",
          {x["name"] for x in kb3.list_documents()} == {"a.md", "b.md", "零散.txt"},
          "-> %r" % [x["name"] for x in kb3.list_documents()])

    # ---- 老库迁移：没有 grp 列的库要能原地升级，**一条数据都不能丢**
    legacy = os.path.join(d, "legacy.db")
    con = sqlite3.connect(legacy)
    con.executescript("""
        CREATE TABLE kb_docs (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE, path TEXT, sha256 TEXT NOT NULL,
            chunks INTEGER DEFAULT 0, chars INTEGER DEFAULT 0,
            added_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE kb_chunks (id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL, ord INTEGER NOT NULL, crumb TEXT DEFAULT '',
            text TEXT NOT NULL, grams TEXT NOT NULL);
    """)
    con.execute("insert into kb_docs(name, path, sha256, chunks, chars, added_at,"
                " updated_at) values ('老资料.md','', 'x', 1, 10, '2026-10-01',"
                " '2026-10-01')")
    con.execute("insert into kb_chunks(doc_id, ord, crumb, text, grams)"
                " values (1, 0, '', '旧数据里的四十八小时说明', ?)",
                (kb_mod.bigrams("旧数据里的四十八小时说明"),))
    con.commit()
    con.close()
    kb4 = kb_mod.KnowledgeBase(legacy)     # 打开就应当自动迁移
    docs = kb4.list_documents()
    check("14h 老库自动迁移：资料还在、落「默认」组",
          len(docs) == 1 and docs[0]["grp"] == "" and docs[0]["name"] == "老资料.md",
          "-> %r" % [(x["name"], x["grp"]) for x in docs])
    con = sqlite3.connect(legacy)
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    check("14i 迁移后版本号写进去了（不会每次打开都 ALTER）",
          ver == kb_mod.SCHEMA_VERSION, "-> %r" % ver)

    # ---- v2 → v3：把**老片的 grams 重算**（标题进索引），不重读原文件
    import shutil
    legacy2 = os.path.join(d, "legacy2.db")
    kb_tmp = kb_mod.KnowledgeBase(legacy2)
    q = os.path.join(d, "标题测试.md")
    _write(d, "标题测试.md", "## 分布式训练为什么要并行\n单卡放不下是常态。\n")
    kb_tmp.add_document(q)
    # 手工退回到 v2 的样子：grams 只有正文、标题没进去
    con = sqlite3.connect(legacy2)
    for cid, text in con.execute("select id, text from kb_chunks").fetchall():
        con.execute("update kb_chunks set grams=? where id=?",
                    (kb_mod.bigrams(text), cid))
    con.execute("insert into kb_index(kb_index) values('rebuild')")
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()
    kb_v3 = kb_mod.KnowledgeBase(legacy2)      # 打开 → 自动迁移到 3
    hits = kb_v3.search("分布式训练为什么要并行")
    check("14j v2 老库打开后自动把标题补进索引（不重读原文件）",
          bool(hits) and hits[0]["crumb"] == "分布式训练为什么要并行",
          "-> %r" % [(h["doc"], h["crumb"]) for h in hits])


def test_group_entities(d):
    """「先建组、再往组里加文件」（2026-10-17 用户要求）。

    ⚠️ 关键：**空组要能存在** —— 否则刚建好、还没加文件的组会直接消失，
        用户会说"我明明建了"。所以组是实体（`kb_groups` 表），不只是文件上的一列。
    """
    print("\n[15] 分组的实体")
    kb5 = kb_mod.KnowledgeBase(os.path.join(d, "kb5.db"))
    kb5.create_group("AI")
    kb5.create_group("工程")
    check("15a 空组也显示（0 份），默认组永远在最后",
          [(g["label"], g["docs"]) for g in kb5.groups()]
          == [("AI", 0), ("工程", 0), ("默认", 0)],
          "-> %r" % [(g["label"], g["docs"]) for g in kb5.groups()])

    try:
        kb5.create_group("AI")
        ok = False
    except kb_mod.KBError:
        ok = True
    check("15b 重名要报错（否则界面上两个同名组分不清）", ok)
    try:
        kb5.create_group("默认")
        ok = False
    except kb_mod.KBError:
        ok = True
    check("15c 「默认」是保留名", ok)

    p = _write(d, "定向.md", "# 一节\n确认收货后四十八小时内可退。\n")
    kb5.add_document(p, grp="AI")
    check("15d 往**指定组**加文件（不用事后挪）",
          {x["name"]: x["grp"] for x in kb5.list_documents()}["定向.md"] == "AI")
    check("15e 加完这个组就有统计了",
          [(g["label"], g["docs"]) for g in kb5.groups()][0] == ("AI", 1))

    n = kb5.rename_group("AI", "人工智能")
    check("15f 改组名会把组里的资料一起带走",
          n == 1 and {x["name"]: x["grp"] for x in kb5.list_documents()}["定向.md"]
          == "人工智能", "-> 改了 %d 份" % n)
    check("15g 改名后旧组名不再出现",
          [g["label"] for g in kb5.groups()] == ["人工智能", "工程", "默认"],
          "-> %r" % [g["label"] for g in kb5.groups()])

    n = kb5.delete_group("人工智能", with_docs=False)
    check("15h 删组但**不删资料**：资料落回「默认」组（不丢）",
          n == 0 and {x["name"]: x["grp"] for x in kb5.list_documents()}["定向.md"] == "",
          "-> %r" % [(x["name"], x["grp"]) for x in kb5.list_documents()])
    check("15i 组里没资料之后，那个组名才消失",
          "人工智能" not in [g["label"] for g in kb5.groups()],
          "-> %r" % [g["label"] for g in kb5.groups()])

    # ---- 「添加文件夹」= **整个文件夹变成一个组**（组名 = 文件夹名）
    #
    # ⚠️ 2026-10-17 用户改的口径：不再按子目录拆组。选 D:\资料\AI 就得到组「AI」，
    #    里面连同子目录里的文件都归它。
    box = os.path.join(d, "整包文件夹")
    os.makedirs(os.path.join(box, "子目录"), exist_ok=True)
    _write(box, "一.md", "# 一节\n整包里的第一个文件，提到四十八小时。\n")
    _write(os.path.join(box, "子目录"), "二.md", "# 一节\n子目录里的文件。\n")
    r = kb5.import_tree(box, grp=os.path.basename(box))
    check("15j 整个文件夹 = 一个组，子目录不另起组",
          r["ok"] == 2 and r["groups"] == ["整包文件夹"], "-> %r" % r)
    check("15k 子目录里的那份也归这一组",
          {x["name"]: x["grp"] for x in kb5.list_documents()}.get("二.md")
          == "整包文件夹",
          "-> %r" % [(x["name"], x["grp"]) for x in kb5.list_documents()])

    empty = os.path.join(d, "空文件夹")
    os.makedirs(empty, exist_ok=True)
    r = kb5.import_tree(empty, grp="空文件夹")
    check("15l 空文件夹也会建出那个组（0 份也看得见）",
          r["ok"] == 0 and any(g["label"] == "空文件夹" and g["docs"] == 0
                               for g in kb5.groups()),
          "-> %r" % [(g["label"], g["docs"]) for g in kb5.groups()])

    r = kb5.import_tree(box, grp=kb_mod.NO_GROUP_LABEL)
    check("15m 文件夹名正好是保留名（「默认」）→ 落进默认组，不报错",
          not r["errors"] and {x["name"]: x["grp"]
                               for x in kb5.list_documents()}["一.md"] == "",
          "-> %r" % (r,))


def test_formats(d):
    """多格式：PDF（有文字层）/ Word(.docx) / 老 .doc 明确报错 / 扫描件不偷偷 OCR。"""
    print("\n[16] 多格式导入")
    # ---- PDF：造一个带中文文字层的
    pdf = os.path.join(d, "手册.pdf")
    try:
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        font = r"C:\Windows\Fonts\msyh.ttc"
        page.insert_font(fontname="msyh", fontfile=font)
        page.insert_text((60, 60), "售后条款", fontname="msyh", fontsize=12)
        page.insert_text((60, 90), "确认收货后四十八小时内可退。", fontname="msyh", fontsize=11)
        doc.save(pdf)
        doc.close()
        has_pdf = True
    except Exception as e:
        has_pdf = False
        print("      （跳过 PDF 那几条：%s: %s）" % (type(e).__name__, e))

    if has_pdf:
        text, note = kb_mod.read_document(pdf)
        check("16a PDF 抽出文字（走文字层，**没有 OCR**）",
              "四十八小时" in text and "## 第 1 页" in text, "-> %r" % text[:40])
        check("16b 页号成了面包屑（出处能看出是第几页）", not note)
        kb6 = kb_mod.KnowledgeBase(os.path.join(d, "kb_pdf.db"))
        r = kb6.add_document(pdf)
        crumbs = [x["doc"] for x in kb6.search("四十八小时")]
        check("16c PDF 导入后能检索到", r["chunks"] > 0 and crumbs == ["手册.pdf"],
              "-> %r" % crumbs)

        # 扫描件：空白页（没有文字层）→ 默认**不 OCR**，要明确报错
        blank = os.path.join(d, "扫描件.pdf")
        doc = fitz.open()
        doc.new_page()
        doc.save(blank)
        doc.close()
        try:
            kb_mod.read_document(blank)
            ok = False
        except kb_mod.KBError as e:
            ok = "扫描件" in str(e) or "OCR" in str(e)
        check("16d 没有文字层的 PDF 明确报错（默认不偷偷 OCR）", ok)

        try:
            kb_mod.read_document(pdf, ocr_scanned=False)
            ok = True
        except Exception:
            ok = False
        check("16e 显式关掉 OCR 时也能正常读有文字层的 PDF", ok)

    # ---- Word(.docx)：段落 + 表格
    docx_path = os.path.join(d, "价目.docx")
    try:
        import docx
        dd = docx.Document()
        dd.add_heading("维修价目", level=1)
        dd.add_paragraph("以下是上门维修的基础费用。")
        t = dd.add_table(rows=2, cols=2)
        t.cell(0, 0).text = "项目"
        t.cell(0, 1).text = "价格"
        t.cell(1, 0).text = "上门检测"
        t.cell(1, 1).text = "五十元"
        dd.save(docx_path)
        has_docx = True
    except Exception as e:
        has_docx = False
        print("      （跳过 Word 那几条：%s: %s）" % (type(e).__name__, e))

    if has_docx:
        text, note = kb_mod.read_docx(docx_path)
        check("16f Word 段落抽出来了", "上门维修的基础费用" in text)
        check("16g 标题变成了 markdown 标题（面包屑要用）", "## 维修价目" in text,
              "-> %r" % text[:30])
        check("16h 表格抽出来了（表格不在 paragraphs 里，要单独遍历）",
              "上门检测" in text and "五十元" in text)
        check("16i 有表格时会提示（表格排在正文后面）", "表格" in note, "-> %r" % note)

    # ---- 老 .doc：明确告诉用户怎么办
    bad = os.path.join(d, "老格式.doc")
    with open(bad, "wb") as f:
        f.write(b"\xd0\xcf\x11\xe0")          # 随便几个字节，反正读不了
    try:
        kb_mod.read_document(bad)
        ok = False
    except kb_mod.KBError as e:
        ok = ".docx" in str(e) or "PDF" in str(e)
    check("16j 老 .doc 报错并说明怎么转（不让人对着\"导入失败\"猜）", ok)

    try:
        kb_mod.read_document(os.path.join(d, "什么.xyz"))
        ok = False
    except kb_mod.KBError as e:
        ok = "不支持的格式" in str(e)
    check("16k 不认识的格式也明确报错", ok)

    # ---- **添加文件夹也要读里面的其他格式**（2026-10-17 用户要求）
    #
    # ⚠️ 原来 `import_tree` 只收 `.md` / `.txt`，文件夹里的 PDF/Word 被**静默跳过** ——
    #    用户以为整个文件夹都导进来了。
    if has_pdf and has_docx:
        box = os.path.join(d, "混合格式")
        os.makedirs(os.path.join(box, "子目录"), exist_ok=True)
        _write(box, "笔记.md", "# 一节\n四十八小时。\n")
        _write(os.path.join(box, "子目录"), "说明.txt", "# 二节\n另一个文件。\n")
        shutil.copy(pdf, os.path.join(box, "手册.pdf"))
        # ⚠️ 这里要**新造**一份 docx（前面那份里没有"四十八小时"，
        #    直接 copy 过来下面那条检索断言会假失败）
        import docx as _docx
        _d = _docx.Document()
        _d.add_paragraph("Word 里也写了四十八小时的条款。")
        _d.save(os.path.join(box, "条款.docx"))
        with open(os.path.join(box, "演示.pptx"), "wb") as f:
            f.write(b"PK\x03\x04fake")
        kb7 = kb_mod.KnowledgeBase(os.path.join(d, "kb_mix.db"))
        r = kb7.import_tree(box, grp="混合格式")
        names = sorted(x["name"] for x in kb7.list_documents())
        check("16l 文件夹里的 md/txt/pdf/docx **全都要读**",
              r["ok"] == 4 and names == ["手册.pdf", "条款.docx", "笔记.md", "说明.txt"],
              "-> %r / %r" % (r["ok"], names))
        check("16m 不支持的格式**数出来**（不静默跳过）",
              r["ignored"].get(".pptx") == 1, "-> %r" % (r["ignored"],))
        check("16n 三种格式都能被同一句检索到",
              {h["doc"] for h in kb7.search("四十八小时", top_k=5)}
              == {"笔记.md", "手册.pdf", "条款.docx"},
              "-> %r" % [(h["doc"], h["crumb"]) for h in kb7.search("四十八小时", top_k=5)])


def test_query_from_screen():
    print("\n[12] 从屏幕拼查询（run_watch._kb_query）")
    from run_watch import _kb_query

    cur = "客户: 你们A2多少钱\n>> 客户: A2 那个能放冷冻吗，耐温多少？\n"
    q = _kb_query(cur)
    check("12a 说话人前缀被剥掉（`客户:` 会和资料里的「客户」撞）",
          "客户" not in q and "能放冷冻吗" in q, "-> %r" % q)
    check("12b 「我想说」排在前面（它最准）",
          _kb_query(cur, "耐温").startswith("耐温"), "-> %r" % _kb_query(cur, "耐温"))
    check("12c 空屏返回空串（调用方据此跳过检索）", _kb_query("") == "")
    check("12d 只有锚点也能用", _kb_query(">> 在吗") == "在吗")
    check("12e 全长有上限（不会把整屏塞进查询）", len(_kb_query("问" * 500)) <= 200)

    # ⚠️⚠️ 2026-10-17 用户实测：一屏里混着几个话题
    #   （"1加一等于多少" / "分布式训练为什么要并行" / "怎么并行" / ">> 你的毕业论文选什么"），
    #   把锚点和两条背景拼成一个查询 → **覆盖率被稀释，锚点明明有资料却查不到**。
    #   所以默认只查锚点，空了才补背景（两段式）。
    mixed = ("对方: 1加一等于多少\n对方: 分布式训练为什么要并行\n"
             "对方: 怎么并行\n>> 对方: 你的毕业论文选什么\n")
    q_anchor = _kb_query(mixed)
    check("12f 默认**只查锚点**（不掺背景，免得把话题带偏）",
          q_anchor == "你的毕业论文选什么", "-> %r" % q_anchor)
    q_full = _kb_query(mixed, with_context=True)
    check("12g 补背景时才加上最近两条（第二段用）",
          "分布式训练为什么要并行" in q_full and q_full.startswith("你的毕业论文选什么"),
          "-> %r" % q_full)


def main():
    print("=" * 72)
    print("本地知识库 kb.py")
    print("=" * 72)
    test_bigrams()
    test_sanitize()
    test_build_query()
    test_chunk()
    test_read_encoding()
    d = _tmpdir()
    kb = test_import(d)
    test_search(kb)
    test_render(kb)
    test_into_prompt(kb)
    test_remove(kb)
    test_paths()
    test_reset_keeps_kb(d)
    test_groups(d)
    test_group_entities(d)
    test_formats(d)
    test_query_from_screen()
    print("\n" + "=" * 72)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
