"""
스모크 테스트 — 파이프라인 연결 상태 검증

API 호출 없이 모든 모듈이 정상 import되고,
핵심 함수들이 메인 플로우에서 호출되는지 확인한다.

사용법:
    python3 scripts/smoke_test.py
"""

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PASS = "✅"
FAIL = "❌"
results = []


def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    print(f"  {status} {name}" + (f" — {detail}" if detail else ""))


def test_imports():
    """핵심 모듈 import 확인."""
    print("\n[1] 모듈 import 검증")

    modules = [
        ("config", "config"),
        ("models", "models"),
        ("scraper.fia_scraper", "FIA 스크래퍼"),
        ("scraper.collector", "수집기"),
        ("processor.pipeline", "파이프라인"),
        ("processor.cost_guard", "비용 관리"),
        ("processor.review", "리뷰"),
        ("renderer.design_tokens", "디자인 토큰"),
        ("renderer.carousel_renderer", "캐러셀 렌더러"),
        ("notifier.telegram_bot", "텔레그램 봇"),
    ]

    for mod, label in modules:
        try:
            __import__(mod)
            check(f"{label} ({mod})", True)
        except Exception as e:
            check(f"{label} ({mod})", False, str(e))


def test_exports():
    """renderer 패키지에서 핵심 함수 export 확인."""
    print("\n[2] 렌더러 export 검증")

    from renderer import (
        render_cover,
        render_interview_slide,
        render_source,
        render_carousel,
        render_quote_card,
        save_carousel,
    )
    from renderer.carousel_renderer import (
        render_question_slide,
        render_answer_slide,
    )

    funcs = [
        ("render_cover", render_cover),
        ("render_interview_slide", render_interview_slide),
        ("render_question_slide", render_question_slide),
        ("render_answer_slide", render_answer_slide),
        ("render_source", render_source),
        ("render_carousel", render_carousel),
        ("render_quote_card", render_quote_card),
        ("save_carousel", save_carousel),
    ]
    for name, fn in funcs:
        check(name, callable(fn))


def test_pipeline_wiring():
    """파이프라인 내부에서 핵심 함수들이 실제로 호출되는지 AST로 검증."""
    print("\n[3] 파이프라인 함수 호출 검증 (AST)")

    pipeline_path = PROJECT_ROOT / "processor" / "pipeline.py"
    source = pipeline_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 모든 함수 호출 이름 수집
    call_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                call_names.add(node.func.attr)

    # 모든 정의된 함수 이름 수집
    defined_funcs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            defined_funcs.add(node.name)

    # 핵심 함수가 호출되는지 확인
    must_be_called = [
        ("_first_pass_driver", "1차 선별"),
        ("_second_pass_driver", "2차 선별"),
        ("_translate_driver_qa", "번역"),
        ("_generate_cover_headline", "커버 헤드라인"),
        ("_extract_key_quote", "핵심 발언 추출"),
        ("_split_qa_into_slides", "슬라이드 분할"),
        ("_postprocess_translation", "번역 후처리"),
    ]

    for func_name, label in must_be_called:
        called = func_name in call_names
        defined = func_name in defined_funcs
        if defined and not called:
            check(f"{label} ({func_name})", False, "정의됨, 호출 안 됨 — 데드 코드!")
        elif defined and called:
            check(f"{label} ({func_name})", True)
        else:
            check(f"{label} ({func_name})", False, "정의되지 않음")

    # 데드 코드 탐지 — 정의됐지만 호출 안 되는 내부 함수
    print("\n[4] 데드 코드 탐지")
    internal_funcs = {f for f in defined_funcs if f.startswith("_") and f != "__init__"}
    dead = internal_funcs - call_names
    if dead:
        for fn in sorted(dead):
            check(f"{fn}", False, "정의됨, 호출 안 됨")
    else:
        check("데드 코드 없음", True)


def test_main_wiring():
    """main.py에서 핵심 함수/모듈 호출 확인."""
    print("\n[5] main.py 연결 검증")

    main_path = PROJECT_ROOT / "main.py"
    source = main_path.read_text(encoding="utf-8")

    must_contain = [
        ("run_pipeline", "파이프라인 실행"),
        ("render_carousel", "캐러셀 렌더링"),
        ("save_carousel", "이미지 저장"),
        ("send_carousel_set", "텔레그램 전송"),
    ]

    for keyword, label in must_contain:
        check(f"{label} ({keyword})", keyword in source)


def test_config_consistency():
    """설정값 일관성 검증."""
    print("\n[6] 설정값 일관성 검증")

    from config import CONTENT, BUDGET

    pipeline_path = PROJECT_ROOT / "processor" / "pipeline.py"
    source = pipeline_path.read_text(encoding="utf-8")

    # SLIDE_MAX_CHARS 일관성
    if "_SLIDE_MAX_CHARS" in source:
        # pipeline.py에서 하드코딩된 값 추출
        for line in source.splitlines():
            if "_SLIDE_MAX_CHARS" in line and "=" in line and not line.strip().startswith("#"):
                val = line.split("=")[1].strip().split("#")[0].strip()
                try:
                    pipeline_val = int(val)
                    config_val = CONTENT.get("slide_max_chars", 0)
                    check("SLIDE_MAX_CHARS 일치",
                          pipeline_val == config_val,
                          f"pipeline={pipeline_val}, config={config_val}")
                except ValueError:
                    pass
                break


def test_prompt_files():
    """프롬프트 파일 존재 확인."""
    print("\n[7] 프롬프트 파일 검증")

    prompts = [
        "first_pass.txt",
        "second_pass.txt",
        "interview_translate.txt",
        "cover_summary.txt",
        "key_quote.txt",
    ]

    for p in prompts:
        path = PROJECT_ROOT / "prompts" / "v1" / p
        exists = path.exists() and path.stat().st_size > 0
        check(p, exists, f"{path.stat().st_size:,} bytes" if exists else "파일 없음")


if __name__ == "__main__":
    print("=" * 60)
    print("F1 Instagram 파이프라인 스모크 테스트")
    print("=" * 60)

    test_imports()
    test_exports()
    test_pipeline_wiring()
    test_main_wiring()
    test_config_consistency()
    test_prompt_files()

    # 결과 요약
    total = len(results)
    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = total - passed

    print("\n" + "=" * 60)
    print(f"결과: {passed}/{total} 통과", end="")
    if failed:
        print(f" ({failed}건 실패)")
        sys.exit(1)
    else:
        print(" — 모든 검증 통과!")
        sys.exit(0)
