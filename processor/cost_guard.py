"""
CostGuard — Claude API 비용 추적 및 예산 제어 모듈

지원 모델:
  - Claude Haiku 4.5  : input $0.80/1M tokens, output $4.00/1M tokens
  - Claude Sonnet 4.6 : input $3.00/1M tokens, output $15.00/1M tokens

예산 정책:
  - GP당 상한  : $0.10
  - 월 상한    : $10.00
  - 경고 임계값: $8.00 (월 누적)
  - 초과 시    : BudgetExceededError 발생

로그:
  - 매 호출마다 logs/{gp_name}_cost.jsonl 에 JSONL 형식으로 기록
"""

from __future__ import annotations

import json
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# 상수 정의
# ---------------------------------------------------------------------------

ModelName = Literal["claude-haiku-4-5", "claude-sonnet-4-6"]

# 가격표 (달러/토큰)
_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {
        "input":  0.80 / 1_000_000,   # $0.80  per 1M input  tokens
        "output": 4.00 / 1_000_000,   # $4.00  per 1M output tokens
    },
    "claude-sonnet-4-6": {
        "input":  3.00 / 1_000_000,   # $3.00  per 1M input  tokens
        "output": 15.00 / 1_000_000,  # $15.00 per 1M output tokens
    },
}

# 예산 한도 (달러)
BUDGET_GP_LIMIT:      float = 0.10   # GP당 최대 지출
BUDGET_MONTHLY_LIMIT: float = 10.00  # 월 최대 지출
BUDGET_WARNING_THRESHOLD: float = 8.00  # 경고 발생 임계값 (월 누적)

# 로그 기본 디렉토리 — 프로젝트 루트의 logs/ 폴더
_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


# ---------------------------------------------------------------------------
# 예외 클래스
# ---------------------------------------------------------------------------

class BudgetExceededError(Exception):
    """월간 예산 상한($10)을 초과했을 때 발생하는 예외."""

    def __init__(self, monthly_total: float, limit: float) -> None:
        self.monthly_total = monthly_total
        self.limit = limit
        super().__init__(
            f"월간 예산 상한 초과: ${monthly_total:.4f} / ${limit:.2f} "
            f"— API 호출이 차단되었습니다."
        )


class GPBudgetWarning(UserWarning):
    """GP당 예산 상한 접근 시 발생하는 경고."""


class MonthlyBudgetWarning(UserWarning):
    """월 예산 경고 임계값($8) 도달 시 발생하는 경고."""


# ---------------------------------------------------------------------------
# 비용 계산 유틸리티
# ---------------------------------------------------------------------------

def calculate_cost(
    model: ModelName,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """
    토큰 수로 API 호출 비용을 계산합니다.

    Args:
        model: 사용된 모델 이름
        input_tokens: 입력 토큰 수
        output_tokens: 출력 토큰 수

    Returns:
        호출 비용 (달러)

    Raises:
        ValueError: 지원하지 않는 모델 이름
    """
    if model not in _PRICING:
        raise ValueError(
            f"지원하지 않는 모델: '{model}'. "
            f"지원 모델: {list(_PRICING.keys())}"
        )
    pricing = _PRICING[model]
    return (
        input_tokens  * pricing["input"] +
        output_tokens * pricing["output"]
    )


# ---------------------------------------------------------------------------
# CostGuard 클래스
# ---------------------------------------------------------------------------

class CostGuard:
    """
    Claude API 호출 비용을 추적하고 예산 한도를 강제합니다.

    사용 예시:
        guard = CostGuard(gp_name="bahrain_2026")
        guard.record(
            model="claude-haiku-4-5",
            prompt_stage="first_pass",
            input_tokens=350,
            output_tokens=60,
        )
    """

    def __init__(
        self,
        gp_name: str,
        log_dir: Path | str | None = None,
    ) -> None:
        """
        Args:
            gp_name:  GP 식별자 (예: "bahrain_2026"). 로그 파일명에 사용됩니다.
            log_dir:  로그 파일을 저장할 디렉토리. None이면 프로젝트 루트의 logs/를 사용합니다.
        """
        self.gp_name = gp_name
        self.log_dir = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._log_path: Path = self.log_dir / f"{gp_name}_cost.jsonl"

        # 누적 비용 (메모리 내)
        self._gp_total: float = 0.0
        self._monthly_total: float = 0.0

        # 기존 로그에서 누적값 복원
        self._restore_from_log()

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def record(
        self,
        model: ModelName,
        prompt_stage: str,
        input_tokens: int,
        output_tokens: int,
        quote_id: str | None = None,
        extra: dict | None = None,
    ) -> float:
        """
        API 호출 한 건을 기록하고 비용을 반환합니다.

        예산 초과 여부를 사전 검사한 뒤, 호출 비용을 계산하고
        JSONL 로그에 기록합니다.

        Args:
            model:          사용된 Claude 모델 이름
            prompt_stage:   프롬프트 단계 식별자 (예: "first_pass", "translate")
            input_tokens:   입력 토큰 수
            output_tokens:  출력 토큰 수
            quote_id:       처리 중인 발언 ID (선택)
            extra:          추가 메타데이터 딕셔너리 (선택)

        Returns:
            이번 호출의 비용 (달러)

        Raises:
            BudgetExceededError: 월간 예산 상한($10)을 초과한 상태에서 호출 시
        """
        # 1. 사전 검사 — 이미 상한 초과 상태라면 즉시 차단
        self._check_budget_before_call()

        # 2. 비용 계산
        cost = calculate_cost(model, input_tokens, output_tokens)

        # 3. 누적값 업데이트
        self._gp_total      += cost
        self._monthly_total += cost

        # 4. 로그 기록
        entry = self._build_log_entry(
            model=model,
            prompt_stage=prompt_stage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            gp_total=self._gp_total,
            monthly_total=self._monthly_total,
            quote_id=quote_id,
            extra=extra,
        )
        self._write_log(entry)

        # 5. 사후 경고 발생
        self._emit_warnings_after_record(cost)

        return cost

    @property
    def gp_total(self) -> float:
        """현재 GP의 누적 비용 (달러)."""
        return self._gp_total

    @property
    def monthly_total(self) -> float:
        """이번 달 누적 비용 (달러). 로그 전체를 기준으로 합산됩니다."""
        return self._monthly_total

    def summary(self) -> dict:
        """현재 비용 요약을 딕셔너리로 반환합니다."""
        return {
            "gp_name":       self.gp_name,
            "gp_total":      round(self._gp_total, 6),
            "monthly_total": round(self._monthly_total, 6),
            "gp_limit":      BUDGET_GP_LIMIT,
            "monthly_limit": BUDGET_MONTHLY_LIMIT,
            "gp_remaining":  round(max(0.0, BUDGET_GP_LIMIT - self._gp_total), 6),
            "monthly_remaining": round(
                max(0.0, BUDGET_MONTHLY_LIMIT - self._monthly_total), 6
            ),
            "log_path": str(self._log_path),
        }

    def reset_gp(self) -> None:
        """GP별 누적 비용을 초기화합니다. (새 GP 시작 시 호출)"""
        self._gp_total = 0.0

    # ------------------------------------------------------------------
    # 내부 메서드
    # ------------------------------------------------------------------

    def _check_budget_before_call(self) -> None:
        """
        호출 전 예산 상태를 검사합니다.
        월간 상한 초과 시 BudgetExceededError를 발생시킵니다.
        """
        if self._monthly_total >= BUDGET_MONTHLY_LIMIT:
            raise BudgetExceededError(self._monthly_total, BUDGET_MONTHLY_LIMIT)

    def _emit_warnings_after_record(self, cost: float) -> None:
        """
        기록 후 경고 임계값 도달 여부를 확인하고 경고를 발생시킵니다.
        """
        # GP 예산 경고 (80% 도달 시)
        gp_warning_threshold = BUDGET_GP_LIMIT * 0.80
        if self._gp_total >= gp_warning_threshold:
            warnings.warn(
                f"[CostGuard] GP 예산 경고: GP 누적 ${self._gp_total:.4f} / "
                f"${BUDGET_GP_LIMIT:.2f} "
                f"(한도의 {self._gp_total / BUDGET_GP_LIMIT * 100:.1f}% 사용)",
                GPBudgetWarning,
                stacklevel=3,
            )

        # 월간 경고 임계값 ($8)
        if self._monthly_total >= BUDGET_WARNING_THRESHOLD:
            warnings.warn(
                f"[CostGuard] 월간 예산 경고: 월 누적 ${self._monthly_total:.4f} / "
                f"${BUDGET_MONTHLY_LIMIT:.2f} "
                f"(경고 임계값 ${BUDGET_WARNING_THRESHOLD:.2f} 도달)",
                MonthlyBudgetWarning,
                stacklevel=3,
            )

        # GP 한도 초과 경고 (차단은 하지 않음 — 다음 호출 시 차단)
        if self._gp_total > BUDGET_GP_LIMIT:
            warnings.warn(
                f"[CostGuard] GP 예산 초과: GP 누적 ${self._gp_total:.4f}이 "
                f"GP 한도 ${BUDGET_GP_LIMIT:.2f}를 초과했습니다.",
                GPBudgetWarning,
                stacklevel=3,
            )

    def _build_log_entry(
        self,
        model: str,
        prompt_stage: str,
        input_tokens: int,
        output_tokens: int,
        cost: float,
        gp_total: float,
        monthly_total: float,
        quote_id: str | None,
        extra: dict | None,
    ) -> dict:
        """JSONL에 기록할 로그 엔트리를 생성합니다."""
        entry: dict = {
            "timestamp":      datetime.now(tz=timezone.utc).isoformat(),
            "gp_name":        self.gp_name,
            "prompt_stage":   prompt_stage,
            "model":          model,
            "input_tokens":   input_tokens,
            "output_tokens":  output_tokens,
            "total_tokens":   input_tokens + output_tokens,
            "cost_usd":       round(cost, 8),
            "gp_total_usd":   round(gp_total, 8),
            "monthly_total_usd": round(monthly_total, 8),
        }
        if quote_id is not None:
            entry["quote_id"] = quote_id
        if extra:
            entry["extra"] = extra
        return entry

    def _write_log(self, entry: dict) -> None:
        """로그 엔트리를 JSONL 파일에 추가합니다."""
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _restore_from_log(self) -> None:
        """
        기존 JSONL 로그 파일에서 누적 비용을 복원합니다.

        - GP 누적: 현재 GP의 로그 파일에서 gp_name과 일치하는 항목의 합계
        - 월 누적: logs/ 디렉토리 전체의 *_cost.jsonl 파일을 순회하여
                   이번 달(UTC 기준) 모든 항목을 합산 (다중 GP 지원)
        """
        current_month = datetime.now(tz=timezone.utc).strftime("%Y-%m")
        gp_total = 0.0
        monthly_total = 0.0

        # GP 누적: 현재 GP 로그 파일만 참조
        if self._log_path.exists():
            with self._log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if entry.get("gp_name") == self.gp_name:
                        gp_total += entry.get("cost_usd", 0.0)

        # 월간 누적: logs/ 디렉토리 전체의 *_cost.jsonl 파일 순회
        for log_file in self.log_dir.glob("*_cost.jsonl"):
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
                    if ts.startswith(current_month):
                        monthly_total += entry.get("cost_usd", 0.0)

        self._gp_total      = gp_total
        self._monthly_total = monthly_total


# ---------------------------------------------------------------------------
# 편의 함수
# ---------------------------------------------------------------------------

def estimate_cost(
    model: ModelName,
    input_tokens: int,
    output_tokens: int,
) -> dict:
    """
    실제 기록 없이 예상 비용을 계산합니다.

    Returns:
        {"model": ..., "input_tokens": ..., "output_tokens": ..., "estimated_cost_usd": ...}
    """
    cost = calculate_cost(model, input_tokens, output_tokens)
    return {
        "model":               model,
        "input_tokens":        input_tokens,
        "output_tokens":       output_tokens,
        "estimated_cost_usd":  round(cost, 8),
    }


def load_gp_log(gp_name: str, log_dir: Path | str | None = None) -> list[dict]:
    """
    특정 GP의 비용 로그를 전체 로드합니다.

    Args:
        gp_name:  GP 식별자
        log_dir:  로그 디렉토리 경로 (None이면 기본값 사용)

    Returns:
        로그 엔트리 리스트
    """
    log_dir = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
    log_path = log_dir / f"{gp_name}_cost.jsonl"

    if not log_path.exists():
        return []

    entries = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def monthly_cost_report(
    log_dir: Path | str | None = None,
    year_month: str | None = None,
) -> dict:
    """
    지정 월의 전체 비용 리포트를 생성합니다.

    Args:
        log_dir:     로그 디렉토리 경로 (None이면 기본값 사용)
        year_month:  "YYYY-MM" 형식. None이면 현재 달 사용.

    Returns:
        {
            "year_month":    "2026-03",
            "total_usd":     0.0423,
            "by_gp":         {"bahrain_2026": 0.0210, ...},
            "by_model":      {"claude-haiku-4-5": 0.0300, ...},
            "by_stage":      {"first_pass": 0.0100, ...},
            "entry_count":   42,
        }
    """
    log_dir = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
    target_month = year_month or datetime.now(tz=timezone.utc).strftime("%Y-%m")

    total = 0.0
    by_gp: dict[str, float]    = {}
    by_model: dict[str, float] = {}
    by_stage: dict[str, float] = {}
    count = 0

    if not log_dir.exists():
        return {
            "year_month": target_month,
            "total_usd": 0.0,
            "by_gp": {},
            "by_model": {},
            "by_stage": {},
            "entry_count": 0,
        }

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
                if not ts.startswith(target_month):
                    continue

                cost  = entry.get("cost_usd", 0.0)
                gp    = entry.get("gp_name", "unknown")
                model = entry.get("model", "unknown")
                stage = entry.get("prompt_stage", "unknown")

                total += cost
                by_gp[gp]      = by_gp.get(gp, 0.0)      + cost
                by_model[model] = by_model.get(model, 0.0) + cost
                by_stage[stage] = by_stage.get(stage, 0.0) + cost
                count += 1

    return {
        "year_month":  target_month,
        "total_usd":   round(total, 6),
        "by_gp":       {k: round(v, 6) for k, v in sorted(by_gp.items())},
        "by_model":    {k: round(v, 6) for k, v in sorted(by_model.items())},
        "by_stage":    {k: round(v, 6) for k, v in sorted(by_stage.items())},
        "entry_count": count,
    }
