---
description: GPT Pro(웹 전용, 최신 플래그십)에게 repomix로 패킹한 코드/질문을 보내 의견을 받아온다
---

# /insane-review

사용자의 요청(`$ARGUMENTS`)을 구독 ChatGPT 웹에 보내 분석/의견을 받아 반영한다. **추론강도 미지정 시 기본은 Pro다. 사용자가 다른 추론강도를 명시하면 그 요청이 우선이며 Pro로 덮어쓰지 않는다.**

> **원칙: 사용자에게 CLI 타이핑을 시키지 않는다.** 환경이 안 갖춰졌으면 Claude가 `--check-env`로 감지하고,
> 필요한 결정은 **AskUserQuestion 선택지**로 물어본 뒤 Claude가 대신 실행한다. 초보자도 클릭만으로 따라올 수 있어야 한다.
> (AskUserQuestion은 frontmatter `allowed-tools`에 **절대 넣지 않는다** — 넣으면 자동승인돼 UI가 안 뜬다.)

## Step 0 — 첫 실행 셋업 (1회, 자동)

가장 먼저 실행한다 — 부트스트랩: 업데이트 알림 훅 설치 + Python 의존성(pyperclip·playwright) 자동 설치. (repomix는 `npx -y`로 실행되어 사전설치 불필요.)

```bash
bash "${CLAUDE_PLUGIN_ROOT}/setup/setup.sh" ask
```

출력이 `STAR_ASK`로 시작하면 즉시 **AskUserQuestion**을 1회 호출한다 — 질문·선택지는 **사용자의 현재 대화 언어**로 작성한다(대화 언어가 분명하면 그것을, 아니면 `STAR_ASK` 뒤 언어코드 `ko/ja/en`을 사용; 무조건 한국어 기본값 금지).
- header: 짧은 현지화된 "GitHub Star" 라벨
- question: 이 플러그인(과 gptaku-plugins 마켓플레이스)에 GitHub ⭐로 응원할지 — 선택 안 해도 모든 기능은 그대로 작동
- options: 정확히 2개 — (1) 응원/스타 → `bash "${CLAUDE_PLUGIN_ROOT}/setup/setup.sh" star yes`; (2) 괜찮아요 → `bash "${CLAUDE_PLUGIN_ROOT}/setup/setup.sh" star no`

출력이 비어 있으면 조용히 넘어간다. 질문 외에는 부연하지 않는다.

## Step 0.5 — 환경 점검 (Claude가 직접 실행)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/pack_and_ask.py" --ensure-env
```

마지막 줄 `STATUS node=… deps=… browser=… login=… …`이 전부 ok면 바로 Step 1로 간다.
**하나라도 ok가 아닐 때만** `${CLAUDE_PLUGIN_ROOT}/references/onboarding.md`를 읽고 그 분기대로 AskUserQuestion 선택지로 해결한다.

## Step 1~ — 리뷰 실행

추론강도·패킹(풀코드, `--compress` 금지)·누락 감사·채팅 프로젝트 정리 규칙의 정본은 `${CLAUDE_PLUGIN_ROOT}/skills/insane-review/SKILL.md`다. 여기서는 순서만 적는다. 추론강도는 미지정이면 Pro, 사용자가 명시하면 그 값을 `--effort`로 그대로 넘긴다(SKILL.md "추론강도").

1. **의도 파악** — `$ARGUMENTS`(또는 직전 대화 맥락)에서 물을 핵심 질문을 한 문장으로 정한다.
   타겟/범위가 애매하면 **AskUserQuestion으로 선택지**를 준다(타이핑 요구 금지). 예) header `리뷰 대상`,
   options = 후보 디렉토리들 + "프로젝트 전체" + "질문만(코드 없이)".
2. **타겟 선별** — SKILL.md §2대로 완전한 관련 집합을 네가 정한다. 순수 질문이면 생략.
3. **실행** — SKILL.md §3의 명령으로 패킹·투입·회수한다. `--prompt`를 생략하면 기본 리뷰 프롬프트(결론 먼저·P0/P1/P2·`파일:라인` 인용·확신도·"발견 없음" 허용)가 쓰인다.
4. **누락 확인** — SKILL.md §3.5대로 `📦 패킹 포함 N개 파일` 목록을 감사한다.
5. **회수·검증·반영** — SKILL.md §4대로 한다. 요약하면: `.insane-review/response_*.md`를 읽고, **발견 사항마다 인용된 코드를 로컬에서 다시 읽어 `확인됨`/`반박됨`/`미확인`으로 분류하고 한 줄 근거를 단 뒤에** 보고한다. 조치 대상으로는 `확인됨`만 제시하고 `반박됨`은 짧게 목록으로만 둔다. 리포트 상단 `- 모델:` 라인을 그대로 인용하고 모델 버전을 추측하지 않는다.

(Read로 읽은 SKILL.md는 참고 자료이고, 이 커맨드가 실행 지시서다.)
