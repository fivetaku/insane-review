# insane-review

**GPT-5.5 Pro(웹 전용·API 없음)를 Claude Code 안에서 쓰는 브릿지.**

repomix로 관련 코드만 정밀 패킹 → 구독 ChatGPT Pro 웹에 자동 투입 → 분석/의견 회수. **API 비용 0**, 사용자의 ChatGPT 요금제로 동작.

## 왜?

GPT-5.5 Pro는 ChatGPT 웹(구독)에서만 쓸 수 있고 **공식 API가 없다.** Codex CLI·`omc ask`·agent-council의 API provider로는 부를 수 없다. insane-review는 **로그인된 ChatGPT 웹 세션을 CDP로 자동화**해 Pro를 Claude Code 워크플로우 안으로 끌어온다.

## 두 가지 모드

1. **단독 리뷰어** — "검토/수정/리뷰해줘" → Claude가 의도 파악 → 관련 타겟만 repomix 패킹 → Pro 분석 → 반영.
2. **agent-council 멤버** — Pro를 웹 전용 council 멤버로 등록해 다른 모델들과 토론(`references/council-setup.md`).

## 구성

| 경로 | 역할 |
|---|---|
| `skills/insane-review/SKILL.md` | 오케스트레이션 — 의도→타겟선별→패킹→Pro→반영 절차 |
| `commands/insane-review.md` | `/insane-review` 실행 지시서 |
| `bin/pack_and_ask.py` | repomix 패킹 + ChatGPT 웹 자동화(CDP) + 모델선택 + 리즈닝 컷 + 회수 |
| `references/council-setup.md` | agent-council 멤버 등록법 |

## 빠른 사용

```bash
# 레포 일부 + 질문 (Pro)
python3 bin/pack_and_ask.py --target . --include "src/auth/**" --compress \
  --model pro --force-answer-after 120 --prompt "이 인증 흐름의 취약점은?"

# 질문만 (의견)
python3 bin/pack_and_ask.py --model pro --force-answer-after 90 --prompt "이 설계 어떻게 생각해?"

# 패킹만 (토큰 확인)
python3 bin/pack_and_ask.py --target . --include "src/**" --compress --pack-only
```

## 선행 조건
- Comet/Chrome가 디버그포트(9222)로 실행 + `chatgpt.com` 로그인 + 모델 5.5 Pro
- `pip install playwright pyperclip`, `npx`(repomix)

## 검증된 기능 (라이브 관측)
모델 자동선택(`--model pro`) · 파일첨부 · 프롬프트-only · **`--force-answer-after`(리즈닝 강제 종료, "지금 답변 받기")** · 재시도 · 정밀 타겟팅(`--include`/repomix `--stdin`) · council 모드(응답만 stdout).

> ⚠️ 웹 UI 자동화는 OpenAI ToS상 권장되지 않으며 DOM 변경 시 셀렉터 보수가 필요하다. 개인 구독 사용 전제.
