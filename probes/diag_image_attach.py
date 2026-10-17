"""探针：现场诊断「图片发给模型看」这条链路断在哪一步。

背景：`ocr_read.attach_image` 打开后，`[图片]` 消息应该被裁成 base64 塞进
`ChatSnapshot.images`，`AIClient.chat` 再带进多模态请求。**任何一步断了都不会报错**
—— 表现就是"模型说它看不到图"。所以这里把每一步都量出来：

    ① 选的是哪条读取链路（UIA / OCR）· 开关开着没
    ② 读到的消息里 `kind == "image"` 有几条、rect 是不是空的
    ③ 抓窗口位图：抓到了没、是不是全黑（`is_blank`）
    ④ `snap.images` 最终有几张、每张多大（裁下来的图存到 out/）
    ⑤ （可选 `--ask`）真发给模型，问它图里写了什么

用法：微信开着、且**那个会话正显示在屏幕上**，然后

    python probes\\diag_image_attach.py            # 只诊断，不调接口
    python probes\\diag_image_attach.py --ask      # 顺带发一张给模型确认
    python probes\\diag_image_attach.py --backend ocr   # 强制走 OCR 链路

⚠️ 纯只读：不激活、不点击、不改配置、不写库。
⚠️ 和产品一样会把图**存到 out/**（自己看），`--ask` 才会**上传**给模型。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "image_attach")


def main():
    args = sys.argv[1:]
    ask = "--ask" in args
    forced = None
    attach = None
    for i, a in enumerate(args):
        if a == "--backend" and i + 1 < len(args):
            forced = args[i + 1].strip().lower()
        if a == "--attach" and i + 1 < len(args):
            attach = args[i + 1].strip().lower() in ("on", "1", "true", "yes")

    from wechat_assistant.config import load_config
    from wechat_assistant import capture

    over = {}
    if forced:
        over["reader.backend"] = forced
    if attach is not None:
        over["ocr_read.attach_image"] = attach

    cfg = load_config()
    if over:
        # 覆盖层：只在本探针进程里生效，不落盘
        cfg = _Override(cfg, over)

    print("[①] 开关 / 链路")
    print("    reader.backend = %r（本探针用 %r）"
          % (cfg.get("reader.backend", "auto"), forced or cfg.get("reader.backend", "auto")))
    print("    ocr_read.attach_image = %s"
          % cfg.get("ocr_read.attach_image", False))
    print("    ai.vision_model = %r" % (cfg.get("ai.vision_model") or "(空=用 ai.model)"))

    # ---- 建读取器（和 run_watch 一样走 make_reader）----
    from wechat_assistant.ocr_reader import make_reader

    worker = None
    if str(cfg.get("reader.backend", "auto")).lower() != "ocr":
        try:
            from wechat_assistant.uia_worker import UiaWorker
            worker = UiaWorker(timeout=20.0, logger=lambda m: None)
        except Exception as e:
            print("    ⚠️ UIA worker 建不起来：%s: %s" % (type(e).__name__, e))
            worker = None
    reader = make_reader(cfg, logger=lambda m: print("        [读取器]", m), worker=worker)
    try:
        real = type(reader).__name__
        inner = type(getattr(reader, "ocr", None) or getattr(reader, "uia", None) or reader).__name__
        print("    实际读取器：%s（内部 %s）" % (real, inner))

        win = reader.find_window()
        if not win:
            print("    ❌ 找不到微信主窗口 —— 先打开微信（不是「图片和视频」预览窗口）")
            return 1
        hwnd = win["hwnd"]
        print("    窗口 hwnd=%s pid=%d" % (hex(hwnd), win["pid"]))

        # 前台判断：PrintWindow 对**后台**窗口可能整幅全黑（实测过）
        import ctypes
        fg = ctypes.windll.user32.GetForegroundWindow()
        print("    微信是否前台：%s" % ("是" if fg == hwnd else "**否**（后台时抓图可能全黑）"))

        # ---- ③ 先单独量一次抓图 ----
        print("\n[③] 抓窗口位图（PrintWindow）")
        img = capture.capture_window(hwnd)
        if img is None:
            print("    ❌ capture_window 返回 None（GetWindowRect 失败 / 尺寸异常）")
        else:
            small = img.resize((64, 64)).convert("L")
            px = list(small.getdata())
            print("    ✅ 抓到 %dx%d，灰度范围 %d~%d，is_blank=%s"
                  % (img.width, img.height, min(px), max(px), capture.is_blank(img)))
            if capture.is_blank(img):
                print("    ⚠️ 全黑/纯色 —— 这就是「图发不出去」的第一嫌疑人：")
                print("       微信在后台时 PrintWindow 会画不出内容，裁出来的图没法用。")

        # ---- ②④ 走产品自己的读取 ----
        print("\n[②] read_chat()")
        snap = reader.read_chat(hwnd)
        if not snap:
            print("    ❌ 返回 None")
            return 1
        print("    标题=%r  is_group=%s  消息 %d 条"
              % (snap.title, snap.is_group, len(snap.messages)))
        _crop = getattr(reader, "last_crop", None)
        if _crop:
            print("    裁剪区 last_crop=%s -> 聊天区 %dx%d"
                  % (_crop, _crop[2] - _crop[0], _crop[3] - _crop[1]))

        imgs = [m for m in snap.messages if m.kind == "image"]
        print("    kind==image 的消息：%d 条" % len(imgs))
        for m in imgs:
            print("      · text=%r cls=%r rect=%s" % (m.text, m.cls, m.rect))
        for m in snap.messages:
            if m.kind != "text":
                print("      （非文本）%-8s %r  rect=%s"
                      % (m.kind, (m.text or "")[:24], m.rect))

        print("\n[④] snap.images（产品真正发给模型的东西）")
        print("    共 %d 张" % len(snap.images))
        if not snap.images:
            print("    ❌ 空 —— 模型当然说它看不到图。")
            print("       若上面 kind==image 是 0 条 → 图片消息没被认出来；")
            print("       若是 N 条但这里是 0 张 → 裁图那一步失败了（看 ③ 的 is_blank）。")
        else:
            from PIL import Image
            import io
            import base64
            os.makedirs(OUT, exist_ok=True)
            for i, b64 in enumerate(snap.images, 1):
                raw = base64.b64decode(b64)
                im = Image.open(io.BytesIO(raw))
                p = os.path.join(OUT, "crop_%02d.png" % i)
                try:
                    im.save(p)
                except OSError as e:
                    p = "(没存下来：%s)" % e
                print("    第 %d 张：PNG %d 字节，%s → %s" % (i, len(raw), im.size, p))
            try:
                from probes.ascii_shot import render
            except Exception:
                try:
                    sys.path.insert(0, os.path.join(ROOT, "probes"))
                    from ascii_shot import render
                except Exception:
                    render = None
            if render is not None and snap.images:
                im = Image.open(os.path.join(OUT, "crop_01.png"))
                print("\n    ↓ 第 1 张字符画（缩到 72 列；能看出裁得对不对）")
                print(render(im, cols=72))

        # ---- ⑤ 真的发给模型 ----
        if ask:
            print("\n[⑤] 真发一张给模型（走产品同一套 AIClient.chat）")
            if not snap.images:
                print("    ⏭️ 没图可发，跳过")
            else:
                from wechat_assistant.ai import AIClient, AIError
                ai = AIClient(cfg, logger=lambda m: None)
                try:
                    ans = ai.chat("你是 OCR。只把图里的文字原样抄出来。",
                                  "这张图里写了什么？原样抄。",
                                  images=[snap.images[0]])
                    print("    模型（%s）回：%r" % (ai.vision_model, (ans or "")[:300]))
                    print("    用量：%s" % ai.last_usage)
                except AIError as e:
                    print("    ❌ %s" % e)
                    return 2
    finally:
        if worker is not None:
            try:
                worker.close()
            except Exception:
                pass
    return 0


class _Override:
    """给 `Config` 加一层只读覆盖（本探针用来临时换 backend，不落盘）。"""

    def __init__(self, inner, over):
        self._inner = inner
        self._over = dict(over)

    def get(self, dotted, default=None):
        if dotted in self._over:
            return self._over[dotted]
        return self._inner.get(dotted, default)

    def __getattr__(self, name):
        return getattr(self._inner, name)


if __name__ == "__main__":
    sys.exit(main())
