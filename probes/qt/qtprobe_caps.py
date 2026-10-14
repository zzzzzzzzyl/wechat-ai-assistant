import sys, os, time
sys.path.insert(0, r"E:\Claude\wechat-ai-assistant")
sys.path.insert(0, r"E:\Claude\.qtprobe")
os.chdir(r"E:\Claude\wechat-ai-assistant")
from PySide6 import QtCore, QtGui, QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
w = QtWidgets.QWidget()
w.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.Tool)
w.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
w.resize(360, 420)
f = w.windowFlags()
print("1. ???            :", bool(f & QtCore.Qt.FramelessWindowHint))
print("2. ??              :", bool(f & QtCore.Qt.WindowStaysOnTopHint))
print("3. ?????(Tool)  :", bool(f & QtCore.Qt.Tool))
print("4. ??????      :", w.testAttribute(QtCore.Qt.WA_TranslucentBackground))
print("5. ??????      : QtGui.QPainter.Antialiasing?Qt ???")
print("6. ???? 2        : QPen.setWidth(2)")
print()
# ?????????????????? ?? ? Tk ? 516 ???
img = QtGui.QImage(360, 420, QtGui.QImage.Format_ARGB32)
img.fill(QtCore.Qt.transparent)
p = QtGui.QPainter(img)
p.setRenderHint(QtGui.QPainter.Antialiasing, True)
path = QtGui.QPainterPath()
path.addRoundedRect(QtCore.QRectF(0.5, 0.5, 359, 419), 10, 10)
p.fillPath(path, QtGui.QColor("#1E1E1F"))
pen = QtGui.QPen(QtGui.QColor("#3A3A3B")); pen.setWidth(2); p.setPen(pen)
p.drawPath(path)
p.end()
BODY = (0x1E,0x1E,0x1F); EDGE = (0x3A,0x3A,0x3B)
trans = 0
for y in range(420):
    for x in range(360):
        c = img.pixelColor(x, y)
        rgb = (c.red(), c.green(), c.blue())
        if c.alpha() > 0 and rgb not in (BODY, EDGE):
            trans += 1
print("?? + ???????? =", trans, "?Tk ????? 516?? 4x ???????")
print("=> Qt ?? setRenderHint ???")
