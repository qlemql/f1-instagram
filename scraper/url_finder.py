"""
FIA 웹사이트에서 최신 F1 기자회견 URL을 탐지하는 모듈.

실제 FIA 뉴스 페이지 구조:
- 뉴스 목록: https://www.fia.com/news
- 기자회견 URL 패턴: /news/f1-{year}-{gp-name}-{type}-press-conference-transcript
- 검색 필터: title 파라미터로 "press conference transcript" 검색
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.fia.com"
NEWS_SEARCH_URL = f"{BASE_URL}/news"

# 기자회견 URL 슬러그 패턴
# 예: f1-2026-japanese-grand-prix-post-race-press-conference-transcript
TRANSCRIPT_SLUG_RE = re.compile(
    r"f1-\d{4}-[\w-]+-press-conference-transcript$"
)

# 기자회견 유형 우선순위 (숫자가 낮을수록 우선)
CONFERENCE_TYPE_PRIORITY: dict[str, int] = {
    "post-race": 0,
    "post-qualifying": 1,
    "saturday": 2,
    "friday": 3,
    "thursday": 4,
    "team-principals": 5,
}


@dataclass
class ConferenceURL:
    """발견된 기자회견 URL 정보."""
    url: str
    title: str
    date_str: str          # FIA 표기 날짜 (예: "29.03.26")
    conference_type: str   # post-race / friday / thursday 등
    gp_name: str           # Japanese Grand Prix 등
    year: int

    @property
    def type_priority(self) -> int:
        for key, priority in CONFERENCE_TYPE_PRIORITY.items():
            if key in self.conference_type.lower():
                return priority
        return 99


class URLFinder:
    """
    FIA 뉴스 페이지를 파싱해 최신 F1 기자회견 URL 목록을 반환한다.

    사용 예::

        finder = URLFinder()
        urls = finder.fetch_recent(limit=10)
        latest = finder.get_latest()
    """

    DEFAULT_TIMEOUT = 15.0
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._client = httpx.Client(
            headers=self.DEFAULT_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "URLFinder":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_recent(self, limit: int = 20) -> list[ConferenceURL]:
        """
        FIA 뉴스에서 최근 기자회견 URL 목록을 반환한다.

        Args:
            limit: 최대 반환 개수

        Returns:
            ConferenceURL 리스트 (최신순 정렬)

        Raises:
            httpx.HTTPError: 네트워크 오류
            ValueError: 파싱 실패
        """
        raw_links = self._scrape_news_listing()
        results: list[ConferenceURL] = []

        for href, title, date_str in raw_links:
            conf_url = self._parse_conference_url(href, title, date_str)
            if conf_url:
                results.append(conf_url)
            if len(results) >= limit:
                break

        logger.info("발견된 기자회견 URL: %d개", len(results))
        return results

    def get_latest(self, conference_type: Optional[str] = None) -> Optional[ConferenceURL]:
        """
        가장 최신 기자회견 URL을 반환한다.

        Args:
            conference_type: 특정 유형 필터 (예: "post-race", "friday").
                             None이면 가장 최신 항목 반환.

        Returns:
            ConferenceURL 또는 None (찾지 못한 경우)
        """
        urls = self.fetch_recent(limit=30)
        if not urls:
            return None

        if conference_type:
            filtered = [u for u in urls if conference_type.lower() in u.conference_type.lower()]
            return filtered[0] if filtered else None

        # 날짜 기준 최신 우선(내림차순), 같은 날짜면 유형 우선순위(오름차순)
        # date_str 형식: DD.MM.YY → 정렬을 위해 YY.MM.DD 로 변환
        def sort_key(u: ConferenceURL) -> tuple:
            parts = u.date_str.split(".")
            if len(parts) == 3:
                date_key = (parts[2], parts[1], parts[0])  # YY, MM, DD
            else:
                date_key = ("00", "00", "00")
            return (date_key, u.type_priority)

        return sorted(urls, key=sort_key, reverse=True)[0]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _scrape_news_listing(self) -> list[tuple[str, str, str]]:
        """
        FIA 뉴스 검색 페이지를 파싱해 (href, title, date_str) 튜플 목록을 반환.

        FIA 뉴스 목록 HTML 구조 (실제 확인):
          - 각 뉴스 항목: <a href="/news/...">제목</a> + 날짜 텍스트
          - title 파라미터로 "press conference transcript" 검색 가능
        """
        params = urlencode({"title": "press conference transcript"})
        search_url = f"{NEWS_SEARCH_URL}?{params}"

        logger.debug("뉴스 목록 요청: %s", search_url)
        try:
            resp = self._client.get(search_url)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error("뉴스 목록 HTTP 오류: %s", e)
            raise
        except httpx.RequestError as e:
            logger.error("뉴스 목록 네트워크 오류: %s", e)
            raise

        soup = BeautifulSoup(resp.text, "lxml")
        return self._extract_links_from_listing(soup)

    def _extract_links_from_listing(
        self, soup: BeautifulSoup
    ) -> list[tuple[str, str, str]]:
        """
        뉴스 목록 페이지 BeautifulSoup에서 (href, title, date_str) 추출.

        FIA 뉴스 목록 구조:
          <a href="/news/f1-2026-...transcript">제목 텍스트</a>
          날짜는 인접 텍스트 또는 형제 요소에 DD.MM.YY 형식으로 존재
        """
        results: list[tuple[str, str, str]] = []
        seen: set[str] = set()

        # /news/ 로 시작하는 모든 앵커 탐색
        for a_tag in soup.find_all("a", href=True):
            href: str = a_tag["href"].strip()

            # 절대경로 변환
            if href.startswith("/"):
                href = BASE_URL + href

            # transcript URL 패턴 필터
            slug = href.split("/news/")[-1].rstrip("/")
            if not TRANSCRIPT_SLUG_RE.match(slug):
                continue

            if href in seen:
                continue
            seen.add(href)

            title = a_tag.get_text(strip=True)

            # 날짜 탐색: 부모/형제 요소에서 DD.MM.YY 패턴 검색
            date_str = self._find_date_near(a_tag)

            results.append((href, title, date_str))

        logger.debug("뉴스 목록에서 추출된 링크: %d개", len(results))
        return results

    def _find_date_near(self, tag) -> str:
        """태그 근처의 날짜 텍스트(DD.MM.YY)를 찾아 반환."""
        date_re = re.compile(r"\b\d{2}\.\d{2}\.\d{2}\b")

        # 부모 컨테이너 텍스트에서 날짜 검색
        for parent in tag.parents:
            text = parent.get_text(" ")
            match = date_re.search(text)
            if match:
                return match.group()
            # 너무 넓은 범위는 탐색 중단
            if parent.name in ("body", "html"):
                break

        return ""

    def _parse_conference_url(
        self, href: str, title: str, date_str: str
    ) -> Optional[ConferenceURL]:
        """
        URL과 제목 텍스트에서 ConferenceURL 객체를 생성한다.

        URL 패턴:
          f1-{year}-{gp-name}-{type}-press-conference-transcript
        예:
          f1-2026-japanese-grand-prix-post-race-press-conference-transcript
          f1-2026-australian-grand-prix-thursday-press-conference-transcript
        """
        slug = href.split("/news/")[-1].rstrip("/")

        # 연도 추출
        year_match = re.search(r"f1-(\d{4})-", slug)
        if not year_match:
            return None
        year = int(year_match.group(1))

        # "-press-conference-transcript" 앞 부분에서 유형과 GP명 추출
        core = slug.replace(f"f1-{year}-", "").replace("-press-conference-transcript", "")

        # 알려진 유형 키워드로 분리
        type_keywords = [
            "post-race", "post-qualifying", "post-sprint",
            "friday", "saturday", "thursday", "team-principals",
        ]
        conference_type = "unknown"
        gp_slug = core
        for kw in type_keywords:
            if core.endswith(f"-{kw}"):
                conference_type = kw
                gp_slug = core[: -(len(kw) + 1)]
                break

        # GP 이름: kebab-case → Title Case
        gp_name = gp_slug.replace("-", " ").title()

        return ConferenceURL(
            url=href,
            title=title,
            date_str=date_str,
            conference_type=conference_type,
            gp_name=gp_name,
            year=year,
        )
