"""
carousel_renderer.py
Pillow 기반 F1 인터뷰 캐러셀 렌더러 (1080×1350 / 4:5 표준)

슬라이드 구성:
  1. 커버 카드   — 팀 그라데이션 배경 + 대형 차 번호 + 드라이버명 + 핵심 요약
  2~N. 본문 카드 — 인터뷰 한국어 번역 텍스트 + 큰 따옴표 장식
  N+1. 출처 카드 — GP명 / 날짜 / 워터마크

공개 함수:
  render_cover(driver_kr, team, car_number, gp_name, summary) → Image
  render_interview_slide(driver_kr, team, text, page_num, total_pages) → Image
  render_source(gp_name, date, source_text) → Image
  render_carousel(carousel_data) → list[Image]
  save_carousel(images, output_dir) → list[str]
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ── 경로 설정 ────────────────────────────────────────────────────────────────
# renderer/ 와 프로젝트 루트를 모두 sys.path에 추가한다.
# renderer/__init__.py 가 card_renderer → models 순으로 임포트하므로,
# 프로젝트 루트를 먼저 삽입하여 models.py 를 찾을 수 있도록 한다.
_RENDERER_DIR = Path(__file__).parent
_PROJECT_DIR  = _RENDERER_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from renderer.design_tokens import (
    ALPHA,
    CARD,
    CAROUSEL,
    CAROUSEL_TYPO,
    COLORS,
    FONT_SIZE,
    FONTS,
    LAYOUT,
    LINE_SPACING,
    darken_color,
    get_driver_number,
    get_team_color,
    hex_to_rgb,
    hex_to_rgba,
    lighten_color,
    make_vertical_gradient,
)

# ── 상수 ────────────────────────────────────────────────────────────────────
W = CAROUSEL["width"]    # 1080
H = CAROUSEL["height"]   # 1350

MARGIN        = LAYOUT["margin"]          # 72  — 안전 영역 여백
PAD_H         = LAYOUT["padding_h"]       # 64  — 좌우 콘텐츠 패딩
PAD_V         = LAYOUT["padding_v"]       # 48  — 상하 콘텐츠 패딩
ACCENT_H      = 6                         # 상단/하단 팀 컬러 바 높이 (px)
CONTENT_W     = W - PAD_H * 2            # 실제 콘텐츠 너비 (952 px)

WATERMARK_TEXT = "@f1presskr"

# ── 폰트 캐시 ────────────────────────────────────────────────────────────────
_font_cache: dict[str, ImageFont.FreeTypeFont] = {}


def _font(key: str, size: int) -> ImageFont.FreeTypeFont:
    """폰트를 캐시에서 불러오거나 로드해 반환한다."""
    cache_key = f"{key}_{size}"
    if cache_key not in _font_cache:
        path = FONTS.get(key)
        if path and Path(path).exists():
            _font_cache[cache_key] = ImageFont.truetype(str(path), size)
        else:
            print(f"[경고] 폰트 없음: {path} — PIL 기본 폰트 사용")
            _font_cache[cache_key] = ImageFont.load_default()
    return _font_cache[cache_key]


# ── 팀 키 변환 ───────────────────────────────────────────────────────────────
_TEAM_KEY_MAP: dict[str, str] = {
    "Red Bull":       "red_bull",
    "Ferrari":        "ferrari",
    "Mercedes":       "mercedes",
    "McLaren":        "mclaren",
    "Aston Martin":   "aston_martin",
    "Alpine":         "alpine",
    "Williams":       "williams",
    "RB":             "rb",
    "Haas":           "haas",
    "Sauber/Audi":    "sauber_audi",
    "Cadillac":       "cadillac",
}


def _resolve_team_key(team: str) -> str:
    """팀 표시명을 team_colors.json 키로 변환한다."""
    if team in _TEAM_KEY_MAP:
        return _TEAM_KEY_MAP[team]
    tl = team.lower()
    for display, key in _TEAM_KEY_MAP.items():
        if display.lower() in tl or tl in display.lower():
            return key
    print(f"[경고] 알 수 없는 팀 '{team}' — red_bull fallback")
    return "red_bull"


# ── 텍스트 유틸 ─────────────────────────────────────────────────────────────

def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """
    max_width(px) 안에 들어오도록 텍스트를 줄바꿈해 리스트로 반환한다.
    한글/영어 혼용: 단어 단위 우선, 초과 시 글자 단위 분리.
    """
    words = text.split()
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            # 단어 자체가 너비 초과 → 글자 단위 분리
            if draw.textbbox((0, 0), word, font=font)[2] > max_width:
                sub = ""
                for ch in word:
                    test = sub + ch
                    if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
                        sub = test
                    else:
                        lines.append(sub)
                        sub = ch
                current = sub
            else:
                current = word

    if current:
        lines.append(current)
    return lines or [""]


def _draw_multiline(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    x: int,
    y: int,
    max_width: int,
    fill: tuple,
    line_spacing: int = 12,
    align: str = "left",
) -> int:
    """
    줄바꿈 텍스트를 그리고 블록 하단 y 좌표를 반환한다.

    Args:
        align: "left" | "center" | "right"
    """
    lines = _wrap_text(draw, text, font, max_width)
    cy = y
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_h = bbox[3] - bbox[1]
        line_w = bbox[2] - bbox[0]
        if align == "center":
            dx = x + (max_width - line_w) // 2
        elif align == "right":
            dx = x + max_width - line_w
        else:
            dx = x
        draw.text((dx, cy), line, font=font, fill=fill)
        cy += line_h + line_spacing
    return cy


def _fit_font(
    draw: ImageDraw.ImageDraw,
    font_key: str,
    text: str,
    max_width: int,
    max_height: int,
    start_size: int,
    min_size: int = 16,
    line_spacing: int = 12,
) -> Tuple[ImageFont.FreeTypeFont, int]:
    """텍스트가 영역 안에 들어오도록 폰트 크기를 자동으로 줄인다."""
    size = start_size
    while size >= min_size:
        font = _font(font_key, size)
        lines = _wrap_text(draw, text, font, max_width)
        total_h = 0
        for ln in lines:
            bbox = draw.textbbox((0, 0), ln, font=font)
            total_h += (bbox[3] - bbox[1]) + line_spacing
        if total_h <= max_height:
            return font, size
        size -= 2
    return _font(font_key, min_size), min_size


# ── 공통 레이어 그리기 ───────────────────────────────────────────────────────

def _draw_noise(img: Image.Image, intensity: int = 50_000, seed: int = 42) -> None:
    """
    미세 노이즈 텍스처를 RGBA 레이어로 합성한다.
    재현 가능한 시드로 항상 동일한 패턴을 생성.
    """
    noise = Image.new("RGBA", img.size, (0, 0, 0, 0))
    px = noise.load()
    rng = random.Random(seed)
    for _ in range(intensity):
        nx = rng.randint(0, img.width - 1)
        ny = rng.randint(0, img.height - 1)
        v  = rng.randint(180, 255)
        a  = rng.randint(3, 10)
        px[nx, ny] = (v, v, v, a)
    img.paste(noise, mask=noise)


def _draw_team_bar(
    draw: ImageDraw.ImageDraw,
    accent_rgb: Tuple[int, int, int],
    position: str = "top",
    height: int = ACCENT_H,
) -> None:
    """팀 컬러 수평 바를 상단 또는 하단에 그린다."""
    if position == "top":
        rect = [(0, 0), (W, height)]
    else:
        rect = [(0, H - height), (W, H)]
    draw.rectangle(rect, fill=accent_rgb)


def _draw_watermark(
    draw: ImageDraw.ImageDraw,
    text: str = WATERMARK_TEXT,
    fill: tuple = (255, 255, 255, 80),
) -> None:
    """우측 하단 워터마크를 그린다."""
    _cw = CAROUSEL_TYPO["cover_watermark"]
    font = _font(_cw["font"], _cw["size"])
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(
        (W - PAD_H - tw, H - PAD_V - 28),
        text,
        font=font,
        fill=fill,
    )


def _draw_team_chip(
    draw: ImageDraw.ImageDraw,
    team_short: str,
    accent_rgb: Tuple[int, int, int],
    x: int,
    y: int,
) -> Tuple[int, int]:
    """
    팀명 칩(둥근 사각형 + 팀 컬러)을 그리고 (우측 끝 x, 하단 y)를 반환한다.
    """
    font = _font("pretendard_bold", FONT_SIZE["body"])
    bbox = draw.textbbox((0, 0), team_short, font=font)
    # bbox 오프셋 보정 (폰트에 따라 (0,0)에서 시작하지 않을 수 있음)
    tx_off = bbox[0]
    ty_off = bbox[1]
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad_x, pad_y = 18, 10
    chip_w = tw + pad_x * 2
    chip_h = th + pad_y * 2
    # 배경 사각형
    draw.rounded_rectangle(
        [(x, y), (x + chip_w, y + chip_h)],
        radius=6,
        fill=(*accent_rgb, 220),
    )
    # 팀명 텍스트 — bbox 오프셋 보정으로 칩 내부 정중앙 배치
    text_x = x + pad_x - tx_off
    text_y = y + pad_y - ty_off
    draw.text((text_x, text_y), team_short, font=font, fill=(255, 255, 255, 255))
    return (x + chip_w, y + chip_h)


# ── 커버 카드 ────────────────────────────────────────────────────────────────

_CONFERENCE_TYPE_KR: dict[str, str] = {
    "post-race": "결승 인터뷰",
    "post-qualifying": "예선 인터뷰",
    "qualifying": "예선 인터뷰",
    "friday": "금요일 기자회견",
    "thursday": "목요일 기자회견",
    "pre-race": "레이스 전 기자회견",
}


def render_cover(
    driver_kr: str,
    team: str,
    car_number: str = "",
    gp_name: str = "",
    summary: str = "",
    conference_type: str = "",
) -> Image.Image:
    """
    1장 커버 카드를 렌더링한다.

    Args:
        driver_kr:       드라이버 한글 이름 (예: "루이스 해밀턴")
        team:            팀 표시명 (예: "Ferrari")
        car_number:      차량 번호 문자열. 빈 문자열이면 자동 조회.
        gp_name:         GP 이름 (예: "2026 일본 GP")
        summary:         핵심 한 줄 요약 (예: "새 차, 완전히 다른 느낌")
        conference_type: 기자회견 종류 (예: "post-race", "post-qualifying")

    Returns:
        PIL.Image (RGBA, 1080×1350)
    """
    team_key = _resolve_team_key(team)
    tc = get_team_color(team_key)

    bg_rgb     = hex_to_rgb(tc["bg"])
    accent_rgb = hex_to_rgb(tc["accent"])
    accent_sec = hex_to_rgb(tc.get("accent_secondary", tc["accent"]))

    # 차 번호 자동 조회
    if not car_number:
        car_number = get_driver_number(driver_kr) or get_driver_number(driver_kr.upper())

    # ── 배경: 팀 컬러 수직 그라데이션 ──────────────────────────────────────
    # 상단: bg 약간 밝게 (깊이감) → 하단: bg 살짝 더 밝게
    top_color    = darken_color(bg_rgb, 0.0)          # 원색
    bottom_color = lighten_color(bg_rgb, 0.12)        # 12% 밝게
    img = make_vertical_gradient(W, H, top_color, bottom_color).convert("RGBA")

    # 미세 노이즈 텍스처
    _draw_noise(img, intensity=55_000)

    draw = ImageDraw.Draw(img, "RGBA")

    # ── 상단 팀 컬러 바 ─────────────────────────────────────────────────────
    _draw_team_bar(draw, accent_rgb, position="top", height=ACCENT_H)

    # ── 차 번호: 초대형 반투명 타이포 (배경 우하단 레이어) ────────────────────
    # 레이어 순서: 배경 → 차 번호(반투명) → 나머지 텍스트
    # 드라이버명과 겹치지 않도록 우하단 배치, 일부가 캔버스 밖으로 나감
    if car_number:
        _cn = CAROUSEL_TYPO["cover_number"]
        num_font = _font(_cn["font"], _cn["size"])
        num_bbox = draw.textbbox((0, 0), car_number, font=num_font)
        nw = num_bbox[2] - num_bbox[0]
        nh = num_bbox[3] - num_bbox[1]
        # 우하단 배치: 드라이버명 아래 영역, 우측 정렬
        nx = W - nw - PAD_H + 20
        ny = H // 2 + 40
        draw.text(
            (nx, ny),
            car_number,
            font=num_font,
            fill=(*accent_rgb, 22),   # ~8% 투명도 — 더 은은하게
        )

    # ── 상단 영역: 팀 칩 + GP 정보 ─────────────────────────────────────────
    top_y   = ACCENT_H + PAD_V
    chip_x  = PAD_H
    chip_ex, chip_ey = _draw_team_chip(draw, tc["short_name"], accent_rgb, chip_x, top_y)

    if gp_name:
        # GP명 + 기자회견 종류를 함께 표시
        conf_label = _CONFERENCE_TYPE_KR.get(conference_type, "") if conference_type else ""
        gp_display = f"{gp_name}  {conf_label}".rstrip() if conf_label else gp_name
        _cg = CAROUSEL_TYPO["cover_gp"]
        gp_font = _font(_cg["font"], _cg["size"])
        gp_x    = chip_ex + 16
        gp_bbox = draw.textbbox((0, 0), gp_display, font=gp_font)
        gp_th   = gp_bbox[3] - gp_bbox[1]
        gp_y    = top_y + (chip_ey - top_y - gp_th) // 2
        draw.text(
            (gp_x, gp_y), gp_display, font=gp_font,
            fill=(*hex_to_rgb(tc["text_secondary"]), 200),
        )

    # ── 드라이버 풀네임 (Pretendard Bold — Bebas Neue는 한글 미지원) ─────────
    _cd = CAROUSEL_TYPO["cover_driver"]
    name_font, _ = _fit_font(
        draw, _cd["font"], driver_kr,
        max_width=CONTENT_W,
        max_height=200,
        start_size=_cd["max"],
        min_size=_cd["min"],
        line_spacing=_cd["spacing"],
    )
    name_y = H // 2 - 110
    name_end_y = _draw_multiline(
        draw, driver_kr, name_font,
        x=PAD_H, y=name_y,
        max_width=CONTENT_W,
        fill=(255, 255, 255, 255),
        line_spacing=_cd["spacing"],
        align="left",
    )

    # ── 구분 라인 ────────────────────────────────────────────────────────────
    divider_y = name_end_y + 20
    draw.line(
        [(PAD_H, divider_y), (PAD_H + CONTENT_W, divider_y)],
        fill=(*accent_rgb, 130),
        width=2,
    )

    # ── 스와이프 유도 텍스트 y좌표 미리 계산 (오버플로 방어용) ─────────────
    _cs = CAROUSEL_TYPO["cover_swipe"]
    swipe_font = _font(_cs["font"], _cs["size"])
    swipe_text = "인터뷰 전문 →"
    swipe_bbox = draw.textbbox((0, 0), swipe_text, font=swipe_font)
    swipe_h    = swipe_bbox[3] - swipe_bbox[1]
    # 하단 바(ACCENT_H) + 여백 20px 위에 딱 붙임
    swipe_y    = H - ACCENT_H - swipe_h - 20

    # ── 핵심 요약 (큰 한 줄, 따옴표 없이 텍스트만) ──────────────────────────
    if summary:
        # 따옴표 문자 제거 후 출력
        summary_clean = summary.strip().strip('"').strip("'").strip('\u201c').strip('\u201d')
        _csm = CAROUSEL_TYPO["cover_summary"]
        sum_font, sum_size = _fit_font(
            draw, _csm["font"], summary_clean,
            max_width=CONTENT_W,
            max_height=220,
            start_size=_csm["max"],
            min_size=_csm["min"],
            line_spacing=_csm["spacing"],
        )
        sum_y = divider_y + 32
        sum_end_y = _draw_multiline(
            draw, summary_clean, sum_font,
            x=PAD_H, y=sum_y,
            max_width=CONTENT_W,
            fill=(255, 255, 255, 240),
            line_spacing=_csm["spacing"],
            align="left",
        )

        # 요약 렌더링 후 y좌표가 스와이프 텍스트에 근접하면 폰트 축소 후 재렌더링
        _csr = CAROUSEL_TYPO["cover_summary_reduced"]
        if sum_end_y + 40 > swipe_y:
            reduced_size = max(sum_size - 8, _csr["min"])
            sum_font, _ = _fit_font(
                draw, _csr["font"], summary_clean,
                max_width=CONTENT_W,
                max_height=swipe_y - sum_y - 40,
                start_size=reduced_size,
                min_size=_csr["min"],
                line_spacing=_csr["spacing"],
            )
            # 배경 패치로 이전 텍스트 덮기
            draw.rectangle(
                [(PAD_H - 4, sum_y - 4), (PAD_H + CONTENT_W + 4, swipe_y - 2)],
                fill=(*top_color, 255),
            )
            _draw_multiline(
                draw, summary_clean, sum_font,
                x=PAD_H, y=sum_y,
                max_width=CONTENT_W,
                fill=(255, 255, 255, 240),
                line_spacing=_csr["spacing"],
                align="left",
            )
    draw.text(
        (PAD_H, swipe_y),
        swipe_text,
        font=swipe_font,
        fill=(*accent_rgb, 200),
    )

    # ── 워터마크 ─────────────────────────────────────────────────────────────
    # 스와이프 텍스트 우측에 같은 높이로 배치
    _cw = CAROUSEL_TYPO["cover_watermark"]
    wm_font = _font(_cw["font"], _cw["size"])
    wm_bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=wm_font)
    wm_w    = wm_bbox[2] - wm_bbox[0]
    wm_h    = wm_bbox[3] - wm_bbox[1]
    wm_y    = H - ACCENT_H - wm_h - 20
    draw.text(
        (W - PAD_H - wm_w, wm_y),
        WATERMARK_TEXT,
        font=wm_font,
        fill=(255, 255, 255, 70),
    )

    # ── 하단 팀 컬러 바 ─────────────────────────────────────────────────────
    _draw_team_bar(draw, accent_rgb, position="bottom", height=ACCENT_H)

    return img.convert("RGBA")


# ── 인터뷰 본문 카드 ─────────────────────────────────────────────────────────

def render_interview_slide(
    driver_kr: str,
    team: str,
    text: str,
    page_num: int,
    total_pages: int,
) -> Image.Image:
    """
    인터뷰 본문 슬라이드를 렌더링한다.

    Args:
        driver_kr:   드라이버 한글 이름
        team:        팀 표시명
        text:        한국어 번역 인터뷰 텍스트
        page_num:    현재 페이지 번호 (2부터 시작)
        total_pages: 전체 페이지 수 (커버 + 본문 N장 + 출처)

    Returns:
        PIL.Image (RGBA, 1080×1350)
    """
    team_key = _resolve_team_key(team)
    tc = get_team_color(team_key)

    bg_rgb     = hex_to_rgb(tc["bg"])
    accent_rgb = hex_to_rgb(tc["accent"])
    text_sec   = hex_to_rgb(tc["text_secondary"])

    # ── 배경: 커버보다 약간 다른 톤 (본문은 더 어둡게) ─────────────────────
    top_color    = darken_color(bg_rgb, 0.05)
    bottom_color = lighten_color(bg_rgb, 0.07)
    img = make_vertical_gradient(W, H, top_color, bottom_color).convert("RGBA")

    _draw_noise(img, intensity=48_000, seed=page_num * 7)

    draw = ImageDraw.Draw(img, "RGBA")

    # ── 코너 글로우: 좌하단에 accent 컬러 소프트 원형 ──────────────────────
    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    glow_r = 480
    for step in range(8):
        t      = step / 7
        radius = int(glow_r * (1 - t * 0.55))
        alpha  = int(28 * (1 - t))   # 바깥으로 갈수록 투명
        cx, cy = -60, H + 60         # 좌하단 코너 밖
        gd.ellipse(
            [(cx - radius, cy - radius), (cx + radius, cy + radius)],
            fill=(*accent_rgb, alpha),
        )
    img.paste(glow_layer, mask=glow_layer)
    draw = ImageDraw.Draw(img, "RGBA")   # draw 객체 갱신

    # ── 상단 팀 컬러 바 ─────────────────────────────────────────────────────
    _draw_team_bar(draw, accent_rgb, position="top", height=ACCENT_H)

    # ── 상단 헤더: 드라이버명(작게) + dot 페이지 인디케이터 ─────────────────
    _ih = CAROUSEL_TYPO["interview_header"]
    _id = CAROUSEL_TYPO["interview_dot"]
    header_y    = ACCENT_H + 22
    driver_font = _font(_ih["font"], _ih["size"])
    draw.text(
        (PAD_H, header_y),
        driver_kr,
        font=driver_font,
        fill=(*text_sec, 180),
    )

    # dot 인디케이터: ●●○○○ (현재 슬라이드 = 채움, 나머지 = 빈 원)
    # 10장 초과 시 "현재/전체" 숫자 표기로 전환 (넘침 방지)
    # page_num: 커버=1, 본문 시작=2 / total_pages: 커버+본문+출처
    dot_filled   = "\u25cf"   # ●
    dot_empty    = "\u25cb"   # ○
    dot_font     = _font(_id["font"], _id["size"])
    # 본문 슬라이드 수 = total_pages - 2 (커버, 출처 제외)
    body_count   = total_pages - 2
    current_body = page_num - 1   # 본문 1번째 = 1
    if body_count > 10:
        dots = f"{current_body}/{body_count}"
    else:
        dots = ""
        for i in range(1, body_count + 1):
            dots += dot_filled if i == current_body else dot_empty
            if i < body_count:
                dots += " "
    dot_bbox = draw.textbbox((0, 0), dots, font=dot_font)
    dot_w    = dot_bbox[2] - dot_bbox[0]
    dot_h    = dot_bbox[3] - dot_bbox[1]
    # 드라이버명과 수직 중앙 정렬
    name_bbox = draw.textbbox((0, 0), driver_kr, font=driver_font)
    name_h    = name_bbox[3] - name_bbox[1]
    dot_y     = header_y + (name_h - dot_h) // 2
    draw.text(
        (W - PAD_H - dot_w, dot_y),
        dots,
        font=dot_font,
        fill=(*text_sec, 180),
    )

    # 헤더 하단 구분선 (얇게)
    header_line_y = header_y + name_h + 16
    draw.line(
        [(PAD_H, header_line_y), (W - PAD_H, header_line_y)],
        fill=(*accent_rgb, 55),
        width=1,
    )

    # ── 큰 따옴표 장식 ───────────────────────────────────────────────────────
    # 텍스트보다 먼저 그려서 텍스트가 그 위에 올라오도록 (배경 레이어)
    _iq = CAROUSEL_TYPO["interview_quote"]
    quote_deco_font = _font(_iq["font"], _iq["size"])
    q_bbox          = draw.textbbox((0, 0), "\u201c", font=quote_deco_font)
    q_w             = q_bbox[2] - q_bbox[0]
    q_h             = q_bbox[3] - q_bbox[1]

    # 따옴표 위치: 헤더 구분선 바로 아래, 좌측 여백 안쪽
    quote_x = PAD_H - 8
    quote_y = header_line_y + 16
    draw.text(
        (quote_x, quote_y),
        "\u201c",
        font=quote_deco_font,
        fill=(*accent_rgb, 50),
    )

    # ── 텍스트 영역 정의 ─────────────────────────────────────────────────────
    # 따옴표 우측으로 충분히 밀어서 첫 글자 가림 방지 (+34px indent)
    text_indent = q_w + 24          # 따옴표 너비 + 여유
    text_x      = PAD_H + text_indent
    text_top    = header_line_y + 52
    text_w      = CONTENT_W - text_indent
    text_bottom = H - ACCENT_H - 100
    text_max_h  = text_bottom - text_top

    _ib = CAROUSEL_TYPO["interview_body"]
    body_font, body_size = _fit_font(
        draw, _ib["font"], text,
        max_width=text_w,
        max_height=text_max_h,
        start_size=_ib["max"],
        min_size=_ib["min"],
        line_spacing=_ib["spacing"],
    )

    # 텍스트 블록 높이 계산 → 수직 중앙 배치
    lines = _wrap_text(draw, text, body_font, text_w)
    block_h = 0
    for ln in lines:
        ln_bbox = draw.textbbox((0, 0), ln, font=body_font)
        block_h += (ln_bbox[3] - ln_bbox[1]) + _ib["spacing"]
    text_y = text_top + max(0, (text_max_h - block_h) // 3)

    # ── 본문 텍스트 (따옴표 위에 렌더링) ─────────────────────────────────────
    _draw_multiline(
        draw, text, body_font,
        x=text_x, y=text_y,
        max_width=text_w,
        fill=(255, 255, 255, 248),
        line_spacing=_ib["spacing"],
        align="left",
    )

    # ── 워터마크 (하단 우측) ─────────────────────────────────────────────────
    _draw_watermark(draw)

    # ── 하단 팀 컬러 바 ─────────────────────────────────────────────────────
    _draw_team_bar(draw, accent_rgb, position="bottom", height=ACCENT_H)

    return img.convert("RGBA")


# ── Q 슬라이드 (질문 카드) ─────────────────────────────────────────────────

def render_question_slide(
    driver_kr: str,
    team: str,
    question_text: str,
    page_num: int,
    total_pages: int,
) -> Image.Image:
    """
    Q(질문) 슬라이드를 렌더링한다.

    디자인 특징:
    - 배경: 팀 accent 색상 기반 (약간 어둡게)
    - "Q." 마커: 좌상단에 크게, 대비색(흰색)
    - 질문 텍스트: Bold, 40~44px, 2~3줄 이내
    - 하단: 드라이버명 + 페이지 인디케이터

    Args:
        driver_kr:     드라이버 한글 이름
        team:          팀 표시명
        question_text: 질문 텍스트 (한국어)
        page_num:      현재 페이지 번호
        total_pages:   전체 페이지 수

    Returns:
        PIL.Image (RGBA, 1080x1350)
    """
    team_key = _resolve_team_key(team)
    tc = get_team_color(team_key)

    accent_rgb = hex_to_rgb(tc["accent"])
    accent_sec = hex_to_rgb(tc.get("accent_secondary", tc["accent"]))

    # ── 배경: accent 색상을 어둡게 한 그라데이션 ─────────────────────────────
    # darken 비율 0.30 (기존 0.55보다 밝아져 Q슬라이드 시각적 차별화 강화)
    bg_base = darken_color(accent_rgb, 0.30)
    top_color = darken_color(bg_base, 0.10)
    bottom_color = lighten_color(bg_base, 0.08)
    img = make_vertical_gradient(W, H, top_color, bottom_color).convert("RGBA")

    # 미세 노이즈
    _draw_noise(img, intensity=45_000, seed=page_num * 13)

    draw = ImageDraw.Draw(img, "RGBA")

    # ── 상단 accent 바: Q슬라이드는 12px (A슬라이드 6px보다 두꺼워 시각 차별화) ──
    _draw_team_bar(draw, accent_rgb, position="top", height=12)

    # ── 배경 장식: 초대형 반투명 "Q" (우측 하단에 깔리는 레이어) ────────────
    _qb = CAROUSEL_TYPO["q_bg"]
    q_bg_font = _font(_qb["font"], _qb["size"])
    q_bg_text = "Q"
    q_bg_bbox = draw.textbbox((0, 0), q_bg_text, font=q_bg_font)
    q_bg_w = q_bg_bbox[2] - q_bg_bbox[0]
    q_bg_h = q_bg_bbox[3] - q_bg_bbox[1]
    draw.text(
        (W - q_bg_w + 40, H - q_bg_h - 40),
        q_bg_text,
        font=q_bg_font,
        fill=(255, 255, 255, 18),
    )

    # ── 전경: "Q." 마커 (좌측 상단, 선명하게) ──────────────────────────────
    _qm = CAROUSEL_TYPO["q_marker"]
    q_marker_font = _font(_qm["font"], _qm["size"])
    q_marker_text = "Q."
    q_marker_bbox = draw.textbbox((0, 0), q_marker_text, font=q_marker_font)
    q_marker_h = q_marker_bbox[3] - q_marker_bbox[1]
    q_marker_y = PAD_V + 40
    draw.text(
        (PAD_H, q_marker_y),
        q_marker_text,
        font=q_marker_font,
        fill=(255, 255, 255, 255),
    )

    # ── 구분선 (Q. 마커 아래, 짧은 바) ──────────────────────────────────────
    sep_y = q_marker_y + q_marker_h + 20
    draw.line(
        [(PAD_H, sep_y), (PAD_H + 100, sep_y)],
        fill=(255, 255, 255, 140),
        width=3,
    )

    # ── 질문 텍스트 영역 (수직 중앙 부근에 배치) ────────────────────────────
    text_top = sep_y + 60
    text_w = CONTENT_W
    text_max_h = H - text_top - 200  # 하단 영역 확보

    _qt = CAROUSEL_TYPO["q_text"]
    body_font, body_size = _fit_font(
        draw, _qt["font"], question_text,
        max_width=text_w,
        max_height=text_max_h,
        start_size=_qt["max"],
        min_size=_qt["min"],
        line_spacing=_qt["spacing"],
    )

    # 텍스트 블록 높이를 먼저 계산하여 수직 중앙 보정
    lines = _wrap_text(draw, question_text, body_font, text_w)
    block_h = 0
    for ln in lines:
        ln_bbox = draw.textbbox((0, 0), ln, font=body_font)
        block_h += (ln_bbox[3] - ln_bbox[1]) + _qt["spacing"]
    # 사용 가능한 영역의 수직 중앙
    available_h = H - text_top - 200
    text_y = text_top + max(0, (available_h - block_h) // 3)

    _draw_multiline(
        draw, question_text, body_font,
        x=PAD_H, y=text_y,
        max_width=text_w,
        fill=(255, 255, 255, 248),
        line_spacing=_qt["spacing"],
        align="left",
    )

    # ── 하단: 드라이버명 + dot 인디케이터 ────────────────────────────────────
    _qf = CAROUSEL_TYPO["q_footer"]
    _qfd = CAROUSEL_TYPO["q_footer_dot"]
    footer_y = H - ACCENT_H - 60
    driver_font = _font(_qf["font"], _qf["size"])
    draw.text(
        (PAD_H, footer_y),
        driver_kr,
        font=driver_font,
        fill=(255, 255, 255, 180),
    )

    # dot 인디케이터 (10장 초과 시 숫자 표기로 전환)
    dot_filled = "\u25cf"
    dot_empty  = "\u25cb"
    dot_font   = _font(_qfd["font"], _qfd["size"])
    body_count = total_pages - 2
    current_body = page_num - 1
    if body_count > 10:
        dots = f"{current_body}/{body_count}"
    else:
        dots = ""
        for i in range(1, body_count + 1):
            dots += dot_filled if i == current_body else dot_empty
            if i < body_count:
                dots += " "
    dot_bbox = draw.textbbox((0, 0), dots, font=dot_font)
    dot_w = dot_bbox[2] - dot_bbox[0]
    name_bbox = draw.textbbox((0, 0), driver_kr, font=driver_font)
    name_h = name_bbox[3] - name_bbox[1]
    dot_h = dot_bbox[3] - dot_bbox[1]
    dot_y = footer_y + (name_h - dot_h) // 2
    draw.text(
        (W - PAD_H - dot_w, dot_y),
        dots,
        font=dot_font,
        fill=(255, 255, 255, 160),
    )

    # ── 하단 accent 바: Q슬라이드는 12px ────────────────────────────────────
    _draw_team_bar(draw, accent_rgb, position="bottom", height=12)

    return img.convert("RGBA")


# ── A 슬라이드 (답변 카드) ─────────────────────────────────────────────────

def render_answer_slide(
    driver_kr: str,
    team: str,
    answer_text: str,
    page_num: int,
    total_pages: int,
) -> Image.Image:
    """
    A(답변) 슬라이드를 렌더링한다.

    디자인 특징:
    - 배경: 다크 (기존 본문 슬라이드와 유사)
    - 여는 따옴표 장식: 좌상단에 작게, 팀 accent 컬러
    - 텍스트: Medium, 32~36px
    - 하단: 드라이버명 + 페이지 인디케이터

    Args:
        driver_kr:    드라이버 한글 이름
        team:         팀 표시명
        answer_text:  답변 텍스트 (한국어, 80~120자)
        page_num:     현재 페이지 번호
        total_pages:  전체 페이지 수

    Returns:
        PIL.Image (RGBA, 1080x1350)
    """
    team_key = _resolve_team_key(team)
    tc = get_team_color(team_key)

    bg_rgb     = hex_to_rgb(tc["bg"])
    accent_rgb = hex_to_rgb(tc["accent"])
    text_sec   = hex_to_rgb(tc["text_secondary"])

    # ── 배경: 다크 그라데이션 ────────────────────────────────────────────────
    top_color    = darken_color(bg_rgb, 0.05)
    bottom_color = lighten_color(bg_rgb, 0.07)
    img = make_vertical_gradient(W, H, top_color, bottom_color).convert("RGBA")

    _draw_noise(img, intensity=48_000, seed=page_num * 7)

    draw = ImageDraw.Draw(img, "RGBA")

    # ── 코너 글로우: 좌하단에 accent 컬러 소프트 원형 ────────────────────────
    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    glow_r = 480
    for step in range(8):
        t      = step / 7
        radius = int(glow_r * (1 - t * 0.55))
        alpha  = int(28 * (1 - t))
        cx, cy = -60, H + 60
        gd.ellipse(
            [(cx - radius, cy - radius), (cx + radius, cy + radius)],
            fill=(*accent_rgb, alpha),
        )
    img.paste(glow_layer, mask=glow_layer)
    draw = ImageDraw.Draw(img, "RGBA")

    # ── 상단 팀 컬러 바 ──────────────────────────────────────────────────────
    _draw_team_bar(draw, accent_rgb, position="top", height=ACCENT_H)

    # ── 상단 헤더: 드라이버명(작게) + dot 페이지 인디케이터 ──────────────────
    _ah = CAROUSEL_TYPO["a_header"]
    _ad = CAROUSEL_TYPO["a_dot"]
    header_y    = ACCENT_H + 22
    driver_font = _font(_ah["font"], _ah["size"])
    draw.text(
        (PAD_H, header_y),
        driver_kr,
        font=driver_font,
        fill=(*text_sec, 180),
    )

    # dot 인디케이터 (10장 초과 시 숫자 표기로 전환)
    dot_filled   = "\u25cf"
    dot_empty    = "\u25cb"
    dot_font     = _font(_ad["font"], _ad["size"])
    body_count   = total_pages - 2
    current_body = page_num - 1
    if body_count > 10:
        dots = f"{current_body}/{body_count}"
    else:
        dots = ""
        for i in range(1, body_count + 1):
            dots += dot_filled if i == current_body else dot_empty
            if i < body_count:
                dots += " "
    dot_bbox = draw.textbbox((0, 0), dots, font=dot_font)
    dot_w    = dot_bbox[2] - dot_bbox[0]
    dot_h    = dot_bbox[3] - dot_bbox[1]
    name_bbox = draw.textbbox((0, 0), driver_kr, font=driver_font)
    name_h    = name_bbox[3] - name_bbox[1]
    dot_y     = header_y + (name_h - dot_h) // 2
    draw.text(
        (W - PAD_H - dot_w, dot_y),
        dots,
        font=dot_font,
        fill=(*text_sec, 180),
    )

    # 헤더 하단 구분선
    header_line_y = header_y + name_h + 16
    draw.line(
        [(PAD_H, header_line_y), (W - PAD_H, header_line_y)],
        fill=(*accent_rgb, 55),
        width=1,
    )

    # ── "A." 마커 + 따옴표 장식 ──────────────────────────────────────────────
    # 큰 반투명 따옴표 (배경 장식)
    _aq = CAROUSEL_TYPO["a_quote"]
    quote_deco_font = _font(_aq["font"], _aq["size"])
    q_char = "\u201c"
    q_bbox = draw.textbbox((0, 0), q_char, font=quote_deco_font)
    q_w = q_bbox[2] - q_bbox[0]

    quote_x = PAD_H - 6
    quote_y = header_line_y + 12
    draw.text(
        (quote_x, quote_y),
        q_char,
        font=quote_deco_font,
        fill=(*accent_rgb, 45),
    )

    # 작은 "A." 마커 (눈에 보이는 것)
    _am = CAROUSEL_TYPO["a_marker"]
    a_marker_font = _font(_am["font"], _am["size"])
    a_marker_text = "A."
    a_bbox = draw.textbbox((0, 0), a_marker_text, font=a_marker_font)
    a_h = a_bbox[3] - a_bbox[1]
    a_y = header_line_y + 28
    draw.text(
        (PAD_H, a_y),
        a_marker_text,
        font=a_marker_font,
        fill=(*accent_rgb, 200),
    )

    # ── 텍스트 영역 ─────────────────────────────────────────────────────────
    text_top   = a_y + a_h + 28
    text_w     = CONTENT_W
    text_bottom = H - ACCENT_H - 80
    text_max_h = text_bottom - text_top

    _at = CAROUSEL_TYPO["a_text"]
    body_font, body_size = _fit_font(
        draw, _at["font"], answer_text,
        max_width=text_w,
        max_height=text_max_h,
        start_size=_at["max"],
        min_size=_at["min"],
        line_spacing=_at["spacing"],
    )

    # 텍스트 블록 높이 계산 → 수직 중앙 배치
    lines = _wrap_text(draw, answer_text, body_font, text_w)
    block_h = 0
    for ln in lines:
        ln_bbox = draw.textbbox((0, 0), ln, font=body_font)
        block_h += (ln_bbox[3] - ln_bbox[1]) + _at["spacing"]
    text_y = text_top + max(0, (text_max_h - block_h) // 3)

    # ── 본문 텍스트 ─────────────────────────────────────────────────────────
    _draw_multiline(
        draw, answer_text, body_font,
        x=PAD_H, y=text_y,
        max_width=text_w,
        fill=(255, 255, 255, 240),
        line_spacing=_at["spacing"],
        align="left",
    )

    # ── 워터마크 ─────────────────────────────────────────────────────────────
    _draw_watermark(draw)

    # ── 하단 팀 컬러 바 ──────────────────────────────────────────────────────
    _draw_team_bar(draw, accent_rgb, position="bottom", height=ACCENT_H)

    return img.convert("RGBA")


# ── 출처 카드 ────────────────────────────────────────────────────────────────

def render_source(
    gp_name: str,
    date: str,
    source_text: str = "FIA Official Press Conference",
    team: str = "",
) -> Image.Image:
    """
    마지막 출처 카드를 렌더링한다.

    Args:
        gp_name:     GP 이름 (예: "2026 일본 그랑프리")
        date:        날짜 문자열 (예: "2026.03.29")
        source_text: 출처 텍스트 (기본: "FIA Official Press Conference")
        team:        팀 컬러 적용 (빈 문자열이면 기본 다크 테마)

    Returns:
        PIL.Image (RGBA, 1080×1350)
    """
    # 팀 컬러 또는 기본 다크
    if team:
        team_key = _resolve_team_key(team)
        tc       = get_team_color(team_key)
        bg_rgb   = hex_to_rgb(tc["bg"])
        accent_rgb = hex_to_rgb(tc["accent"])
    else:
        bg_rgb     = hex_to_rgb(COLORS["bg_default"])
        accent_rgb = hex_to_rgb(COLORS["f1_red"])

    top_color    = darken_color(bg_rgb, 0.08)
    bottom_color = lighten_color(bg_rgb, 0.06)
    img = make_vertical_gradient(W, H, top_color, bottom_color).convert("RGBA")

    _draw_noise(img, intensity=40_000, seed=99)

    draw = ImageDraw.Draw(img, "RGBA")

    # ── 상단 팀 컬러 바 ─────────────────────────────────────────────────────
    _draw_team_bar(draw, accent_rgb, position="top", height=ACCENT_H)

    # ── 중앙 글로우 (크기 축소 — 콘텐츠 가독성 우선) ───────────────────────
    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    for step in range(5):
        t      = step / 4
        radius = int(220 * (1 - t * 0.5))   # 420 → 220 으로 축소
        alpha  = int(16 * (1 - t))            # 22 → 16 으로 약화
        cx, cy = W // 2, H // 2
        gd.ellipse(
            [(cx - radius, cy - radius), (cx + radius, cy + radius)],
            fill=(*accent_rgb, alpha),
        )
    img.paste(glow_layer, mask=glow_layer)
    draw = ImageDraw.Draw(img, "RGBA")

    # ── 상단 팀 컬러 바 ─────────────────────────────────────────────────────
    _draw_team_bar(draw, accent_rgb, position="top", height=ACCENT_H)

    # ── 중앙 콘텐츠 블록 (수직 집중 배치) ───────────────────────────────────
    # 전체 블록 높이를 계산해서 정중앙 정렬
    # 구성: "• SOURCE •" + 구분선 + 출처 텍스트 + GP명(52px) + 날짜
    BLOCK_GAP = 28   # 요소 간 간격

    # 1) "• SOURCE •" 레이블
    _sl = CAROUSEL_TYPO["source_label"]
    src_label_font = _font(_sl["font"], _sl["size"])
    src_label      = "\u2022 SOURCE \u2022"
    src_lb_bbox    = draw.textbbox((0, 0), src_label, font=src_label_font)
    src_lb_h       = src_lb_bbox[3] - src_lb_bbox[1]
    src_lb_w       = src_lb_bbox[2] - src_lb_bbox[0]

    # 2) 출처 텍스트 Bebas Neue 높이 예측
    _st = CAROUSEL_TYPO["source_text"]
    src_font, _ = _fit_font(
        draw, _st["font"], source_text,
        max_width=CONTENT_W,
        max_height=160,
        start_size=_st["max"],
        min_size=_st["min"],
        line_spacing=_st["spacing"],
    )
    src_lines = _wrap_text(draw, source_text, src_font, CONTENT_W)
    src_block_h = sum(
        draw.textbbox((0, 0), ln, font=src_font)[3]
        - draw.textbbox((0, 0), ln, font=src_font)[1] + _st["spacing"]
        for ln in src_lines
    )

    # 3) GP명 높이 — _fit_font로 자동 크기 조정 (긴 GP명 넘침 방지)
    _sg = CAROUSEL_TYPO["source_gp"]
    gp_font, _ = _fit_font(
        draw, _sg["font"], gp_name,
        max_width=CONTENT_W,
        max_height=80,
        start_size=_sg["max"],
        min_size=_sg["min"],
        line_spacing=_sg["spacing"],
    )
    gp_bbox    = draw.textbbox((0, 0), gp_name, font=gp_font)
    gp_h       = gp_bbox[3] - gp_bbox[1]

    # 4) 날짜 높이
    _sd = CAROUSEL_TYPO["source_date"]
    date_font  = _font(_sd["font"], _sd["size"])
    date_bbox  = draw.textbbox((0, 0), date, font=date_font)
    date_h     = date_bbox[3] - date_bbox[1]

    sep_h = 1   # 구분선 높이

    total_block_h = (
        src_lb_h + BLOCK_GAP
        + sep_h + 16          # 구분선 + 여백
        + src_block_h + BLOCK_GAP
        + gp_h + BLOCK_GAP
        + date_h
    )

    # 정중앙 시작 y
    block_start_y = (H - total_block_h) // 2

    cy = block_start_y

    # "• SOURCE •"
    draw.text(
        ((W - src_lb_w) // 2, cy),
        src_label,
        font=src_label_font,
        fill=(*accent_rgb, 170),
    )
    cy += src_lb_h + BLOCK_GAP

    # 가는 구분선
    draw.line(
        [(W // 2 - 60, cy), (W // 2 + 60, cy)],
        fill=(*accent_rgb, 90),
        width=1,
    )
    cy += 16

    # 출처 텍스트
    cy = _draw_multiline(
        draw, source_text, src_font,
        x=PAD_H, y=cy,
        max_width=CONTENT_W,
        fill=(255, 255, 255, 230),
        line_spacing=_st["spacing"],
        align="center",
    )
    cy += BLOCK_GAP

    # GP 이름 (자동 크기: 52px 시작, 최소 34px)
    gp_w = gp_bbox[2] - gp_bbox[0]
    draw.text(
        ((W - gp_w) // 2, cy),
        gp_name,
        font=gp_font,
        fill=(255, 255, 255, 225),
    )
    cy += gp_h + BLOCK_GAP

    # 날짜
    date_w = date_bbox[2] - date_bbox[0]
    draw.text(
        ((W - date_w) // 2, cy),
        date,
        font=date_font,
        fill=(190, 190, 190, 180),
    )

    # ── 계정 워터마크 (중앙 하단, 바 바로 위) ────────────────────────────────
    _sw = CAROUSEL_TYPO["source_watermark"]
    wm_font = _font(_sw["font"], _sw["size"])
    wm_bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=wm_font)
    wm_w    = wm_bbox[2] - wm_bbox[0]
    wm_h    = wm_bbox[3] - wm_bbox[1]
    draw.text(
        ((W - wm_w) // 2, H - ACCENT_H - wm_h - 28),
        WATERMARK_TEXT,
        font=wm_font,
        fill=(*accent_rgb, 210),
    )

    # ── 하단 팀 컬러 바 ─────────────────────────────────────────────────────
    _draw_team_bar(draw, accent_rgb, position="bottom", height=ACCENT_H)

    return img.convert("RGBA")


# ── 원카드: 핵심 발언 카드 (1080×1080) ──────────────────────────────────────

_CARD_W = CARD["width"]    # 1080
_CARD_H = CARD["height"]   # 1080

def render_quote_card(
    driver_kr: str,
    team: str,
    quote: str,
    context: str = "",
    gp_name: str = "",
    conference_type: str = "",
) -> Image.Image:
    """
    핵심 발언 원카드(1080×1080)를 렌더링한다.

    Args:
        driver_kr:       드라이버 한글 이름
        team:            팀 표시명
        quote:           핵심 발언 원문 (30~80자)
        context:         맥락 설명 (하단 표시)
        gp_name:         GP 이름
        conference_type: 기자회견 종류

    Returns:
        PIL.Image (RGBA, 1080×1080)
    """
    team_key = _resolve_team_key(team)
    tc = get_team_color(team_key)

    bg_rgb     = hex_to_rgb(tc["bg"])
    accent_rgb = hex_to_rgb(tc["accent"])

    # ── 배경: 팀 컬러 기반 다크 그라데이션 ──────────────────────────────────
    top_color    = darken_color(bg_rgb, 0.03)
    bottom_color = lighten_color(bg_rgb, 0.08)
    img = make_vertical_gradient(_CARD_W, _CARD_H, top_color, bottom_color).convert("RGBA")

    _draw_noise(img, intensity=50_000)

    draw = ImageDraw.Draw(img, "RGBA")

    # ── 상단 팀 컬러 바 ────────────────────────────────────────────────────
    draw.rectangle([(0, 0), (_CARD_W, ACCENT_H)], fill=accent_rgb)

    # ── 큰 따옴표 장식 (배경 레이어) ───────────────────────────────────────
    _qd = CAROUSEL_TYPO["quote_card_deco"]
    deco_font = _font(_qd["font"], _qd["size"])
    deco_char = "\u201c"
    deco_bbox = draw.textbbox((0, 0), deco_char, font=deco_font)
    deco_w = deco_bbox[2] - deco_bbox[0]
    deco_h = deco_bbox[3] - deco_bbox[1]
    draw.text(
        (PAD_H - 16, 120),
        deco_char,
        font=deco_font,
        fill=(*accent_rgb, 35),
    )

    # ── 핵심 발언 텍스트 (중앙 영역) ──────────────────────────────────────
    _qt = CAROUSEL_TYPO["quote_card_text"]
    text_top    = 200
    text_bottom = _CARD_H - 340
    text_max_h  = text_bottom - text_top

    body_font, _ = _fit_font(
        draw, _qt["font"], quote,
        max_width=CONTENT_W,
        max_height=text_max_h,
        start_size=_qt["max"],
        min_size=_qt["min"],
        line_spacing=_qt["spacing"],
    )

    # 텍스트 블록 높이 → 수직 1/3 배치
    lines = _wrap_text(draw, quote, body_font, CONTENT_W)
    block_h = 0
    for ln in lines:
        ln_bbox = draw.textbbox((0, 0), ln, font=body_font)
        block_h += (ln_bbox[3] - ln_bbox[1]) + _qt["spacing"]
    text_y = text_top + max(0, (text_max_h - block_h) // 3)

    _draw_multiline(
        draw, quote, body_font,
        x=PAD_H, y=text_y,
        max_width=CONTENT_W,
        fill=(255, 255, 255, 245),
        line_spacing=_qt["spacing"],
        align="left",
    )

    # ── 닫는 따옴표 장식 ───────────────────────────────────────────────────
    close_font = _font(_qd["font"], 120)
    close_char = "\u201d"
    close_bbox = draw.textbbox((0, 0), close_char, font=close_font)
    close_w = close_bbox[2] - close_bbox[0]
    # 텍스트 블록 끝 우측에 배치
    draw.text(
        (_CARD_W - PAD_H - close_w + 8, text_y + block_h - 20),
        close_char,
        font=close_font,
        fill=(*accent_rgb, 60),
    )

    # ── 구분선 ─────────────────────────────────────────────────────────────
    sep_y = _CARD_H - 300
    draw.line(
        [(PAD_H, sep_y), (PAD_H + 100, sep_y)],
        fill=(*accent_rgb, 140),
        width=2,
    )

    # ── 드라이버명 + 팀명 ──────────────────────────────────────────────────
    _qdr = CAROUSEL_TYPO["quote_card_driver"]
    _qtm = CAROUSEL_TYPO["quote_card_team"]
    driver_font = _font(_qdr["font"], _qdr["size"])
    team_font   = _font(_qtm["font"], _qtm["size"])

    driver_y = sep_y + 20
    draw.text(
        (PAD_H, driver_y),
        driver_kr,
        font=driver_font,
        fill=(255, 255, 255, 240),
    )
    driver_bbox = draw.textbbox((0, 0), driver_kr, font=driver_font)
    driver_w = driver_bbox[2] - driver_bbox[0]
    # 팀명을 드라이버명 우측에 간격 두고 배치
    draw.text(
        (PAD_H + driver_w + 16, driver_y + 4),
        tc["short_name"],
        font=team_font,
        fill=(*accent_rgb, 200),
    )

    # ── GP 정보 ────────────────────────────────────────────────────────────
    _qgp = CAROUSEL_TYPO["quote_card_gp"]
    gp_font = _font(_qgp["font"], _qgp["size"])
    conf_label = _CONFERENCE_TYPE_KR.get(conference_type, "") if conference_type else ""
    gp_display = f"{gp_name}  {conf_label}".rstrip() if conf_label else gp_name
    if gp_display:
        gp_y = driver_y + 40
        draw.text(
            (PAD_H, gp_y),
            gp_display,
            font=gp_font,
            fill=(180, 180, 180, 160),
        )

    # ── 맥락 설명 (하단) ──────────────────────────────────────────────────
    if context:
        _qc = CAROUSEL_TYPO["quote_card_context"]
        ctx_font = _font(_qc["font"], _qc["size"])
        ctx_y = _CARD_H - 180
        # 맥락 텍스트를 2줄 이내로
        ctx_lines = _wrap_text(draw, context, ctx_font, CONTENT_W)
        ctx_lines = ctx_lines[:2]  # 최대 2줄
        for i, ln in enumerate(ctx_lines):
            ln_bbox = draw.textbbox((0, 0), ln, font=ctx_font)
            ln_h = ln_bbox[3] - ln_bbox[1]
            draw.text(
                (PAD_H, ctx_y + i * (ln_h + 8)),
                ln,
                font=ctx_font,
                fill=(*accent_rgb, 150),
            )

    # ── 워터마크 (하단 우측) ───────────────────────────────────────────────
    _qw = CAROUSEL_TYPO["quote_card_watermark"]
    wm_font = _font(_qw["font"], _qw["size"])
    wm_bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=wm_font)
    wm_w = wm_bbox[2] - wm_bbox[0]
    wm_h = wm_bbox[3] - wm_bbox[1]
    draw.text(
        (_CARD_W - PAD_H - wm_w, _CARD_H - ACCENT_H - wm_h - 20),
        WATERMARK_TEXT,
        font=wm_font,
        fill=(255, 255, 255, 70),
    )

    # ── 하단 팀 컬러 바 ────────────────────────────────────────────────────
    draw.rectangle([(0, _CARD_H - ACCENT_H), (_CARD_W, _CARD_H)], fill=accent_rgb)

    return img.convert("RGBA")


# ── 캐러셀 전체 렌더링 ───────────────────────────────────────────────────────

def render_carousel(carousel_data: dict) -> List[Image.Image]:
    """
    캐러셀 전체 슬라이드를 렌더링하여 이미지 리스트를 반환한다.

    Q/A 분리 모드와 레거시 모드를 모두 지원한다.

    Args:
        carousel_data: 아래 구조의 딕셔너리:
        {
            "driver_kr":    str,          # 드라이버 한글 이름
            "team":         str,          # 팀 표시명 (예: "Ferrari")
            "car_number":   str,          # 차량 번호 (선택, 빈 문자열이면 자동)
            "gp_name":      str,          # GP 이름 (예: "2026 일본 GP")
            "summary":      str,          # 커버 핵심 요약 문장
            "date":         str,          # 날짜 문자열
            "source_text":  str,          # 출처 텍스트 (선택)

            # Q/A 분리 모드 (신규):
            "slides": list[dict],         # [{"text_kr", "slide_type"}, ...]
                                          #  slide_type: "question" | "answer"

            # 레거시 모드:
            "interview_texts": list[str], # 본문 슬라이드별 텍스트 리스트
        }

    Returns:
        list[Image.Image]: [커버, Q/A 슬라이드들..., 출처]
    """
    driver_kr       = carousel_data.get("driver_kr", "")
    team            = carousel_data.get("team", "Red Bull")
    car_number      = carousel_data.get("car_number", "")
    gp_name         = carousel_data.get("gp_name", "")
    conference_type = carousel_data.get("conference_type", "")
    summary         = carousel_data.get("summary", "")
    date            = carousel_data.get("date", "")
    source_text     = carousel_data.get("source_text", "FIA Official Press Conference")

    # Q/A 분리 모드 vs 레거시 모드 판별
    slides_data  = carousel_data.get("slides", [])
    legacy_texts = carousel_data.get("interview_texts", [])

    if slides_data:
        # Q/A 분리 모드
        body_count = len(slides_data)
    else:
        # 레거시 모드
        body_count = len(legacy_texts)

    total_pages = 1 + body_count + 1   # 커버 + 본문 + 출처

    images: List[Image.Image] = []

    # 1. 커버 카드
    images.append(render_cover(driver_kr, team, car_number, gp_name, summary, conference_type))

    if slides_data:
        # Q/A 분리 모드: slide_type에 따라 다른 렌더러 호출
        for i, slide in enumerate(slides_data):
            page_num = i + 2
            slide_type = slide.get("slide_type", "answer")
            text_kr = slide.get("text_kr", "")

            if slide_type == "question":
                images.append(
                    render_question_slide(
                        driver_kr, team, text_kr, page_num, total_pages
                    )
                )
            else:
                images.append(
                    render_answer_slide(
                        driver_kr, team, text_kr, page_num, total_pages
                    )
                )
    else:
        # 레거시 모드: 기존 render_interview_slide 사용
        for i, text in enumerate(legacy_texts):
            page_num = i + 2
            images.append(
                render_interview_slide(driver_kr, team, text, page_num, total_pages)
            )

    # 마지막. 출처 카드
    images.append(render_source(gp_name, date, source_text, team))

    return images


# ── 저장 유틸 ────────────────────────────────────────────────────────────────

def save_carousel(
    images: List[Image.Image],
    output_dir: str,
    prefix: str = "slide",
) -> List[str]:
    """
    이미지 리스트를 PNG 파일로 저장하고 경로 리스트를 반환한다.

    Args:
        images:     render_carousel() 반환값
        output_dir: 저장 디렉토리 경로
        prefix:     파일명 접두사 (기본: "slide")

    Returns:
        list[str]: 저장된 파일 절대 경로 리스트
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    saved: List[str] = []
    for idx, img in enumerate(images):
        # 슬라이드 번호: 01, 02, ... (2자리 패딩)
        fname = f"{prefix}_{idx + 1:02d}.png"
        fpath = out / fname
        # RGBA → RGB 변환 후 저장 (PNG는 RGBA 지원하지만 일관성을 위해 RGB로)
        img.convert("RGB").save(str(fpath), format="PNG", optimize=False)
        saved.append(str(fpath))
        print(f"  저장: {fpath}")

    return saved


# ── 테스트 러너 ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    print("=== 캐러셀 렌더러 테스트 (Q/A 분리 모드) ===")

    # Q/A 분리 더미 데이터
    DUMMY_QA_DATA = {
        "driver_kr":  "루이스 해밀턴",
        "team":       "Ferrari",
        "car_number": "44",
        "gp_name":    "2026 일본 GP · 3라운드",
        "summary":    "이 팀에서 뭔가 특별한 걸 만들고 싶다",
        "slides": [
            {
                "text_kr": "페라리에서의 첫 시즌, 차에 적응하는 과정은 어떠셨나요?",
                "slide_type": "question",
            },
            {
                "text_kr": "처음 페라리 머신을 몰았을 때, 솔직히 말하면 정말 낯설었어요. 하지만 시간이 지나면서 차가 제 손발처럼 느껴지기 시작했습니다.",
                "slide_type": "answer",
            },
            {
                "text_kr": "엔지니어들과 긴밀히 소통하며 세팅을 맞춰나가는 과정이 정말 즐거웠어요. 매 세션마다 조금씩 나아지는 게 느껴집니다.",
                "slide_type": "answer",
            },
            {
                "text_kr": "스즈카 서킷에 대한 기대감이 있으신가요?",
                "slide_type": "question",
            },
            {
                "text_kr": "일본은 항상 제가 좋아하는 서킷이에요. 스즈카의 S 코너 구간은 드라이버로서 최대한의 집중력을 요구하죠.",
                "slide_type": "answer",
            },
            {
                "text_kr": "올해는 더 자신 있게 공략할 수 있을 것 같습니다. 우승을 목표로 하고 있어요.",
                "slide_type": "answer",
            },
            {
                "text_kr": "팀 분위기는 어떤가요? 메르세데스와의 차이점이 있다면?",
                "slide_type": "question",
            },
            {
                "text_kr": "팀 분위기는 정말 좋습니다. 메르세데스에서 보낸 12년도 훌륭했지만, 페라리에는 또 다른 에너지가 있어요. 티포시들의 열정, 팀원들의 헌신, 그리고 이 빨간 차... 완전히 새로운 챕터를 시작하는 느낌입니다.",
                "slide_type": "answer",
            },
        ],
        "date":        "2026.03.29",
        "source_text": "FIA Official Press Conference",
    }

    output_dir = Path(__file__).parent.parent / "output" / "test_qa_carousel"
    print(f"출력 경로: {output_dir}")

    print("렌더링 시작...")
    images = render_carousel(DUMMY_QA_DATA)
    print(f"생성된 슬라이드: {len(images)}장")

    paths = save_carousel(images, str(output_dir), prefix="ferrari_hamilton_qa")
    print("\n완료!")
    print(f"총 {len(paths)}장 저장")
    for p in paths:
        print(f"  → {p}")
