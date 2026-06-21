[English](README.md) | 한국어

# insane-review

> **GPT-5.5 Pro는 API가 없다. 그래도 Claude Code 안에서 쓴다.**

GPT-5.5 Pro는 ChatGPT 웹(구독)에서만 쓸 수 있고 **공식 API가 없다.** Codex CLI·`omc ask`·agent-council의 API provider로는 못 부른다. insane-review는 **로그인된 ChatGPT 웹 세션을 CDP로 자동화**한다 — repomix로 관련 코드만 패킹해 Pro에 투입하고 분석을 회수한다. **API 비용 0**, 당신의 ChatGPT 요금제로 동작한다.

[빠른 시작](#빠른-시작) • [왜?](#왜-insane-review) • [동작 방식](#동작-방식) • [기능](#기능) • [타임아웃 조정](#타임아웃--튜닝) • [선행 조건](#선행-조건)

---

## 빠른 시작

### 1. 마켓플레이스 추가 (최초 1회)

```
/plugin marketplace add https://github.com/fivetaku/gptaku_plugins.git
```

### 2. 설치

```
/plugin install insane-review
```

### 3. Claude Code 재시작

플러그인 로드에 필요하다.

### 4. 브라우저 브릿지 준비 (머신당 1회)

Pro는 웹 전용이라 디버그포트로 로그인된 브라우저가 필요하다:

```bash
# Comet(또는 Chrome)을 CDP 포트로 띄우고 → chatgpt.com 로그인 → GPT-5.5 Pro 선택
open -a Comet --args --remote-debugging-port=9222

# 환경 점검(node/repomix, playwright, pyperclip, CDP 브라우저). --install이면 pip 의존성 자동설치
python3 bin/pack_and_ask.py --check-env --install
```

### 5. 실행

```
/insane-review src/auth 인증 흐름 검토해줘
```

또는 자연어로 "Pro한테 이거 리뷰시켜줘" / "GPT-5.5 Pro 의견 받아와" — Claude가 타겟을 정해 패킹한다.

---

## 왜 insane-review?

- **Pro를 부를 유일한 길** — API가 없다. 로그인된 웹 세션을 CDP로 모는 것이 유일한 브릿지이고, 구독료 외 비용은 0이다.
- **관련 집합 판단은 Claude가** — 파일을 손으로 나열할 필요 없다. 리뷰는 **풀코드**로 보낸다(`--compress`는 함수 본문을 날려 "문제없음" 오판을 부르므로 리뷰엔 안 씀). 패킹된 파일 목록을 감사해 빠진 게 없게 한다.
- **설계가 fail-closed** — 모델 불일치, 미검증 로그인, 잘린 프롬프트, 빈 pack, 이전 턴의 답변은 *조용히 보내거나 저장하지 않고* 거부한다. Pro 자신의 셀프리뷰 4라운드로 강화(P0 6 → 0).
- **두 역할, 하나의 엔진** — 수정/리뷰 요청 시 단독 리뷰어, 또는 [agent-council](references/council-setup.md)의 웹 전용 멤버로 다른 모델들과 토론.
- **따라갈 수 있는 근거** — 라인번호를 함께 패킹해 Pro가 `파일:라인`으로 답한다.

---

## 동작 방식

```
"Pro한테 리뷰시켜줘"  /  council 멤버 호출
  ↓
Claude가 '완전한 관련 파일 집합'을 선별 (풀코드 — 리뷰엔 --compress 안 함)
  ↓
repomix 패킹  (라인번호 · secretlint · 패킹파일 감사 · 토큰 수)
  ↓
로그인된 ChatGPT 세션에 CDP attach
GPT-5.5 + Pro 추론단계 선택  → 메뉴 재오픈해 검증 (불일치=중단, fail-closed)
  ↓
pack 첨부 + 프롬프트  → 프롬프트가 입력창에 실제로 들어갔는지 확인  → 전송
  ↓
'이번 턴' 완료까지 대기 (턴-스코프: 새 assistant 노드 + 새 copy 버튼)
긴 리즈닝은 --force-answer-after로 조기 종료 가능
  ↓
응답 회수 → ./.insane-review/response_*.md 저장 (원자적 쓰기)
```

출력은 **실행한 현재 프로젝트**의 `.insane-review/` 폴더에 저장된다(kkirikkiri의 `.kkirikkiri/` 패턴). 플러그인 내부에는 절대 저장하지 않는다:

```
.insane-review/
├── pack_<대상>_<ts>.md        # 보낸 내용 (chmod 600)
└── response_<대상>_<ts>.md    # Pro 답변 + 검증된 모델 헤더
```

---

## 기능

### 커맨드

| 커맨드 | 동작 |
|--------|------|
| `/insane-review [대상/질문]` | 관련 코드를 패킹해 GPT-5.5 Pro에 리뷰 요청 |
| 자연어 | "Pro한테 리뷰시켜줘", "GPT-5.5 Pro 의견 받아와" — 동일 흐름 |

### 두 가지 모드

1. **단독 리뷰어** — 수정/리뷰 요청 → Claude가 타겟 선별 → repomix 패킹 → Pro 분석 → 반영.
2. **agent-council 멤버** — Pro를 웹 전용 council 멤버로 등록해 다른 모델들과 토론. [`references/council-setup.md`](references/council-setup.md) 참고.

### 주요 플래그

| 플래그 | 용도 |
|--------|------|
| `--target <dir>` | 패킹할 폴더(생략 시 질문만 = 의견 모드) |
| `--include <glob>` / `--ignore <glob>` | 패킹 범위 좁히기 |
| `--model pro` | 추론단계 선택(예: Pro) |
| `--require-model "GPT-5.5"` | 활성 모델명 검증 — 불일치 시 전송 중단(fail-closed) |
| `--prompt "..."` / `--prompt-file` | 질문 |
| `--pack-only` | 패킹만(토큰 확인), 전송 안 함 |
| `--council` | council 모드 — 응답만 stdout, 로그는 stderr |
| `--compress` | tree-sitter 골격만 — **리뷰엔 쓰지 마라**(함수 본문 제거) |
| `--check-env` / `--install` | 로컬 환경 점검 / 의존성 설치 |

---

## 타임아웃 / 튜닝

Pro 완전추론은 10~15분이 걸릴 수 있어, 응답 대기·패킹 타임아웃을 CLI와 환경변수로 조정한다.

| 컨트롤 | 기본값 | 동작 |
|--------|--------|------|
| `--force-answer-after <초>` | off | **소프트 컷**: N초 후에도 리즈닝 중이면 **"지금 답변 받기"**를 눌러 **거기까지 추론한 내용으로** 완성 답변을 받아 저장(아래 설명) |
| `--max-wait <초>` | `1200`(20분) | 응답 최대 대기. 넘으면 fail-closed로 포기(미완성 미저장) |
| `INSANE_REVIEW_MAX_WAIT` | `1200` | `--max-wait`의 환경변수판 |
| `INSANE_REVIEW_REPOMIX_TIMEOUT` | `300` | repomix 패킹 단계 최대 초 |
| `--retries <n>` | `1` | 전송/회수 실패 시 재시도 횟수 |

**두 "타임아웃"은 다르다 — 혼동 금지:**

- **`--force-answer-after N` (소프트 컷, 비용 bound에 권장).** Pro는 오래 추론한다. 이 옵션은 N초에 ChatGPT의 *"지금 답변 받기"*를 눌러 **그 시점까지 추론한 내용으로** 답하게 한다. 그 답변은 정상적인 완성 턴이라 평소처럼 회수·저장된다. council 멤버를 10분 넘게 기다리는 대신 120초에서 끊을 때 쓴다. **→ "타임아웃되면 거기까지 리즈닝한 걸로 답변" = 바로 이 기능.**
- **`--max-wait N` (하드 천장, fail-closed).** N초 안에 턴이 끝나지 않고 *force-answer도 안 걸렸으면*, insane-review는 **저장하지 않고** 포기한다 — 반쯤 스트리밍된 텍스트를 결과로 넘기지 않는다. 잘린 리뷰를 '완료'인 척 주지 않기 위한 의도된 fail-closed다.

기타 환경변수:

| 변수 | 기본값 | 동작 |
|------|--------|------|
| `INSANE_REVIEW_CDP_PORT` | `9222` | 브라우저 원격 디버깅 포트 |
| `INSANE_REVIEW_COMET` / `INSANE_REVIEW_CHROME` | 앱 기본 경로 | 브라우저 실행파일 경로 |
| `INSANE_REVIEW_REPOMIX_VERSION` | `1.15.0` | 핀된 repomix 버전(재현성) |
| `INSANE_REVIEW_OUT` | `./.insane-review` | 출력 폴더(또는 `--out-dir`) |

```bash
# 예: Pro에 최대 25분 주되, 5분까지도 추론 중이면 거기서 끊어 답변 받기
INSANE_REVIEW_MAX_WAIT=1500 python3 bin/pack_and_ask.py \
  --target . --include "src/**" --model pro --require-model "GPT-5.5" \
  --force-answer-after 300 --prompt "동시성 버그 어디 있어?"
```

---

## 선행 조건

### 필요

- [Claude Code](https://docs.anthropic.com/claude-code)
- Python 3.11+ + `playwright`, `pyperclip`
- Node.js / `npx`
- **GPT-5.5 Pro 구독 ChatGPT 계정**, 디버그포트(`--remote-debugging-port=9222`)로 띄운 Comet/Chrome에 로그인된 상태

### 자동 처리 vs. 직접

| 의존성 | 첫 실행 동작 |
|--------|-------------|
| **repomix** | **완전 자동** — `npx -y repomix@<핀>`으로 필요 시 받아옴, 사전설치 불필요 |
| **playwright / pyperclip** | 첫 사용 시 `--check-env`로 점검, `--install`로 설치(`pip install`). 없는 채 일반 실행하면 중간에 깨지지 않고 안내하며 멈춤(fail-closed) |
| **브라우저 로그인 + GPT-5.5 Pro** | **수동** — 자동화 불가. `chatgpt.com` 로그인 + Pro 선택을 1회 직접 |

```bash
# 한 번에: node/repomix·playwright·pyperclip·CDP 브라우저 점검 + 부족한 pip 의존성 설치
python3 bin/pack_and_ask.py --check-env --install
```

### 참고

웹 UI 자동화는 OpenAI ToS상 권장되지 않으며, ChatGPT DOM 변경 시 셀렉터 보수가 필요할 수 있다. 개인 구독 사용 전제.

---

## 라이선스

MIT

---

<div align="center">

**API는 없다. 그래도 Pro.**

</div>
