"""F1 Instagram 카드뉴스 자동화 — 공통 데이터 모델

수집기 → AI 파이프라인 → 렌더러 간 인터페이스를 정의한다.
모든 모듈은 이 파일의 데이터 모델만으로 통신한다.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, List
from datetime import datetime
import json

# 캐러셀 슬라이드 제한
MAX_BODY_SLIDES = 17   # 커버(1) + 본문(최대17) + 출처(1) = 19장 이내
SLIDE_MAX_CHARS = 200  # 슬라이드 1장 권장 최대 글자 수


# ─────────────────────────────────────────────
# 1. 수집기 출력 → AI 파이프라인 입력
# ─────────────────────────────────────────────

@dataclass
class Statement:
    """기자회견 개별 발언"""
    speaker: str          # "Max VERSTAPPEN"
    text: str             # 발언 원문 (영어)
    is_question: bool     # True면 기자 질문
    section: str = ""     # "TRACK INTERVIEWS", "PRESS CONFERENCE" 등
    seq: int = 0          # 발언 순서 번호


@dataclass
class PressConference:
    """기자회견 전체 데이터 (수집기 출력)"""
    url: str
    title: str
    date_str: str              # "29.03.26"
    date_iso: str              # "2026-03-29"
    gp_name: str               # "Japanese Grand Prix"
    conference_type: str       # "post-race", "friday", "qualifying" 등
    participants: List[str]    # ["Max VERSTAPPEN", "Lewis HAMILTON"]
    statements: List[Statement]
    scraped_at: str = ""

    def driver_statements(self) -> List[Statement]:
        """기자 질문을 제외한 드라이버/팀 대표 발언만 반환"""
        return [s for s in self.statements if not s.is_question]

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, path: str) -> "PressConference":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["statements"] = [Statement(**s) for s in data["statements"]]
        return cls(**data)


# ─────────────────────────────────────────────
# 2. AI 파이프라인 내부 모델
# ─────────────────────────────────────────────

@dataclass
class DriverInterview:
    """드라이버 1명의 선정된 Q&A 묶음 (선별 완료 후 번역 전)"""
    speaker: str              # "Max VERSTAPPEN"
    event_label: str          # "Japanese Grand Prix – post-race"
    qa_pairs: list            # [(question_text, answer_text), ...]  — 둘 다 영문 원문


@dataclass
class ScoredStatement:
    """1차 선별 결과 — 점수가 매겨진 발언"""
    speaker: str
    text: str
    score: int           # 0~10
    reason: str          # 평가 이유
    seq: int = 0


@dataclass
class SelectedQuote:
    """2차 선별 결과 — 최종 선정된 발언"""
    speaker: str
    text: str             # 영어 원문
    card_type: str        # "info" | "meme" | "highlight"
    score: int = 0
    seq: int = 0


@dataclass
class TranslatedQuote:
    """번역 완료된 발언"""
    speaker: str           # 영어 원문 이름
    speaker_kr: str        # "페르스타펜"
    text_en: str           # 영어 원문
    text_kr: str           # 한국어 번역
    card_type: str         # "info" | "meme"
    seq: int = 0


# ─────────────────────────────────────────────
# 3. AI 파이프라인 출력 → 렌더러 입력
# ─────────────────────────────────────────────

@dataclass
class InterviewSlide:
    """캐러셀 본문 슬라이드 1장 데이터"""
    slide_num: int         # 2부터 시작 (1은 커버)
    text_kr: str           # 한국어 번역 텍스트 (~80~150자)
    text_en: str = ""      # 원문 영어 텍스트 (선택, 참고용)
    slide_type: str = "answer"  # "question" | "answer"


@dataclass
class CarouselSet:
    """드라이버 1명의 캐러셀 게시물 전체 데이터"""
    # 식별 정보
    speaker: str           # "Max VERSTAPPEN"
    speaker_kr: str        # "막스 페르스타펜"
    team: str              # "Red Bull"
    gp_name: str           # "2026 일본 GP"
    gp_name_en: str        # "Japanese Grand Prix"
    conference_type: str   # "post-race"
    date_iso: str          # "2026-03-29"

    # 커버 카드 (슬라이드 1)
    cover_headline: str    # 15~25자 핵심 한 줄 요약

    # 본문 슬라이드 (슬라이드 2~N)
    slides: List[InterviewSlide] = field(default_factory=list)

    # 출처 슬라이드는 렌더러에서 자동 생성 (gp_name_en + date_iso 활용)

    # 메타
    total_cost_usd: float = 0.0
    created_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )

    @property
    def total_slides(self) -> int:
        """커버(1) + 본문 + 출처(1)"""
        return 1 + len(self.slides) + 1

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, path: str) -> "CarouselSet":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["slides"] = [InterviewSlide(**s) for s in data["slides"]]
        return cls(**data)


@dataclass
class CarouselBatch:
    """하나의 기자회견에서 생성된 드라이버별 캐러셀 묶음"""
    gp_name: str
    conference_type: str
    date_iso: str
    carousels: List[CarouselSet] = field(default_factory=list)
    total_cost_usd: float = 0.0
    created_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, path: str) -> "CarouselBatch":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        carousels = []
        for c in data.get("carousels", []):
            c["slides"] = [InterviewSlide(**s) for s in c.get("slides", [])]
            carousels.append(CarouselSet(**c))
        data["carousels"] = carousels
        return cls(**data)


# ─────────────────────────────────────────────
# 4. 드라이버 → 팀 매핑 (2026 시즌)
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# 4-a. 드라이버 한국어 표기 (인스타그램 게시물용 성 단독 표기)
# ─────────────────────────────────────────────

DRIVER_NAME_KR: dict[str, str] = {
    "Max VERSTAPPEN":     "베르스타펜",
    "Liam LAWSON":        "로슨",
    "Charles LECLERC":    "르클레르",
    "Lewis HAMILTON":     "해밀턴",
    "George RUSSELL":     "러셀",
    "Kimi ANTONELLI":     "안토넬리",
    "Lando NORRIS":       "노리스",
    "Oscar PIASTRI":      "피아스트리",
    "Fernando ALONSO":    "알론소",
    "Lance STROLL":       "스트롤",
    "Pierre GASLY":       "가슬리",
    "Franco COLAPINTO":   "콜라핀토",
    "Carlos SAINZ":       "사인츠",
    "Alexander ALBON":    "알본",
    "Isack HADJAR":       "하자르",
    "Arvid LINDBLAD":     "린드블라드",
    "Esteban OCON":       "오콘",
    "Oliver BEARMAN":     "베어먼",
    "Nico HULKENBERG":    "휠켄베르크",
    "Gabriel BORTOLETO":  "보톨레토",
}


def get_driver_name_kr(speaker: str) -> str:
    """드라이버 이름(영문)으로 한국어 성 표기를 반환한다.

    Args:
        speaker: 드라이버 이름 (예: "Kimi ANTONELLI")

    Returns:
        한국어 성 표기 (예: "안토넬리").
        매칭 실패 시 원본 이름 반환.
    """
    # 1) 정확한 매칭
    if speaker in DRIVER_NAME_KR:
        return DRIVER_NAME_KR[speaker]

    # 2) 성(Surname) 으로 매칭 — 대소문자 무시
    speaker_upper = speaker.upper()
    for name, kr in DRIVER_NAME_KR.items():
        surname = name.split()[-1].upper()
        if surname in speaker_upper:
            return kr

    # 3) 원본 이름 반환
    return speaker


# ─────────────────────────────────────────────
# 4-b. 드라이버 → 팀 매핑 (2026 시즌)
# ─────────────────────────────────────────────

DRIVER_TEAM_MAP: dict[str, str] = {
    # Red Bull
    "Max VERSTAPPEN": "Red Bull",
    "Liam LAWSON": "Red Bull",
    # Ferrari
    "Charles LECLERC": "Ferrari",
    "Lewis HAMILTON": "Ferrari",
    # Mercedes
    "George RUSSELL": "Mercedes",
    "Kimi ANTONELLI": "Mercedes",
    # McLaren
    "Lando NORRIS": "McLaren",
    "Oscar PIASTRI": "McLaren",
    # Aston Martin
    "Fernando ALONSO": "Aston Martin",
    "Lance STROLL": "Aston Martin",
    # Alpine
    "Pierre GASLY": "Alpine",
    "Franco COLAPINTO": "Alpine",
    # Williams
    "Carlos SAINZ": "Williams",
    "Alexander ALBON": "Williams",
    # RB (VCARB)
    "Isack HADJAR": "RB",
    "Arvid LINDBLAD": "RB",
    # Haas
    "Esteban OCON": "Haas",
    "Oliver BEARMAN": "Haas",
    # Sauber/Audi
    "Nico HULKENBERG": "Sauber/Audi",
    "Gabriel BORTOLETO": "Sauber/Audi",
}


def get_team_for_driver(speaker: str) -> str:
    """드라이버 이름으로 팀 조회. 매칭 실패 시 빈 문자열 반환."""
    # 정확한 매칭 시도
    if speaker in DRIVER_TEAM_MAP:
        return DRIVER_TEAM_MAP[speaker]

    # 약칭 또는 성만으로 매칭 시도
    speaker_upper = speaker.upper()
    for name, team in DRIVER_TEAM_MAP.items():
        surname = name.split()[-1]
        if surname in speaker_upper:
            return team

    return ""
