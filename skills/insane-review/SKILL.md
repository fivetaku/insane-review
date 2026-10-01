---
name: insane-review
description: GPT Pro(웹 전용·API 없음 — 현 시점 최신 플래그십 모델의 Pro 추론)를 Claude Code 안에서 활용한다. 사용자가 검토/수정/문제/리뷰/의견을 요청하면, 의도를 파악해 repomix로 관련 코드만 정밀 패킹한 뒤 구독 ChatGPT Pro에 투입하고 분석을 회수해 반영한다. 트리거 — "GPT한테 물어봐", "Pro 모델 의견", "다른 모델로 검토해줘", "GPT Pro로 리뷰", "repomix로 묶어서 GPT에 넣어줘", "GPT는 어떻게 생각해", "ask gpt pro", "second opinion". agent-council의 웹 전용 멤버로도 동작.
---

# insane-review

**왜 존재하나:** GPT Pro(최신 플래그십 모델의 Pro 추론)는 **웹(구독)에서만** 쓸 수 있고 **API가 없다.** 그래서 Codex CLI·`omc ask`·agent-council의 기존 API provider로는 못 부른다. 이 스킬은 **구독 ChatGPT 웹을 자동화해 Pro를 Claude Code 안으로 끌어오는 유일한 경로**다. API 비용 0, 사용자의 요금제로 동작.

핵심 가치는 "통째 패킹"이 아니라 **"의도 파악 → 관련 타겟만 정밀 선별 → 그것만 패킹"** 이다. 이 선별을 Claude(너)가 수행하는 것이 이 도구의 차별점이다.

## 선행 조건 — 선택지 기반 온보딩 (사용자에게 CLI 타이핑 금지)

**커맨드 Step 0이 이걸 자동화한다.** Claude가 `--check-env`를 직접 돌려 마지막 `STATUS node=… deps=… browser=… login=…`을
파싱하고, 막힌 단계마다 **AskUserQuestion 선택지**로 물어본 뒤 Claude가 대신 실행한다(`--install`, 브라우저 실행, 재점검).
초보자는 클릭만으로 따라온다.

- **deps**(`playwright`·`pyperclip`): 없으면 "지금 자동 설치" 선택 → `--check-env --install`. (`npx`/repomix는 `npx -y`로 완전 자동.)
- **browser**: 크로미움 계열 브라우저가 디버그포트(9222)에 **전용 프로필**로 떠 있어야 함(주 브라우저와 격리; Chrome 136+는 전용 프로필 없으면 CDP가 안 열림). 없으면 `--check-env`의 `BROWSERS …` 목록으로 브라우저를 고르게 한 뒤 Claude가 `pack_and_ask.py --launch-browser "<이름>"`(크로스플랫폼 mac/win/linux·전용 프로필·선택 자동 저장)을 실행. 1개뿐이면 전용 브라우저 1개 설치를 권장. (쿠키는 전용 프로필에 보존 → 로그인 유지.)
- **launch_mode**(최초 1회 질문): STATUS에 `launch_mode=unset`이면 **AskUserQuestion으로 한 번 물어** `--set-launch-mode <값>`으로 저장한다(이후 재질문 없음). 선택지는 `background`(권장·기본) / `foreground` 둘을 앞에 두고, `headless`는 "권장 안 함" 표기와 함께 마지막에 둔다. 사용자가 답을 안 주거나 넘기면 **background로 진행**한다(미설정 기본값도 background라 그대로 동작). 값이 이미 있으면 묻지 말 것. 나중에 바꾸려면 `--set-launch-mode <값>`(env `INSANE_REVIEW_LAUNCH_MODE`가 config보다 우선). 세 모드의 실측 결과(2026-08-26, macOS):
  - `background` **(기본)**: `open -g`로 띄우고, playwright가 새 탭을 만들 때 앱이 앞으로 나오므로 **탭 생성 직후 다시 숨긴다**. ChatGPT는 정상 브라우저로 인식 → **왕복 성공**. 포커스를 안 뺏어 작업 흐름이 안 끊긴다.
  - `foreground`: 창이 뜨고 앞으로 나온다. 왕복은 되지만 하던 일이 끊긴다. 진행 상황을 눈으로 보고 싶을 때만.
  - `headless`(`--headless=new`): 창이 아예 없다. **하지만 ChatGPT가 컴포저를 안 내줘 전송이 실패한다**(쿠키가 유효해도 `ChatGPT 컴포저 미확인`으로 재시도 소진 — CF 챌린지 추정). **권장하지 않음**; 굳이 쓰려면 `--check-env`로 `login=ok`를 확인하고, 실패하면 즉시 background로 되돌릴 것.
- **login**: `--check-env`의 로그인 프로브가 `login=no`면, "방금 연 브라우저에서 chatgpt.com 로그인 + Pro 추론 선택" 후 "로그인 완료" 선택 → 재점검. **로그인은 자동 불가 → 반드시 사용자에게 요청**(에러로 끝내지 말 것).
- **추론강도**: 미지정 기본은 **Pro**. 사용자가 다른 강도를 명시하면 `--effort "<요청값>"`로 정확히 전달하고 Pro로 덮어쓰지 않는다. `--model`은 기존 호환 별칭이며 서로 다른 값을 중복 지정하면 중단한다. 지원 의미는 `instant`/즉시, `medium`/`standard`/중간, `high`/높음, `xhigh`/`extra high`/`very high`/`extended`/매우 높음, `pro`다. Chat 모드에서 실제 단계 라벨과 최종 선택을 검증하며, 미지원·불일치면 다른 값으로 대체하지 않고 전송을 중단한다. 특정 모델 고정은 `--require-model "<이름>"`; 모델명 미확인도 실패다. 숨긴 모델 목록이나 `Latest`만으로 현재 모델 버전을 추정하지 않는다.

## 핵심 절차 (검토/수정/리뷰 요청을 받았을 때)

### 1) 의도 파악
사용자가 GPT Pro에게 **무엇을** 묻고 싶은지 한 문장으로 정리한다. (버그 원인? 설계 리뷰? 리팩터 방향? 특정 함수 검증?)

### 2) 타겟 선별 — **완전한 관련 집합을 네가(Claude) 판단** (사용자가 누락을 잡아주는 구조면 안 된다)
"repomix로 무엇을 넣을지 = 무엇이 완전한 관련 집합인지", "repomix만으로 충분한지 vs 관련 파일을 다 넣어야 하는지"의 **판단은 네 책임**이다. 기본은 **"넓게, 빠짐없이"**:
- **단일 모듈/플러그인/기능 리뷰면 그 디렉토리를 통째로** 넣어라(`--target <dir>`, `--include` 생략 또는 광범위). 코드 한 파일만 넣으면 실행지시서·설정·통합 맥락이 빠진다(실측: `bin/**`만 넣어 3파일 → README/command/config 누락).
- 더 넓은 범위면 지목 파일에서 **import/require·호출자·피호출자(grep/LSP `find_references`/`goto_definition`)·테스트·타입·설정**까지 추적해 집합을 *닫는다*.
- **패킹 후 `📦 패킹 포함 N개 파일` 감사 목록이 네가 의도한 완전한 집합을 담았는지 직접 확인**한다(§3.5). 사용자가 지적하기 전에 네가 잡아라.
- 결과를 **정확한 파일 목록**(→ `--stdin`) 또는 **글롭**(→ `--include "src/auth/**,*.test.ts"`)으로 만든다.
- **코드 리뷰/원인분석은 풀 코드로 보내라 — `--compress` 쓰지 마라.** 압축은 함수 본문(조건·early return·예외·루프 = 버그 판단 근거)을 제거해 리뷰 AI가 구현을 *상상*하게 만든다(실측: 본문 58% 손실 → false-positive·fail-open 폭증). 멀티 AI 합의(GPT-5.5 Pro·codex·agy·gjc)로 확정.
- 타겟이 너무 커서 컨텍스트를 넘기면 **압축하지 말고 `--include`로 관련 파일만 좁혀 풀로** 보낸다. `--compress`는 오직 "큰 레포 *개요*"(정확성 리뷰 아님)용.

### 3) 패킹 + 투입 + 회수 — 스크립트 실행
아래 `pro`는 **사용자가 강도를 지정하지 않았을 때만** 쓰는 예시다. 명시한 강도가 있으면 그 값으로 바꾼다.
```bash
python3 <plugin>/bin/pack_and_ask.py \
  --target <repo_root> --include "<관련 파일 글롭>" \
  --effort pro \
  --prompt "<의도를 담은 정확한 질문 — '판정마다 파일/라인/코드조각을 인용하라'를 반드시 포함>"
```
또는 정확한 파일 목록을 직접 줄 때(레포를 cwd로):
```bash
printf "src/a.ts\nsrc/b.ts\n" > /tmp/files.txt
# (현재 스크립트는 --include 기반; 정밀 목록은 --include 글롭으로 대체하거나 repomix --stdin 직접 사용)
```
**레포 없이 순수 질문(의견)만:** `--target` 생략 → 프롬프트만 전송.
```bash
python3 <plugin>/bin/pack_and_ask.py --effort pro --force-answer-after 90 \
  --prompt "<질문>"
```

### 3.5) 누락 검증 — **빠진 파일 없는지 감사**
패킹 직후 출력의 **`📦 패킹 포함 N개 파일: ...`** 목록이 **의도한 관련 파일을 전부 담았는지** 확인한다. 빠진 게 있으면 repomix가 떨어뜨린 것 — 원인별 대응:
- `🔒 secretlint: 의심 파일 N개 제외` → **시크릿 든 파일이 통째 빠짐**(숨은 누락). 그 파일이 리뷰 대상이면 시크릿을 가린 사본을 따로 넣거나 `--no-security-check`(외부 유출 주의).
- 기본 ignore/`.gitignore`가 떨어뜨림 → `--no-default-patterns`/`--no-gitignore`.
- 서브모듈 파일이 빠짐(부모서 패킹) → 서브모듈 안에서 `--target`.
- `⚠️ pack이 큼(truncation)` 경고 → ChatGPT가 잘라먹을 수 있으니 `--include`로 더 좁히거나 여러 번 나눠 보낸다.
- **손실 플래그 금지**: `--compress`/`--remove-comments`/`--remove-empty-lines`는 내용을 누락시키니 리뷰엔 쓰지 않는다. 라인번호는 기본 ON(인용용).

### 4) 회수 & 반영
- 응답은 **현재 프로젝트의 `.insane-review/response_*.md`**에 저장되고, stdout 끝에 미리보기가 나온다.
- 그 의견을 읽고 **실제로 검증된 모델·추론강도의 의견임을 명시**하여 사용자에게 반영/요약한다(모델명은 리포트 상단 `- 모델:` 라인의 실제 검증된 이름을 인용). 동의/이견을 너의 판단과 함께 제시하라.

## 주의/가드 (실측 기반)

- **git submodule**: 부모 레포 루트에서 서브모듈 파일은 repomix가 제외한다. 서브모듈 안에서 실행하거나 `--target <submodule>` 또는 `--no-gitignore --no-default-patterns`.
- **압축은 코드 파일만** 줄인다(마크다운/문서 위주 폴더엔 무효).
- **정밀 리뷰엔 `--force-answer-after`를 쓰지 마라** — Pro 추론을 중간에 끊어 "다 생각 안 한 채" 답하게 만든다(gjc 지적, fail-open과 곱해져 미완성 답을 정답 저장). 완전 추론이 더 정확. 안전장치는 `--max-wait`(기본 20분, env/`--max-wait`로 조절)만. force-answer는 빠른 의견·짧은 질문에만.
- **fail-closed**: 첨부 미확인 / 모델·추론단계 미검증(`--effort pro` 검증 실패, 또는 `--require-model` 사용 시 모델명 불일치) / timeout·빈 응답은 **성공 저장 안 하고 중단·재시도**한다(잘못된 컨텍스트나 미완성 답을 리뷰로 저장하지 않음).
- 큰 콘텐츠는 **파일 첨부**로 들어간다(붙여넣기 X). 스크립트가 자동 처리.
- 실패 시 `--retries N`으로 전송/회수를 재시도.

## 채팅 정리 — 폴더명 ChatGPT 프로젝트 (기본 on)
매 실행이 일반 채팅 목록에 쌓이지 않도록, **현재 폴더명과 같은 이름의 ChatGPT 프로젝트** 안에 채팅을 정리한다. 폴더당 프로젝트 1개로 묶여 일반 목록이 깨끗하게 유지된다.
- 폴더명→프로젝트URL은 per-repo 캐시(`.insane-review/projects.json`)에 저장 → 다음 실행부턴 사이드바를 안 건드리고 바로 그 프로젝트로 들어간다(견고).
- 프로젝트가 없으면 자동 생성, 있으면 재사용(중복 생성 안 함). **프로젝트 미지원 플랜이거나 UI가 바뀌어 실패해도 하드중단 없이 일반 채팅으로 폴백.**
- 이름 바꾸려면 `--project "<이름>"`, 끄려면 `--no-project`.

## 주요 플래그
`--target`(생략=프롬프트only) · `--include`(정밀 글롭) · `--compress` · `--effort pro` · `--force-answer-after N` · `--retries N` · `--style xml|markdown|plain` · `--browser <이름|경로>`(전용 프로필; 생략=config→첫 감지) · `--launch-browser <이름>`(전용 프로필 실행+저장) · `--list-browsers` · `--project "<이름>"`(기본=폴더명) · `--no-project` · `--pack-only` · `--council`

## agent-council 멤버로 쓰기
`references/council-setup.md` 참고. `--council` 모드는 프롬프트를 위치인자로 받고 **응답만 stdout**으로 내보내(진행로그는 stderr) council worker가 그대로 캡처한다. Pro를 웹 전용 council 멤버로 등록하면 다른 모델들과 토론에 참여시킬 수 있다.
