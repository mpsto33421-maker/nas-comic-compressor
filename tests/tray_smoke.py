"""트레이 동작 스모크 테스트: X → 트레이로 숨김, 숨긴 채 작업 계속, 완료 알림, 트레이 '종료'.
실행: QT_QPA_PLATFORM=offscreen python tests/tray_smoke.py"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("FAKE_BZ_SLEEP", "0.05")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon  # noqa: E402

import ncc.gui as gui  # noqa: E402
from test_ncc import FAKE, mk  # noqa: E402

# 헤드리스 환경에도 트레이가 있는 것처럼 동작시킴
QSystemTrayIcon.isSystemTrayAvailable = staticmethod(lambda: True)
messages = []
QSystemTrayIcon.showMessage = lambda self, *a, **k: messages.append(a[:2])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
boxes = []
QMessageBox.information = staticmethod(lambda parent, title, text: boxes.append(title))

base = tempfile.mkdtemp()
lib = os.path.join(base, "lib")
for v in range(4):
    for i in range(4):
        mk(lib, f"작품/{v:02d}권/{i:03d}.jpg")
real_runner = gui.BandizipRunner
gui.BandizipRunner = lambda p: real_runner([sys.executable, FAKE])

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
w = gui.MainWindow()
w.settings.setValue("close_to_tray", True)
w.act_close_to_tray.setChecked(True)
w.show()
fake = os.path.join(base, "bz")
open(fake, "w").close()
w._set_bz(fake)
w.ed_root.setText(lib)
assert w.tray is not None
log = []


def start():
    w._scan_clicked()
    QTimer.singleShot(300, after_scan)


def after_scan():
    if w.scan_thread.isRunning():
        QTimer.singleShot(100, after_scan)
        return
    w._start()
    QTimer.singleShot(150, close_to_tray)


def close_to_tray():
    w.close()  # X 버튼
    app.processEvents()
    assert not w.isVisible(), "X 후에도 창이 보임"
    assert w.job_thread is not None and w.job_thread.isRunning(), "X 후 작업이 멈춤"
    log.append(("tooltip", w.tray.toolTip()))
    QTimer.singleShot(100, wait_done)


def wait_done():
    if w.job_thread is not None:
        QTimer.singleShot(100, wait_done)
        return
    assert any("완료" in m[0] for m in messages), messages
    assert boxes == [], "숨겨진 상태에서 결과창이 바로 뜸"
    assert w._pending_summary
    w.show_from_tray()
    app.processEvents()
    assert w.isVisible()
    QTimer.singleShot(400, quit_step)


def quit_step():
    assert boxes == ["작업 결과"], boxes
    w.quit_app()


QTimer.singleShot(100, start)
app.exec()
print("tooltip during run:", log)
print("tray messages:", messages)
print("cbz:", sorted(f for d, _, fs in os.walk(lib) for f in fs if f.endswith(".cbz")))
print("TRAY OK")
