"""自检用的目标窗口：独立进程，方便验证跨进程居中。"""

from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QLabel, QWidget

parser = argparse.ArgumentParser()
parser.add_argument("--popup", action="store_true")
parser.add_argument("--same-class-feature", action="store_true")
parser.add_argument("--restore-after-show-ms", type=int, default=-1)
parser.add_argument("--delay-ms", type=int, default=0)
parser.add_argument("--lifetime-ms", type=int, default=9000)
args = parser.parse_args()

app = QApplication(sys.argv[:1])
window = QWidget()
window.setWindowTitle("Aplomb Test Target")
window.resize(640, 420)
window.move(60, 60)
label = QLabel("Aplomb 居中自检目标窗口\n这个窗口应当被精确移到屏幕正中。", window)
label.setGeometry(20, 20, 600, 380)

notice = None
if args.popup:
    notice = QWidget(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    notice.setWindowTitle("Aplomb Test Notice")
    notice.resize(280, 96)
    notice_label = QLabel("通知测试：它必须留在右下角。", notice)
    notice_label.setGeometry(16, 16, 248, 64)

feature = None
if args.same_class_feature:
    # 与主窗口同属 QWidget 窗口类，但使用无标题栏的独立顶层窗口，模拟
    # QQ 音乐等程序的音量/功能浮层。旧的“路径 + 类名”规则会误命中它。
    feature = QWidget(None, Qt.Window | Qt.FramelessWindowHint)
    feature.setWindowTitle("Aplomb Test Feature")
    feature.resize(360, 260)
    feature_label = QLabel("同类功能窗口：它不能被自动居中。", feature)
    feature_label.setGeometry(16, 16, 328, 220)


def show_windows() -> None:
    window.show()
    if args.restore_after_show_ms >= 0:
        QTimer.singleShot(
            args.restore_after_show_ms,
            lambda: window.move(83, 117),
        )
    if notice is not None:
        area = app.primaryScreen().availableGeometry()
        notice.move(area.right() - notice.width() - 24,
                    area.bottom() - notice.height() - 24)
        notice.show()
    if feature is not None:
        feature.move(110, 170)
        feature.show()


QTimer.singleShot(max(0, args.delay_ms), show_windows)
QTimer.singleShot(max(args.lifetime_ms, args.delay_ms + 1000), app.quit)
sys.exit(app.exec())
