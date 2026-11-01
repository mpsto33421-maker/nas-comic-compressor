"""자동 테스트 (임시 디렉터리 + 가짜 bz).  실행: python -m pytest -q tests"""
import os
import sys
import threading
import time
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ncc.bandizip import BandizipRunner, find_bandizip  # noqa: E402
from ncc.compressor import (Compressor, Options, find_temp_artifacts,  # noqa: E402
                            remove_temp_artifact)
from ncc.jobs import BatchJob, Callbacks  # noqa: E402
from ncc.runlog import RunLog  # noqa: E402
from ncc.scanner import scan  # noqa: E402

FAKE = os.path.join(os.path.dirname(__file__), "fake_bz.py")
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 200


def mk(base, rel, data=JPG):
    p = os.path.join(base, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def runner():
    bz = os.environ.get("NCC_REAL_BZ")  # Windows 실기 테스트용: 실제 bz.exe 경로
    if bz:
        return BandizipRunner(bz)
    return BandizipRunner([sys.executable, FAKE])


def rel(root, paths):
    return sorted(os.path.relpath(p, root).replace("\\", "/") for p in paths)


def run_all(root, tmp_path, opt=None, items=None):
    sr = scan(str(root), min_images=1)
    log = RunLog(str(tmp_path / "logs"))
    job = BatchJob(str(root), items if items is not None else sr.candidates, runner(),
                   opt or Options(lock_retry_seconds=2), log, Callbacks(), sr)
    s = job.run()
    log.close()
    return sr, s, log


# 1. 이미지가 직접 들어 있는 폴더 (유형 B)
def test_01_direct_images(tmp_path):
    r = tmp_path / "lib"
    mk(r, "웹툰A/001.jpg"); mk(r, "웹툰A/002.png", PNG); mk(r, "웹툰A/003.webp", WEBP)
    sr, s, _ = run_all(r, tmp_path)
    assert rel(r, [c.path for c in sr.candidates]) == ["웹툰A"]
    assert s.success == 1
    out = r / "웹툰A.cbz"
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == ["001.jpg", "002.png", "003.webp"]
    assert (r / "웹툰A" / "001.jpg").exists()  # 원본 보존(기본값)


# 2. 권별 하위 폴더 (유형 A)
def test_02_volume_subfolders(tmp_path):
    r = tmp_path / "lib"
    for v in ("01권", "02권"):
        mk(r, f"슬램덩크/{v}/001.jpg"); mk(r, f"슬램덩크/{v}/002.jpg")
    sr, s, _ = run_all(r, tmp_path)
    assert rel(r, [c.path for c in sr.candidates]) == ["슬램덩크/01권", "슬램덩크/02권"]
    assert (r / "슬램덩크" / "01권.cbz").exists() and (r / "슬램덩크" / "02권.cbz").exists()
    assert not (r / "슬램덩크.cbz").exists()
    with zipfile.ZipFile(r / "슬램덩크" / "01권.cbz") as z:
        assert sorted(z.namelist()) == ["001.jpg", "002.jpg"]  # 상위 경로 없음


# 3. 기존 ZIP만 있는 폴더 (유형 C)
def test_03_only_archives(tmp_path):
    r = tmp_path / "lib"
    for v in ("01권.zip", "02권.zip", "03권.cbz"):
        mk(r, f"원피스/{v}", b"PK\x05\x06" + b"\x00" * 18)
    before = {p: os.path.getmtime(p) for p in map(str, (r / "원피스").iterdir())}
    sr, s, _ = run_all(r, tmp_path)
    assert sr.candidates == []
    assert {p: os.path.getmtime(p) for p in map(str, (r / "원피스").iterdir())} == before


# 4. ZIP과 이미지 폴더 혼합 (유형 D) + 같은 이름 zip이 있는 폴더는 제외
def test_04_mixed(tmp_path):
    r = tmp_path / "lib"
    mk(r, "만화A/01권.zip", b"PK\x05\x06" + b"\x00" * 18)
    mk(r, "만화A/01권/001.jpg")  # 이미 01권.zip 이 있으므로 제외되어야 함
    mk(r, "만화A/02권/001.jpg"); mk(r, "만화A/03권/001.jpg")
    sr, s, _ = run_all(r, tmp_path)
    assert rel(r, [c.path for c in sr.candidates]) == ["만화A/02권", "만화A/03권"]
    assert any("같은 이름의 압축파일" in x.reason for x in sr.skipped)
    assert (r / "만화A" / "01권.zip").read_bytes().startswith(b"PK\x05\x06")
    assert not (r / "만화A" / "01권.cbz").exists()


# 5. 다단계 하위 폴더 (유형 E)
def test_05_deep(tmp_path):
    r = tmp_path / "lib"
    mk(r, "작가별/작가A/작품B/01권/001.jpg"); mk(r, "작가별/작가A/작품B/02권/001.jpg")
    mk(r, "작가별/작가A/작품B/02권/extra/readme.txt", b"hi")  # 이미지 없는 하위폴더
    sr, s, _ = run_all(r, tmp_path)
    assert rel(r, [c.path for c in sr.candidates]) == ["작가별/작가A/작품B/01권", "작가별/작가A/작품B/02권"]
    v2 = [c for c in sr.candidates if c.name == "02권"][0]
    assert v2.empty_subdirs == ["extra"] and not v2.deletable_layout
    assert s.success == 2


# 6. 이미지와 표지파일이 섞인 폴더 (유형 F)
def test_06_cover_with_volumes(tmp_path):
    r = tmp_path / "lib"
    mk(r, "작품C/cover.jpg"); mk(r, "작품C/01권/001.jpg"); mk(r, "작품C/02권/001.jpg")
    sr, s, _ = run_all(r, tmp_path)
    assert rel(r, [c.path for c in sr.candidates]) == ["작품C/01권", "작품C/02권"]
    assert (r / "작품C" / "cover.jpg").exists()
    assert not (r / "작품C.cbz").exists()
    assert any("상위 폴더 보존" in x.reason for x in sr.skipped)


# 7. 한글/일본어 파일명 + 8. 공백/특수문자 경로
def test_07_08_unicode_and_special(tmp_path):
    r = tmp_path / "lib"
    d = "[作者] ワンピース 第1巻 (완결) & 특별편 #1 ~ 100%"
    names = ["표지 01.jpg", "ページ_02.JPG", "-dash.jpg", "a,b;c'd.png"]
    for n in names:
        mk(r, f"{d}/{n}", PNG if n.endswith(".png") else JPG)
    sr, s, _ = run_all(r, tmp_path)
    assert s.success == 1, s.lines()
    with zipfile.ZipFile(r / (d + ".cbz")) as z:
        assert sorted(z.namelist()) == sorted(names)


# 9. 동일 이름 CBZ가 이미 있는 경우
def test_09_existing_cbz(tmp_path):
    r = tmp_path / "lib"
    mk(r, "작품/01권/001.jpg")
    existing = mk(r, "작품/01권.cbz", b"ORIGINAL")
    sr, s, _ = run_all(r, tmp_path)
    assert sr.candidates == []
    assert open(existing, "rb").read() == b"ORIGINAL"


def test_09b_output_appears_after_scan(tmp_path):
    r = tmp_path / "lib"
    mk(r, "작품/01권/001.jpg")
    sr = scan(str(r), min_images=1)
    mk(r, "작품/01권.cbz", b"ORIGINAL")  # 스캔 후 생김
    log = RunLog(str(tmp_path / "logs"))
    s = BatchJob(str(r), sr.candidates, runner(), Options(), log, Callbacks(), sr).run()
    assert s.skipped == 1 and (r / "작품" / "01권.cbz").read_bytes() == b"ORIGINAL"


# 10. 압축 도중 오류 (압축 실패 / 검사 실패 / 내부 목록 불일치)
@pytest.mark.parametrize("mode", ["create", "test", "badname", "extra"])
def test_10_errors(tmp_path, monkeypatch, mode):
    if os.environ.get("NCC_REAL_BZ"):
        pytest.skip("가짜 bz 전용")
    monkeypatch.setenv("FAKE_BZ_FAIL", mode)
    r = tmp_path / "lib"
    mk(r, "작품/01권/001.jpg"); mk(r, "작품/01권/한글.jpg")
    sr, s, _ = run_all(r, tmp_path, Options(delete_originals=True))
    assert s.failed == 1 and s.success == 0
    assert not (r / "작품" / "01권.cbz").exists()
    assert find_temp_artifacts(str(r)) == []          # 불완전한 파일이 남지 않음
    assert (r / "작품" / "01권" / "001.jpg").exists()  # 원본 보존


def test_10b_consecutive_failure_stop(tmp_path, monkeypatch):
    if os.environ.get("NCC_REAL_BZ"):
        pytest.skip("가짜 bz 전용")
    monkeypatch.setenv("FAKE_BZ_FAIL", "create")
    r = tmp_path / "lib"
    for i in range(6):
        mk(r, f"작품/{i:02d}권/001.jpg")
    sr, s, _ = run_all(r, tmp_path, Options(max_consecutive_failures=3))
    assert s.failed == 3 and s.not_run == 3 and "연속" in s.stop_reason


# 11. 작업 중 취소
def test_11_cancel(tmp_path, monkeypatch):
    if os.environ.get("NCC_REAL_BZ"):
        pytest.skip("가짜 bz 전용")
    monkeypatch.setenv("FAKE_BZ_SLEEP", "0.3")
    r = tmp_path / "lib"
    for v in range(3):
        for i in range(10):
            mk(r, f"작품/{v:02d}권/{i:03d}.jpg")
    sr = scan(str(r), min_images=1)
    log = RunLog(str(tmp_path / "logs"))
    job = BatchJob(str(r), sr.candidates, runner(), Options(), log, Callbacks(), sr)
    t = threading.Thread(target=job.run)
    t.start()
    time.sleep(1.5)
    job.cancel()
    t.join(30)
    assert not t.is_alive()
    s = job.summary
    assert s.cancelled + s.not_run >= 2
    assert not list((r / "작품").glob("*.cbz"))
    assert find_temp_artifacts(str(r)) == []
    assert len(list((r / "작품" / "00권").iterdir())) == 10


def test_11b_pause_resume(tmp_path):
    r = tmp_path / "lib"
    mk(r, "a/01/1.jpg"); mk(r, "a/02/1.jpg")
    sr = scan(str(r), min_images=1)
    log = RunLog(str(tmp_path / "logs"))
    paused = []
    job = BatchJob(str(r), sr.candidates, runner(), Options(), log,
                   Callbacks(paused=lambda p: paused.append(p)), sr)
    job.pause()
    t = threading.Thread(target=job.run)
    t.start()
    time.sleep(0.5)
    assert job.summary.success == 0
    job.resume()
    t.join(30)
    assert job.summary.success == 2 and paused[:2] == [True, False]


# 12. 접근 권한이 없는 폴더 (실제 권한 차단: POSIX chmod / Windows icacls)
@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root는 권한 검사를 우회")
def test_12_no_permission(tmp_path):
    import subprocess
    r = tmp_path / "lib"
    mk(r, "작품/01권/001.jpg"); mk(r, "작품/잠금/001.jpg"); mk(r, "작품/cover.jpg")
    locked = r / "작품" / "잠금"
    user = os.environ.get("USERNAME", "")
    if os.name == "nt":
        subprocess.run(["icacls", str(locked), "/deny", f"{user}:(OI)(CI)(RX)"], capture_output=True)
    else:
        os.chmod(locked, 0)
    try:
        try:
            os.listdir(locked)
            pytest.skip("이 환경에서는 접근 차단을 만들 수 없음 (test_12b가 같은 로직을 모의 검증)")
        except PermissionError:
            pass
        sr = scan(str(r), min_images=1)
        assert rel(r, [c.path for c in sr.candidates]) == ["작품/01권"]
        assert any("잠금" in e.path for e in sr.errors)
    finally:
        if os.name == "nt":
            subprocess.run(["icacls", str(locked), "/remove:d", user], capture_output=True)
        else:
            os.chmod(locked, 0o755)


def test_12b_unreadable_subdir_protects_parent(tmp_path, monkeypatch):
    """하위 폴더 목록을 못 읽으면 (권한/네트워크) 상위 폴더를 압축하지 않음 - root에서도 검증 가능하도록 os.scandir 모킹."""
    r = tmp_path / "lib"
    mk(r, "작품/001.jpg"); mk(r, "작품/잠금/001.jpg")
    real = os.scandir

    def fake(p):
        if os.path.basename(str(p)) == "잠금":
            raise PermissionError(13, "Permission denied", str(p))
        return real(p)
    monkeypatch.setattr("ncc.scanner.os.scandir", fake)
    sr = scan(str(r), min_images=1)
    assert sr.candidates == []
    assert any("오류" in x.reason for x in sr.skipped)
    assert sr.errors


# ---- 추가 안전성 테스트 ------------------------------------------------
def test_root_not_compressed_and_ignored_dirs(tmp_path):
    r = tmp_path / "lib"
    mk(r, "loose.jpg"); mk(r, ".sync/x/1.jpg"); mk(r, "@eaDir/a/1.jpg"); mk(r, "#recycle/b/1.jpg")
    sr = scan(str(r), min_images=1)
    assert sr.candidates == []


def test_non_image_files_kept_and_not_deleted(tmp_path):
    r = tmp_path / "lib"
    names = [f"{i:03d}.jpg" for i in range(1, 8)]
    for n in names:
        mk(r, f"작품/01권/{n}")
    mk(r, "작품/01권/info.txt", b"t"); mk(r, "작품/01권/inner.zip", b"PK")
    sr, s, _ = run_all(r, tmp_path, Options(delete_originals=True))
    assert s.success == 1
    with zipfile.ZipFile(r / "작품" / "01권.cbz") as z:
        assert sorted(z.namelist()) == names         # 압축파일 안에 압축파일 없음
    assert (r / "작품" / "01권" / "001.jpg").exists()  # 이미지 외 파일 → 삭제 금지


def test_delete_originals_verified(tmp_path):
    r = tmp_path / "lib"
    mk(r, "작품/01권/001.jpg"); mk(r, "작품/01권/002.jpg", JPG + b"x")
    sr, s, _ = run_all(r, tmp_path, Options(delete_originals=True))
    assert s.success == 1 and s.deleted_originals == 1
    assert not (r / "작품" / "01권").exists()
    with zipfile.ZipFile(r / "작품" / "01권.cbz") as z:
        assert z.read("002.jpg") == JPG + b"x"


def test_changed_after_scan(tmp_path):
    r = tmp_path / "lib"
    mk(r, "작품/01권/001.jpg")
    sr = scan(str(r), min_images=1)
    mk(r, "작품/01권/002.jpg")
    log = RunLog(str(tmp_path / "logs"))
    msgs = []
    s = BatchJob(str(r), sr.candidates, runner(), Options(), log,
                 Callbacks(item_finished=lambda i, res: msgs.append(res.message)), sr).run()
    assert s.failed == 1 and "다시 검색" in msgs[0]
    assert not (r / "작품" / "01권.cbz").exists()


def test_large_file_list_chunked(tmp_path):
    r = tmp_path / "lib"
    for i in range(700):
        mk(r, f"작품/01권/아주_긴_파일이름_테스트용_{i:05d}_" + "가" * 20 + ".jpg", JPG)
    sr, s, _ = run_all(r, tmp_path)
    assert s.success == 1
    with zipfile.ZipFile(r / "작품" / "01권.cbz") as z:
        assert len(z.namelist()) == 700


def test_lock_retry(tmp_path, monkeypatch):
    """NAS 동기화/백신이 파일을 잠깐 잠그는 상황(이전 버전의 Errno 13) → 재시도 후 성공."""
    import ncc.compressor as comp
    r = tmp_path / "lib"
    mk(r, "작품/01권/001.jpg")
    real = comp.rename_noreplace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 4:
            raise PermissionError(13, "Permission denied", a)
        return real(a, b)
    monkeypatch.setattr(comp, "rename_noreplace", flaky)
    sr, s, _ = run_all(r, tmp_path, Options(lock_retry_seconds=20))
    assert s.success == 1 and calls["n"] == 4


def test_temp_cleanup(tmp_path):
    r = tmp_path / "lib"
    t1 = mk(r, "작품/.ncc-0123456789abcdef.tmp.zip", b"PK")
    mk(r, "작품/.ncc-nsvv0xez.tmp/result.zip", b"PK")
    mk(r, "작품/.ncc-zzzzzzzz.tmp/other.txt", b"x")
    found = find_temp_artifacts(str(r))
    assert len(found) == 3
    ok = 0
    for f in found:
        try:
            remove_temp_artifact(f)
            ok += 1
        except OSError:
            pass
    assert ok == 2 and not os.path.exists(t1)
    assert (r / "작품" / ".ncc-zzzzzzzz.tmp" / "other.txt").exists()


def test_find_bandizip_none_ok():
    find_bandizip()  # 예외 없이 동작


# ---- 표지/낱장 이미지 보호 (v1.1) ----------------------------------------
def test_cover_with_volume_archives_not_compressed(tmp_path):
    """작품 폴더: 0000.jpg + 1권.zip ~ 5권.zip → 표지 한 장짜리 압축파일을 만들지 않음."""
    r = tmp_path / "lib"
    for v in range(1, 6):
        mk(r, f"[윤승운] 한국사/만화로 보는 한국사 {v}권.zip", b"PK\x05\x06" + b"\x00" * 18)
    mk(r, "[윤승운] 한국사/0000.jpg")
    mk(r, "작품2/cover.jpg"); mk(r, "작품2/01권.cbz", b"PK")
    sr = scan(str(r))
    assert sr.candidates == []
    assert sum("낱장 이미지" in x.reason for x in sr.skipped) == 2


def test_cover_only_and_min_images(tmp_path):
    r = tmp_path / "lib"
    mk(r, "a/cover.jpg"); mk(r, "a/0000.png", PNG)       # 표지 이름뿐
    mk(r, "b/한장.jpg")                                   # 1장
    for i in range(3):
        mk(r, f"c/{i:03d}.jpg")                          # 정상 권
    mk(r, "d/0000.jpg"); mk(r, "d/0001.jpg")             # 0000 표지 + 본문
    sr = scan(str(r))
    assert rel(r, [c.path for c in sr.candidates]) == ["c", "d"]
    reasons = " ".join(x.reason for x in sr.skipped)
    assert "표지로 보이는" in reasons and "1장뿐" in reasons
    assert len(scan(str(r), min_images=1).candidates) == 3


def test_volume_with_bonus_zip_still_compressed(tmp_path):
    """이미지가 충분히 많은 권 폴더는 안에 zip이 있어도 압축(내부 zip은 넣지 않음)."""
    r = tmp_path / "lib"
    for i in range(10):
        mk(r, f"작품/01권/{i:03d}.jpg")
    mk(r, "작품/01권/bonus.zip", b"PK")
    sr = scan(str(r))
    assert rel(r, [c.path for c in sr.candidates]) == ["작품/01권"]


def test_cover_name_rule():
    from ncc.rules import is_cover_name as c
    assert all(c(n) for n in ["0000.jpg", "cover.jpg", "Cover_01.png", "표지.jpg", "folder.jpg", "000.webp"])
    assert not any(c(n) for n in ["001.jpg", "0001.jpg", "page_cover.jpg", "100.jpg", "a.jpg"])


# ---- 남은 원본 정리 (v1.3) -------------------------------------------------
def _zip(path, files, prefix=""):
    with zipfile.ZipFile(path, "w") as z:
        for n, data in files.items():
            z.writestr(prefix + n, data)


def test_leftover_cleanup(tmp_path):
    from ncc.cleanup import find_leftovers, verify_and_delete
    r = tmp_path / "lib"
    imgs = {"001.jpg": JPG, "002.jpg": JPG + b"2"}
    for n, d in imgs.items():
        mk(r, f"작품/01권/{n}", d); mk(r, f"작품/02권/{n}", d); mk(r, f"작품/03권/{n}", d)
        mk(r, f"작품/04권/{n}", d); mk(r, f"작품/05권/{n}", d); mk(r, f"작품/06권/{n}", d)
    mk(r, "작품/01권/Thumbs.db", b"x")
    _zip(r / "작품" / "01권.cbz", imgs)                                # 완전 일치 → 삭제
    _zip(r / "작품" / "02권.zip", {"001.jpg": JPG})                    # 이미지 누락 → 보존
    _zip(r / "작품" / "03권.zip", {"001.jpg": JPG, "002.jpg": JPG + b"X"})  # 내용 다름(크기 같음) → 보존
    mk(r, "작품/04권/메모.txt", b"t"); _zip(r / "작품" / "04권.zip", imgs)  # 다른 파일 → 제외
    _zip(r / "작품" / "05권.zip", imgs, prefix="05권/")               # 내부 상위폴더 → 삭제
    mk(r, "작품/06권.rar", b"Rar!")                                     # 검증 불가 → 대상 아님
    mk(r, "작품/07권/001.jpg")                                          # 압축파일 없음 → 대상 아님
    res = find_leftovers(str(r))
    names = [os.path.basename(c.folder) for c in res.candidates]
    assert names == ["01권", "02권", "03권", "05권"], names
    assert any("04권" in p for p, _ in res.skipped)
    out = {os.path.basename(c.folder): verify_and_delete(c) for c in res.candidates}
    assert out["01권"][0] and not (r / "작품" / "01권").exists()
    assert out["05권"][0] and not (r / "작품" / "05권").exists()
    assert not out["02권"][0] and (r / "작품" / "02권" / "002.jpg").exists()
    assert not out["03권"][0] and (r / "작품" / "03권" / "002.jpg").exists()
    assert (r / "작품" / "04권").exists() and (r / "작품" / "07권").exists()
    assert (r / "작품" / "01권.cbz").exists()


def test_leftover_partial_and_dry_run(tmp_path):
    from ncc.cleanup import find_leftovers, verify_and_delete
    r = tmp_path / "lib"
    mk(r, "작품/01권/002.jpg", JPG + b"2")                 # 일부만 남은 원본 (001은 이미 삭제됨)
    _zip(r / "작품" / "01권.zip", {"001.jpg": JPG, "002.jpg": JPG + b"2"})
    c = find_leftovers(str(r)).candidates[0]
    ok, msg = verify_and_delete(c, dry_run=True)
    assert ok and (r / "작품" / "01권" / "002.jpg").exists()
    ok, msg = verify_and_delete(c)
    assert ok and not (r / "작품" / "01권").exists()
