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
SLIDE_MAX_CHARS = 210  # 슬라이드 1장 권장 최대 글자 수 (config.py와 동일)


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

    # 핵심 발언 (원카드용)
    key_quote: str = ""          # 핵심 발언 원문
    key_quote_context: str = ""  # 맥락 설명 (빈 문자열이면 표시 안 함)
    key_quote_theme: str = ""    # 테마 태그

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

# ─────────────────────────────────────────────
# 4-a-2. 드라이버 한국어 풀네임 표기 (인스타그램 커버 등 전체 이름 필요 시)
# pipeline.py의 _DRIVER_KO_MAP을 통합 — 단일 소스 관리
# ─────────────────────────────────────────────

DRIVER_FULLNAME_KR: dict[str, str] = {
    "Max VERSTAPPEN":     "막스 페르스타펜",
    "Liam LAWSON":        "리암 로슨",
    "Charles LECLERC":    "샤를 르클레르",
    "Lewis HAMILTON":     "루이스 해밀턴",
    "George RUSSELL":     "조지 러셀",
    "Kimi ANTONELLI":     "키미 안토넬리",
    "Lando NORRIS":       "란도 노리스",
    "Oscar PIASTRI":      "오스카 피아스트리",
    "Fernando ALONSO":    "페르난도 알론소",
    "Lance STROLL":       "랜스 스트롤",
    "Pierre GASLY":       "피에르 가슬리",
    "Franco COLAPINTO":   "프랑코 콜라핀토",
    "Carlos SAINZ":       "카를로스 사인츠",
    "Alexander ALBON":    "알렉산더 알본",
    "Isack HADJAR":       "이삭 아다르",
    "Arvid LINDBLAD":     "아르비드 린드블라드",
    "Esteban OCON":       "에스테반 오콘",
    "Oliver BEARMAN":     "올리버 베어먼",
    "Nico HULKENBERG":    "니코 휠켄베르크",
    "Gabriel BORTOLETO":  "가브리엘 보르톨레토",
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
    # Cadillac
    "Valtteri BOTTAS":   "Cadillac",
    "Sergio PEREZ":      "Cadillac",
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


# ─────────────────────────────────────────────
# 5. 팀 스태프 → 팀 매핑 (2026 시즌)
# ─────────────────────────────────────────────

TEAM_STAFF_MAP: dict[str, dict] = {
    # ── Red Bull ──────────────────────────────
    # TP: Laurent Mekies (크리스찬 호너 2025 중반 경질 후 승계)
    # TD: Pierre Waché
    "Red Bull": {
        "team_principal": "Laurent MEKIES",
        "technical_director": "Pierre WACHE",
        "staff": [
            "Laurent MEKIES",   # Team Principal & CEO (2025.07~)
            "Pierre WACHE",     # Technical Director
        ],
    },
    # ── Ferrari ──────────────────────────────
    # TP: Frédéric Vasseur
    # Chassis TD: Loïc Serra  /  PU TD: Enrico Gualtieri
    "Ferrari": {
        "team_principal": "Frederic VASSEUR",
        "technical_director": "Loic SERRA",
        "staff": [
            "Frederic VASSEUR",  # Team Principal & Managing Director
            "Loic SERRA",        # Chassis Technical Director
            "Enrico GUALTIERI",  # Power Unit Technical Director
        ],
    },
    # ── Mercedes ──────────────────────────────
    # TP: Toto Wolff  /  TD: James Allison
    "Mercedes": {
        "team_principal": "Toto WOLFF",
        "technical_director": "James ALLISON",
        "staff": [
            "Toto WOLFF",        # Team Principal & CEO
            "James ALLISON",     # Technical Director
            "Bradley LORD",      # Deputy Team Principal
        ],
    },
    # ── McLaren ──────────────────────────────
    # TP: Andrea Stella  /  CTO: Peter Prodromou  /  Chief Designer: Rob Marshall
    "McLaren": {
        "team_principal": "Andrea STELLA",
        "technical_director": "Peter PRODROMOU",
        "staff": [
            "Andrea STELLA",    # Team Principal
            "Peter PRODROMOU",  # Technical Director
            "Rob MARSHALL",     # Chief Designer
        ],
    },
    # ── Aston Martin ──────────────────────────
    # TP: Adrian Newey (2026~)  /  CSO: Andy Cowell
    "Aston Martin": {
        "team_principal": "Adrian NEWEY",
        "technical_director": "Adrian NEWEY",
        "staff": [
            "Adrian NEWEY",    # Team Principal & Managing Technical Partner (2026~)
            "Andy COWELL",     # Chief Strategy Officer (전 Team Principal)
        ],
    },
    # ── Alpine ──────────────────────────────
    # 공식 TP 없음 — Flavio Briatore(Executive Advisor) + Steve Nielsen(Managing Director)
    # ETD: David Sanchez
    "Alpine": {
        "team_principal": "Flavio BRIATORE",
        "technical_director": "David SANCHEZ",
        "staff": [
            "Flavio BRIATORE",  # Executive Advisor (실질 수장)
            "Steve NIELSEN",    # Managing Director
            "David SANCHEZ",    # Executive Technical Director
        ],
    },
    # ── Williams ──────────────────────────────
    # TP: James Vowles  /  CTO: Pat Fry
    "Williams": {
        "team_principal": "James VOWLES",
        "technical_director": "Pat FRY",
        "staff": [
            "James VOWLES",  # Team Principal
            "Pat FRY",       # Chief Technical Officer
        ],
    },
    # ── RB (Racing Bulls) ──────────────────────
    # TP: Alan Permane (2026.01~, 메키에스 레드불 승격 후)
    # TD: Dan Fallows  /  CTO: Tim Goss
    "RB": {
        "team_principal": "Alan PERMANE",
        "technical_director": "Dan FALLOWS",
        "staff": [
            "Alan PERMANE",  # Team Principal (2026.01~)
            "Dan FALLOWS",   # Technical Director
            "Tim GOSS",      # Chief Technical Officer
        ],
    },
    # ── Haas ──────────────────────────────
    # TP: Ayao Komatsu  /  TD: Andrea De Zordo
    "Haas": {
        "team_principal": "Ayao KOMATSU",
        "technical_director": "Andrea DE ZORDO",
        "staff": [
            "Ayao KOMATSU",      # Team Principal
            "Andrea DE ZORDO",   # Technical Director
        ],
    },
    # ── Sauber/Audi ──────────────────────────
    # Jonathan Wheatley 2026 개막 2라운드 후 사임 → Mattia Binotto가 TP 겸임
    # TD: James Key
    "Sauber/Audi": {
        "team_principal": "Mattia BINOTTO",
        "technical_director": "James KEY",
        "staff": [
            "Mattia BINOTTO",    # Head of Audi F1 Project + Team Principal (겸임, 2026~)
            "James KEY",         # Technical Director
        ],
    },
    # ── Cadillac ──────────────────────────────
    # TP: Graeme Lowdon  /  CTO: Nick Chester  /  Exec. Consultant: Pat Symonds
    "Cadillac": {
        "team_principal": "Graeme LOWDON",
        "technical_director": "Nick CHESTER",
        "staff": [
            "Graeme LOWDON",  # Team Principal
            "Nick CHESTER",   # Chief Technical Officer
            "Pat SYMONDS",    # Executive Engineering Consultant
        ],
    },
}

# ─────────────────────────────────────────────
# 5-b. 스태프 이름 → 팀 역방향 매핑 (자동 생성)
# ─────────────────────────────────────────────

STAFF_TEAM_MAP: dict[str, str] = {}
for _team, _info in TEAM_STAFF_MAP.items():
    for _name in _info["staff"]:
        STAFF_TEAM_MAP[_name] = _team

# ─────────────────────────────────────────────
# 5-c. 팀 스태프 한국어 표기 (성 단독, 인스타그램 게시물용)
# ─────────────────────────────────────────────

STAFF_NAME_KR: dict[str, str] = {
    # Red Bull
    "Laurent MEKIES":    "메키에스",
    "Pierre WACHE":      "바셰",
    # Ferrari
    "Frederic VASSEUR":  "바쉐르",
    "Loic SERRA":        "세라",
    "Enrico GUALTIERI":  "과리에리",
    # Mercedes
    "Toto WOLFF":        "볼프",
    "James ALLISON":     "앨리슨",
    "Bradley LORD":      "로드",
    # McLaren
    "Andrea STELLA":     "스텔라",
    "Peter PRODROMOU":   "프로드로무",
    "Rob MARSHALL":      "마샬",
    # Aston Martin
    "Adrian NEWEY":      "뉴이",
    "Andy COWELL":       "카우엘",
    # Alpine
    "Flavio BRIATORE":   "브리아토레",
    "Steve NIELSEN":     "닐슨",
    "David SANCHEZ":     "산체스",
    # Williams
    "James VOWLES":      "바울스",
    "Pat FRY":           "프라이",
    # RB
    "Alan PERMANE":      "퍼먼",
    "Dan FALLOWS":       "팔로스",
    "Tim GOSS":          "고스",
    # Haas
    "Ayao KOMATSU":      "코마쓰",
    "Andrea DE ZORDO":   "데 조르도",
    # Sauber/Audi
    "Mattia BINOTTO":    "비노토",
    "James KEY":         "키",
    # Cadillac
    "Graeme LOWDON":     "로든",
    "Nick CHESTER":      "체스터",
    "Pat SYMONDS":       "사이먼즈",
}


def get_team_for_speaker(speaker: str) -> str:
    """드라이버 또는 팀 스태프 이름으로 팀 조회. 매칭 실패 시 빈 문자열 반환.

    탐색 순서:
      1) 드라이버 정확 매칭 (DRIVER_TEAM_MAP)
      2) 스태프 정확 매칭 (STAFF_TEAM_MAP)
      3) 드라이버 성(Surname) 퍼지 매칭
      4) 스태프 성(Surname) 퍼지 매칭
    """
    # 1) 드라이버 정확 매칭
    if speaker in DRIVER_TEAM_MAP:
        return DRIVER_TEAM_MAP[speaker]

    # 2) 스태프 정확 매칭
    if speaker in STAFF_TEAM_MAP:
        return STAFF_TEAM_MAP[speaker]

    speaker_upper = speaker.upper()

    # 3) 드라이버 성 퍼지 매칭
    for name, team in DRIVER_TEAM_MAP.items():
        surname = name.split()[-1].upper()
        if surname in speaker_upper:
            return team

    # 4) 스태프 성 퍼지 매칭
    for name, team in STAFF_TEAM_MAP.items():
        surname = name.split()[-1].upper()
        if surname and surname in speaker_upper:
            return team

    return ""


def get_speaker_name_kr(speaker: str) -> str:
    """드라이버 또는 팀 스태프 이름(영문)으로 한국어 성 표기를 반환한다.

    Args:
        speaker: 영문 이름 (예: "Toto WOLFF", "Max VERSTAPPEN")

    Returns:
        한국어 성 표기 (예: "볼프", "베르스타펜").
        매칭 실패 시 원본 이름 반환.
    """
    # 1) 드라이버 정확 매칭
    if speaker in DRIVER_NAME_KR:
        return DRIVER_NAME_KR[speaker]

    # 2) 스태프 정확 매칭
    if speaker in STAFF_NAME_KR:
        return STAFF_NAME_KR[speaker]

    speaker_upper = speaker.upper()

    # 3) 드라이버 성 퍼지 매칭
    for name, kr in DRIVER_NAME_KR.items():
        surname = name.split()[-1].upper()
        if surname in speaker_upper:
            return kr

    # 4) 스태프 성 퍼지 매칭
    for name, kr in STAFF_NAME_KR.items():
        surname = name.split()[-1].upper()
        if surname and surname in speaker_upper:
            return kr

    return speaker
