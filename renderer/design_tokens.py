"""
design_tokens.py
디자인 시스템 토큰 정의
- 폰트 경로, 공통 컬러, 카드 사이즈, 레이아웃 수치
- team_colors.json 로드 유틸리티 포함
"""

import json
import os
from pathlib import Path
from typing import Tuple

# ── 디렉토리 기준 경로 ──────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent          # renderer/
FONTS_DIR = BASE_DIR / "fonts"
TEAM_COLORS_FILE = BASE_DIR / "team_colors.json"

# ── 폰트 경로 ──────────────────────────────────────────────────────────────
FONTS = {
    # 헤드라인용 (드라이버명, 메인 카피)
    "bebas_neue": FONTS_DIR / "BebasNeue-Regular.ttf",
    # 본문용 (서브 카피, 인용구, 워터마크)
    "pretendard_bold":   FONTS_DIR / "Pretendard-Bold.ttf",
    "pretendard_medium": FONTS_DIR / "Pretendard-Medium.ttf",
    "pretendard_regular":FONTS_DIR / "Pretendard-Regular.ttf",
}

# ── 카드 사이즈 ─────────────────────────────────────────────────────────────
CARD = {
    "width":  1080,
    "height": 1080,
    # 인스타그램 세로형 (후속 확장용)
    "width_portrait":  1080,
    "height_portrait": 1350,
}

# ── 그리드 / 간격 ───────────────────────────────────────────────────────────
LAYOUT = {
    # 안전 영역 (인스타그램 UI 잘림 방지)
    "margin": 72,
    # 내부 콘텐츠 패딩
    "padding_h": 64,   # 수평
    "padding_v": 48,   # 수직
    # 컴포넌트 간 갭
    "gap_xs":  8,
    "gap_sm": 16,
    "gap_md": 24,
    "gap_lg": 40,
    # 액센트 바 (상단 팀 컬러 라인)
    "accent_bar_height": 8,
    # 인용구 박스 좌측 바 두께
    "quote_bar_width": 6,
    # 인용구 박스 패딩
    "quote_padding": 28,
}

# ── 타이포그래피 크기 스케일 (px) ───────────────────────────────────────────
FONT_SIZE = {
    "display":  80,   # 드라이버명 (Bebas Neue)
    "headline": 52,   # 메인 카피 (Bebas Neue)
    "title":    36,   # 섹션 제목
    "body_lg":  28,   # 서브 카피 (Pretendard Bold)
    "body":     22,   # 본문 (Pretendard Medium)
    "caption":  18,   # 캡션, 인용구 (Pretendard Regular)
    "micro":    14,   # 워터마크 (Pretendard Regular)
}

# ── 공통 컬러 팔레트 ────────────────────────────────────────────────────────
COLORS = {
    # 기본 배경 (팀 컬러 미적용 시)
    "bg_default":   "#0A0A0A",
    "bg_surface":   "#141414",
    "bg_elevated":  "#1E1E1E",

    # F1 시그니처
    "f1_red":       "#E8002D",
    "f1_white":     "#FFFFFF",

    # 텍스트
    "text_primary":    "#FFFFFF",
    "text_secondary":  "#AAAAAA",
    "text_disabled":   "#555555",

    # 공통 UI
    "divider":      "#333333",
    "overlay_dark": "#000000CC",   # 80% 불투명 검정 (오버레이용)

    # 인용구 박스 배경
    "quote_bg":     "#1C1C1C",
    "quote_border": "#2E2E2E",
}

# ── 알파 (투명도) 상수 ──────────────────────────────────────────────────────
ALPHA = {
    "accent_bar_overlay": 220,   # 0–255
    "quote_bg":           200,
    "watermark":          100,
}


# ── team_colors.json 로드 ───────────────────────────────────────────────────
def load_team_colors() -> dict:
    """
    team_colors.json 을 파싱하여 teams dict를 반환한다.

    Returns:
        dict: { "red_bull": { name, bg, accent, ... }, ... }

    Raises:
        FileNotFoundError: team_colors.json 파일이 없을 때
        KeyError: JSON 구조가 예상과 다를 때
    """
    if not TEAM_COLORS_FILE.exists():
        raise FileNotFoundError(
            f"team_colors.json을 찾을 수 없습니다: {TEAM_COLORS_FILE}"
        )
    with open(TEAM_COLORS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["teams"]


def get_team_color(team_key: str) -> dict:
    """
    특정 팀의 컬러 딕셔너리를 반환한다.

    Args:
        team_key: team_colors.json의 팀 키 (예: "red_bull", "ferrari")

    Returns:
        dict: { name, short_name, bg, accent, accent_secondary,
                text_primary, text_secondary }

    Raises:
        KeyError: 존재하지 않는 팀 키일 때
    """
    teams = load_team_colors()
    if team_key not in teams:
        available = ", ".join(teams.keys())
        raise KeyError(
            f"알 수 없는 팀 키: '{team_key}'. 사용 가능: {available}"
        )
    return teams[team_key]


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    """
    HEX 색상 문자열을 (R, G, B) 튜플로 변환한다.

    Args:
        hex_color: '#RRGGBB' 형식 문자열

    Returns:
        (r, g, b) int 튜플 (0–255)
    """
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def hex_to_rgba(hex_color: str, alpha: int = 255) -> Tuple[int, int, int, int]:
    """
    HEX 색상 문자열을 (R, G, B, A) 튜플로 변환한다.

    Args:
        hex_color: '#RRGGBB' 형식 문자열
        alpha: 0–255 투명도 (255=불투명)

    Returns:
        (r, g, b, a) int 튜플
    """
    r, g, b = hex_to_rgb(hex_color)
    return (r, g, b, alpha)


if __name__ == "__main__":
    # 빠른 동작 확인
    teams = load_team_colors()
    print(f"로드된 팀 수: {len(teams)}")
    for key, info in teams.items():
        print(f"  {key:15s} → accent: {info['accent']:8s}  bg: {info['bg']}")
