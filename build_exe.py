# -*- coding: utf-8 -*-
"""打包入口：**先做一次假阳性豁免，再调 PyInstaller**。

为什么需要这一层
----------------
PyInstaller 在启动时会做一遍依赖体检（`PyInstaller/compat.py` 的
`check_requirements()`），里面有一条：

    装了 `pathlib` / `typing` / `enum34` 这些**标准库的旧回溯包**就拒绝运行。

理由是那些包会**遮蔽**标准库实现、导致一堆莫名其妙的报错。
但那句话只在"确实遮蔽了"的时候成立，而本机的情况是：

    import pathlib  ->  C:\\...\\Python311\\Lib\\pathlib.py     ← 标准库，赢了
    dist-info       ->  C:\\...\\site-packages\\pathlib-1.0.1.dist-info
                        （某个老项目（langchain-chatchat 那一系）的依赖留下的）

**sys.path 里标准库在 site-packages 之前，所以那份回溯包根本没被 import 过。**
PyInstaller 只看 `importlib.metadata.distribution("pathlib")` 有没有、不看它会不会被
加载，于是把这里判成了不兼容。

为什么不直接把那个包卸掉
------------------------
它是别人装的环境里的东西（`pip uninstall pathlib` 要写全局 site-packages，
而且可能影响那个老项目）。**为了打一个包去动用户全局 Python 环境，代价不对等。**
所以这里只在**本次进程内**让那次查询返回"没装"，其余一切照旧 —— 不动任何文件。

怎么用
------
    python build_exe.py                # 等于 python -m PyInstaller wechat_ai.spec
    python build_exe.py --clean        # 额外参数原样透传给 PyInstaller

⚠️ 如果哪天 `import pathlib` 真的指向了 site-packages（体检里会打印出来），
    这个豁免就是**不该用**的 —— 那时必须真的卸掉那个包，而不是绕过检查。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---------------------------------------------------------------- 体检
import importlib.metadata as _md          # noqa: E402
import pathlib as _pathlib                # noqa: E402


def _shadowed(name: str, real_file: str) -> bool:
    """这个回溯包**真的**遮蔽了标准库吗？

    判据很直白：**实际 import 到的那个文件，是不是落在这个包自己的
    site-packages 里**。是 -> 遮蔽了（PyInstaller 的检查是对的，别绕过）；
    不是 -> 标准库赢了，那个包只是躺在磁盘上没被加载。
    """
    try:
        dist = _md.distribution(name)
    except _md.PackageNotFoundError:
        print("[build] %s 回溯包：没装" % name)
        return False
    where = os.path.abspath(str(dist.locate_file("")))
    real = os.path.abspath(real_file)
    print("[build] %s 回溯包：装在 %s" % (name, where))
    print("[build] %s 回溯包：实际 import 到 %s" % (name, real))
    return os.path.normcase(real).startswith(
        os.path.normcase(where).rstrip("\\/") + os.sep)


def _waive_false_positive() -> None:
    if not _shadowed("pathlib", _pathlib.__file__):
        _orig = _md.distribution

        def _distribution(name, *a, **kw):
            if name == "pathlib":
                # 这次体检只问"装没装"，不问"会不会被加载"。上面已经确认它
                # 不会遮蔽标准库，所以这里替它回答"没装"。
                raise _md.PackageNotFoundError(name)
            return _orig(name, *a, **kw)

        _md.distribution = _distribution
        print("[build] 已豁免 pathlib 回溯包的假阳性（它没遮蔽标准库）")


_waive_false_positive()

# ---------------------------------------------------------------- 打包
SPEC = os.path.join(HERE, "wechat_ai.spec")
argv = [SPEC] + sys.argv[1:]
if not any(a in ("--noconfirm", "-y") for a in argv):
    argv.append("--noconfirm")

print("[build] PyInstaller %s" % argv)
from PyInstaller.__main__ import run   # noqa: E402

run(argv)
