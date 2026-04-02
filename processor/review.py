"""F1 Instagram 카드뉴스 자동화 — 터미널 대화형 검수 모듈

검수 흐름:
  1. data/drafts/ 폴더의 CarouselBatch JSON 파일을 순회
  2. 드라이버별 캐러셀(CarouselSet)을 순서대로 출력
     - 커버 헤드라인 + 슬라이드 내용 미리보기
  3. 승인(y) / 수정(e) / 제거(n) / 전체 스킵(s) 선택
  4. 승인된 CarouselBatch만 반환
  5. 피드백(승인/거부/수정 내역)을 data/feedback/ 에 JSONL로 저장
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from models import CarouselBatch, CarouselSet, InterviewSlide

logger = logging.getLogger(__name__)

# 데이터 디렉토리
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DRAFTS_DIR   = _PROJECT_ROOT / "data" / "drafts"
_BACKUP_DIR   = _PROJECT_ROOT / "data" / "drafts_backup"
_FEEDBACK_DIR = _PROJECT_ROOT / "data" / "feedback"


# ──────────────────────────────────────────────
# 내부 유틸리티
# ──────────────────────────────────────────────

def _ensure_dirs() -> None:
    _DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    _FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)


def _save_feedback(
    draft_path: Path,
    batch: CarouselBatch,
    action: str,
    edits: Optional[dict] = None,
) -> None:
    """승인/거부/수정 내역을 data/feedback/ 에 JSONL로 저장한다."""
    _ensure_dirs()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    feedback_path = _FEEDBACK_DIR / f"feedback_{today}.jsonl"

    record = {
        "draft_file": draft_path.name,
        "gp_name": batch.gp_name,
        "conference_type": batch.conference_type,
        "action": action,          # "approved" | "rejected" | "edited"
        "edits": edits or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    with open(feedback_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.debug(f"피드백 저장: {feedback_path.name} — {action}")


def _save_driver_feedback(
    draft_path: Path,
    carousel: CarouselSet,
    action: str,
    edits: Optional[dict] = None,
) -> None:
    """드라이버 단위 승인/거부/수정 내역을 저장한다."""
    _ensure_dirs()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    feedback_path = _FEEDBACK_DIR / f"feedback_{today}.jsonl"

    record = {
        "draft_file": draft_path.name,
        "driver": carousel.speaker_kr,
        "speaker": carousel.speaker,
        "team": carousel.team,
        "gp_name": carousel.gp_name,
        "conference_type": carousel.conference_type,
        "action": action,
        "edits": edits or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    with open(feedback_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.debug(f"드라이버 피드백 저장: {carousel.speaker_kr} — {action}")


def _backup_draft(draft_path: Path) -> None:
    """수정 전 원본을 data/drafts_backup/ 에 복사한다."""
    _ensure_dirs()
    dest = _BACKUP_DIR / draft_path.name
    if not dest.exists():
        shutil.copy2(draft_path, dest)
        logger.debug(f"원본 백업: {dest}")


def _save_batch(batch: CarouselBatch, draft_path: Path) -> None:
    """수정된 CarouselBatch를 드래프트 파일에 덮어씁니다."""
    batch.to_json(str(draft_path))
    logger.debug(f"드래프트 업데이트: {draft_path.name}")


def _wrap(text: str, width: int = 60, indent: str = "") -> str:
    """긴 텍스트를 width 너비로 줄바꿈한다."""
    words = text.split()
    lines: list[str] = []
    current = indent
    for word in words:
        if current == indent:
            current += word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = indent + word
    if current:
        lines.append(current)
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 드라이버 단위 수정 모드
# ──────────────────────────────────────────────

def _edit_carousel(carousel: CarouselSet) -> tuple[CarouselSet, dict]:
    """단일 CarouselSet을 대화형으로 수정한다.

    수정 옵션:
      1. 커버 요약 (cover_headline)
      2. 슬라이드 내용 (특정 슬라이드 번호 선택)
      3. 드라이버 포함/제외할 슬라이드 선택
    """
    all_edits: dict = {}

    while True:
        print()
        print("수정할 항목:")
        print("  1. 커버 요약")
        print("  2. 슬라이드 내용")
        print("  3. 포함/제외할 슬라이드 선택")
        print("  0. 수정 완료")

        choice = input("선택: ").strip()

        if choice == "1":
            print(f"현재: \"{carousel.cover_headline}\"")
            new_val = input("새 값: ").strip()
            if new_val:
                all_edits["cover_headline"] = {
                    "before": carousel.cover_headline,
                    "after": new_val,
                }
                carousel.cover_headline = new_val
                print("수정 완료.")

        elif choice == "2":
            total = len(carousel.slides)
            if total == 0:
                print("슬라이드가 없습니다.")
                continue
            print(f"슬라이드 번호를 입력하세요 (1~{total}): ", end="")
            idx_str = input().strip()
            try:
                idx = int(idx_str) - 1
                if 0 <= idx < total:
                    slide = carousel.slides[idx]
                    print(f"현재 슬라이드 {idx + 1}:")
                    print(f"  {slide.text_kr}")
                    new_val = input("새 내용: ").strip()
                    if new_val:
                        old_text = slide.text_kr
                        slide.text_kr = new_val
                        carousel.slides[idx] = slide
                        all_edits[f"slide_{idx + 1}"] = {
                            "before": old_text,
                            "after": new_val,
                        }
                        print("수정 완료.")
                else:
                    print("범위를 벗어난 번호입니다.")
            except ValueError:
                print("숫자를 입력해주세요.")

        elif choice == "3":
            total = len(carousel.slides)
            if total == 0:
                print("슬라이드가 없습니다.")
                continue
            print()
            print("포함할 슬라이드 번호를 쉼표로 입력하세요 (예: 1,2,4).")
            print("현재 슬라이드 목록:")
            for i, slide in enumerate(carousel.slides, 1):
                preview = slide.text_kr[:40] + ("..." if len(slide.text_kr) > 40 else "")
                print(f"  {i}. {preview}")
            raw = input("포함할 번호: ").strip()
            try:
                keep_indices = {int(x.strip()) - 1 for x in raw.split(",") if x.strip()}
                original_count = len(carousel.slides)
                removed_nums = []
                kept = []
                for i, slide in enumerate(carousel.slides):
                    if i in keep_indices:
                        kept.append(slide)
                    else:
                        removed_nums.append(i + 1)
                carousel.slides = kept
                if removed_nums:
                    all_edits["removed_slides"] = removed_nums
                    print(f"제거된 슬라이드: {removed_nums}")
                print(f"{original_count}장 → {len(kept)}장으로 조정 완료.")
            except ValueError:
                print("올바른 숫자를 입력해주세요.")

        elif choice == "0":
            break
        else:
            print("잘못된 입력입니다.")

    return carousel, all_edits


# ──────────────────────────────────────────────
# 단일 드라이버 검수
# ──────────────────────────────────────────────

class _SkipAll(Exception):
    """검수 중 전체 스킵(s) 선택 시 발생하는 내부 예외."""
    pass


def _review_one_carousel(
    carousel: CarouselSet,
    draft_path: Path,
    driver_index: int,
    total_drivers: int,
) -> Optional[CarouselSet]:
    """단일 드라이버의 캐러셀을 대화형으로 검수한다.

    반환값:
        승인된 CarouselSet (수정 포함) 또는 None (제거)
    Raises:
        _SkipAll: 전체 스킵(s) 선택 시
    """
    total_slides = len(carousel.slides)

    print()
    print("-" * 60)
    print(
        f"[드라이버 {driver_index}/{total_drivers}] "
        f"{carousel.speaker_kr} ({carousel.team})"
    )
    print(f"커버 요약: \"{carousel.cover_headline}\"")
    print(f"인터뷰 슬라이드: {total_slides}장")

    for i, slide in enumerate(carousel.slides, 1):
        print()
        print(f"--- 슬라이드 {i}/{total_slides} ---")
        wrapped = _wrap(slide.text_kr, width=55, indent="")
        print(wrapped)

    print()
    answer = input("→ 승인(y) / 수정(e) / 제거(n) / 전체 스킵(s)? ").strip().lower()

    if answer == "y":
        _save_driver_feedback(draft_path, carousel, "approved")
        print("승인되었습니다.")
        return carousel

    elif answer == "e":
        carousel, edits = _edit_carousel(carousel)
        _save_driver_feedback(draft_path, carousel, "edited", edits)
        print("수정 내용이 저장되었습니다. 승인합니다.")
        return carousel

    elif answer == "n":
        _save_driver_feedback(draft_path, carousel, "rejected")
        print("제거되었습니다.")
        return None

    elif answer == "s":
        raise _SkipAll()

    else:
        print("잘못된 입력입니다. 기본값으로 제거합니다.")
        _save_driver_feedback(draft_path, carousel, "rejected")
        return None


# ──────────────────────────────────────────────
# 단일 CarouselBatch 검수
# ──────────────────────────────────────────────

def _review_one_batch(
    batch: CarouselBatch,
    draft_path: Path,
    batch_index: int,
    total_batches: int,
) -> Optional[CarouselBatch]:
    """단일 CarouselBatch를 드라이버별로 대화형 검수한다.

    반환값:
        승인 드라이버가 1명 이상인 CarouselBatch 또는 None (전부 거부)
    Raises:
        _SkipAll: 전체 스킵(s) 선택 시
    """
    print()
    print("=" * 60)
    print(
        f"[배치 {batch_index}/{total_batches}] "
        f"{batch.gp_name} — {batch.conference_type}"
    )
    print(f"날짜: {batch.date_iso}  |  드라이버: {len(batch.carousels)}명")
    print("=" * 60)

    approved_carousels: list[CarouselSet] = []
    total_drivers = len(batch.carousels)

    for driver_idx, carousel in enumerate(batch.carousels, 1):
        result = _review_one_carousel(
            carousel, draft_path, driver_idx, total_drivers
        )
        if result is not None:
            approved_carousels.append(result)

    if not approved_carousels:
        _save_feedback(draft_path, batch, "rejected")
        print("이 배치의 모든 드라이버가 제거되었습니다.")
        return None

    # 승인된 드라이버만 포함한 새 배치 반환
    batch.carousels = approved_carousels
    _save_feedback(
        draft_path,
        batch,
        "approved" if len(approved_carousels) == total_drivers else "edited",
    )
    return batch


# ──────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────

def review_drafts(drafts_dir: Optional[str | Path] = None) -> list[CarouselBatch]:
    """data/drafts/ 폴더의 드래프트를 대화형으로 검수하고 승인된 것만 반환한다.

    Args:
        drafts_dir: 드래프트 폴더 경로 (None이면 data/drafts/ 사용)

    Returns:
        승인된 CarouselBatch 목록 (수정 내용 반영됨)
    """
    _ensure_dirs()

    target = Path(drafts_dir) if drafts_dir else _DRAFTS_DIR
    draft_files = sorted(target.glob("*.json"))

    if not draft_files:
        print(f"검수할 드래프트가 없습니다: {target}")
        return []

    print()
    print(f"검수 대상: {len(draft_files)}개 드래프트 파일")
    print("(Ctrl+C로 중단해도 이미 처리된 파일은 유지됩니다)")

    approved: list[CarouselBatch] = []
    total = len(draft_files)

    for file_idx, draft_path in enumerate(draft_files, 1):
        try:
            batch = CarouselBatch.from_json(str(draft_path))
        except Exception as e:
            logger.error(f"드래프트 파일 로드 실패: {draft_path.name} — {e}")
            print(f"[오류] {draft_path.name} 파일을 읽을 수 없어 건너뜁니다.")
            continue

        try:
            result = _review_one_batch(batch, draft_path, file_idx, total)
            if result is not None:
                # 수정된 내용을 드래프트 파일에 반영 (원본은 이미 백업됨)
                _backup_draft(draft_path)
                _save_batch(result, draft_path)
                approved.append(result)
        except _SkipAll:
            print()
            print("전체 스킵을 선택했습니다. 검수를 종료합니다.")
            print(f"(지금까지 승인된 {len(approved)}개 배치는 렌더링됩니다)")
            break
        except KeyboardInterrupt:
            print()
            print("검수가 중단되었습니다 (Ctrl+C).")
            print(f"(지금까지 승인된 {len(approved)}개 배치는 렌더링됩니다)")
            break

    print()
    print(f"검수 완료: 전체 {total}개 중 {len(approved)}개 배치 승인됨")
    return approved


def save_drafts(
    batches: list[CarouselBatch],
    drafts_dir: Optional[str | Path] = None,
) -> list[Path]:
    """CarouselBatch 목록을 data/drafts/ 폴더에 JSON으로 저장한다.

    AI 파이프라인 완료 후 main.py에서 호출된다.

    Args:
        batches: 저장할 CarouselBatch 목록
        drafts_dir: 저장 폴더 경로 (None이면 data/drafts/ 사용)

    Returns:
        저장된 파일 경로 목록
    """
    _ensure_dirs()
    target = Path(drafts_dir) if drafts_dir else _DRAFTS_DIR

    saved_paths: list[Path] = []
    for batch in batches:
        # 파일명: {gp_name_slug}_{conference_type}_{date}.json
        slug = batch.gp_name.lower().replace(" ", "_")
        filename = f"{slug}_{batch.conference_type}_{batch.date_iso}.json"
        path = target / filename
        batch.to_json(str(path))
        saved_paths.append(path)
        logger.info(f"드래프트 저장: {path.name}")

    return saved_paths
