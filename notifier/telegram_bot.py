"""F1 Instagram 카드뉴스 자동화 — 텔레그램 봇 전송 모듈

공개 함수:
    send_card_set(image_paths, card_set)  — 카드 이미지 앨범 전송
    weekly_cost_report()                  — 주간 비용 리포트 텍스트 전송

텔레그램 제한:
    - 미디어 그룹(앨범)은 최대 10장. 초과 시 여러 그룹으로 분할 전송.
    - 캡션은 첫 번째 아이템에만 붙임 (그룹 단위).

전송 정책:
    - 실패 시 1회 재시도 (재시도 간격 3초)
    - TELEGRAM_BOT_TOKEN 없으면 경고 로그만 출력하고 스킵
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Sequence

import telegram
from telegram import InputMediaPhoto
from telegram.error import TelegramError

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from models import CardSet

logger = logging.getLogger(__name__)

# 텔레그램 미디어 그룹 최대 장수
_TELEGRAM_ALBUM_LIMIT = 10

# 재시도 대기 시간 (초)
_RETRY_DELAY_SECONDS = 3

# 로그 디렉토리 (프로젝트 루트의 logs/)
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


# ---------------------------------------------------------------------------
# 내부 유틸리티
# ---------------------------------------------------------------------------

def _check_credentials() -> bool:
    """봇 토큰과 채팅 ID 설정 여부 확인."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN 환경 변수가 설정되지 않았습니다. "
            "텔레그램 전송을 스킵합니다."
        )
        return False
    if not TELEGRAM_CHAT_ID:
        logger.warning(
            "TELEGRAM_CHAT_ID 환경 변수가 설정되지 않았습니다. "
            "텔레그램 전송을 스킵합니다."
        )
        return False
    return True


def _chunk_list(lst: list, size: int) -> list[list]:
    """리스트를 size 크기 청크로 분할."""
    return [lst[i : i + size] for i in range(0, len(lst), size)]


def _build_caption(card_set: CardSet, card_index: int, total: int) -> str:
    """카드 캡션 포맷: "{gp_name} {conference_type}\n카드 {n}/{total}"."""
    return (
        f"{card_set.gp_name} {card_set.conference_type}\n"
        f"카드 {card_index}/{total}"
    )


async def _send_album_async(
    bot: telegram.Bot,
    chat_id: str,
    image_paths: list[str],
    caption: str,
) -> bool:
    """
    미디어 그룹(앨범)을 비동기로 전송. 실패 시 1회 재시도.

    Args:
        bot:         텔레그램 Bot 인스턴스
        chat_id:     전송 대상 채팅 ID
        image_paths: 이미지 파일 경로 목록 (최대 10장)
        caption:     첫 번째 이미지에 붙을 캡션

    Returns:
        True: 전송 성공, False: 재시도 포함 최종 실패
    """
    for attempt in range(1, 3):  # 최초 1회 + 재시도 1회
        try:
            media_group: list[InputMediaPhoto] = []
            for idx, path in enumerate(image_paths):
                with open(path, "rb") as f:
                    img_bytes = f.read()
                media_group.append(
                    InputMediaPhoto(
                        media=img_bytes,
                        caption=caption if idx == 0 else None,
                        parse_mode="HTML",
                    )
                )

            await bot.send_media_group(
                chat_id=chat_id,
                media=media_group,
            )
            logger.info(
                f"앨범 전송 성공: {len(image_paths)}장 "
                f"(캡션: {caption!r:.50}...)"
            )
            return True

        except TelegramError as exc:
            if attempt == 1:
                logger.error(
                    f"텔레그램 전송 실패 (1차 시도): {exc}. "
                    f"{_RETRY_DELAY_SECONDS}초 후 재시도합니다."
                )
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
            else:
                logger.error(f"텔레그램 전송 최종 실패 (재시도 포함): {exc}")
                return False
        except FileNotFoundError as exc:
            logger.error(f"이미지 파일을 찾을 수 없습니다: {exc}")
            return False

    return False


async def _send_text_async(
    bot: telegram.Bot,
    chat_id: str,
    text: str,
) -> bool:
    """
    텍스트 메시지를 비동기로 전송. 실패 시 1회 재시도.

    Returns:
        True: 전송 성공, False: 재시도 포함 최종 실패
    """
    for attempt in range(1, 3):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
            )
            logger.info("텍스트 메시지 전송 성공.")
            return True

        except TelegramError as exc:
            if attempt == 1:
                logger.error(
                    f"텍스트 메시지 전송 실패 (1차 시도): {exc}. "
                    f"{_RETRY_DELAY_SECONDS}초 후 재시도합니다."
                )
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
            else:
                logger.error(f"텍스트 메시지 전송 최종 실패 (재시도 포함): {exc}")
                return False

    return False


# ---------------------------------------------------------------------------
# 주간 비용 리포트 내부 헬퍼
# ---------------------------------------------------------------------------

def _load_week_entries(log_dir: Path) -> list[dict]:
    """
    이번 주(월요일~오늘, UTC) JSONL 로그 파일들을 읽어 엔트리 목록 반환.

    파일명 패턴: *_cost.jsonl
    """
    if not log_dir.exists():
        return []

    today = datetime.now(tz=timezone.utc)
    # 이번 주 월요일 00:00:00 UTC
    week_start = (today - timedelta(days=today.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start_str = week_start.isoformat()

    entries: list[dict] = []
    for log_file in log_dir.glob("*_cost.jsonl"):
        with log_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = entry.get("timestamp", "")
                if ts >= week_start_str:
                    entries.append(entry)

    return entries


def _build_weekly_report_text(entries: list[dict]) -> str:
    """엔트리 목록으로 주간 비용 리포트 텍스트 생성."""
    today = datetime.now(tz=timezone.utc)
    week_start = today - timedelta(days=today.weekday())

    total_calls = len(entries)
    total_cost = sum(e.get("cost_usd", 0.0) for e in entries)

    by_gp: dict[str, float] = {}
    by_model: dict[str, float] = {}
    for e in entries:
        gp    = e.get("gp_name", "unknown")
        model = e.get("model", "unknown")
        cost  = e.get("cost_usd", 0.0)
        by_gp[gp]       = by_gp.get(gp, 0.0)       + cost
        by_model[model] = by_model.get(model, 0.0) + cost

    # 리포트 텍스트 구성
    lines: list[str] = [
        "<b>F1 카드뉴스 주간 비용 리포트</b>",
        f"기간: {week_start.strftime('%Y-%m-%d')} ~ {today.strftime('%Y-%m-%d')} (UTC)",
        "",
        f"총 API 호출 수: <b>{total_calls}회</b>",
        f"총 비용: <b>${total_cost:.4f}</b>",
        "",
    ]

    if by_gp:
        lines.append("<b>GP별 비용</b>")
        for gp, cost in sorted(by_gp.items(), key=lambda x: -x[1]):
            lines.append(f"  • {gp}: ${cost:.4f}")
        lines.append("")

    if by_model:
        lines.append("<b>모델별 비용</b>")
        for model, cost in sorted(by_model.items(), key=lambda x: -x[1]):
            lines.append(f"  • {model}: ${cost:.4f}")
        lines.append("")

    # 월 예산 대비 현황
    from config import BUDGET
    monthly_limit = BUDGET["max_usd_per_month"]
    # 이번 달 전체 합산 (월간 누적)
    current_month = today.strftime("%Y-%m")
    monthly_total = 0.0
    if _LOG_DIR.exists():
        for log_file in _LOG_DIR.glob("*_cost.jsonl"):
            with log_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("timestamp", "").startswith(current_month):
                        monthly_total += entry.get("cost_usd", 0.0)

    usage_pct = (monthly_total / monthly_limit * 100) if monthly_limit > 0 else 0
    lines.append(
        f"이번 달 누적: <b>${monthly_total:.4f}</b> / "
        f"${monthly_limit:.2f} ({usage_pct:.1f}% 사용)"
    )

    if total_calls == 0:
        lines.insert(3, "이번 주 API 호출 기록이 없습니다.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------

def send_card_set(image_paths: Sequence[str], card_set: CardSet) -> bool:
    """
    카드 이미지들을 텔레그램 채널에 미디어 그룹(앨범)으로 전송한다.

    10장 초과 시 10장씩 청크로 분할하여 순서대로 전송한다.
    각 청크의 첫 번째 이미지에만 캡션을 붙인다.

    캡션 포맷: "{gp_name} {conference_type}\\n카드 {n}/{total}"
    - n: 청크 내 첫 번째 카드의 전체 인덱스
    - total: 전체 카드 수

    Args:
        image_paths: 전송할 이미지 파일 절대 경로 목록
        card_set:    해당 이미지들의 메타데이터 (gp_name, conference_type 등)

    Returns:
        True: 모든 청크 전송 성공, False: 하나 이상 실패
    """
    if not _check_credentials():
        return False

    paths = list(image_paths)
    if not paths:
        logger.warning("전송할 이미지 경로가 비어 있습니다. 스킵합니다.")
        return True

    total = len(paths)
    chunks = _chunk_list(paths, _TELEGRAM_ALBUM_LIMIT)
    logger.info(
        f"텔레그램 전송 시작: {total}장 → {len(chunks)}개 청크 "
        f"({card_set.gp_name} / {card_set.conference_type})"
    )

    all_success = True

    async def _run() -> bool:
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        success = True
        card_index = 1  # 전체 기준 카드 번호 (1부터 시작)

        for chunk_no, chunk in enumerate(chunks, start=1):
            caption = _build_caption(card_set, card_index, total)
            logger.info(
                f"청크 {chunk_no}/{len(chunks)} 전송 중: "
                f"{len(chunk)}장 (캡션: {caption!r})"
            )
            ok = await _send_album_async(
                bot=bot,
                chat_id=TELEGRAM_CHAT_ID,
                image_paths=chunk,
                caption=caption,
            )
            if not ok:
                success = False
                logger.error(
                    f"청크 {chunk_no}/{len(chunks)} 전송 실패. "
                    "나머지 청크는 계속 시도합니다."
                )
            card_index += len(chunk)

        return success

    all_success = asyncio.run(_run())

    if all_success:
        logger.info(f"텔레그램 전송 완료: {total}장 전송 성공.")
    else:
        logger.error(f"텔레그램 전송 부분 실패: 일부 청크가 전송되지 않았습니다.")

    return all_success


def weekly_cost_report() -> bool:
    """
    이번 주(월~오늘) API 비용 리포트를 텔레그램으로 전송한다.

    logs/ 디렉토리의 *_cost.jsonl 파일을 읽어
    총 호출 수, 총 비용, GP별 비용, 모델별 비용을 정리하여 전송한다.

    Returns:
        True: 전송 성공, False: 전송 실패 또는 자격 증명 없음
    """
    if not _check_credentials():
        return False

    logger.info("주간 비용 리포트 생성 중...")

    entries = _load_week_entries(_LOG_DIR)
    report_text = _build_weekly_report_text(entries)

    logger.info(
        f"주간 리포트: {len(entries)}건 API 호출 기록 집계 완료. 전송 시작..."
    )

    async def _run() -> bool:
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        return await _send_text_async(
            bot=bot,
            chat_id=TELEGRAM_CHAT_ID,
            text=report_text,
        )

    success = asyncio.run(_run())

    if success:
        logger.info("주간 비용 리포트 전송 완료.")
    else:
        logger.error("주간 비용 리포트 전송 실패.")

    return success
