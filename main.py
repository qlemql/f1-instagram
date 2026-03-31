"""F1 Instagram 카드뉴스 자동화 — 메인 오케스트레이터

사용법:
    # 자동 모드: 최신 기자회견 수집 → 처리 → 렌더링 → 전송
    python main.py

    # 특정 URL 수집
    python main.py --url "https://www.fia.com/news/..."

    # 수집만 (AI 처리 없이)
    python main.py --scrape-only

    # 기존 수집 데이터로 처리 + 렌더링
    python main.py --from-file data/raw/japanese_gp_post-race_2026-03-29.json

    # 드라이런 (API 호출 없이 더미 데이터로 렌더링)
    python main.py --dry-run
"""

import argparse
import os
import sys
import logging
from pathlib import Path

# .env 파일에서 환경 변수 로드
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

from scraper.collector import Collector
from models import PressConference, CardContent, CardSet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def scrape(url: str = None) -> list[PressConference]:
    """기자회견 텍스트 수집"""
    collector = Collector()

    if url:
        logger.info(f"특정 URL 수집: {url}")
        pc = collector.collect_url(url)
        return [pc] if pc else []
    else:
        logger.info("최신 기자회견 자동 수집 시작")
        results = collector.collect_new()
        logger.info(f"수집 완료: {len(results)}건")
        return results


def process(conferences: list[PressConference]) -> list[CardSet]:
    """AI 파이프라인으로 카드 생성"""
    from processor.pipeline import run_pipeline

    card_sets = []
    for pc in conferences:
        logger.info(f"처리 중: {pc.gp_name} - {pc.conference_type}")
        try:
            card_set = run_pipeline(pc, pc.gp_name)
            if card_set and card_set.cards:
                card_sets.append(card_set)
                logger.info(
                    f"  → 카드 {len(card_set.cards)}장 생성, "
                    f"비용 ${card_set.total_cost_usd:.4f}"
                )
            else:
                logger.warning(f"  → 카드 생성 실패 또는 0장")
        except Exception as e:
            logger.error(f"  → 파이프라인 에러: {e}")
    return card_sets


def render(card_sets: list[CardSet]) -> list[str]:
    """카드 이미지 렌더링"""
    from renderer.card_renderer import render_card_set

    all_paths = []
    for cs in card_sets:
        logger.info(f"렌더링: {cs.gp_name} - {cs.conference_type} ({len(cs.cards)}장)")
        paths = render_card_set(cs)
        all_paths.extend(paths)
        logger.info(f"  → {len(paths)}장 렌더링 완료")
    return all_paths


def notify(image_paths: list[str], card_sets: list[CardSet]):
    """카드 이미지를 텔레그램 채널로 전송한다.

    TELEGRAM_BOT_TOKEN 환경 변수가 없으면 경고 로그만 출력하고 스킵한다.
    카드셋별로 해당 이미지를 묶어 미디어 그룹(앨범)으로 전송한다.
    """
    from config import TELEGRAM_BOT_TOKEN
    from notifier.telegram_bot import send_card_set

    if not TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN이 설정되지 않아 텔레그램 전송을 스킵합니다."
        )
        return

    if not image_paths:
        logger.warning("전송할 이미지가 없습니다.")
        return

    logger.info(f"텔레그램 전송 시작: {len(image_paths)}장 / {len(card_sets)}개 카드셋")

    # 카드셋별로 이미지 분배 (seq 기반 순서 보장)
    # card_set.cards의 수량만큼 image_paths에서 순서대로 슬라이싱
    offset = 0
    for card_set in card_sets:
        count = len(card_set.cards)
        chunk = image_paths[offset : offset + count]
        offset += count

        if not chunk:
            logger.warning(
                f"{card_set.gp_name} / {card_set.conference_type}: "
                "대응하는 이미지가 없어 스킵합니다."
            )
            continue

        success = send_card_set(chunk, card_set)
        if success:
            logger.info(
                f"전송 완료: {card_set.gp_name} / {card_set.conference_type} "
                f"({len(chunk)}장)"
            )
        else:
            logger.error(
                f"전송 실패: {card_set.gp_name} / {card_set.conference_type}"
            )


def dry_run():
    """API 호출 없이 더미 데이터로 전체 흐름 테스트"""
    logger.info("=== 드라이런 모드 ===")

    dummy_cards = [
        CardContent(
            speaker_kr="페르스타펜",
            team="Red Bull",
            main_copy="올해 차는 정말 완벽해",
            sub_copy="시즌 초반 레드불의 압도적 퍼포먼스에 대한 자신감을 드러냈다",
            quote_en="The car is absolutely incredible this year.",
            card_type="info",
            gp_name="2026 일본 GP",
            seq=1,
        ),
        CardContent(
            speaker_kr="노리스",
            team="McLaren",
            main_copy="포디엄? 당연하죠",
            sub_copy="맥라렌의 꾸준한 상승세를 반영한 자신감 넘치는 발언",
            quote_en="Podium? Of course, I'm in a McLaren.",
            card_type="meme",
            gp_name="2026 일본 GP",
            seq=2,
        ),
        CardContent(
            speaker_kr="해밀턴",
            team="Ferrari",
            main_copy="페라리와 함께하는 매 순간이 특별하다",
            sub_copy="새로운 팀에서의 적응을 마치고 본격적인 시즌을 시작하는 감회",
            quote_en="Every moment with Ferrari feels special.",
            card_type="info",
            gp_name="2026 일본 GP",
            seq=3,
        ),
    ]

    card_set = CardSet(
        gp_name="Japanese Grand Prix",
        conference_type="post-race",
        date_iso="2026-03-29",
        cards=dummy_cards,
        total_cost_usd=0.0,
    )

    paths = render([card_set])
    logger.info(f"=== 드라이런 완료: {len(paths)}장 렌더링 ===")
    for p in paths:
        logger.info(f"  → {p}")
    return paths


def main():
    parser = argparse.ArgumentParser(description="F1 카드뉴스 자동화 파이프라인")
    parser.add_argument("--url", help="특정 FIA/F1 URL 수집")
    parser.add_argument("--scrape-only", action="store_true", help="수집만 실행")
    parser.add_argument("--from-file", help="기존 JSON 파일로 처리")
    parser.add_argument("--dry-run", action="store_true", help="더미 데이터로 테스트")
    args = parser.parse_args()

    if args.dry_run:
        dry_run()
        return

    # API 키 조기 실패 (--dry-run, --scrape-only 제외)
    if not args.scrape_only and not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error(
            "ANTHROPIC_API_KEY 환경 변수가 설정되지 않았습니다. "
            "Claude API 키를 설정해주세요."
        )
        sys.exit(1)

    # Step 1: 수집
    if args.from_file:
        logger.info(f"파일에서 로드: {args.from_file}")
        conferences = [PressConference.from_json(args.from_file)]
    else:
        conferences = scrape(url=args.url)

    if not conferences:
        logger.info("수집된 기자회견 없음. 종료.")
        sys.exit(0)

    if args.scrape_only:
        logger.info("수집 완료 (--scrape-only). 종료.")
        return

    # Step 2: AI 처리
    card_sets = process(conferences)
    if not card_sets:
        logger.warning("카드 생성 결과 없음. 종료.")
        sys.exit(0)

    # Step 3: 렌더링
    image_paths = render(card_sets)

    # Step 4: 전송 (Phase 3)
    notify(image_paths, card_sets)

    logger.info(f"=== 파이프라인 완료: {len(image_paths)}장 생성 ===")


if __name__ == "__main__":
    main()
