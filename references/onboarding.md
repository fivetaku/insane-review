# insane-review 환경 온보딩 (STATUS가 전부 ok가 아닐 때만 읽는다)

커맨드 Step 0.5에서 `--ensure-env`의 마지막 줄 `STATUS node=… deps=… browser=… login=… cookie=… cookie_exp=… saved_browser=…`이
전부 ok가 아니면 이 문서를 따른다. 막힌 첫 단계를 아래처럼 AskUserQuestion으로 물어보고 → 선택대로 Claude가 실행 →
`--ensure-env`를 다시 돌려 재확인한다(최대 3~4회 반복). 사용자에게 CLI 타이핑을 시키지 않는다.

`--ensure-env`는 **저장된 브라우저가 있고 CDP가 닫혀 있으면 조용히 1회 자동 기동**한 뒤 상태를 보고한다
(저장값-only·첫감지 폴백 없음, `browser=wrong`이면 자동기동 안 함). 즉 최초 1회 온보딩 이후엔 브라우저를 다시 묻지 않는다.

- **`deps=missing`** → AskUserQuestion(header `의존성`):
  - "지금 자동 설치 (추천)" → Claude가 `--check-env --install` 실행
  - "직접 설치할게요" → `pip install playwright pyperclip` 안내만
  - "취소"
- **`browser=down`** — `--ensure-env`가 저장값 자동기동을 **이미 시도한 뒤**의 상태다. `saved_browser`로 분기한다:
  - **`saved_browser=<이름>`인데도 down** (저장 브라우저 자동기동 실패 — 보통 프로필 락/앱 이동) → AskUserQuestion(header `브라우저`):
    ["다시 시도"(→ `--ensure-env` 재호출) / "다른 브라우저로 변경"(→ 아래 감지 분기) / "취소"]. 이때만 묻는다.
  - **`saved_browser=none`** (최초 1회) → 아래 감지 분기로 한 번 묻고 `--launch-browser "<이름>"`로 띄운다(선택 자동 저장 → 다음 실행부터 무질문 자동기동).

  직접 띄울 때는 `open -a`가 아니라
  `python3 "${CLAUDE_PLUGIN_ROOT}/bin/pack_and_ask.py" --launch-browser "<이름>"` (크로스플랫폼·전용 프로필·선택 자동 저장)로 한다.
  항상 전용 프로필로 실행되므로 사용자 주 브라우저 세션은 건드리지 않는다. 감지 결과로 분기:
  - **2개 이상 감지** → AskUserQuestion(header `브라우저`): `BROWSERS`의 각 브라우저를 선택지로 준다. 사용자 주 브라우저로
    추정되는 것(현재 실행 중일 가능성)엔 "메인 추정 — 가급적 다른 것" 주석. 선택 → `--launch-browser "<이름>"` → 재점검.
  - **정확히 1개 감지**(그게 사용자 메인일 가능성 높음) → AskUserQuestion(header `브라우저`):
    - **"전용 브라우저 하나 설치 (추천)"** → 가벼운 크로미움(Chrome/Brave 등)을 자동화 전용으로 따로 설치하도록 안내
      (ChatGPT 로그인해두고 그 창은 안 건드림). 설치 후 `--launch-browser "<이름>"`.
      *(왜: 메인과 같은 앱을 2창으로 띄우면 빈 프로필·오조작·일부 앱의 멀티인스턴스 불안정으로 혼란이 생긴다.)*
    - **"지금 이 브라우저의 격리 프로필로 진행"** → `--launch-browser "<그 이름>"`. 전용 프로필이라 메인과 분리되지만,
      같은 앱 2창이라 자동화 창은 실수로 건드리지 말 것을 한 줄 고지.
    - "취소"
  - **0개 감지** → AskUserQuestion(header `브라우저`): "크로미움 계열 브라우저가 없습니다 — 설치할까요?" → ["Chrome 설치 안내"/"취소"]
- **`browser=wrong`**(포트 점유) → AskUserQuestion(header `포트충돌`): "9222를 다른 프로세스가 쓰고 있어요. 종료하고 전용 브라우저를 다시 띄울까요?" → ["다시 띄우기"(점유 프로세스 종료 안내 후 `--launch-browser`)/"취소"]
- **`launch_mode=unset`** → SKILL.md 선행 조건의 launch_mode 항목대로 한 번만 묻는다(답이 없으면 background).
- **`login=no`** (로그인 벽이 실제로 확인된 경우에만 나온다) → AskUserQuestion(header `로그인`): "방금 띄운 **전용 브라우저 창**에서 **chatgpt.com 로그인 + Pro 추론 선택**을 끝낸 뒤 계속하세요. (전용 프로필이라 이 로그인은 계속 유지됩니다.)"
  - "로그인 완료 — 계속" → `--ensure-env` 재확인
  - "취소"
- **`login=unknown`** (컴포저·로그인 벽 모두 미확인 — 로딩 지연/CF 챌린지 가능. 로그인을 요구하지 말 것):
  - `cookie=ok`면 세션은 살아있는 것 → `--ensure-env`를 1~2회 재실행해 재점검. 계속 unknown이면 사용자에게 "전용 브라우저 창에 챌린지/오류 화면이 떠 있는지 확인" 요청(재로그인 아님).
  - `cookie=missing|expired`면 그때만 위 `login=no` 분기와 동일하게 로그인 안내.
- **`node=missing`** → AskUserQuestion(header `Node`): "Node.js가 필요합니다(repomix 자동설치에 사용). 설치를 도와드릴까요?" → ["brew로 설치"/"직접 설치할게요"/"취소"] (brew 선택 시 `brew install node`)

`STATUS … login=ok`까지 가면 커맨드 Step 1로 돌아간다. 사용자가 "취소"하면 멈추고 무엇이 남았는지 한 줄로 알려준다.
