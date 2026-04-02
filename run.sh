#!/bin/bash
# F1 카드뉴스 자동화 — 원클릭 실행 스크립트
# 사용법: ./run.sh

set -e

cd "$(dirname "$0")"

echo "========================================"
echo "  F1 카드뉴스 자동화 파이프라인"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"
echo ""

# .env 로드
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
else
    echo "[에러] .env 파일이 없습니다."
    exit 1
fi

# API 키 확인
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "[에러] ANTHROPIC_API_KEY가 설정되지 않았습니다."
    exit 1
fi

echo "[1/4] FIA 기자회견 수집 + AI 처리 + 렌더링 + 전송 시작..."
echo ""

python3 main.py --auto

echo ""
echo "========================================"
echo "  완료! 텔레그램을 확인하세요."
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"
