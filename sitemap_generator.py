#!/usr/bin/env python3
"""
로컬 사이트맵 생성기

사용 예:
    python sitemap_generator.py https://www.example.com/
    python sitemap_generator.py
    python sitemap_generator.py https://www.example.com/ --output output --workers 5

기능:
- 같은 호스트의 내부 링크를 순회
- 추적 파라미터와 중복 URL 정리
- noindex, 외부 canonical, 오류 페이지 제외
- 로그인, 장바구니, 마이페이지 등 기본 제외
- sitemap.xml 생성
- 제외 URL, 오류 URL, 리다이렉트, canonical 불일치 CSV 생성
- URL이 많으면 sitemap index와 여러 sitemap 파일로 자동 분할
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import threading
import time
import urllib.robotparser
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


USER_AGENT = "LocalSitemapGenerator/1.0 (+SEO audit; local desktop tool)"

# URL 정규화 단계에서 제거할 추적/표시용 파라미터
DROP_QUERY_KEYS = {
    "mtn",
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "ttclid",
    "wbraid",
    "gbraid",
    "ref",
    "referrer",
    "source",
    "campaign",
    "n_media",
    "n_query",
    "n_rank",
    "n_ad_group",
    "n_ad",
    "n_keyword_id",
    "n_keyword",
    "n_campaign_type",
    "n_campaign",
    "n_ad_group_type",
    "sort",
    "sortcd",
    "sortby",
    "order",
    "display",
    "listtype",
    "viewtype",
    "viewmode",
    "rows",
    "rowcount",
    "pagesize",
}

DROP_QUERY_PREFIXES = (
    "utm_",
    "_ga",
)

# 크롤링 자체에서 제외할 기본 경로
DEFAULT_EXCLUDE_PATHS = (
    "/admin/",
    "/config/",
    "/module/",
    "/tmp/",
    "/member/",
    "/mypage/",
    "/order/",
    "/oauth/",
    "/auth/",
)

# 링크는 따라가더라도 사이트맵에는 보통 넣지 않는 파라미터
SITEMAP_EXCLUDE_QUERY_KEYS = {
    "page",
    "keyword",
    "search",
    "q",
    "query",
    "noheader",
    "isbest",
    "popup",
    "layer",
    "mode",
    "ajax",
}

# 정적 파일 및 다운로드 파일
ASSET_EXTENSIONS = {
    ".7z", ".avi", ".avif", ".bmp", ".css", ".csv", ".doc", ".docx",
    ".eot", ".exe", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".js",
    ".json", ".map", ".mov", ".mp3", ".mp4", ".mpeg", ".ogg", ".otf",
    ".pdf", ".png", ".ppt", ".pptx", ".rar", ".rss", ".svg", ".tar",
    ".tif", ".tiff", ".ttf", ".txt", ".wav", ".webm", ".webp", ".woff",
    ".woff2", ".xls", ".xlsx", ".xml", ".zip",
}

# 액션, 팝업, 비정상적인 크롤링 확장을 막기 위한 경로 패턴
SKIP_PATH_PATTERNS = (
    re.compile(r"/(?:ajax|popup|layer|download)/", re.I),
    re.compile(r"(?:_ps|_proc)\.php$", re.I),
    re.compile(r"/board/(?:write|modify|delete)\.php$", re.I),
    re.compile(r"/goods/goods_search\.php$", re.I),
)


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int | None
    content_type: str
    text: str | None
    error: str | None


class SitemapCrawler:
    def __init__(
        self,
        start_url: str,
        output_dir: Path,
        *,
        max_pages: int = 10000,
        workers: int = 5,
        timeout: float = 15.0,
        delay: float = 0.05,
        respect_robots: bool = True,
        allow_subdomains: bool = False,
        include_page_urls: bool = False,
        extra_exclude_paths: Iterable[str] = (),
    ) -> None:
        self.original_start_url = start_url
        self.output_dir = output_dir
        self.max_pages = max_pages
        self.workers = max(1, workers)
        self.timeout = timeout
        self.delay = max(0.0, delay)
        self.respect_robots = respect_robots
        self.allow_subdomains = allow_subdomains
        self.include_page_urls = include_page_urls

        self.exclude_paths = tuple(
            self._normalize_exclude_path(path)
            for path in (*DEFAULT_EXCLUDE_PATHS, *extra_exclude_paths)
            if path.strip()
        )

        self.thread_local = threading.local()
        self.start_url = ""
        self.base_scheme = ""
        self.base_host = ""
        self.base_netloc = ""
        self.origin = ""
        self.robots: urllib.robotparser.RobotFileParser | None = None

        self.seen: set[str] = set()
        self.queued: set[str] = set()
        self.sitemap_urls: set[str] = set()

        self.excluded_rows: list[dict[str, str]] = []
        self.broken_rows: list[dict[str, str]] = []
        self.redirect_rows: list[dict[str, str]] = []
        self.canonical_rows: list[dict[str, str]] = []

    @staticmethod
    def _normalize_exclude_path(path: str) -> str:
        path = path.strip()
        if not path.startswith("/"):
            path = "/" + path
        return path.lower()

    def get_session(self) -> requests.Session:
        session = getattr(self.thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
                }
            )
            retry = Retry(
                total=1,
                connect=1,
                read=1,
                status=1,
                backoff_factor=0.3,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET", "HEAD"}),
                raise_on_status=False,
            )
            adapter = HTTPAdapter(
                max_retries=retry,
                pool_connections=self.workers,
                pool_maxsize=self.workers,
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self.thread_local.session = session
        return session

    @staticmethod
    def ensure_scheme(url: str) -> str:
        url = url.strip()
        if not url:
            raise ValueError("사이트 주소가 비어 있습니다.")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        return url

    def initialize(self) -> None:
        candidate = self.ensure_scheme(self.original_start_url)
        print(f"[확인] 시작 URL 연결: {candidate}")

        try:
            response = requests.get(
                candidate,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"시작 URL에 연결할 수 없습니다: {exc}") from exc

        final = self.normalize_url(response.url)
        if not final:
            raise RuntimeError("최종 시작 URL을 정규화할 수 없습니다.")

        parts = urlsplit(final)
        if not parts.hostname:
            raise RuntimeError("시작 URL의 호스트를 확인할 수 없습니다.")

        self.start_url = final
        self.base_scheme = parts.scheme
        self.base_host = parts.hostname.lower()
        self.base_netloc = parts.netloc.lower()
        self.origin = f"{self.base_scheme}://{self.base_netloc}"

        print(f"[확인] 기준 호스트: {self.base_netloc}")
        if response.url != candidate:
            print(f"[확인] 시작 URL 리다이렉트: {candidate} -> {response.url}")

        self.load_robots()

    def load_robots(self) -> None:
        if not self.respect_robots:
            print("[robots] robots.txt 확인을 건너뜁니다.")
            return

        robots_url = urljoin(self.origin + "/", "robots.txt")
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)

        try:
            response = requests.get(
                robots_url,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
            )
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
                self.robots = parser
                print(f"[robots] 적용: {robots_url}")
            else:
                print(f"[robots] 응답 {response.status_code}, 제한 없이 진행")
        except requests.RequestException as exc:
            print(f"[robots] 확인 실패, 제한 없이 진행: {exc}")

    def normalize_url(self, url: str, base_url: str | None = None) -> str | None:
        if base_url:
            url = urljoin(base_url, url)

        url = url.strip()
        if not url:
            return None

        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            return None

        hostname = (parts.hostname or "").lower()
        if not hostname:
            return None

        # 같은 호스트의 HTTP 링크는 시작 URL의 HTTPS/HTTP 기준으로 통일
        if self.base_host and hostname == self.base_host:
            scheme = self.base_scheme

        port = parts.port
        if port and not (
            (scheme == "http" and port == 80)
            or (scheme == "https" and port == 443)
        ):
            netloc = f"{hostname}:{port}"
        else:
            netloc = hostname

        path = re.sub(r"/{2,}", "/", parts.path or "/")
        query_pairs = []

        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            lower_key = key.lower()
            if lower_key in DROP_QUERY_KEYS:
                continue
            if any(lower_key.startswith(prefix) for prefix in DROP_QUERY_PREFIXES):
                continue
            query_pairs.append((key, value))

        query_pairs.sort(key=lambda item: (item[0].lower(), item[1]))
        query = urlencode(query_pairs, doseq=True)

        return urlunsplit((scheme, netloc, path, query, ""))

    def is_internal(self, url: str) -> bool:
        hostname = (urlsplit(url).hostname or "").lower()
        if hostname == self.base_host:
            return True
        if self.allow_subdomains and hostname.endswith("." + self.base_host):
            return True
        return False

    def path_exclusion_reason(self, url: str) -> str | None:
        parts = urlsplit(url)
        path_lower = parts.path.lower()

        for prefix in self.exclude_paths:
            if path_lower.startswith(prefix):
                return f"제외 경로: {prefix}"

        suffix = Path(path_lower).suffix
        if suffix in ASSET_EXTENSIONS:
            return f"정적/다운로드 파일: {suffix}"

        for pattern in SKIP_PATH_PATTERNS:
            if pattern.search(path_lower):
                return f"제외 패턴: {pattern.pattern}"

        query = dict(parse_qsl(parts.query, keep_blank_values=True))

        # 빈 상품번호와 같이 명백히 잘못된 동적 URL
        if path_lower.endswith("/goods/goods_view.php"):
            goods_no = query.get("goodsNo")
            if goods_no is None:
                goods_no = query.get("goodsno")
            if not goods_no:
                return "상품번호(goodsNo)가 비어 있음"

        return None

    def sitemap_exclusion_reason(self, url: str) -> str | None:
        parts = urlsplit(url)
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)

        for key, value in query_pairs:
            lower_key = key.lower()
            if lower_key == "page" and self.include_page_urls:
                continue
            if lower_key in SITEMAP_EXCLUDE_QUERY_KEYS:
                return f"사이트맵 제외 파라미터: {key}"
            if value == "" and lower_key in {"goodsno", "catecd", "sno"}:
                return f"빈 필수 파라미터: {key}"

        return None

    def robots_allows(self, url: str) -> bool:
        if not self.robots:
            return True
        return self.robots.can_fetch(USER_AGENT, url)

    def fetch(self, url: str) -> FetchResult:
        if not self.robots_allows(url):
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=None,
                content_type="",
                text=None,
                error="robots.txt 차단",
            )

        try:
            response = self.get_session().get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
            )
            if self.delay:
                time.sleep(self.delay)

            content_type = response.headers.get("Content-Type", "").lower()
            text = response.text if "html" in content_type else None

            return FetchResult(
                requested_url=url,
                final_url=response.url,
                status_code=response.status_code,
                content_type=content_type,
                text=text,
                error=None,
            )
        except requests.RequestException as exc:
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=None,
                content_type="",
                text=None,
                error=str(exc),
            )

    @staticmethod
    def get_meta_robots(soup: BeautifulSoup) -> str:
        values: list[str] = []
        for name in ("robots", "googlebot", "yeti"):
            tag = soup.find("meta", attrs={"name": re.compile(f"^{name}$", re.I)})
            if tag and tag.get("content"):
                values.append(str(tag.get("content")).lower())
        return ",".join(values)

    def extract_canonical(self, soup: BeautifulSoup, page_url: str) -> str | None:
        tag = soup.select_one('link[rel~="canonical" i]')
        if not tag or not tag.get("href"):
            return None
        return self.normalize_url(str(tag.get("href")), page_url)

    def extract_links(self, soup: BeautifulSoup, page_url: str) -> set[str]:
        links: set[str] = set()

        for tag in soup.find_all(["a", "area"], href=True):
            href = str(tag.get("href", "")).strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue

            normalized = self.normalize_url(href, page_url)
            if not normalized or not self.is_internal(normalized):
                continue

            reason = self.path_exclusion_reason(normalized)
            if reason:
                self.excluded_rows.append(
                    {
                        "source_url": page_url,
                        "url": normalized,
                        "reason": reason,
                    }
                )
                continue

            links.add(normalized)

        return links

    def process_result(self, result: FetchResult) -> set[str]:
        requested = result.requested_url

        if result.error:
            if result.error == "robots.txt 차단":
                self.excluded_rows.append(
                    {
                        "source_url": requested,
                        "url": requested,
                        "reason": result.error,
                    }
                )
            else:
                self.broken_rows.append(
                    {
                        "url": requested,
                        "status": "",
                        "error": result.error,
                    }
                )
            return set()

        final_url = self.normalize_url(result.final_url)
        if not final_url:
            self.broken_rows.append(
                {
                    "url": requested,
                    "status": str(result.status_code or ""),
                    "error": "최종 URL 정규화 실패",
                }
            )
            return set()

        if requested != final_url:
            self.redirect_rows.append(
                {
                    "requested_url": requested,
                    "final_url": final_url,
                    "status": str(result.status_code or ""),
                }
            )

        if not self.is_internal(final_url):
            self.excluded_rows.append(
                {
                    "source_url": requested,
                    "url": final_url,
                    "reason": "외부 호스트로 리다이렉트",
                }
            )
            return set()

        if result.status_code != 200:
            self.broken_rows.append(
                {
                    "url": requested,
                    "status": str(result.status_code or ""),
                    "error": "HTTP 200 아님",
                }
            )
            return set()

        if result.text is None:
            self.excluded_rows.append(
                {
                    "source_url": requested,
                    "url": final_url,
                    "reason": f"HTML 아님: {result.content_type}",
                }
            )
            return set()

        soup = BeautifulSoup(result.text, "html.parser")
        discovered = self.extract_links(soup, final_url)

        meta_robots = self.get_meta_robots(soup)
        if "noindex" in meta_robots:
            self.excluded_rows.append(
                {
                    "source_url": requested,
                    "url": final_url,
                    "reason": f"meta robots noindex: {meta_robots}",
                }
            )
            return discovered

        sitemap_reason = self.sitemap_exclusion_reason(final_url)
        if sitemap_reason:
            self.excluded_rows.append(
                {
                    "source_url": requested,
                    "url": final_url,
                    "reason": sitemap_reason,
                }
            )
            return discovered

        canonical = self.extract_canonical(soup, final_url)
        if canonical:
            if not self.is_internal(canonical):
                self.excluded_rows.append(
                    {
                        "source_url": requested,
                        "url": final_url,
                        "reason": f"외부 canonical: {canonical}",
                    }
                )
                return discovered

            canonical_reason = self.path_exclusion_reason(canonical)
            if canonical_reason:
                self.excluded_rows.append(
                    {
                        "source_url": requested,
                        "url": final_url,
                        "reason": f"canonical 제외: {canonical_reason}",
                    }
                )
                return discovered

            if canonical != final_url:
                self.canonical_rows.append(
                    {
                        "url": final_url,
                        "canonical": canonical,
                    }
                )
                discovered.add(canonical)
                return discovered

            self.sitemap_urls.add(canonical)
        else:
            self.sitemap_urls.add(final_url)

        return discovered

    def crawl(self) -> None:
        self.initialize()

        queue: deque[str] = deque([self.start_url])
        self.queued.add(self.start_url)

        print(
            f"[시작] 최대 {self.max_pages:,}개 페이지, "
            f"동시 요청 {self.workers}개"
        )

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            while queue and len(self.seen) < self.max_pages:
                batch: list[str] = []

                while (
                    queue
                    and len(batch) < self.workers
                    and len(self.seen) + len(batch) < self.max_pages
                ):
                    url = queue.popleft()
                    self.queued.discard(url)

                    if url in self.seen:
                        continue

                    reason = self.path_exclusion_reason(url)
                    if reason:
                        self.excluded_rows.append(
                            {
                                "source_url": "",
                                "url": url,
                                "reason": reason,
                            }
                        )
                        self.seen.add(url)
                        continue

                    self.seen.add(url)
                    batch.append(url)

                if not batch:
                    continue

                futures = {
                    executor.submit(self.fetch, url): url
                    for url in batch
                }

                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # 예상 밖의 개별 페이지 오류
                        self.broken_rows.append(
                            {
                                "url": url,
                                "status": "",
                                "error": f"처리 예외: {exc}",
                            }
                        )
                        continue

                    discovered = self.process_result(result)

                    for link in discovered:
                        if link not in self.seen and link not in self.queued:
                            queue.append(link)
                            self.queued.add(link)

                if len(self.seen) % 25 < self.workers:
                    print(
                        f"[진행] 방문 {len(self.seen):,} / "
                        f"대기 {len(queue):,} / "
                        f"사이트맵 {len(self.sitemap_urls):,}"
                    )

        if queue:
            print(
                f"[주의] 최대 페이지 제한 {self.max_pages:,}개에 도달했습니다. "
                f"아직 대기 URL {len(queue):,}개가 남아 있습니다."
            )

    @staticmethod
    def indent_xml(tree: ET.ElementTree) -> None:
        try:
            ET.indent(tree, space="  ")
        except AttributeError:
            pass

    def write_sitemap_file(self, urls: list[str], path: Path) -> None:
        namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
        ET.register_namespace("", namespace)
        root = ET.Element(f"{{{namespace}}}urlset")

        for url in urls:
            url_node = ET.SubElement(root, f"{{{namespace}}}url")
            loc = ET.SubElement(url_node, f"{{{namespace}}}loc")
            loc.text = url

        tree = ET.ElementTree(root)
        self.indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)

    def write_sitemap_index(self, filenames: list[str], path: Path) -> None:
        namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
        ET.register_namespace("", namespace)
        root = ET.Element(f"{{{namespace}}}sitemapindex")

        for filename in filenames:
            node = ET.SubElement(root, f"{{{namespace}}}sitemap")
            loc = ET.SubElement(node, f"{{{namespace}}}loc")
            loc.text = urljoin(self.origin + "/", filename)

        tree = ET.ElementTree(root)
        self.indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def write_outputs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

        urls = sorted(self.sitemap_urls)
        chunk_size = 45000

        if len(urls) <= chunk_size:
            self.write_sitemap_file(urls, self.output_dir / "sitemap.xml")
            sitemap_files = ["sitemap.xml"]
        else:
            sitemap_files = []
            for index in range(0, len(urls), chunk_size):
                chunk = urls[index:index + chunk_size]
                filename = f"sitemap-{index // chunk_size + 1}.xml"
                self.write_sitemap_file(chunk, self.output_dir / filename)
                sitemap_files.append(filename)

            # 루트에 올릴 파일 이름을 sitemap.xml로 유지
            self.write_sitemap_index(sitemap_files, self.output_dir / "sitemap.xml")

        self.write_csv(
            self.output_dir / "excluded_urls.csv",
            self.excluded_rows,
            ["source_url", "url", "reason"],
        )
        self.write_csv(
            self.output_dir / "broken_urls.csv",
            self.broken_rows,
            ["url", "status", "error"],
        )
        self.write_csv(
            self.output_dir / "redirect_urls.csv",
            self.redirect_rows,
            ["requested_url", "final_url", "status"],
        )
        self.write_csv(
            self.output_dir / "canonical_mismatch.csv",
            self.canonical_rows,
            ["url", "canonical"],
        )

        summary = (
            f"시작 URL: {self.start_url}\n"
            f"기준 호스트: {self.base_netloc}\n"
            f"방문 URL: {len(self.seen):,}\n"
            f"사이트맵 URL: {len(urls):,}\n"
            f"제외 기록: {len(self.excluded_rows):,}\n"
            f"오류 URL: {len(self.broken_rows):,}\n"
            f"리다이렉트: {len(self.redirect_rows):,}\n"
            f"canonical 불일치: {len(self.canonical_rows):,}\n"
            f"생성 파일: {', '.join(sitemap_files)}\n"
        )
        (self.output_dir / "summary.txt").write_text(summary, encoding="utf-8")

        print("\n[완료]")
        print(summary.rstrip())
        print(f"출력 폴더: {self.output_dir.resolve()}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="사이트 내부 링크를 크롤링하여 sitemap.xml을 생성합니다."
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="시작 사이트 주소. 생략하면 실행 후 입력받습니다.",
    )
    parser.add_argument(
        "--output",
        default="output",
        help="결과 저장 폴더. 기본값: output",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10000,
        help="최대 방문 페이지 수. 기본값: 10000",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="동시 요청 수. 기본값: 5",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="페이지별 요청 제한 시간(초). 기본값: 15",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.05,
        help="각 요청 후 대기 시간(초). 기본값: 0.05",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="robots.txt 규칙을 무시합니다.",
    )
    parser.add_argument(
        "--allow-subdomains",
        action="store_true",
        help="시작 호스트의 하위 도메인도 내부 URL로 취급합니다.",
    )
    parser.add_argument(
        "--include-page-urls",
        action="store_true",
        help="page 파라미터가 있는 페이지네이션 URL도 사이트맵에 포함합니다.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATH",
        help="추가 제외 경로. 여러 번 사용할 수 있습니다. 예: --exclude /event/",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    url = args.url or input("사이트 주소를 입력하세요: ").strip()

    if not url:
        print("사이트 주소가 입력되지 않았습니다.", file=sys.stderr)
        return 2

    crawler = SitemapCrawler(
        start_url=url,
        output_dir=Path(args.output),
        max_pages=max(1, args.max_pages),
        workers=max(1, args.workers),
        timeout=max(1.0, args.timeout),
        delay=max(0.0, args.delay),
        respect_robots=not args.ignore_robots,
        allow_subdomains=args.allow_subdomains,
        include_page_urls=args.include_page_urls,
        extra_exclude_paths=args.exclude,
    )

    try:
        crawler.crawl()
        crawler.write_outputs()
    except KeyboardInterrupt:
        print("\n사용자가 작업을 중단했습니다.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\n오류: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
