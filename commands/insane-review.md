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

## Step 0.5 — 환경 온보딩 (브라우저·로그인; 선택지 기반, 막힌 단계만)

먼저 Claude가 직접 실행한다(사용자에게 시키지 말 것):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/pack_and_ask.py" --ensure-env
```

`--ensure-env`는 **저장된 브라우저가 있고 CDP가 닫혀 있으면 조용히 1회 자동 기동**한 뒤 상태를 보고한다
(저장값-only·첫감지 폴백 없음, `browser=wrong`이면 자동기동 안 함). **즉 최초 1회 온보딩 이후엔 브라우저를 다시 묻지 않고 알아서 뜬다.**
마지막 줄 `STATUS node=… deps=… browser=… login=… cookie=… cookie_exp=… saved_browser=…`을 파싱한다. **전부 ok가 아니면**, 막힌 첫 단계를 아래처럼
AskUserQuestion으로 물어보고 → 선택대로 Claude가 실행 → `--ensure-env`를 다시 돌려 재확인한다(최대 3~4회 반복).

- **`deps=missing`** → AskUserQuestion(header `의존성`):
  - "지금 자동 설치 (추천)" → Claude가 `--check-env --install` 실행
  - "직접 설치할게요" → `pip install playwright pyperclip` 안내만
  - "취소"
- **`browser=down`** — `--ensure-env`가 저장값 자동기동을 **이미 시도한 뒤**의 상태다. `saved_browser`로 분기한다:
  - **`saved_browser=<이름>`인데도 down** (저장 브라우저 자동기동 **실패** — 보통 프로필 락/앱 이동) → AskUserQuestion(header `브라우저`):
    ["다시 시도"(→ `--ensure-env` 재호출) / "다른 브라우저로 변경"(→ 아래 감지 분기) / "취소"]. **이때만 묻는다** — 저장값이 있으면 자동기동이 기본이며 매번 새로 묻지 않는다.
  - **`saved_browser=none`** (최초 1회 — 아직 기본 미설정) → 아래 감지 분기로 한 번 묻고 `--launch-browser "<이름>"`로 띄운다(선택 **자동 저장 → 다음 실행부터 무질문 자동기동**).

  사용자에게 물어 직접 띄울 때는 `open -a`가 아니라
  `python3 "${CLAUDE_PLUGIN_ROOT}/bin/pack_and_ask.py" --launch-browser "<이름>"` (크로스플랫폼·전용 프로필·선택 자동 저장)로 한다.
  **항상 전용 프로필로 실행되므로 사용자 주 브라우저 세션은 건드리지 않는다.** 감지 결과로 분기:
  - **2개 이상 감지** → AskUserQuestion(header `브라우저`): `BROWSERS`의 각 브라우저를 선택지로 준다. 사용자 주 브라우저로
    추정되는 것(현재 실행 중일 가능성)엔 "메인 추정 — 가급적 다른 것" 주석. 선택 → `--launch-browser "<이름>"` → 재점검.
  - **정확히 1개 감지**(그게 사용자 메인일 가능성↑) → AskUserQuestion(header `브라우저`):
    - **"전용 브라우저 하나 설치 (추천)"** → 가벼운 크로미움(Chrome/Brave 등)을 자동화 전용으로 따로 설치하도록 안내
      (ChatGPT Pro 로그인해두고 그 창은 안 건드림). 설치 후 `--launch-browser "<이름>"`.
      *(왜: 메인과 같은 앱을 2창으로 띄우면 빈 프로필·오조작·일부 앱의 멀티인스턴스 불안정으로 혼란이 생긴다.)*
    - **"지금 이 브라우저의 격리 프로필로 진행"** → `--launch-browser "<그 이름>"`. 전용 프로필이라 메인과 분리되지만,
      **같은 앱 2창이라 자동화 창은 실수로 건드리지 말 것**을 한 줄 고지.
    - "취소"
  - **0개 감지** → AskUserQuestion(header `브라우저`): "크로미움 계열 브라우저가 없습니다 — 설치할까요?" → ["Chrome 설치 안내"/"취소"]
- **`browser=wrong`**(포트 점유) → AskUserQuestion(header `포트충돌`): "9222를 다른 프로세스가 쓰고 있어요. 종료하고 전용 브라우저를 다시 띄울까요?" → ["다시 띄우기"(점유 프로세스 종료 안내 후 `--launch-browser`)/"취소"]
- **`login=no`** (로그인 벽이 실제로 확인된 경우에만 나온다) → AskUserQuestion(header `로그인`): "방금 띄운 **전용 브라우저 창**에서 **chatgpt.com 로그인 + Pro 추론(최신 플래그십 모델) 선택**을 끝낸 뒤 계속하세요. (전용 프로필이라 이 로그인은 계속 유지됩니다.)"
  - "로그인 완료 — 계속" → `--ensure-env` 재확인
  - "취소"
- **`login=unknown`** (컴포저·로그인 벽 모두 미확인 — 로딩 지연/CF 챌린지 가능. **로그인을 요구하지 말 것**):
  - `cookie=ok`면 세션은 살아있는 것 → `--ensure-env`를 1~2회 재실행해 재점검. 계속 unknown이면 사용자에게 "전용 브라우저 창에 챌린지/오류 화면이 떠 있는지 확인" 요청 (재로그인 아님).
  - `cookie=missing|expired`면 그때만 위 `login=no` 분기와 동일하게 로그인 안내.
- **`node=missing`** → AskUserQuestion(header `Node`): "Node.js가 필요합니다(repomix 자동설치에 사용). 설치를 도와드릴까요?" → ["brew로 설치"/"직접 설치할게요"/"취소"] (brew 선택 시 `brew install node`)

`STATUS … login=ok`까지 가면 Step 1로. 사용자가 "취소"하면 멈추고 무엇이 남았는지 한 줄로 알려준다.

## Step 1~ — 리뷰 실행

1. **의도 파악** — `$ARGUMENTS`(또는 직전 대화 맥락)에서 GPT Pro에게 물을 핵심 질문을 한 문장으로 정한다.
   타겟/범위가 애매하면 **AskUserQuestion으로 선택지**를 줘서 고르게 한다(타이핑 요구 금지). 예) header `리뷰 대상`,
   options = 후보 디렉토리들 + "프로젝트 전체" + "질문만(코드 없이)".
2. **타겟 선별(완전한 집합은 네 판단)** — 코드면 의도에 직결된 **모듈/디렉토리를 통째로**(`--target <dir>`, 풀코드).
   더 넓으면 import·호출자·테스트·설정까지 닫는다. **`--compress` 금지**(본문 누락). 순수 질문이면 생략.
3. **추론강도 결정 후 실행** (정확성 리뷰는 풀코드 + 모델검증):
   - 사용자가 명시한 강도를 `--effort "<요청값>"`로 전달한다. 미지정일 때만 아래 예시의 `pro`를 쓴다. `--model`은 기존 호환 별칭이다. 두 옵션에 다른 값을 함께 넣지 않는다.
   - 지원 의미: `instant`/즉시, `medium`/`standard`/중간, `high`/높음, `xhigh`/`extra high`/`very high`/`extended`/매우 높음, `pro`.
   - 요청 단계가 UI에 없거나 최종 선택을 검증하지 못하면 전송을 중단한다. 다른 강도로 대체하지 않는다. Chat 모드에서 검증하며 모델명을 못 읽으면 버전을 추측하지 않는다.
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/bin/pack_and_ask.py" \
     --target <repo_or_dir> --include "<관련 파일 글롭 또는 생략=전체>" \
     --effort pro \
     --prompt "<의도 담은 질문 — 판정마다 파일:라인·코드조각 인용 강제>"
   ```
   - 응답이 오래 걸려도 되면 그대로(완전추론). 시간을 bound하고 싶으면 `--force-answer-after <초>`로
     "거기까지 추론한 내용으로" 답을 받는다. 단독 리뷰는 보통 끄고(완전추론), council은 켜서 cap.
4. **누락 확인** — 출력의 `📦 패킹 포함 N개 파일`이 의도한 완전한 집합을 담았는지 확인(빠지면 §3.5 원인 제거).
5. **회수·반영** — 현재 프로젝트의 **`.insane-review/response_*.md`**를 읽고, **실제로 검증된 모델·추론강도의 의견임을 명시**해
   반영하고 너의 판단(동의/이견)을 덧붙인다. 모델명은 리포트 상단 `- 모델:` 라인(실제 검증된 이름)을 그대로 인용한다.

> **채팅 정리(기본 on):** 매 실행은 일반 채팅 목록 대신 **현재 폴더명과 같은 ChatGPT 프로젝트** 안에 정리된다(폴더당 프로젝트 1개, 캐시 재사용·자동 생성·실패 시 일반채팅 폴백). 채팅이 일반 목록에 쌓이는 걸 막는다. 이름은 `--project "<이름>"`, 끄려면 `--no-project`.

세부 절차·가드는 `skills/insane-review/SKILL.md` 참고. (Read는 참고용; 이 커맨드가 실행 지시서다.)
