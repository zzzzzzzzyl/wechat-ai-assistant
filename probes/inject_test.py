# -*- coding: utf-8 -*-
"""注入测试：把状态里的比对基线截短，模拟「来了新消息」。

为什么需要它：
    真实的新消息不可控（要等别人发）。这个脚本把 state.json 里某个会话的
    tail 人为截短，于是下一次轮询时，尾部多出来的那些消息就会被识别为「新消息」。
    这样就能在不等真消息的情况下，端到端验证
    「读微信 → 比对 → 报告」整条链路。

用法：
    python probes/inject_test.py             # 显示当前基线
    python probes/inject_test.py 4           # 把基线截到只剩 4 条
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.config import load_config, resolve_state_path

SESSION = "张三"


def main():
    cfg = load_config()
    path = resolve_state_path(cfg)
    if not os.path.exists(path):
        print("状态文件不存在: %s（先跑一次 run_watch.py）" % path)
        return 1

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    sync = data.get("sync", {})
    if not sync:
        print("状态里还没有任何会话基线（先跑一次 run_watch.py --once）")
        return 1

    if len(sys.argv) < 2:
        print("当前基线:")
        for name, v in sync.items():
            print("  %-24s %d 条" % (name, len(v.get("tail", []))))
        print("\n加个数字参数即可截短，例如: python probes/inject_test.py 4")
        return 0

    n = int(sys.argv[1])
    # 指定的会话名用第 2 个参数传；不传就处理全部（避免中文参数在不同终端下的编码坑）
    target = sys.argv[2] if len(sys.argv) > 2 else None
    names = [target] if target else list(sync.keys())

    changed = 0
    for name in names:
        tail = sync.get(name, {}).get("tail", [])
        if n >= len(tail):
            print("跳过 %s：要截到 %d，当前只有 %d 条" % (name, n, len(tail)))
            continue
        removed = tail[n:]
        sync[name]["tail"] = tail[:n]
        changed += 1
        print("已把 %s 的基线截短为 %d 条，被移除的 %d 条下次轮询会被当成「新消息」:"
              % (name, n, len(removed)))
        for item in removed:
            print("    -> %r" % (item,))

    if not changed:
        print("没有可截短的会话")
        return 0

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("\n现在运行: python run_watch.py --once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
