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
    "max_selected_quotes": 5,
    "min_score_first_pass": 7,
    "sonnet_trigger_score_variance": 4,
    "tone_ratio": {"info": 0.9, "meme": 0.1},
}

# API 키 (환경 변수에서 로드)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 데이터 소스
FIA_NEWS_URL = "https://www.fia.com/news"
F1_URL = "https://www.formula1.com/en/latest"
