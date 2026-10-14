# -*- coding: utf-8 -*-
"""会话注册表测试：默认模式、白名单、黑名单、显式设置。

为什么单独测这个
----------------
「哪些会话会被处理」直接决定了**隐私边界**：不在名单里的会话必须
**连消息正文都不读**。而且这块有个隐蔽的坑 —— 会话模式是**持久化**的，
改了 config 里的 `default_mode` 对**已经发现过**的会话不生效，
因为它们早就写死在 state.json 里了（实测 11 个会话有 8 个卡在 off）。
这个「自动拉取」行为很容易在后续重构里被弄丢，所以用测试钉住。

用法：python tests/test_sessions.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.sessions import (MODE_OBSERVE, MODE_OFF, MODE_ON,  # noqa: E402
                                       SessionRegistry)

PASS, FAIL = 0, 0



def confirm(r, title):
    """走完「两次确认」：第一次只记数、第二次才登记。返回**第二次**的返回值。

    ⚠️ 测试里原来都是 `r.discover("张三")` 一次调用 —— 那模拟的是**一拍**，
        而 `discover` 现在要两拍（防 OCR 一次性乱码，见 `sessions.SESSION_CONFIRM_N`）。
    ⚠️ **别改成把 `SESSION_CONFIRM_N` 调成 1** —— 那等于把被测的那道人墙关掉，
        测试就再也验不到它了。
    """
    r.discover(title)                      # 第一拍：只记数
    return r.discover(title)               # 第二拍：真正登记

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def make(path, **kw):
    return SessionRegistry(state_path=path, logger=lambda m: None, **kw)


# ============================================================ 1. 白名单模式

def test_whitelist_mode(tmp):
    print("\n1. default_mode=off（白名单模式）")
    p = os.path.join(tmp, "w.json")
    r = make(p, default_mode=MODE_OFF, bootstrap_whitelist=["张三"])
    confirm(r, "张三")
    confirm(r, "李四")
    check("白名单里的被启用", r.is_enabled("张三"))
    check("名单外的保持关闭", not r.is_enabled("李四"), r.mode_of("李四"))
    check("enabled_by 记为 config", r.sessions["张三"].get("enabled_by") == "config")


# ============================================================ 2. 全开模式

def test_allow_all(tmp):
    print("\n2. default_mode=on（所有会话都可以）")
    p = os.path.join(tmp, "a.json")
    r = make(p, default_mode=MODE_ON)
    for name in ("张三", "李四", "某个群"):
        confirm(r, name)
    check("新会话默认就是启用", all(r.is_enabled(n) for n in ("张三", "李四", "某个群")),
          str([r.mode_of(n) for n in ("张三", "李四", "某个群")]))


# ============================================================ 3. 自动拉取

def test_pull_existing(tmp):
    print("\n3. 改成 on 之后，能拉动「已发现但从未配置」的旧会话")
    p = os.path.join(tmp, "p.json")

    # 先按旧的白名单模式跑一轮，留下几个 off 的会话
    r1 = make(p, default_mode=MODE_OFF, bootstrap_whitelist=["张三"])
    confirm(r1, "张三")
    confirm(r1, "群友丙")
    confirm(r1, "24信息学院新生群")
    r1.save()
    check("此时新会话是 off", not r1.is_enabled("群友丙"))

    # 用户改了配置：default_mode 变成 on
    r2 = make(p, default_mode=MODE_ON)
    check("旧会话被拉起来", r2.is_enabled("群友丙") and r2.is_enabled("24信息学院新生群"),
          str([r2.mode_of("群友丙"), r2.mode_of("24信息学院新生群")]))
    check("原本就启用的仍然启用", r2.is_enabled("张三"))


# ============================================================ 4. 黑名单

def test_disabled(tmp):
    print("\n4. 黑名单：强制不处理（优先级最高）")
    p = os.path.join(tmp, "d.json")
    r = make(p, default_mode=MODE_ON, disabled_sessions=["某某银行", "XX 工作群"])
    confirm(r, "随便一个会话")
    confirm(r, "某某银行")
    confirm(r, "XX 工作群")
    check("普通会话启用", r.is_enabled("随便一个会话"))
    check("黑名单里的被排除", not r.is_enabled("某某银行"), r.mode_of("某某银行"))
    check("黑名单标记来源",
          r.sessions["某某银行"].get("enabled_by") == "config-disabled")

    # 黑名单优先级高于白名单
    p2 = os.path.join(tmp, "d2.json")
    r2 = make(p2, default_mode=MODE_OFF,
              bootstrap_whitelist=["甲方"], disabled_sessions=["甲方"])
    confirm(r2, "甲方")
    check("同时出现在两个名单里 -> 以黑名单为准", not r2.is_enabled("甲方"),
          r2.mode_of("甲方"))


# ============================================================ 5. 显式设置

def test_explicit_setting(tmp):
    print("\n5. 用户显式设置过的，不该被 default_mode 覆盖")
    p = os.path.join(tmp, "e.json")
    r1 = make(p, default_mode=MODE_ON)
    confirm(r1, "不想被记录的会话")
    r1.set_mode("不想被记录的会话", MODE_OFF)      # 用户手动关掉
    r1.save()

    r2 = make(p, default_mode=MODE_ON)
    check("显式关掉的仍然关着", not r2.is_enabled("不想被记录的会话"),
          "mode=%s enabled_by=%s" % (r2.mode_of("不想被记录的会话"),
                                     r2.sessions["不想被记录的会话"].get("enabled_by")))
    check("标记来源为用户", r2.sessions["不想被记录的会话"].get("enabled_by") == "user")


# ============================================================ 6. observe 模式

def test_observe(tmp):
    print("\n6. observe：只记录不生成")
    p = os.path.join(tmp, "o.json")
    r = make(p, default_mode=MODE_OFF)
    confirm(r, "旁观会话")
    r.set_mode("旁观会话", MODE_OBSERVE)
    check("observe 不是「启用（要生成）」", not r.is_enabled("旁观会话"))
    check("但它的 mode 确实是 observe", r.mode_of("旁观会话") == MODE_OBSERVE)
    # recorder 用的是「mode in (on, observe)」这个判据，不是 is_enabled
    from wechat_assistant.recorder import RECORD_MODES
    check("recorder 会记录 observe 的会话", MODE_OBSERVE in RECORD_MODES,
          str(RECORD_MODES))
    check("recorder 不会记录 off 的会话", MODE_OFF not in RECORD_MODES)


# ============================================================ 7. 持久化往返

def test_persistence(tmp):
    print("\n7. 持久化往返")
    p = os.path.join(tmp, "s.json")
    r1 = make(p, default_mode=MODE_ON)
    confirm(r1, "甲")
    confirm(r1, "乙")
    r1.set_mode("乙", MODE_OFF)
    r1.set_tail("甲", [(1, None, "你好")])
    r1.save()

    r2 = make(p, default_mode=MODE_ON)
    check("会话表存回来了", r2.is_enabled("甲") and not r2.is_enabled("乙"),
          str([r2.mode_of("甲"), r2.mode_of("乙")]))
    # tuple 存进 JSON 再读回会变 list，get_tail 必须规范化成 tuple
    check("游标读回来是 tuple 不是 list",
          r2.get_tail("甲") == [(1, None, "你好")],
          str(r2.get_tail("甲")))

    # 原始文件里确实存的是 JSON 数组（这是无法避免的）
    with open(p, "r", encoding="utf-8") as f:
        raw = json.load(f)
    check("文件里 sessions/sync 都在",
          "sessions" in raw and "sync" in raw, str(sorted(raw.keys())))


# ============================================================ 8. YAML 布尔坑

def test_touch(tmp):
    """`touch()` —— 记下「刚刚看到这个会话」的时刻（设置面板按时间排要用）。

    ⚠️ 时间故意用**字符串** `"YYYY-MM-DD HH:MM:SS"`，跟 `screens.seen_at` 同一个格式：
        这样两个来源可以直接按**字典序**比大小，不用解析（见 `session_ui._seen_key`）。
    """
    print("\n7b. 记「刚刚打开过」的时刻")
    p = os.path.join(tmp, "t.json")
    r = make(p, default_mode=MODE_ON)
    confirm(r, "甲")
    r.touch("甲")
    t1 = r.sessions["甲"].get("last_seen_at")
    check("写进去了", bool(t1), repr(t1))
    check("格式是定宽 `YYYY-MM-DD HH:MM:SS`",
          isinstance(t1, str) and len(t1) == 19 and t1[10] == " ",
          repr(t1))
    # ⚠️ 必须是**能直接比大小**的格式：解析成时间戳再比是多余的
    check("同一秒内两次 touch 不会倒退", r.sessions["甲"]["last_seen_at"] >= t1,
          repr(r.sessions["甲"]["last_seen_at"]))
    # ⚠️ 没登记过的会话不能凭空造一条出来（否则设置列表会冒出幽灵条目）
    r.touch("没见过的会话")
    check("没登记过的会话不会被创建", "没见过的会话" not in r.sessions,
          repr(sorted(r.sessions)))
    # ⚠️ touch **不落盘**（记录器每切一次会话就落一次盘太浪费），
    #    但 `save()` 之后必须存得住
    _raw = {}
    if os.path.exists(p):
        _raw = (json.load(open(p, encoding="utf-8")) or {}).get("sessions", {})
    check("touch 不落盘（文件里还没有）", "last_seen_at" not in _raw.get("甲", {}),
          repr(_raw.get("甲")))
    r.save()
    r2 = make(p, default_mode=MODE_ON)
    check("save() 之后存得住", r2.sessions["甲"].get("last_seen_at") == t1,
          repr(r2.sessions["甲"].get("last_seen_at")))


def test_run_watch_discovers_sessions(tmp):
    """⚠️ 防复发：**product 的运行期必须登记新会话**（2026-10-11 修的真 bug）。

    背景：`discover()` 才是「登记一个新会话」，而 `touch()` 只更新
    `last_seen_at` —— 会话**不在表里**时它直接 return（上面那条测试锁着这点）。

    ⚠️⚠️ **2026-10-13 挪了位置**：原来 `_on_session` 里调 `discover()`，但那个
        回调**只在标题变了的那一拍**才发 —— 而"两次确认"要求
        "同一个标题在**不同的拍**里看到两次"（10 秒内连续）。也就是说
        一共只数过一拍：**除非你在 10 秒内切走再切回来，任何会话都登记不上。**

        实测后果（查库）：`state.json` 里只有 **2** 个会话，而
        `screens.db` / `history.db` 里有 **15** 个（多出来的 `X` / `+` / `①` /
        `简历` 根本不是会话）。用户早就报过「设置里的会话列表长不出来」。

    所以现在：
        · `_on_session`（切换会话那一刻）只 `touch()`；
        · **登记挪进 `Recorder._register_session()`，每一拍都数一次**；
        · 而且**没登记的会话不写库**（`is_registered` 当闸门）——
          否则一次性的乱码标题照样在库里留下记录。

    这一节**直接查源码**：这几件事一个都不能少，也不能挪回去。
    """
    print("\n7c. 运行期必须登记新会话（每拍数一次）")
    import ast

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "run_watch.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_on_session":
            fn = node
            break
    check("run_watch 里有 _on_session", fn is not None, "")

    # 只看**代码**，跳过 docstring/注释 —— 不然随便一句"要记得 discover"
    # 的文字就能让测试误判为通过
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)) else fn.body
    code = ast.unparse(ast.Module(body=body, type_ignores=[]))
    check("_on_session 里调了 registry.touch(（更新时间）",
          "registry.touch(" in code, code[:120])
    check("⚠️ 登记**不再**放在 _on_session 里（它只在标题变化那拍发，"
          "两次确认永远攒不满）",
          "registry.discover(" not in code, code[:200])

    # 挪到记录器里之后，静态守门也得跟着挪 —— 否则"没人登记"这个 bug 会静默复发
    rec_src = open(os.path.join(root, "wechat_assistant", "recorder.py"),
                   encoding="utf-8").read()
    check("登记挪进 recorder（每拍数一次）", "reg.discover(" in rec_src,
          "recorder.py 里找不到 reg.discover(")
    check("并且写库前有闸：没登记就不写", "reg.is_registered(" in rec_src,
          "recorder.py 里找不到 reg.is_registered(")

    # 行为那条也验一遍：登记之后它就是"有了"，而且 mode 跟随 default_mode
    p = os.path.join(tmp, "t2.json")
    r = make(p, default_mode=MODE_ON)
    check("新登记时返回 True（表示首次发现）", confirm(r, "张三") is True, "")
    check("再登记一次返回 False", confirm(r, "张三") is False, "")
    check("登记后 mode 跟随 default_mode", r.mode_of("张三") == MODE_ON,
          r.mode_of("张三"))
    check("登记后才排得上用场（touch 能写进去了）",
          (r.touch("张三"), r.sessions["张三"].get("last_seen_at"))[1] is not None,
          "")


def test_system_sessions(tmp):
    """⭐ 系统会话（公众号 / 微信支付 / 服务通知 …）必须**默认就不被处理**。

    用户 2026-10-11 的要求：「公众号、微信支付等不需要」。

    为什么需要专门挡：这些东西**不是聊天**，但微信里长得一模一样
    （有标题栏、有内容区、下面还有输入框），所以像素这条路照样会把
    它们读成会话、把内容读成消息。只能按**名字**挡。

    ⚠️ 用**子串**而不是全等：公众号有成千上万个（人民日报 / 新华社 / …），
       全等没法穷举。
    ⚠️ 三道防线都要在（只做一道会漏）：
       ① `discover()` 新建时就建成 off —— 否则 `default_mode: on` 会直接启用它
       ② `mode_of()` 每次都判 —— 否则用户在面板里手动设成「开」就绕过去了
       ③ `_apply_bootstrap()` 在**已有的表**上再刷一遍 —— 否则加这个配置之前
          就已经登记过的那些（当时是 on）不会生效
    """
    print("\n7d. 系统会话（公众号 / 微信支付 …）默认不处理")
    patterns = ["公众号", "微信支付", "服务通知"]
    p = os.path.join(tmp, "t3.json")
    r = make(p, default_mode=MODE_ON, disabled_contains=patterns)

    check("普通会话照旧跟随 default_mode",
          (confirm(r, "张三"), r.mode_of("张三"))[1] == MODE_ON, "")
    for name in patterns:
        confirm(r, name)
        check("「%s」登记后就是 off" % name, r.mode_of(name) == MODE_OFF,
              r.mode_of(name))
    check("名字里**含**关键词的也算（子串匹配）",
          (confirm(r, "XX公众号运营交流群"),
           r.mode_of("XX公众号运营交流群"))[1] == MODE_OFF, "")

    # 防线②：手动设成「开」也不生效（和黑名单一样的"安全阀"语义）
    r.set_mode("公众号", MODE_ON)
    check("手动设成「开」也仍然不生效（防线②）", r.mode_of("公众号") == MODE_OFF,
          r.mode_of("公众号"))
    check("普通会话手动设「开」照常生效",
          (r.set_mode("张三", MODE_ON), r.mode_of("张三"))[1] == MODE_ON, "")

    # 防线③：老 state.json 里已经以 on 登记过的，加了配置之后要被刷成 off
    p2 = os.path.join(tmp, "t4.json")
    old = make(p2, default_mode=MODE_ON)       # 不带关键词建库
    confirm(old, "微信支付")
    check("（前提）不带关键词时它以 on 建进去",
          old.mode_of("微信支付") == MODE_ON, old.mode_of("微信支付"))
    old.save()
    new = make(p2, default_mode=MODE_ON, disabled_contains=patterns)  # 带上关键词重开
    check("重开后**已有的**系统会话被刷成 off（防线③）",
          new.mode_of("微信支付") == MODE_OFF, new.mode_of("微信支付"))
    check("而且落盘了（enabled_by 标成 system-pattern）",
          (new.sessions["微信支付"].get("enabled_by") == "system-pattern"),
          repr(new.sessions["微信支付"].get("enabled_by")))


def test_merge_variants(tmp):
    """⚠️⚠️ 只差 emoji/符号的会话名必须当成**同一个会话**（2026-10-11）。

    用户问：「换回 UIA 模式，用户名原来拿不到颜文字、使用后可以拿到，
    会不会导致重复记录？」—— **会**，实测过：

        真值 '♥️示例群名🏠'
          UIA -> '♥️示例群名🏠'   （读控件的 Name，原样）
          OCR -> '口口示例群名口'   （认字形，emoji 认不出）

    而 `session_key` 就是标题原文 → 两个 key → **同一个会话的历史被劈成两半**，
    关系设定 / 人设 / 知识库 / 黑名单各认各的。
    实测 6 个真实会话名里 **3 个**两个后端不一致（`probes/probe_title_variants.py`）。

    ⚠️ 边界必须守住：**全 emoji 的名字归一化后是空串，绝不能互相合并** ——
       否则「😝😜🤪😛😋」和「🎉🎊」会挤成一条。
    """
    print("\n7e. 会话名变体合并（emoji / 符号差异）")
    from wechat_assistant.sessions import norm_title

    check("emoji 被去掉",
          norm_title("♥️示例群名🏠") == "示例群名",
          norm_title("♥️示例群名🏠"))
    check("文字和数字都保留", norm_title("🈲示例23班乙群🈲") == "示例23班乙群",
          norm_title("🈲示例23班乙群🈲"))
    check("纯文字原样（只 casefold）", norm_title("张三") == "张三", "")
    check("全 emoji -> 空串（调用方要据此**不合并**）", norm_title("😝😜🤪😛😋") == "",
          repr(norm_title("😝😜🤪😛😋")))
    check("没改普通标点（'项目（A）' 不该被归一化掉）",
          norm_title("项目（A）") == "项目（a）", norm_title("项目（A）"))

    a = "♥️示例群名🏠"
    b = "示例群名"

    p0 = os.path.join(tmp, "m0.json")
    off = make(p0, default_mode=MODE_ON, merge_variants=False)
    confirm(off, a)
    confirm(off, b)
    check("关掉合并会各建一条（复现原来的 bug）", len(off.sessions) == 2,
          "%d 条" % len(off.sessions))

    p1 = os.path.join(tmp, "m1.json")
    on = make(p1, default_mode=MODE_ON, merge_variants=True)
    check("第一个变体登记成功", confirm(on, a) is True, "")
    check("第二个变体**不再新建**", confirm(on, b) is False, "")
    check("会话表仍然只有 1 条", len(on.sessions) == 1, "%d 条" % len(on.sessions))
    check("key_for 把变体认到同一条", on.key_for(b) == a, on.key_for(b))
    check("两个写法都记进 raw_names",
          set(on.sessions[a].get("raw_names") or []) == {a, b},
          repr(on.sessions[a].get("raw_names")))
    check("mode_of 走变体也拿到同一条的模式", on.mode_of(b) == MODE_ON, on.mode_of(b))

    on.save()
    on2 = make(p1, default_mode=MODE_ON, merge_variants=True)
    check("重启后仍然认得变体（索引在建表之后重建）",
          on2.key_for(b) == a, on2.key_for(b))

    p2 = os.path.join(tmp, "m2.json")
    emo = make(p2, default_mode=MODE_ON, merge_variants=True)
    confirm(emo, "😝😜🤪😛😋")
    confirm(emo, "🎉🎊")
    # ⚠️ 2026-10-11 改：全 emoji 的标题现在是**垃圾标题**，**根本不登记**
    #    （`is_junk_title`：归一化后为空）。这比"不误合并"更彻底 ——
    #    OCR 读纯 emoji 会得到 '00000'，以前它会变成一条叫 "00000" 的会话。
    check("全 emoji 的名字**不登记**（连会话都不建）", len(emo.sessions) == 0,
          "%d 条：%s" % (len(emo.sessions), list(emo.sessions)))

    p3 = os.path.join(tmp, "m3.json")
    ex = make(p3, default_mode=MODE_ON, merge_variants=True)
    confirm(ex, "项目A")
    check("归一化名相同的另一个名字会复用（大小写差异）",
          ex.key_for("项目a") == "项目A", ex.key_for("项目a"))


def test_merge_known_sessions(tmp):
    """⚠️⚠️ 设置里的会话**名单**必须是「注册表 ∪ 屏快照库」。

    用户报（2026-10-11）：「重新启动程序后设置中的会话历史会不见」。

    根因：列表以前**只读注册表**，而注册表只在**正常退出时**才落盘。
    实测证据：`state.json` 最后写入 21:35，而 `screens.db` 的记录一直到 23:18
    —— 那次运行**没走到保存就被结束了**，于是那一轮发现的会话全丢，
    重启后设置里只剩 1 个，而库里其实有 16 个。

    ⚠️ 补进来的条目是**临时**的（`from_db`），不会写回注册表 ——
       只有用户真的改了模式，`set_mode()` 才把它建成正式条目。
    """
    print("\n7f. 设置里的会话名单 = 注册表 ∪ 屏快照库")
    from wechat_assistant.sessions import merge_known_sessions

    reg = {"张三": {"mode": MODE_ON, "is_new": False}}
    db = {"张三": "2026-10-11 21:00:00",
          "财信息学院23级级群(499)": "2026-10-11 22:59:59",
          "giao (4)": "2026-10-11 22:03:59"}

    out = merge_known_sessions(reg, db, MODE_ON)
    check("并集：库里剩下来的也进来了", len(out) == 3, "%d 个" % len(out))
    check("注册表里那个会话**原样保留**（不被覆盖）",
          out["张三"] is reg["张三"], repr(out["张三"]))
    check("库补进来的条目标了 from_db", out["giao (4)"].get("from_db") is True)
    check("库补进来的条目跟着 default_mode",
          out["giao (4)"]["mode"] == MODE_ON, out["giao (4)"]["mode"])
    check("库补进来的**不是**新会话（别在界面上打「新」标）",
          out["giao (4)"]["is_new"] is False)

    check("注册表对象没被就地改动（不然临时条目会被当成真的落盘）",
          "giao (4)" not in reg)
    check("纯函数：再调一次结果一样",
          len(merge_known_sessions(reg, db, MODE_ON)) == 3)

    check("库是空的 -> 就是注册表", len(merge_known_sessions(reg, {}, MODE_ON)) == 1)
    check("注册表是空的 -> 全是库里那些（这正是「重启后」那一幕）",
          len(merge_known_sessions({}, db, MODE_ON)) == 3)
    check("两个都空 -> 空", merge_known_sessions(None, None, MODE_ON) == {})
    check("库里的空键被忽略",
          len(merge_known_sessions({}, {"": "x", "A": "y"}, MODE_ON)) == 1)


def test_run_watch_defines_before_use(tmp):
    """⚠️⚠️ `run_watch.main()` 里**局部函数必须先定义再使用**（2026-10-11 血案）。

    用户报的症状是「**悬浮窗没出现**」。根因不是界面坏了，是**代码顺序错了**：

        我把 `_session_source`（会话名单 = 注册表 ∪ 屏快照库）写在了
        `FloatingAssistant(session_source=_session_source, ...)` 的**后面** ——
        求值时 `NameError`。而那一整段被 `except Exception` 包着：

            except Exception as e:
                log("悬浮窗不可用（改用命令行输出）: ...")
                ui = None

        异常被吞成"降级运行"，**悬浮窗一个都不出现**，日志里只有一行
        「悬浮窗不可用」，看起来像界面/环境问题。

    ⚠️ 这正是这个项目反复栽的那个坑：**`except Exception: log(...); x = None`
        会把"代码有 bug"变成"某个功能静默不工作"**。

    判据：把 `main()` 里所有局部函数定义的行号收起来，再检查
    `FloatingAssistant(...)` 的每个关键字实参 —— 某个名字如果是局部函数，
    它的定义行必须**小于**调用行。
    """
    print("\n7g. run_watch 局部函数：先定义再用（否则悬浮窗静默不出现）")
    import ast
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = open(_os.path.join(root, "run_watch.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    main_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_fn = node
            break
    check("run_watch.py 里能找到 main()", main_fn is not None)
    if main_fn is None:
        return

    # ① main() 里（含嵌套）所有局部函数：名字 -> 定义行
    local_fns = {}
    for node in ast.walk(main_fn):
        if isinstance(node, ast.FunctionDef):
            local_fns.setdefault(node.name, node.lineno)

    # ② 所有 FloatingAssistant(...) 调用，检查关键字实参
    calls = [n for n in ast.walk(main_fn)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "FloatingAssistant"]
    check("main() 里确实建了 FloatingAssistant", len(calls) >= 1, "%d 处" % len(calls))
    bad = []
    for node in calls:
        for kw in node.keywords:
            if isinstance(kw.value, ast.Name) and kw.value.id in local_fns:
                if local_fns[kw.value.id] >= node.lineno:
                    bad.append((kw.arg, kw.value.id,
                                local_fns[kw.value.id], node.lineno))
    check("它的关键字实参都在调用之前定义好了", not bad,
          "反了（名字, 定义行, 调用行）: %s" % bad if bad else "")


def test_normalize_mode(tmp):
    print("\n8. mode 归一化（YAML 会把裸写的 on/off 当成布尔值）")
    from wechat_assistant.sessions import normalize_mode

    # 实测踩过：config 里写 `default_mode: on`，YAML 1.1 解析成布尔 True，
    # 于是 default_mode 永远不等于字符串 "on" -> 所有会话都不启用，且不报错。
    check("True -> on（YAML 的坑）", normalize_mode(True) == MODE_ON,
          repr(normalize_mode(True)))
    check("False -> off", normalize_mode(False) == MODE_OFF)
    check("None -> off", normalize_mode(None) == MODE_OFF)
    check("字符串 on", normalize_mode("on") == MODE_ON)
    check("大写 ON", normalize_mode("ON") == MODE_ON)
    check("带空格的 ' on '", normalize_mode("  on  ") == MODE_ON)
    check("yes -> on", normalize_mode("yes") == MODE_ON)
    check("字符串 off", normalize_mode("off") == MODE_OFF)
    check("no -> off", normalize_mode("no") == MODE_OFF)
    check("observe", normalize_mode("observe") == MODE_OBSERVE)
    check("乱填的 -> off（安全侧）", normalize_mode("随便什么") == MODE_OFF)

    # 端到端：默认值给成布尔 True 时也应当全体启用
    p = os.path.join(tmp, "n.json")
    r = make(p, default_mode=True)          # 故意模拟 YAML 解析成布尔的情况
    confirm(r, "甲")
    check("布尔 True 也能正确启用", r.is_enabled("甲"),
          "mode=%s default_mode=%r" % (r.mode_of("甲"), r.default_mode))



def test_title_walls(tmp):
    """两道「源头人墙」：**垃圾标题不建会话** + **两次确认才建** + 统一去噪。

    用户报（2026-10-11）：「开启后会出现一些不知道从哪里来的 X、G、0 这些」，
    并要求「可不可以**从源头上解决**」「**两次确定后再加新的会话**」「统一去噪」。

    ⚠️ 为什么必须**显式**验：它们是"拦在源头"的，失效时**不会报错**，
        只是列表里又开始冒出怪名字 —— 很难归因。
    """
    from wechat_assistant.sessions import is_junk_title, norm_title

    print("\n9. 垃圾标题不建会话 + 两次确认才建 + 统一去噪")

    # ---- 人墙 ①：垃圾标题
    junk = ["X", "G", "0", "口", "●", "😝😜", "999994", "", "   ", "口口"]
    bad = [t for t in junk if not is_junk_title(t)]
    check("垃圾标题全部判为 junk（%d 个）" % len(junk), not bad,
          "漏了: %r" % bad if bad else "")

    keep = ["张三", "示例23班", "白", "周", "示例群名(13)"]
    killed = [t for t in keep if is_junk_title(t)]
    check("正常标题一个都不能误杀（%d 个）" % len(keep), not killed,
          "误杀: %r" % killed if killed else "")
    check("  ⚠️ 单个**汉字**要保住（'白' 是正常昵称，不是乱码）",
          not is_junk_title("白"))
    check("  ⚠️ 单个**拉丁字母**要丢掉（那才是 OCR 碎片）", is_junk_title("G"))

    # ---- 人墙 ②：两次确认
    r = make(os.path.join(tmp, "walls.json"), default_mode=MODE_ON)
    check("第一次看到：**不建**（只记数）", r.discover("张三") is False)
    check("  这时列表里还没有它", "张三" not in r.sessions, repr(list(r.sessions)))
    check("第二次看到：**才建**，并返回 True（表示要落盘）",
          r.discover("张三") is True)
    check("  现在在列表里了", "张三" in r.sessions)
    check("只出现一次的乱码：永远建不上",
          r.discover("偶发乱码Q") is False and "偶发乱码Q" not in r.sessions)
    check("已登记的再看到返回 False（不是新发现）", r.discover("张三") is False)
    check("垃圾标题看两次也**不建**",
          r.discover("X") is False and r.discover("X") is False
          and "X" not in r.sessions)

    # ---- 统一去噪
    check("去噪: '●示例群…(13)' 和 '示例群…(13)' 归一化后相同",
          norm_title("●示例群名 (13)")
          == norm_title("示例群名(13)"))
    check("去噪: '口口示例群…口' 和 '示例群…' 归一化后相同",
          norm_title("口口示例群名口")
          == norm_title("示例群名"))
    check("  ⚠️ 归一化**不删数字**（'示例23班' 和 '软工二班' 是两个会话）",
          norm_title("示例23班") != norm_title("软工二班"))
    check("  ⚠️ 归一化会统一大小写", norm_title("项目A") == norm_title("项目a"))


def main():
    print("=" * 76)
    print("会话注册表测试（默认模式 / 白名单 / 黑名单 / 隐私边界）")
    print("=" * 76)

    tmp = tempfile.mkdtemp(prefix="wca_sess_")
    try:
        test_whitelist_mode(tmp)
        test_allow_all(tmp)
        test_pull_existing(tmp)
        test_disabled(tmp)
        test_explicit_setting(tmp)
        test_observe(tmp)
        test_persistence(tmp)
        test_touch(tmp)
        test_run_watch_discovers_sessions(tmp)
        test_system_sessions(tmp)
        test_merge_variants(tmp)
        test_merge_known_sessions(tmp)
        test_run_watch_defines_before_use(tmp)
        test_title_walls(tmp)
        test_normalize_mode(tmp)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 76)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
