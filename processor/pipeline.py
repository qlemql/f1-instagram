"""F1 Instagram 카드뉴스 자동화 — 3단계 AI 선별 파이프라인

흐름:
  1. 드라이버 발언 추출 (질문 제외)
  2. 1차 선별: Haiku로 각 발언 점수 채점 (배치, 5~6개씩)
  3. 7점 이상 필터
  4. 2차 선별: Haiku로 최종 3~5개 선정 (1회 호출)
  5. 3차 검토: Sonnet 조건부 — 점수 분산 ≥ 4, 선정 수 < 1, 밈 후보 ≥ 2
  6. 번역: Haiku로 한국어 번역 (배치)
  7. 요약: Haiku로 메인/서브 카피 생성 (배치)
  8. 밈 판단: card_type == "meme"인 카드 Sonnet으로 최종 확인
  9. CardSet 반환
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from pathlib import Path
from typing import Optional

from anthropic import Anthropic

from config import HAIKU_MODEL, SONNET_MODEL, CONTENT
from models import (
    CardContent,
    CardSet,
    PressConference,
    ScoredStatement,
    SelectedQuote,
    TranslatedQuote,
    get_team_for_driver,
)
from processor.cost_guard import CostGuard

logger = logging.getLogger(__name__)

# 프롬프트 디렉토리
_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "v1"

# CostGuard에서 사용하는 모델 식별자 (버전 없는 short name)
_HAIKU_GUARD  = "claude-haiku-4-5"
_SONNET_GUARD = "claude-sonnet-4-6"

# 1차 선별 배치 크기
_FIRST_PASS_BATCH_SIZE = 5


# ──────────────────────────────────────────────
# 유틸리티
# ──────────────────────────────────────────────

def _load_prompt(name: str) -> str:
    """prompts/v1/{name}.txt 를 읽어 반환한다."""
    path = _PROMPT_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")


def _extract_json(text: str) -> str:
    """
    Claude 응답에서 JSON 블록을 추출한다.
    ```json ... ``` 펜스가 있으면 내부만, 없으면 전체 텍스트를 시도한다.
    """
    # 코드 펜스 우선 추출
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 코드 펜스 없이 JSON 오브젝트/배열 직접 탐색
    m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    return text.strip()


def _call_api(
    client: Anthropic,
    guard: CostGuard,
    model: str,
    guard_model: str,
    prompt_stage: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int = 1024,
    quote_id: Optional[str] = None,
) -> Optional[str]:
    """
    Claude API를 호출하고 비용을 기록한다.
    실패 시 None을 반환한다 (호출부에서 스킵 처리).
    """
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        content_text = response.content[0].text

        guard.record(
            model=guard_model,
            prompt_stage=prompt_stage,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            quote_id=quote_id,
        )
        return content_text

    except Exception as exc:
        logger.warning("[pipeline] API 호출 실패 (stage=%s, quote_id=%s): %s",
                       prompt_stage, quote_id, exc)
        return None


# ──────────────────────────────────────────────
# 단계 1: 드라이버 발언 추출
# ──────────────────────────────────────────────

def _extract_driver_statements(conference: PressConference) -> list:
    """is_question=False 인 발언만 반환한다."""
    return conference.driver_statements()


# ──────────────────────────────────────────────
# 단계 2: 1차 선별 — 점수 채점 (배치)
# ──────────────────────────────────────────────

def _first_pass_batch(
    client: Anthropic,
    guard: CostGuard,
    statements: list,
    event_label: str,
    system_prompt: str,
) -> list[ScoredStatement]:
    """
    발언 목록을 _FIRST_PASS_BATCH_SIZE 단위로 묶어 Haiku에 점수를 요청한다.
    발언마다 개별 JSON을 반환받는 방식이므로, 배치 내 각 발언에 순차 호출한다.

    비용 최적화: 배치 단위로 system prompt를 재사용하고,
    user message를 여러 발언이 포함된 단일 요청으로 묶는다.
    단, 현재 first_pass.txt 프롬프트는 발언 1개 기준이므로
    배치 호출은 "묶어서 한 번에 요청 + 배열 응답" 방식으로 처리한다.
    """
    scored: list[ScoredStatement] = []

    # 배치 단위로 분할
    for batch_start in range(0, len(statements), _FIRST_PASS_BATCH_SIZE):
        batch = statements[batch_start: batch_start + _FIRST_PASS_BATCH_SIZE]

        # 배치를 JSON 배열로 구성
        items = []
        for stmt in batch:
            items.append({
                "id": f"q{stmt.seq}",
                "speaker": stmt.speaker,
                "event": event_label,
                "quote": stmt.text,
            })

        # 배치 전용 user message: 여러 발언을 한 번에 채점 요청
        user_msg = (
            "Score each of the following quotes individually.\n"
            "Return a JSON array where each element has: "
            "{\"id\": \"<id>\", \"score\": <0-10>, \"reason\": \"<Korean>\"}.\n"
            "Respond ONLY with the JSON array.\n\n"
            f"Quotes:\n{json.dumps(items, ensure_ascii=False, indent=2)}"
        )

        raw = _call_api(
            client=client,
            guard=guard,
            model=HAIKU_MODEL,
            guard_model=_HAIKU_GUARD,
            prompt_stage="first_pass",
            system_prompt=system_prompt,
            user_message=user_msg,
            max_tokens=512,
            quote_id=f"batch_{batch_start}",
        )

        if raw is None:
            logger.warning("[first_pass] 배치 %d 스킵 (API 오류)", batch_start)
            continue

        # JSON 파싱
        try:
            json_str = _extract_json(raw)
            results = json.loads(json_str)

            # 단일 오브젝트로 왔을 때 배열로 감싸기
            if isinstance(results, dict):
                results = [results]

            # id → statement 매핑
            id_to_stmt = {f"q{s.seq}": s for s in batch}

            for item in results:
                qid = item.get("id", "")
                stmt = id_to_stmt.get(qid)
                if stmt is None:
                    continue
                scored.append(ScoredStatement(
                    speaker=stmt.speaker,
                    text=stmt.text,
                    score=int(item.get("score", 0)),
                    reason=item.get("reason", ""),
                    seq=stmt.seq,
                ))

        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("[first_pass] JSON 파싱 실패 (배치 %d): %s | raw=%s",
                           batch_start, exc, raw[:200])
            # 파싱 실패 시 배치의 각 발언을 score=0으로 삽입하지 않고 스킵
            continue

    return scored


# ──────────────────────────────────────────────
# 단계 4: 2차 선별
# ──────────────────────────────────────────────

def _second_pass(
    client: Anthropic,
    guard: CostGuard,
    candidates: list[ScoredStatement],
    event_label: str,
    system_prompt: str,
) -> tuple[list[str], str]:
    """
    후보 발언 전체를 1회 호출로 최종 3~5개 선정한다.
    반환: (선정된 id 목록, 선정 근거)
    """
    items = []
    for s in candidates:
        items.append({
            "id": f"q{s.seq}",
            "speaker": s.speaker,
            "event": event_label,
            "quote": s.text,
            "score": s.score,
            "reason": s.reason,
        })

    user_msg = json.dumps(items, ensure_ascii=False, indent=2)

    raw = _call_api(
        client=client,
        guard=guard,
        model=HAIKU_MODEL,
        guard_model=_HAIKU_GUARD,
        prompt_stage="second_pass",
        system_prompt=system_prompt,
        user_message=user_msg,
        max_tokens=512,
    )

    if raw is None:
        return [], "API 오류로 선정 실패"

    try:
        json_str = _extract_json(raw)
        data = json.loads(json_str)
        selected_ids = data.get("selected", [])
        rationale = data.get("rationale", "")
        return selected_ids, rationale
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("[second_pass] JSON 파싱 실패: %s | raw=%s", exc, raw[:200])
        return [], "파싱 실패"


# ──────────────────────────────────────────────
# 단계 5: 3차 검토 (조건부 Sonnet)
# ──────────────────────────────────────────────

def _should_trigger_quality_check(
    all_scored: list[ScoredStatement],
    selected_ids: list[str],
) -> tuple[bool, str]:
    """
    3차 검토 트리거 조건 판단.
    반환: (트리거 여부, 트리거 사유)
    """
    reasons = []

    # 조건 1: 점수 분산 ≥ 4
    scores = [s.score for s in all_scored]
    if len(scores) >= 2:
        variance = statistics.variance(scores)
        if variance >= CONTENT["sonnet_trigger_score_variance"]:
            reasons.append(f"점수 분산 {variance:.1f} ≥ {CONTENT['sonnet_trigger_score_variance']}")

    # 조건 2: 선정 발언 수 < 1
    if len(selected_ids) < 1:
        reasons.append("선정 발언 없음")

    # 조건 3: 밈 후보 ≥ 2 (score 7 이상 & reason에 '밈' 포함하거나 score ≥ 8인 짧은 발언)
    meme_candidates = [
        s for s in all_scored
        if s.score >= 7 and (
            "밈" in s.reason
            or "meme" in s.reason.lower()
            or (s.score >= 9 and len(s.text.split()) <= 15)
        )
    ]
    if len(meme_candidates) >= 2:
        reasons.append(f"밈 후보 {len(meme_candidates)}개")

    trigger = len(reasons) > 0
    return trigger, " / ".join(reasons) if reasons else ""


def _quality_check(
    client: Anthropic,
    guard: CostGuard,
    all_scored: list[ScoredStatement],
    selected_ids: list[str],
    rationale: str,
    trigger_reason: str,
    system_prompt: str,
) -> tuple[list[str], str]:
    """
    Sonnet으로 3차 검토를 수행한다.
    APPROVE/REVISE/REJECT 판정을 반환하고,
    REJECT가 아니면 selected_ids를 그대로 유지한다.
    반환: (최종 selected_ids, final_recommendation)
    """
    all_quotes_payload = []
    for s in all_scored:
        meme_candidate = (
            "밈" in s.reason
            or "meme" in s.reason.lower()
            or (s.score >= 9 and len(s.text.split()) <= 15)
        )
        all_quotes_payload.append({
            "id": f"q{s.seq}",
            "speaker": s.speaker,
            "quote": s.text,
            "score": s.score,
            "reason": s.reason,
            "meme_candidate": meme_candidate,
        })

    payload = {
        "all_quotes": all_quotes_payload,
        "selected_ids": selected_ids,
        "selection_rationale": rationale,
        "trigger_reason": trigger_reason,
    }

    raw = _call_api(
        client=client,
        guard=guard,
        model=SONNET_MODEL,
        guard_model=_SONNET_GUARD,
        prompt_stage="quality_check",
        system_prompt=system_prompt,
        user_message=json.dumps(payload, ensure_ascii=False, indent=2),
        max_tokens=1024,
    )

    if raw is None:
        logger.warning("[quality_check] Sonnet 호출 실패, 2차 선별 결과 유지")
        return selected_ids, "APPROVE"

    try:
        json_str = _extract_json(raw)
        data = json.loads(json_str)
        recommendation = data.get("final_recommendation", "APPROVE")
        reason = data.get("recommendation_reason", "")
        logger.info("[quality_check] %s — %s", recommendation, reason)

        if recommendation == "REJECT":
            # REJECT: 선정 목록 비우기 (추후 fallback 처리)
            logger.warning("[quality_check] REJECT — 선정 발언 전체 제거")
            return [], recommendation

        return selected_ids, recommendation

    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("[quality_check] JSON 파싱 실패: %s | raw=%s", exc, raw[:200])
        return selected_ids, "APPROVE"


# ──────────────────────────────────────────────
# 단계 5b: card_type 결정
# ──────────────────────────────────────────────

def _determine_card_type(scored: ScoredStatement) -> str:
    """
    발언의 card_type을 결정한다.
    밈 징후 발언 → "meme", 나머지 → "info"
    """
    is_meme = (
        "밈" in scored.reason
        or "meme" in scored.reason.lower()
        or (scored.score >= 9 and len(scored.text.split()) <= 12)
    )
    return "meme" if is_meme else "info"


# ──────────────────────────────────────────────
# 단계 6: 번역 (배치)
# ──────────────────────────────────────────────

def _translate_quotes(
    client: Anthropic,
    guard: CostGuard,
    selected_quotes: list[SelectedQuote],
    event_label: str,
    system_prompt: str,
) -> list[TranslatedQuote]:
    """
    선정된 발언들을 배치로 번역한다.
    프롬프트가 단일 발언 기준이므로 여러 발언을 묶어 배열 응답으로 요청한다.
    """
    if not selected_quotes:
        return []

    items = []
    for sq in selected_quotes:
        items.append({
            "id": f"q{sq.seq}",
            "speaker": sq.speaker,
            "event": event_label,
            "quote": sq.text,
        })

    user_msg = (
        "Translate each of the following quotes.\n"
        "Return a JSON array where each element has: "
        "{\"id\": \"<id>\", \"speaker_ko\": \"<Korean name>\", "
        "\"translation\": \"<Korean translation>\", \"notes\": <null or string>}.\n"
        "Respond ONLY with the JSON array.\n\n"
        f"Quotes:\n{json.dumps(items, ensure_ascii=False, indent=2)}"
    )

    raw = _call_api(
        client=client,
        guard=guard,
        model=HAIKU_MODEL,
        guard_model=_HAIKU_GUARD,
        prompt_stage="translate",
        system_prompt=system_prompt,
        user_message=user_msg,
        max_tokens=1024,
    )

    translated: list[TranslatedQuote] = []
    id_to_sq = {f"q{sq.seq}": sq for sq in selected_quotes}

    if raw is None:
        logger.warning("[translate] 배치 번역 실패, 모든 발언 스킵")
        return []

    try:
        json_str = _extract_json(raw)
        results = json.loads(json_str)

        if isinstance(results, dict):
            results = [results]

        for item in results:
            qid = item.get("id", "")
            sq = id_to_sq.get(qid)
            if sq is None:
                continue
            translated.append(TranslatedQuote(
                speaker=sq.speaker,
                speaker_kr=item.get("speaker_ko", sq.speaker),
                text_en=sq.text,
                text_kr=item.get("translation", ""),
                card_type=sq.card_type,
                seq=sq.seq,
            ))

    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("[translate] JSON 파싱 실패: %s | raw=%s", exc, raw[:200])

    return translated


# ──────────────────────────────────────────────
# 단계 7: 요약 (배치)
# ──────────────────────────────────────────────

def _summarize_quotes(
    client: Anthropic,
    guard: CostGuard,
    translated_quotes: list[TranslatedQuote],
    event_label: str,
    system_prompt: str,
) -> dict[int, dict]:
    """
    번역된 발언들을 배치로 요약한다.
    반환: {seq: {"main_copy": ..., "sub_copy": ...}}
    """
    if not translated_quotes:
        return {}

    items = []
    for tq in translated_quotes:
        items.append({
            "id": f"q{tq.seq}",
            "speaker_ko": tq.speaker_kr,
            "event": event_label,
            "translation": tq.text_kr,
        })

    user_msg = (
        "Write main copy and sub copy for each of the following translated quotes.\n"
        "Return a JSON array where each element has: "
        "{\"id\": \"<id>\", \"main_copy\": \"<25자 이내>\", \"sub_copy\": \"<40자 이내>\"}.\n"
        "Respond ONLY with the JSON array.\n\n"
        f"Quotes:\n{json.dumps(items, ensure_ascii=False, indent=2)}"
    )

    raw = _call_api(
        client=client,
        guard=guard,
        model=HAIKU_MODEL,
        guard_model=_HAIKU_GUARD,
        prompt_stage="summarize",
        system_prompt=system_prompt,
        user_message=user_msg,
        max_tokens=1024,
    )

    copies: dict[int, dict] = {}

    if raw is None:
        logger.warning("[summarize] 배치 요약 실패")
        return {}

    try:
        json_str = _extract_json(raw)
        results = json.loads(json_str)

        if isinstance(results, dict):
            results = [results]

        for item in results:
            qid = item.get("id", "")
            if not qid.startswith("q"):
                continue
            try:
                seq = int(qid[1:])
            except ValueError:
                continue
            copies[seq] = {
                "main_copy": item.get("main_copy", ""),
                "sub_copy": item.get("sub_copy", ""),
            }

    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("[summarize] JSON 파싱 실패: %s | raw=%s", exc, raw[:200])

    return copies


# ──────────────────────────────────────────────
# 단계 8: 밈 판단 (Sonnet, card_type == "meme")
# ──────────────────────────────────────────────

def _meme_judge(
    client: Anthropic,
    guard: CostGuard,
    translated_quotes: list[TranslatedQuote],
    event_label: str,
    system_prompt: str,
) -> dict[int, str]:
    """
    card_type == "meme" 인 발언들을 Sonnet 1회 배치 호출로 최종 확인한다.
    meme_potential == "low" 이면 card_type을 "info"로 강등한다.
    반환: {seq: 최종 card_type}
    """
    meme_quotes = [tq for tq in translated_quotes if tq.card_type == "meme"]
    result: dict[int, str] = {}

    if not meme_quotes:
        return result

    # 밈 후보 전체를 하나의 배열로 묶어 배치 호출
    items = []
    for tq in meme_quotes:
        items.append({
            "id": f"q{tq.seq}",
            "speaker_ko": tq.speaker_kr,
            "event": event_label,
            "translation": tq.text_kr,
            "score": 0,    # 이 단계에서 원점수는 없으므로 0 기본값
            "context": None,
        })

    user_msg = (
        "Judge the meme potential of each of the following quotes.\n"
        "Return a JSON array where each element has: "
        "{\"id\": \"<id>\", \"meme_potential\": \"<high|medium|low>\", \"reason\": \"<Korean>\"}.\n"
        "Respond ONLY with the JSON array.\n\n"
        f"Quotes:\n{json.dumps(items, ensure_ascii=False, indent=2)}"
    )

    raw = _call_api(
        client=client,
        guard=guard,
        model=SONNET_MODEL,
        guard_model=_SONNET_GUARD,
        prompt_stage="meme_judge",
        system_prompt=system_prompt,
        user_message=user_msg,
        max_tokens=512,
        quote_id="batch",
    )

    if raw is None:
        logger.warning("[meme_judge] 배치 호출 실패, 모든 밈 후보 card_type 유지")
        for tq in meme_quotes:
            result[tq.seq] = "meme"
        return result

    try:
        json_str = _extract_json(raw)
        results = json.loads(json_str)

        if isinstance(results, dict):
            results = [results]

        id_to_seq = {f"q{tq.seq}": tq.seq for tq in meme_quotes}

        for item in results:
            qid = item.get("id", "")
            seq = id_to_seq.get(qid)
            if seq is None:
                continue
            potential = item.get("meme_potential", "medium")
            if potential == "low":
                logger.info("[meme_judge] %s: meme_potential=low → card_type=info", qid)
                result[seq] = "info"
            else:
                result[seq] = "meme"

    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("[meme_judge] JSON 파싱 실패: %s | raw=%s", exc, raw[:200])
        for tq in meme_quotes:
            result[tq.seq] = "meme"

    # 배치 응답에서 누락된 항목은 기본값 "meme" 유지
    for tq in meme_quotes:
        if tq.seq not in result:
            logger.warning("[meme_judge] q%d 응답 누락, card_type 유지", tq.seq)
            result[tq.seq] = "meme"

    return result


# ──────────────────────────────────────────────
# 메인 파이프라인
# ──────────────────────────────────────────────

def run_pipeline(conference: PressConference, gp_name: str) -> CardSet:
    """
    3단계 AI 선별 파이프라인 실행.

    Args:
        conference: 수집된 기자회견 데이터
        gp_name:    GP 식별자 (예: "japan_2026") — 비용 로그 파일명에 사용

    Returns:
        CardSet: 렌더러에 전달할 카드 세트
    """
    client = Anthropic()
    guard  = CostGuard(gp_name=gp_name)

    # 이벤트 레이블 (프롬프트에 삽입)
    event_label = f"{conference.gp_name} – {conference.conference_type}"

    # 프롬프트 로드
    prompt_first_pass    = _load_prompt("first_pass")
    prompt_second_pass   = _load_prompt("second_pass")
    prompt_quality_check = _load_prompt("quality_check")
    prompt_translate     = _load_prompt("translate")
    prompt_summarize     = _load_prompt("summarize")
    prompt_meme_judge    = _load_prompt("meme_judge")

    logger.info("[pipeline] 시작: %s (%s)", conference.gp_name, conference.conference_type)

    # ── 단계 1: 드라이버 발언 추출 ──────────────────────
    driver_stmts = _extract_driver_statements(conference)
    logger.info("[pipeline] 드라이버 발언 %d개 추출", len(driver_stmts))

    if not driver_stmts:
        logger.warning("[pipeline] 드라이버 발언 없음 — 빈 CardSet 반환")
        return CardSet(
            gp_name=conference.gp_name,
            conference_type=conference.conference_type,
            date_iso=conference.date_iso,
            cards=[],
            total_cost_usd=guard.gp_total,
        )

    # ── 단계 2: 1차 선별 (배치 채점) ────────────────────
    all_scored = _first_pass_batch(
        client=client,
        guard=guard,
        statements=driver_stmts,
        event_label=event_label,
        system_prompt=prompt_first_pass,
    )
    logger.info("[pipeline] 1차 선별 완료: %d개 채점", len(all_scored))

    # ── 단계 3: 최소 점수 필터 ──────────────────────────
    min_score = CONTENT["min_score_first_pass"]
    candidates = [s for s in all_scored if s.score >= min_score]
    logger.info("[pipeline] %d점 이상 후보: %d개", min_score, len(candidates))

    # 후보가 없을 경우 하위 점수 완화 (최소 3개 확보 시도)
    if len(candidates) < 3 and all_scored:
        all_scored_sorted = sorted(all_scored, key=lambda x: x.score, reverse=True)
        candidates = all_scored_sorted[:max(3, len(candidates))]
        logger.info("[pipeline] 후보 부족 → 상위 %d개로 완화", len(candidates))

    if not candidates:
        logger.warning("[pipeline] 선별 가능 후보 없음 — 빈 CardSet 반환")
        return CardSet(
            gp_name=conference.gp_name,
            conference_type=conference.conference_type,
            date_iso=conference.date_iso,
            cards=[],
            total_cost_usd=guard.gp_total,
        )

    # ── 단계 4: 2차 선별 ────────────────────────────────
    selected_ids, rationale = _second_pass(
        client=client,
        guard=guard,
        candidates=candidates,
        event_label=event_label,
        system_prompt=prompt_second_pass,
    )
    logger.info("[pipeline] 2차 선별 완료: %d개 선정 — %s", len(selected_ids), rationale)

    # ── 단계 5: 3차 검토 (조건부 Sonnet) ────────────────
    trigger, trigger_reason = _should_trigger_quality_check(all_scored, selected_ids)
    if trigger:
        logger.info("[pipeline] 3차 검토 트리거: %s", trigger_reason)
        selected_ids, recommendation = _quality_check(
            client=client,
            guard=guard,
            all_scored=all_scored,
            selected_ids=selected_ids,
            rationale=rationale,
            trigger_reason=trigger_reason,
            system_prompt=prompt_quality_check,
        )
        logger.info("[pipeline] 3차 검토 결과: %s, 선정 %d개", recommendation, len(selected_ids))
    else:
        logger.info("[pipeline] 3차 검토 스킵 (트리거 조건 미충족)")

    # 선정 ID → ScoredStatement 매핑
    id_to_scored: dict[str, ScoredStatement] = {f"q{s.seq}": s for s in all_scored}

    selected_quotes: list[SelectedQuote] = []
    for qid in selected_ids:
        scored = id_to_scored.get(qid)
        if scored is None:
            logger.warning("[pipeline] 선정 ID %s를 scored 목록에서 찾지 못함", qid)
            continue
        card_type = _determine_card_type(scored)
        selected_quotes.append(SelectedQuote(
            speaker=scored.speaker,
            text=scored.text,
            card_type=card_type,
            score=scored.score,
            seq=scored.seq,
        ))

    if not selected_quotes:
        logger.warning("[pipeline] 최종 선정 발언 없음 — 빈 CardSet 반환")
        return CardSet(
            gp_name=conference.gp_name,
            conference_type=conference.conference_type,
            date_iso=conference.date_iso,
            cards=[],
            total_cost_usd=guard.gp_total,
        )

    # ── 단계 6: 번역 ─────────────────────────────────────
    translated_quotes = _translate_quotes(
        client=client,
        guard=guard,
        selected_quotes=selected_quotes,
        event_label=event_label,
        system_prompt=prompt_translate,
    )
    logger.info("[pipeline] 번역 완료: %d개", len(translated_quotes))

    # 번역 실패한 발언은 원문 그대로 사용
    translated_seqs = {tq.seq for tq in translated_quotes}
    for sq in selected_quotes:
        if sq.seq not in translated_seqs:
            logger.warning("[pipeline] q%d 번역 누락, 원문 사용", sq.seq)
            translated_quotes.append(TranslatedQuote(
                speaker=sq.speaker,
                speaker_kr=sq.speaker,
                text_en=sq.text,
                text_kr=sq.text,
                card_type=sq.card_type,
                seq=sq.seq,
            ))

    # ── 단계 7: 요약 ─────────────────────────────────────
    copies = _summarize_quotes(
        client=client,
        guard=guard,
        translated_quotes=translated_quotes,
        event_label=event_label,
        system_prompt=prompt_summarize,
    )
    logger.info("[pipeline] 요약 완료: %d개", len(copies))

    # ── 단계 8: 밈 판단 (Sonnet) ─────────────────────────
    meme_type_overrides = _meme_judge(
        client=client,
        guard=guard,
        translated_quotes=translated_quotes,
        event_label=event_label,
        system_prompt=prompt_meme_judge,
    )
    logger.info("[pipeline] 밈 판단 완료: %d개 처리", len(meme_type_overrides))

    # ── 단계 9: CardSet 조립 ─────────────────────────────
    cards: list[CardContent] = []
    for i, tq in enumerate(sorted(translated_quotes, key=lambda x: x.seq)):
        # 밈 판단 결과 반영
        final_card_type = meme_type_overrides.get(tq.seq, tq.card_type)

        # 카피 가져오기
        copy_data = copies.get(tq.seq, {})
        main_copy = copy_data.get("main_copy", "")
        sub_copy  = copy_data.get("sub_copy", "")

        # 팀 매핑
        team = get_team_for_driver(tq.speaker)

        # GP 표시명 (한국어 형식으로 변환)
        gp_display = _format_gp_display(conference.gp_name, conference.date_iso)

        cards.append(CardContent(
            speaker_kr=tq.speaker_kr,
            team=team,
            main_copy=main_copy,
            sub_copy=sub_copy,
            quote_en=tq.text_en,
            card_type=final_card_type,
            gp_name=gp_display,
            seq=i + 1,
        ))

    # ── 단계 10: 카드 최소 품질 게이트 ──────────────────
    filtered_cards: list[CardContent] = []
    for card in cards:
        reasons = []
        if len(card.main_copy) < 5:
            reasons.append(f"main_copy 너무 짧음 ({len(card.main_copy)}자)")
        if not card.quote_en:
            reasons.append("quote_en 비어 있음")
        if not card.sub_copy:
            reasons.append("sub_copy 비어 있음")

        if reasons:
            logger.warning(
                "[pipeline] 카드 품질 미달 제외 — seq=%d, speaker=%s, 사유: %s",
                card.seq, card.speaker_kr, " / ".join(reasons),
            )
        else:
            filtered_cards.append(card)

    excluded = len(cards) - len(filtered_cards)
    if excluded > 0:
        logger.info("[pipeline] 품질 게이트: %d장 제외, %d장 통과", excluded, len(filtered_cards))

    total_cost = guard.gp_total
    logger.info("[pipeline] 완료: 카드 %d장, 총 비용 $%.5f", len(filtered_cards), total_cost)

    return CardSet(
        gp_name=conference.gp_name,
        conference_type=conference.conference_type,
        date_iso=conference.date_iso,
        cards=filtered_cards,
        total_cost_usd=round(total_cost, 6),
    )


# ──────────────────────────────────────────────
# 헬퍼: GP 표시명 포맷
# ──────────────────────────────────────────────

def _format_gp_display(gp_name: str, date_iso: str) -> str:
    """
    'Japanese Grand Prix' + '2026-03-29' → '2026 일본 GP'
    알 수 없는 GP는 원본 그대로 반환.
    """
    year = date_iso[:4] if date_iso else ""

    _GP_KR_MAP: dict[str, str] = {
        "Australian Grand Prix": "호주 GP",
        "Bahrain Grand Prix": "바레인 GP",
        "Saudi Arabian Grand Prix": "사우디 GP",
        "Japanese Grand Prix": "일본 GP",
        "Chinese Grand Prix": "중국 GP",
        "Miami Grand Prix": "마이애미 GP",
        "Emilia Romagna Grand Prix": "에밀리아 로마냐 GP",
        "Monaco Grand Prix": "모나코 GP",
        "Canadian Grand Prix": "캐나다 GP",
        "Spanish Grand Prix": "스페인 GP",
        "Austrian Grand Prix": "오스트리아 GP",
        "British Grand Prix": "영국 GP",
        "Hungarian Grand Prix": "헝가리 GP",
        "Belgian Grand Prix": "벨기에 GP",
        "Dutch Grand Prix": "네덜란드 GP",
        "Italian Grand Prix": "이탈리아 GP",
        "Azerbaijan Grand Prix": "아제르바이잔 GP",
        "Singapore Grand Prix": "싱가포르 GP",
        "United States Grand Prix": "미국 GP",
        "Mexico City Grand Prix": "멕시코 GP",
        "São Paulo Grand Prix": "상파울루 GP",
        "Las Vegas Grand Prix": "라스베이거스 GP",
        "Qatar Grand Prix": "카타르 GP",
        "Abu Dhabi Grand Prix": "아부다비 GP",
    }

    kr_name = _GP_KR_MAP.get(gp_name, gp_name)
    return f"{year} {kr_name}".strip()
