# F1 Instagram — Press Conference Card-News Automation

> Automated pipeline that turns F1 press conferences into Korean Instagram carousels, running on a strict $10/month Claude API budget.

🇰🇷 Target: Korean F1 fans  
💰 Budget: $10/month total Claude API spend (hard cap)  
⚙️ Runtime: GitHub Actions (free tier)


---

## What it does

For every F1 Grand Prix weekend, the pipeline:

1. **Scrapes** the FIA press conference transcript (`formula1.com` as fallback)
2. **Scores and selects** quotable Q&As per driver using Claude Haiku
3. **Translates** the selected Q&As into Korean (full translation, not summarization)
4. **Generates** a one-line headline cover for each driver
5. **Splits** the translation into ~180-character slides via code (no AI here, on purpose)
6. **Renders** the slides as Instagram carousel cards (1080×1350) with team-color theming
7. **Delivers** the result to my Telegram for review → I upload to Instagram manually

End result: one carousel post per driver, with team colors, Korean translation, and a quotable headline.

## The cost-engineering problem

A naive implementation calls Claude Sonnet on every press conference quote. With ~30 quotes per session × 23 GPs/year, that easily exceeds $10/month, and most of the spend is wasted on uninteresting quotes.

The actual pipeline is **Haiku-only**, with the expensive operations replaced by deterministic code:

| Stage | What does it | Why this choice |
|-------|--------------|-----------------|
| Quote scoring (1st pass) | Haiku, batched 5 quotes/call | Batching cuts overhead by ~80% |
| Top-quote selection (2nd pass) | Haiku, 1 call per driver | One driver = one post, scoped per-driver |
| Q&A translation | Haiku, full translation | Sonnet quality unnecessary for translation |
| Cover headline | Haiku, single-line generation | One short call per post |
| Slide splitting | **Pure Python, no AI** | Deterministic, free, works perfectly |
| Image rendering | Pillow, no headless browser | Faster and free |

Splitting slides with code instead of AI was the single biggest cost win — turning what would have been ~17 LLM calls per post into a `textwrap.wrap()` call.

## CostGuard

A dedicated `CostGuard` module enforces the budget at the SDK boundary. No Claude call goes through without a cost check.

- Per-call cost calculated from token counts × model pricing table
- Per-GP soft limit: `$0.10` (warning, not blocking)
- Monthly hard limit: `$10.00` — raises `BudgetExceededError`, blocks all subsequent calls
- Warning threshold: `$8.00` accumulated, raises `MonthlyBudgetWarning`
- Every call logged as JSONL to `logs/{gp_name}_cost.jsonl` for post-hoc analysis

This pattern came from the frustration of side projects silently bleeding API budget while you sleep. Hard caps are non-negotiable for unattended automation.

## Card rendering

Pure Pillow, no headless browser. Each card uses:

- **Team colors** from `team_colors.json` (all 11 teams in the 2026 grid)
- **Typography**: Bebas Neue for impact, Pretendard for Korean body text
- **Carousel format**: 1080×1350, max 18 slides per post (1 cover + 17 body + 1 source)
- **Design tokens** in `renderer/design_tokens.py` for consistent spacing, color, and typography across cards

## Why GitHub Actions

The pipeline is event-light — it runs ~23 times a year for race weekends, plus pre-season testing. Spinning up a server would be wasteful. GitHub Actions gives me:

- Free compute within open-source limits
- Native cron scheduling per GP weekend
- Free secrets management for API keys and Telegram bot tokens
- Clean separation between code repo and runtime — push to deploy

## Stack

`Python 3.12` · `Anthropic Claude Haiku 4.5` · `Pillow` · `BeautifulSoup4` · `httpx` · `python-telegram-bot` · `GitHub Actions`

## Project structure

```
scraper/        FIA + formula1.com extractors with URL discovery
processor/      Multi-stage selection pipeline + CostGuard
renderer/       Pillow card-news generator + design tokens + team colors
notifier/       Telegram bot for review delivery
prompts/v1/     Versioned Claude prompts (selection, translation, headline)
config.py       Models, content rules, budget constants
main.py         End-to-end orchestrator
```

## Design principles

- **One post per driver** — independent carousel set, parallelizable
- **Translation is full**, not compressed — quote integrity matters
- **Slide splitting is code, not AI** — deterministic and free
- **CostGuard is mandatory** — every API call goes through it
- **Prompts are versioned** under `prompts/v1/` so iterations don't break old runs

---

Built with Claude Code as the primary dev environment.  
Author: [Hyun (qlemql)](https://github.com/qlemql)
