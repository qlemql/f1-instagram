"""F1 Instagram 캐러셀 자동화 — 인터뷰 번역 파이프라인

흐름:
  1. 드라이버 발언 추출 (질문 포함 — Q&A 재구성에 사용)
  2. 1차 선별 (Haiku): 드라이버별 답변 점수 채점 (배치, 5개씩)
  3. 2차 선별 (Haiku): 드라이버별 핵심 답변 3~5개 선정 (드라이버당 1회 호출)
  4. 번역 (Haiku): 선정된 Q&A 전문 번역
  5. 커버 요약 (Haiku): 번역된 내용 기반 한 줄 헤드라인 생성
  6. 슬라이드 분할: 번역 텍스트를 ~180자씩 슬라이드로 코드 분할
  7. 중간 결과 저장: data/drafts/{gp}_{driver}.json
  8. CarouselBatch 반환

설계 원칙:
  - 1게시물 = 1드라이버 (드라이버별 독립 CarouselSet)
  - 번역은 전문 번역 (압축 금지)
  - 슬라이드 분할은 AI가 아닌 코드로 처리 (비용 절약)
  - 18장(커버1 + 본문17 + 출처1) 초과 시 경고 로그
  - Haiku 우선, CostGuard 연동
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from pathlib import Path
from typing import Optional

from anthropic import Anthropic

from config import HAIKU_MODEL, CONTENT
from models import (
    CarouselBatch,
    CarouselSet,
    DRIVER_FULLNAME_KR,
    DRIVER_TEAM_MAP,
    InterviewSlide,
    MAX_BODY_SLIDES,
    PressConference,
    ScoredStatement,
    SelectedQuote,
    get_team_for_driver,
    get_team_for_speaker,
)
from processor.cost_guard import CostGuard

logger = logging.getLogger(__name__)

# 프롬프트 디렉토리
_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "v1"

# 드래프트 저장 디렉토리
_DRAFT_DIR = Path(__file__).resolve().parent.parent / "data" / "drafts"

# CostGuard에서 사용하는 모델 식별자 (버전 없는 short name)
_HAIKU_GUARD = "claude-haiku-4-5"

# 1차 선별 배치 크기
_FIRST_PASS_BATCH_SIZE = 5

# 슬라이드 분할: 문단 단위 우선, 최대 글자 수
_SLIDE_TARGET_CHARS = 180
_SLIDE_MAX_CHARS = 210

# Q/A 분리 슬라이드: 답변 분할 글자 수
# migrate_drafts.py와 동일한 기준 사용 (200/280/80)
_ANSWER_TARGET_CHARS = 200
_ANSWER_MAX_CHARS = 280
_ANSWER_MIN_CHARS = 80   # 이보다 짧으면 이전 슬라이드에 합침
_MAX_TOTAL_SLIDES = 20   # 커버(1) + Q/A 본문 + 출처(1) 합계 최대


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
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

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
    실패 시 None을 반환한다.
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
# 약칭 → 풀네임 변환
# ──────────────────────────────────────────────

def _build_abbr_map(participants: list[str]) -> dict[str, str]:
    """
    participants 풀네임 목록에서 약칭→풀네임 매핑을 생성한다.

    FIA 기자회견에서 동일 화자가 처음엔 "이름 SURNAME:" 형태로 등장하고
    이후 "이니셜(예: KA, OP, CL):" 약칭으로 전환되는 패턴을 처리한다.

    매핑 전략:
      1. 풀네임의 이름(first name) 첫 글자 + 성(last name) 첫 글자 → 2글자 이니셜
         예: "Kimi ANTONELLI" → "KA"
      2. 성(last name) 첫 2글자 → 2글자 약칭
         예: "Charles LECLERC" → "CL"
      3. 이름(first name) 첫 글자 + 성(last name) 첫 2글자 → 3글자
         예: "Oscar PIASTRI" → "OPI" (충돌 방지용)

    충돌 시 먼저 등록된 항목을 우선한다.
    """
    abbr_map: dict[str, str] = {}

    for fullname in participants:
        parts = fullname.split()
        if len(parts) < 2:
            continue

        first = parts[0]
        last = parts[-1]

        candidates = [
            (first[0] + last[0]).upper(),
            (first[0] + last[:2]).upper(),
            last[:2].upper(),
            last[:3].upper(),
        ]

        for abbr in candidates:
            if abbr not in abbr_map:
                abbr_map[abbr] = fullname

    return abbr_map


def _resolve_speaker(speaker: str, abbr_map: dict[str, str]) -> str:
    """약칭이면 풀네임으로 변환한다."""
    if re.match(r"^[A-Z]{1,4}$", speaker):
        resolved = abbr_map.get(speaker)
        if resolved:
            logger.debug("[speaker] 약칭 변환: %s → %s", speaker, resolved)
            return resolved
        else:
            logger.warning("[speaker] 약칭 '%s' 매핑 실패 (participants에 없음)", speaker)
    return speaker


def _normalize_conference_speakers(conference: PressConference) -> PressConference:
    """PressConference의 모든 Statement에서 약칭 speaker를 풀네임으로 교체한다."""
    abbr_map = _build_abbr_map(conference.participants)
    if not abbr_map:
        return conference

    logger.debug("[speaker] 약칭 매핑 테이블: %s", abbr_map)

    for stmt in conference.statements:
        if not stmt.is_question:
            original = stmt.speaker
            stmt.speaker = _resolve_speaker(original, abbr_map)

    return conference


# ──────────────────────────────────────────────
# 단계 1: 드라이버별 Q&A 재구성
# ──────────────────────────────────────────────

def _group_qa_by_driver(
    conference: PressConference,
) -> tuple[dict[str, list[dict]], list[dict]]:
    """
    statements를 드라이버별 개별 Q&A + 공동 Q&A로 분리한다.

    공동 질문 판별: 하나의 질문 뒤에 2명 이상의 서로 다른 드라이버가 답변하면 공동 질문.

    반환:
        (
            {speaker: [{"seq": int, "q": str, "a": str}, ...]},  # 개별 Q&A
            [{"q": str, "answers": [{"speaker": str, "a": str, "seq": int}, ...]}],  # 공동 Q&A
        )
    """
    stmts = conference.statements

    # 1단계: 질문별 답변 그룹 만들기
    q_groups: list[dict] = []  # [{"q_idx": int, "q": str, "answers": [...]}]

    for i, stmt in enumerate(stmts):
        if not stmt.is_question:
            continue
        # 이 질문 뒤의 연속 답변 수집
        answers = []
        for j in range(i + 1, len(stmts)):
            if stmts[j].is_question:
                break
            answers.append({
                "speaker": stmts[j].speaker,
                "a": stmts[j].text,
                "seq": stmts[j].seq,
            })
        if answers:
            q_groups.append({
                "q_idx": i,
                "q": stmt.text,
                "answers": answers,
            })

    # 2단계: 공동 질문 vs 개별 질문 분류
    shared_qa: list[dict] = []
    individual_qa: dict[str, list[dict]] = {}

    for group in q_groups:
        unique_speakers = list(dict.fromkeys(a["speaker"] for a in group["answers"]))
        if len(unique_speakers) >= 2:
            # 공동 질문
            shared_qa.append({
                "q": group["q"],
                "answers": group["answers"],
            })
        else:
            # 개별 질문 — 각 답변을 해당 드라이버에 할당
            for ans in group["answers"]:
                speaker = ans["speaker"]
                if speaker not in individual_qa:
                    individual_qa[speaker] = []
                individual_qa[speaker].append({
                    "seq": ans["seq"],
                    "q": group["q"],
                    "a": ans["a"],
                })

    return individual_qa, shared_qa


# ──────────────────────────────────────────────
# 단계 2: 1차 선별 — 발언 점수 채점 (배치)
# ──────────────────────────────────────────────

def _first_pass_driver(
    client: Anthropic,
    guard: CostGuard,
    driver: str,
    qa_list: list[dict],
    event_label: str,
    system_prompt: str,
) -> list[ScoredStatement]:
    """
    드라이버 1명의 답변 목록을 배치로 채점한다.
    """
    scored: list[ScoredStatement] = []

    for batch_start in range(0, len(qa_list), _FIRST_PASS_BATCH_SIZE):
        batch = qa_list[batch_start: batch_start + _FIRST_PASS_BATCH_SIZE]

        items = []
        for qa in batch:
            items.append({
                "id": f"q{qa['seq']}",
                "speaker": driver,
                "event": event_label,
                "question": qa["q"],
                "answer": qa["a"],
            })

        user_msg = (
            "Score each of the following F1 press conference answers individually.\n"
            "Return a JSON array where each element has: "
            "{\"id\": \"<id>\", \"score\": <0-10>, \"reason\": \"<Korean one-line>\"}.\n"
            "Focus only on the answer (not the question) when scoring.\n"
            "Respond ONLY with the JSON array.\n\n"
            f"Answers:\n{json.dumps(items, ensure_ascii=False, indent=2)}"
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
            quote_id=f"{driver}_batch_{batch_start}",
        )

        if raw is None:
            logger.warning("[first_pass] %s 배치 %d 스킵 (API 오류)", driver, batch_start)
            continue

        try:
            json_str = _extract_json(raw)
            results = json.loads(json_str)

            if isinstance(results, dict):
                results = [results]

            id_to_qa = {f"q{qa['seq']}": qa for qa in batch}

            for item in results:
                qid = item.get("id", "")
                qa = id_to_qa.get(qid)
                if qa is None:
                    continue
                scored.append(ScoredStatement(
                    speaker=driver,
                    text=qa["a"],
                    score=int(item.get("score", 0)),
                    reason=item.get("reason", ""),
                    seq=qa["seq"],
                ))

        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("[first_pass] JSON 파싱 실패 (%s 배치 %d): %s | raw=%s",
                           driver, batch_start, exc, raw[:200])

    return scored


# ──────────────────────────────────────────────
# 단계 3: 2차 선별 — 드라이버별 핵심 답변 3~5개
# ──────────────────────────────────────────────

def _second_pass_driver(
    client: Anthropic,
    guard: CostGuard,
    driver: str,
    scored: list[ScoredStatement],
    qa_list: list[dict],
    event_label: str,
    system_prompt: str,
) -> list[dict]:
    """
    드라이버 1명의 채점된 발언 목록에서 핵심 Q&A 3~5개를 선정한다.
    반환: [{"seq": int, "q": str, "a": str}, ...]  — 선정된 Q&A 쌍
    """
    # score 기준 내림차순 정렬
    sorted_scored = sorted(scored, key=lambda s: s.score, reverse=True)

    items = []
    seq_to_qa = {qa["seq"]: qa for qa in qa_list}
    for s in sorted_scored:
        qa = seq_to_qa.get(s.seq, {})
        items.append({
            "id": f"q{s.seq}",
            "speaker": driver,
            "event": event_label,
            "question": qa.get("q", ""),
            "answer": s.text,
            "score": s.score,
            "reason": s.reason,
        })

    user_msg = (
        "Select the best 3 to 5 answers from the following list for an Instagram carousel post "
        "about this driver's press conference.\n"
        "Prefer high-scoring answers with substantive content. "
        "Avoid selecting duplicate topics.\n"
        "Return a JSON object: "
        "{\"selected\": [\"<id1>\", \"<id2>\", ...], \"rationale\": \"<Korean, 60자 이내>\"}.\n"
        "If fewer than 3 suitable answers exist, select all available.\n"
        "Respond ONLY with the JSON object.\n\n"
        f"Answers:\n{json.dumps(items, ensure_ascii=False, indent=2)}"
    )

    raw = _call_api(
        client=client,
        guard=guard,
        model=HAIKU_MODEL,
        guard_model=_HAIKU_GUARD,
        prompt_stage="second_pass",
        system_prompt=system_prompt,
        user_message=user_msg,
        max_tokens=256,
        quote_id=driver,
    )

    if raw is None:
        logger.warning("[second_pass] %s API 오류, 상위 3개 자동 선정", driver)
        top3 = sorted_scored[:3]
        fallback = [seq_to_qa[s.seq] for s in top3 if s.seq in seq_to_qa]
        fallback.sort(key=lambda qa: qa["seq"])
        return fallback

    try:
        json_str = _extract_json(raw)
        data = json.loads(json_str)
        selected_ids = data.get("selected", [])
        rationale = data.get("rationale", "")
        logger.info("[second_pass] %s: %d개 선정 — %s", driver, len(selected_ids), rationale)

        selected_qa = []
        id_to_qa = {f"q{qa['seq']}": qa for qa in qa_list}
        for sid in selected_ids:
            qa = id_to_qa.get(sid)
            if qa:
                selected_qa.append(qa)
        # 원래 발언 순서(seq)대로 정렬하여 Q→A 순서가 유지되도록 보장
        selected_qa.sort(key=lambda qa: qa["seq"])
        return selected_qa

    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("[second_pass] JSON 파싱 실패 (%s): %s | raw=%s", driver, exc, raw[:200])
        top3 = sorted_scored[:3]
        fallback = [seq_to_qa[s.seq] for s in top3 if s.seq in seq_to_qa]
        fallback.sort(key=lambda qa: qa["seq"])
        return fallback


# ──────────────────────────────────────────────
# 단계 3.5: 번역 후처리 — 번역투 제거 안전망
# ──────────────────────────────────────────────

_POSTPROCESS_RULES: list[tuple[str, str]] = [
    # 숫자 표현 정규화
    (r"(\d+)십분의 (\d+)초", lambda m: f"0.{m.group(2)}초"),
    (r"(\d+)십분의 (\d+)", lambda m: f"0.{m.group(2)}초"),
    # 용어 치환
    (r"레귤레이션", "규정"),
    (r"이 규정에서는", "올해 규정에서는"),
    # 경어 정규화
    (r"했습니다", "했어요"),
    (r"됩니다", "돼요"),
    (r"입니다", "이에요"),
    (r"합니다", "해요"),
    (r"됐습니다", "됐어요"),
    (r"겠습니다", "겠어요"),
    (r"있습니다", "있어요"),
    (r"없습니다", "없어요"),
    (r"봅니다", "봐요"),
    (r"갑니다", "가요"),
    # 번역투 접속사
    (r"또한,?\s*", "그리고 "),
    (r"그러나,?\s*", "근데 "),
    (r"따라서,?\s*", "그래서 "),
]


def _postprocess_translation(text: str) -> str:
    """번역 결과에서 반복적인 번역투 패턴을 코드로 치환한다."""
    for pattern, replacement in _POSTPROCESS_RULES:
        if callable(replacement):
            text = re.sub(pattern, replacement, text)
        else:
            text = re.sub(pattern, replacement, text)
    return text


# ──────────────────────────────────────────────
# 단계 4: 번역 — 선정된 Q&A 전문 번역
# ──────────────────────────────────────────────

def _translate_driver_qa(
    client: Anthropic,
    guard: CostGuard,
    driver: str,
    selected_qa: list[dict],
    event_label: str,
    system_prompt: str,
    speaker_ko: str = "",
) -> list[dict]:
    """
    선정된 Q&A 전체를 한 번에 번역한다.
    반환: [{"q_ko": str, "a_ko": str, "q_en": str, "a_en": str}, ...]
    """
    payload = {
        "speaker": driver,
        "speaker_ko": speaker_ko,
        "event": event_label,
        "qa_pairs": [{"q": qa["q"], "a": qa["a"]} for qa in selected_qa],
    }

    user_msg = json.dumps(payload, ensure_ascii=False, indent=2)

    raw = _call_api(
        client=client,
        guard=guard,
        model=HAIKU_MODEL,
        guard_model=_HAIKU_GUARD,
        prompt_stage="interview_translate",
        system_prompt=system_prompt,
        user_message=user_msg,
        max_tokens=3000,
        quote_id=driver,
    )

    if raw is None:
        logger.warning("[translate] %s 번역 실패", driver)
        return []

    try:
        json_str = _extract_json(raw)
        data = json.loads(json_str)
        translated_pairs = data.get("translated_qa", [])

        result = []
        for i, pair in enumerate(translated_pairs):
            orig = selected_qa[i] if i < len(selected_qa) else {}
            result.append({
                "q_ko": _postprocess_translation(pair.get("q_ko", "")),
                "a_ko": _postprocess_translation(pair.get("a_ko", "")),
                "q_en": orig.get("q", ""),
                "a_en": orig.get("a", ""),
            })
        return result

    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("[translate] JSON 파싱 실패 (%s): %s | raw=%s", driver, exc, raw[:200])
        return []


# ──────────────────────────────────────────────
# 단계 5: 커버 헤드라인 생성
# ──────────────────────────────────────────────

def _generate_cover_headline(
    client: Anthropic,
    guard: CostGuard,
    driver: str,
    speaker_ko: str,
    event_label: str,
    translated_qa: list[dict],
    system_prompt: str,
) -> str:
    """
    번역된 Q&A를 기반으로 커버 카드용 한 줄 헤드라인을 생성한다.
    반환: 헤드라인 문자열 (15~25자)
    """
    payload = {
        "speaker_ko": speaker_ko,
        "event": event_label,
        "translated_qa": [
            {"q_ko": qa["q_ko"], "a_ko": qa["a_ko"]}
            for qa in translated_qa
        ],
    }

    user_msg = json.dumps(payload, ensure_ascii=False, indent=2)

    raw = _call_api(
        client=client,
        guard=guard,
        model=HAIKU_MODEL,
        guard_model=_HAIKU_GUARD,
        prompt_stage="cover_summary",
        system_prompt=system_prompt,
        user_message=user_msg,
        max_tokens=256,
        quote_id=driver,
    )

    if raw is None:
        logger.warning("[cover_summary] %s 헤드라인 생성 실패", driver)
        return f"{speaker_ko} 인터뷰 전문"

    try:
        json_str = _extract_json(raw)
        data = json.loads(json_str)
        headline = data.get("headline", "")
        char_count = data.get("char_count", len(headline))
        rationale = data.get("rationale", "")
        logger.info("[cover_summary] %s: \"%s\" (%d자) — %s",
                    driver, headline, char_count, rationale)

        # 실제 공백 제거 기준 글자 수 검증
        actual_char_count = len(headline.replace(" ", ""))
        if actual_char_count < 10 or actual_char_count > 28:
            logger.warning(
                "[cover_summary] 헤드라인 글자 수 범위 초과: '%s' (%d자)",
                headline, actual_char_count,
            )

        return headline

    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("[cover_summary] JSON 파싱 실패 (%s): %s | raw=%s", driver, exc, raw[:200])
        return f"{speaker_ko} 인터뷰 전문"


# ──────────────────────────────────────────────
# 단계 5-B: 핵심 발언 추출 (key_quote)
# ──────────────────────────────────────────────

def _extract_key_quote(
    client: Anthropic,
    guard: CostGuard,
    driver: str,
    speaker_ko: str,
    event_label: str,
    translated_qa: list[dict],
    system_prompt: str,
) -> dict:
    """
    번역된 Q&A에서 뉴스 가치가 가장 높은 핵심 발언 1~2문장을 추출한다.
    반환: {"quote": str, "context": str, "theme": str}
    """
    payload = {
        "speaker_ko": speaker_ko,
        "event": event_label,
        "translated_qa": [
            {"q_ko": qa["q_ko"], "a_ko": qa["a_ko"]}
            for qa in translated_qa
        ],
    }

    user_msg = json.dumps(payload, ensure_ascii=False, indent=2)

    raw = _call_api(
        client=client,
        guard=guard,
        model=HAIKU_MODEL,
        guard_model=_HAIKU_GUARD,
        prompt_stage="key_quote",
        system_prompt=system_prompt,
        user_message=user_msg,
        max_tokens=256,
        quote_id=driver,
    )

    if raw is None:
        logger.warning("[key_quote] %s 핵심 발언 추출 실패", driver)
        return {"quote": "", "context": "", "theme": ""}

    try:
        json_str = _extract_json(raw)
        data = json.loads(json_str)
        quote = data.get("quote", "")
        context = data.get("context", "")
        theme = data.get("theme", "")
        logger.info("[key_quote] %s: \"%s\" [%s] — %s", driver, quote, theme, context)
        return {"quote": quote, "context": context, "theme": theme}

    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("[key_quote] JSON 파싱 실패 (%s): %s | raw=%s", driver, exc, raw[:200])
        return {"quote": "", "context": "", "theme": ""}


# ──────────────────────────────────────────────
# 단계 6: 슬라이드 분할 (코드 처리)
# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
# 단계 6-B: Q/A 분리 슬라이드 분할
# ──────────────────────────────────────────────

def _split_answer(text: str, max_chars: int = _ANSWER_TARGET_CHARS) -> list[str]:
    """
    답변 텍스트를 문장 완결 단위로 분할한다.

    분할 규칙:
    - 문장 끝(마침표, 물음표, 느낌표) 기준으로 끊기
    - 한 장에 80~120자 목표 (최대 150자)
    - 30자 미만 청크는 이전 슬라이드에 합침
    """
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    if not sentences:
        return [text]

    chunks: list[str] = []
    current = ""

    for sent in sentences:
        candidate = (current + " " + sent).strip() if current else sent
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # 단일 문장이 max_chars 초과 시 강제 분할
            if len(sent) > _ANSWER_MAX_CHARS:
                sub_chunks = _hard_split(sent, max_chars)
                chunks.extend(sub_chunks[:-1])
                current = sub_chunks[-1] if sub_chunks else ""
            else:
                current = sent

    if current:
        chunks.append(current)

    # 30자 미만 청크를 이전 청크에 합침
    if len(chunks) > 1:
        merged: list[str] = [chunks[0]]
        for chunk in chunks[1:]:
            if len(chunk) < _ANSWER_MIN_CHARS and merged:
                merged[-1] = merged[-1] + " " + chunk
            else:
                merged.append(chunk)
        chunks = merged

    return chunks if chunks else [text]


def _split_qa_into_slides(translated_qa: list[dict]) -> list[InterviewSlide]:
    """
    번역된 Q&A 목록을 Q슬라이드 + A슬라이드(들)로 분리하여 배열한다.

    구조: [Q슬라이드] → [A슬라이드1] → [A슬라이드2(이어짐)] → [Q슬라이드] → ...

    분할 규칙:
    - Q 슬라이드: 질문 1장 (slide_type="question")
    - A 슬라이드: 답변 80~120자씩, 문장 완결 단위 분할 (slide_type="answer")
    - 전체 슬라이드 수: 커버(1) + Q/A 본문 + 출처(1) <= 20장
    - 초과 시 Q/A 세트를 줄여서 맞춤
    """
    slides: list[InterviewSlide] = []
    slide_num = 2  # 1은 커버 카드

    # 먼저 Q/A 세트별로 필요한 슬라이드 수를 계산
    qa_slide_sets: list[list[InterviewSlide]] = []

    for pair in translated_qa:
        q_ko = pair.get("q_ko", "").strip()
        a_ko = pair.get("a_ko", "").strip()
        q_en = pair.get("q_en", "").strip()
        a_en = pair.get("a_en", "").strip()

        if not a_ko:
            continue

        qa_set: list[InterviewSlide] = []

        # Q 슬라이드 (1장)
        if q_ko:
            qa_set.append(InterviewSlide(
                slide_num=0,  # 나중에 재번호 매김
                text_kr=q_ko,
                text_en=q_en,
                slide_type="question",
            ))

        # A 슬라이드 (1~3장, 답변 길이에 따라)
        answer_chunks = _split_answer(a_ko, max_chars=_ANSWER_TARGET_CHARS)
        answer_chunks_en = _split_answer(a_en, max_chars=_ANSWER_TARGET_CHARS) if a_en else [""] * len(answer_chunks)

        for ci, chunk in enumerate(answer_chunks):
            en_chunk = answer_chunks_en[ci] if ci < len(answer_chunks_en) else ""
            qa_set.append(InterviewSlide(
                slide_num=0,
                text_kr=chunk.strip(),
                text_en=en_chunk.strip(),
                slide_type="answer",
            ))

        qa_slide_sets.append(qa_set)

    # 전체 슬라이드 수 제한: 커버(1) + 본문 + 출처(1) <= _MAX_TOTAL_SLIDES
    max_body_slides = _MAX_TOTAL_SLIDES - 2  # 커버, 출처 제외

    # Q/A 세트를 하나씩 추가하며 제한 확인
    total_body = 0
    for qa_set in qa_slide_sets:
        if total_body + len(qa_set) > max_body_slides:
            # 이 세트를 추가하면 초과 → 여기서 중단
            logger.info("[split_qa] 슬라이드 수 제한으로 Q/A 세트 잘림: "
                        "현재 %d장, 추가 시도 %d장, 최대 %d장",
                        total_body, len(qa_set), max_body_slides)
            break
        slides.extend(qa_set)
        total_body += len(qa_set)

    # 슬라이드 번호 재할당
    for i, slide in enumerate(slides):
        slide.slide_num = slide_num + i

    return slides


# ──────────────────────────────────────────────
# 단계 7: 드래프트 저장
# ──────────────────────────────────────────────

def _save_draft(carousel: CarouselSet, gp_slug: str) -> Path:
    """
    CarouselSet을 data/drafts/{gp_slug}_{driver_slug}.json에 저장한다.
    반환: 저장된 파일 경로
    """
    _DRAFT_DIR.mkdir(parents=True, exist_ok=True)

    # 드라이버 slug: "Max VERSTAPPEN" → "verstappen"
    driver_slug = carousel.speaker.split()[-1].lower()
    filename = f"{gp_slug}_{driver_slug}.json"
    path = _DRAFT_DIR / filename

    carousel.to_json(str(path))
    logger.info("[draft] 저장 완료: %s (%d장)", path.name, carousel.total_slides)
    return path


# ──────────────────────────────────────────────
# 헬퍼: 드라이버 한국어 이름 조회
# ──────────────────────────────────────────────

def _get_driver_ko(speaker: str) -> str:
    """드라이버 한국어 풀네임 반환. models.DRIVER_FULLNAME_KR 단일 소스 사용.
    매칭 실패 시 원본 반환."""
    if speaker in DRIVER_FULLNAME_KR:
        return DRIVER_FULLNAME_KR[speaker]

    speaker_upper = speaker.upper()
    for name, ko in DRIVER_FULLNAME_KR.items():
        surname = name.split()[-1]
        if surname in speaker_upper:
            return ko

    return speaker


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

    # 이미 연도로 시작하는 경우 중복 제거
    if year and kr_name.startswith(year):
        kr_name = kr_name[len(year):].strip()

    return f"{year} {kr_name}".strip()


# ──────────────────────────────────────────────
# 단일 드라이버 처리 파이프라인
# ──────────────────────────────────────────────

def _process_driver(
    client: Anthropic,
    guard: CostGuard,
    driver: str,
    qa_list: list[dict],
    event_label: str,
    conference: PressConference,
    gp_slug: str,
    prompts: dict[str, str],
) -> Optional[CarouselSet]:
    """
    드라이버 1명에 대한 전체 파이프라인을 실행한다.
    반환: CarouselSet 또는 None (처리 실패 시)
    """
    logger.info("[pipeline] 드라이버 처리 시작: %s (%d개 답변)", driver, len(qa_list))

    # 2단계: 1차 선별
    scored = _first_pass_driver(
        client=client,
        guard=guard,
        driver=driver,
        qa_list=qa_list,
        event_label=event_label,
        system_prompt=prompts["first_pass"],
    )
    if not scored:
        logger.warning("[pipeline] %s 1차 선별 실패 또는 발언 없음", driver)
        return None
    logger.info("[pipeline] %s: 1차 선별 %d개 채점", driver, len(scored))

    # 3단계: 2차 선별
    selected_qa = _second_pass_driver(
        client=client,
        guard=guard,
        driver=driver,
        scored=scored,
        qa_list=qa_list,
        event_label=event_label,
        system_prompt=prompts["second_pass"],
    )
    if not selected_qa:
        logger.warning("[pipeline] %s 2차 선별 결과 없음", driver)
        return None
    logger.info("[pipeline] %s: 2차 선별 %d개 선정", driver, len(selected_qa))

    # 드라이버 한국어 이름
    speaker_ko = _get_driver_ko(driver)

    # 4단계: 번역
    translated_qa = _translate_driver_qa(
        client=client,
        guard=guard,
        driver=driver,
        selected_qa=selected_qa,
        event_label=event_label,
        system_prompt=prompts["interview_translate"],
        speaker_ko=speaker_ko,
    )
    if not translated_qa:
        logger.warning("[pipeline] %s 번역 실패", driver)
        return None
    logger.info("[pipeline] %s: 번역 완료 %d쌍", driver, len(translated_qa))

    # 5단계: 커버 헤드라인 생성
    headline = _generate_cover_headline(
        client=client,
        guard=guard,
        driver=driver,
        speaker_ko=speaker_ko,
        event_label=event_label,
        translated_qa=translated_qa,
        system_prompt=prompts["cover_summary"],
    )

    # 5-B단계: 핵심 발언 추출
    key_quote_data = _extract_key_quote(
        client=client,
        guard=guard,
        driver=driver,
        speaker_ko=speaker_ko,
        event_label=event_label,
        translated_qa=translated_qa,
        system_prompt=prompts.get("key_quote", ""),
    )

    # 6단계: 슬라이드 분할 (Q/A 분리)
    slides = _split_qa_into_slides(translated_qa)
    logger.info("[pipeline] %s: 슬라이드 %d장 생성 (Q/A 분리)", driver, len(slides))

    # 슬라이드 수 확인
    total_slides = 1 + len(slides) + 1  # 커버 + 본문 + 출처

    logger.info("[pipeline] %s: 최종 %d장 (커버1 + 본문%d + 출처1)",
                driver, total_slides, len(slides))

    # 팀 / GP 이름
    team = get_team_for_speaker(driver)
    gp_display = _format_gp_display(conference.gp_name, conference.date_iso)

    carousel = CarouselSet(
        speaker=driver,
        speaker_kr=speaker_ko,
        team=team,
        gp_name=gp_display,
        gp_name_en=conference.gp_name,
        conference_type=conference.conference_type,
        date_iso=conference.date_iso,
        cover_headline=headline,
        slides=slides,
        key_quote=key_quote_data.get("quote", ""),
        key_quote_context=key_quote_data.get("context", ""),
        key_quote_theme=key_quote_data.get("theme", ""),
        total_cost_usd=0.0,  # 마지막에 guard.gp_total로 업데이트
    )

    # 7단계: 드래프트 저장
    _save_draft(carousel, gp_slug)

    return carousel


# ──────────────────────────────────────────────
# 메인 파이프라인
# ──────────────────────────────────────────────

def run_pipeline(
    conference: PressConference,
    gp_name: str,
    target_drivers: Optional[list[str]] = None,
) -> CarouselBatch:
    """
    인터뷰 번역 캐러셀 파이프라인 실행.

    Args:
        conference:      수집된 기자회견 데이터
        gp_name:         GP 식별자 (예: "japan_2026") — 비용 로그 / 드래프트 파일명에 사용
        target_drivers:  처리할 드라이버 목록. None이면 전체 참가자 처리.

    Returns:
        CarouselBatch: 드라이버별 CarouselSet 묶음
    """
    client = Anthropic()
    guard = CostGuard(gp_name=gp_name)

    event_label = f"{conference.gp_name} – {conference.conference_type}"

    # 프롬프트 로드
    prompts = {
        "first_pass":         _load_prompt("first_pass"),
        "second_pass":        _load_prompt("second_pass"),
        "interview_translate": _load_prompt("interview_translate"),
        "cover_summary":      _load_prompt("cover_summary"),
        "key_quote":          _load_prompt("key_quote"),
    }

    logger.info("[pipeline] 시작: %s (%s)", conference.gp_name, conference.conference_type)

    # 사전 처리: 약칭 → 풀네임 변환
    conference = _normalize_conference_speakers(conference)
    logger.info("[pipeline] 약칭 정규화 완료 (participants: %d명)", len(conference.participants))

    # 단계 1: 드라이버별 Q&A 재구성 + 공동 질문 분리
    qa_by_driver, shared_qa = _group_qa_by_driver(conference)
    logger.info("[pipeline] 드라이버 %d명 Q&A 재구성 완료 (공동 질문 %d건)",
                len(qa_by_driver), len(shared_qa))

    if not qa_by_driver:
        logger.warning("[pipeline] 드라이버 발언 없음 — 빈 CarouselBatch 반환")
        return CarouselBatch(
            gp_name=conference.gp_name,
            conference_type=conference.conference_type,
            date_iso=conference.date_iso,
            carousels=[],
            total_cost_usd=guard.gp_total,
        )

    # 처리할 발언자 필터링:
    #   - 팀 매핑이 있는 발언자(드라이버 + 팀 스태프)만 처리
    #   - friday 기자회견의 경우 드라이버가 아닌 발언자(팀 대표 등)는 제외
    #     (friday PC는 드라이버 대상이므로 팀 대표 발언은 콘텐츠에 맞지 않음)
    is_friday = conference.conference_type == "friday"

    def _should_process(speaker: str) -> bool:
        if not get_team_for_speaker(speaker):
            return False  # 팀 매핑 실패 → 완전 미인식 발언자
        if is_friday and speaker not in DRIVER_TEAM_MAP:
            # friday PC에서 드라이버가 아닌 발언자(팀 대표 등) 제외
            # 성 기반 퍼지 매칭도 체크
            speaker_upper = speaker.upper()
            is_driver = any(
                name.split()[-1].upper() in speaker_upper
                for name in DRIVER_TEAM_MAP
            )
            if not is_driver:
                return False
        return True

    drivers_to_process = [d for d in qa_by_driver.keys() if _should_process(d)]
    skipped = set(qa_by_driver.keys()) - set(drivers_to_process)
    if skipped:
        logger.info("[pipeline] 발언자 제외 (팀 미인식 또는 friday 비드라이버): %s", skipped)

    if target_drivers:
        drivers_to_process = [
            d for d in drivers_to_process
            if any(t.upper() in d.upper() or d.upper() in t.upper() for t in target_drivers)
        ]
        logger.info("[pipeline] 드라이버 필터 적용: %s", drivers_to_process)

    carousels: list[CarouselSet] = []

    for driver in drivers_to_process:
        qa_list = qa_by_driver[driver]
        if not qa_list:
            continue

        cost_before = guard.gp_total
        carousel = _process_driver(
            client=client,
            guard=guard,
            driver=driver,
            qa_list=qa_list,
            event_label=event_label,
            conference=conference,
            gp_slug=gp_name,
            prompts=prompts,
        )

        if carousel is not None:
            carousel.total_cost_usd = round(guard.gp_total - cost_before, 6)
            carousels.append(carousel)

    # ── 공동 Q&A 번역 ───────────────────────────────────────────────────────
    translated_shared: list[dict] = []
    if shared_qa:
        logger.info("[pipeline] 공동 질문 %d건 번역 시작", len(shared_qa))
        for sq in shared_qa:
            # 각 답변을 개별 번역 (드라이버명 컨텍스트 유지)
            translated_answers = []
            for ans in sq["answers"]:
                speaker = ans["speaker"]
                speaker_ko = _get_speaker_ko(speaker)
                team = get_team_for_speaker(speaker)
                # 단건 Q&A 번역
                payload = {
                    "speaker": speaker,
                    "speaker_ko": speaker_ko,
                    "event": event_label,
                    "qa_pairs": [{"q": sq["q"], "a": ans["a"]}],
                }
                raw = _call_api(
                    client=client,
                    guard=guard,
                    model=HAIKU_MODEL,
                    guard_model=_HAIKU_GUARD,
                    prompt_stage="interview_translate",
                    system_prompt=prompts["interview_translate"],
                    user_message=json.dumps(payload, ensure_ascii=False, indent=2),
                    max_tokens=1500,
                    quote_id=f"shared_{speaker}",
                )
                if raw:
                    try:
                        data = json.loads(_extract_json(raw))
                        tqa = data.get("translated_qa", [{}])
                        a_ko = _postprocess_translation(tqa[0].get("a_ko", "")) if tqa else ""
                        q_ko = _postprocess_translation(tqa[0].get("q_ko", "")) if tqa else ""
                    except (json.JSONDecodeError, IndexError):
                        a_ko = ""
                        q_ko = ""
                else:
                    a_ko = ""
                    q_ko = ""

                translated_answers.append({
                    "speaker": speaker,
                    "speaker_kr": speaker_ko,
                    "team": team or "",
                    "a_ko": a_ko,
                })

            # 질문 번역은 첫 번째 답변 번역 시 함께 처리됨
            translated_shared.append({
                "q": sq["q"],
                "q_ko": q_ko if q_ko else sq["q"],
                "answers": translated_answers,
            })

        logger.info("[pipeline] 공동 질문 %d건 번역 완료", len(translated_shared))

    total_cost = guard.gp_total
    logger.info(
        "[pipeline] 완료: 드라이버 %d명 처리, 공동 질문 %d건, 총 비용 $%.5f",
        len(carousels), len(translated_shared), total_cost,
    )

    return CarouselBatch(
        gp_name=conference.gp_name,
        conference_type=conference.conference_type,
        date_iso=conference.date_iso,
        carousels=carousels,
        shared_qa=translated_shared,
        total_cost_usd=round(total_cost, 6),
    )


# ──────────────────────────────────────────────
# --from-draft 모드: 드래프트에서 직접 로드
# ──────────────────────────────────────────────

def load_from_draft(draft_path: str) -> CarouselSet:
    """
    data/drafts/{gp}_{driver}.json 파일에서 CarouselSet을 로드한다.
    AI 호출 없이 렌더링 단계로 바로 진입할 때 사용.
    """
    return CarouselSet.from_json(draft_path)


def load_batch_from_drafts(gp_slug: str) -> CarouselBatch:
    """
    data/drafts/{gp_slug}_*.json 파일 전체를 로드하여 CarouselBatch로 반환한다.
    """
    _DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    draft_files = sorted(_DRAFT_DIR.glob(f"{gp_slug}_*.json"))

    if not draft_files:
        logger.warning("[draft] %s 에 해당하는 드래프트 파일 없음", gp_slug)
        return CarouselBatch(
            gp_name=gp_slug,
            conference_type="",
            date_iso="",
            carousels=[],
        )

    carousels = []
    for path in draft_files:
        try:
            carousel = CarouselSet.from_json(str(path))
            carousels.append(carousel)
            logger.info("[draft] 로드: %s (%d장)", path.name, carousel.total_slides)
        except Exception as exc:
            logger.warning("[draft] 로드 실패 (%s): %s", path.name, exc)

    # 첫 번째 캐러셀에서 공통 정보 추출
    first = carousels[0] if carousels else None
    return CarouselBatch(
        gp_name=first.gp_name_en if first else gp_slug,
        conference_type=first.conference_type if first else "",
        date_iso=first.date_iso if first else "",
        carousels=carousels,
    )
