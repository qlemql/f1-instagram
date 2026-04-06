"""
renderer 패키지
F1 인스타그램 카드뉴스 Pillow 렌더러
"""

# card_renderer 는 구버전 모델(CardContent, CardSet)을 사용하므로
# 패키지 임포트 시 lazy 방식으로 처리한다 — 직접 import 시에만 로드.
from .design_tokens import (
    CARD,
    CAROUSEL,
    CAROUSEL_TYPO,
    COLORS,
    FONT_SIZE,
    FONTS,
    LAYOUT,
    LINE_SPACING,
    get_team_color,
    hex_to_rgb,
    hex_to_rgba,
    load_team_colors,
    make_vertical_gradient,
    darken_color,
    lighten_color,
    get_driver_number,
)

from .carousel_renderer import (
    render_cover,
    render_interview_slide,
    render_source,
    render_carousel,
    render_quote_card,
    save_carousel,
)


def __getattr__(name: str):
    """CardRenderer는 lazy import로 처리한다."""
    if name == "CardRenderer":
        from .card_renderer import CardRenderer  # noqa: PLC0415
        return CardRenderer
    raise AttributeError(f"module 'renderer' has no attribute {name!r}")


__all__ = [
    "CardRenderer",
    # design_tokens
    "CARD",
    "CAROUSEL",
    "COLORS",
    "CAROUSEL_TYPO",
    "FONT_SIZE",
    "FONTS",
    "LAYOUT",
    "LINE_SPACING",
    "get_team_color",
    "hex_to_rgb",
    "hex_to_rgba",
    "load_team_colors",
    "make_vertical_gradient",
    "darken_color",
    "lighten_color",
    "get_driver_number",
    # carousel_renderer
    "render_cover",
    "render_interview_slide",
    "render_source",
    "render_carousel",
    "render_quote_card",
    "save_carousel",
]
