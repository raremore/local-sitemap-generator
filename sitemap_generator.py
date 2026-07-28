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
from xml.sax.saxutils import escape as xml_escape
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


USER_AGENT = "LocalSitemapGenerator/1.0 (+SEO audit; local desktop tool)"
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 10
MAX_SITEMAP_URLS = 10000
SITEMAP_CHUNK_BYTES = 10 * 1024 * 1024
MAX_SITEMAP_BYTES = 50 * 1024 * 1024

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
    re.compile(r"/logout(?:\.[a-z0-9]+)?/?$", re.I),
)


@dataclass(slots=True)
class RedirectHop:
    requested_url: str
    final_url: str
    status_code: int


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int | None
    content_type: str
    text: str | None
    error: str | None
    x_robots_tag: str = ""
    redirects: tuple[RedirectHop, ...] = ()


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
        include_page_urls: bool = False,
        extra_exclude_paths: Iterable[str] = (),
    ) -> None:
        self.original_start_url = start_url
        self.output_dir = output_dir
        self.max_pages = max_pages
        self.workers = max(1, workers)
        self.timeout = timeout
        self.configured_delay = max(0.0, delay)
        self.request_delay = self.configured_delay
        self.respect_robots = respect_robots
        self.include_page_urls = include_page_urls

        self.exclude_paths = tuple(
            self._normalize_exclude_path(path)
            for path in (*DEFAULT_EXCLUDE_PATHS, *extra_exclude_paths)
            if path.strip()
        )

        self.thread_local = threading.local()
        self.rate_lock = threading.Lock()
        self.next_request_at = 0.0
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
        path = re.sub(r"/{2,}", "/", path)
        if path != "/":
            path = path.rstrip("/")
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

    def set_base_url(self, url: str) -> None:
        parts = urlsplit(url)
        if not parts.hostname:
            raise RuntimeError("URL의 호스트를 확인할 수 없습니다.")

        self.base_scheme = parts.scheme
        self.base_host = parts.hostname.lower()
        self.base_netloc = parts.netloc.lower()
        self.origin = f"{self.base_scheme}://{self.base_netloc}"

    def wait_for_request_slot(self) -> None:
        with self.rate_lock:
            now = time.monotonic()
            wait_seconds = max(0.0, self.next_request_at - now)
            if wait_seconds:
                time.sleep(wait_seconds)

            request_started_at = time.monotonic()
            self.next_request_at = request_started_at + self.request_delay

    def initialize(self) -> FetchResult:
        candidate = self.normalize_url(
            self.ensure_scheme(self.original_start_url),
            use_base_scheme=False,
        )
        if not candidate:
            raise RuntimeError("시작 URL을 정규화할 수 없습니다.")

        print(f"[확인] 시작 URL 연결: {candidate}")
        current_url = candidate
        visited_redirects: set[str] = set()
        redirects: list[RedirectHop] = []

        for _ in range(MAX_REDIRECTS + 1):
            if current_url in visited_redirects:
                raise RuntimeError(f"시작 URL 리다이렉트 순환 감지: {current_url}")
            visited_redirects.add(current_url)

            current_parts = urlsplit(current_url)
            current_origin = f"{current_parts.scheme}://{current_parts.netloc.lower()}"
            if current_origin != self.origin:
                self.set_base_url(current_url)
                self.load_robots()

            if not self.robots_allows(current_url):
                raise RuntimeError(f"시작 URL이 robots.txt에 의해 차단됨: {current_url}")

            try:
                self.wait_for_request_slot()
                response = self.get_session().get(
                    current_url,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise RuntimeError(f"시작 URL에 연결할 수 없습니다: {exc}") from exc

            location = response.headers.get("Location")
            if response.status_code in REDIRECT_STATUS_CODES and location:
                next_url = self.normalize_url(
                    location,
                    current_url,
                    use_base_scheme=False,
                )
                if not next_url:
                    raise RuntimeError(
                        f"시작 URL 리다이렉트 대상을 정규화할 수 없습니다: {location}"
                    )

                redirects.append(
                    RedirectHop(
                        requested_url=current_url,
                        final_url=next_url,
                        status_code=response.status_code,
                    )
                )
                print(
                    f"[확인] 시작 URL 리다이렉트: "
                    f"{current_url} -> {next_url} ({response.status_code})"
                )
                current_url = next_url
                continue

            if response.status_code != 200:
                raise RuntimeError(
                    f"시작 URL 응답이 HTTP 200이 아닙니다: "
                    f"{response.status_code} {current_url}"
                )

            final_url = self.normalize_url(
                response.url,
                use_base_scheme=False,
            )
            if not final_url:
                raise RuntimeError("최종 시작 URL을 정규화할 수 없습니다.")

            self.set_base_url(final_url)
            self.start_url = final_url
            content_type = response.headers.get("Content-Type", "").lower()
            text = response.text if "html" in content_type else None

            print(f"[확인] 기준 호스트: {self.base_netloc}")
            return FetchResult(
                requested_url=candidate,
                final_url=final_url,
                status_code=response.status_code,
                content_type=content_type,
                text=text,
                error=None,
                x_robots_tag=response.headers.get("X-Robots-Tag", ""),
                redirects=tuple(redirects),
            )

        raise RuntimeError(
            f"시작 URL 리다이렉트가 {MAX_REDIRECTS}회를 초과했습니다."
        )

    def load_robots(self) -> None:
        self.robots = None
        self.request_delay = self.configured_delay

        if not self.respect_robots:
            print("[robots] robots.txt 확인을 건너뜁니다.")
            return

        robots_url = urljoin(self.origin + "/", "robots.txt")
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)

        try:
            self.wait_for_request_slot()
            response = requests.get(
                robots_url,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
            )
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
                self.robots = parser
                print(f"[robots] 적용: {robots_url}")

                crawl_delay = parser.crawl_delay(USER_AGENT)
                if crawl_delay is None:
                    crawl_delay = parser.crawl_delay("*")
                if crawl_delay is not None:
                    self.request_delay = max(
                        self.configured_delay,
                        float(crawl_delay),
                    )
                    with self.rate_lock:
                        self.next_request_at = max(
                            self.next_request_at,
                            time.monotonic() + self.request_delay,
                        )
                    print(
                        f"[robots] 요청 간격 적용: "
                        f"{self.request_delay:g}초"
                    )
            else:
                print(f"[robots] 응답 {response.status_code}, 제한 없이 진행")
        except requests.RequestException as exc:
            print(f"[robots] 확인 실패, 제한 없이 진행: {exc}")

    def normalize_url(
        self,
        url: str,
        base_url: str | None = None,
        *,
        use_base_scheme: bool = True,
    ) -> str | None:
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
        if use_base_scheme and self.base_host and hostname == self.base_host:
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
        parts = urlsplit(url)
        return (
            parts.scheme.lower() == self.base_scheme
            and (parts.hostname or "").lower() == self.base_host
            and parts.netloc.lower() == self.base_netloc
        )

    def path_exclusion_reason(self, url: str) -> str | None:
        parts = urlsplit(url)
        path_lower = parts.path.lower()

        for excluded_path in self.exclude_paths:
            if (
                excluded_path == "/"
                or path_lower == excluded_path
                or path_lower.startswith(excluded_path + "/")
            ):
                return f"제외 경로: {excluded_path}"

        suffix = Path(path_lower).suffix
        if suffix in ASSET_EXTENSIONS:
            return f"정적/다운로드 파일: {suffix}"

        for pattern in SKIP_PATH_PATTERNS:
            if pattern.search(path_lower):
                return f"제외 패턴: {pattern.pattern}"

        query = {
            key.lower(): value
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        }

        # 빈 상품번호와 같이 명백히 잘못된 동적 URL
        if path_lower.endswith("/goods/goods_view.php"):
            goods_no = query.get("goodsno")
            if not goods_no:
                return "상품번호(goodsNo)가 비어 있음"

        return None

    def sitemap_exclusion_reason(self, url: str) -> str | None:
        if len(url) >= 2048:
            return "사이트맵 URL 길이 제한(2,048자) 초과"

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
        current_url = url
        visited_redirects: set[str] = set()
        redirects: list[RedirectHop] = []

        for _ in range(MAX_REDIRECTS + 1):
            if current_url in visited_redirects:
                return FetchResult(
                    requested_url=url,
                    final_url=current_url,
                    status_code=None,
                    content_type="",
                    text=None,
                    error="리다이렉트 순환 감지",
                    redirects=tuple(redirects),
                )
            visited_redirects.add(current_url)

            if not self.robots_allows(current_url):
                return FetchResult(
                    requested_url=url,
                    final_url=current_url,
                    status_code=None,
                    content_type="",
                    text=None,
                    error="robots.txt 차단",
                    redirects=tuple(redirects),
                )

            try:
                self.wait_for_request_slot()
                response = self.get_session().get(
                    current_url,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                return FetchResult(
                    requested_url=url,
                    final_url=current_url,
                    status_code=None,
                    content_type="",
                    text=None,
                    error=str(exc),
                    redirects=tuple(redirects),
                )

            location = response.headers.get("Location")
            if response.status_code in REDIRECT_STATUS_CODES and location:
                next_url = self.normalize_url(
                    location,
                    current_url,
                    use_base_scheme=False,
                )
                if not next_url:
                    return FetchResult(
                        requested_url=url,
                        final_url=current_url,
                        status_code=response.status_code,
                        content_type="",
                        text=None,
                        error=f"리다이렉트 대상 정규화 실패: {location}",
                        redirects=tuple(redirects),
                    )

                redirects.append(
                    RedirectHop(
                        requested_url=current_url,
                        final_url=next_url,
                        status_code=response.status_code,
                    )
                )
                current_url = next_url

                if (
                    not self.is_internal(current_url)
                    or self.path_exclusion_reason(current_url)
                ):
                    return FetchResult(
                        requested_url=url,
                        final_url=current_url,
                        status_code=response.status_code,
                        content_type="",
                        text=None,
                        error=None,
                        redirects=tuple(redirects),
                    )
                continue

            content_type = response.headers.get("Content-Type", "").lower()
            text = response.text if "html" in content_type else None
            return FetchResult(
                requested_url=url,
                final_url=response.url,
                status_code=response.status_code,
                content_type=content_type,
                text=text,
                error=None,
                x_robots_tag=response.headers.get("X-Robots-Tag", ""),
                redirects=tuple(redirects),
            )

        return FetchResult(
            requested_url=url,
            final_url=current_url,
            status_code=None,
            content_type="",
            text=None,
            error=f"리다이렉트가 {MAX_REDIRECTS}회를 초과함",
            redirects=tuple(redirects),
        )

    @staticmethod
    def get_meta_robots(soup: BeautifulSoup) -> str:
        values: list[str] = []
        supported_names = {"robots", "googlebot", "yeti"}

        for tag in soup.select("meta[name][content]"):
            name = str(tag.get("name", "")).strip().lower()
            if name in supported_names:
                values.append(str(tag.get("content", "")).strip().lower())

        return ",".join(values)

    @staticmethod
    def has_noindex(*directive_values: str) -> bool:
        combined = ",".join(directive_values).lower()
        return bool(
            re.search(r"(?:^|[\s,])(?:noindex|none)(?=$|[\s,])", combined)
        )

    def extract_canonical(self, soup: BeautifulSoup, page_url: str) -> str | None:
        tag = soup.select_one('link[rel~="canonical" i]')
        if not tag or not tag.get("href"):
            return None
        return self.normalize_url(
            str(tag.get("href")),
            page_url,
            use_base_scheme=False,
        )

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

        for redirect in result.redirects:
            self.redirect_rows.append(
                {
                    "requested_url": redirect.requested_url,
                    "final_url": redirect.final_url,
                    "status": str(redirect.status_code),
                }
            )

        if result.error:
            if result.error == "robots.txt 차단":
                self.excluded_rows.append(
                    {
                        "source_url": requested,
                        "url": result.final_url,
                        "reason": result.error,
                    }
                )
            else:
                self.broken_rows.append(
                    {
                        "url": result.final_url,
                        "status": "",
                        "error": result.error,
                    }
                )
            return set()

        final_url = self.normalize_url(
            result.final_url,
            use_base_scheme=False,
        )
        if not final_url:
            self.broken_rows.append(
                {
                    "url": requested,
                    "status": str(result.status_code or ""),
                    "error": "최종 URL 정규화 실패",
                }
            )
            return set()

        if not self.is_internal(final_url):
            self.excluded_rows.append(
                {
                    "source_url": requested,
                    "url": final_url,
                    "reason": "외부 호스트로 리다이렉트",
                }
            )
            return set()

        final_path_reason = self.path_exclusion_reason(final_url)
        if final_path_reason:
            self.excluded_rows.append(
                {
                    "source_url": requested,
                    "url": final_url,
                    "reason": final_path_reason,
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
        if self.has_noindex(meta_robots, result.x_robots_tag):
            robots_directives = ", ".join(
                value
                for value in (meta_robots, result.x_robots_tag.lower())
                if value
            )
            self.excluded_rows.append(
                {
                    "source_url": requested,
                    "url": final_url,
                    "reason": f"robots noindex: {robots_directives}",
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
        initial_result = self.initialize()
        self.seen.add(self.start_url)
        initial_links = self.process_result(initial_result)
        queue: deque[str] = deque(sorted(initial_links))
        self.queued.update(initial_links)

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

                    for link in sorted(discovered):
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
        if path.stat().st_size > MAX_SITEMAP_BYTES:
            raise RuntimeError(
                f"사이트맵 파일이 50MB 제한을 초과했습니다: {path.name}"
            )

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
        if path.stat().st_size > MAX_SITEMAP_BYTES:
            raise RuntimeError(
                f"사이트맵 index가 50MB 제한을 초과했습니다: {path.name}"
            )

    @staticmethod
    def split_sitemap_urls(urls: list[str]) -> list[list[str]]:
        chunks: list[list[str]] = [[]]
        chunk_bytes = 512

        for url in urls:
            escaped_url = xml_escape(url)
            entry_bytes = len(
                (
                    "  <url>\n"
                    f"    <loc>{escaped_url}</loc>\n"
                    "  </url>\n"
                ).encode("utf-8")
            )
            current_chunk = chunks[-1]

            if current_chunk and (
                len(current_chunk) >= MAX_SITEMAP_URLS
                or chunk_bytes + entry_bytes > SITEMAP_CHUNK_BYTES
            ):
                chunks.append([])
                current_chunk = chunks[-1]
                chunk_bytes = 512

            current_chunk.append(url)
            chunk_bytes += entry_bytes

        return chunks

    @staticmethod
    def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def write_outputs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

        urls = sorted(self.sitemap_urls)
        chunks = self.split_sitemap_urls(urls)

        if len(chunks) == 1:
            self.write_sitemap_file(chunks[0], self.output_dir / "sitemap.xml")
            generated_sitemap_files = ["sitemap.xml"]
        else:
            chunk_filenames = []
            for index, chunk in enumerate(chunks, start=1):
                filename = f"sitemap-{index}.xml"
                self.write_sitemap_file(chunk, self.output_dir / filename)
                chunk_filenames.append(filename)

            # 루트에 올릴 파일 이름을 sitemap.xml로 유지
            self.write_sitemap_index(
                chunk_filenames,
                self.output_dir / "sitemap.xml",
            )
            generated_sitemap_files = ["sitemap.xml", *chunk_filenames]

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
            f"생성 파일: {', '.join(generated_sitemap_files)}\n"
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
        help="전체 요청 사이의 최소 간격(초). 기본값: 0.05",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="robots.txt 규칙을 무시합니다.",
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
