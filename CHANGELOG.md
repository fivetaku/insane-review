# Changelog

## 0.5.3 — 2026-06-29

저장된 브라우저 자동 기동 — 매 실행 브라우저 재질문 제거.

- **`--ensure-env` 신설 + 자동 기동**: 최초 1회 브라우저를 고르면 `~/.insane-review/config.json`에 저장되고, 이후 실행부터는 CDP가 닫혀 있어도(`browser=down`) **저장된 브라우저를 조용히 자동 기동**한 뒤 진행한다(재질문 없음). `--check-env`는 부작용 없는 순수 점검으로 유지하고, 자동 기동(부작용)은 `--ensure-env`로 분리(CQS). 커맨드 Step 0.5가 `--ensure-env`를 호출한다.
- **저장값-only(폴백 차단)**: 자동 기동은 저장된 선택이 있을 때만 발동하고, 없으면 첫 감지 브라우저로 폴백하지 않는다(사용자 메인 브라우저를 무프롬프트로 띄우던 위험 차단). `browser=wrong`(포트 점유) 시엔 자동 기동하지 않고 포트충돌 안내로 분기.
- **STATUS에 `saved_browser=<name|none>` 토큰 추가** — 커맨드가 "최초 1회만 질문"을 명시적으로 판단.
- agent-council(claude/codex/gjc) 리뷰 반영: CQS 분리 · 폴백 풋건 차단 · `wrong` 가드.

## 0.5.1 — 2026-06-22

- **setup.sh hook 교정 보강** (GPT-5.5 Pro 셀프리뷰 후속): 업데이트 알림 hook을 `"gptaku-update-check"` **문자열 포함 여부로만** 판단해, 옛/깨진 경로를 가리키는 기존 hook이 있으면 "있음"으로 오인하고 교정하지 않던 엣지케이스 수정. 이제 기존 hook을 찾아 command가 올바른 경로와 다르면 **교정**하고, 없으면 추가한다(중복 없이). 격리 3-케이스 테스트로 검증(깨진→교정 / 올바름→유지 / 없음→추가).

## 0.5.0 — 2026-06-22

GPT-5.5 Pro 셀프리뷰의 남은 P1 3건 수정.

- **동명 폴더 → 같은 ChatGPT 프로젝트 병합 방지(P1)**: 자동 프로젝트명을 `폴더명 · 경로해시8`로 만든다. 원격(ChatGPT) 프로젝트 탐색이 표시이름으로만 매칭하던 탓에 다른 폴더(`/a/api`, `/b/api`)의 리뷰 채팅이 한 프로젝트로 섞이던 문제 수정. `--project` 명시 시 그 이름 그대로(사용자 의도 존중). 라이브 검증: `insane-review · c9b510fe` 신규 프로젝트 생성 확인.
- **repomix hermetic config(P1)**: 외부 repomix 설정(CWD의 `.ts/.js/json`·글로벌)이 압축·본문생략(`output.files`)·보안검사를 조용히 바꾸지 못하도록 안전한 임시 config를 만들어 `--config`로 강제. 검증: 외부 `compress:true` 설정을 우리 config가 덮어써 함수 본문 보존 확인.
- **첨부→붙여넣기 폴백 구현 + `--attach` 정합(P1)**: 문서엔 있으나 실제론 없던 폴백을 구현 — 첨부 실패 시 pack이 상한(기본 50,000자, env `INSANE_REVIEW_PASTE_MAX`) 내면 프롬프트에 인라인으로 붙여 전송, 초과면 fail-closed(잘린 컨텍스트 전송 방지). `--attach`는 폴백 없이 첨부만 강제(help 문구도 정정). 검증: 작은 pack→래핑 / 상한 초과→None(fail-closed).

## 0.4.2 — 2026-06-22

- **setup.sh 첫 실행 멱등성 버그 수정(P0)** — insane-review 자기 리뷰(GPT-5.5 Pro)가 찾은 버그:
  - ① 실패해도(node 없음 / 의존성 설치 실패) **완료 마커를 무조건 기록**해 다음 실행이 복구를 영영 건너뛰던 문제 → 핵심 의존성(pyperclip·playwright)이 **실제 import될 때만** 마커 기록(실패 시 미기록 → 다음 실행 재시도).
  - ② 체크 스크립트(cjs) 복사가 실패/스킵됐는데도 **hook을 등록**해 '없는 파일'을 가리키는 깨진 hook이 매 세션 에러내던 문제 → cjs가 **실제 복사돼 자리잡았을 때만** hook 등록.
  - 격리 임시환경 3-케이스 테스트로 검증(성공=마커+hook / cjs없음=hook미등록 / deps실패=마커미기록).

## 0.4.1 — 2026-06-22

- **전용 프로필 스테일 인스턴스 자가복구(버그 수정)**: 전용 프로필에 브라우저가 이미 떠 있는데 디버그 포트는 안 열린 상태(같은 `user-data-dir` 싱글톤 교착 — Chromium이 새 런치를 기존 인스턴스로 포워딩하고 종료시켜 포트가 안 열림)에서 `launch_browser_exe`가 30초 타임아웃나던 버그 수정. 첫 런치(15초)에 포트가 안 뜨면 **전용 프로필 프로세스를 정리(로그인은 디스크 보존)하고 1회 재시도**한다. 충돌 없는 보통 경로(2초 내 포트 오픈)에선 아무것도 종료하지 않아 부작용 0. 크로스플랫폼(mac/linux `pkill -f <프로필경로>`, win PowerShell `CommandLine -like`). 결정적 재현 테스트로 검증: 포트 없는 stale 인스턴스 → 자가복구 → CDP 오픈. Chrome 전용 프로필에서 실제 코드리뷰 7,012자 회수(584s, exit 0)도 확인.

## 0.4.0 — 2026-06-22

크로스플랫폼 + 전용 브라우저 프로필 + 동적 브라우저 선택 (mac/win/linux). GPT-5.5 Pro 셀프리뷰에서 드러난 결함 반영, 실제 Chrome 종단 검증(전용 프로필 CDP 오픈 / insert_text 전송·회수 / 4자 짧은 응답 수락).

- **전용 브라우저 프로필 (P1)**: 브라우저를 항상 별도 `--user-data-dir`(`~/.insane-review/browser-profile`)로 띄운다 — 주 브라우저 세션과 격리. **Chrome 136+는 기본 프로필에서 `--remote-debugging-port`를 정책적으로 무시**(쿠키 탈취 방지)하므로 전용 프로필 없이는 CDP가 안 열렸다. 실측: Chrome이 전용 프로필로 CDP 정상 오픈.
- **크로스플랫폼 브라우저 스캔/실행**: mac(`/Applications`)·windows(Program Files/LocalAppData)·linux(`which`)별로 설치된 크로미움(Chrome/Comet/Brave/Edge/Chromium/Vivaldi)을 감지. 실행은 `open -a`(mac 전용) 대신 직접 exec로 통일해 win/linux 지원.
- **동적 브라우저 선택**: `--list-browsers`/`--launch-browser <이름|경로>` 추가. `--browser`가 임의 이름/경로 수용(기존 `comet|chrome` 고정 choices 제거). 온보딩은 설치 개수(0/1/≥2)별 분기 — 1개뿐이면 전용 브라우저 1개 설치를 권장, 선택은 `~/.insane-review/config.json`에 영속(다음부터 재질문 없음).
- **클립보드 제거(크로스플랫폼 입력)**: 프롬프트 입력을 OS 클립보드+⌘V(mac 전용)에서 Playwright 네이티브 `insert_text`로 교체 — win/linux 입력 깨짐 + 동시 실행 시 클립보드 경합을 동시 해결.
- **길이 하한 제거(버그)**: 정상적인 짧은 응답(예: 4자)이 `>=40`자 하한에 걸려 "너무 짧음 → 실패"로 버려지던 버그 수정(GPT 셀프리뷰 P1 재현). 완료 판정은 새 턴 + copy 버튼 + 8초 텍스트 안정으로 충분하므로 이제 빈 문자열만 거부. 실측: 4자 응답 정상 수락(exit 0).
- check-env가 `os=`·`BROWSERS …` 라인을 출력(온보딩 분기용). 커맨드·SKILL 문서 동기화(브라우저 온보딩 → 전용 프로필/`--launch-browser`, 응답 경로 `.insane-review/`, `--browser` 플래그).

## 0.3.2 — 2026-06-22

- **다이얼로그 행/크래시 수정:** `connect_over_cdp`로 실제 브라우저에 붙을 때 ChatGPT 페이지의 JS 다이얼로그(beforeunload 등)가 playwright 기본 auto-dismiss와 레이스 → `ProtocolError: No dialog is showing` 미캐치 예외로 드라이버 크래시(100% CPU 스핀, 프롬프트 제출 실패). 페이지/컨텍스트에 다이얼로그 핸들러(`_guard_dialogs`)를 등록해 기본 auto-dismiss를 대체하고 레이스를 무시. 실제 ChatGPT 제출→응답 회수로 검증.

## 0.3.1 — 2026-06-22

Hardening of the v0.3.0 project grouping, from a GPT-5.5 Pro self-review of the new code:

- **True fallback on errors**: `ensure_project()` is now fully wrapped so any exception (DOM race, navigation timeout) returns `None` instead of propagating; `main()` then verifies the project composer actually loaded and otherwise falls back to a normal chat. Previously an exception aborted the whole run instead of degrading gracefully.
- **No more missed/duplicate projects**: project lookup now matches by the row's **displayed name** (language-agnostic, no longer depends on Korean `aria-label`s) and **scrolls the sidebar** until the target appears, so a virtualized/long project list no longer causes a same-named duplicate to be created. Create/submit buttons match ko/en/ja with an Enter-key fallback.
- **Path-scoped cache key**: the folder→project URL cache is keyed by `"{absolute path}::{name}"`, so two different folders that share a basename (or the same folder run with different `--project`) never collide.

## 0.3.0 — 2026-06-22

- Chats are now organized into a **folder-named ChatGPT Project** instead of piling up in the general chat list. Each run files its chat under a project matching the current folder name (one project per folder), so the main chat list stays clean.
- Resolution order is **cache → sidebar lookup → create**: the folder→project URL is cached per-repo in `.insane-review/projects.json`, so subsequent runs navigate straight to the project without touching the sidebar. Existing same-named projects are reused (no duplicates); missing ones are auto-created via the "새 프로젝트" modal.
- **Fail-safe**: if a project can't be resolved/created (unsupported plan, UI change, etc.) the run falls back to a normal chat instead of hard-failing. The whole attach / model-verify (GPT-5.5 Pro) / send / retrieve flow is unchanged — only *where* the chat lands.
- New flags: `--project "<name>"` (default = current folder name) and `--no-project` (disable grouping).

## 0.2.1 — 2026-06-21

- Added `setup/setup.sh` (first-run bootstrap): installs the marketplace update-notifier hook and auto-installs the Python deps (`pyperclip`, `playwright`) for the GPT-Pro web bridge. repomix still runs via `npx -y` (no preinstall); browser CDP launch + ChatGPT login stay in the command's interactive onboarding (Step 0.5).
- GitHub star is now opt-in via AskUserQuestion (네 / 아니요): asked once and recorded (`~/.gptaku-setup/insane-review.star.json`), never re-asked. The prompt is shown in the user's current language — falling back to the language detected from recent Claude sessions (else English) when there's no signal yet. No auto-star.

## 0.2.0

- GPT-5.5 Pro (web-only) bridge: repomix pack → subscription ChatGPT Pro via CDP → review retrieval. Standalone reviewer + agent-council web member.
