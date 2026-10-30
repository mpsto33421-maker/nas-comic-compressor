# NAS Comic Compressor

Windows x64용 만화 이미지 폴더 ZIP 압축 GUI입니다. 설치된 반디집의 `bz.exe`를 사용하며 Python 설치 없이 단일 EXE로 실행합니다.

## 다운로드

이 저장소의 **Releases**에서 `NAS_Comic_Compressor_1.1.2.exe`를 다운로드합니다. 비공개 저장소의 파일을 받으려면 소유 계정으로 GitHub에 로그인해야 합니다.

## 사용

1. Windows PC에 반디집을 설치합니다.
2. EXE를 실행하고 작업 폴더와 `bz.exe` 경로를 선택합니다.
3. 압축 대상을 검색하고 체크된 항목을 압축합니다.
4. ZIP은 이미지 폴더 바로 옆에 생성됩니다. 이미 같은 이름의 압축파일이 있으면 건너뜁니다.

원본 보존이 기본값입니다. 원본 삭제는 경고를 확인하고 옵션을 켠 새 압축 작업에만 적용하며, 목록·무결성·해시·파일 잠금 검증을 통과한 이미지 전용 폴더만 정리합니다. 동기화용 추가 쓰기 열기가 거부되는 경우에는 ZIP을 확정하더라도 원본을 보존합니다.

Windows 11 x64에서 개발·실행 테스트했습니다. 다른 NAS 서버의 접근 권한 및 파일 잠금 동작은 해당 장치에서 확인해야 합니다.

## 빌드

`app/`에 전체 Python 소스와 생성된 임시 데이터만 사용하는 자동 테스트가 있습니다. GitHub Actions가 Windows 환경에서 테스트하고 EXE를 빌드해 릴리스에 첨부합니다. 반디집이 없는 빌드 환경에서는 반디집 통합 테스트를 건너뜁니다.

GitHub에서 다시 빌드한 EXE는 로컬 빌드와 바이트 단위로 같지 않을 수 있습니다. 각 릴리스에 해당 빌드의 SHA-256 체크섬을 제공합니다.

Third-party notices and license texts are included. Real NAS images, user logs, local paths, and user test manifests are not included.
