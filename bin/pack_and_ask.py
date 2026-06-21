#!/usr/bin/env python3
"""
insane-review — repomix 패킹 → 구독 ChatGPT(웹) GPT-5.5 Pro 투입 → 분석 회수 (API 비용 0)

흐름:
  1) 분석 대상 폴더를 repomix로 단일 파일 패킹 (--compress, secretlint 기본 on)
  2) Comet/Chrome를 CDP로 attach → 로그인된 chatgpt.com 세션 재사용
  3) 패킹본을 '파일 첨부' + 짧은 프롬프트로 투입 (모델/추론단계 검증)
  4) 턴 단위로 응답 완료를 판정(stop-button 사라짐 + copy 버튼 등장 + 텍스트 안정) → 회수
  5) 응답을 .md로 원자적 저장

v2 (2026-06-20): GPT-5.5 Pro 리뷰 반영 — 턴-스코프 판정, 모델 검증, fail-closed CDP/로그인,
force-answer 재시도, UUID/PID 파일명, repomix 버전 핀+timeout, 권한/시크릿, env 설정화.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

# ---- 선택 의존성(라이브 모드에서만 필요) ----
try:
    import pyperclip
except ImportError:
    pyperclip = None
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

# ---------------------------------------------------------------------------
# 설정 (env로 오버라이드 가능 — 하드코딩 탈피)
# ---------------------------------------------------------------------------
COMET_PATH = os.environ.get("INSANE_REVIEW_COMET", "/Applications/Comet.app/Contents/MacOS/Comet")
CHROME_PATH = os.environ.get("INSANE_REVIEW_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CDP_PORT = int(os.environ.get("INSANE_REVIEW_CDP_PORT", "9222"))
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
# repomix 버전 핀(재현성·공급망) — env로 갱신. 빈 문자열이면 latest.
REPOMIX_VERSION = os.environ.get("INSANE_REVIEW_REPOMIX_VERSION", "1.15.0")
REPOMIX_TIMEOUT = int(os.environ.get("INSANE_REVIEW_REPOMIX_TIMEOUT", "300"))

CHATGPT_URL = "https://chatgpt.com/"
INPUT_SELECTORS = ["#prompt-textarea", 'div[contenteditable="true"]']
FILE_INPUT_SELECTOR = 'input[type="file"]'
COPY_BTN = 'button[data-testid="copy-turn-action-button"]'
STREAMING_BTN = 'button[data-testid="stop-button"]'
USER_MSG_SELECTOR = '[data-message-author-role="user"]'
ASSISTANT_MSG_SELECTOR = '[data-message-author-role="assistant"]'
LOGIN_WALL_SELECTORS = [
    'button[data-testid="login-button"]',
    'a[href*="auth/login"]',
    'button:has-text("로그인")',
    'button:has-text("Log in")',
]

MAX_WAIT_SECS = int(os.environ.get("INSANE_REVIEW_MAX_WAIT", "1200"))  # 기본 20분(--max-wait/env로 변경)
MIN_WAIT_SECS = 20
STABLE_CHECK_SECS = 8
STATUS_INTERVAL = 15
FORCE_MAX_TRIES = 6    # force-answer 클릭 재시도 상한

# 출력은 '실행한 현재 프로젝트'의 .insane-review/ 에 저장(플러그인 내부 X — kkirikkiri의 .kkirikkiri 패턴).
# env INSANE_REVIEW_OUT 또는 --out-dir로 오버라이드.
OUT_DIR = Path(os.environ["INSANE_REVIEW_OUT"]).expanduser() if os.environ.get("INSANE_REVIEW_OUT") \
    else Path.cwd() / ".insane-review"

DEFAULT_PROMPT = (
    "첨부는 repomix로 패킹한 코드베이스입니다. 다음을 한국어로 분석해줘:\n"
    "1) 이 프로젝트가 하는 일과 전체 아키텍처\n"
    "2) 핵심 모듈 간 데이터 흐름\n"
    "3) 잠재적 버그/리스크 또는 개선점 3가지 (근거 파일 경로 포함)\n"
    "결론부터 말하고 근거는 그 뒤에."
)


# ===========================================================================
# 1) repomix 패킹 (버전 핀 + timeout + returncode + 권한 + 시크릿 노트)
# ===========================================================================
def pack_repo(target: Path, *, include: str | None, ignore: str | None,
              compress: bool, style: str, token_budget: int | None,
              out_path: Path, line_numbers: bool = True) -> tuple[Path, int | None]:
    if shutil.which("npx") is None:
        sys.exit("❌ npx가 없습니다. Node.js를 설치하세요.")

    # 시크릿 위생: 대상에 secretlint(보안검사)를 끄는 로컬 repomix 설정이 있으면 외부전송 전 중단(fail-closed)
    for cfg in ("repomix.config.json", "repomix.config.json5", "repomix.config.jsonc"):
        p = target / cfg
        if p.exists():
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                sys.exit(f"❌ {cfg} 읽기 실패({str(exc)[:60]}) — 보안설정 검증 불가로 중단(fail-closed).")
            # 키/값의 따옴표 유무(JSON 쌍따옴표 / JSON5 무따옴표·단따옴표) 모두 매칭
            if re.search(r"""['"]?enableSecurityCheck['"]?\s*:\s*false""", raw):
                sys.exit(f"❌ {cfg}에서 보안검사(enableSecurityCheck)가 꺼져 있음 — 시크릿 유출 위험으로 중단.\n"
                         "     보안검사를 켜거나 해당 설정을 제거한 뒤 다시 실행하세요.")

    if compress:
        print("  ⚠️  --compress: 함수 본문이 제거된다(시그니처 골격만). 정확성 리뷰/원인분석엔 부적합 —\n"
              "       리뷰면 끄고, 너무 크면 --include로 관련 파일만 좁혀 풀로 보내라.")

    spec = f"repomix@{REPOMIX_VERSION}" if REPOMIX_VERSION else "repomix@latest"
    cmd = ["npx", "-y", spec, str(target), "-o", str(out_path), "--style", style]
    if line_numbers:
        cmd.append("--output-show-line-numbers")  # AI가 파일:라인 인용 가능 → 근거 강제에 필요
    if compress:
        cmd.append("--compress")
    if include:
        cmd += ["--include", include]
    if ignore:
        cmd += ["--ignore", ignore]
    if token_budget:
        cmd += ["--token-budget", str(token_budget)]

    print(f"  $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=REPOMIX_TIMEOUT)
    except subprocess.TimeoutExpired:
        # 타임아웃 전에 repomix가 부분 산출물을 남겼으면 권한 축소(시크릿 위생 — 모든 실패경로 보장)
        if out_path.exists():
            try:
                os.chmod(out_path, 0o600)
            except OSError:
                pass
        sys.exit(f"❌ repomix 타임아웃({REPOMIX_TIMEOUT}s) — 네트워크/범위 확인")
    out = proc.stdout + proc.stderr

    tokens = None
    m = re.search(r"Total Tokens:\s*([\d,]+)", out)
    if m:
        tokens = int(m.group(1).replace(",", ""))

    # 시크릿 스캔 결과 노출 (repomix는 secretlint 기본 on — hit 파일은 출력에서 제외됨)
    sm = re.search(r"(\d+)\s+suspicious file", out)
    if sm and int(sm.group(1)) > 0:
        print(f"  🔒 secretlint: 의심 파일 {sm.group(1)}개 감지 → 출력에서 제외됨(외부 전송 안전)")

    if proc.returncode != 0:
        # 실패해도 repomix가 산출물을 남겼으면 권한 축소(token-budget 초과 시 파일 생성됨 — 시크릿 위생)
        if out_path.exists():
            try:
                os.chmod(out_path, 0o600)
            except OSError:
                pass
        if token_budget and tokens and tokens > token_budget:
            sys.exit(f"⚠️ 중단: 토큰 예산 초과 — 패킹은 완료됐으나 {tokens:,} > {token_budget:,} 한도. "
                     "범위를 좁히거나(--include) 예산을 늘리세요(--token-budget). [요청한 예산 가드]")
        else:
            sys.exit(f"❌ repomix 실행 실패 (rc={proc.returncode}) — 로그를 확인하세요.\n"
                     "     " + "\n     ".join(out.strip().splitlines()[-6:]))

    if not out_path.exists():
        sys.exit("❌ repomix 출력 파일이 생성되지 않았습니다.")

    # 외부 웹 서비스로 나가는 파일 → 권한 축소
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass

    size = out_path.stat().st_size
    print(f"  ✓ 패킹 완료: {out_path.name}  ({size:,} bytes"
          + (f", ~{tokens:,} tokens)" if tokens else ")"))

    # 누락 검증(감사): 패킹된 파일 수/목록 노출 → 빠진 게 있으면 눈에 띄게
    mf = re.search(r"Total Files:\s*([\d,]+)", out)          # repomix stdout(신뢰가능 카운트)
    n_files = int(mf.group(1).replace(",", "")) if mf else None
    flist = []
    try:
        body = out_path.read_text(encoding="utf-8", errors="replace")
        if style == "markdown":                              # 구조 헤더 '## File:'는 컬럼0(라인번호 없음)
            flist = re.findall(r"(?m)^## File:\s+(.+?)\s*$", body)
    except OSError:
        pass
    cnt = n_files if n_files is not None else len(flist)
    shown = (": " + ", ".join(flist[:10]) + (f" … (+{len(flist) - 10})" if len(flist) > 10 else "")) if flist else ""
    print(f"  📦 패킹 포함 {cnt}개 파일{shown}")
    # 빈/불명 컨텍스트 전송 방지 — 파일수가 0이거나, 신뢰가능 카운트도 목록도 못 얻으면 중단(fail-closed)
    if n_files == 0 or (n_files is None and len(flist) == 0):
        try:
            os.chmod(out_path, 0o600)
        except OSError:
            pass
        reason = "0개" if n_files == 0 else "확인 불가(repomix 파일수 파싱 실패)"
        sys.exit(f"❌ 패킹 파일 수 {reason} — 대상 경로/--include/--ignore를 확인하세요(빈·불명 컨텍스트 전송 방지).")
    if compress:
        print("  ⚠️  위 파일들은 본문이 압축됨(⋮----) — 제어흐름 누락. 리뷰엔 부적합.")
    if tokens and tokens > 120_000:
        print(f"  ⚠️  pack이 큼(~{tokens:,} 토큰) — ChatGPT 웹에서 잘릴(truncation) 수 있다. "
              "--include로 좁히거나 여러 번 나눠 보내라.")
    return out_path, tokens


# ===========================================================================
# 2) 브라우저(CDP) 준비 + fail-closed 검증
# ===========================================================================
def is_port_open(port: int = CDP_PORT) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def cdp_browser_ok() -> bool:
    """포트가 '진짜 CDP 브라우저'인지 /json/version으로 검증(엉뚱한 프로세스 차단)."""
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=4) as r:
            info = json.loads(r.read().decode("utf-8"))
        browser = str(info.get("Browser", ""))
        return any(k in browser for k in ("Chrome", "Chromium", "Comet", "HeadlessChrome", "Edg"))
    except Exception:
        return False


def ensure_browser(browser: str) -> bool:
    if is_port_open():
        if cdp_browser_ok():
            print(f"  ✓ CDP 브라우저 확인 (port {CDP_PORT})")
            return True
        print(f"  ❌ port {CDP_PORT}에 CDP 브라우저가 아닌 다른 프로세스가 떠 있음")
        return False
    path = COMET_PATH if browser == "comet" else CHROME_PATH
    if not Path(path).exists():
        print(f"  ❌ 브라우저 미설치: {path} (env INSANE_REVIEW_{browser.upper()}로 경로 지정 가능)")
        return False
    print(f"  {browser} 시작 중 (CDP {CDP_PORT})...")
    subprocess.Popen([path, f"--remote-debugging-port={CDP_PORT}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for i in range(30):
        if is_port_open() and cdp_browser_ok():
            print(f"  ✓ 시작 완료 ({i + 1}s)")
            time.sleep(2)
            return True
        time.sleep(1)
    print("  ❌ 브라우저 시작 타임아웃")
    return False


def probe_login() -> str:
    """브라우저(CDP) up + playwright 있을 때 ChatGPT 로그인 상태를 best-effort로 확인.
    반환: 'ok' | 'no' | 'unknown'(프로브 불가/오류)."""
    import importlib.util
    if not (is_port_open(CDP_PORT) and cdp_browser_ok()):
        return "unknown"
    if not importlib.util.find_spec("playwright"):
        return "unknown"
    try:
        from playwright.sync_api import sync_playwright as _spw
        with _spw() as pw:
            b = pw.chromium.connect_over_cdp(CDP_URL)
            ctx = pick_context(b)
            if ctx is None:
                return "no"
            page = ctx.new_page()
            try:
                page.goto(CHATGPT_URL, wait_until="load", timeout=30000)
                time.sleep(2)
                return "ok" if looks_logged_in(page) else "no"
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    except Exception:
        return "unknown"


def check_env(do_install: bool = False) -> int:
    """환경 점검 — node/npx, repomix, pyperclip, playwright, CDP 브라우저, ChatGPT 로그인.
    마지막에 'STATUS ...' 라인을 출력해 커맨드(AskUserQuestion 온보딩)가 분기에 파싱한다."""
    import importlib.util
    print("=== insane-review 환경 점검 ===")
    ok, issues = [], []

    npx, node = shutil.which("npx"), shutil.which("node")
    node_ok = bool(node and npx)
    if node_ok:
        ok.append("node/npx 있음")
        ok.append(f"repomix: `npx -y repomix@{REPOMIX_VERSION or 'latest'}`로 자동 설치(사전설치 불필요)")
    else:
        issues.append(("node/npx 없음", "Node.js 설치: https://nodejs.org 또는 `brew install node`"))

    # pip 의존성 — do_install이면 '로그인 프로브 전에' 먼저 설치(설치 후 프로브 가능)
    if do_install:
        for mod, pip in (("pyperclip", "pyperclip"), ("playwright", "playwright")):
            if not importlib.util.find_spec(mod):
                print(f"  [--install] pip install {pip} ...")
                subprocess.run([sys.executable, "-m", "pip", "install", pip])
        importlib.invalidate_caches()

    deps_ok = True
    for mod, pip in (("pyperclip", "pyperclip"), ("playwright", "playwright")):
        if importlib.util.find_spec(mod):
            ok.append(f"python {mod} 있음")
        else:
            issues.append((f"python {mod} 없음", f"pip install {pip} (또는 --install)"))
            deps_ok = False

    if is_port_open(CDP_PORT) and cdp_browser_ok():
        browser_state = "ok"
        ok.append(f"CDP 브라우저({CDP_PORT}) 확인")
    elif is_port_open(CDP_PORT):
        browser_state = "wrong"
        issues.append((f"port {CDP_PORT}이 CDP 브라우저 아님", "다른 프로세스 종료 후 Comet/Chrome을 디버그포트로 실행"))
    else:
        browser_state = "down"
        issues.append((f"브라우저 CDP({CDP_PORT}) 닫힘",
                       f"Comet/Chrome을 --remote-debugging-port={CDP_PORT}로 실행"))

    # ChatGPT 로그인 프로브(브라우저 up + deps 있을 때만)
    login_state = "unknown"
    if browser_state == "ok" and deps_ok:
        login_state = probe_login()
        if login_state == "ok":
            ok.append("ChatGPT 로그인됨 (입력창/모델 어포던스 확인)")
        elif login_state == "no":
            issues.append(("ChatGPT 로그인 안 됨", "해당 브라우저에서 chatgpt.com 로그인 + GPT-5.5 Pro 선택"))

    for o in ok:
        print(f"  ✓ {o}")
    for name, hint in issues:
        print(f"  ✗ {name}\n      → {hint}")

    # 머신 파싱용 상태 라인 — 커맨드 온보딩이 어느 단계가 막혔는지 분기에 사용
    print(f"\nSTATUS node={'ok' if node_ok else 'missing'} deps={'ok' if deps_ok else 'missing'} "
          f"browser={browser_state} login={login_state}")
    print(f"결과: {len(ok)} OK / {len(issues)} 부족" + ("  — 전부 준비됨 ✅" if not issues else "  ⚠️"))
    return len(issues)


# ===========================================================================
# 3) ChatGPT 상호작용 프리미티브
# ===========================================================================
def find_input(page):
    for sel in INPUT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                return el
        except Exception:
            continue
    return None


def count_msgs(page, selector: str) -> int:
    try:
        return len(page.query_selector_all(selector))
    except Exception:
        return 0


def count_msgs_strict(page, selector: str) -> int:
    """기준개수 포착 전용 — 조회 실패를 0으로 숨기지 않는다. 재시도 후에도 실패하면 예외(fail-closed).
    base_* 가 조회실패로 0이 되면 기존 DOM이 '새 턴'으로 오인돼 이전 답변을 저장할 수 있으므로 이를 차단한다."""
    last_exc = None
    for _ in range(3):
        try:
            return len(page.query_selector_all(selector))
        except Exception as exc:
            last_exc = exc
            time.sleep(0.3)
    raise RuntimeError(f"기준 메시지 수 조회 실패({selector}): {str(last_exc)[:60]} → 전송 중단(fail-closed)")


def is_streaming(page) -> bool:
    try:
        return page.query_selector(STREAMING_BTN) is not None
    except Exception:
        return False


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", text).strip() if text else ""


def last_assistant_node(page):
    nodes = page.query_selector_all(ASSISTANT_MSG_SELECTOR)
    return nodes[-1] if nodes else None


def last_assistant_text(page) -> str:
    node = last_assistant_node(page)
    if node:
        try:
            return node.inner_text() or ""
        except Exception:
            return ""
    return ""


def last_turn_complete(page, base_assistant: int = 0, base_copy: int = 0) -> bool:
    """마지막 assistant 턴이 '완료'됐다는 강한 신호: stop-button 사라짐 + 새 copy 버튼 등장.
    base_assistant/base_copy: 전송 전 개수 — assistant 노드와 copy 버튼이 모두 늘었을 때만 새 턴 완료로 인정."""
    if is_streaming(page):
        return False
    try:
        # 전송 전보다 assistant 노드·copy 버튼이 늘지 않았으면 '이전 응답'이므로 완료로 보지 않음(fail-closed)
        if count_msgs(page, ASSISTANT_MSG_SELECTOR) <= base_assistant:
            return False
        return len(page.query_selector_all(COPY_BTN)) > base_copy
    except Exception:
        return False


def copy_last_turn(page, base_copy: int = 0) -> str | None:
    """새 턴의 copy 버튼을 눌러 클립보드로 회수(파이프 안전 검증 포함).
    base_copy: 전송 전 copy 버튼 수 — 그보다 늘었을 때만(=새 응답 버튼) 회수해 이전 응답 오인을 막는다."""
    if pyperclip is None:
        return None
    try:
        btns = page.query_selector_all(COPY_BTN)
        if len(btns) <= base_copy:   # 새 copy 버튼이 아직 없음 → 이전 응답 회수 방지(fail-closed)
            return None
        btn = btns[-1]  # 증가가 확인됐으므로 마지막이 새 응답의 copy 버튼
        for _ in range(3):
            pyperclip.copy("__INSANE_REVIEW_SENTINEL__")
            btn.click(force=True)
            time.sleep(1)
            txt = pyperclip.paste()
            # sentinel이 그대로면 복사 실패 → stale 반환 방지
            if txt and txt != "__INSANE_REVIEW_SENTINEL__" and len(txt) > 10:
                return txt
            time.sleep(0.5)
        return None
    except Exception:
        return None


# ---- 모델 스위처 ----
MODEL_SWITCHER_SELECTORS = [
    'button.__composer-pill[aria-haspopup="menu"]',   # 실측: 모델/추론 pill
    'button[data-testid="model-switcher-dropdown-button"]',
    'button[aria-label*="model" i]',
]
# 실측: pill 클릭 → menuitemradio(즉시/중간/높음/매우 높음/Pro=추론단계) + menuitem("GPT-5.5"=모델명)
EFFORT_ITEM_SELECTORS = ['[role="menuitemradio"]', '[role="menuitem"]', '[role="option"]']


def read_model_pills(page) -> list[str]:
    out = []
    for el in page.query_selector_all('button.__composer-pill'):
        try:
            t = (el.inner_text() or "").strip()
            if t:
                out.append(t)
        except Exception:
            continue
    return out


def _open_switcher(page):
    for sel in MODEL_SWITCHER_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                el.click()
                time.sleep(1.2)
                return True
        except Exception:
            continue
    return False


def read_menu_state(page) -> dict:
    """열린 메뉴에서 모델명(menuitem 중 checked/selected) + 체크된 추론단계(menuitemradio aria-checked)를 읽는다."""
    state = {"model": None, "model_source": None, "models": [], "effort_checked": None, "items": []}
    try:
        # 한 번 순회하며 (1) 모델같은 항목 전부 수집, (2) aria-checked/selected된 활성 모델 검출
        for it in page.query_selector_all('[role="menuitem"], [role="menuitemradio"], [role="option"]'):
            is_checked = it.get_attribute("aria-checked") == "true" or it.get_attribute("aria-selected") == "true"
            t = (it.inner_text() or "").strip()
            if t and re.search(r"GPT|gpt|o\d|Claude|Gemini", t):
                name = t.splitlines()[0][:40]
                if name not in state["models"]:
                    state["models"].append(name)
                if is_checked and not state["model"]:
                    state["model"] = name
                    state["model_source"] = "checked"
        # 활성표시(aria-checked)를 못 찾았을 때만 첫 모델명 폴백 — 출처를 'fallback'으로 표기(검증 시 모호하면 거부)
        if not state["model"] and state["models"]:
            state["model"] = state["models"][0]
            state["model_source"] = "fallback"
    except Exception:
        pass
    try:
        for it in page.query_selector_all('[role="menuitemradio"]'):
            t = (it.inner_text() or "").strip()
            state["items"].append(t)
            if it.get_attribute("aria-checked") == "true":
                state["effort_checked"] = t
    except Exception:
        pass
    return state


def select_model(page, want: str, require_model: str | None = None) -> tuple[bool, str | None]:
    """모델 스위처를 열고 want(추론단계, 예: 'pro')를 선택 + 검증.
    require_model 지정 시 모델명(예: 'GPT-5.5')이 일치하지 않으면 False(실패) 반환.
    반환: (verified, verified_model_name)"""
    want_l = want.lower()
    if not _open_switcher(page):
        print("  ⚠️  모델 스위처를 못 찾음 → 기본 모델로 진행")
        return False, None

    before = read_menu_state(page)
    if before["model"]:
        print(f"  메뉴 모델명: {before['model']!r} / 추론단계 목록: {before['items']}")

    # require_model 검증 (모델명을 읽지 못했거나 모델명이 기대값과 다르면 즉시 중단)
    if require_model:
        if not before["model"]:
            print(f"  ❌ 모델명 획득 실패 (require_model '{require_model}' 검증 불가) → 즉시 중단 (fail-closed)")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False, None
        if require_model.lower() not in before["model"].lower():
            print(f"  ❌ 모델 불일치: 기대 '{require_model}' ≠ 메뉴 '{before['model']}' → 중단(전송 안 함)")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False, None

    # 추론단계 클릭 대상 탐색
    clicked = None
    cands = []
    for sel in EFFORT_ITEM_SELECTORS:
        try:
            cands.extend(page.query_selector_all(sel))
        except Exception:
            continue

    for exact in (True, False):
        for it in cands:
            try:
                t = (it.inner_text() or "").strip()
                low = t.lower()
                if (exact and low == want_l) or (not exact and want_l in low):
                    it.click()
                    clicked = t.splitlines()[0][:40]
                    time.sleep(1.5)  # 클릭 후 드롭다운이 닫히는 시간 대기
                    break
            except Exception:
                continue
        if clicked:
            break

    if not clicked:
        print(f"  ⚠️  '{want}' 추론단계 항목 못 찾음 → 기본값")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False, None

    # Pro 제안: 메뉴 재오픈하여 effort_checked 및 model_checked 상태 검증
    if not _open_switcher(page):
        print("  ⚠️  선택 상태 검증을 위해 메뉴 재오픈 실패")
        return False, None

    after = read_menu_state(page)
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    time.sleep(0.5)

    model_verified = True
    if require_model:
        name_ok = after["model"] is not None and require_model.lower() in after["model"].lower()
        # 폴백(활성표시 없음)으로 잡은 모델명은 메뉴에 모델이 여러 개일 때 신뢰 불가 → fail-closed.
        # 활성표시(checked)거나 메뉴에 모델이 하나뿐이면 폴백이라도 안전(= 활성 모델).
        src_ok = (after.get("model_source") == "checked") or (len(after.get("models") or []) <= 1)
        model_verified = name_ok and src_ok
        if name_ok and not src_ok:
            print(f"  ❌ 활성 모델 확정 불가(체크표시 없음 + 메뉴에 모델 {len(after['models'])}개: {after['models']}) → fail-closed")

    effort_verified = after["effort_checked"] is not None and want_l in after["effort_checked"].lower()
    verified = model_verified and effort_verified

    verified_model = after["model"] or "Unknown Model"
    verified_effort = after["effort_checked"] or "Default"
    verified_model_name = f"{verified_model} ({verified_effort})"

    print(f"  {'✓' if verified else '⚠️'} 최종 모델 검증: model={after['model']} (기대:{require_model}), effort={after['effort_checked']} (기대:{want}) -> 결과={'OK' if verified else '실패'}")
    return verified, verified_model_name


# ---- 첨부 / 입력 / 전송 ----
def attach_file(page, path: Path) -> bool:
    """파일 첨부 후 '파일명이 실제로 첨부 영역에 떴는지' 검증."""
    try:
        inp = page.query_selector(FILE_INPUT_SELECTOR)
        if not inp:
            print("  ⚠️  파일 입력 요소를 못 찾음 → 붙여넣기 폴백")
            return False
        inp.set_input_files(str(path))
        print(f"  파일 첨부 시도: {path.name} (업로드 대기...)")
        stem = path.stem[:14]  # 칩 라벨은 잘릴 수 있어 앞부분만 매칭
        
        # composer 내부 영역(form 또는 textarea의 presentation 부모)으로 locator 한정
        # ChatGPT UI에서 파일 첨부 칩이 노출되는 영역
        composer = page.locator("form:has(#prompt-textarea), [role='presentation']:has(#prompt-textarea)").first
        
        for _ in range(40):
            time.sleep(1)
            try:
                # composer 내부에서만 stem 텍스트를 갖는 칩(요소) 검색
                chip = composer.get_by_text(stem, exact=False)
                if chip.count() > 0:
                    print("  ✓ 첨부 확인됨 (composer 내 파일명 노출)")
                    time.sleep(1.5)
                    return True
            except Exception:
                pass
        print("  ❌ 첨부 칩(파일명) 확인 실패 — fail-closed (잘못된 컨텍스트 전송 방지)")
        return False
    except Exception as exc:
        print(f"  ❌ 첨부 실패({str(exc)[:60]})")
        return False


SEND_BTN_SELECTORS = [
    'button[data-testid="send-button"]',
    'button[data-testid="composer-send-button"]',
    'button[aria-label*="send" i]',
    'button[aria-label*="보내기" i]',
    'button[aria-label*="프롬프트 보내기" i]',
]


def put_text(page, message: str):
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(0.3)
    page.evaluate(
        """() => { const el = document.querySelector('#prompt-textarea')
            || document.querySelector('div[contenteditable=\\"true\\"]');
            if (el) { el.scrollIntoView({block:'center'}); el.focus(); } }"""
    )
    time.sleep(0.3)
    if pyperclip is not None:
        pyperclip.copy(message)
        time.sleep(0.2)
        page.keyboard.press("Meta+v")
    else:
        page.keyboard.type(message)
    time.sleep(0.6)


def read_composer_text(page) -> str:
    """입력창(composer)에 현재 들어있는 텍스트를 읽는다(전송 전 프롬프트 입력 검증용)."""
    try:
        return page.evaluate(
            """() => { const el = document.querySelector('#prompt-textarea')
                || document.querySelector('div[contenteditable=\\"true\\"]');
                return el ? (el.innerText || el.textContent || '') : ''; }"""
        ) or ""
    except Exception:
        return ""


def composer_has_prompt(page, prompt: str) -> bool:
    """프롬프트 '전체'가 composer에 들어갔는지 검증(앞 24자 가드가 아니라 동일성).
    잘림(want⊄got)·중복/오염(got가 과도하게 김) 모두 fail-closed로 거부 → '첨부만/잘린 질문' 전송 차단."""
    want = normalize(prompt)
    if not want:
        return True
    got = normalize(read_composer_text(page))
    if want not in got:                  # 일부만 입력(잘림) → 거부
        return False
    if got.count(want) > 1:              # 프롬프트가 통째로 2번 이상(중복 입력) → 거부(길이 무관)
        return False
    if len(got) > len(want) * 1.5 + 20:  # 그 외 오염 payload → 거부
        return False
    return True


def clear_composer(page):
    """재입력 전 composer를 비운다(중복 입력 방지)."""
    try:
        page.evaluate(
            """() => { const el = document.querySelector('#prompt-textarea')
                || document.querySelector('div[contenteditable=\\"true\\"]');
                if (el) { el.focus(); } }"""
        )
        page.keyboard.press("Meta+a")
        page.keyboard.press("Backspace")
        time.sleep(0.2)
    except Exception:
        pass


def click_send(page) -> bool:
    """전송 버튼이 visible·enabled 될 때까지 폴링 후 클릭(첨부 처리 시간 대비). 끝까지 안 되면 Enter."""
    for _ in range(15):  # 최대 ~15s 대기
        for sel in SEND_BTN_SELECTORS:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible() and btn.is_enabled():
                    btn.click()
                    print("  ✓ 전송 버튼 클릭")
                    time.sleep(1)
                    return True
            except Exception:
                continue
        time.sleep(1)
    print("  ⚠️  전송 버튼이 enabled 안 됨 → Enter 폴백")
    page.keyboard.press("Enter")
    time.sleep(1)
    return False


def click_answer_now(page) -> bool:
    """리즈닝 중 '지금 답변 받기'를 눌러 강제 답변.
    실측: 버튼은 리즈닝 flyout 최상단(우측 패널). 패널이 아래로 스크롤되면 버튼이 밀려나므로
    스크롤 컨테이너를 top으로 올린 뒤 scroll_into_view 후 클릭한다.
    칩 매칭은 '생각 중'으로 좁힌다 — 프롬프트 본문의 '추론' 등과 오매칭 방지."""
    answer_pats = [("지금 답변 받기", True), ("지금 답변받기", True),
                   ("답변 받기", False), ("Get answer", False), ("answer now", False)]
    chip_re = re.compile(r"생각\s*중|Thinking", re.I)

    def scroll_panels_top():
        try:
            page.evaluate("() => { for (const el of document.querySelectorAll('*')) "
                          "{ if (el.scrollHeight > el.clientHeight + 20) el.scrollTop = 0; } }")
        except Exception:
            pass

    def try_answer() -> bool:
        scroll_panels_top()
        for txt, exact in answer_pats:
            try:
                loc = page.get_by_text(txt, exact=exact)
                if loc.count() > 0:
                    try:
                        loc.first.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    loc.first.click(timeout=2500)
                    return True
            except Exception:
                continue
        return False

    if try_answer():
        return True
    # 리즈닝 칩(좁은 매칭)을 눌러 패널을 연 뒤 재시도
    try:
        chip = page.get_by_text(chip_re)
        if chip.count() > 0:
            chip.first.click(timeout=2500)
            time.sleep(1.2)
    except Exception:
        pass
    return try_answer()


def wait_for_turn_response(page, force_after=None, max_wait=None,
                           base_user: int = 0, base_assistant: int = 0, base_copy: int = 0) -> tuple[str, str]:
    """새 user 턴(전송 전 기준개수 대비 증가) 기준 응답 회수.
    base_user/base_assistant: 전송 직전의 메시지 수 — 이전 응답을 성공으로 오인하지 않도록 결속.
    반환: (status, text) — status ∈ {'ok','timeout','not_sent'}."""
    mw = max_wait if max_wait else MAX_WAIT_SECS
    start = time.monotonic()
    last_status = 0
    force_tries = 0

    # 1) 우리 user 턴이 '새로' 떴는지 확인(전송 전 기준보다 증가). 안 떴으면 not_sent → 호출자가 재전송
    sent = False
    while time.monotonic() - start < 25:
        if count_msgs(page, USER_MSG_SELECTOR) > base_user:
            sent = True
            break
        time.sleep(1)
    if not sent:
        return ("not_sent", "")

    # 2) assistant 턴 완료까지 대기 (stop-button 사라짐 + copy 버튼 + 텍스트 안정)
    print(f"    응답 대기 중... (최대 {mw}s"
          + (f", {force_after}s 후 '지금 답변 받기' 재시도" if force_after else "") + ")")
    stable_since = None
    last_text = ""
    while time.monotonic() - start < mw:
        elapsed = int(time.monotonic() - start)

        # force-answer: 성공할 때까지 매 틱 재시도(상한). 실패해도 latch 안 함.
        if force_after and elapsed >= force_after and force_tries < FORCE_MAX_TRIES and is_streaming(page):
            if click_answer_now(page):
                print(f"    ⚡ {elapsed}s — '지금 답변 받기' 클릭(리즈닝 강제 종료)")
                force_tries = FORCE_MAX_TRIES  # 성공 → 그만
            else:
                force_tries += 1
                if force_tries >= FORCE_MAX_TRIES:
                    print(f"    ⚠️  {elapsed}s — '지금 답변 받기' 버튼 {FORCE_MAX_TRIES}회 실패 → 자연완료 대기")

        if elapsed - last_status >= STATUS_INTERVAL and elapsed > 0:
            st = "⏳ 생성중" if is_streaming(page) else "정지(확인중)"
            print(f"    {elapsed}s | {st}")
            last_status = elapsed

        if elapsed < MIN_WAIT_SECS or is_streaming(page):
            stable_since = None
            time.sleep(2)
            continue

        # 완료 신호 + 텍스트 안정성 (새 assistant 턴이 실제로 생겼을 때만 완료로 인정)
        cur = last_assistant_text(page)
        if not last_turn_complete(page, base_assistant=base_assistant, base_copy=base_copy) or len(cur) < 30:
            stable_since = None
            time.sleep(2)
            continue
        if normalize(cur) != normalize(last_text):
            last_text = cur
            stable_since = time.monotonic()
            time.sleep(2)
            continue
        if stable_since and (time.monotonic() - stable_since) >= STABLE_CHECK_SECS:
            # 회수: copy 우선, 실패 시 DOM
            txt = copy_last_turn(page, base_copy=base_copy)
            if txt and len(txt) > 30:
                print(f"    ✅ 응답 수신: {len(txt)}자 ({int(time.monotonic()-start)}s, copy)")
                return ("ok", txt)
            if cur and len(cur) > 30:
                print(f"    ✅ 응답 수신: {len(cur)}자 ({int(time.monotonic()-start)}s, DOM)")
                return ("ok", cur)
        time.sleep(2)

    fallback = last_assistant_text(page)
    return ("timeout", fallback) if fallback else ("timeout", "")


# ===========================================================================
# 4) 로그인된 context 선택 (fail-closed)
# ===========================================================================
def pick_context(browser):
    """인증 세션 쿠키(__Secure-next-auth*)가 있는 context를 1순위로. 그다음 chatgpt.com 쿠키 보유,
    끝으로 contexts[0]. context 자체가 없으면 None. (최종 로그인 판정은 looks_logged_in이 fail-closed로 한 번 더.)"""
    if not browser.contexts:
        return None
    # 1순위: 진짜 인증 쿠키(아무 쿠키나 X — 익명 분석쿠키로 오인 방지)
    for ctx in browser.contexts:
        try:
            cookies = ctx.cookies("https://chatgpt.com")
            if any(str(c.get("name", "")).startswith("__Secure-next-auth") for c in cookies):
                return ctx
        except Exception:
            continue
    # 2순위: chatgpt.com 쿠키가 하나라도 있는 context
    for ctx in browser.contexts:
        try:
            if ctx.cookies("https://chatgpt.com"):
                return ctx
        except Exception:
            continue
    return browser.contexts[0]


def looks_logged_in(page) -> bool:
    # 음성 신호: 입력창 존재 + 로그인 벽 부재
    if find_input(page) is None:
        return False
    for sel in LOGIN_WALL_SELECTORS:
        try:
            if page.query_selector(sel):
                return False
        except Exception:
            continue
    # 양성 신호: 인증된 세션에서만 렌더되는 composer 어포던스(모델 pill 또는 파일첨부 input)를 적극 확인.
    # 렌더 지연 대비 ~3s 폴링. 끝까지 없으면 fail-closed(인증 증명 실패로 간주).
    for _ in range(6):
        try:
            if page.query_selector('button.__composer-pill') or page.query_selector(FILE_INPUT_SELECTOR):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ===========================================================================
# main
# ===========================================================================
def main():
    ap = argparse.ArgumentParser(description="repomix → 구독 ChatGPT(GPT-5.5 Pro) 분석")
    ap.add_argument("--target", default=None, help="분석 대상 폴더(생략 시 프롬프트만 = 의견 모드)")
    ap.add_argument("--include", default=None, help='repomix --include 글롭')
    ap.add_argument("--ignore", default=None, help="repomix --ignore 글롭")
    ap.add_argument("--compress", action="store_true",
                    help="tree-sitter 골격만(토큰 절감) — 본문 제거되니 정확성 리뷰엔 쓰지 마라")
    ap.add_argument("--no-line-numbers", action="store_true",
                    help="라인번호 prefix 끄기(기본 on — AI가 파일:라인 인용하도록)")
    ap.add_argument("--style", default="markdown", choices=["xml", "markdown", "plain"])
    ap.add_argument("--token-budget", type=int, default=None)
    ap.add_argument("--attach", action="store_true", help="첨부 강제(폴백 붙여넣기 비활성)")
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--prompt-file", default=None)
    ap.add_argument("--model", default=None, help='추론단계 선택(예: "pro")')
    ap.add_argument("--require-model", default=None,
                    help='모델명 검증(예: "GPT-5.5") — 불일치 시 전송 중단')
    ap.add_argument("--force-answer-after", type=int, default=None,
                    help="N초 후 리즈닝 중이면 '지금 답변 받기' 재시도")
    ap.add_argument("--max-wait", type=int, default=None,
                    help=f"응답 최대 대기 초(기본 {MAX_WAIT_SECS}=20분; env INSANE_REVIEW_MAX_WAIT로도 설정)")
    ap.add_argument("--browser", default="comet", choices=["comet", "chrome"])
    ap.add_argument("--pack-only", action="store_true")
    ap.add_argument("--keep-pack", action="store_true", help="전송 후 패킹 파일 보존(기본은 유지; 끄려면 --delete-pack)")
    ap.add_argument("--delete-pack", action="store_true", help="응답 회수 후 패킹 파일 삭제(시크릿 위생)")
    ap.add_argument("--out-dir", default=None,
                    help="출력 저장 폴더(기본: 현재 프로젝트의 .insane-review/; env INSANE_REVIEW_OUT)")
    ap.add_argument("--check-env", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--council", action="store_true",
                    help="agent-council 멤버 모드: 로그는 stderr, 응답만 stdout")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("prompt_args", nargs="*", help="프롬프트(위치인자 — council 호환)")
    args = ap.parse_args()

    if args.check_env:
        sys.exit(check_env(do_install=args.install))

    # --require-model은 모델 검증 경로(select_model)에서만 효력 → --model 없이 단독 사용 시 검증이 통째로
    # 스킵되는 fail-open을 차단(fail-closed). 모델/추론단계를 함께 지정해야 검증이 돈다.
    if args.require_model and not args.model:
        sys.exit('❌ --require-model은 --model과 함께 써야 합니다(모델/추론단계를 선택·검증하는 경로).\n'
                 '     예: --model pro --require-model "GPT-5.5"')

    real_stdout = sys.stdout
    if args.council:
        sys.stdout = sys.stderr

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"  출력 폴더: {out_dir}")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_tag = f"{ts}_{os.getpid()}_{uuid.uuid4().hex[:6]}"  # 동시 실행 충돌 방지
    pack_path = None
    tokens = None
    label = "prompt"
    verified_model_name = None

    if args.target:
        target = Path(args.target).resolve()
        if not target.exists():
            sys.exit(f"❌ 대상 폴더 없음: {target}")
        label = re.sub(r"[^A-Za-z0-9_.-]", "-", target.name)
        ext = {"xml": "xml", "markdown": "md", "plain": "txt"}[args.style]
        pack_path = out_dir / f"pack_{label}_{run_tag}.{ext}"
        # 출력 폴더가 대상 안이면 이전 산출물(pack_*/response_*)이 다음 pack에 섞이는 self-inclusion 차단
        eff_ignore = args.ignore
        try:
            rel = out_dir.resolve().relative_to(target)
            rel_glob = f"{rel.as_posix()}/**"
            eff_ignore = f"{eff_ignore},{rel_glob}" if eff_ignore else rel_glob
            print(f"  ↳ 출력 폴더가 대상 내부 → ignore 자동 추가: {rel_glob}")
        except ValueError:
            pass  # 대상 밖 → self-inclusion 없음
        print(f"\n[1/3] repomix 패킹 — {label}")
        pack_path, tokens = pack_repo(
            target, include=args.include, ignore=eff_ignore, compress=args.compress,
            style=args.style, token_budget=args.token_budget, out_path=pack_path,
            line_numbers=not args.no_line_numbers)
        if args.pack_only:
            print(f"\n[pack-only] 산출물: {pack_path}")
            return
    else:
        if args.pack_only:
            sys.exit("❌ --pack-only는 --target이 필요합니다.")
        print("\n[프롬프트-only] 레포 없이 질문만 전송")

    if sync_playwright is None:
        sys.exit("❌ playwright 미설치. pip install playwright")
    if pyperclip is None:
        print("⚠️  pyperclip 미설치 — 붙여넣기/복사회수 신뢰도 하락")

    positional = " ".join(args.prompt_args).strip() if args.prompt_args else ""
    prompt = (args.prompt or positional
              or (Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else None)
              or DEFAULT_PROMPT)

    print(f"\n[2/3] 브라우저 준비 ({args.browser})")
    if not ensure_browser(args.browser):
        sys.exit(1)

    print("\n[3/3] ChatGPT 투입 & 응답 회수")
    response = ""
    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            print(f"  ↻ 재시도 {attempt - 1}/{args.retries} ...")
            time.sleep(3)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(CDP_URL)
                ctx = pick_context(browser)
                if ctx is None:
                    raise RuntimeError("브라우저 context 없음 (로그인된 Comet/Chrome 필요)")
                page = ctx.new_page()
                try:
                    page.goto(CHATGPT_URL, wait_until="load", timeout=60000)
                    time.sleep(3)
                    for _ in range(10):
                        if find_input(page):
                            break
                        time.sleep(1)
                    if not looks_logged_in(page):
                        raise RuntimeError("ChatGPT 로그인 안 됨/입력창 없음 — 해당 브라우저에서 chatgpt.com 로그인 확인")

                    print(f"  현재 pill: {read_model_pills(page)}")
                    if args.model:
                        print(f"  모델/추론단계 선택: '{args.model}'"
                               + (f" (모델명 검증='{args.require_model}')" if args.require_model else ""))
                        verified, v_name = select_model(page, args.model, require_model=args.require_model)
                        if not verified:
                            raise RuntimeError(f"모델/추론단계 검증 실패 (model={args.model}, require={args.require_model}) — 전송 중단")
                        verified_model_name = v_name

                    # 본문은 '첨부'로 — 확인 안 되면 fail-closed (잘못된 컨텍스트로 리뷰 방지)
                    if pack_path is not None:
                        if not attach_file(page, pack_path):
                            raise RuntimeError("코드 첨부 확인 실패 → 중단(fail-closed)")

                    # 전송 직전 기준개수 포착(턴-스코프 결속 — 이전 응답을 성공으로 오인 방지).
                    # 조회 실패를 0으로 숨기면 기존 DOM이 '새 턴'으로 오인되므로 fail-closed 카운터 사용.
                    base_user = count_msgs_strict(page, USER_MSG_SELECTOR)
                    base_assistant = count_msgs_strict(page, ASSISTANT_MSG_SELECTOR)
                    base_copy = count_msgs_strict(page, COPY_BTN)

                    put_text(page, prompt)
                    # 프롬프트 '전체'가 입력창에 들어갔는지 검증 — 아니면 composer 비우고 1회 재입력, 그래도 불일치면 중단
                    # (첨부만/잘린 질문이 전송되어 '오염된 응답'을 성공저장하는 fail-open 차단)
                    if not composer_has_prompt(page, prompt):
                        clear_composer(page)
                        put_text(page, prompt)
                        if not composer_has_prompt(page, prompt):
                            raise RuntimeError("프롬프트가 입력창에 온전히 안 들어감 → 중단(첨부만/잘린 전송 방지, fail-closed)")
                    click_send(page)
                    status, text = wait_for_turn_response(page, force_after=args.force_answer_after,
                                                          max_wait=args.max_wait,
                                                          base_user=base_user, base_assistant=base_assistant,
                                                          base_copy=base_copy)
                    if status == "not_sent":
                        print("  ⚠️  user 턴 미생성(전송 안 됨) → 재시도")
                        continue
                    if status == "timeout":
                        print("  ⚠️  타임아웃 — 미완성 응답은 성공저장 안 함(fail-closed) → 재시도")
                        continue
                    if status == "ok" and text and len(text.strip()) >= 40:
                        response = text
                    else:
                        print(f"  ⚠️  응답 비었거나 너무 짧음(status={status}) → 재시도")
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
            if response:
                break
            print(f"  ⚠️  시도 {attempt}: 응답 비어있음")
        except Exception as exc:
            print(f"  ⚠️  시도 {attempt} 실패: {str(exc)[:160]}")

    if not response:
        sys.exit("❌ 응답 회수 실패 (모든 재시도 소진)")

    # 패킹 파일 시크릿 위생: --delete-pack이면 삭제
    if pack_path is not None and args.delete_pack:
        try:
            pack_path.unlink()
            print(f"  🔒 패킹 파일 삭제됨(--delete-pack)")
        except OSError:
            pass

    resp_path = out_dir / f"response_{label}_{run_tag}.md"
    pack_line = (f"- 패킹: `{pack_path.name}`" + (f" (~{tokens:,} tokens)\n" if tokens else "\n")
                 if pack_path is not None else "- 패킹: (없음 / 프롬프트-only)\n")
    model_line = f"- 모델: `{verified_model_name}`\n" if verified_model_name else ""
    body = (f"# {label} — GPT 응답 (구독 ChatGPT)\n\n" + pack_line + model_line
            + f"- 프롬프트: {prompt[:80]}...\n\n---\n\n{response}\n")
    tmp = resp_path.with_suffix(".md.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, resp_path)  # 원자적 저장
    print(f"\n[완료] 응답 저장: {resp_path}")
    if args.council:
        real_stdout.write(response + "\n")
        real_stdout.flush()
    else:
        print("─" * 50)
        print(response[:800] + ("\n...(생략)" if len(response) > 800 else ""))


if __name__ == "__main__":
    main()
