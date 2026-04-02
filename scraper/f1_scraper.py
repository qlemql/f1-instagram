"""
formula1.com 기자회견 텍스트 수집 모듈 (FIA fallback).

formula1.com 구조 특징:
- Contentful CMS 기반, URL에 고유 ID 포함
  예: /en/latest/article/2026-japanese-grand-prix-post-race-press-conference.{ID}
- 기사 본문은 <div class="f1-article-content"> 또는 <div class="article-content"> 내
  <p>, <strong>, <br> 태그로 구성
- 발언자 표기: FIA와 동일하게 <strong>이름 SURNAME: </strong> 형태
- 뉴스 목록: /en/latest/all.html 에서 press conference 기사 탐색

파싱 전략:
  1. 뉴스 목록 페이지에서 press conference/transcript 기사 URL 탐색
  2. 기사 URL에서 고유 ID 추출 후 본문 접근
  3. FIA 스크래퍼와 동일한 br/strong 기반 파싱 로직 적용
  4. models.PressConference 형태로 반환
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

# 프로젝트 루트에서 실행 시 사용
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import PressConference, Statement

logger = logging.getLogger(__name__)

BASE_URL = "https://www.formula1.com"
LATEST_URL = f"{BASE_URL}/en/latest"
ALL_NEWS_URL = f"{BASE_URL}/en/latest/all.html"

# ── 정규식 패턴 ─────────────────────────────────────────────────────────────

# press conference/transcript 관련 기사 URL 패턴
# 예: /en/latest/article/2026-japanese-grand-prix-post-race-press-conference.AbCdEfGhIj1234
# 검증 결과(2026-03): Contentful ID는 영숫자 10~30자, press-conference 위치는 슬러그 어디에나 올 수 있음
PRESS_CONF_SLUG_RE = re.compile(
    r"/en/latest/article/([\w-]*(?:press-conference|transcript)[\w-]*)\.[A-Za-z0-9]{10,}$"
)

# 발화자 레이블: "이름 SURNAME: " 또는 "약칭: "
SPEAKER_LABEL_RE = re.compile(
    r"^(?P<speaker>[A-Z][A-Za-zÀ-ÖØ-öø-ÿ\s\-\.]+?):\s*(?P<rest>.*)$",
    re.DOTALL,
)

# 질문 줄: Q: 로 시작
QUESTION_LABEL_RE = re.compile(r"^Q\s*:")

# 섹션 헤더 패턴
SECTION_HEADER_RE = re.compile(
    r"^(PART\s+(ONE|TWO|THREE|FOUR|FIVE|\d+)|"
    r"QUESTIONS?\s+FROM\s+THE\s+FLOOR|"
    r"TRACK\s+INTERVIEWS?|"
    r"PRESS\s+CONFERENCE|"
    r"TEAM\s+REPRESENTATIVES?|"
    r"END[S]?\s*$)",
    re.IGNORECASE,
)

# 참가자 줄: "1 – 이름 (팀)" 형태
PARTICIPANT_LINE_RE = re.compile(r"^\d+\s*[–\-]\s*(.+)")

# 날짜 패턴 (다양한 형태 지원)
DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATE_DMY_RE = re.compile(r"\b(\d{1,2})\s+(\w+)\s+(\d{4})\b")  # "29 March 2026"
DATE_COMPACT_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{2})\b")  # "29.03.26"

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


class F1Scraper:
    """
    formula1.com 기자회견 기사를 수집해 PressConference 객체로 반환한다.

    FIA 스크래퍼 실패 시 fallback으로 사용한다.

    사용 예::

        with F1Scraper() as scraper:
            pc = scraper.scrape("https://www.formula1.com/en/latest/article/...")
            # 또는 URL 자동 탐색:
            pc = scraper.scrape_latest(gp_name="Japanese Grand Prix",
                                       conference_type="post-race")
    """

    DEFAULT_TIMEOUT = 20.0
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._client = httpx.Client(
            headers=self.DEFAULT_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "F1Scraper":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── Public API ─────────────────────────────────────────────────────────

    def scrape(self, url: str) -> PressConference:
        """
        주어진 formula1.com 기자회견 URL을 파싱해 PressConference를 반환한다.

        Args:
            url: formula1.com 기사 URL

        Returns:
            models.PressConference 객체

        Raises:
            httpx.HTTPError: 네트워크/HTTP 오류
            ValueError: 본문을 찾지 못한 경우
        """
        logger.info("[F1Scraper] 스크래핑 시작: %s", url)
        html = self._fetch(url)
        soup = BeautifulSoup(html, "lxml")
        return self._parse(url, soup)

    def find_conference_urls(
        self,
        gp_name: Optional[str] = None,
        conference_type: Optional[str] = None,
        limit: int = 20,
    ) -> list[str]:
        """
        formula1.com 뉴스 목록에서 기자회견 기사 URL을 탐색한다.

        Args:
            gp_name: GP 이름 필터 (예: "Japanese Grand Prix"). None이면 전체.
            conference_type: 기자회견 유형 필터 (예: "post-race"). None이면 전체.
            limit: 최대 반환 개수

        Returns:
            기사 URL 리스트 (최신순)
        """
        urls: list[str] = []

        # 뉴스 목록 페이지에서 탐색
        for page_url in self._iter_news_pages():
            try:
                html = self._fetch(page_url)
            except httpx.HTTPError as e:
                logger.warning("[F1Scraper] 뉴스 목록 페이지 접근 실패: %s – %s", page_url, e)
                break

            soup = BeautifulSoup(html, "lxml")
            found = self._extract_conference_links(soup, gp_name, conference_type)
            urls.extend(found)

            if len(urls) >= limit:
                break

        unique = list(dict.fromkeys(urls))  # 중복 제거, 순서 유지
        logger.info("[F1Scraper] 발견된 기자회견 URL: %d개", len(unique[:limit]))
        return unique[:limit]

    def scrape_latest(
        self,
        gp_name: Optional[str] = None,
        conference_type: Optional[str] = None,
    ) -> Optional[PressConference]:
        """
        formula1.com에서 최신 기자회견을 자동 탐색 후 수집한다.

        Returns:
            PressConference 또는 None (찾지 못한 경우)
        """
        urls = self.find_conference_urls(gp_name, conference_type, limit=5)
        if not urls:
            logger.warning("[F1Scraper] 기자회견 URL을 찾지 못했습니다.")
            return None

        for url in urls:
            try:
                return self.scrape(url)
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("[F1Scraper] 수집 실패 (다음 URL 시도): %s – %s", url, e)

        return None

    # ── Fetch ──────────────────────────────────────────────────────────────

    def _fetch(self, url: str) -> str:
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
            logger.debug("[F1Scraper] HTTP %d: %s", resp.status_code, url)
            return resp.text
        except httpx.HTTPStatusError as e:
            logger.error("[F1Scraper] HTTP 오류 [%d]: %s", e.response.status_code, url)
            raise
        except httpx.TimeoutException:
            logger.error("[F1Scraper] 타임아웃: %s", url)
            raise
        except httpx.RequestError as e:
            logger.error("[F1Scraper] 네트워크 오류: %s – %s", url, e)
            raise

    # ── URL 탐색 ────────────────────────────────────────────────────────────

    def _iter_news_pages(self):
        """뉴스 목록 URL을 순서대로 반환하는 제너레이터."""
        yield ALL_NEWS_URL
        # 추가 페이지네이션이 있는 경우를 대비한 확장 포인트
        for page in range(2, 6):
            yield f"{ALL_NEWS_URL}?page={page}"

    def _extract_conference_links(
        self,
        soup: BeautifulSoup,
        gp_name: Optional[str],
        conference_type: Optional[str],
    ) -> list[str]:
        """
        BeautifulSoup 파싱 결과에서 기자회견 기사 링크를 추출한다.

        formula1.com URL 패턴:
          /en/latest/article/{slug}.{contentful-id}
        """
        results: list[str] = []

        for a_tag in soup.find_all("a", href=True):
            href: str = a_tag["href"].strip()

            # 절대 URL 변환
            if href.startswith("/"):
                href = BASE_URL + href
            elif not href.startswith("http"):
                continue

            # press conference 기사 패턴 확인
            if not self._is_press_conference_url(href):
                continue

            # GP 이름 필터
            if gp_name and not self._url_matches_gp(href, gp_name):
                continue

            # 기자회견 유형 필터
            if conference_type and not self._url_matches_type(href, conference_type):
                continue

            results.append(href)

        return results

    def _is_press_conference_url(self, url: str) -> bool:
        """URL이 기자회견 기사인지 확인한다."""
        slug = url.split("/en/latest/article/")[-1] if "/en/latest/article/" in url else ""
        if not slug:
            return False

        # "press-conference" 또는 "transcript" 포함 여부
        return "press-conference" in slug or "transcript" in slug

    def _url_matches_gp(self, url: str, gp_name: str) -> bool:
        """URL이 특정 GP와 매칭되는지 확인한다."""
        gp_slug = gp_name.lower().replace(" ", "-").replace("grand prix", "grand-prix")
        # 핵심 단어(국가/도시명)만 추출해 비교
        key_words = [w for w in gp_slug.split("-") if len(w) > 3 and w not in ("grand", "prix")]
        return any(word in url.lower() for word in key_words)

    def _url_matches_type(self, url: str, conference_type: str) -> bool:
        """URL이 특정 기자회견 유형과 매칭되는지 확인한다."""
        type_slug = conference_type.lower().replace(" ", "-")
        return type_slug in url.lower()

    # ── Parse ──────────────────────────────────────────────────────────────

    def _parse(self, url: str, soup: BeautifulSoup) -> PressConference:
        title = self._extract_title(soup)
        date_str, date_iso = self._extract_date(soup)
        gp_name, conference_type = self._extract_meta_from_title_and_url(title, url)

        body_container = self._find_body_container(soup)
        if body_container is None:
            raise ValueError(f"[F1Scraper] 본문 컨테이너를 찾지 못했습니다: {url}")

        lines = self._split_by_br(body_container)

        # br 분절 결과가 없으면 p 태그 단위로 재시도
        if not lines:
            lines = self._split_by_paragraph(body_container)

        if not lines:
            raise ValueError(f"[F1Scraper] 본문 내 줄을 추출하지 못했습니다: {url}")

        logger.debug("[F1Scraper] 추출된 줄 수: %d", len(lines))
        statements, participants = self._parse_lines(lines)

        return PressConference(
            url=url,
            title=title,
            date_str=date_str,
            date_iso=date_iso,
            gp_name=gp_name,
            conference_type=conference_type,
            participants=participants,
            statements=statements,
            scraped_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        )

    # ── HTML 추출 헬퍼 ─────────────────────────────────────────────────────

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """페이지 제목 추출."""
        # formula1.com 기사 제목: h1.f1-heading 또는 h1
        for selector in ("h1.f1-heading", "h1.article-title", "h1"):
            tag = soup.select_one(selector)
            if tag:
                return tag.get_text(strip=True)

        title_tag = soup.find("title")
        if title_tag:
            return title_tag.get_text(strip=True).split("|")[0].strip()
        return ""

    def _extract_date(self, soup: BeautifulSoup) -> tuple[str, str]:
        """
        날짜 추출. (date_str, date_iso) 튜플 반환.

        formula1.com 날짜 위치:
          - <time datetime="2026-03-29T..."> 태그
          - <span class="f1-article-date"> 또는 유사한 클래스
          - meta[property="article:published_time"]
        """
        # 1순위: <time> 태그의 datetime 속성
        time_tag = soup.find("time")
        if time_tag:
            dt_attr = time_tag.get("datetime", "")
            m = DATE_ISO_RE.search(dt_attr)
            if m:
                date_iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                date_str = self._iso_to_fia_format(date_iso)
                return date_str, date_iso

        # 2순위: meta 태그
        for meta in soup.find_all("meta", property=True):
            prop = meta.get("property", "")
            if "published_time" in prop or "modified_time" in prop:
                content = meta.get("content", "")
                m = DATE_ISO_RE.search(content)
                if m:
                    date_iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                    date_str = self._iso_to_fia_format(date_iso)
                    return date_str, date_iso

        # 3순위: 텍스트 탐색 (ISO 형식)
        for tag in soup.find_all(["span", "div", "p", "time"], limit=300):
            text = tag.get_text(strip=True)
            if len(text) < 50:
                m = DATE_ISO_RE.search(text)
                if m:
                    date_iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                    date_str = self._iso_to_fia_format(date_iso)
                    return date_str, date_iso

                # "29 March 2026" 형태
                m2 = DATE_DMY_RE.search(text)
                if m2:
                    day_s, month_s, year_s = m2.group(1), m2.group(2).lower(), m2.group(3)
                    month_num = MONTH_MAP.get(month_s)
                    if month_num:
                        date_iso = f"{year_s}-{month_num:02d}-{int(day_s):02d}"
                        date_str = self._iso_to_fia_format(date_iso)
                        return date_str, date_iso

        return "", ""

    def _iso_to_fia_format(self, date_iso: str) -> str:
        """ISO 날짜(YYYY-MM-DD) → FIA 포맷(DD.MM.YY)."""
        try:
            dt = datetime.strptime(date_iso, "%Y-%m-%d")
            return dt.strftime("%d.%m.%y")
        except ValueError:
            return ""

    def _find_body_container(self, soup: BeautifulSoup) -> Optional[Tag]:
        """
        기자회견 본문 컨테이너를 찾는다.

        formula1.com 기사 본문 후보 셀렉터 (실제 구조는 버전마다 상이):
          - div.f1-article-content
          - div.article-content
          - div.article-body
          - article 태그
          - React/Next.js CSS 모듈 기반 ArticleBodyWrapper 패턴
          - 텍스트 밀도 가장 높은 div

        검증 결과 (2026-03): formula1.com은 Next.js + React 기반으로 렌더링되며
        컴포넌트명 ArticleBodyWrapper, CSS 모듈 클래스 패턴(ModuleName-module_xxx__hash)을 사용.
        정적 클래스명이 없을 경우 p 태그 밀도 휴리스틱으로 폴백.
        """
        # 1순위: formula1.com 전용 클래스 (레거시 및 현재)
        for selector in (
            "div.f1-article-content",
            "div.article-content",
            "div.article-body",
            "div[class*='articleContent']",
            "div[class*='article-content']",
            "div[class*='ArticleContent']",
            "div[class*='ArticleBody']",
            "div[class*='article-body']",
            "div[class*='BodyWrapper']",
            "div[class*='bodyWrapper']",
        ):
            container = soup.select_one(selector)
            if container and len(container.get_text(strip=True)) > 200:
                strong_count = len(container.find_all("strong"))
                logger.debug(
                    "[F1Scraper] 본문 컨테이너 발견 (%s): strong=%d",
                    selector, strong_count,
                )
                return container

        # 2순위: <article> 태그
        article = soup.find("article")
        if article and len(article.get_text(strip=True)) > 200:
            logger.debug("[F1Scraper] 본문 컨테이너 발견 (article 태그)")
            return article

        # 3순위: strong + p 태그가 가장 많은 div
        best_div = None
        best_score = 0
        for div in soup.find_all("div"):
            score = (
                len(div.find_all("strong")) * 2
                + len(div.find_all("p"))
                + len(div.find_all("br"))
            )
            if score > best_score:
                best_score = score
                best_div = div

        if best_div and best_score >= 10:
            logger.debug("[F1Scraper] 본문 컨테이너 발견 (휴리스틱, score=%d)", best_score)
            return best_div

        # 4순위: p 태그들의 부모 중 텍스트 가장 긴 것
        best_p = None
        best_text_len = 0
        for p in soup.find_all("p"):
            tl = len(p.get_text())
            if tl > best_text_len:
                best_text_len = tl
                best_p = p

        if best_p and best_text_len > 200:
            parent = best_p.parent
            if parent:
                logger.debug("[F1Scraper] 본문 컨테이너 발견 (p의 부모)")
                return parent

        return None

    # ── 줄 분절 ────────────────────────────────────────────────────────────

    def _split_by_br(self, container: Tag) -> list[list]:
        """
        컨테이너 내 자식 노드를 <br> 기준으로 분절한다.
        FIA 스크래퍼와 동일한 로직.
        """
        lines: list[list] = []
        current: list = []

        def process_node(node):
            if isinstance(node, Tag) and node.name == "br":
                text = "".join(
                    c.get_text() if isinstance(c, Tag) else str(c)
                    for c in current
                ).strip().replace("\xa0", " ").strip()
                if text:
                    lines.append(list(current))
                current.clear()
            elif isinstance(node, Tag) and node.name in ("p", "div"):
                # p/div 내부도 재귀 탐색
                for child in node.children:
                    process_node(child)
                # p/div 경계를 br처럼 취급
                text = "".join(
                    c.get_text() if isinstance(c, Tag) else str(c)
                    for c in current
                ).strip().replace("\xa0", " ").strip()
                if text:
                    lines.append(list(current))
                current.clear()
            else:
                current.append(node)

        for child in container.children:
            process_node(child)

        # 마지막 줄
        if current:
            text = "".join(
                c.get_text() if isinstance(c, Tag) else str(c)
                for c in current
            ).strip().replace("\xa0", " ").strip()
            if text:
                lines.append(current)

        return lines

    def _split_by_paragraph(self, container: Tag) -> list[list]:
        """
        br 분절이 없을 때 p 태그 단위로 분절한다.
        """
        lines: list[list] = []
        for p in container.find_all("p"):
            text = p.get_text(strip=True).replace("\xa0", " ")
            if text:
                lines.append(list(p.children))
        return lines

    # ── 줄 파싱 (FIA 스크래퍼와 동일 로직) ────────────────────────────────

    def _parse_lines(
        self, lines: list[list]
    ) -> tuple[list[Statement], list[str]]:
        """
        줄 목록을 순회해 Statement 리스트와 참가자 목록을 반환한다.
        seq 번호를 Statement에 부여한다.
        """
        statements: list[Statement] = []
        participants: set[str] = set()
        current_section = "INTRO"
        seq = 0

        for nodes in lines:
            line_info = self._classify_line(nodes)
            kind = line_info["kind"]

            if kind == "section_header":
                current_section = line_info["text"]
                self._extract_participants_from_section(line_info["text"], participants)
                logger.debug("[F1Scraper] 섹션 전환: %s", current_section)

            elif kind == "participant":
                participants.add(line_info["name"])

            elif kind == "question":
                statements.append(Statement(
                    speaker="Q",
                    text=line_info["text"],
                    is_question=True,
                    section=current_section,
                    seq=seq,
                ))
                seq += 1

            elif kind == "answer":
                speaker = line_info["speaker"]
                if speaker and not re.match(r"^[A-Z]{1,3}$", speaker):
                    participants.add(speaker)
                statements.append(Statement(
                    speaker=speaker,
                    text=line_info["text"],
                    is_question=False,
                    section=current_section,
                    seq=seq,
                ))
                seq += 1

        return statements, sorted(participants)

    def _classify_line(self, nodes: list) -> dict:
        """줄(노드 리스트)을 분석해 종류와 내용을 반환한다."""
        strong_nodes = [n for n in nodes if isinstance(n, Tag) and n.name == "strong"]
        non_strong_text = "".join(
            str(n) if isinstance(n, NavigableString) else n.get_text()
            for n in nodes
            if not (isinstance(n, Tag) and n.name == "strong")
        ).replace("\xa0", " ").strip()

        # strong 없이 텍스트만 있는 줄
        if not strong_nodes:
            full_text = non_strong_text
            # 텍스트 자체가 "이름: 발언" 패턴인지 확인
            m = SPEAKER_LABEL_RE.match(full_text)
            if m:
                speaker = m.group("speaker").strip().rstrip(":")
                rest = m.group("rest").strip()
                if QUESTION_LABEL_RE.match(full_text):
                    question_body = re.sub(r"^Q\s*:\s*", "", full_text).strip()
                    return {"kind": "question", "text": question_body, "speaker": "Q", "name": ""}
                return {"kind": "answer", "text": rest, "speaker": speaker, "name": ""}
            return {"kind": "unknown", "text": full_text, "speaker": "", "name": ""}

        first_strong_text = strong_nodes[0].get_text().replace("\xa0", " ").strip()

        # 케이스 A: 질문
        if QUESTION_LABEL_RE.match(first_strong_text):
            full_q_text = first_strong_text
            if non_strong_text:
                full_q_text += " " + non_strong_text
            question_body = re.sub(r"^Q\s*:\s*", "", full_q_text).strip()
            return {"kind": "question", "text": question_body, "speaker": "Q", "name": ""}

        # 케이스 D: 섹션 헤더
        if SECTION_HEADER_RE.match(first_strong_text):
            return {"kind": "section_header", "text": first_strong_text.strip(), "speaker": "", "name": ""}

        # 케이스 E: 참가자 정보
        pm = PARTICIPANT_LINE_RE.match(first_strong_text)
        if pm:
            raw_name = pm.group(1).strip()
            name = re.sub(r"\s*\(.*?\)\s*$", "", raw_name).strip()
            return {"kind": "participant", "text": first_strong_text, "speaker": "", "name": name}

        # 케이스 B/C: 발화자 + 답변
        m = SPEAKER_LABEL_RE.match(first_strong_text)
        if m:
            speaker = m.group("speaker").strip().rstrip(":")
            rest_in_strong = m.group("rest").strip()
            full_text = (rest_in_strong + " " + non_strong_text).strip()
            return {"kind": "answer", "text": full_text, "speaker": speaker, "name": ""}

        if first_strong_text.endswith(":"):
            speaker = first_strong_text[:-1].strip()
            return {"kind": "answer", "text": non_strong_text, "speaker": speaker, "name": ""}

        # 기타
        full_text = first_strong_text
        if non_strong_text:
            full_text += " " + non_strong_text
        return {"kind": "unknown", "text": full_text.strip(), "speaker": "", "name": ""}

    # ── 메타데이터 헬퍼 ────────────────────────────────────────────────────

    def _extract_participants_from_section(
        self, header_text: str, participants: set[str]
    ) -> None:
        """섹션 헤더에서 참가자 이름 추출."""
        after_dash = re.split(r"[–\-]", header_text, maxsplit=1)
        if len(after_dash) < 2:
            return
        names_part = after_dash[1]
        for entry in names_part.split(","):
            name = re.sub(r"\(.*?\)", "", entry).strip()
            if name:
                participants.add(name)

    def _extract_meta_from_title_and_url(
        self, title: str, url: str
    ) -> tuple[str, str]:
        """
        제목과 URL에서 GP 이름과 기자회견 유형 추출.

        예:
          "2026 Japanese Grand Prix Post-Race Press Conference"
          → ("2026 Japanese Grand Prix", "post-race")
        """
        # GP 이름
        gp_match = re.search(r"(\d{4}\s+[\w\s]+Grand Prix)", title, re.IGNORECASE)
        if not gp_match:
            # URL 슬러그에서 추출 시도
            slug = url.split("/en/latest/article/")[-1].split(".")[0]
            year_match = re.search(r"(\d{4})", slug)
            gp_slug_match = re.search(
                r"\d{4}-([\w-]+-grand-prix)", slug
            )
            if year_match and gp_slug_match:
                gp_name = gp_slug_match.group(1).replace("-", " ").title()
            else:
                gp_name = ""
        else:
            gp_name = gp_match.group(1).strip()

        # 기자회견 유형
        type_keywords = {
            "post-race": r"post[-\s]race",
            "post-qualifying": r"post[-\s]qualifying",
            "post-sprint": r"post[-\s]sprint",
            "friday": r"\bfriday\b",
            "saturday": r"\bsaturday\b",
            "thursday": r"\bthursday\b",
            "team-principals": r"team[-\s]principals?",
        }
        conference_type = "unknown"
        combined = title + " " + url
        for key, pattern in type_keywords.items():
            if re.search(pattern, combined, re.IGNORECASE):
                conference_type = key
                break

        return gp_name, conference_type
