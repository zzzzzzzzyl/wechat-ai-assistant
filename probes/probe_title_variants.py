# -*- coding: utf-8 -*-
"""验证「换回 UIA 模式会不会因为会话名对不上而重复记录」。

    python probes/probe_title_variants.py

用户 2026-10-11 的问题：「换到 uia 模式，很多东西是不是会冲突，比如说用户名
原来拿不到颜文字，使用后可以拿到导致重复记录？」

这条**是真的**，因为它取决于两个已存在的事实：

  ① **`session_key` 就是会话标题原文**（`SessionRegistry.key_for()` 直接 return
     入参，没有归一化、没有别名表 —— 代码注释里自己写着「P0 直接沿用会话名
     （暂无别名映射）」）。
  ② **两个后端读同一个标题可能得到不同的字符串**：
     · UIA 读控件的 `Name` —— emoji / 颜文字 / 特殊符号**原样拿到**
     · OCR 认的是**字形** —— emoji 大概率认不出来（模型没那种训练数据）

  于是「♥️示例群名🏠」在 UIA 下是那个带 emoji 的 key，
  在 OCR 下可能是「示例群名」——**两个 key、两份记录**，
  同一个会话的历史被劈成两半，而且设置（关系/人设/黑名单）也各认各的。

这个脚本做两件事：
  A. **渲染**几个真实会话名（带 emoji / 颜文字）成标题栏样子的图，跑 OCR，
     看它到底能拿回多少 —— 这是"两个后端会不会给出不同字符串"的直接证据。
  B. 用注册表**实测**两个变体名会各建一条 —— 证明"重复记录"确实会发生。

⚠️ 纯本地：只渲染图和调 OCR，不碰微信。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi as _dpi  # noqa: E402

_dpi.enable()

import tempfile  # noqa: E402

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.ocr_reader import OcrReader  # noqa: E402
from wechat_assistant.sessions import (MODE_ON, SessionRegistry)  # noqa: E402

#: 用户 state.json 里真实存在的名字（前三个带 emoji / 特殊符号）
NAMES = [
    "张三",
    "♥️示例群名🏠",
    "🈲示例23班乙群🈲",
    "😝😜🤪😛😋",
    "群友丙",
    "公众号",
]


def find_font(size):
    """找一个能画中文的字体（不画 emoji —— 那正是要验的点）。"""
    for p in (r"C:\Windows\Fonts\msyh.ttc",
              r"C:\Windows\Fonts\msyh.ttf",
              r"C:\Windows\Fonts\simhei.ttf",
              r"C:\Windows\Fonts\simsun.ttc"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def render(name, font, pad=14):
    """把名字画成标题栏那样的一条（深底浅字，和微信一致）。"""
    tmp = Image.new("RGB", (10, 10))
    d = ImageDraw.Draw(tmp)
    box = d.textbbox((0, 0), name, font=font)
    w = max(60, box[2] - box[0] + pad * 2)
    h = max(40, box[3] - box[1] + pad * 2)
    img = Image.new("RGB", (w, h), (28, 28, 28))
    ImageDraw.Draw(img).text((pad - box[0], pad - box[1]), name,
                             font=font, fill=(230, 230, 230))
    return img


def main():
    cfg = load_config()
    rd = OcrReader(cfg)
    rd._engine().load()
    rd._engine().warmup()
    font = find_font(34)
    outdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
    os.makedirs(outdir, exist_ok=True)

    print("== A. 带 emoji / 颜文字的会话名，OCR 能拿回多少 ==")
    print("   （UIA 读控件的 Name，是**原样**的；下面看 OCR 认字形能拿到什么）\n")
    same = diff = 0
    for name in NAMES:
        img = render(name, font)
        blocks = rd._recognize(img)
        got = ""
        if blocks:
            got = min(blocks, key=lambda z: z.x1).text or ""
            got = got.strip()
        ok = (got == name)
        same += ok
        diff += (not ok)
        print("   真值 %-26r -> OCR %-26r  %s"
              % (name, got, "一致" if ok else "**不一致**"))
        if not ok:
            p = os.path.join(outdir, "title_variant_%s.png"
                             % name.replace("/", "_")[:12].strip() or "x")
            img.save(p)

    print("\n   => %d 个一致 / %d 个不一致" % (same, diff))
    if diff:
        print("   ⚠️ **不一致的那些就是隐患**：两个后端会给出不同的字符串。")

    print("\n== B. 注册表实测：两个变体名会不会各建一条 ==")
    d = tempfile.mkdtemp()
    a = "♥️示例群名🏠"
    b = "示例群名"

    # ① 关掉合并 = 修之前的行为（这一档要能复现出"重复"）
    off = SessionRegistry(os.path.join(d, "off.json"), default_mode=MODE_ON,
                          merge_variants=False)
    off.discover(a)
    off.discover(b)
    print("   merge_variants=False（修之前的行为）:")
    print("      会话表 %d 条：%s" % (len(off.sessions), list(off.sessions)))
    if len(off.sessions) == 2:
        print("      ⚠️ **确认会重复**：同一个会话被劈成两行，")
        print("         历史库 / 屏快照库 / 关系设定 / 人设 / 黑名单**各认各的**。")
    else:
        print("      （本来以为会重复，结果没有 —— 去查）")

    # ② 打开合并 = 现在的默认行为
    on = SessionRegistry(os.path.join(d, "on.json"), default_mode=MODE_ON,
                         merge_variants=True)
    on.discover(a)
    on.discover(b)
    print("\n   merge_variants=True（现在的默认）:")
    print("      会话表 %d 条：%s" % (len(on.sessions), list(on.sessions)))
    print("      key_for(%r) = %r" % (b, on.key_for(b)))
    k = list(on.sessions)[0]
    print("      raw_names = %s（两个写法都记着）"
          % on.sessions[k].get("raw_names"))
    if len(on.sessions) == 1 and on.key_for(b) == a:
        print("      ✅ 合并生效：两个写法认到同一条，历史不会被劈开")
    else:
        print("      ✗ **没合并上**")

    # ③ 边界：全 emoji 的名字归一化成空串 -> **绝不能**互相合并
    emo = SessionRegistry(os.path.join(d, "emo.json"), default_mode=MODE_ON,
                          merge_variants=True)
    emo.discover("😝😜🤪😛😋")
    emo.discover("🎉🎊")
    print("\n   全 emoji 的两个名字（归一化后都是空串）:")
    print("      会话表 %d 条：%s" % (len(emo.sessions), list(emo.sessions)))
    if len(emo.sessions) == 2:
        print("      ✅ 没有被误合并（空归一化名不参与合并）")
    else:
        print("      ✗ **被误合并了** —— 两个不同的会话挤成一条")

    print("\n== C. 现有代码里有别名/归一化机制吗 ==")
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "wechat_assistant", "sessions.py"), encoding="utf-8")
    text = src.read()
    print("   key_for 里有没有归一化/查表: %s"
          % ("有" if ("alias" in text or "_norm" in text) else "**没有** —— 直接 return 入参"))
    print("   `raw_names` 字段: %s"
          % ("有（但它只在**同一个 key** 下记变体，跨 key 用不上）"
             if "raw_names" in text else "没有"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
