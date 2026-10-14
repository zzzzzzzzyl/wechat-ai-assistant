# -*- coding: utf-8 -*-
"""文本噪声判据 + 口吻样本过滤 + 发言人≠会话标题。

    python tests/test_textfilter.py

起因（2026-10-12 用户贴出真机输出）：
    口吻样本里出现了 `职位详情` / `D` / `KI` / `小程序` / `: 46`
    —— OCR 把图标/卡片/界面上的字读成了「我」发的消息，而它们**是 is_self=1**，
    原来只挡 from_ai=1，所以**真的进了口吻样本**，AI 会去模仿它们 ✗

⚠️ 这个文件刻意**不依赖微信、不依赖 Qt**（纯函数 + 一个临时 sqlite），随时能跑。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import ai, store as store_mod  # noqa: E402
from wechat_assistant.reader import (ChatSnapshot, MessageItem,  # noqa: E402
                                     label_unknown_senders)
from wechat_assistant.textfilter import (clean_style_samples,  # noqa: E402
                                         is_noise_text)

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


#: 用户在真机输出里贴出来的那批噪声（**必须全丢**）
JUNK = [
    "职位详情", "D", "B", "KI", ": 46", "小程序", "微信电脑版",
    "可领取以下优惠卷 社路79代比10代金势 21元", "活动规则 讲在有R", "21元",
]
#: 真的口吻样本（**一个都不许误杀**）—— 短句、单字、名字都算
KEEP = [
    "嗯", "好", "我在测试", "明天几点", "好啊", "多查", "王五",
    "想你了", "笑死我了哈哈哈", "晚安", "到了吗",
]


def test_noise_judge():
    print("\n1. 噪声判据（值不值得当口吻样本）")
    missed = [t for t in JUNK if not is_noise_text(t)]
    check("用户贴出来的噪声**全部**判为噪声（%d 个）" % len(JUNK), not missed,
          "漏网: %r" % missed if missed else "")
    killed = [t for t in KEEP if is_noise_text(t)]
    check("真口吻样本一个都不误杀（%d 个）" % len(KEEP), not killed,
          "误杀: %r" % killed if killed else "")
    # ⚠️ 边界：纯英文会被丢（`OK`）—— 这是**有意**的取舍，写下来免得以后当 bug 修
    check("  ⚠️ 纯英文（OK）会被丢 —— 有意的取舍（宁可少一条样本）",
          is_noise_text("OK"))
    check("  ⚠️ 单个汉字（嗯）**不许**丢（那是真实口吻）", not is_noise_text("嗯"))
    check("空串/空白算噪声", is_noise_text("") and is_noise_text("   "))
    check("clean_style_samples 保持顺序并去重",
          clean_style_samples(["嗯", "D", "好啊", "嗯"]) == ["嗯", "好啊"])


def test_style_paths():
    print("\n2. 两条取样路都要过滤（库 / 当前屏）")
    # ---- 当前屏那条（ai.collect_style_samples）
    class M(object):
        def __init__(self, text, me=True):
            self.text = text
            self.is_self = me

    msgs = [M("嗯"), M("职位详情"), M("KI"), M("好啊"), M("想你了")]
    got = ai.collect_style_samples(msgs)
    check("ai.collect_style_samples 过滤掉噪声",
          got == ["嗯", "好啊", "想你了"], str(got))

    # ---- 库里那条（store.own_style_texts）—— 真的建一个临时库
    tmp = tempfile.mkdtemp(prefix="wca_tf_")
    st = store_mod.MessageStore(os.path.join(tmp, "h.db"))
    try:
        # ⚠️ `save_new_only` 要的是**对象**（它 `getattr(m, "text")`），
        #    传 dict 会静默地一条都不写 —— 第一版就这么写的，
        #    于是"过滤成功"是因为库是空的（**通过得不对**）。
        #    现在用产品自己的 `MessageItem`，并且**断言真的写进去了**。
        #
        # ⚠️ 2026-10-14：必须**一屏一次调用**（记录器就是这么做的）。
        #    逐条调用时，第 2 条开始就"找不到重叠点"（上一句不在这屏里）
        #    -> 新规则下**一律不写**，于是这条断言会因为"库是空的"而失败 ——
        #    那不是噪声该不该入库的问题，是调用方式不对。
        added = st.save_new_only("s1", [
            MessageItem(text=t, cls="mmui::ChatTextItemView",
                        sender_name=None, is_self=True)
            for t in ["嗯", "职位详情", "D", "我在测试", "小程序", "好啊"]])
        check("噪声**确实写进了库**（is_self=1）—— 所以闸必须在取样处",
              added >= 6, "新增 %d 条" % added)
        got2 = st.own_style_texts("s1")
        check("store.own_style_texts 过滤掉噪声",
              got2 == ["嗯", "我在测试", "好啊"], str(got2))
    finally:
        try:
            st.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_title_sender():
    print("\n3. 发言人 ≠ 当前会话标题（用户实测：`张三: …`）")

    def snap(title, group, rows):
        s = ChatSnapshot(title=title, is_group=group)
        for who, t in rows:
            s.messages.append(MessageItem(
                text=t, cls="mmui::ChatTextItemView",
                sender_name=who, is_self=False))
        label_unknown_senders(s)
        return [(m.sender_name or "", m.text) for m in s.messages]

    got = snap("张三", False, [("张三", "广东省教育厅原厅长罗伟其被开除党籍")])
    check("私聊里「发言人 == 会话标题」被清掉", got[0][0] == "", str(got))

    # ⚠️ **群聊里不清**（有意的）：群里标题通常是群名，
    #    而"某人真名恰好等于群名"的概率高得多，清了会丢**真名字**。
    got2 = snap("小明", True, [("小明", "在吗"), ("细姨", "嗯")])
    check("  ⚠️ 群聊里**不动**（标题=群名时清了会丢真名字）",
          got2[0][0] == "小明", str(got2))
    check("  群里认不出昵称的仍然标「群友」",
          snap("项目群", True, [("细姨", "嗯"), (None, "看图")])[1][0] == "群友")

    # 不相等时当然不动
    got3 = snap("张三", False, [("南方日报", "王五")])
    check("标题不同名时不动（`南方日报` 那类靠别的判据，不归这条管）",
          got3[0][0] == "南方日报", str(got3))


def main():
    print("=" * 72)
    print("文本噪声判据 / 口吻样本 / 发言人≠标题")
    print("=" * 72)
    test_noise_judge()
    test_style_paths()
    test_title_sender()
    print("\n" + "=" * 72)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
