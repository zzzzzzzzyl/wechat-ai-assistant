# -*- coding: utf-8 -*-
"""口吻样本 / 反模板 prompt / 关系设定 —— 效果对比测试。

同一段对话，用三种配置各生成一次，直接看差别：
    A. 只给基础人设（改造前的样子）
    B. 加关系设定
    C. 加关系设定 + 口吻样本（改造后）

用法：python tests/test_prompt_style.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.ai import AIClient, collect_style_samples
from wechat_assistant.config import load_config

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


class FakeMsg:
    """模拟 reader.MessageItem，用来测 collect_style_samples。"""

    def __init__(self, text, is_self):
        self.text = text
        self.is_self = is_self


def test_collect():
    print("1. 口吻样本采集")
    long_text = "这是一条特别长的消息" * 8      # 80 字，超过 max_len=60
    msgs = [
        FakeMsg("我可是正规的学习资料", False),   # 别人发的
        FakeMsg("行", True),
        FakeMsg("zsy正人君子", True),
        FakeMsg(long_text, True),                # 太长
        FakeMsg("https://example.com/abc", True),  # 链接
        FakeMsg("哦哦", True),
        FakeMsg("行", True),                     # 重复
        FakeMsg("?", None),                      # 未判定
    ]
    s = collect_style_samples(msgs, limit=12, max_len=60)
    print("     采集到: %s" % s)
    check("只取自己发的", all(x in ("行", "zsy正人君子", "哦哦") for x in s), str(s))
    check("排除超长消息", long_text not in s)
    check("排除链接", not any("http" in x for x in s))
    check("去重", len(s) == len(set(s)), str(s))
    check("保持时间顺序",
          s == ["zsy正人君子", "哦哦", "行"],
          "期望 ['zsy正人君子','哦哦','行']，实际 %s" % s)
    check("未判定的不采", "?" not in s)


def main():
    print("=" * 82)
    print("口吻样本 / 反模板 / 关系设定 —— 效果对比")
    print("=" * 82)

    test_collect()

    cfg = load_config()
    ai = AIClient(cfg)
    base_persona = cfg.get("persona.default")
    relationship = cfg.get("persona.default_relationship", "")

    history = [
        {"who": "李四", "text": "我可是正规的学习资料你在想什么"},
        {"who": "我", "text": "行"},
        {"who": "李四", "text": "看点文献论文什么的"},
        {"who": "我", "text": "zsy正人君子"},
    ]
    current = "你写完了吗，我还没动"

    # 模拟「我」平时的口吻：短、不客气、没句号
    samples = ["行", "zsy正人君子", "哦哦", "不知道啊", "不看了", "晚安"]

    print("\n2. 组装出的 system prompt 结构")
    _, _ = ai.build_prompts(base_persona, history, current, 3,
                            style_samples=samples, relationship=relationship)
    system, user = ai.build_prompts(base_persona, history, current, 3,
                                    style_samples=samples, relationship=relationship)
    for key in ("【你们的关系】",
                "【这是「我」平时说话的样子，照着模仿】",
                "【硬性要求：不要写成 AI 腔】",
                "【第一步 · 先判断对方那句话的真实意思】",
                "【第二步 · 按这个判断写 3 条候选】",
                "【输出格式】"):
        check("prompt 含 %s" % key, key in system)
    check("要求先判断气话", "气话" in system and "intent" in system)
    check("口吻样本进入了 prompt", all(s in system for s in samples))
    check("对话历史在 user 里", "李四" in user and current in user)

    print("\n3. 真实生成对比")
    try:
        r_plain = ai.generate_replies(base_persona, history, current, n_replies=3)
        print("\n  【A】只给基础人设：")
        for i, r in enumerate(r_plain, 1):
            print("      %d. %s" % (i, r))

        r_full = ai.generate_replies(base_persona, history, current, n_replies=3,
                                     style_samples=samples, relationship=relationship)
        print("\n  【C】关系 + 口吻样本 + 反模板：")
        for i, r in enumerate(r_full, 1):
            print("      %d. %s" % (i, r))

        check("两次都生成了 3 条", len(r_plain) == 3 and len(r_full) == 3)
        check("解析出了 intent 判断", bool(ai.last_intent), repr(ai.last_intent[:50]))

        # 下面两条是「效果对比」，**故意不做硬断言**：
        # 模型 temperature=1.0，同样的输入两次结果都可能不同（实测偶尔会翻），
        # 拿它当测试会造成偶发失败。硬断言留给确定性的部分 ——
        # prompt 结构、口吻样本采集、解析逻辑（见第 2 节与 test_ai.py）。
        avg_a = sum(len(x) for x in r_plain) / max(1, len(r_plain))
        avg_c = sum(len(x) for x in r_full) / max(1, len(r_full))
        print("\n  平均字数: A=%.1f  C=%.1f" % (avg_a, avg_c))
        if avg_c > avg_a + 2:
            print("  [提示] 这次 C 没比 A 短 —— 口吻样本都是短句，通常应当更短"
                  "（模型随机性，不判失败）")
        n_dot = sum(x.endswith("。") for x in r_full)
        if n_dot > 1:
            print("  [提示] 这次 C 有 %d/3 条以句号结尾 —— 反模板规则要求别习惯性加句号"
                  "（模型随机性，不判失败）" % n_dot)
    except Exception as e:
        check("真实生成", False, "%s: %s" % (type(e).__name__, e))

    print("\n5. 「我想说」：用户写了意图时 prompt 怎么变")
    my_intent = "我其实想回去，但不想显得被说服了"
    sys_on, _ = ai.build_prompts(base_persona, history, current, 3,
                                 style_samples=samples, relationship=relationship,
                                 user_intent=my_intent)
    check("prompt 含「最高优先级」段", "最高优先级" in sys_on and "想表达的意思" in sys_on)
    check("意图原文进了 prompt", my_intent in sys_on)
    check("第二步改成「同一立场的三种说法」", "同一个立场的三种说法" in sys_on)
    check("不再要求顺着对方的情绪写", "顺着对方的情绪走" not in sys_on)
    check("没给意图时仍然是原来的写法", "顺着对方的情绪走" in system)

    print("\n6. 真实生成对比：给不给意图，差别有多大")
    # 用一个**容易猜错立场**的场景：对方在念叨，但「我」其实想回去。
    history_home = [
        {"who": "妈妈", "text": "上回说国庆回来，也没见你回来"},
        {"who": "我", "text": "看情况吧"},
    ]
    current_home = "现在中秋节\n你不回来跟家人团聚\n一个人在那边"
    try:
        r_no = ai.generate_replies(base_persona, history_home, current_home,
                                   n_replies=3, relationship="家人，妈妈",
                                   style_samples=["行", "好", "嗯", "再看看"])
        print("\n  【不填意图】AI 自己判断立场：")
        print("     判断: %s" % ai.last_intent)
        for i, r in enumerate(r_no, 1):
            print("     %d. %s" % (i, r))

        r_yes = ai.generate_replies(base_persona, history_home, current_home,
                                    n_replies=3, relationship="家人，妈妈",
                                    style_samples=["行", "好", "嗯", "再看看"],
                                    user_intent="我其实想回去，但不想被说教")
        print("\n  【填了意图「我其实想回去，但不想被说教」】:")
        print("     判断: %s" % ai.last_intent)
        for i, r in enumerate(r_yes, 1):
            print("     %d. %s" % (i, r))

        check("两种情况下都生成了 3 条",
              len(r_no) == 3 and len(r_yes) == 3,
              "%d / %d" % (len(r_no), len(r_yes)))
        print("\n  ↑ 请人工看一眼：填了意图的那三条是否**都在表达「想回去」**")
    except Exception as e:
        check("真实生成对比", False, "%s: %s" % (type(e).__name__, e))

    print("\n" + "=" * 82)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 82)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
