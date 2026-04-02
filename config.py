"""F1 Instagram 카드뉴스 자동화 — 전역 설정"""

import os

# AI 모델 설정
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6-20250514"

# 예산 설정
BUDGET = {
    "max_usd_per_month": 10.0,
    "max_usd_per_gp": 0.10,
    "warning_threshold_usd": 8.0,
    "abort_threshold_usd": 10.0,
}

# 이미지 설정
IMAGE = {
    "card_size": (1080, 1080),
    "carousel_size": (1080, 1350),
    "format": "PNG",
    "quality": 95,
}

# 콘텐츠 설정
CONTENT = {
    # 캐러셀 선별 설정
    "min_selected_qa": 3,          # 드라이버당 최소 선정 Q&A 수
    "max_selected_qa": 5,          # 드라이버당 최대 선정 Q&A 수
    "min_score_first_pass": 5,     # 1차 선별 최소 점수 (캐러셀은 기준 완화)
    # 슬라이드 설정
    "slide_target_chars": 180,     # 슬라이드 1장 권장 글자 수
    "slide_max_chars": 210,        # 슬라이드 1장 최대 글자 수
    "max_body_slides": 17,         # 본문 슬라이드 최대 장 수 (커버1 + 본문N + 출처1 ≤ 19)
}

# API 키 (환경 변수에서 로드)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 데이터 소스
FIA_NEWS_URL = "https://www.fia.com/news"
F1_URL = "https://www.formula1.com/en/latest"
