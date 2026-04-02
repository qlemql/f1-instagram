"""migrate_drafts.py
기존 data/drafts/ JSON 파일을 Q/A 분리 형식으로 마이그레이션한다.

처리 규칙:
  - text_kr이 "Q." 또는 "Q:" 로 시작하고 "\n\n" 포함 → Q+A 합쳐진 슬라이드
      · "\n\n" 기준으로 분리
      · Q 부분 → slide_type="question" (Q./Q: 접두사 제거)
      · A 부분 → 80~120자씩 문장 완결 단위로 분할하여 slide_type="answer"
  - 그 외 → slide_type="answer" (이전 답변의 overflow)
  - slide_num 재번호: 커버=1, 이후 순차
  - speaker_kr 필드를 DRIVER_NAME_KR 테이블로 업데이트
  - 결과를 같은 파일에 덮어쓰기 (백업 먼저)
  - 20장 제한 확인 (커버1 + 본문 + 출처1)

실행: python3 scripts/migrate_drafts.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
_PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from models import DRIVER_NAME_KR, get_driver_name_kr

# ── 상수 ─────────────────────────────────────────────────────────────────────
_DRAFTS_DIR       = _PROJECT_DIR / "data" / "drafts"
_BACKUP_DIR       = _DRAFTS_DIR / "backup"
_MAX_TOTAL_SLIDES = 20   # 커버(1) + 본문 + 출처(1)
_MAX_BODY_SLIDES  = _MAX_TOTAL_SLIDES - 2  # = 18
_ANSWER_TARGET    = 200  # 목표 글자 수
_ANSWER_MIN       = 80   # 최소 (이보다 짧으면 다음 문장까지 포함)
_ANSWER_MAX       = 280  # 최대 (이보다 길면 강제 분할)


# ── 텍스트 분할 유틸 ──────────────────────────────────────────────────────────

def _split_answer(text: str) -> list[str]:
    """답변 텍스트를 문장 완결 단위로 슬라이드 분할한다.

    전략:
    1. 먼저 문장 단위로 분리 (마침표, 물음표, 느낌표 기준)
    2. 문장들을 _ANSWER_TARGET(100자) 근처가 되도록 묶음
    3. _ANSWER_MIN(60자) 미만이면 다음 문장과 합침
    """
    text = text.strip()
    if not text:
        return []

    if len(text) <= _ANSWER_MAX:
        return [text]

    # 1단계: 문장 단위로 분리
    # 한국어 문장 종결: ~요. ~다. ~죠. ~까? 등 + 영어 마침표
    sentences = re.split(r'(?<=[.!?])\s+', text)
    # 빈 문장 제거
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return [text]

    # 2단계: 문장들을 적절한 크기로 묶기
    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        candidate = (current + " " + sentence).strip() if current else sentence

        if len(candidate) <= _ANSWER_MAX:
            # 아직 여유 있음 → 계속 합침
            current = candidate
        else:
            # 합치면 초과 → 현재까지를 저장하고 새로 시작
            if current:
                chunks.append(current)
            # 이 문장 자체가 MAX 초과하면 강제 분할
            if len(sentence) > _ANSWER_MAX:
                # 공백 기준으로 강제 분할
                words = sentence.split()
                current = ""
                for word in words:
                    test = (current + " " + word).strip() if current else word
                    if len(test) > _ANSWER_MAX and current:
                        chunks.append(current)
                        current = word
                    else:
                        current = test
            else:
                current = sentence

    if current:
        chunks.append(current)

    return [c.strip() for c in chunks if c.strip()]


def _strip_question_prefix(text: str) -> str:
    """'Q.' 또는 'Q:' 접두사와 이후 공백을 제거한다."""
    text = text.strip()
    if text.startswith("Q."):
        text = text[2:].lstrip()
    elif text.startswith("Q:"):
        text = text[2:].lstrip()
    return text


# ── 슬라이드 변환 ─────────────────────────────────────────────────────────────

def _process_slides(raw_slides: list[dict]) -> list[dict]:
    """원본 슬라이드 목록을 Q/A 분리 형식으로 변환한다.

    Returns:
        변환된 슬라이드 딕셔너리 목록 (slide_num 미포함, 이후 재번호 매김)
    """
    new_slides: list[dict] = []

    for slide in raw_slides:
        text_kr  = slide.get("text_kr", "").strip()
        text_en  = slide.get("text_en", "").strip()

        is_qa = (
            (text_kr.startswith("Q.") or text_kr.startswith("Q:"))
            and "\n\n" in text_kr
        )

        if is_qa:
            # "\n\n" 기준으로 Q / A 분리
            split_idx = text_kr.index("\n\n")
            q_part = text_kr[:split_idx].strip()
            a_part = text_kr[split_idx:].strip()

            # Q 슬라이드
            q_text = _strip_question_prefix(q_part)
            new_slides.append({
                "text_kr":    q_text,
                "text_en":    "",
                "slide_type": "question",
            })

            # A 슬라이드 (분할)
            a_chunks = _split_answer(a_part)
            for chunk in a_chunks:
                new_slides.append({
                    "text_kr":    chunk,
                    "text_en":    "",
                    "slide_type": "answer",
                })

            # text_en은 첫 번째 답변 슬라이드에만 기록
            if a_chunks and text_en:
                # text_en을 Q/A 모두에서 빈 문자열로 두거나 참고용으로
                # 마지막 추가 슬라이드에 기록 — 여기서는 생략 (렌더러 미사용)
                pass

        else:
            # 기존 overflow 답변 슬라이드
            a_chunks = _split_answer(text_kr) if len(text_kr) > _ANSWER_MAX else [text_kr]
            for chunk in a_chunks:
                new_slides.append({
                    "text_kr":    chunk,
                    "text_en":    text_en if chunk == a_chunks[0] else "",
                    "slide_type": "answer",
                })

    # 짧은 answer 슬라이드 병합
    new_slides = _merge_short_answers(new_slides)

    return new_slides


_MERGE_MIN = 80   # 이 길이 미만이면 병합 대상
_MERGE_MAX = 280  # 병합 결과가 이 길이를 초과하면 병합하지 않음


def _merge_short_answers(slides: list[dict]) -> list[dict]:
    """연속된 answer 슬라이드 중 짧은 것을 이전 answer에 병합한다."""
    if not slides:
        return slides

    merged: list[dict] = [slides[0]]

    for s in slides[1:]:
        prev = merged[-1]

        # 현재 슬라이드가 answer이고 짧으며, 이전도 answer일 때 병합 시도
        if (
            s["slide_type"] == "answer"
            and prev["slide_type"] == "answer"
            and len(s["text_kr"]) < _MERGE_MIN
            and len(prev["text_kr"]) + len(s["text_kr"]) + 1 <= _MERGE_MAX
        ):
            prev["text_kr"] = prev["text_kr"].rstrip() + " " + s["text_kr"].lstrip()
        else:
            merged.append(s)

    return merged


def _renumber_slides(slides: list[dict]) -> list[dict]:
    """slide_num을 2부터 순차 재번호 매긴다. (커버=1, 본문=2~N)"""
    result = []
    for i, s in enumerate(slides, start=2):
        result.append({
            "slide_num": i,
            "text_kr":   s["text_kr"],
            "text_en":   s.get("text_en", ""),
            "slide_type": s.get("slide_type", "answer"),
        })
    return result


# ── CarouselSet 마이그레이션 ──────────────────────────────────────────────────

def migrate_carousel(carousel: dict) -> dict:
    """CarouselSet 딕셔너리를 마이그레이션한다."""
    # speaker_kr 업데이트
    speaker     = carousel.get("speaker", "")
    speaker_kr  = get_driver_name_kr(speaker)

    # 슬라이드 변환
    raw_slides  = carousel.get("slides", [])
    new_slides  = _process_slides(raw_slides)

    # 20장 제한 확인: 커버(1) + 본문(len) + 출처(1) <= 20
    # 즉 본문 슬라이드 최대 18장
    if len(new_slides) > _MAX_BODY_SLIDES:
        print(
            f"  [경고] {speaker}: 슬라이드 {len(new_slides)}장 → "
            f"{_MAX_BODY_SLIDES}장으로 자름"
        )
        new_slides = new_slides[:_MAX_BODY_SLIDES]

    # slide_num 재번호
    numbered_slides = _renumber_slides(new_slides)

    result = dict(carousel)
    result["speaker_kr"] = speaker_kr
    result["slides"]     = numbered_slides
    return result


# ── CarouselBatch 마이그레이션 ────────────────────────────────────────────────

def migrate_batch_file(json_path: Path) -> None:
    """CarouselBatch JSON 파일을 마이그레이션하고 덮어쓴다."""
    print(f"\n처리 중: {json_path.name}")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 백업
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup  = _BACKUP_DIR / f"{json_path.stem}_{ts}.json"
    shutil.copy2(json_path, backup)
    print(f"  백업: {backup.name}")

    if "carousels" in data:
        # CarouselBatch 형식
        migrated_carousels = []
        for c in data["carousels"]:
            mc = migrate_carousel(c)
            total = 1 + len(mc["slides"]) + 1
            print(
                f"  └ {mc['speaker']:20s} → speaker_kr={mc['speaker_kr']:10s} "
                f"슬라이드 {len(mc['slides'])}장 (합계 {total}장)"
            )
            migrated_carousels.append(mc)
        data["carousels"] = migrated_carousels

    else:
        # CarouselSet 단독 형식
        migrated = migrate_carousel(data)
        total = 1 + len(migrated["slides"]) + 1
        print(
            f"  └ {migrated['speaker']:20s} → speaker_kr={migrated['speaker_kr']:10s} "
            f"슬라이드 {len(migrated['slides'])}장 (합계 {total}장)"
        )
        data = migrated

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"  저장 완료: {json_path.name}")


# ── 단독 CarouselSet JSON 마이그레이션 ───────────────────────────────────────

def migrate_single_file(json_path: Path) -> None:
    """CarouselSet 단독 JSON 파일을 마이그레이션한다."""
    print(f"\n처리 중 (단독): {json_path.name}")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup  = _BACKUP_DIR / f"{json_path.stem}_{ts}.json"
    shutil.copy2(json_path, backup)
    print(f"  백업: {backup.name}")

    migrated = migrate_carousel(data)
    total    = 1 + len(migrated["slides"]) + 1
    print(
        f"  └ {migrated['speaker']:20s} → speaker_kr={migrated['speaker_kr']:10s} "
        f"슬라이드 {len(migrated['slides'])}장 (합계 {total}장)"
    )

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(migrated, f, ensure_ascii=False, indent=2)

    print(f"  저장 완료: {json_path.name}")


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    json_files = sorted(_DRAFTS_DIR.glob("*.json"))
    if not json_files:
        print("드래프트 파일이 없습니다.")
        return

    print(f"총 {len(json_files)}개 파일 발견")

    for path in json_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                probe = json.load(f)

            if "carousels" in probe:
                migrate_batch_file(path)
            else:
                migrate_single_file(path)

        except Exception as e:
            print(f"  [오류] {path.name}: {e}")

    print("\n마이그레이션 완료.")


if __name__ == "__main__":
    main()
