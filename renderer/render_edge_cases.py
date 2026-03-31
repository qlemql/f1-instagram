"""
render_edge_cases.py
엣지케이스 더미 카드 생성 및 CardSet 배치 렌더링 검증 스크립트.

검증 항목:
  1. 최대 길이 텍스트 (main_copy 25자 + sub_copy 40자)
  2. 영어 인용구 200자 초과
  3. 드라이버 이름이 긴 경우 (Alexander ALBON 등)
  4. 밈 카드 시각적 차별화
  5. render_card_set() 배치 렌더링
"""

from __future__ import annotations

import sys
from pathlib import Path

# 상위 디렉토리(프로젝트 루트) 경로 추가
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from models import CardContent, CardSet
from card_renderer import CardRenderer, render_card_set


def make_edge_case_card_set() -> CardSet:
    """엣지케이스를 포함한 더미 CardSet 생성"""

    cards = [
        # ── 케이스 1: 최대 길이 텍스트 ──────────────────────────────────────
        # main_copy 25자 꽉 채움 + sub_copy 40자 꽉 채움
        CardContent(
            speaker_kr="페르스타펜",
            team="Red Bull",
            main_copy="이번 레이스는 정말 완벽한 결과였어",   # 한글 25자
            sub_copy="레드불 홈 그라운드에서의 완벽한 우승으로 챔피언십 선두를 굳건히 지켰다",  # 한글 40자
            quote_en="Everything came together perfectly today. The car was amazing, "
                     "the strategy was spot on, and I'm really happy.",
            card_type="info",
            gp_name="2026 일본 GP",
            seq=1,
        ),

        # ── 케이스 2: 영어 인용구 200자 초과 ────────────────────────────────
        CardContent(
            speaker_kr="해밀턴",
            team="Ferrari",
            main_copy="페라리와 함께 새 역사를 쓰다",
            sub_copy="새 시즌 첫 레이스에서 포디엄 획득, 페라리 이적 후 기대감 충만",
            quote_en=(
                "I have to say, this is one of the most special moments of my career. "
                "Joining Ferrari was a dream that I have had since I was a kid watching "
                "Michael Schumacher win championships. The team has done an incredible job "
                "over the winter and I believe we can really challenge for the title this year. "
                "The car feels fantastic and the whole team atmosphere is just amazing."
            ),  # 약 330자 — 200자 초과 케이스
            card_type="info",
            gp_name="2026 일본 GP",
            seq=2,
        ),

        # ── 케이스 3: 드라이버 이름이 긴 경우 ───────────────────────────────
        CardContent(
            speaker_kr="알렉산더 알본",
            team="Williams",
            main_copy="윌리엄스의 진격, Q3 진출 달성",
            sub_copy="태국계 영국인 드라이버 알본, 2026 시즌 첫 Q3 진출로 팀 기세 올려",
            quote_en="Getting into Q3 with Williams is a great achievement. "
                     "The team has worked so hard and we deserve this result.",
            card_type="highlight",
            gp_name="2026 일본 GP",
            seq=3,
        ),

        # ── 케이스 4: 밈 카드 시각적 차별화 ─────────────────────────────────
        CardContent(
            speaker_kr="노리스",
            team="McLaren",
            main_copy="포디엄? 당연하죠, 저 맥라렌인데요",
            sub_copy="노리스의 특유의 유머로 팬들 폭소, 레이스 후 인터뷰 명장면",
            quote_en="Podium? Of course, I'm in a McLaren. Did you expect anything less? "
                     "We're just built different this season, honestly.",
            card_type="meme",
            gp_name="2026 일본 GP",
            seq=4,
        ),

        # ── 케이스 5: 밈 카드 + 긴 이름 조합 ───────────────────────────────
        CardContent(
            speaker_kr="가브리엘 보르톨레토",
            team="Sauber/Audi",
            main_copy="아우디와 함께라면 불가능은 없다",
            sub_copy="브라질 신예 보르톨레토, 자우버-아우디 첫 시즌부터 인상적인 퍼포먼스",
            quote_en="Racing for Audi in Formula 1 is a dream come true. "
                     "I cannot wait to show what we can do together.",
            card_type="meme",
            gp_name="2026 일본 GP",
            seq=5,
        ),

        # ── 케이스 6: 최소 콘텐츠 (quote 없음) ──────────────────────────────
        CardContent(
            speaker_kr="러셀",
            team="Mercedes",
            main_copy="메르세데스, 부활의 신호탄",
            sub_copy="2026년 새 레귤레이션에서 메르세데스 경쟁력 회복 기대감",
            quote_en="",   # 인용구 없는 케이스
            card_type="info",
            gp_name="2026 일본 GP",
            seq=6,
        ),

        # ── 케이스 7: 한글+영어 혼용 main_copy ──────────────────────────────
        CardContent(
            speaker_kr="피아스트리",
            team="McLaren",
            main_copy="McLaren P1! 원-투 피니시",
            sub_copy="맥라렌 역사적인 원-투 피니시 달성, 노리스-피아스트리 콤비 빛났다",
            quote_en="One-two for McLaren! This is what we worked for all winter long.",
            card_type="highlight",
            gp_name="2026 일본 GP",
            seq=7,
        ),

        # ── 케이스 8: 아주 짧은 텍스트 (경계값 반대) ────────────────────────
        CardContent(
            speaker_kr="르클레르",
            team="Ferrari",
            main_copy="완벽했다",
            sub_copy="르클레르, 간결하게 승리를 표현",
            quote_en="Perfect.",
            card_type="info",
            gp_name="2026 일본 GP",
            seq=8,
        ),
    ]

    return CardSet(
        gp_name="Japanese Grand Prix",
        conference_type="post-race",
        date_iso="2026-03-29",
        cards=cards,
        total_cost_usd=0.0,
    )


def main():
    print("=" * 60)
    print("엣지케이스 카드 렌더링 검증")
    print("=" * 60)

    card_set = make_edge_case_card_set()

    print(f"\n카드 세트: {card_set.gp_name} / {card_set.conference_type}")
    print(f"총 {len(card_set.cards)}장 렌더링 시작...\n")

    saved = render_card_set(card_set)

    print("\n── 저장된 파일 목록 ──")
    for path in saved:
        print(f"  {path}")

    print(f"\n검증 완료: {len(saved)}장 생성")
    print("\n확인 포인트:")
    print("  card_1.png : 최대 길이 텍스트 (25자 + 40자) — 오버플로우 없는지 확인")
    print("  card_2.png : 영어 인용구 330자 — 말줄임 또는 폰트 축소 확인")
    print("  card_3.png : '알렉산더 알본' 긴 이름 — 이름 렌더링 확인")
    print("  card_4.png : 밈 카드 — 골드 보더, MEME 뱃지, 인용구 박스 골드 확인")
    print("  card_5.png : 밈 카드 + 긴 이름 조합")
    print("  card_6.png : 인용구 없는 카드 — 레이아웃 깨짐 없는지 확인")
    print("  card_7.png : 한글+영어 혼용 main_copy")
    print("  card_8.png : 최소 텍스트 (경계값)")


if __name__ == "__main__":
    main()
