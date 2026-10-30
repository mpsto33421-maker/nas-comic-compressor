"""테스트용 가짜 bz.exe (반디집 콘솔 도구의 c / a / t 명령을 흉내냄).

환경변수로 오류 주입:
  FAKE_BZ_FAIL=create|test|badname|extra   압축 실패 / 검사 실패 / 이름 깨짐 / 엉뚱한 파일 추가
  FAKE_BZ_SLEEP=<초>                       파일당 지연 (취소 테스트)
"""
import os
import sys
import time
import zipfile


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # 실제 bz의 -consolemode:utf8 과 동일
    cmd = argv[0]
    rest = argv[1:]
    sw = [a for a in rest if a.startswith("-")
          and not a.startswith((".\\", "./"))]
    pos = [a for a in rest if a not in sw]
    fail = os.environ.get("FAKE_BZ_FAIL", "")
    delay = float(os.environ.get("FAKE_BZ_SLEEP", "0") or 0)
    print("bz 7.46(fake) - Bandizip console tool.", flush=True)
    if cmd in ("c", "a"):
        archive, files = pos[0], pos[1:]
        if fail == "create":
            print("Error: cannot open file", flush=True)
            return 3
        level = 0
        for s in sw:
            if s.startswith("-l:"):
                level = int(s[3:])
        comp = zipfile.ZIP_STORED if level == 0 else zipfile.ZIP_DEFLATED
        mode = "w" if cmd == "c" else "a"
        print(("Creating" if cmd == "c" else "Updating") + f" archive: {archive}", flush=True)
        with zipfile.ZipFile(archive, mode, compression=comp) as z:
            for f in files:
                name = os.path.basename(f.replace("\\", "/"))
                if fail == "badname":
                    name = name.encode("utf-8").decode("cp437")
                z.write(f.replace("\\", os.sep) if os.sep == "/" else f, arcname=name)
                print(f"Compressing: {name}", flush=True)
                if delay:
                    time.sleep(delay)
            if fail == "extra" and cmd == "c":
                z.writestr("evil.txt", b"x")
        return 0
    if cmd == "t":
        archive = pos[0]
        if fail == "test":
            print("Error: CRC failed", flush=True)
            return 2
        with zipfile.ZipFile(archive) as z:
            bad = z.testzip()
        if bad:
            print(f"CRC error: {bad}")
            return 2
        print("All OK", flush=True)
        return 0
    print("unknown command")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
