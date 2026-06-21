---
description: GPT-5.5 Pro(웹 전용)에게 repomix로 패킹한 코드/질문을 보내 의견을 받아온다
---

# /insane-review

사용자의 요청(`$ARGUMENTS`)을 GPT-5.5 Pro(구독 웹)에게 보내 분석/의견을 받아 반영한다.

> **원칙: 사용자에게 CLI 타이핑을 시키지 않는다.** 환경이 안 갖춰졌으면 Claude가 `--check-env`로 감지하고,
> 필요한 결정은 **AskUserQuestion 선택지**로 물어본 뒤 Claude가 대신 실행한다. 초보자도 클릭만으로 따라올 수 있어야 한다.
> (AskUserQuestion은 frontmatter `allowed-tools`에 **절대 넣지 않는다** — 넣으면 자동승인돼 UI가 안 뜬다.)

## Step 0 — 환경 온보딩 (선택지 기반, 막힌 단계만)

먼저 Claude가 직접 실행한다(사용자에게 시키지 말 것):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/pack_and_ask.py" --check-env
```

마지막 줄 `STATUS node=… deps=… browser=… login=…`을 파싱한다. **전부 ok가 아니면**, 막힌 첫 단계를 아래처럼
AskUserQuestion으로 물어보고 → 선택대로 Claude가 실행 → `--check-env`를 다시 돌려 재확인한다(최대 3~4회 반복).

- **`deps=missing`** → AskUserQuestion(header `의존성`):
  - "지금 자동 설치 (추천)" → Claude가 `--check-env --install` 실행
  - "직접 설치할게요" → `pip install playwright pyperclip` 안내만
  - "취소"
- **`browser=down`** → AskUserQuestion(header `브라우저`):
  - "Comet 자동 실행 (추천)" → Claude가 `open -a Comet --args --remote-debugging-port=9222` → 3초 대기 후 재점검
  - "Chrome 자동 실행" → Claude가 `open -a "Google Chrome" --args --remote-debugging-port=9222`
  - "이미 띄웠어요" → 재점검만
  - "취소"
- **`browser=wrong`**(포트 점유) → AskUserQuestion(header `포트충돌`): "9222를 다른 프로세스가 쓰고 있어요. 그 브라우저를 디버그포트로 다시 띄울까요?" → ["다시 띄우기"/"취소"]
- **`login=no`** → AskUserQuestion(header `로그인`): "방금 연 브라우저에서 **chatgpt.com 로그인 + GPT-5.5 Pro 선택**을 끝낸 뒤 계속하세요."
  - "로그인 완료 — 계속" → `--check-env` 재확인
  - "취소"
- **`node=missing`** → AskUserQuestion(header `Node`): "Node.js가 필요합니다(repomix 자동설치에 사용). 설치를 도와드릴까요?" → ["brew로 설치"/"직접 설치할게요"/"취소"] (brew 선택 시 `brew install node`)

`STATUS … login=ok`까지 가면 Step 1로. 사용자가 "취소"하면 멈추고 무엇이 남았는지 한 줄로 알려준다.

## Step 1~ — 리뷰 실행

1. **의도 파악** — `$ARGUMENTS`(또는 직전 대화 맥락)에서 GPT Pro에게 물을 핵심 질문을 한 문장으로 정한다.
   타겟/범위가 애매하면 **AskUserQuestion으로 선택지**를 줘서 고르게 한다(타이핑 요구 금지). 예) header `리뷰 대상`,
   options = 후보 디렉토리들 + "프로젝트 전체" + "질문만(코드 없이)".
2. **타겟 선별(완전한 집합은 네 판단)** — 코드면 의도에 직결된 **모듈/디렉토리를 통째로**(`--target <dir>`, 풀코드).
   더 넓으면 import·호출자·테스트·설정까지 닫는다. **`--compress` 금지**(본문 누락). 순수 질문이면 생략.
3. **실행** (정확성 리뷰는 풀코드 + 모델검증):
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/bin/pack_and_ask.py" \
     --target <repo_or_dir> --include "<관련 파일 글롭 또는 생략=전체>" \
     --model pro --require-model "GPT-5.5" \
     --prompt "<의도 담은 질문 — 판정마다 파일:라인·코드조각 인용 강제>"
   ```
   - 응답이 오래 걸려도 되면 그대로(완전추론). 시간을 bound하고 싶으면 `--force-answer-after <초>`로
     "거기까지 추론한 내용으로" 답을 받는다. 단독 리뷰는 보통 끄고(완전추론), council은 켜서 cap.
4. **누락 확인** — 출력의 `📦 패킹 포함 N개 파일`이 의도한 완전한 집합을 담았는지 확인(빠지면 §3.5 원인 제거).
5. **회수·반영** — 현재 프로젝트의 **`.insane-review/response_*.md`**를 읽고, **GPT-5.5 Pro의 의견임을 명시**해
   반영하고 너의 판단(동의/이견)을 덧붙인다.

세부 절차·가드는 `skills/insane-review/SKILL.md` 참고. (Read는 참고용; 이 커맨드가 실행 지시서다.)
