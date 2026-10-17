"""探针：配置里的「视觉模型」到底认不认图。

背景（2026-10-13 实装）：`ocr_read.attach_image` 打开后，图片消息会被裁出来、
base64 塞进 `ChatSnapshot.images`，`AIClient.chat(images=...)` 再把 content
换成 OpenAI 多模态数组、模型换成 `ai.vision_model`。

**这条链路只有一个外部依赖：接口那头的模型真的收图。** 收不了图时症状很轻
—— 要么 400，要么模型装作没看见图（只按上下文里那堆 `[图片]` 猜），
后者在界面上和"功能好好的"一模一样。

所以本探针**绕过微信**：本地画一张写着已知文字的图，直接问模型图里写了什么。
答对 = 接口这一环通；答错/报错 = 问题在接口，不在读取层。

用法：
    python probes\\probe_vision_api.py            # 用 config.yaml + settings.json + secrets.yaml
    python probes\\probe_vision_api.py --text     # 只测纯文本（对照组，验证 key/地址没坏）

⚠️ 只读：不改配置、不写库、不上传真机截图（图是现画的）。
"""
import os
import sys
import base64
import io

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 图里写的文字 —— 故意挑模型不可能"猜中"的组合
IMG_TEXT = ["张三 外卖订单", "已下单 ¥19.00", "取餐号 A317"]

ANSWER_KEY = ["张三", "19", "A317"]


def make_image():
    """画一张白底黑字的图，返回 PNG bytes。"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (640, 240), "white")
    d = ImageDraw.Draw(img)
    font = None
    for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc"):
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.exists(p):
            try:
                font = ImageFont.truetype(p, 34)
                break
            except Exception:
                pass
    ys = (30, 105, 180)
    for text, y in zip(IMG_TEXT, ys):
        d.text((40, y), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main():
    only_text = "--text" in sys.argv

    from wechat_assistant.config import load_config
    from wechat_assistant.ai import AIClient, AIError

    cfg = load_config()
    ai = AIClient(cfg, logger=lambda m: print("   [log]", m))
    print("[配置] 接口 %r · 对话模型 %r · 视觉模型 %r"
          % (ai.base_url, ai.model, ai.vision_model))
    print("[配置] 按钮「识别图片」= %s"
          % ("开" if cfg.get("ocr_read.attach_image", False) else "关"))
    print("       缺项：%s" % ("、".join(ai.missing) or "无"))

    # ---- 对照组：纯文本 ----
    print("\n[1/2] 纯文本调用（验证 key / 地址 / 模型名本身没问题）")
    try:
        r = ai.chat("你只会说“收到”。", "测试一句就好。")
        print("      ✅ 通。模型回：%r" % (r or "")[:200])
    except AIError as e:
        print("      ❌ %s" % e)
        print("      → 连纯文本都不通，先修配置，别查图片。")
        return 1

    if only_text:
        return 0

    # ---- 图片 ----
    png = make_image()
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       ".tmp", "vision_probe_input.png")
    b64 = base64.b64encode(png).decode("ascii")
    print("\n[2/2] 看图（本地画的 %d 字节 PNG，内容：%s）" % (len(png), " / ".join(IMG_TEXT)))
    # 存一份图便于人眼核对。⚠️ 存不下来不算失败（沙箱下 `out\\` 可能拒写）
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(png)
        print("      输入图已存到 %s" % out)
    except OSError as e:
        print("      （输入图没存下来：%s）" % e)
    try:
        ans = ai.chat("你是 OCR。只把图里的文字原样抄出来，不要解释。",
                      "图里写了哪几行字？原样抄给我。",
                      images=[b64])
    except AIError as e:
        print("      ❌ 带图调用失败：%s" % e)
        print("      → 接口不接受内联图片。`ocr_read.attach_image` 打开也没用，")
        print("        要换成真支持视觉的模型 / 服务商（或走本地 OCR 那条路）。")
        return 2

    print("      模型回：%r" % (ans or "")[:400])
    hit = [k for k in ANSWER_KEY if k.lower() in (ans or "").lower()]
    print("      命中关键内容 %d/%d：%s" % (len(hit), len(ANSWER_KEY), hit))
    if len(hit) == len(ANSWER_KEY):
        print("      ✅ 接口这一环通：模型真的看见了图。")
        return 0
    if not hit:
        print("      ❌ 一个字都没读出来 —— 模型**没收到图**或收到了但看不见。")
        print("         （常见原因：模型名不支持视觉、网关把 content 数组降级成字符串）")
        return 3
    print("      ⚠️ 只看懂一部分，建议再跑一次确认（也可能是模型懒得抄全）。")
    return 4


if __name__ == "__main__":
    sys.exit(main())
