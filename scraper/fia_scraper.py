"""
FIA F1 기자회견 텍스트 수집 및 파싱 모듈.

실제 FIA HTML 구조 (2025-2026 직접 확인):
- 기자회견 본문 전체가 단일 <p style="text-align:justify"> 태그 안에 위치
- <br> 태그로 "줄"이 구분되며 각 줄이 하나의 발언 단위
- 각 줄의 구조:
    <strong>Q: 질문 전체</strong>           ← 질문 (strong 전체가 질문)
    <strong>이름 SURNAME: </strong>답변 텍스트  ← 발화자+답변 (strong=이름, 나머지=답변)
    <strong>약칭:</strong> 답변 텍스트         ← 약칭 발화자
    <strong>SECTION HEADER</strong>           ← 섹션 헤더 (콜론 없음)
- 빈 줄( ) = 구분용 빈 행, 무시
- 날짜: 뉴스 페이지 <h2> 또는 상단 메타 텍스트에서 DD.MM.YY 형식

파싱 전략:
  1. <p style="text-align:justify"> 를 본문 컨테이너로 직접 선택
  2. 컨테이너의 자식 노드를 순회하며 <br> 기준으로 "줄" 단위로 분절
  3. 각 줄에서 <strong>의 텍스트와 나머지 텍스트를 분리해 발화자/내용 분류
  4. 섹션 헤더, 참가자 목록 라인은 메타데이터로 처리
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

logger = logging.getLogger(__name__)

BASE_URL = "https://www.fia.com"

# ── 정규식 패턴 ────────────────────────────────────────────────────────────

# "이름 SURNAME:" 또는 "약칭:" 형태의 발화자 레이블
# 예: "Kimi ANTONELLI:", "KA:", "Q:", "Q: (기자명 – 매체)"
SPEAKER_LABEL_RE = re.compile(
    r"^(?P<speaker>[A-Z][A-Za-zÀ-ÖØ-öø-ÿ\s\-\.]+?):\s*(?P<rest>.*)$",
    re.DOTALL,
)

# 질문 줄: Q: 로 시작
QUESTION_LABEL_RE = re.compile(r"^Q\s*:")

# 섹션 헤더 패턴
# "PART ONE – ...", "QUESTIONS FROM THE FLOOR", "PRESS CONFERENCE", "TRACK INTERVIEWS"
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

# 날짜 패턴: DD.MM.YY
DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{2})\b")


# ── 데이터 모델 ────────────────────────────────────────────────────────────


@dataclass
class Statement:
    """기자회견의 단일 발언 단위."""
    speaker: str        # 발화자 (예: "Kimi ANTONELLI", "KA", "Q")
    text: str           # 발언 내용
    is_question: bool   # True이면 질문(Q:)
    section: str        # 속한 섹션 (예: "TRACK INTERVIEWS", "PRESS CONFERENCE")


@dataclass
class PressConference:
    """파싱된 FIA F1 기자회견 전체 데이터."""
    url: str
    title: str
    date_str: str       # FIA 표기 날짜 (DD.MM.YY)
    date_iso: str       # ISO 8601 (YYYY-MM-DD), 변환 실패 시 ""
    gp_name: str        # Grand Prix 이름
    conference_type: str  # post-race / friday / thursday 등
    participants: list[str]
    statements: list[Statement] = field(default_factory=list)
    scraped_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z"
    )

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def save_json(self, path: "Path | str") -> Path:
        """JSON 파일로 저장하고 경로를 반환한다."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        logger.info("저장 완료: %s", path)
        return path


# ── 스크래퍼 ──────────────────────────────────────────────────────────────


class FIAScraper:
    """
    FIA 기자회견 페이지를 스크래핑하여 PressConference 객체를 반환한다.

    사용 예::

        with FIAScraper() as scraper:
            pc = scraper.scrape("https://www.fia.com/news/f1-2026-...")
            pc.save_json("data/press_conference.json")
    """

    DEFAULT_TIMEOUT = 20.0
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

    def __enter__(self) -> "FIAScraper":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── Public API ────────────────────────────────────────────────────────

    def scrape(self, url: str) -> PressConference:
        """
        주어진 FIA 기자회견 URL을 파싱해 PressConference를 반환한다.

        Args:
            url: FIA 기자회견 URL

        Returns:
            PressConference 객체

        Raises:
            httpx.HTTPError: 네트워크/HTTP 오류
            ValueError: 본문을 찾지 못한 경우
        """
        logger.info("스크래핑 시작: %s", url)
        html = self._fetch(url)
        soup = BeautifulSoup(html, "lxml")
        return self._parse(url, soup)

    def scrape_to_json(self, url: str, output_path: "Path | str") -> PressConference:
        """scrape() 후 JSON 파일로 저장한다."""
        pc = self.scrape(url)
        pc.save_json(output_path)
        return pc

    # ── Fetch ─────────────────────────────────────────────────────────────

    def _fetch(self, url: str) -> str:
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
            logger.debug("HTTP %d: %s", resp.status_code, url)
            return resp.text
        except httpx.HTTPStatusError as e:
            logger.error("HTTP 오류 [%d]: %s", e.response.status_code, url)
            raise
        except httpx.TimeoutException:
            logger.error("타임아웃: %s", url)
            raise
        except httpx.RequestError as e:
            logger.error("네트워크 오류: %s – %s", url, e)
            raise

    # ── Parse ─────────────────────────────────────────────────────────────

    def _parse(self, url: str, soup: BeautifulSoup) -> PressConference:
        title = self._extract_title(soup)
        date_str = self._extract_date(soup)
        date_iso = self._convert_date(date_str)
        gp_name, conference_type = self._extract_meta_from_title(title)

        body_container = self._find_body_container(soup)
        if body_container is None:
            raise ValueError(f"본문 컨테이너를 찾지 못했습니다: {url}")

        lines = self._split_by_br(body_container)
        if not lines:
            raise ValueError(f"본문 내 줄을 추출하지 못했습니다: {url}")

        logger.debug("추출된 줄 수: %d", len(lines))
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
        )

    # ── HTML 추출 헬퍼 ────────────────────────────────────────────────────

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """페이지 제목 추출: <h1> → <h2> → <title> 순으로 시도."""
        for tag_name in ("h1", "h2"):
            tag = soup.find(tag_name)
            if tag:
                text = tag.get_text(strip=True)
                if "press conference" in text.lower():
                    return text
        # 제목 태그에서 첫 번째 h2라도 반환
        h = soup.find("h2")
        if h:
            return h.get_text(strip=True)
        title_tag = soup.find("title")
        if title_tag:
            return title_tag.get_text(strip=True).split("|")[0].strip()
        return ""

    def _extract_date(self, soup: BeautifulSoup) -> str:
        """
        페이지에서 날짜 문자열(DD.MM.YY)을 추출한다.

        FIA 실제 HTML 구조 (직접 확인):
          <span class="date-display-single">29.03.26</span>
        """
        # 1순위: FIA 전용 날짜 클래스
        date_span = soup.find("span", class_="date-display-single")
        if date_span:
            text = date_span.get_text(strip=True)
            m = DATE_RE.search(text)
            if m:
                return m.group()

        # 2순위: created-date div 내 텍스트
        date_div = soup.find("div", class_="created-date")
        if date_div:
            text = date_div.get_text(strip=True)
            m = DATE_RE.search(text)
            if m:
                return m.group()

        # 3순위: 범용 탐색
        for tag in soup.find_all(["span", "div", "time"], limit=200):
            text = tag.get_text(strip=True)
            if len(text) < 30:  # 날짜만 있는 짧은 태그 우선
                m = DATE_RE.search(text)
                if m:
                    return m.group()

        return ""

    def _find_body_container(self, soup: BeautifulSoup) -> Optional[Tag]:
        """
        기자회견 본문이 담긴 컨테이너 태그를 찾는다.

        FIA 실제 구조 (직접 확인):
          - <p style="text-align:justify"> 안에 전체 본문
          - strong 태그 50개 이상, br 태그 80개 이상

        대안 탐색:
          - Drupal field 클래스
          - 가장 큰 텍스트 블록
        """
        # 1순위: style="text-align:justify" 를 가진 p 태그
        for p in soup.find_all("p", style=True):
            style = p.get("style", "")
            if "text-align" in style:
                strong_count = len(p.find_all("strong"))
                br_count = len(p.find_all("br"))
                if strong_count >= 5 and br_count >= 5:
                    logger.debug(
                        "본문 컨테이너 발견 (style p): strong=%d, br=%d",
                        strong_count, br_count,
                    )
                    return p

        # 2순위: Drupal 표준 본문 래퍼
        drupal_selectors = [
            "div.field--name-body",
            "div.field--type-text-with-summary",
            "div.node__content",
            "div.field-items",
        ]
        for selector in drupal_selectors:
            container = soup.select_one(selector)
            if container:
                p = container.find("p")
                if p and len(p.find_all("br")) >= 5:
                    logger.debug("본문 컨테이너 발견 (%s)", selector)
                    return p

        # 3순위: strong + br이 가장 많은 p 태그
        best_p = None
        best_score = 0
        for p in soup.find_all("p"):
            score = len(p.find_all("strong")) + len(p.find_all("br"))
            if score > best_score:
                best_score = score
                best_p = p

        if best_p and best_score >= 10:
            logger.debug("본문 컨테이너 발견 (휴리스틱, score=%d)", best_score)
            return best_p

        return None

    # ── 줄 분절 ───────────────────────────────────────────────────────────

    def _split_by_br(self, container: Tag) -> list[list]:
        """
        컨테이너의 자식 노드를 <br> 기준으로 분절해 "줄" 목록을 반환한다.

        각 줄은 [node, node, ...] 형태의 노드 리스트.
        빈 줄(공백만 있는 줄)은 제거한다.
        """
        lines: list[list] = []
        current: list = []

        for child in container.children:
            if isinstance(child, Tag) and child.name == "br":
                # 현재 줄에 실질적 내용이 있으면 저장
                text = "".join(
                    c.get_text() if isinstance(c, Tag) else str(c)
                    for c in current
                ).strip().replace("\xa0", " ").strip()
                if text:
                    lines.append(current)
                current = []
            else:
                current.append(child)

        # 마지막 줄 처리
        if current:
            text = "".join(
                c.get_text() if isinstance(c, Tag) else str(c)
                for c in current
            ).strip().replace("\xa0", " ").strip()
            if text:
                lines.append(current)

        return lines

    # ── 줄 파싱 ───────────────────────────────────────────────────────────

    def _parse_lines(
        self, lines: list[list]
    ) -> tuple[list[Statement], list[str]]:
        """
        줄 목록을 순회해 Statement 리스트와 참가자 목록을 반환한다.

        각 줄의 노드 구조:
          케이스 A: [<strong>Q: 질문 전체</strong>]
                    → 질문 발언, is_question=True
          케이스 B: [<strong>이름 SURNAME: </strong>, "답변 텍스트"]
                    → 발화자 이름 + 답변
          케이스 C: [<strong>약칭:</strong>, " 답변 텍스트"]
                    → 약칭 발화자 + 답변
          케이스 D: [<strong>SECTION HEADER</strong>]
                    → 섹션 헤더, Statement 생성 안 함
          케이스 E: [<strong>1 – 이름 (팀)</strong>]
                    → 참가자 정보
        """
        statements: list[Statement] = []
        participants: set[str] = set()
        current_section = "INTRO"

        for nodes in lines:
            line_info = self._classify_line(nodes)

            kind = line_info["kind"]

            if kind == "section_header":
                current_section = line_info["text"]
                # 섹션 헤더에서 참가자 추출 시도
                self._extract_participants_from_section(line_info["text"], participants)
                logger.debug("섹션 전환: %s", current_section)

            elif kind == "participant":
                participants.add(line_info["name"])

            elif kind == "question":
                statements.append(Statement(
                    speaker="Q",
                    text=line_info["text"],
                    is_question=True,
                    section=current_section,
                ))

            elif kind == "answer":
                speaker = line_info["speaker"]
                # 풀네임만 참가자로 등록 (약칭 제외: 2-3자 대문자 이니셜)
                if speaker and not re.match(r"^[A-Z]{1,3}$", speaker):
                    participants.add(speaker)
                statements.append(Statement(
                    speaker=speaker,
                    text=line_info["text"],
                    is_question=False,
                    section=current_section,
                ))

        return statements, sorted(participants)

    def _classify_line(self, nodes: list) -> dict:
        """
        줄(노드 리스트)을 분석해 종류와 내용을 반환한다.

        Returns:
            dict with keys:
              - kind: "section_header" | "participant" | "question" | "answer" | "unknown"
              - text: 전체 텍스트
              - speaker: 발화자 (answer 시)
              - name: 참가자 이름 (participant 시)
        """
        # 전체 텍스트와 strong 노드 추출
        strong_nodes = [n for n in nodes if isinstance(n, Tag) and n.name == "strong"]
        non_strong_text = "".join(
            str(n) if isinstance(n, NavigableString) else n.get_text()
            for n in nodes
            if not (isinstance(n, Tag) and n.name == "strong")
        ).replace("\xa0", " ").strip()

        if not strong_nodes:
            return {"kind": "unknown", "text": non_strong_text, "speaker": "", "name": ""}

        first_strong_text = strong_nodes[0].get_text().replace("\xa0", " ").strip()

        # ── 케이스 A: 질문 (Q: 로 시작하는 strong) ──
        if QUESTION_LABEL_RE.match(first_strong_text):
            # Q: 이후 텍스트가 strong 내에 있는 경우 (strong 전체가 질문)
            full_q_text = first_strong_text
            if non_strong_text:
                full_q_text += " " + non_strong_text
            # "Q: " 접두어 제거
            question_body = re.sub(r"^Q\s*:\s*", "", full_q_text).strip()
            # 기자 정보 "(기자명 – 매체)" 포함 가능
            return {"kind": "question", "text": question_body, "speaker": "Q", "name": ""}

        # ── 케이스 D: 섹션 헤더 ──
        if SECTION_HEADER_RE.match(first_strong_text):
            return {"kind": "section_header", "text": first_strong_text.strip(), "speaker": "", "name": ""}

        # ── 케이스 E: 참가자 정보 (1 – 이름 (팀)) ──
        pm = PARTICIPANT_LINE_RE.match(first_strong_text)
        if pm:
            raw_name = pm.group(1).strip()
            # "(팀명)" 제거
            name = re.sub(r"\s*\(.*?\)\s*$", "", raw_name).strip()
            return {"kind": "participant", "text": first_strong_text, "speaker": "", "name": name}

        # ── 케이스 B/C: 발화자 + 답변 ──
        # strong 텍스트에 콜론이 있으면 발화자:답변 구조
        m = SPEAKER_LABEL_RE.match(first_strong_text)
        if m:
            speaker = m.group("speaker").strip().rstrip(":")
            rest_in_strong = m.group("rest").strip()
            full_text = (rest_in_strong + " " + non_strong_text).strip()
            return {"kind": "answer", "text": full_text, "speaker": speaker, "name": ""}

        # strong이 "이름:" 형태이고 나머지 텍스트가 발언인 경우
        if first_strong_text.endswith(":"):
            speaker = first_strong_text[:-1].strip()
            return {"kind": "answer", "text": non_strong_text, "speaker": speaker, "name": ""}

        # ── 기타: 구분자나 메타 정보 ──
        full_text = first_strong_text
        if non_strong_text:
            full_text += " " + non_strong_text
        return {"kind": "unknown", "text": full_text.strip(), "speaker": "", "name": ""}

    # ── 메타데이터 헬퍼 ───────────────────────────────────────────────────

    def _extract_participants_from_section(
        self, header_text: str, participants: set[str]
    ) -> None:
        """
        "PART ONE – Nico HULKENBERG (Audi), George RUSSELL (Mercedes)" 형태에서
        참가자 이름 추출.
        """
        after_dash = re.split(r"[–\-]", header_text, maxsplit=1)
        if len(after_dash) < 2:
            return
        names_part = after_dash[1]
        for entry in names_part.split(","):
            name = re.sub(r"\(.*?\)", "", entry).strip()
            if name:
                participants.add(name)

    def _extract_meta_from_title(self, title: str) -> tuple[str, str]:
        """
        제목에서 GP 이름과 기자회견 유형 추출.

        예: "F1 - 2026 Australian Grand Prix Post-Race Press Conference Transcript"
             → ("2026 Australian Grand Prix", "post-race")
        """
        gp_match = re.search(r"(\d{4}\s+[\w\s]+Grand Prix)", title, re.IGNORECASE)
        gp_name = gp_match.group(1).strip() if gp_match else ""

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
        for key, pattern in type_keywords.items():
            if re.search(pattern, title, re.IGNORECASE):
                conference_type = key
                break

        return gp_name, conference_type

    def _convert_date(self, date_str: str) -> str:
        """FIA 날짜 형식(DD.MM.YY) → ISO 8601(YYYY-MM-DD)."""
        m = DATE_RE.match(date_str.strip())
        if not m:
            return ""
        day, month, year_short = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = 2000 + year_short
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            logger.warning("날짜 변환 실패: %s", date_str)
            return ""
