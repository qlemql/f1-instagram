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

    # 기존 드래프트 검수 → 렌더링 (수집/AI 처리 없이)
    python main.py --review

    # 자동 모드 (검수 없이 즉시 렌더링)
    python main.py --auto
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
from models import PressConference, CarouselSet, CarouselBatch, InterviewSlide

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


def process(conferences: list[PressConference]) -> list[CarouselBatch]:
    """AI 파이프라인으로 캐러셀 배치 생성"""
    from processor.pipeline import run_pipeline

    batches = []
    for pc in conferences:
        logger.info(f"처리 중: {pc.gp_name} - {pc.conference_type}")
        try:
            batch = run_pipeline(pc, pc.gp_name)
            if batch and batch.carousels:
                batches.append(batch)
                logger.info(
                    f"  → 드라이버 {len(batch.carousels)}명 캐러셀 생성, "
                    f"비용 ${batch.total_cost_usd:.4f}"
                )
            else:
                logger.warning(f"  → 캐러셀 생성 실패 또는 0건")
        except Exception as e:
            logger.error(f"  → 파이프라인 에러: {e}")
    return batches


def render(batches: list[CarouselBatch]) -> list[str]:
    """캐러셀 이미지 렌더링"""
    from renderer.carousel_renderer import render_carousel, save_carousel
    from models import DRIVER_TEAM_MAP, get_team_for_driver
    import re

    all_paths = []
    for batch in batches:
        logger.info(
            f"렌더링: {batch.gp_name} - {batch.conference_type} "
            f"({len(batch.carousels)}명)"
        )
        for carousel in batch.carousels:
            carousel_data = {
                "driver_kr": carousel.speaker_kr,
                "team": carousel.team,
                "car_number": "",
                "gp_name": carousel.gp_name,
                "summary": carousel.cover_headline,
                "slides": [{"text_kr": s.text_kr, "slide_type": s.slide_type} for s in carousel.slides],
                "date": carousel.date_iso,
                "source_text": "FIA Official Press Conference",
            }
            images = render_carousel(carousel_data)

            # 출력 디렉토리
            slug = re.sub(r'[^a-z0-9]+', '_', batch.gp_name.lower()).strip('_')
            surname = carousel.speaker.split()[-1].lower()
            out_dir = f"output/{slug}_{batch.conference_type}/{surname}"

            paths = save_carousel(images, out_dir, prefix=surname)
            all_paths.extend(paths)
            logger.info(
                f"  → {carousel.speaker_kr}: {len(paths)}장 렌더링 완료"
            )
    return all_paths


def notify(image_paths: list[str], batches: list[CarouselBatch]):
    """캐러셀 이미지를 텔레그램 채널로 전송한다.

    드라이버별 슬라이드 수에 맞게 image_paths를 분할하여
    notifier.telegram_bot.send_carousel_set에 위임한다.
    재시도 로직은 telegram_bot 내부에서 처리된다.
    """
    from notifier.telegram_bot import send_carousel_set

    if not image_paths:
        logger.warning("전송할 이미지가 없습니다.")
        return

    offset = 0
    for batch in batches:
        for carousel in batch.carousels:
            count = carousel.total_slides
            chunk = image_paths[offset : offset + count]
            offset += count

            if not chunk:
                logger.warning(f"{carousel.speaker_kr}: 이미지 없어 스킵")
                continue

            ok = send_carousel_set(chunk, carousel, batch.gp_name)
            if ok:
                logger.info(f"전송 완료: {carousel.speaker_kr} ({len(chunk)}장)")
            else:
                logger.error(f"전송 실패: {carousel.speaker_kr}")


def dry_run():
    """API 호출 없이 더미 데이터로 전체 흐름 테스트"""
    logger.info("=== 드라이런 모드 ===")

    dummy_carousels = [
        CarouselSet(
            speaker="Max VERSTAPPEN",
            speaker_kr="막스 페르스타펜",
            team="Red Bull",
            gp_name="2026 일본 GP",
            gp_name_en="Japanese Grand Prix",
            conference_type="post-race",
            date_iso="2026-03-29",
            cover_headline="올해 차는 정말 완벽해",
            slides=[
                InterviewSlide(
                    slide_num=2,
                    text_kr="시즌 초반부터 레드불의 압도적인 퍼포먼스를 확인할 수 있었어요. 차가 모든 코너에서 완벽하게 반응했고, 팀 전체가 정말 잘 해줬습니다.",
                    text_en="The car is absolutely incredible this year.",
                ),
            ],
            total_cost_usd=0.0,
        ),
        CarouselSet(
            speaker="Lando NORRIS",
            speaker_kr="란도 노리스",
            team="McLaren",
            gp_name="2026 일본 GP",
            gp_name_en="Japanese Grand Prix",
            conference_type="post-race",
            date_iso="2026-03-29",
            cover_headline="포디엄? 당연하죠",
            slides=[
                InterviewSlide(
                    slide_num=2,
                    text_kr="맥라렌의 꾸준한 상승세를 반영한 자신감 넘치는 발언이었어요. 포디엄은 우리에겐 이제 당연한 목표가 됐습니다.",
                    text_en="Podium? Of course, I'm in a McLaren.",
                ),
            ],
            total_cost_usd=0.0,
        ),
    ]

    batch = CarouselBatch(
        gp_name="Japanese Grand Prix",
        conference_type="post-race",
        date_iso="2026-03-29",
        carousels=dummy_carousels,
        total_cost_usd=0.0,
    )

    paths = render([batch])
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
    parser.add_argument(
        "--review",
        action="store_true",
        help="data/drafts/ 의 기존 드래프트를 검수 후 렌더링",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="검수 없이 자동 진행 (품질이 안정된 후 사용)",
    )
    args = parser.parse_args()

    # ── --review: 드래프트 검수 → 렌더링 ──────────────────────
    if args.review:
        from processor.review import review_drafts
        logger.info("=== 검수 모드: data/drafts/ 파일 검수 시작 ===")
        approved = review_drafts()
        if not approved:
            logger.info("승인된 드래프트 없음. 종료.")
            sys.exit(0)
        image_paths = render(approved)
        notify(image_paths, approved)
        logger.info(f"=== 검수 완료: {len(image_paths)}장 렌더링 ===")
        return

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

    # Step 2-b: 드래프트 저장
    from processor.review import save_drafts, review_drafts
    draft_paths = save_drafts(card_sets)
    logger.info(f"드래프트 {len(draft_paths)}개 저장 완료: data/drafts/")

    # --auto 이면 검수 생략
    if args.auto:
        logger.info("--auto 옵션: 검수 없이 자동 진행합니다.")
        approved = card_sets
    else:
        # 검수 여부 확인
        print()
        try:
            answer = input("검수를 진행하시겠습니까? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
            print()

        if answer == "y":
            approved = review_drafts()
            if not approved:
                logger.info("승인된 드래프트 없음. 종료.")
                sys.exit(0)
        else:
            logger.info("검수 없이 전체 카드셋을 렌더링합니다.")
            approved = card_sets

    # Step 3: 렌더링
    image_paths = render(approved)

    # Step 4: 전송 (Phase 3)
    notify(image_paths, approved)

    logger.info(f"=== 파이프라인 완료: {len(image_paths)}장 생성 ===")


if __name__ == "__main__":
    main()
