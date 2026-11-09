# -*- coding: utf-8 -*-
"""取图与发送者判定的取图方式测试（离线，不需要真实微信窗口）。

锁住两个容易回归的点：

  1. **坐标换算**：PrintWindow 位图以窗口左上角为原点，而 UIA / GetWindowRect
     给的都是屏幕坐标。忘了减窗口原点，裁剪出来的就是错的位置——而且不会报错，
     只是悄悄判错发送者。所以这里把换算和越界收敛都钉死。

  2. **PrintWindow 返回值不能当成功标志**：本机 pywin32 的 `win32gui.PrintWindow`
     成功时返回 None 而不是 1。曾经写成 `if not ok: return None`，
     把**成功**的抓图当成失败（症状是「PrintWindow 不可用」但窗口明明好好的）。
     这里断言 `capture_window` 对无效句柄不抛异常、返回 None，
     有效/无效一律靠 `is_blank()` 判断内容。

用法：python tests/test_capture.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from wechat_assistant.capture import (capture_window, crop_window_region,  # noqa: E402
                                      is_blank, to_window_coords)
from wechat_assistant.sender import SenderResolver  # noqa: E402

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


class FakeCfg:
    """只实现 DottedKey.get()，够 SenderResolver 用。"""

    def __init__(self, **kw):
        self._d = kw

    def get(self, key, default=None):
        return self._d.get(key, default)


def solid(color, size=(200, 100)):
    return Image.new("RGB", size, color)


def striped(size=(200, 100)):
    """画几条黑线，保证不是纯色。"""
    img = Image.new("RGB", size, (255, 255, 255))
    px = img.load()
    for x in range(0, size[0], 4):
        for y in range(size[1]):
            px[x, y] = (0, 0, 0)
    return img


# ============================================================ 1. 坐标换算

def test_coords():
    print("\n1. 屏幕坐标 -> 窗口内坐标")
    wrect = (100, 50, 1100, 750)      # 窗口左上角在屏幕 (100, 50)

    check("基本换算",
          to_window_coords((300, 200, 500, 300), wrect) == (200, 150, 400, 250),
          str(to_window_coords((300, 200, 500, 300), wrect)))

    check("窗口在原点时不变",
          to_window_coords((10, 20, 30, 40), (0, 0, 500, 500)) == (10, 20, 30, 40))

    check("负坐标保留（交给裁剪收敛）",
          to_window_coords((-10, -20, 30, 40), wrect) == (-110, -70, -70, -10))

    check("rect 为空返回 None", to_window_coords(None, wrect) is None)
    check("wrect 为空返回 None", to_window_coords((1, 2, 3, 4), None) is None)


# ============================================================ 2. 裁剪与收敛

def test_crop():
    print("\n2. 从窗口位图裁剪（越界自动收敛）")
    img = solid((255, 255, 255), (1000, 600))
    wrect = (100, 50, 1100, 650)      # 窗口 1000x600

    sub = crop_window_region(img, wrect, (200, 150, 400, 250))
    check("正常裁剪尺寸", sub is not None and sub.size == (200, 100),
          str(sub.size if sub else None))

    # 右边超出窗口右边界（屏幕坐标 1100 之后）
    sub = crop_window_region(img, wrect, (900, 100, 2000, 200))
    check("右越界收敛到窗口宽度", sub is not None and sub.size == (200, 100),
          str(sub.size if sub else None))

    # 左边在窗口左边之外：屏幕 x 0..300，窗口从屏幕 x=100 开始，
    # 可见部分对应窗口内 0..200，所以宽度是 200（不是 100）
    sub = crop_window_region(img, wrect, (0, 100, 300, 200))
    check("左越界收敛", sub is not None and sub.size == (200, 100),
          str(sub.size if sub else None))

    check("完全在窗口外返回 None",
          crop_window_region(img, wrect, (5000, 5000, 6000, 6000)) is None)
    check("零面积返回 None",
          crop_window_region(img, wrect, (200, 150, 200, 250)) is None)
    check("图为 None 返回 None", crop_window_region(None, wrect, (200, 150, 400, 250)) is None)


# ============================================================ 3. 空白判定

def test_blank():
    print("\n3. 空白判定（判断 PrintWindow 到底画没画出东西）")

    check("纯色图判为空白", is_blank(solid((200, 200, 200))) is True)
    check("纯黑图判为空白", is_blank(solid((0, 0, 0))) is True)
    check("有条纹的图不是空白", is_blank(striped()) is False)
    check("None 判为空白", is_blank(None) is True)

    # 最小化时 PrintWindow 常常给整幅纯色，这正是要靠它拦掉的场景
    check("接近背景色的微小噪声仍算空白",
          is_blank(solid((240, 240, 240))) is True)


# ============================================================ 4. capture_window 健壮性

def test_capture_robust():
    print("\n4. capture_window 对无效输入不崩")
    for bad in (0, -1, 999999999):
        try:
            img = capture_window(bad)
            check("句柄 %s 不抛异常" % bad, img is None,
                  "返回 %s" % ("None" if img is None else "图"))
        except Exception as e:
            check("句柄 %s 不抛异常" % bad, False, "%s: %s" % (type(e).__name__, e))


# ============================================================ 5. 卡住的窗口不能拖死调用方

def test_hung_guard():
    """`capture_window` 的两道保险（2026-10-19 用户报「后台/休眠后卡死」）。

    `PrintWindow` 是**同步跨进程消息**、没有超时参数：目标窗口的 UI 线程不泵
    消息时它**永远不返回**（实测 >8s）。而调用方有一个在 **Qt UI 线程**上
    （自动主题每 5 秒重采），挂住就是整个面板假死。
    所以这里锁三件事：① 不响应的窗口根本不试；② 万一中途卡住，按超时返回 None；
    ③ 超时不抛异常、不阻塞调用方。
    """
    print("\n5. 目标窗口卡死时不拖死调用方")
    import time as _t

    from wechat_assistant import capture

    real_once = capture._capture_once
    real_resp = capture.window_responding
    real_hung = capture._hung_until
    try:
        # ---- ① 窗口"不响应"：一次 PrintWindow 都不该发
        calls = {"n": 0}

        def fake_once(hwnd, flags):
            calls["n"] += 1
            return striped()

        capture._capture_once = fake_once
        capture.window_responding = lambda hwnd: False
        t0 = _t.perf_counter()
        img = capture.capture_window(1)
        dt = _t.perf_counter() - t0
        check("不响应的窗口返回 None", img is None)
        check("不响应的窗口不去调 PrintWindow", calls["n"] == 0,
              "调用了 %d 次" % calls["n"])
        check("立刻返回（<0.2s）", dt < 0.2, "%.3fs" % dt)

        # ---- ② 正常窗口：照样能拿到图（保险不能误伤好路径）
        capture.window_responding = lambda hwnd: True
        img = capture.capture_window(1)
        check("响应正常时能拿到图",
              img is not None and img.size == (200, 100),
              str(img.size if img else None))

        # ---- ③ 抓图中途卡住：看门狗超时返回 None，且不抛异常
        def stuck(hwnd, flags):
            _t.sleep(5.0)          # 模拟卡在 SendMessage 里
            return striped()

        capture._capture_once = stuck
        t0 = _t.perf_counter()
        img = capture.capture_window(1, timeout=0.2)
        dt = _t.perf_counter() - t0
        check("卡住时返回 None", img is None)
        check("卡住时按超时返回（0.2~1.5s）", 0.15 <= dt < 1.5, "%.3fs" % dt)

        # ---- ④ 超时之后要退避：紧接着再抓不该再等一轮、也不该再发 PrintWindow
        #     （微信"每 2~4 秒卡一下"时，`IsHungAppWindow` 那道闸不会触发，
        #      全靠这条退避把 UI 线程的卡顿压到"每 10 秒最多一次"）
        check("超时后进入了退避", capture._hung_until > _t.monotonic())
        calls["n"] = 0
        t0 = _t.perf_counter()
        img = capture.capture_window(1)
        dt = _t.perf_counter() - t0
        check("退避期间返回 None", img is None)
        check("退避期间不再调 PrintWindow", calls["n"] == 0,
              "调用了 %d 次" % calls["n"])
        check("退避期间立刻返回（<0.05s）", dt < 0.05, "%.3fs" % dt)
    finally:
        capture._capture_once = real_once
        capture.window_responding = real_resp
        capture._hung_until = real_hung

    # ---- ⑤ 真实句柄判定：无效句柄 / 0 都必须判成"不响应"
    check("window_responding(0) = False",
          capture.window_responding(0) is False)
    check("window_responding(无效句柄) = False",
          capture.window_responding(999999999) is False)


# ============================================================ 6. capture_mode 解析

def test_capture_mode():
    print("\n6. sender.capture_mode 解析（默认必须是 printwindow）")

    def mode_of(**kw):
        cfg = FakeCfg(**kw)
        return SenderResolver(cfg, ocr=None).capture_mode

    check("不配置时默认 printwindow", mode_of() == "printwindow", mode_of())
    check("显式 printwindow", mode_of(**{"sender.capture_mode": "printwindow"}) == "printwindow")
    check("显式 screen", mode_of(**{"sender.capture_mode": "screen"}) == "screen")
    check("大小写/空格容错",
          mode_of(**{"sender.capture_mode": "  SCREEN  "}) == "screen")
    check("非法值回落到 printwindow",
          mode_of(**{"sender.capture_mode": "whatever"}) == "printwindow")
    check("空串回落到 printwindow",
          mode_of(**{"sender.capture_mode": ""}) == "printwindow")


def test_image_dedupe():
    """图片去重：同一张图的"两次裁剪"要判同，不同的图不能误并（2026-11-09）。

    背景：聊天里同一张图被连发两遍（重复转发的表情包 / 连发截图）时，
    同一次请求会带两张一样的图 —— 视觉模型按图算 token，等于白花一份钱。
    去重靠 **dHash**（64 位，PIL 自带，不引入 imagehash 依赖）。
    """
    print("\n7. 图片去重（dHash）")
    from wechat_assistant import capture

    base = Image.effect_noise((300, 200), 40).convert("RGB")

    # 模拟"同一张图，两次裁剪位置差 1px + 重新缩放"
    same_ish = base.crop((0, 0, 299, 199)).resize((300, 200))
    other = Image.effect_noise((300, 200), 40).convert("RGB")

    h1, h2, h3 = capture.dhash(base), capture.dhash(same_ish), capture.dhash(other)
    check("同一张图（偏移+缩放）判为相同", capture.is_same_picture(h1, h2),
          "汉明距离 %d" % bin(h1 ^ h2).count("1"))
    check("不同的图**不**判为相同", not capture.is_same_picture(h1, h3),
          "汉明距离 %d" % bin(h1 ^ h3).count("1"))

    kept, dropped = capture.dedupe_crops([base, same_ish, other])
    check("去重：3 张留 2 张", len(kept) == 2 and dropped == 1,
          "%d / %d" % (len(kept), dropped))
    check("去重保留**首次出现**的那张（顺序不变）",
          kept[0] is base and kept[1] is other)

    kept2, dropped2 = capture.dedupe_crops([base, base, base])
    check("同一张出现三次 -> 只留一张", len(kept2) == 1 and dropped2 == 2,
          "%d / %d" % (len(kept2), dropped2))

    # JPEG 重编码（模型实际收到的就是 JPEG）之后仍应判同
    import io

    buf = io.BytesIO()
    base.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    jpeg = Image.open(buf).convert("RGB")
    check("JPEG 重编码后仍判为同一张",
          capture.is_same_picture(h1, capture.dhash(jpeg)),
          "汉明距离 %d" % bin(h1 ^ capture.dhash(jpeg)).count("1"))


if __name__ == "__main__":
    print("=" * 74)
    print("取图模块 & 发送者判定取图方式 测试")
    print("=" * 74)

    test_coords()
    test_crop()
    test_blank()
    test_capture_robust()
    test_hung_guard()
    test_capture_mode()
    test_image_dedupe()

    print("\n" + "=" * 74)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 74)
    sys.exit(1 if FAIL else 0)
