# -*- coding: utf-8 -*-
"""新消息增量识别算法的单元测试。

不依赖微信，直接测 find_new_messages 的边界情况。
运行：python tests/test_increment.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.watcher import find_new_messages

# 消息标识 = (is_self, sender, text)
OTHER = False
MINE = True


def m(text, mine=False, sender=None):
    return (mine, sender, text)


PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  [通过] %s" % name)
    else:
        FAIL += 1
        print("  [失败] %s" % name)
        print("         期望: %r" % (want,))
        print("         实际: %r" % (got,))


def main():
    print("=" * 70)
    print("新消息增量识别测试")
    print("=" * 70)

    print("\n1. 没有变化")
    tail = [m("你好"), m("在吗", mine=True)]
    now = [m("你好"), m("在吗", mine=True)]
    check("无新消息", find_new_messages(tail, now), [])

    print("\n2. 追加一条")
    now = [m("你好"), m("在吗", mine=True), m("在的")]
    check("识别出一条", find_new_messages(tail, now), [m("在的")])

    print("\n3. 追加多条")
    now = [m("你好"), m("在吗", mine=True), m("在的"), m("帮我个忙"), m("急")]
    check("识别出三条", find_new_messages(tail, now),
          [m("在的"), m("帮我个忙"), m("急")])

    print("\n4. 同一句话连发两次（哈希去重会漏掉第二条）")
    tail = [m("在吗")]
    now = [m("在吗"), m("在吗")]
    check("重复内容也算新消息", find_new_messages(tail, now), [m("在吗")])

    print("\n5. 连发两条相同的，再加一条")
    tail = [m("在吗")]
    now = [m("在吗"), m("在吗"), m("在吗"), m("说话啊")]
    check("正确切分重复项", find_new_messages(tail, now), [m("在吗"), m("在吗"), m("说话啊")])

    print("\n6. 同上，但尾部还有重复")
    tail = [m("A"), m("B")]
    now = [m("A"), m("B"), m("A"), m("B")]
    check("尾部长匹配取最靠后的匹配", find_new_messages(tail, now), [m("A"), m("B")])

    print("\n7. 界面滚动导致可见消息完全变了（匹配不上）")
    tail = [m("老消息1"), m("老消息2")]
    now = [m("新的一屏1"), m("新的一屏2"), m("新的一屏3")]
    check("保守地只认最后一条", find_new_messages(tail, now), [m("新的一屏3")])

    print("\n8. 当前可见消息比基线还少（刚切换会话/向上滚动）")
    tail = [m("A"), m("B"), m("C")]
    now = [m("B"), m("C")]
    check("不认定有新消息", find_new_messages(tail, now), [])

    print("\n9. 我发出的消息也算新消息")
    tail = [m("多少钱")]
    now = [m("多少钱"), m("50半年", mine=True)]
    check("识别自己发的消息", find_new_messages(tail, now), [m("50半年", mine=True)])

    print("\n10. 群聊：带发送者标识")
    tail = [m("多少钱啊", sender="群友甲")]
    now = [m("多少钱啊", sender="群友甲"),
           m("我也来a", sender="群友甲"),
           m("50半年", mine=True)]
    check("群聊多发送者", find_new_messages(tail, now),
          [m("我也来a", sender="群友甲"), m("50半年", mine=True)])

    print("\n11. 基线为空（调用方应自行排除，这里只验证返回全部）")
    check("空基线返回全部", find_new_messages([], [m("A"), m("B")]), [m("A"), m("B")])

    print("\n12. 只有一条消息的情况")
    tail = [m("A")]
    now = [m("A")]
    check("单条无变化", find_new_messages(tail, now), [])
    now = [m("A"), m("B")]
    check("单条追加", find_new_messages(tail, now), [m("B")])

    print("\n13. JSON 往返（tuple 存进去、list 读回来）后仍应正确匹配")
    import json

    tail = [m("A"), m("B")]
    after_json = json.loads(json.dumps(tail))          # 模拟 state.json 存取
    check("原始 list 与内存 tuple 不相等（这是坑本身）",
          after_json == tail, False)
    normalized = [tuple(x) for x in after_json]        # get_tail() 里做的规范化
    check("规范化后无变化", find_new_messages(normalized, [m("A"), m("B")]), [])
    check("规范化后追加一条", find_new_messages(normalized, [m("A"), m("B"), m("C")]), [m("C")])
    check("规范化后追加多条", find_new_messages(normalized, [m("A"), m("B"), m("C"), m("D")]),
          [m("C"), m("D")])

    print("\n" + "=" * 70)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
