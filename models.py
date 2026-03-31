"""F1 Instagram 카드뉴스 자동화 — 공통 데이터 모델

수집기 → AI 파이프라인 → 렌더러 간 인터페이스를 정의한다.
모든 모듈은 이 파일의 데이터 모델만으로 통신한다.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, List
from datetime import datetime
import json


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
class CardContent:
    """카드 렌더링에 필요한 최종 데이터"""
    speaker_kr: str        # "페르스타펜"
    team: str              # "Red Bull" (team_colors.json 키와 매칭)
    main_copy: str         # 메인 카피 (25자 이내)
    sub_copy: str          # 서브 카피 (40자 이내)
    quote_en: str          # 영어 원문 인용 (카드 하단)
    card_type: str         # "info" | "meme"
    gp_name: str           # "2026 일본 GP"
    seq: int = 0           # 카드 순서

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CardSet:
    """하나의 기자회견에서 생성된 카드 세트"""
    gp_name: str
    conference_type: str
    date_iso: str
    cards: List[CardContent]
    total_cost_usd: float = 0.0
    created_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, path: str) -> "CardSet":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["cards"] = [CardContent(**c) for c in data["cards"]]
        return cls(**data)


# ─────────────────────────────────────────────
# 4. 드라이버 → 팀 매핑 (2026 시즌)
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
    "Jack DOOHAN": "Alpine",
    # Williams
    "Carlos SAINZ": "Williams",
    "Alexander ALBON": "Williams",
    # RB (VCARB)
    "Isack HADJAR": "RB",
    "Yuki TSUNODA": "RB",
    # Haas
    "Esteban OCON": "Haas",
    "Oliver BEARMAN": "Haas",
    # Sauber/Audi
    "Nico HULKENBERG": "Sauber/Audi",
    "Gabriel BORTOLETO": "Sauber/Audi",
    # Cadillac
    "Mario ANDRETTI": "Cadillac",  # TBD — 확정 시 업데이트
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
