# Local Sitemap Generator

웹사이트의 내부 링크를 크롤링하여 `sitemap.xml`과 SEO 점검용 CSV 보고서를 생성하는 Python 도구입니다.

Developed by raremore at RE:CODER Labs.

## 주요 기능

- 동일 호스트의 내부 링크 수집
- 추적 파라미터 제거 및 중복 URL 정리
- meta robots와 `X-Robots-Tag`의 `noindex` 페이지 제외
- 외부 canonical과 오류 페이지 제외
- 관리자, 회원, 주문 및 정적 파일 경로 자동 제외
- 실제 HTTP 상태 코드가 포함된 리다이렉트 이력 CSV 생성
- 제외 URL, 오류 URL, canonical 불일치 CSV 생성
- URL 25,000개 또는 파일 용량 10MB에 도달하면 sitemap 자동 분할
- `robots.txt`와 `Crawl-delay` 규칙 적용

## 요구 사항

- Windows
- [PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/install-powershell-on-windows) 권장 또는 Windows PowerShell 5.1
- [Python 3.10](https://www.python.org) 이상
- 인터넷 연결

## 가장 간단한 실행 방법

PowerShell에서 프로젝트 폴더로 이동한 다음 실행합니다.

```powershell
.\pw_run.ps1
```

실행하면 다음과 같이 사이트 주소를 입력받습니다.

```text
사이트 주소를 입력하세요: https://www.example.com/
```

`pw_run.ps1`은 다음 작업을 자동으로 처리합니다.

1. `.venv` 가상환경이 없으면 생성
2. 필요한 Python 패키지가 없으면 `pip`를 최신 버전으로 업그레이드
3. `requirements.txt`에 정의된 Python 패키지 설치
4. 사이트맵 생성기 실행

PowerShell 실행 정책 때문에 스크립트 실행이 차단되면 다음 명령을 사용합니다.

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\pw_run.ps1
```

## Python으로 직접 실행

Python 파일만 직접 실행해도 사이트 주소를 입력받을 수 있습니다.

```powershell
python sitemap_generator.py
```

주소를 명령어에 바로 전달할 수도 있습니다.

```powershell
python sitemap_generator.py https://www.example.com/
```

처음 직접 실행하는 경우 필요한 패키지를 먼저 설치합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe sitemap_generator.py
```

## 자주 사용하는 옵션

```powershell
# 결과 폴더 변경
python sitemap_generator.py https://www.example.com/ --output result

# 최대 방문 페이지와 동시 요청 수 변경
python sitemap_generator.py https://www.example.com/ --max-pages 10000 --workers 5

# 전체 HTTP 요청 사이의 최소 간격 변경
python sitemap_generator.py https://www.example.com/ --delay 0.2

# 추가 제외 경로
python sitemap_generator.py https://www.example.com/ --exclude /event/ --exclude /promotion/

# robots.txt 규칙 무시
python sitemap_generator.py https://www.example.com/ --ignore-robots

# page 파라미터가 있는 페이지네이션 URL도 사이트맵에 포함
python sitemap_generator.py https://www.example.com/ --include-page-urls
```

전체 옵션은 다음 명령으로 확인할 수 있습니다.

```powershell
python sitemap_generator.py --help
```

## 생성 파일

결과는 기본적으로 `output` 폴더에 저장됩니다.

| 파일 | 내용 |
| --- | --- |
| `sitemap.xml` | 사이트맵 또는 분할 사이트맵 index |
| `sitemap-1.xml` 등 | URL이 많을 때 생성되는 분할 사이트맵 |
| `excluded_urls.csv` | 제외된 URL과 제외 이유 |
| `broken_urls.csv` | 요청 실패 및 비정상 HTTP 응답 |
| `redirect_urls.csv` | 리다이렉트된 URL |
| `canonical_mismatch.csv` | 현재 URL과 canonical이 다른 페이지 |
| `summary.txt` | 방문 및 생성 결과 요약 |

## 기본 자동 제외

- `/admin/`, `/config/`, `/module/`, `/tmp/`
- `/member/`, `/mypage/`, `/order/`, 인증 관련 경로
- `/logout`, `/logout.cm`, `/logout.php` 등의 로그아웃 액션
- 이미지, 동영상, 문서, 압축 파일 등의 정적·다운로드 파일
- 상품번호가 비어 있는 상품 URL
- `noindex` 페이지
- 외부 canonical 페이지
- 오류 응답 페이지
- UTM, `mtn`, `gclid` 등의 추적 파라미터
- `page`, `keyword`, `search` 등의 검색·페이지네이션 URL

검색·페이지네이션 URL은 사이트맵에서는 제외하지만, 내부 링크를 발견하기 위해 크롤링할 수 있습니다.

## 주의사항

- JavaScript로만 노출되는 링크는 수집되지 않을 수 있습니다.
- 로그인 후에만 보이는 페이지는 수집하지 않습니다.
- 한 sitemap에는 하나의 호스트만 포함되므로 하위 도메인은 별도로 실행해야 합니다.
- 사이트 서버에 부담을 주지 않도록 동시 요청 수를 과도하게 높이지 마세요.
- canonical이 현재 URL과 다르면 현재 URL은 사이트맵에서 제외하고 `canonical_mismatch.csv`에 기록합니다.
- 생성한 `sitemap.xml`을 서버 루트에 업로드한 뒤 `robots.txt`의 Sitemap 주소와 일치하는지 확인하세요.

## License

This project is licensed under the [MIT License](LICENSE).
