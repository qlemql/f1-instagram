"""
수집기 오케스트레이터 — FIA 1차 수집, formula1.com fallback.

동작 흐름:
  1. URLFinder로 FIA 뉴스 목록에서 기자회견 URL 탐색
  2. data/raw/ 폴더 내 기존 파일과 비교해 신규 URL만 처리 (중복 방지)
  3. FIAScraper로 1차 수집 시도
  4. FIA 실패 시 F1Scraper(formula1.com)로 fallback 수집
  5. 수집 결과를 models.PressConference로 변환
  6. 유효성 검사 (statements 수, participants) 후 경고 로그
  7. data/raw/{gp_slug}_{conference_type}_{date_iso}.json 저장

사용 예::

    from scraper.collector import Collector

    collector = Collector()
    results = collector.collect_new()
    for pc in results:
        print(pc.gp_name, pc.conference_type, len(pc.statements))
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

# 프로젝트 루트 경로 설정
import sys
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from models import PressConference, Statement
from scraper.fia_scraper import (
    FIAScraper,
    PressConference as FIAPressConference,
    Statement as FIAStatement,
)
from scraper.url_finder import URLFinder, ConferenceURL
from scraper.f1_scraper import F1Scraper

logger = logging.getLogger(__name__)

# data/raw 폴더 기본 경로
DEFAULT_RAW_DIR = _PROJECT_ROOT / "data" / "raw"


# ── 어댑터: FIA 내부 모델 → models.PressConference ─────────────────────────

def _adapt_fia_pc(fia_pc: FIAPressConference) -> PressConference:
    """
    fia_scraper.py의 내부 PressConference 객체를
    models.py의 PressConference로 변환한다.

    FIA Statement(speaker, text, is_question, section)
    → models Statement(speaker, text, is_question, section, seq)
    """
    statements = [
        Statement(
            speaker=s.speaker,
            text=s.text,
            is_question=s.is_question,
            section=s.section,
            seq=idx,
        )
        for idx, s in enumerate(fia_pc.statements)
    ]

    return PressConference(
        url=fia_pc.url,
        title=fia_pc.title,
        date_str=fia_pc.date_str,
        date_iso=fia_pc.date_iso,
        gp_name=fia_pc.gp_name,
        conference_type=fia_pc.conference_type,
        participants=list(fia_pc.participants),
        statements=statements,
        scraped_at=(
            fia_pc.scraped_at
            if hasattr(fia_pc, "scraped_at")
            else datetime.utcnow().isoformat(timespec="seconds") + "Z"
        ),
    )


# ── 유효성 검사 ─────────────────────────────────────────────────────────────

def validate_press_conference(pc: PressConference) -> bool:
    """
    수집된 PressConference의 최소 유효성 검사.

    검사 항목:
      - statements 수가 0이면 경고
      - participants가 비어 있으면 경고

    Returns:
        True: 모든 검사 통과
        False: 하나 이상의 경고 발생 (수집 결과는 유지)
    """
    valid = True

    if len(pc.statements) == 0:
        logger.warning(
            "[Collector] 경고: statements가 0개입니다. "
            "gp=%s type=%s url=%s",
            pc.gp_name, pc.conference_type, pc.url,
        )
        valid = False

    if not pc.participants:
        logger.warning(
            "[Collector] 경고: participants가 비어 있습니다. "
            "gp=%s type=%s url=%s",
            pc.gp_name, pc.conference_type, pc.url,
        )
        valid = False

    if valid:
        logger.info(
            "[Collector] 유효성 검사 통과: %s %s (statements=%d, participants=%d)",
            pc.gp_name, pc.conference_type,
            len(pc.statements), len(pc.participants),
        )

    return valid


# ── 파일명 생성 ─────────────────────────────────────────────────────────────

def _make_filename(gp_name: str, conference_type: str, date_iso: str) -> str:
    """
    저장 파일명을 생성한다.

    패턴: {gp_slug}_{conference_type}_{date_iso}.json
    예: japanese_grand_prix_post-race_2026-03-29.json
    """
    gp_slug = re.sub(r"[^\w\s-]", "", gp_name.lower()).strip()
    gp_slug = re.sub(r"[\s]+", "_", gp_slug)
    conf_slug = re.sub(r"[^\w-]", "-", conference_type.lower()).strip("-")
    date_part = date_iso if date_iso else "unknown"
    return f"{gp_slug}_{conf_slug}_{date_part}.json"


# ── 오케스트레이터 ──────────────────────────────────────────────────────────

class Collector:
    """
    FIA(1차) + formula1.com(fallback) 수집기 오케스트레이터.

    사용 예::

        collector = Collector()

        # 신규 기자회견만 수집
        new_pcs = collector.collect_new(limit=5)

        # 특정 URL 수집
        pc = collector.collect_url("https://www.fia.com/news/...")
    """

    def __init__(
        self,
        raw_dir: "Path | str" = DEFAULT_RAW_DIR,
        fia_timeout: float = 20.0,
        f1_timeout: float = 20.0,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._fia_timeout = fia_timeout
        self._f1_timeout = f1_timeout

    # ── Public API ─────────────────────────────────────────────────────────

    def collect_new(
        self,
        limit: int = 10,
        conference_type: Optional[str] = None,
    ) -> list[PressConference]:
        """
        FIA 뉴스에서 신규 기자회견을 탐색하고 수집한다.

        Args:
            limit: 최대 탐색 URL 수
            conference_type: 특정 유형 필터 (예: "post-race")

        Returns:
            수집 성공한 PressConference 리스트 (신규 항목만)
        """
        logger.info("[Collector] 신규 기자회견 수집 시작 (limit=%d)", limit)

        # 1. 기존 수집 URL 목록 로드
        seen_urls = self._load_seen_urls()
        logger.info("[Collector] 기존 수집 URL: %d개", len(seen_urls))

        # 2. FIA에서 최신 URL 목록 탐색
        try:
            with URLFinder() as finder:
                conference_urls = finder.fetch_recent(limit=limit)
        except httpx.HTTPError as e:
            logger.error("[Collector] FIA URL 탐색 실패: %s", e)
            conference_urls = []

        if not conference_urls:
            logger.warning("[Collector] FIA에서 기자회견 URL을 찾지 못했습니다.")
            return []

        # 3. 유형 필터 적용
        if conference_type:
            conference_urls = [
                cu for cu in conference_urls
                if conference_type.lower() in cu.conference_type.lower()
            ]

        # 4. 신규 URL만 처리
        new_urls = [cu for cu in conference_urls if cu.url not in seen_urls]
        logger.info(
            "[Collector] 신규 URL: %d개 (전체 %d개 중)",
            len(new_urls), len(conference_urls),
        )

        if not new_urls:
            logger.info("[Collector] 수집할 신규 기자회견이 없습니다.")
            return []

        # 5. 각 URL 수집
        results: list[PressConference] = []
        for conf_url in new_urls:
            pc = self._collect_one(conf_url.url, conf_url)
            if pc is not None:
                results.append(pc)

        logger.info("[Collector] 수집 완료: %d개", len(results))
        return results

    def collect_url(
        self,
        url: str,
        force: bool = False,
    ) -> Optional[PressConference]:
        """
        특정 URL을 직접 수집한다.

        Args:
            url: FIA 또는 formula1.com 기자회견 URL
            force: True이면 이미 수집된 URL도 재수집

        Returns:
            PressConference 또는 None (실패 시)
        """
        if not force:
            seen_urls = self._load_seen_urls()
            if url in seen_urls:
                logger.info("[Collector] 이미 수집된 URL, 건너뜀: %s", url)
                return None

        return self._collect_one(url)

    # ── 내부 수집 로직 ──────────────────────────────────────────────────────

    def _collect_one(
        self,
        url: str,
        conf_url: Optional[ConferenceURL] = None,
    ) -> Optional[PressConference]:
        """
        단일 URL에 대해 FIA → formula1.com fallback 순으로 수집을 시도한다.

        Args:
            url: 수집 대상 URL
            conf_url: URLFinder에서 탐색된 ConferenceURL 객체 (메타 정보용)

        Returns:
            PressConference 또는 None
        """
        # FIA URL 여부 판단
        is_fia_url = "fia.com" in url

        pc: Optional[PressConference] = None

        # ── 1차: FIA 수집 ────────────────────────────────────────────────
        if is_fia_url:
            pc = self._try_fia(url)

        # ── 2차: formula1.com fallback ───────────────────────────────────
        if pc is None:
            logger.info("[Collector] formula1.com fallback 시도: %s", url)
            pc = self._try_f1_fallback(url, conf_url)

        if pc is None:
            logger.error("[Collector] 모든 수집 방법 실패: %s", url)
            return None

        # ── 유효성 검사 ──────────────────────────────────────────────────
        validate_press_conference(pc)

        # ── 저장 ─────────────────────────────────────────────────────────
        self._save(pc)

        return pc

    def _try_fia(self, url: str) -> Optional[PressConference]:
        """FIA 스크래퍼로 수집을 시도한다."""
        try:
            with FIAScraper(timeout=self._fia_timeout) as scraper:
                fia_pc = scraper.scrape(url)
            pc = _adapt_fia_pc(fia_pc)
            logger.info(
                "[Collector] FIA 수집 성공: %s %s (statements=%d)",
                pc.gp_name, pc.conference_type, len(pc.statements),
            )
            return pc
        except httpx.HTTPStatusError as e:
            logger.warning(
                "[Collector] FIA HTTP 오류 [%d], fallback으로 전환: %s",
                e.response.status_code, url,
            )
        except httpx.TimeoutException:
            logger.warning("[Collector] FIA 타임아웃, fallback으로 전환: %s", url)
        except httpx.RequestError as e:
            logger.warning("[Collector] FIA 네트워크 오류, fallback으로 전환: %s – %s", url, e)
        except ValueError as e:
            logger.warning("[Collector] FIA 파싱 실패, fallback으로 전환: %s – %s", url, e)
        except Exception as e:
            logger.error("[Collector] FIA 예상치 못한 오류: %s – %s", url, e)

        return None

    def _try_f1_fallback(
        self,
        original_url: str,
        conf_url: Optional[ConferenceURL],
    ) -> Optional[PressConference]:
        """
        formula1.com에서 동일한 기자회견을 탐색 후 수집을 시도한다.

        전략:
          1. ConferenceURL 메타 정보(gp_name, conference_type)로 URL 탐색
          2. 탐색 실패 시 latest 뉴스에서 유사 기자회견 탐색
        """
        gp_name = conf_url.gp_name if conf_url else None
        conference_type = conf_url.conference_type if conf_url else None

        try:
            with F1Scraper(timeout=self._f1_timeout) as scraper:
                # 특정 GP/유형으로 URL 탐색
                f1_urls = scraper.find_conference_urls(
                    gp_name=gp_name,
                    conference_type=conference_type,
                    limit=3,
                )

                if not f1_urls:
                    logger.warning(
                        "[Collector] formula1.com에서 URL을 찾지 못했습니다. "
                        "gp=%s type=%s",
                        gp_name, conference_type,
                    )
                    return None

                for f1_url in f1_urls:
                    try:
                        pc = scraper.scrape(f1_url)
                        logger.info(
                            "[Collector] formula1.com 수집 성공: %s %s (statements=%d)",
                            pc.gp_name, pc.conference_type, len(pc.statements),
                        )
                        return pc
                    except (httpx.HTTPError, ValueError) as e:
                        logger.warning(
                            "[Collector] formula1.com 수집 실패 (다음 URL 시도): %s – %s",
                            f1_url, e,
                        )

        except httpx.RequestError as e:
            logger.error("[Collector] formula1.com 네트워크 오류: %s", e)
        except Exception as e:
            logger.error("[Collector] formula1.com 예상치 못한 오류: %s", e)

        return None

    # ── 저장 ───────────────────────────────────────────────────────────────

    def _save(self, pc: PressConference) -> Path:
        """
        PressConference를 data/raw/{filename}.json 에 저장한다.

        파일명 패턴: {gp_slug}_{conference_type}_{date_iso}.json
        """
        filename = _make_filename(pc.gp_name, pc.conference_type, pc.date_iso)
        save_path = self.raw_dir / filename

        # 동일 파일 이미 존재하면 덮어쓰지 않음 (collect_url(force=True) 제외)
        if save_path.exists():
            logger.info("[Collector] 파일 이미 존재, 저장 건너뜀: %s", save_path)
            return save_path

        pc.to_json(str(save_path))
        logger.info("[Collector] 저장 완료: %s", save_path)
        return save_path

    def save_force(self, pc: PressConference) -> Path:
        """기존 파일이 있어도 강제 덮어쓰기 저장."""
        filename = _make_filename(pc.gp_name, pc.conference_type, pc.date_iso)
        save_path = self.raw_dir / filename
        pc.to_json(str(save_path))
        logger.info("[Collector] 강제 저장 완료: %s", save_path)
        return save_path

    # ── 중복 방지 ──────────────────────────────────────────────────────────

    def _load_seen_urls(self) -> set[str]:
        """
        data/raw/ 폴더의 기존 JSON 파일에서 이미 수집된 URL 집합을 반환한다.
        """
        seen: set[str] = set()
        for json_file in self.raw_dir.glob("*.json"):
            try:
                with open(json_file, encoding="utf-8") as f:
                    data = json.load(f)
                url = data.get("url", "")
                if url:
                    seen.add(url)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    "[Collector] 기존 파일 파싱 실패 (건너뜀): %s – %s", json_file, e
                )
        return seen

    def get_collected_urls(self) -> list[str]:
        """이미 수집된 URL 목록을 반환한다 (외부 참조용)."""
        return sorted(self._load_seen_urls())

    def load_all(self) -> list[PressConference]:
        """
        data/raw/ 폴더의 모든 JSON 파일을 PressConference로 로드한다.
        """
        results: list[PressConference] = []
        for json_file in sorted(self.raw_dir.glob("*.json")):
            try:
                pc = PressConference.from_json(str(json_file))
                results.append(pc)
            except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
                logger.warning(
                    "[Collector] 파일 로드 실패 (건너뜀): %s – %s", json_file, e
                )
        logger.info("[Collector] 로드 완료: %d개 파일", len(results))
        return results
