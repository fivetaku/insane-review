---
description: GPT Pro(웹 전용, 최신 플래그십)에게 repomix로 패킹한 코드/질문을 보내 의견을 받아온다
---

# /insane-review

사용자의 요청(`$ARGUMENTS`)을 구독 ChatGPT 웹에 보내 분석/의견을 받아 반영한다. **추론강도 미지정 시 기본은 Pro다. 사용자가 다른 추론강도를 명시하면 그 요청이 우선이며 Pro로 덮어쓰지 않는다.**

> (AskUserQuestion은 frontmatter `allowed-tools`에 **절대 넣지 않는다** — 넣으면 자동승인돼 UI가 안 뜬다.)

## Step 0 — 첫 실행 셋업 (1회, 자동)

가장 먼저 실행한다 — 부트스트랩: 업데이트 알림 훅 설치 + Python 의존성(pyperclip·playwright) 자동 설치. (repomix는 `npx -y`로 실행되어 사전설치 불필요.)

```bash
bash "${CLAUDE_PLUGIN_ROOT}/setup/setup.sh" ask autoupdate
```

출력이 `STAR_ASK`로 시작하면 즉시 **AskUserQuestion**을 1회 호출한다 — 질문·선택지는 **사용자의 현재 대화 언어**로 작성한다(대화 언어가 분명하면 그것을, 아니면 `STAR_ASK` 뒤 언어코드 `ko/ja/en`을 사용; 무조건 한국어 기본값 금지).
- header: 짧은 현지화된 "GitHub Star" 라벨
- question: 이 플러그인(과 gptaku-plugins 마켓플레이스)에 GitHub ⭐로 응원할지 — 선택 안 해도 모든 기능은 그대로 작동
- options: 정확히 2개 — (1) 응원/스타 → `bash "${CLAUDE_PLUGIN_ROOT}/setup/setup.sh" star yes`; (2) 괜찮아요 → `bash "${CLAUDE_PLUGIN_ROOT}/setup/setup.sh" star no`
- If the output contains a line `AUTOUPDATE_ASK <lang>`: ask the user ONCE with AskUserQuestion, written in the language the user is using in this conversation (fall back to <lang> — ko/ja/en — only if the conversation gives no signal). Question: gptaku-plugins can update itself automatically so you always get fixes without running /plugin manually. Turn on auto-update? Options (2): "Turn on auto-update" / "Not now".
  - "Turn on auto-update" → run `bash "${CLAUDE_PLUGIN_ROOT}/setup/setup.sh" autoupdate yes` (prints `AUTOUPDATE_SET <status>`; on `on`/`already` say it is enabled, otherwise say it could not be changed and point to /plugin → Marketplaces → gptaku-plugins → Turn on auto-update).
  - "Not now" → run `bash "${CLAUDE_PLUGIN_ROOT}/setup/setup.sh" autoupdate no`.
  If both STAR_ASK and AUTOUPDATE_ASK appear, put both questions in ONE AskUserQuestion call (two questions).

출력이 비어 있으면 조용히 넘어간다. 질문 외에는 부연하지 않는다.

## 이후 — 스킬 절차 실행

`${CLAUDE_PLUGIN_ROOT}/skills/insane-review/SKILL.md`를 Read로 읽고 그 절차(환경 점검 → 의도 파악 → 타겟 선별 → 패킹·투입·회수 → 누락 확인 → 로컬 검증·반영)를 그대로 따른다. 요청 내용은 `$ARGUMENTS`(또는 직전 대화 맥락)다. 선택지 질문은 SKILL.md의 지시대로 AskUserQuestion으로 한다.

(Read로 읽은 SKILL.md가 절차 정본이고, 이 커맨드는 첫 실행 설정과 인자 전달만 맡는다.)
