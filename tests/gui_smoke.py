"""GUI 스모크 테스트: 검색 → 압축 → 결과까지 자동 진행하고 스크린샷 저장.
실행: QT_QPA_PLATFORM=offscreen python tests/gui_smoke.py <출력png>"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import ncc.gui as gui  # noqa: E402
from test_ncc import FAKE, mk, PNG  # noqa: E402

out_png = sys.argv[1] if len(sys.argv) > 1 else "gui.png"
base = tempfile.mkdtemp()
lib = os.path.join(base, "만화책")
for v in ("01권", "02권", "03권"):
    for i in range(5):
        mk(lib, f"슬램덩크/{v}/{i:03d}.jpg")
mk(lib, "웹툰A/001.png", PNG); mk(lib, "웹툰A/002.png", PNG); mk(lib, "작품C/cover.jpg"); mk(lib, "작품C/01권/001.jpg"); mk(lib, "작품C/01권/002.jpg")
mk(lib, "원피스/01권.zip", b"PK\x05\x06" + b"\0" * 18); mk(lib, "원피스/01권/001.jpg")

fake_bz = os.path.join(base, "bz")  # is_valid_bz 통과용 이름
real_runner = gui.BandizipRunner
gui.BandizipRunner = lambda p: real_runner([sys.executable, FAKE])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
results = {}
QMessageBox.information = staticmethod(lambda parent, title, text: results.setdefault("text", text))

app = QApplication(sys.argv)
gui.apply_dark(app)
w = gui.MainWindow()
w.show()
open(fake_bz, "w").close()
w._set_bz(fake_bz)
w.ed_root.setText(lib)
w.ed_root.setFocus()  # 시작 시 입력칸 비활성화로 목록이 지워지던 버그 재현용


def step_scan():
    w._scan_clicked()


def wait_scan():
    if w.scan_thread and w.scan_thread.isRunning():
        QTimer.singleShot(100, wait_scan)
        return
    print("scan:", w.lbl_scan.text())
    w.ed_root.setFocus()
    w._start()
    w.ed_root.editingFinished.emit()  # 입력칸 비활성화 시 발생하는 신호
    app.processEvents()
    assert w.model.rowCount() == 5, "작업 시작 후 목록이 사라짐"
    QTimer.singleShot(100, wait_job)


def wait_job():
    if w.job_thread is not None:
        QTimer.singleShot(100, wait_job)
        return
    print(results.get("text"))
    app.processEvents()
    w.grab().save(out_png)
    print("statuses:", [c.status for c in w.model.items])
    assert w.proxy.rowCount() == 5
    w.table.selectRow(0)
    w._check_rows([0], False)
    assert not w.model.items[0].checked
    print("cbz:", sorted(os.path.relpath(os.path.join(d, f), lib) for d, _, fs in os.walk(lib) for f in fs if f.endswith(".cbz")))
    w.close()
    app.quit()


QTimer.singleShot(200, step_scan)
QTimer.singleShot(400, wait_scan)
app.exec()
shutil.rmtree(base, ignore_errors=True)
