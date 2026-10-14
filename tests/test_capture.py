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


# ============================================================ 5. capture_mode 解析

def test_capture_mode():
    print("\n5. sender.capture_mode 解析（默认必须是 printwindow）")

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


if __name__ == "__main__":
    print("=" * 74)
    print("取图模块 & 发送者判定取图方式 测试")
    print("=" * 74)

    test_coords()
    test_crop()
    test_blank()
    test_capture_robust()
    test_capture_mode()

    print("\n" + "=" * 74)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 74)
    sys.exit(1 if FAIL else 0)
