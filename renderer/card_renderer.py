"""
card_renderer.py
Pillow 기반 발언 카드(1080×1080) 렌더러

레이아웃:
  1. 상단 팀 컬러 액센트 바
  2. 팀명 칩
  3. 드라이버 이름 (Bebas Neue / 대형)
  4. 메인 카피 (한글 헤드라인)
  5. 구분선
  6. 인용구 박스 (원문 인용)
  7. 서브 카피 (컨텍스트 설명)
  8. 하단 워터마크

변경 이력:
  - CardContent 데이터클래스 직접 수신 (render_from_card)
  - 엣지케이스 오버플로우 방지: 텍스트 트런케이션 + 폰트 자동 축소
  - card_type="meme" 시각적 차별화 (노란 테두리, MEME 레이블)
  - render_card_set() 함수 추가
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Tuple, List

from PIL import Image, ImageDraw, ImageFont

# models.py가 상위 디렉토리에 있으므로 경로 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from renderer.design_tokens import (
    ALPHA,
    CARD,
    COLORS,
    FONT_SIZE,
    FONTS,
    LAYOUT,
    LINE_SPACING,
    get_team_color,
    hex_to_rgb,
    hex_to_rgba,
)

# CardContent, CardSet은 런타임에 임포트 (순환참조 방지)
from models import CardContent, CardSet


# ── 팀명 → team_colors.json 키 매핑 ────────────────────────────────────────
TEAM_KEY_MAP: dict[str, str] = {
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

# meme 카드 액센트 컬러 (팀 컬러 위에 오버레이)
MEME_BORDER_COLOR = "#FFD700"   # 골드 옐로우
MEME_LABEL_TEXT   = "MEME"


def _team_key_from_name(team_name: str) -> str:
    """
    팀 이름(모델 필드값)을 team_colors.json 키로 변환한다.
    매칭 실패 시 "red_bull"을 fallback으로 반환하고 경고 출력.
    """
    key = TEAM_KEY_MAP.get(team_name)
    if key:
        return key
    # 부분 매칭 시도 (대소문자 무시)
    name_lower = team_name.lower()
    for display, k in TEAM_KEY_MAP.items():
        if display.lower() in name_lower or name_lower in display.lower():
            return k
    print(f"[경고] 알 수 없는 팀: '{team_name}' — red_bull 사용")
    return "red_bull"


# ── 폰트 로더 ───────────────────────────────────────────────────────────────
def _load_font(font_path: Path, size: int) -> ImageFont.FreeTypeFont:
    """
    폰트 파일을 로드한다. 파일이 없으면 PIL 기본 폰트를 반환하며 경고를 출력.
    """
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    print(f"[경고] 폰트 없음: {font_path} — PIL 기본 폰트 사용 (렌더링 품질 저하)")
    return ImageFont.load_default()


# ── 텍스트 줄바꿈 유틸 ──────────────────────────────────────────────────────
def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """
    max_width(px) 안에 맞도록 텍스트를 줄바꿈한 리스트를 반환한다.
    한글 / 영어 혼용 지원: 단어 단위 우선, 초과 시 글자 단위 분리.
    """
    words = text.split()
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            # 단어 자체가 max_width를 초과하면 글자 단위 분리
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
    return lines


def _measure_text_height(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    line_spacing: int = 8,
) -> int:
    """줄바꿈 후 총 텍스트 블록 높이(px)를 반환한다."""
    lines = _wrap_text(draw, text, font, max_width)
    if not lines:
        return 0
    total = 0
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        total += (bbox[3] - bbox[1]) + line_spacing
    return total


def _draw_multiline(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    x: int,
    y: int,
    max_width: int,
    fill: tuple,
    line_spacing: int = 8,
    align: str = "left",
) -> int:
    """
    줄바꿈 처리된 텍스트를 그리고, 다음 y 좌표를 반환한다.

    Returns:
        int: 텍스트 블록 하단 y 좌표
    """
    lines = _wrap_text(draw, text, font, max_width)
    current_y = y
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_h = bbox[3] - bbox[1]
        if align == "center":
            line_w = bbox[2] - bbox[0]
            draw_x = x + (max_width - line_w) // 2
        else:
            draw_x = x
        draw.text((draw_x, current_y), line, font=font, fill=fill)
        current_y += line_h + line_spacing
    return current_y


# ── 오버플로우 방지 유틸 ─────────────────────────────────────────────────────

def _fit_font_to_height(
    draw: ImageDraw.ImageDraw,
    font_path: Path,
    text: str,
    max_width: int,
    max_height: int,
    start_size: int,
    min_size: int = 14,
    line_spacing: int = 8,
) -> Tuple[ImageFont.FreeTypeFont, int]:
    """
    텍스트가 max_height 이내에 들어오도록 폰트 크기를 점진적으로 줄인다.

    Returns:
        (font, actual_size) 튜플
    """
    size = start_size
    while size >= min_size:
        font = _load_font(font_path, size)
        h = _measure_text_height(draw, text, font, max_width, line_spacing)
        if h <= max_height:
            return font, size
        size -= 2
    # 최소 크기에서도 초과 시 트런케이션
    return _load_font(font_path, min_size), min_size


def _truncate_to_fit(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
    ellipsis: str = "…",
) -> str:
    """
    max_lines 줄을 초과하면 마지막 줄에 말줄임표를 붙여 트런케이션한다.
    """
    lines = _wrap_text(draw, text, font, max_width)
    if len(lines) <= max_lines:
        return text

    # max_lines번째 줄에서 잘라내기
    truncated_lines = lines[:max_lines]
    last_line = truncated_lines[-1]

    # 마지막 줄에 말줄임표 추가 (너비 초과 방지)
    while last_line:
        candidate = last_line + ellipsis
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            truncated_lines[-1] = candidate
            break
        last_line = last_line[:-1]

    return " ".join(truncated_lines)


# ── 메인 렌더러 클래스 ──────────────────────────────────────────────────────
class CardRenderer:
    """
    F1 발언 카드 렌더러.

    기본 사용법 (dict 기반 — 하위 호환):
        renderer = CardRenderer()
        img = renderer.render(
            team_key="red_bull",
            driver_name="MAX VERSTAPPEN",
            main_copy="올해 차는 정말 좋다",
            sub_copy="레드불의 새 머신에 대한 자신감 표현",
            quote="'This car is absolutely incredible.'",
            gp_label="2026 바레인 GP",
        )

    CardContent 직접 사용 (권장):
        renderer = CardRenderer()
        img = renderer.render_from_card(card_content)
    """

    def __init__(self) -> None:
        # 폰트 사전 로딩
        self._fonts: dict[str, ImageFont.FreeTypeFont] = {}

    def _font(self, key: str, size: int) -> ImageFont.FreeTypeFont:
        cache_key = f"{key}_{size}"
        if cache_key not in self._fonts:
            self._fonts[cache_key] = _load_font(FONTS[key], size)
        return self._fonts[cache_key]

    # ── 개별 레이어 드로잉 메서드 ──────────────────────────────────────────

    def _draw_background(
        self, draw: ImageDraw.ImageDraw, bg_color: str
    ) -> None:
        """팀 다크 배경 채우기."""
        draw.rectangle(
            [(0, 0), (CARD["width"], CARD["height"])],
            fill=hex_to_rgb(bg_color),
        )

    def _draw_meme_background_tint(self, img: Image.Image) -> None:
        """
        밈 카드 전용: 배경을 약간 밝게 하는 미세 화이트 오버레이.
        팀 배경색에 +15 밝기 효과를 준다.
        """
        tint = Image.new("RGBA", img.size, (255, 255, 255, 18))
        img.paste(tint, mask=tint)

    def _draw_noise_overlay(self, img: Image.Image) -> None:
        """
        미세 노이즈 텍스처 오버레이 (간단한 점묵 패턴).
        외부 텍스처 에셋 없이 동작하도록 직접 생성.
        """
        import random
        noise_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        pixels = noise_layer.load()
        rng = random.Random(42)  # 재현 가능성을 위해 시드 고정
        for _ in range(60_000):
            x = rng.randint(0, CARD["width"] - 1)
            y = rng.randint(0, CARD["height"] - 1)
            v = rng.randint(200, 255)
            pixels[x, y] = (v, v, v, rng.randint(4, 12))
        img.paste(noise_layer, mask=noise_layer)

    def _draw_meme_border(self, img: Image.Image) -> None:
        """
        밈 카드 전용: 카드 외곽에 골드 컬러 보더를 그린다.
        """
        border_draw = ImageDraw.Draw(img)
        bw = 6  # 보더 두께
        r, g, b = hex_to_rgb(MEME_BORDER_COLOR)
        border_draw.rectangle(
            [(bw // 2, bw // 2), (CARD["width"] - bw // 2, CARD["height"] - bw // 2)],
            outline=(r, g, b, 200),
            width=bw,
        )

    def _draw_meme_label(
        self,
        draw: ImageDraw.ImageDraw,
        img: Image.Image,
        y_accent_bottom: int,
    ) -> None:
        """
        밈 카드 전용: 우측 상단에 'MEME' 레이블 뱃지를 그린다.
        accent bar 아래 오른쪽에 위치.
        """
        font = self._font("bebas_neue", FONT_SIZE["micro"] + 4)
        margin = LAYOUT["margin"]
        pad_h, pad_v = 14, 6

        bbox = draw.textbbox((0, 0), MEME_LABEL_TEXT, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        chip_w = text_w + pad_h * 2
        chip_h = text_h + pad_v * 2

        chip_x2 = CARD["width"] - margin
        chip_x1 = chip_x2 - chip_w
        chip_y1 = y_accent_bottom + LAYOUT["gap_sm"]
        chip_y2 = chip_y1 + chip_h

        # 뱃지 배경 (골드)
        badge_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        badge_draw = ImageDraw.Draw(badge_layer)
        r, g, b = hex_to_rgb(MEME_BORDER_COLOR)
        badge_draw.rounded_rectangle(
            [(chip_x1, chip_y1), (chip_x2, chip_y2)],
            radius=4,
            fill=(r, g, b, 230),
        )
        img.paste(badge_layer, mask=badge_layer)

        # 뱃지 텍스트 (검정)
        draw.text(
            (chip_x1 + pad_h, chip_y1 + pad_v),
            MEME_LABEL_TEXT,
            font=font,
            fill=(20, 20, 20),
        )

    def _draw_accent_bar(
        self,
        draw: ImageDraw.ImageDraw,
        accent_color: str,
        y: int = 0,
        is_meme: bool = False,
    ) -> int:
        """
        상단 팀 컬러 액센트 바.
        밈 카드: 골드 컬러를 혼합하여 약간 다른 색조 적용.
        Returns: 바 하단 y 좌표
        """
        bar_h = LAYOUT["accent_bar_height"]
        if is_meme:
            # 밈 카드: 골드 계열 바 적용
            color = MEME_BORDER_COLOR
        else:
            color = accent_color
        draw.rectangle(
            [(0, y), (CARD["width"], y + bar_h)],
            fill=hex_to_rgb(color),
        )
        return y + bar_h

    def _draw_team_chip(
        self,
        draw: ImageDraw.ImageDraw,
        img: Image.Image,
        team_short: str,
        accent_color: str,
        text_primary: str,
        y: int,
    ) -> int:
        """
        팀명 칩 (컬러 배지 + 팀 약칭).
        Returns: 칩 하단 y 좌표
        """
        font = self._font("pretendard_bold", FONT_SIZE["micro"] + 2)
        margin = LAYOUT["margin"]
        pad_h, pad_v = 18, 8

        bbox = draw.textbbox((0, 0), team_short.upper(), font=font)
        text_w = bbox[2] - bbox[0]
        chip_w = text_w + pad_h * 2
        chip_h = (bbox[3] - bbox[1]) + pad_v * 2

        # 칩 배경
        chip_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        chip_draw = ImageDraw.Draw(chip_layer)
        r, g, b = hex_to_rgb(accent_color)
        chip_draw.rounded_rectangle(
            [(margin, y), (margin + chip_w, y + chip_h)],
            radius=4,
            fill=(r, g, b, 230),
        )
        img.paste(chip_layer, mask=chip_layer)

        # 칩 텍스트
        tr, tg, tb = hex_to_rgb(text_primary)
        draw.text(
            (margin + pad_h, y + pad_v),
            team_short.upper(),
            font=font,
            fill=(tr, tg, tb),
        )
        return y + chip_h

    def _draw_driver_name(
        self,
        draw: ImageDraw.ImageDraw,
        name: str,
        text_primary: str,
        accent_color: str,
        y: int,
    ) -> int:
        """
        드라이버 이름 렌더링.

        - 한글 이름: Pretendard Bold 단일 블록으로 렌더링
        - 영어 이름: Bebas Neue로 이름(FIRST)/성(LAST) 분리 렌더링

        오버플로우 방지:
          - 이름이 길면 폰트 크기 자동 축소 (최소 36px)
          - 카드 너비 초과 시 줄바꿈 처리
        Returns: 블록 하단 y 좌표
        """
        margin = LAYOUT["margin"]
        max_w = CARD["width"] - margin * 2

        has_korean = any("\uAC00" <= ch <= "\uD7A3" for ch in name)

        if has_korean:
            # ── 한글 이름: Pretendard Bold 단일 블록 ──────────────────────
            display_size = FONT_SIZE["display"] - 10  # 한글은 약간 작게
            # 너비 오버플로우 방지
            while display_size >= 36:
                font_test = self._font("pretendard_bold", display_size)
                bbox_test = draw.textbbox((0, 0), name, font=font_test)
                if bbox_test[2] - bbox_test[0] <= max_w:
                    break
                display_size -= 4

            font_name = self._font("pretendard_bold", display_size)
            ra, ga, ba = hex_to_rgb(accent_color)
            draw.text((margin, y), name, font=font_name, fill=(ra, ga, ba))
            bbox = draw.textbbox((margin, y), name, font=font_name)
            return bbox[3]

        else:
            # ── 영어 이름: Bebas Neue 이름/성 분리 ───────────────────────
            parts = name.strip().upper().split(maxsplit=1)
            first = parts[0] if parts else name.upper()
            last = parts[1] if len(parts) > 1 else ""

            display_size = FONT_SIZE["display"]

            # 성(last) 폰트 크기 결정
            while last:
                font_last_test = self._font("bebas_neue", display_size)
                bbox_test = draw.textbbox((0, 0), last, font=font_last_test)
                if bbox_test[2] - bbox_test[0] <= max_w:
                    break
                display_size -= 4
                if display_size < 36:
                    display_size = 36
                    break

            first_size = max(display_size - 24, 28)

            font_last = self._font("bebas_neue", display_size)
            font_first = self._font("bebas_neue", first_size)

            # 첫 번째 파트 (이름)
            r, g, b = hex_to_rgb(text_primary)
            draw.text((margin, y), first, font=font_first, fill=(r, g, b))
            bbox_f = draw.textbbox((margin, y), first, font=font_first)
            y_next = bbox_f[3] - 8

            # 두 번째 파트 (성) — accent 컬러
            if last:
                ra, ga, ba = hex_to_rgb(accent_color)
                draw.text((margin, y_next), last, font=font_last, fill=(ra, ga, ba))
                bbox_l = draw.textbbox((margin, y_next), last, font=font_last)
                y_next = bbox_l[3]

            return y_next

    def _draw_divider(
        self,
        draw: ImageDraw.ImageDraw,
        accent_color: str,
        y: int,
    ) -> int:
        """
        얇은 구분선 (좌측 accent 색 강조 + 우측 어두운 선).
        Returns: 선 하단 y 좌표
        """
        margin = LAYOUT["margin"]
        line_y = y + LAYOUT["gap_md"]
        accent_end = margin + 64

        draw.line(
            [(margin, line_y), (accent_end, line_y)],
            fill=hex_to_rgb(accent_color),
            width=2,
        )
        draw.line(
            [(accent_end + 4, line_y), (CARD["width"] - margin, line_y)],
            fill=hex_to_rgb(COLORS["divider"]),
            width=2,
        )
        return line_y + LAYOUT["gap_md"]

    def _draw_main_copy(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        text_primary: str,
        y: int,
        max_available_h: Optional[int] = None,
    ) -> int:
        """
        메인 카피 (한글 헤드라인, Bebas Neue/Pretendard Bold).

        오버플로우 방지:
          - max_available_h가 지정되면 그 높이 이내로 폰트 자동 축소
          - 최대 3줄로 트런케이션
        Returns: 블록 하단 y 좌표
        """
        margin = LAYOUT["margin"]
        max_w = CARD["width"] - margin * 2

        has_korean = any("\uAC00" <= ch <= "\uD7A3" for ch in text)
        start_size = FONT_SIZE["headline"] - 6 if has_korean else FONT_SIZE["headline"]
        font_key = "pretendard_bold" if has_korean else "bebas_neue"

        if max_available_h:
            font, _ = _fit_font_to_height(
                draw, FONTS[font_key], text,
                max_width=max_w,
                max_height=max_available_h,
                start_size=start_size,
                min_size=22,
                line_spacing=LINE_SPACING["relaxed"],
            )
        else:
            font = self._font(font_key, start_size)

        # 최대 3줄 트런케이션
        text = _truncate_to_fit(draw, text, font, max_w, max_lines=3)

        r, g, b = hex_to_rgb(text_primary)
        return _draw_multiline(
            draw, text, font, margin, y, max_w, (r, g, b),
            line_spacing=LINE_SPACING["relaxed"],
        )

    def _draw_quote_box(
        self,
        draw: ImageDraw.ImageDraw,
        img: Image.Image,
        quote: str,
        accent_color: str,
        y: int,
        is_meme: bool = False,
        max_available_h: Optional[int] = None,
    ) -> int:
        """
        인용구 박스 (좌측 accent 바 + 반투명 배경 + 이탤릭체 텍스트).

        오버플로우 방지:
          - max_available_h가 지정되면 그 높이 이내로 폰트 자동 축소
          - 최대 7줄로 트런케이션 (200자 인용구 대응)
        밈 카드: 박스 border 컬러 골드, 좌측 바 골드로 차별화.
        Returns: 블록 하단 y 좌표
        """
        margin = LAYOUT["margin"]
        qpad = LAYOUT["quote_padding"]
        bar_w = LAYOUT["quote_bar_width"]
        max_w = CARD["width"] - margin * 2 - qpad * 2 - bar_w

        # 인용구용 폰트 크기 결정 (오버플로우 방지)
        caption_size = FONT_SIZE["caption"]
        if max_available_h:
            font, caption_size = _fit_font_to_height(
                draw, FONTS["pretendard_medium"], quote,
                max_width=max_w,
                max_height=max_available_h - qpad * 2,
                start_size=caption_size,
                min_size=12,
                line_spacing=LINE_SPACING["normal"],
            )
        else:
            font = self._font("pretendard_medium", caption_size)

        # 최대 7줄 트런케이션
        quote = _truncate_to_fit(draw, quote, font, max_w, max_lines=7)

        # 텍스트 높이 계산
        lines = _wrap_text(draw, quote, font, max_w)
        bbox_sample = draw.textbbox((0, 0), "샘플Ag", font=font)
        line_h = bbox_sample[3] - bbox_sample[1] + LINE_SPACING["normal"]
        text_block_h = line_h * len(lines)
        box_h = text_block_h + qpad * 2

        box_x1 = margin
        box_y1 = y + LAYOUT["gap_lg"] // 2
        box_x2 = CARD["width"] - margin
        box_y2 = box_y1 + box_h

        # 반투명 박스 배경
        box_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        box_draw = ImageDraw.Draw(box_layer)
        qr, qg, qb = hex_to_rgb(COLORS["quote_bg"])
        border_color = hex_to_rgb(MEME_BORDER_COLOR) if is_meme else hex_to_rgb(COLORS["quote_border"])
        box_draw.rounded_rectangle(
            [(box_x1, box_y1), (box_x2, box_y2)],
            radius=6,
            fill=(qr, qg, qb, ALPHA["quote_bg"]),
            outline=border_color,
            width=2 if is_meme else 1,
        )
        img.paste(box_layer, mask=box_layer)

        # 좌측 accent 바 (밈 카드: 골드)
        bar_color = MEME_BORDER_COLOR if is_meme else accent_color
        ar, ag, ab = hex_to_rgb(bar_color)
        draw.rounded_rectangle(
            [(box_x1, box_y1), (box_x1 + bar_w, box_y2)],
            radius=3,
            fill=(ar, ag, ab),
        )

        # 인용 텍스트
        text_x = box_x1 + bar_w + qpad
        text_y = box_y1 + qpad
        r, g, b = hex_to_rgb(COLORS["text_secondary"])
        _draw_multiline(
            draw, quote, font, text_x, text_y, max_w, (r, g, b),
            line_spacing=LINE_SPACING["normal"],
        )

        return box_y2

    def _draw_sub_copy(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        text_secondary: str,
        y: int,
        max_available_h: Optional[int] = None,
    ) -> int:
        """
        서브 카피 (컨텍스트 설명 텍스트).

        오버플로우 방지:
          - max_available_h 지정 시 폰트 자동 축소
          - 최대 3줄로 트런케이션
        Returns: 블록 하단 y 좌표
        """
        margin = LAYOUT["margin"]
        max_w = CARD["width"] - margin * 2
        start_y = y + LAYOUT["gap_md"]

        if max_available_h:
            font, _ = _fit_font_to_height(
                draw, FONTS["pretendard_medium"], text,
                max_width=max_w,
                max_height=max_available_h,
                start_size=FONT_SIZE["body"],
                min_size=14,
                line_spacing=LINE_SPACING["tight"],
            )
        else:
            font = self._font("pretendard_medium", FONT_SIZE["body"])

        # 최대 3줄 트런케이션
        text = _truncate_to_fit(draw, text, font, max_w, max_lines=3)

        r, g, b = hex_to_rgb(text_secondary)
        return _draw_multiline(
            draw, text, font, margin, start_y, max_w, (r, g, b),
            line_spacing=LINE_SPACING["tight"],
        )

    def _draw_gp_label(
        self,
        draw: ImageDraw.ImageDraw,
        label: str,
        accent_color: str,
        y: int,
    ) -> None:
        """
        GP 라벨 (왼쪽 하단 영역).
        """
        margin = LAYOUT["margin"]
        font = self._font("pretendard_bold", FONT_SIZE["micro"])
        r, g, b = hex_to_rgb(accent_color)
        draw.text((margin, y), label.upper(), font=font, fill=(r, g, b))

    def _draw_watermark(
        self,
        draw: ImageDraw.ImageDraw,
        y: int,
    ) -> None:
        """
        하단 워터마크 (우측 정렬).
        """
        margin = LAYOUT["margin"]
        font = self._font("pretendard_regular", FONT_SIZE["micro"])
        text = "@F1_KR_FAN"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        x = CARD["width"] - margin - text_w
        r, g, b, a = hex_to_rgba(COLORS["text_disabled"], ALPHA["watermark"])
        draw.text((x, y), text, font=font, fill=(r, g, b, a))

    # ── 공통 렌더 로직 ─────────────────────────────────────────────────────
    def _render_core(
        self,
        team_key: str,
        driver_name: str,
        main_copy: str,
        sub_copy: str,
        quote: str = "",
        gp_label: str = "",
        is_meme: bool = False,
    ) -> Image.Image:
        """
        실제 렌더링 파이프라인.
        render() / render_from_card() 모두 이 메서드를 호출한다.
        """
        team = get_team_color(team_key)

        # 캔버스 생성 (RGBA → 저장 시 RGB 변환)
        img = Image.new("RGBA", (CARD["width"], CARD["height"]), (0, 0, 0, 255))
        draw = ImageDraw.Draw(img)

        # 1. 배경
        self._draw_background(draw, team["bg"])

        # 밈 카드: 배경 미세 브라이튼
        if is_meme:
            self._draw_meme_background_tint(img)

        # 2. 노이즈 텍스처
        self._draw_noise_overlay(img)
        draw = ImageDraw.Draw(img)

        # 3. 상단 액센트 바
        y = self._draw_accent_bar(draw, team["accent"], is_meme=is_meme)
        accent_bar_bottom = y

        y += LAYOUT["gap_lg"]  # 바 아래 여백

        # 밈 카드: MEME 레이블 뱃지 (오른쪽 상단)
        if is_meme:
            self._draw_meme_label(draw, img, accent_bar_bottom)
            draw = ImageDraw.Draw(img)

        # 4. 팀 칩
        y = self._draw_team_chip(
            draw, img,
            team["short_name"],
            team["accent"],
            team["text_primary"],
            y,
        )
        draw = ImageDraw.Draw(img)

        y += LAYOUT["gap_md"]

        # 5. 드라이버 이름
        y = self._draw_driver_name(
            draw, driver_name,
            team["text_primary"],
            team["accent"],
            y,
        )

        # 6. 구분선
        y = self._draw_divider(draw, team["accent"], y)

        # ── 남은 콘텐츠 영역 계산 (오버플로우 방지) ─────────────────────────
        # 하단 예약 영역: GP 라벨 + 워터마크 + 여백
        bottom_reserved = LAYOUT["margin"] + 40
        content_bottom = CARD["height"] - bottom_reserved

        # 현재 y 이후 사용 가능한 총 높이
        available_h = content_bottom - y

        # 구역 배분: main_copy 25%, quote 50%, sub_copy 25%
        main_h  = int(available_h * 0.25)
        quote_h = int(available_h * 0.50)
        sub_h   = int(available_h * 0.20)

        # 7. 메인 카피
        y = self._draw_main_copy(
            draw, main_copy, team["text_primary"], y,
            max_available_h=main_h,
        )

        # 8. 인용구 박스 (있을 때만)
        if quote:
            y = self._draw_quote_box(
                draw, img, quote, team["accent"], y,
                is_meme=is_meme,
                max_available_h=quote_h,
            )
            draw = ImageDraw.Draw(img)

        # 9. 서브 카피
        y = self._draw_sub_copy(
            draw, sub_copy, team["text_secondary"], y,
            max_available_h=sub_h,
        )

        # 10. 하단 영역 (GP 라벨 + 워터마크)
        bottom_y = CARD["height"] - LAYOUT["margin"] - 20
        if gp_label:
            self._draw_gp_label(draw, gp_label, team["accent"], bottom_y)
        self._draw_watermark(draw, bottom_y)

        # 밈 카드: 외곽 보더
        if is_meme:
            self._draw_meme_border(img)

        # RGBA → RGB (PNG 저장용)
        final = Image.new("RGB", img.size, (0, 0, 0))
        final.paste(img, mask=img.split()[3])
        return final

    # ── 공개 API ────────────────────────────────────────────────────────────

    def render_from_card(self, card: CardContent) -> Image.Image:
        """
        CardContent 데이터클래스를 직접 받아 카드 이미지를 렌더링한다.

        Args:
            card: models.CardContent 인스턴스

        Returns:
            PIL.Image.Image: 1080×1080 RGB 이미지
        """
        team_key = _team_key_from_name(card.team)
        is_meme = card.card_type == "meme"

        return self._render_core(
            team_key=team_key,
            driver_name=card.speaker_kr,   # 한국어 이름 사용 (없으면 그대로)
            main_copy=card.main_copy,
            sub_copy=card.sub_copy,
            quote=card.quote_en,
            gp_label=card.gp_name,
            is_meme=is_meme,
        )

    def render(
        self,
        team_key: str,
        driver_name: str,
        main_copy: str,
        sub_copy: str,
        quote: str = "",
        gp_label: str = "",
        watermark: str = "@F1_KR_FAN",
        card_type: str = "info",
    ) -> Image.Image:
        """
        카드 이미지를 렌더링하여 PIL Image 객체를 반환한다. (하위 호환 유지)

        Args:
            team_key:    팀 키 (예: "red_bull", "ferrari")
            driver_name: 드라이버 전체 이름 (예: "Max Verstappen")
            main_copy:   메인 헤드라인 한글 문장
            sub_copy:    서브 설명 텍스트
            quote:       원문 인용구 (선택)
            gp_label:    GP 이름 (선택, 예: "2026 바레인 GP")
            card_type:   "info" | "meme" (기본값: "info")

        Returns:
            PIL.Image.Image: 1080×1080 RGB 이미지
        """
        return self._render_core(
            team_key=team_key,
            driver_name=driver_name,
            main_copy=main_copy,
            sub_copy=sub_copy,
            quote=quote,
            gp_label=gp_label,
            is_meme=(card_type == "meme"),
        )


# ── CardSet 배치 렌더링 ──────────────────────────────────────────────────────

def render_card_set(
    card_set: CardSet,
    output_dir: Optional[str] = None,
    renderer: Optional[CardRenderer] = None,
) -> List[str]:
    """
    CardSet을 받아 모든 카드를 렌더링하고 저장된 파일 경로 리스트를 반환한다.

    출력 경로: {output_dir}/{gp_name}_{conference_type}/card_{n}.png
      - gp_name, conference_type의 공백은 '_'로 치환하고 소문자 정규화.
      - n은 1부터 시작하는 카드 순서 번호.

    Args:
        card_set:   models.CardSet 인스턴스
        output_dir: 최상위 출력 디렉토리 (기본값: 프로젝트 루트의 output/)
        renderer:   재사용할 CardRenderer 인스턴스 (기본값: 새로 생성)

    Returns:
        List[str]: 저장된 PNG 파일 절대 경로 리스트
    """
    if renderer is None:
        renderer = CardRenderer()

    # 출력 디렉토리 결정
    if output_dir is None:
        # renderer/ 기준 → 상위(프로젝트 루트) → output/
        base = Path(__file__).parent.parent / "output"
    else:
        base = Path(output_dir)

    # 폴더명 정규화: "Japanese Grand Prix" + "post-race" → "japanese_grand_prix_post-race"
    gp_slug = card_set.gp_name.replace(" ", "_").lower()
    conf_slug = card_set.conference_type.replace(" ", "_").lower()
    folder_name = f"{gp_slug}_{conf_slug}"
    out_dir = base / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: List[str] = []

    for n, card in enumerate(card_set.cards, start=1):
        try:
            img = renderer.render_from_card(card)
            file_name = f"card_{n}.png"
            file_path = out_dir / file_name
            img.save(str(file_path))
            saved_paths.append(str(file_path))
            print(f"  [렌더링] {file_path.name}  ({card.speaker_kr} / {card.card_type})")
        except Exception as exc:
            print(f"  [오류] card_{n} 렌더링 실패: {exc}")

    print(f"\n총 {len(saved_paths)}/{len(card_set.cards)}장 저장 완료 → {out_dir}")
    return saved_paths


# ── 직접 실행 시 빠른 테스트 ─────────────────────────────────────────────────
if __name__ == "__main__":
    renderer = CardRenderer()
    img = renderer.render(
        team_key="red_bull",
        driver_name="Max Verstappen",
        main_copy="올해 차는 정말 완벽하다",
        sub_copy="바레인 테스트 직후 인터뷰에서 새 머신에 대한 극찬",
        quote="'This car is absolutely incredible. Everything feels perfect.'",
        gp_label="2026 바레인 GP",
    )
    out_path = Path(__file__).parent.parent / "output" / "test_card.png"
    out_path.parent.mkdir(exist_ok=True)
    img.save(str(out_path))
    print(f"저장 완료: {out_path}")
