---
description: GPT-5.5 Pro(웹 전용)에게 repomix로 패킹한 코드/질문을 보내 의견을 받아온다
---

# /insane-review

사용자의 요청(`$ARGUMENTS`)을 GPT-5.5 Pro(구독 웹)에게 보내 분석/의견을 받아 반영한다.

실행 지시:

1. **의도 파악** — `$ARGUMENTS`(또는 직전 대화 맥락)에서 GPT Pro에게 물을 핵심 질문을 한 문장으로 정한다.
2. **타겟 선별(완전한 집합은 네 판단)** — 코드면 의도에 직결된 **모듈/디렉토리를 통째로**(`--target <dir>`, 풀코드). 더 넓으면 import·호출자·테스트·설정까지 닫는다. **`--compress` 금지**(본문 누락). 순수 질문이면 생략.
3. **선행 확인** — Comet/Chrome가 디버그포트(9222)로 떠 chatgpt.com 로그인됐는지. 일반 모드로 떠 있으면 사용자에게 재시작 요청(로그인은 유지됨).
4. **실행** (정확성 리뷰는 풀코드 + 모델검증, force-answer 끄기):
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/bin/pack_and_ask.py" \
     --target <repo_or_dir> --include "<관련 파일 글롭 또는 생략=전체>" \
     --model pro --require-model "GPT-5.5" \
     --prompt "<의도 담은 질문 — 판정마다 파일:라인·코드조각 인용 강제>"
   ```
5. **누락 확인** — 출력의 `📦 패킹 포함 N개 파일`이 의도한 완전한 집합을 담았는지 확인(빠지면 §3.5 원인 제거).
6. **회수·반영** — 현재 프로젝트의 **`.insane-review/response_*.md`**를 읽고, **GPT-5.5 Pro의 의견임을 명시**해 반영하고 너의 판단(동의/이견)을 덧붙인다.

세부 절차·가드는 `skills/insane-review/SKILL.md` 참고. (Read는 참고용; 이 커맨드가 실행 지시서다.)
