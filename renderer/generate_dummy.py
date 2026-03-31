"""
generate_dummy.py
더미 데이터로 발언 카드 1장을 생성하여 output/dummy_card.png에 저장하는 스크립트.

Usage:
    cd renderer
    python generate_dummy.py

    # 다른 팀 테스트
    python generate_dummy.py --team ferrari
    python generate_dummy.py --team mclaren
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

# 렌더러가 같은 패키지 내에 있으므로 상위 디렉토리를 경로에 추가
sys.path.insert(0, str(Path(__file__).parent))

from card_renderer import CardRenderer


# ── 팀별 더미 데이터 ────────────────────────────────────────────────────────
DUMMY_DATA: dict[str, dict] = {
    "red_bull": {
        "driver_name": "Max Verstappen",
        "main_copy": "올해 차는 정말 좋다",
        "sub_copy": "레드불의 새 머신에 대한 자신감 표현. 바레인 프리시즌 테스트 3일차 인터뷰.",
        "quote": "'This car is absolutely incredible. I'm very happy with how everything feels.'",
        "gp_label": "2026 바레인 GP",
    },
    "ferrari": {
        "driver_name": "Lewis Hamilton",
        "main_copy": "페라리와 함께하는 이 순간이 꿈만 같다",
        "sub_copy": "첫 시즌 시작 전 기자회견에서 페라리 합류에 대한 소감을 밝혔다.",
        "quote": "'Joining Ferrari has always been a dream. This moment feels surreal.'",
        "gp_label": "2026 바레인 GP",
    },
    "mercedes": {
        "driver_name": "George Russell",
        "main_copy": "새 규정은 우리에게 유리하게 작용할 것",
        "sub_copy": "2026 기술 규정 변경에 대한 메르세데스의 자신감.",
        "quote": "'The new regulations should suit our car concept well.'",
        "gp_label": "2026 바레인 GP",
    },
    "mclaren": {
        "driver_name": "Lando Norris",
        "main_copy": "챔피언이 된 지금도 배울 게 많다",
        "sub_copy": "2025 시즌 월드 챔피언에 오른 노리스가 새 시즌 각오를 밝혔다.",
        "quote": "'Winning the championship just makes me hungrier to keep improving.'",
        "gp_label": "2026 바레인 GP",
    },
    "aston_martin": {
        "driver_name": "Fernando Alonso",
        "main_copy": "뉴이가 설계한 차, 기대가 크다",
        "sub_copy": "에이드리언 뉴이가 이끈 첫 번째 머신 AMR26에 대한 알론소의 기대감.",
        "quote": "'Adrian's work is something special. I can feel it in every corner.'",
        "gp_label": "2026 바레인 GP",
    },
    "alpine": {
        "driver_name": "Pierre Gasly",
        "main_copy": "핑크와 블루, 이게 바로 알파인이다",
        "sub_copy": "A526 리버리 공개 행사에서 가슬리가 팀 정체성에 대해 언급했다.",
        "quote": "'The pink and blue combination is truly unique. We stand out.'",
        "gp_label": "2026 바레인 GP",
    },
    "williams": {
        "driver_name": "Carlos Sainz",
        "main_copy": "윌리엄스로 새 출발, 설레는 마음",
        "sub_copy": "페라리를 떠나 윌리엄스로 이적한 사인츠의 새 시즌 소감.",
        "quote": "'Williams is a team with a great history. I'm excited to be here.'",
        "gp_label": "2026 바레인 GP",
    },
    "rb": {
        "driver_name": "Yuki Tsunoda",
        "main_copy": "이제 진짜 내 실력을 보여줄 때다",
        "sub_copy": "5번째 F1 시즌에 도전하는 츠노다의 각오.",
        "quote": "'I feel more confident than ever. This is my moment.'",
        "gp_label": "2026 바레인 GP",
    },
    "haas": {
        "driver_name": "Esteban Ocon",
        "main_copy": "하스는 생각보다 강하다",
        "sub_copy": "알파인을 떠나 하스로 이적한 오콘의 첫 인상.",
        "quote": "'The team has improved a lot. I see real potential here.'",
        "gp_label": "2026 바레인 GP",
    },
    "sauber_audi": {
        "driver_name": "Nico Hulkenberg",
        "main_copy": "아우디의 F1 데뷔, 역사적인 순간",
        "sub_copy": "아우디가 공식 F1 팀으로 첫 시즌을 시작하는 것에 대한 훌켄베르크의 소감.",
        "quote": "'Being part of Audi's F1 debut is something truly historic.'",
        "gp_label": "2026 바레인 GP",
    },
    "cadillac": {
        "driver_name": "Valtteri Bottas",
        "main_copy": "캐딜락으로 F1에 돌아왔다",
        "sub_copy": "11번째 팀으로 데뷔하는 캐딜락에 합류한 보타스의 복귀 소감.",
        "quote": "'It feels amazing to be back on the grid with Cadillac.'",
        "gp_label": "2026 바레인 GP",
    },
}


def generate(team_key: str, output_path: Optional[Path] = None) -> Path:
    """
    지정 팀의 더미 카드를 렌더링하여 저장한다.

    Args:
        team_key:    팀 키 (예: "red_bull")
        output_path: 저장 경로 (None이면 output/dummy_card.png)

    Returns:
        Path: 저장된 파일 경로
    """
    if team_key not in DUMMY_DATA:
        available = ", ".join(DUMMY_DATA.keys())
        raise ValueError(f"알 수 없는 팀 키: '{team_key}'. 사용 가능: {available}")

    data = DUMMY_DATA[team_key]

    if output_path is None:
        repo_root = Path(__file__).parent.parent
        output_path = repo_root / "output" / "dummy_card.png"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    renderer = CardRenderer()
    img = renderer.render(
        team_key=team_key,
        driver_name=data["driver_name"],
        main_copy=data["main_copy"],
        sub_copy=data["sub_copy"],
        quote=data.get("quote", ""),
        gp_label=data.get("gp_label", ""),
    )
    img.save(str(output_path))
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="F1 발언 카드 더미 이미지 생성기"
    )
    parser.add_argument(
        "--team",
        default="red_bull",
        help=f"팀 키 (기본: red_bull). 선택 가능: {', '.join(DUMMY_DATA.keys())}",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="저장 경로 (기본: output/dummy_card.png)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="전체 11개 팀 카드 생성 (output/dummy_{team}.png)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).parent.parent

    if args.all:
        print("전체 팀 카드 생성 시작...")
        for key in DUMMY_DATA:
            out = repo_root / "output" / f"dummy_{key}.png"
            try:
                saved = generate(key, out)
                print(f"  [{key:15s}] 저장 완료 → {saved}")
            except Exception as e:
                print(f"  [{key:15s}] 실패: {e}")
        print("전체 팀 카드 생성 완료.")
    else:
        out = Path(args.output) if args.output else None
        try:
            saved = generate(args.team, out)
            print(f"더미 카드 생성 완료 → {saved}")
        except Exception as e:
            print(f"오류: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
