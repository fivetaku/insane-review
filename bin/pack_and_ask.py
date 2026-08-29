#!/usr/bin/env python3
"""
insane-review — repomix 패킹 → 구독 ChatGPT(웹) GPT Pro(최신 플래그십) 투입 → 분석 회수 (API 비용 0)

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
from contextlib import contextmanager
import hashlib
import json
import os
import platform
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
# 전용(격리) 프로필 — 사용자 주 브라우저 세션과 분리. Chrome 136+는 '기본 프로필'에서
# --remote-debugging-port를 정책적으로 무시하므로(쿠키 탈취 방지), 이 별도 user-data-dir이
# 없으면 디버그 포트가 아예 안 열린다. 모든 OS 공통으로 전용 프로필을 쓴다.
BROWSER_PROFILE_DIR = Path(os.environ.get(
    "INSANE_REVIEW_PROFILE", str(Path.home() / ".insane-review" / "browser-profile")))
# 선택한 브라우저를 영속화(재질문 방지) — 우선순위: --browser > env > config 저장값 > 첫 감지.
CONFIG_PATH = Path(os.environ.get(
    "INSANE_REVIEW_CONFIG", str(Path.home() / ".insane-review" / "config.json")))
# repomix 버전 핀(재현성·공급망) — env로 갱신. 빈 문자열이면 latest.
REPOMIX_VERSION = os.environ.get("INSANE_REVIEW_REPOMIX_VERSION", "1.15.0")
REPOMIX_TIMEOUT = int(os.environ.get("INSANE_REVIEW_REPOMIX_TIMEOUT", "300"))

CHATGPT_URL = "https://chatgpt.com/"


def _guard_dialogs(ctx, page=None):
    """Stop playwright's default dialog auto-dismiss from racing over CDP.

    Over connect_over_cdp, any JS dialog (beforeunload/alert/confirm) on the
    ChatGPT page triggers playwright's built-in auto-dismiss. Across CDP that
    races the browser → `ProtocolError: No dialog is showing`, an UNCAUGHT
    driver exception that crashes the run (100% CPU spin) before the prompt is
    ever submitted. Registering our own handler disables the default and
    swallows the race.
    """
    def _on_dialog(d):
        try:
            d.dismiss()
        except Exception:
            pass
    def _attach(p):
        try:
            p.on("dialog", _on_dialog)
        except Exception:
            pass
    try:
        for p in (getattr(ctx, "pages", None) or []):
            _attach(p)
        ctx.on("page", _attach)   # cover future tabs/pages too
    except Exception:
        pass
    if page is not None:
        _attach(page)


INPUT_SELECTORS = ["#prompt-textarea", 'div[contenteditable="true"]']
FILE_INPUT_SELECTOR = 'input[type="file"]'
# 폴백 리스트(첫 항목=현행 실측 셀렉터, 이후=구조적 폴백) — INPUT_SELECTORS와 같은 컨벤션
COPY_BTN_SELECTORS = [
    'button[data-testid="copy-turn-action-button"]',
    'button[aria-label="Copy"]',
    'button[data-testid*="copy"]',
]
STREAMING_BTN_SELECTORS = [
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop streaming"]',
    'button[data-testid*="stop"]',
]
USER_MSG_SELECTORS = ['[data-message-author-role="user"]', 'section[data-turn="user"]', 'article[data-turn="user"]']
ASSISTANT_MSG_SELECTORS = ['[data-message-author-role="assistant"]', 'section[data-turn="assistant"]', 'article[data-turn="assistant"]']
# 턴 컨테이너(실측 2026-08-25: section[data-turn]) — copy 툴바는 메시지 div 바깥, 이 컨테이너 안에 있다
TURN_CONTAINER_SELECTOR = 'section[data-turn], article[data-turn], [data-turn]'

# 사용량 한도(쿼터) 차단 배너 감지 문구 — dialog/alert 표면에서만 대조(오탐 방지). 자유롭게 추가.
QUOTA_HINTS = [
    "usage limit", "reached your limit", "limit reached", "you've hit",
    "reached the current usage cap", "try again later", "upgrade to",
    "사용량 한도", "한도에 도달", "사용 한도", "요금제를 업그레이드",
]


def _q(page, selectors):
    """폴백 리스트에서 첫 매치 노드(없으면 None)."""
    for sel in selectors:
        try:
            node = page.query_selector(sel)
        except Exception:
            continue
        if node is not None:
            return node
    return None


def _qa(page, selectors):
    """폴백 리스트에서 첫 비어있지 않은 query_selector_all 결과(없으면 [])."""
    for sel in selectors:
        try:
            nodes = page.query_selector_all(sel)
        except Exception:
            continue
        if nodes:
            return nodes
    return []


def detect_quota_block(page):
    """쿼터/한도 차단 감지(보수적 — role=dialog/alert 표면만 스캔, 본문 응답 텍스트는 안 봄).
    매칭된 문구를 반환, 없으면 None. 실패는 조용히 None(대기 루프를 깨지 않음)."""
    try:
        for sel in ('[role="dialog"]', '[role="alert"]'):
            for node in page.query_selector_all(sel):
                txt = (node.inner_text() or "").strip()
                if not txt:
                    continue
                low = txt.lower()
                for hint in QUOTA_HINTS:
                    if hint.lower() in low:
                        return txt[:200]
    except Exception:
        return None
    return None
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
STALL_RELOAD_SECS = int(os.environ.get("INSANE_REVIEW_STALL_RELOAD", "45"))  # 빈 턴·스트리밍 없음 지속 시 재로드까지
STALL_MAX_RELOADS = 3
# '지금 답변 받기' 버튼(cot v5 UI, 실측 2026-07-19): 본문 리즈닝 고정행 안의 button.
ANSWER_NOW_ROW_SELECTOR = 'div[data-testid="cot-v5-pinned-row"]'
ANSWER_NOW_TEXT_RE = re.compile(r"답변\s*받기|Get answer|answer now", re.I)
# 최대 대기 소진 시 마지막 수단으로 '지금 답변 받기'를 누른 뒤 답변 플러시를 기다리는 추가 유예.
FORCE_TIMEOUT_GRACE_SECS = int(os.environ.get("INSANE_REVIEW_FORCE_GRACE", "240"))

# --- v0.6.0 identity 결속 ---
# 전송이 만든 '대화 URL'(/c/<id>)에 회수를 결속한다. count 델타는 페이지가 다른 채팅을
# 보여주는 순간 무너진다(2026-07-18 스테일 캡처 실측) — URL 결속이 1차 방어, id-diff가 2차.
CONV_URL_RE = re.compile(r"/c/[0-9a-f]{8}[0-9a-f-]{4,}", re.I)
CONV_URL_CAPTURE_SECS = int(os.environ.get("INSANE_REVIEW_URL_CAPTURE_SECS", "90"))
# Pro 추론단계는 20~60분이 정상 범위(실측) — Pro 선택·검증 시 기본 최대 대기를 자동 상향.
# 사용자가 --max-wait 또는 INSANE_REVIEW_MAX_WAIT를 명시하면 그 값이 우선.
PRO_MAX_WAIT_SECS = int(os.environ.get("INSANE_REVIEW_PRO_MAX_WAIT", "3600"))
# 프로젝트 그룹핑 시 이전 채팅/파일 오염 방지 한 줄(패킹 첨부 전송에만 부착).
PROJECT_SCOPE_GUARD = ("\n\n(참고: 이번 메시지에 첨부된 파일만 근거로 답하라. "
                       "이 프로젝트의 이전 채팅·파일은 이번 과제와 무관하다.)")
# 첨부 실패 시 pack을 프롬프트에 인라인으로 붙여 보내는 폴백의 크기 상한(초과 시 자르지 않고 중단).
PASTE_FALLBACK_MAX_CHARS = int(os.environ.get("INSANE_REVIEW_PASTE_MAX", "50000"))

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
    # hermetic: 외부 repomix 설정(CWD의 .ts/.js/json·글로벌 설정)이 압축·본문생략(output.files)·
    # 보안검사를 조용히 바꾸지 못하도록 안전한 임시 config를 만들어 --config로 강제한다
    # (--config 지정 시 repomix는 자동탐색 대신 이 파일을 쓴다). compress는 요청값만 반영.
    hermetic_cfg = {
        "output": {"compress": bool(compress), "files": True,
                   "removeComments": False, "removeEmptyLines": False},
        "security": {"enableSecurityCheck": True},
    }
    cfg_path = out_path.with_name(out_path.name + ".repomixcfg.json")
    try:
        cfg_path.write_text(json.dumps(hermetic_cfg), encoding="utf-8")
    except OSError:
        cfg_path = None
    cmd = ["npx", "-y", spec, str(target), "-o", str(out_path), "--style", style]
    if cfg_path is not None:
        cmd += ["--config", str(cfg_path)]   # 외부 설정 차단(압축·보안·본문생략 강제)
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
        if cfg_path is not None:
            try:
                cfg_path.unlink()
            except OSError:
                pass
        # 타임아웃 전에 repomix가 부분 산출물을 남겼으면 권한 축소(시크릿 위생 — 모든 실패경로 보장)
        if out_path.exists():
            try:
                os.chmod(out_path, 0o600)
            except OSError:
                pass
        sys.exit(f"❌ repomix 타임아웃({REPOMIX_TIMEOUT}s) — 네트워크/범위 확인")
    if cfg_path is not None:   # hermetic 임시 config 정리(성공 경로)
        try:
            cfg_path.unlink()
        except OSError:
            pass
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


# ---- 크로스플랫폼 브라우저 레지스트리 (mac / windows / linux) ----
def host_os() -> str:
    s = platform.system()
    return "mac" if s == "Darwin" else "win" if s == "Windows" else "linux"


# Arc은 CDP/멀티인스턴스가 불안정해 자동 목록에서 제외(사용자가 절대경로로 직접 지정은 가능).
def _browser_registry() -> list[tuple[str, list[str]]]:
    """[(표시이름, [후보 실행경로...])] — OS별. 절대경로는 존재검사, 비절대는 PATH(which)로 해석."""
    osname = host_os()
    home = Path.home()
    if osname == "mac":
        A = "/Applications"
        return [
            ("Chrome",   [f"{A}/Google Chrome.app/Contents/MacOS/Google Chrome"]),
            ("Comet",    [f"{A}/Comet.app/Contents/MacOS/Comet"]),
            ("Brave",    [f"{A}/Brave Browser.app/Contents/MacOS/Brave Browser"]),
            ("Edge",     [f"{A}/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]),
            ("Chromium", [f"{A}/Chromium.app/Contents/MacOS/Chromium"]),
            ("Vivaldi",  [f"{A}/Vivaldi.app/Contents/MacOS/Vivaldi"]),
        ]
    if osname == "win":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        lad = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        return [
            ("Chrome",   [rf"{pf}\Google\Chrome\Application\chrome.exe",
                          rf"{pfx}\Google\Chrome\Application\chrome.exe",
                          rf"{lad}\Google\Chrome\Application\chrome.exe"]),
            ("Edge",     [rf"{pf}\Microsoft\Edge\Application\msedge.exe",
                          rf"{pfx}\Microsoft\Edge\Application\msedge.exe"]),
            ("Brave",    [rf"{pf}\BraveSoftware\Brave-Browser\Application\brave.exe",
                          rf"{pfx}\BraveSoftware\Brave-Browser\Application\brave.exe",
                          rf"{lad}\BraveSoftware\Brave-Browser\Application\brave.exe"]),
            ("Chromium", [rf"{lad}\Chromium\Application\chrome.exe"]),
            ("Vivaldi",  [rf"{lad}\Vivaldi\Application\vivaldi.exe"]),
        ]
    return [  # linux
        ("Chrome",   ["google-chrome", "google-chrome-stable"]),
        ("Chromium", ["chromium", "chromium-browser"]),
        ("Brave",    ["brave-browser", "brave"]),
        ("Edge",     ["microsoft-edge", "microsoft-edge-stable"]),
        ("Vivaldi",  ["vivaldi", "vivaldi-stable"]),
    ]


def detect_browsers() -> list[tuple[str, str]]:
    """이 OS에 설치된 크로미움 계열 브라우저 [(이름, 실행경로)]. env 경로 오버라이드도 우선 반영."""
    found, seen = [], set()
    for env, nm in (("INSANE_REVIEW_BROWSER_PATH", None),
                    ("INSANE_REVIEW_CHROME", "Chrome"), ("INSANE_REVIEW_COMET", "Comet")):
        p = os.environ.get(env)
        if p and Path(p).exists():
            name = nm or Path(p).stem
            if name.lower() not in seen:
                found.append((name, p)); seen.add(name.lower())
    for name, cands in _browser_registry():
        if name.lower() in seen:
            continue
        for c in cands:
            p = c if os.path.isabs(c) else (shutil.which(c) or "")
            if p and Path(p).exists():
                found.append((name, p)); seen.add(name.lower())
                break
    return found


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cfg = _load_config()
        cfg[key] = value
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


def save_browser_choice(name_or_path: str) -> None:
    """선택한 브라우저(이름 또는 경로)를 config에 영속화 → 다음 실행부터 재질문 안 함."""
    _save_config_key("browser", name_or_path)


LAUNCH_MODES = ("foreground", "background", "headless")


def save_launch_mode(mode: str) -> None:
    """전용 브라우저를 어떻게 띄울지 영속화(최초 1회 선택 → 이후 재질문 없음).

    foreground : 기존 동작 — 창이 뜨고 포커스를 가져간다(진행 상황을 눈으로 보고 싶을 때)
    background : 창을 숨긴 채 실행(macOS `open -g` + 새 탭 생성 후 재숨김). **기본값** — 작업 흐름을 안 끊는다
    headless   : 창 자체가 없다. 가장 조용하지만 ChatGPT가 헤드리스를 차단하면 로그인/전송이 실패할 수 있어
                 --check-env로 검증된 환경에서만 권장
    """
    if mode not in LAUNCH_MODES:
        return
    _save_config_key("launch_mode", mode)


def hide_browser_if_background() -> None:
    """background 모드에서 브라우저 앱을 다시 숨긴다.

    `open -g`로 조용히 띄워도 playwright가 `ctx.new_page()`로 새 탭을 만드는 순간
    macOS가 그 앱을 앞으로 끌어올린다. 탭 생성 직후 이걸 호출해 다시 내린다
    (앱만 숨길 뿐 프로세스·CDP 세션은 그대로라 자동화는 계속 동작한다)."""
    if host_os() != "mac" or get_launch_mode() != "background":
        return
    proc = (_load_config().get("launch_proc_name") or "").strip()
    if not proc:
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to set visible of process "{proc}" to false'],
            capture_output=True, timeout=5)
    except Exception:
        pass


def get_launch_mode() -> str:
    env = (os.environ.get("INSANE_REVIEW_LAUNCH_MODE") or "").strip().lower()
    if env in LAUNCH_MODES:
        return env
    mode = (_load_config().get("launch_mode") or "").strip().lower()
    # 미설정 기본값은 background — 창이 안 보이면서도 ChatGPT가 정상 브라우저로 인식한다.
    # (headless는 컴포저를 못 받아 전송 실패, foreground는 포커스를 뺏어 작업 흐름을 끊는다. 2026-08-26 실측)
    return mode if mode in LAUNCH_MODES else "background"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "browser"


def profile_dir_for(name: str) -> Path:
    """브라우저별 전용 프로필 분리. 크로미움 계열은 브라우저(앱)마다 쿠키 암호화 키가 달라
    (mac Keychain 'X Safe Storage' 항목이 앱별), 같은 user-data-dir을 다른 브라우저로 열면
    기존 세션 쿠키가 복호화 불가 → 로그인이 통째로 깨진다. 기존 프로필(BROWSER_PROFILE_DIR)은
    최초 사용 브라우저(owner)가 계속 소유해 기존 로그인을 보존하고, 다른 브라우저는
    'browser-profile-<이름>' 접미사 디렉토리를 쓴다."""
    cfg = _load_config()
    owner = cfg.get("profile_owner")
    if not owner:
        # 소유자 미기록: 레거시 프로필이 있으면 저장된 browser(없으면 이번 브라우저)가 승계
        owner = (cfg.get("browser") if BROWSER_PROFILE_DIR.exists() else None) or name
        _save_config_key("profile_owner", owner)
    # owner가 절대경로로 저장됐을 수 있음(--browser <경로>) → stem으로 비교
    owner_name = Path(owner).stem if os.path.isabs(str(owner)) else str(owner)
    if _slug(owner_name) == _slug(name):
        return BROWSER_PROFILE_DIR
    return BROWSER_PROFILE_DIR.with_name(f"{BROWSER_PROFILE_DIR.name}-{_slug(name)}")


def resolve_browser(name_or_path: str | None) -> tuple[str, str] | None:
    """--browser 값(이름 'chrome' 또는 절대경로)을 (이름, 경로)로 해석.
    인자 없으면 config 저장값 → 첫 감지 브라우저 순. 못 찾으면 None."""
    if name_or_path:
        if os.path.isabs(name_or_path) and Path(name_or_path).exists():
            return (Path(name_or_path).stem, name_or_path)
        for name, path in detect_browsers():
            if name.lower() == name_or_path.lower():
                return (name, path)
        return None
    saved = _load_config().get("browser")
    if saved:
        r = resolve_browser(saved)
        if r:
            return r
    bs = detect_browsers()
    return bs[0] if bs else None


def _kill_profile_browsers(profile_dir: Path) -> None:
    """전용 프로필을 점유 중인 브라우저 프로세스를 정리(크로스플랫폼 best-effort).
    전용 프로필이라 종료해도 로그인 쿠키는 디스크에 보존된다 — 스테일 인스턴스가
    새 런치를 흡수해(같은 user-data-dir 싱글톤) 디버그 포트가 안 열리는 교착을 푼다."""
    target = str(profile_dir)
    try:
        if host_os() == "win":
            ps = ("Get-CimInstance Win32_Process | "
                  f"Where-Object {{ $_.CommandLine -like '*{target}*' }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                  "-ErrorAction SilentlyContinue }")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=15)
        else:
            subprocess.run(["pkill", "-f", target], capture_output=True, timeout=10)
    except Exception:
        pass


def launch_browser_exe(path: str, name: str | None = None) -> bool:
    """전용 프로필 + 디버그 포트로 크로미움 직접 실행(크로스플랫폼) 후 CDP가 뜰 때까지 대기.
    전용 프로필에 스테일 인스턴스가 떠 있어 새 런치가 포트를 못 여는 경우(같은 user-data-dir
    싱글톤 교착)를 감지해 그 프로세스를 정리하고 1회 재시도한다.
    프로필은 브라우저별로 분리(profile_dir_for) — 다른 브라우저가 같은 프로필을 열어
    쿠키 암호화 키 불일치로 로그인이 깨지는 것을 막는다."""
    profile_dir = profile_dir_for(name or Path(path).stem)
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    mode = get_launch_mode()
    # macOS에서 앱을 다시 숨기려면 System Events용 프로세스명이 필요하다(실행파일 basename).
    _save_config_key("launch_proc_name", Path(path).name)
    cmd = [path, f"--remote-debugging-port={CDP_PORT}",
           f"--user-data-dir={profile_dir}",
           "--no-first-run", "--no-default-browser-check"]
    if mode == "headless":
        # 신형 헤드리스만 CDP·쿠키가 정상 동작한다(구형 --headless는 로그인 세션이 깨짐)
        cmd.append("--headless=new")

    def _spawn_and_wait(secs: int) -> bool:
        try:
            if mode == "background" and host_os() == "mac":
                # `open -g`: 창은 뜨되 포커스를 가져가지 않아 사용자의 작업 흐름을 끊지 않는다.
                # -n(새 인스턴스)로 전용 프로필이 기존 창에 흡수되는 것을 막는다.
                subprocess.Popen(["open", "-g", "-n", "-a", path, "--args", *cmd[1:]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            print(f"  ❌ 실행 실패: {str(exc)[:80]}")
            return False
        for i in range(secs):
            if is_port_open() and cdp_browser_ok():
                print(f"  ✓ 시작 완료 ({i + 1}s)")
                time.sleep(2)
                return True
            time.sleep(1)
        return False

    print(f"  브라우저 시작: {Path(path).name} (CDP {CDP_PORT}, 전용 프로필 {profile_dir.name})")
    if _spawn_and_wait(15):
        return True
    # 포트 미개방 = 전용 프로필에 떠 있던 스테일 인스턴스가 런치를 흡수했을 가능성.
    # 그 프로세스를 정리(로그인 보존)하고 싱글톤 락이 풀리길 기다린 뒤 1회 재시도.
    print("  ⚠️  디버그 포트 미개방 — 전용 프로필 스테일 인스턴스 정리 후 재시도")
    _kill_profile_browsers(profile_dir)
    time.sleep(3)
    if _spawn_and_wait(20):
        return True
    print("  ❌ 브라우저 시작 타임아웃 (전용 프로필 정리 후에도 실패)")
    return False


def ensure_browser(browser_arg: str | None) -> bool:
    """이미 CDP가 떠 있으면 그걸 검증·사용, 아니면 지정/감지된 브라우저를 전용 프로필로 띄운다."""
    if is_port_open():
        if cdp_browser_ok():
            print(f"  ✓ CDP 브라우저 확인 (port {CDP_PORT})")
            return True
        print(f"  ❌ port {CDP_PORT}에 CDP 브라우저가 아닌 다른 프로세스가 떠 있음")
        return False
    resolved = resolve_browser(browser_arg)
    if not resolved:
        avail = ", ".join(n for n, _ in detect_browsers()) or "없음"
        print(f"  ❌ 사용할 브라우저를 찾지 못함 (지정='{browser_arg}', 설치감지=[{avail}])")
        return False
    return launch_browser_exe(resolved[1], resolved[0])


# 실행 중 브라우저가 디스크에서 자동 업데이트되면(스테일 인스턴스) CDP 연결이 이 에러로 깨진다.
# 실측(2026-07-09, Chrome 150.46 실행 중 + 디스크 150.101): connect_over_cdp가 아래 메시지로 실패.
_STALE_CDP_MARKERS = ("Browser context management is not supported",)


def _restart_profile_browser() -> bool:
    """전용 프로필 브라우저를 재기동(쿠키는 디스크 보존 → 로그인 유지)."""
    saved = _load_config().get("browser")
    r = resolve_browser(saved) if saved else resolve_browser(None)
    if not r:
        return False
    _kill_profile_browsers(profile_dir_for(r[0]))
    time.sleep(3)
    return launch_browser_exe(r[1], r[0])


def connect_cdp(pw):
    """connect_over_cdp + 스테일 브라우저 자동 복구.
    브라우저가 떠 있는 동안 자동 업데이트되면 CDP가 깨진다(위 마커). 이때 전용 프로필
    프로세스만 재기동(로그인 보존)하고 1회 재연결 — 사용자에게 '로그인 풀림'으로 보이던
    상황의 상당수가 이 스테일 케이스다."""
    try:
        return pw.chromium.connect_over_cdp(CDP_URL)
    except Exception as exc:
        if not any(m in str(exc) for m in _STALE_CDP_MARKERS):
            raise
        print("  ♻️  CDP 연결 실패(스테일 브라우저 — 실행 중 자동업데이트 추정) → 전용 브라우저 재기동(로그인 보존)")
        if not _restart_profile_browser():
            raise
        return pw.chromium.connect_over_cdp(CDP_URL)


def _cookie_state(ctx) -> tuple[str, str]:
    """세션 쿠키(__Secure-next-auth.session-token*)의 존재·만료를 확인.
    반환: (state, expiry) — state ∈ 'ok' | 'expired' | 'missing' | 'unknown'.
    UI 프로브가 흔들려도(로딩/CF 챌린지) 쿠키로 '세션 자체'의 생사를 진단하기 위한 것."""
    try:
        cookies = ctx.cookies("https://chatgpt.com")
    except Exception:
        return ("unknown", "-")
    toks = [c for c in cookies
            if str(c.get("name", "")).startswith("__Secure-next-auth.session-token")]
    if not toks:
        return ("missing", "-")
    exp = max(float(c.get("expires") or 0) for c in toks)
    if exp <= 0:
        return ("ok", "session")   # 만료 미설정(세션 쿠키)
    exp_s = datetime.fromtimestamp(exp).strftime("%Y-%m-%d")
    return (("ok" if exp > time.time() else "expired"), exp_s)


def probe_login() -> dict:
    """브라우저(CDP) up + playwright 있을 때 ChatGPT 로그인 상태를 확인.
    반환: {'login': 'ok'|'no'|'unknown', 'cookie': 'ok'|'expired'|'missing'|'unknown', 'cookie_exp': str}
    - login='no'는 로그인 벽이 실제로 보일 때만. 컴포저가 늦게 떠도(SPA 로딩/CF 챌린지) 'no'로
      오판하지 않고 'unknown' — 멀쩡한 세션에 재로그인을 요구하던 거짓 음성 방지.
    - cookie는 UI와 무관하게 세션 쿠키의 생사를 별도 보고(진단용)."""
    import importlib.util
    res = {"login": "unknown", "cookie": "unknown", "cookie_exp": "-", "mode": "unknown"}
    if not (is_port_open(CDP_PORT) and cdp_browser_ok()):
        return res
    if not importlib.util.find_spec("playwright"):
        return res
    try:
        from playwright.sync_api import sync_playwright as _spw
        with _spw() as pw:
            b = connect_cdp(pw)
            ctx = pick_context(b)
            if ctx is None:
                res["login"], res["cookie"] = "no", "missing"
                return res

            res["cookie"], res["cookie_exp"] = _cookie_state(ctx)
            page = ctx.new_page()
            hide_browser_if_background()  # 새 탭 생성이 앱을 앞으로 끌어올리므로 즉시 재숨김
            _guard_dialogs(ctx, page)
            try:
                page.goto(CHATGPT_URL, wait_until="load", timeout=30000)
                res["login"] = login_state(page, wait_secs=15)
                if res["login"] == "ok":
                    # Chat/Work는 sticky이고 Work엔 Pro가 없다 — 진단에 현재 모드를 노출.
                    # 토글은 컴포저보다 늦게 렌더되므로 잠깐 기다렸다 읽는다.
                    m = None
                    for _ in range(12):
                        m = read_mode(page)
                        if m:
                            break
                        time.sleep(0.5)
                    res["mode"] = "none" if m is None else (m or "unknown")
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    except Exception:
        pass
    return res


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
        issues.append((f"port {CDP_PORT}이 CDP 브라우저 아님", "다른 프로세스 종료 후 --launch-browser로 전용 프로필 실행"))
    else:
        browser_state = "down"
        issues.append((f"브라우저 CDP({CDP_PORT}) 닫힘",
                       "전용 브라우저를 디버그포트+전용프로필로 실행(--launch-browser; 아래 BROWSERS 참고)"))

    # ChatGPT 로그인 프로브(브라우저 up + deps 있을 때만)
    probe = {"login": "unknown", "cookie": "unknown", "cookie_exp": "-", "mode": "unknown"}
    if browser_state == "ok" and deps_ok:
        probe = probe_login()
        if probe["login"] == "ok":
            ok.append("ChatGPT 로그인됨 (입력창/모델 어포던스 확인)")
        elif probe["login"] == "no":
            issues.append(("ChatGPT 로그인 안 됨 (로그인 벽 확인됨)",
                           "해당 브라우저에서 chatgpt.com 로그인 + Pro 추론 선택"))
        elif probe["cookie"] == "ok":
            # UI 미확인이지만 세션 쿠키는 살아있음 → 로그인 요구 대상 아님(로딩/챌린지 가능성)
            ok.append(f"ChatGPT 세션 쿠키 유효(만료 {probe['cookie_exp']}) — UI 확인만 지연(로딩/챌린지 가능), 재점검 권장")
        else:
            issues.append((f"ChatGPT 로그인 확인 불가 (login=unknown, cookie={probe['cookie']})",
                           "전용 브라우저 창에서 chatgpt.com 상태 확인 후 재점검"))

    for o in ok:
        print(f"  ✓ {o}")
    for name, hint in issues:
        print(f"  ✗ {name}\n      → {hint}")

    # 저장된 브라우저 선택값(있으면 이름) — 커맨드가 "최초 1회만 질문" 분기를 명시적으로 판단.
    _saved = _load_config().get("browser")
    if _saved:
        _r = resolve_browser(_saved)
        saved_browser = _r[0] if _r else _saved
    else:
        saved_browser = "none"

    # 머신 파싱용 상태 라인 — 커맨드 온보딩이 어느 단계가 막혔는지 분기에 사용(토큰 additive)
    print(f"\nSTATUS node={'ok' if node_ok else 'missing'} deps={'ok' if deps_ok else 'missing'} "
          f"browser={browser_state} login={probe['login']} cookie={probe['cookie']} "
          f"cookie_exp={probe['cookie_exp']} saved_browser={saved_browser} os={host_os()} "
          f"launch_mode={(_load_config().get('launch_mode') or 'unset')} "
          f"mode={probe.get('mode', 'unknown')}")
    # 설치된 크로미움 목록 — 커맨드가 브라우저 선택 AskUserQuestion을 구성하는 데 사용
    bs = detect_browsers()
    print("BROWSERS " + ",".join(n for n, _ in bs))
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


def _selector_union(selectors) -> str:
    """폴백 리스트를 CSS selector-list 하나로 — querySelectorAll은 같은 노드를 중복 반환하지 않는다.
    '첫 비영 셀렉터만 세는' 방식은 기준 시점과 현재 시점이 서로 다른 셀렉터를 세게 되어
    count-delta가 깨진다(2026-08-24 실측: 와일드카드 copy 1개 → 정밀 copy 1개 = '증가 없음' 오판)."""
    return selectors if isinstance(selectors, str) else ", ".join(selectors)


def count_msgs(page, selectors) -> int:
    try:
        return len(page.query_selector_all(_selector_union(selectors)))
    except Exception:
        return 0


def count_msgs_strict(page, selectors) -> int:
    """기준개수 포착 전용 — 조회 실패를 0으로 숨기지 않는다. 재시도 후에도 실패하면 예외(fail-closed).
    base_* 가 조회실패로 0이 되면 기존 DOM이 '새 턴'으로 오인돼 이전 답변을 저장할 수 있으므로 이를 차단한다."""
    last_exc = None
    for _ in range(3):
        try:
            return len(page.query_selector_all(_selector_union(selectors)))
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise RuntimeError(f"기준 메시지 수 조회 실패({selectors}): {str(last_exc)[:60]} → 전송 중단(fail-closed)")


def is_streaming(page) -> bool:
    try:
        return _q(page, STREAMING_BTN_SELECTORS) is not None
    except Exception:
        return False


def msg_id_set(page) -> set:
    """현재 DOM의 data-message-id 집합(역할 무관, 실측 2026-07-19: 모든 메시지 노드에 존재).
    실패 시 빈 집합 — base로 쓰일 때 빈 집합은 '아무것도 제외 안 함'이라 fail-open이 아니다
    (URL 결속이 1차 방어이므로 id는 우리 채팅 안에서만 판정에 쓰인다)."""
    try:
        return set(page.eval_on_selector_all(
            "[data-message-id]", 'els => els.map(e => e.getAttribute("data-message-id"))'))
    except Exception:
        return set()


def new_assistant_node(page, base_ids: set | None, base_assistant: int = 0):
    """회수 대상 assistant 노드. base_ids가 있으면 id 차집합의 마지막 신규 노드,
    없으면(레거시) 전송 전보다 노드가 늘었을 때만 마지막 노드. 없으면 None."""
    try:
        nodes = page.query_selector_all(_selector_union(ASSISTANT_MSG_SELECTORS))
        if not nodes:
            return None
        if base_ids is None:
            return nodes[-1] if len(nodes) > base_assistant else None
        # id가 없는 컨테이너(section/article 폴백)는 차집합 판정 불가 → 제외(옛 턴을 '신규'로 오인 방지)
        fresh = [n for n in nodes
                 if (n.get_attribute("data-message-id") or "") and n.get_attribute("data-message-id") not in base_ids]
        return fresh[-1] if fresh else None
    except Exception:
        return None


def _node_text(node) -> str:
    try:
        return (node.inner_text() or "") if node is not None else ""
    except Exception:
        return ""


def new_assistant_text(page, base_ids: set) -> str:
    """base_ids에 없는 '신규' assistant 턴의 텍스트(여럿이면 마지막). 없으면 ''."""
    return _node_text(new_assistant_node(page, base_ids))


def current_url(page) -> str:
    """페이지의 '실제' 현재 URL. page.url은 로컬 캐시라 CDP 왕복 없이는 SPA pushState를
    반영하지 못한다(실측 2026-07-23: 전송 후 30s 폴링에도 스테일, evaluate 1회로 즉시 갱신).
    location.href 평가가 1순위, 실패 시 page.url 폴백."""
    try:
        return page.evaluate("() => location.href") or ""
    except Exception:
        try:
            return page.url or ""
        except Exception:
            return ""


def capture_conv_url(page, timeout_secs: int = CONV_URL_CAPTURE_SECS) -> str | None:
    """전송 후 SPA가 발급하는 대화 URL(/c/<id>)을 포착. 실패 시 None(호출자 fail-closed)."""
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        u = current_url(page)
        if CONV_URL_RE.search(u):
            return u
        time.sleep(1)
    return None


def write_run_manifest(path: Path, conv_url: str, label: str, run_tag: str,
                       prompt_text: str, pack_path) -> None:
    """전송 직후 대화 URL 등을 원자적으로 디스크에 기록 — stdout은 터미널 크래시에 유실되므로
    manifest가 있어야 프로세스가 죽어도 --harvest로 항상 회수할 수 있다(2026-07-19 카운슬)."""
    try:
        data = {"chat_url": conv_url, "label": label, "run_tag": run_tag,
                "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
                "pack": str(pack_path) if pack_path else None,
                "created_at": datetime.now().astimezone().isoformat()}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        print(f"  🧾 run manifest 기록: {path.name}")
    except Exception:
        pass


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", text).strip() if text else ""


def last_assistant_node(page):
    nodes = _qa(page, ASSISTANT_MSG_SELECTORS)
    return nodes[-1] if nodes else None


def last_assistant_text(page) -> str:
    node = last_assistant_node(page)
    if node:
        try:
            return node.inner_text() or ""
        except Exception:
            return ""
    return ""


def node_copy_button(node):
    """해당 assistant 노드 '안'의 턴 복사 버튼(전역 마지막 버튼이 아님 — 코드블록 copy/다른 턴 오클릭 방지)."""
    if node is None:
        return None
    scopes = [node]
    try:
        container = node.evaluate_handle("(n, sel) => n.closest(sel)", TURN_CONTAINER_SELECTOR).as_element()
        if container is not None:
            scopes.insert(0, container)
    except Exception:
        pass
    for scope in scopes:
        for sel in COPY_BTN_SELECTORS:
            try:
                btn = scope.query_selector(sel)
                if btn is not None:
                    return btn
            except Exception:
                continue
    return None


def send_button_ready(page) -> bool:
    """컴포저가 다시 전송 가능 상태(=이전 턴 종결)인지. copy 툴바가 늦게 붙는 변형의 보조 종결 신호."""
    for sel in SEND_BTN_SELECTORS:
        try:
            for btn in page.query_selector_all(sel):
                if btn.is_visible() and btn.is_enabled():
                    return True
        except Exception:
            continue
    return False


def turn_terminal(page, node) -> bool:
    """대상 턴 종결 판정: 스트리밍 중 아님 + (그 노드의 copy 버튼 존재 또는 전송 버튼 복귀).
    전역 copy 버튼 '개수 증가'를 필수 조건으로 삼던 설계는 툴바 지연/가상화/셀렉터 전환에 전부 취약해
    완성된 답을 두고 최대 대기를 소진시켰다(2026-08-24 GPT Pro 리뷰 P0)."""
    if node is None or is_streaming(page):
        return False
    return node_copy_button(node) is not None or send_button_ready(page)


def clipboard_matches(txt: str, expected: str | None) -> bool:
    """클립보드 경합 오염 가드. 짧은 응답(<80자)은 정규화 전체 일치, 긴 응답은 시작·중간·끝 3조각 대조."""
    if not expected:
        return True
    exp = normalize(expected)
    got = normalize(txt)
    if len(exp) < 80:
        return got == exp
    probes = (exp[:30], exp[len(exp) // 2: len(exp) // 2 + 30], exp[-30:])
    return all(p in got for p in probes if p)


def copy_assistant_node(node, expected: str | None = None) -> str | None:
    """대상 노드의 copy 버튼으로 클립보드 회수(마크다운 보존). sentinel로 '복사 실패', expected 대조로
    '남의 복사'를 각각 거른다. 실패 시 None → 호출자가 DOM 텍스트로 폴백(내용 오염 < 서식 손실)."""
    if pyperclip is None:
        return None
    btn = node_copy_button(node)
    if btn is None:
        return None
    try:
        for _ in range(3):
            pyperclip.copy("__INSANE_REVIEW_SENTINEL__")
            btn.click(force=True)
            time.sleep(1)
            txt = pyperclip.paste()
            if txt and txt != "__INSANE_REVIEW_SENTINEL__" and txt.strip() and clipboard_matches(txt, expected):
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
# 실측(2026-07-10): pill 클릭 → menuitemradio(즉시/중간/높음/매우 높음/Pro=추론단계)
#   + menuitem("GPT-5.6 Sol"=모델 서브메뉴 트리거). 트리거를 hover하면 모델 radio들
#   (GPT-5.6 Sol/GPT-5.5/GPT-5.4/GPT-5.3/o3)이 같은 메뉴 DOM에 menuitemradio로 추가된다.
# 실측(2026-08-18): UI 개편 — pill 팝오버가 슬라이더(simple 뷰)로 열린다.
#   [data-testid="composer-intelligence-picker-content"] 안에 '고급' menuitem이 있고,
#   클릭하면 advanced 뷰('모델' / '추론 강도' 서브메뉴 트리거)로 전환된다.
#   '추론 강도'를 hover하면 옛 menuitemradio 목록(즉시/중간/높음/매우 높음/Pro)이 그대로 뜬다.
#   활성 모델명은 '모델' 행의 trailing span(예: 'GPT-5.6 Sol')에 표시된다.
EFFORT_ITEM_SELECTORS = ['[role="menuitemradio"]', '[role="menuitem"]', '[role="option"]']
INTELLIGENCE_PICKER_SELECTOR = '[data-testid="composer-intelligence-picker-content"]'

# ---- Chat / Work 모드 (실측 2026-08-29) ----
# 헤더 중앙 radiogroup에 'Chat'/'Work' 라디오 2개. URL·_account 쿠키가 동일해
# workspace_id 결속으로는 구분되지 않는다. Work 모드엔 Pro 추론단계가 아예 없고
# (슬라이더에 Pro 눈금 부재, data-max="false") pill이 '5.6 Sol 매우 높음'으로 뜬다.
# 선택은 sticky — 사람이 웹에서 Work로 바꿔 쓰면 이후 자동 실행이 조용히 비-Pro로 나간다.
MODE_RADIO_SELECTOR = '[role="radiogroup"] [role="radio"]'
JS_READ_MODE = """() => {
  const rs = [...document.querySelectorAll('[role="radiogroup"] [role="radio"]')]
    .map(x => ({label: (x.textContent || '').trim(), checked: x.getAttribute('aria-checked') === 'true'}));
  if (!rs.some(r => /^(chat|work)$/i.test(r.label))) return null;
  const on = rs.find(r => r.checked);
  return on ? on.label : '';
}"""
JS_CLICK_MODE = """(want) => {
  const el = [...document.querySelectorAll('[role="radiogroup"] [role="radio"]')]
    .find(x => (x.textContent || '').trim().toLowerCase() === want.toLowerCase());
  if (!el) return false;
  el.click();
  return true;
}"""


def read_mode(page) -> str | None:
    """현재 Chat/Work 모드. 토글이 없는 계정/UI면 None."""
    try:
        return page.evaluate(JS_READ_MODE)
    except Exception:
        return None


def ensure_chat_mode(page) -> tuple[bool, str | None]:
    """Pro가 존재하는 Chat 모드로 보정한다.
    반환: (chat 모드 확정 여부, 관측된 모드). 토글 자체가 없으면 (True, None) —
    구 UI/개인 계정은 애초에 모드 분기가 없으므로 통과시킨다."""
    mode = read_mode(page)
    if mode is None:
        return True, None
    if mode.lower() == "chat":
        return True, mode
    print(f"  ⚠️  현재 모드가 '{mode or '미상'}' — Work 모드엔 Pro가 없다. Chat으로 전환 시도")
    try:
        if not page.evaluate(JS_CLICK_MODE, "Chat"):
            print("  ❌ Chat 라디오를 찾지 못함")
            return False, mode
    except Exception as e:
        print(f"  ❌ 모드 전환 실패: {e}")
        return False, mode
    for _ in range(10):
        time.sleep(0.5)
        now = read_mode(page)
        if now and now.lower() == "chat":
            print("  ✓ Chat 모드로 전환됨")
            return True, now
    print("  ❌ Chat 모드 전환이 반영되지 않음")
    return False, read_mode(page)


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


def _enter_effort_view(page) -> None:
    """새 슬라이더 UI(2026-08): 팝오버가 simple 슬라이더 뷰로 열리므로
    '고급' 클릭 → '추론 강도' hover로 옛 menuitemradio 목록을 노출시킨다.
    구 UI(팝오버 testid 없음)면 아무것도 하지 않는다."""
    try:
        if not page.query_selector(INTELLIGENCE_PICKER_SELECTOR):
            return  # 구 UI
        # 1) '고급' 항목 클릭 (simple 뷰일 때만 존재 — advanced 뷰면 스킵)
        for it in page.query_selector_all('[role="menuitem"]'):
            t = (it.inner_text() or "").strip()
            if t.startswith("고급") or t.lower().startswith("advanced"):
                it.click()
                time.sleep(0.8)
                break
        # 2) '추론 강도' 서브메뉴 트리거 hover → 추론단계 radio 노출 대기
        for it in page.query_selector_all('[role="menuitem"][data-has-submenu]'):
            t = (it.inner_text() or "").strip()
            if t.startswith("추론") or "reasoning" in t.lower() or "effort" in t.lower():
                it.hover()
                for _ in range(10):
                    time.sleep(0.3)
                    if page.query_selector('[role="menuitemradio"]'):
                        break
                break
    except Exception:
        pass


def _close_switcher(page) -> None:
    """스위처 팝오버 닫기. 새 UI에선 서브메뉴가 열려 있으면 Escape 1회는 서브메뉴만
    닫으므로, 팝오버가 사라질 때까지 최대 3회 누른다.
    주의: 메뉴가 이미 닫혀 있으면 Escape를 누르지 않는다 — 응답 생성 중에 페이지에
    Escape가 가면 '응답 생성을 중지할까요?' 다이얼로그가 떠버린다(2026-08-18 실측)."""
    try:
        for _ in range(3):
            if not page.query_selector(f'{INTELLIGENCE_PICKER_SELECTOR}, [role="menu"][data-state="open"]'):
                break
            page.keyboard.press("Escape")
            time.sleep(0.3)
    except Exception:
        pass


def _open_switcher_raw(page) -> bool:
    """pill 클릭으로 팝오버만 연다(뷰 전환 없음). 이미 열려 있으면 그대로 True."""
    try:
        if page.query_selector(INTELLIGENCE_PICKER_SELECTOR):
            return True
    except Exception:
        pass
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


def _open_switcher(page):
    if _open_switcher_raw(page):
        _enter_effort_view(page)
        return True
    return False


def _slider_value(page) -> tuple[int | None, int | None]:
    """새 UI 슬라이더의 (현재값, 최대값). 슬라이더 없으면 (None, None)."""
    try:
        r = page.evaluate("""() => {
          const s = document.querySelector('[role="slider"]');
          return s ? [ +s.getAttribute('aria-valuenow'), +s.getAttribute('aria-valuemax') ] : null;
        }""")
        return (r[0], r[1]) if r else (None, None)
    except Exception:
        return (None, None)


def _set_effort_slider(page, target_idx: int) -> bool:
    """새 UI(2026-08): 추론단계 슬라이더를 target_idx로 이동.
    서브메뉴 radio는 슬라이더 파티클 애니메이션의 상시 리렌더로 클릭이 detach 실패하므로
    (일반/force/좌표 클릭 전부 무효 실측), 유일하게 안정적인 경로는
    SliderControl 프로그램 focus + ArrowLeft/ArrowRight 키 입력이다."""
    try:
        for _attempt in range(2):
            cur, mx = _slider_value(page)
            if cur is None:
                return False
            if cur == target_idx:
                return True
            ok = page.evaluate("""() => {
              const c = document.querySelector('[data-model-reasoning-effort-slider]')?.closest('[role="menuitem"]');
              if (!c) return false;
              c.focus();
              return document.activeElement === c;
            }""")
            if not ok:
                return False
            key = "ArrowRight" if target_idx > cur else "ArrowLeft"
            for _ in range(abs(target_idx - cur)):
                page.keyboard.press(key)
                time.sleep(0.4)
        cur, _mx = _slider_value(page)
        return cur == target_idx
    except Exception:
        return False


# 새 UI 슬라이더 인덱스 폴백 맵(서브메뉴 라벨을 못 읽었을 때만 사용).
EFFORT_SLIDER_FALLBACK = {"즉시": 0, "중간": 1, "높음": 2, "매우 높음": 3, "pro": 4,
                          "instant": 0, "standard": 1, "high": 2, "extended": 3}


def read_menu_state(page) -> dict:
    """열린 메뉴에서 모델명(menuitem 중 checked/selected) + 체크된 추론단계(menuitemradio aria-checked)를 읽는다."""
    state = {"model": None, "model_source": None, "models": [], "effort_checked": None, "items": []}
    try:
        # 새 UI(2026-08): advanced 뷰의 '모델' 행 trailing span이 곧 활성 모델명(예: 'GPT-5.6 Sol').
        for it in page.query_selector_all('[role="menuitem"][data-has-submenu]'):
            t = (it.inner_text() or "").strip()
            if t.startswith("모델") or t.lower().startswith("model"):
                sp = it.query_selector(".trailing span")
                name = ((sp.inner_text() or "").strip() if sp else "")[:40]
                if name:
                    state["model"] = name
                    state["model_source"] = "checked"
                    state["models"].append(name)
                break
    except Exception:
        pass
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
            # 모델 서브메뉴가 펼쳐져 있으면 모델 radio(예: 'GPT-5.6 Sol')도 menuitemradio+checked로
            # 잡혀 추론단계 판정을 덮어쓴다 — 모델명 패턴은 effort 후보에서 제외.
            if re.search(r"GPT|gpt|o\d|Claude|Gemini", t):
                continue
            if it.get_attribute("aria-checked") == "true":
                state["effort_checked"] = t
    except Exception:
        pass
    return state


def select_model(page, want: str, require_model: str | None = None) -> tuple[bool, str | None]:
    """모델 스위처를 열고 want(추론단계, 예: 'pro')를 선택 + 검증.
    require_model 지정 시 모델명(예: 'GPT-5.6')이 일치하지 않으면 False(실패) 반환.
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
            _close_switcher(page)
            return False, None
        if require_model.lower() not in before["model"].lower():
            print(f"  ❌ 모델 불일치: 기대 '{require_model}' ≠ 메뉴 '{before['model']}' → 중단(전송 안 함)")
            _close_switcher(page)
            return False, None

    # ---- 새 UI(2026-08, 슬라이더) 경로 ----
    if page.query_selector(INTELLIGENCE_PICKER_SELECTOR):
        items = before["items"]  # 예: ['즉시','중간','높음','매우 높음','Pro'] (advanced 서브메뉴 실측)
        idx = None
        label = None
        for exact in (True, False):
            for i, t in enumerate(items):
                low = t.strip().lower()
                if (exact and low == want_l) or (not exact and want_l in low):
                    idx, label = i, t.strip()
                    break
            if idx is not None:
                break
        if idx is None:
            # 서브메뉴 라벨을 못 읽은 경우 폴백: pro=슬라이더 최댓값, 그 외 고정 맵
            _cur, mx = _slider_value(page)
            if want_l == "pro" and mx is not None:
                idx, label = mx, "Pro"
            elif want_l in EFFORT_SLIDER_FALLBACK:
                idx, label = EFFORT_SLIDER_FALLBACK[want_l], want
        if idx is None:
            print(f"  ⚠️  '{want}' 추론단계 항목 못 찾음(슬라이더 UI) → 기본값")
            _close_switcher(page)
            return False, None

        # 서브메뉴가 열린 advanced 뷰에선 슬라이더 키 입력이 불안정 → 닫고 simple 뷰로 재오픈
        _close_switcher(page)
        time.sleep(0.5)
        if not _open_switcher_raw(page):
            print("  ⚠️  슬라이더 재오픈 실패")
            return False, None
        slider_ok = _set_effort_slider(page, idx)
        _close_switcher(page)
        time.sleep(0.5)

        pills = read_model_pills(page)
        pill_txt = pills[0] if pills else ""
        effort_verified = slider_ok and (pill_txt == label or want_l in pill_txt.lower())
        # 모델 검증은 advanced 뷰에서 읽은 before(model_source='checked') 기준
        model_verified = True
        if require_model:
            model_verified = (before["model"] is not None
                              and require_model.lower() in before["model"].lower()
                              and before.get("model_source") == "checked")
        verified = model_verified and effort_verified
        verified_model_name = f"{before['model'] or 'Unknown Model'} ({pill_txt or label})"
        print(f"  {'✓' if verified else '⚠️'} 최종 모델 검증(슬라이더): model={before['model']} (기대:{require_model}), "
              f"effort=슬라이더 {idx}({pill_txt or '?'}) (기대:{want}) -> 결과={'OK' if verified else '실패'}")
        return verified, verified_model_name

    # ---- 구 UI(radio 메뉴) 경로 ----
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
                # 서브메뉴 트리거(예: '추론 강도Pro' 행)는 클릭 대상이 아님 — 오클릭 방지.
                if it.get_attribute("aria-haspopup"):
                    continue
                t = (it.inner_text() or "").strip()
                low = t.lower()
                if (exact and low == want_l) or (not exact and want_l in low):
                    try:
                        it.click(timeout=4000)
                    except Exception:
                        # 새 UI 서브메뉴는 애니메이션 탓에 액션ability 체크에 걸린다(2026-08-18 실측) → force 폴백
                        it.click(force=True, timeout=4000)
                    clicked = t.splitlines()[0][:40]
                    time.sleep(1.5)  # 클릭 후 드롭다운이 닫히는 시간 대기
                    break
            except Exception:
                continue
        if clicked:
            break

    if not clicked:
        print(f"  ⚠️  '{want}' 추론단계 항목 못 찾음 → 기본값")
        _close_switcher(page)
        return False, None

    # Pro 제안: 메뉴 재오픈하여 effort_checked 및 model_checked 상태 검증
    if not _open_switcher(page):
        print("  ⚠️  선택 상태 검증을 위해 메뉴 재오픈 실패")
        return False, None

    after = read_menu_state(page)
    _close_switcher(page)
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
            print("  ⚠️  파일 입력 요소를 못 찾음 → 호출자 폴백 판단(붙여넣기 or 중단)")
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


def build_paste_fallback(prompt: str, pack_path: Path) -> str | None:
    """첨부 실패 시 pack을 프롬프트에 인라인으로 붙여 보낼 메시지를 구성.
    크기 상한 초과면 None(호출자가 조용히 자르지 않고 fail-closed) — 잘린 컨텍스트 전송 방지."""
    try:
        body = pack_path.read_text(encoding="utf-8", errors="strict")
    except OSError:
        return None
    if len(body) > PASTE_FALLBACK_MAX_CHARS:
        return None
    return f'{prompt}\n\n<repomix_pack file="{pack_path.name}">\n{body}\n</repomix_pack>'


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
    # 크로스플랫폼: OS 클립보드/⌘V(맥 전용) 대신 Playwright 네이티브 insert_text(insertText 이벤트).
    # → mac/win/linux 동일 동작 + 동시 실행 시 클립보드 경합 제거. 실패 시 키 입력 폴백.
    try:
        page.keyboard.insert_text(message)
    except Exception:
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
    실측 2026-07-19(cot v5 UI): 버튼은 우측 flyout이 아니라 본문 리즈닝 고정행
    (div[data-testid="cot-v5-pinned-row"]) 안의 button. 이 행은 TransitionGroup
    애니메이션 속이라 Playwright 안정성 판정이 타임아웃될 수 있어 force 클릭 폴백을 둔다.
    구 UI(우측 flyout) 대비 텍스트 매칭 경로는 폴백으로 유지.
    칩 매칭은 '생각 중'으로 좁힌다 — 프롬프트 본문의 '추론' 등과 오매칭 방지."""
    # 1) 신 UI: 고정행 셀렉터 직행(스크롤 조작 불필요 — 요소 단위 scroll_into_view만)
    try:
        row = page.query_selector(ANSWER_NOW_ROW_SELECTOR)
        if row:
            btns = [b for b in row.query_selector_all("button") if b.is_visible()]
            target = next((b for b in btns if ANSWER_NOW_TEXT_RE.search(b.inner_text() or "")),
                          btns[0] if len(btns) == 1 else None)
            if target:
                try:
                    target.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    target.click(timeout=2500)
                    return True
                except Exception:
                    try:
                        target.click(force=True)  # 애니메이션 중 안정성 판정 실패 대비
                        return True
                    except Exception:
                        pass  # 셀렉터 경로 실패 → 아래 텍스트 매칭 폴백
    except Exception:
        pass

    # 2) 구 UI 폴백: 텍스트 매칭(+ 리즈닝 칩 열기)
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
                    try:
                        loc.first.click(timeout=2500)
                    except Exception:
                        loc.first.click(force=True, timeout=2500)  # 애니메이션 안정성 판정 실패 대비
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
                           base_user: int = 0, base_assistant: int = 0, base_copy: int = 0,
                           conv_url: str | None = None, base_ids: set | None = None,
                           skip_sent_check: bool = False, on_bound=None) -> tuple[str, str, str | None]:
    """전송이 만든 '대화 URL' + message-id에 결속해 응답을 회수(v0.6.0 identity 결속).
    - conv_url: 이미 결속된 대화 URL(회수 재시도/harvest). None이면 전송 직후 SPA에서 포착.
    - base_ids: 전송 직전 DOM의 data-message-id 집합 — 신규 턴을 id 차집합으로 판정.
    - skip_sent_check: 회수 재시도/harvest 경로 — user 턴 존재를 전제(재전송 없음).
    반환: (status, text, conv_url) — status ∈ {'ok','timeout','not_sent','sent_unknown_location','quota'}."""
    mw = max_wait if max_wait else MAX_WAIT_SECS
    start = time.monotonic()
    last_status = 0
    force_tries = 0

    # 1) 우리 user 턴이 '새로' 떴는지(count 증가 또는 대화 URL 발급). 안 떴으면 not_sent → 호출자가 재전송
    if not skip_sent_check:
        sent = False
        while time.monotonic() - start < 40:  # 25→40s: 첨부 처리 지연 오판→중복 전송 방지(2026-07-19 카운슬)
            url_flipped = bool(CONV_URL_RE.search(current_url(page)))
            if count_msgs(page, USER_MSG_SELECTORS) > base_user or url_flipped:
                sent = True
                break
            time.sleep(1)
        if not sent:
            return ("not_sent", "", conv_url)

    # 1.5) 대화 URL 결속 — 포착 실패 시 fail-closed. 어디로 갔는지 모르는 채 기다리면
    # 스테일 캡처(2026-07-18 실측: 옛 채팅 메시지를 새 응답으로 성공 저장)가 재발하고,
    # 재전송하면 중복 채팅이 생기므로 전용 상태로 종료해 호출자가 둘 다 하지 않게 한다.
    if conv_url is None:
        conv_url = capture_conv_url(page)
        if conv_url is None:
            return ("sent_unknown_location", "", None)
        print(f"  🔗 대화 결속: {conv_url}")
        if on_bound is not None:
            # 결속 즉시 영속화 — 응답 대기(최대 60분) 중 프로세스가 죽어도 manifest로 --harvest 가능
            try:
                on_bound(conv_url)
            except Exception:
                pass
    _m = CONV_URL_RE.search(conv_url)
    conv_key = _m.group(0) if _m else None

    # 2) assistant 턴 완료까지 대기 (stop-button 사라짐 + copy 버튼 + 텍스트 안정)
    print(f"    응답 대기 중... (최대 {mw}s"
          + (f", {force_after}s 후 '지금 답변 받기' 재시도" if force_after else "") + ")")
    stable_since = None
    stall_since = None
    reloads = 0
    last_text = ""
    deadline = start + mw
    grace_used = False
    while True:
        if time.monotonic() >= deadline:
            # 최대 대기 소진 — 아직 리즈닝 중이면 마지막 수단으로 '지금 답변 받기'를 눌러
            # 답변을 플러시시키고 1회에 한해 추가 유예를 준다(실패로 버리는 것보다 회수가 낫다).
            if not grace_used and is_streaming(page) and click_answer_now(page):
                grace_used = True
                deadline = time.monotonic() + FORCE_TIMEOUT_GRACE_SECS
                print(f"    ⏰ 최대 대기 소진 — 마지막 수단 '지금 답변 받기' 클릭 → {FORCE_TIMEOUT_GRACE_SECS}s 추가 대기")
                continue
            break
        elapsed = int(time.monotonic() - start)

        # 결속 이탈 감지(사용자 클릭/SPA 이동 — 2026-07-18 스테일 캡처의 직접 원인) → 대화 URL로 복귀.
        drifted = bool(conv_key) and conv_key not in current_url(page)
        if drifted:
            print(f"    ↩️  결속 채팅 이탈 감지({elapsed}s) → 복귀: {conv_url}")
            try:
                page.goto(conv_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            stable_since = None
            time.sleep(2)
            continue

        # force-answer: 성공할 때까지 매 틱 재시도(상한). 실패해도 latch 안 함.
        if force_after and elapsed >= force_after and force_tries < FORCE_MAX_TRIES and is_streaming(page):
            if click_answer_now(page):
                print(f"    ⚡ {elapsed}s — '지금 답변 받기' 클릭(리즈닝 강제 종료)")
                force_tries = FORCE_MAX_TRIES  # 성공 → 그만
            else:
                force_tries += 1
                if force_tries >= FORCE_MAX_TRIES:
                    print(f"    ⚠️  {elapsed}s — '지금 답변 받기' 버튼 {FORCE_MAX_TRIES}회 실패 → 자연완료 대기")

        # 대상 턴(신규 assistant 노드)과 종결 신호 — 게이트별로 로그에 남겨 막힌 predicate를 바로 알 수 있게
        node = new_assistant_node(page, base_ids, base_assistant=base_assistant)
        cur = _node_text(node)
        streaming = is_streaming(page)
        terminal = turn_terminal(page, node)

        if elapsed - last_status >= STATUS_INTERVAL and elapsed > 0:
            print(f"    {elapsed}s | " + ("⏳ 생성중" if streaming else "정지")
                  + f" | assistant={count_msgs(page, ASSISTANT_MSG_SELECTORS)}/{base_assistant}"
                  + f" fresh_len={len(cur.strip())} copy={'y' if node_copy_button(node) else 'n'}"
                  + f" send={'y' if send_button_ready(page) else 'n'} terminal={'y' if terminal else 'n'}")
            last_status = elapsed

        if elapsed < MIN_WAIT_SECS or streaming:
            stable_since = None
            stall_since = None
            time.sleep(2)
            continue

        # 스톨 복구(실측 2026-08-25): 스트리밍 표시도 없고 assistant 노드가 빈 채로 멈추는 클라이언트 스트림 유실.
        # 서버엔 답이 있어 재로드하면 즉시 보인다(어제 '재시도 29초 성공'의 실체). 결속 URL로 재로드(재전송 아님).
        if not cur.strip():
            stall_since = stall_since or time.monotonic()
            if time.monotonic() - stall_since >= STALL_RELOAD_SECS and reloads < STALL_MAX_RELOADS:
                reloads += 1
                print(f"    🔄 {elapsed}s — 응답 렌더 스톨(빈 턴/스트리밍 없음) → 결속 채팅 재로드 {reloads}/{STALL_MAX_RELOADS}")
                try:
                    page.goto(conv_url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                stall_since = None
                stable_since = None
                time.sleep(3)
                continue
        else:
            stall_since = None

        if not terminal or not cur.strip():
            quota_msg = detect_quota_block(page)
            if quota_msg:
                print(f"    ⛔ 사용량 한도 감지 → 대기 중단: {quota_msg[:80]}")
                return ("quota", "", conv_url)
            stable_since = None
            time.sleep(2)
            continue
        if normalize(cur) != normalize(last_text):
            last_text = cur
            stable_since = time.monotonic()
            time.sleep(2)
            continue
        if stable_since and (time.monotonic() - stable_since) >= STABLE_CHECK_SECS:
            # 회수: 대상 노드의 copy 우선(마크다운 보존), 대조 실패/버튼 없음은 그 노드의 DOM 텍스트로 폴백
            txt = copy_assistant_node(node, expected=cur)
            if txt and txt.strip():
                print(f"    ✅ 응답 수신: {len(txt)}자 ({int(time.monotonic()-start)}s, copy)")
                return ("ok", txt, conv_url)
            print(f"    ✅ 응답 수신: {len(cur)}자 ({int(time.monotonic()-start)}s, DOM)")
            return ("ok", cur, conv_url)
        time.sleep(2)

    fallback = _node_text(new_assistant_node(page, base_ids, base_assistant=base_assistant))
    return ("timeout", fallback, conv_url) if fallback else ("timeout", "", conv_url)


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


def login_state(page, wait_secs: int = 12) -> str:
    """로그인 3단계 판정: 'ok' | 'no' | 'unknown'.
    - 'no': 로그인 벽(로그인 버튼 등)이 실제로 보일 때만 — 이것만이 재로그인 요구의 근거.
    - 'ok': 입력창 + 인증 세션에서만 렌더되는 컴포저 어포던스(모델 pill/파일 input) 확인.
    - 'unknown': wait_secs 동안 둘 다 안 보임(SPA 로딩 지연/CF 챌린지/UI 변경).
      기존엔 이 경우를 'no'로 오판해 멀쩡한 세션에 재로그인을 반복 요구했다(거짓 음성).
    판정 전체를 폴링 — 느린 환경일수록 컴포저가 늦게 떠서 단발 조회는 오판한다."""
    deadline = time.monotonic() + wait_secs
    while True:
        for sel in LOGIN_WALL_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    return "no"
            except Exception:
                continue
        try:
            if find_input(page) is not None and (
                    page.query_selector('button.__composer-pill')
                    or page.query_selector(FILE_INPUT_SELECTOR)):
                return "ok"
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return "unknown"
        time.sleep(0.5)


def looks_logged_in(page) -> bool:
    """전송 경로용 fail-closed 래퍼 — 'ok'만 통과(unknown도 전송 안 함)."""
    return login_state(page) == "ok"


# ===========================================================================
# 3.9) ChatGPT 프로젝트 그룹핑 — 폴더명 프로젝트로 채팅 정리 (캐시→탐색→생성)
# 일반 채팅 목록이 매 실행마다 쌓이는 걸 막고, 폴더별로 채팅을 프로젝트 안에 묶는다.
# 프로젝트 홈 화면에도 컴포저(#prompt-textarea)·파일첨부(input[type=file])·모델 pill이
# 그대로 있어, 프로젝트 URL로 goto만 하면 이후 첨부/모델검증/전송/회수 로직은 변경 없이 동작.
# ===========================================================================
def _load_project_cache(cache_path: Path) -> dict:
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_project_cache(cache_path: Path, cache: dict) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # 프로세스별 고유 tmp — 고정 이름(.json.tmp)은 동시 실행 시 서로의 tmp를 replace/삭제한다
        tmp = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, cache_path)  # 원자적 저장
    except Exception:
        pass


@contextmanager
def project_cache_lock(cache_path: Path, timeout: int = 90):
    """projects.json의 read-check-find-create-write 임계구역 직렬화(디렉터리 lock, 표준 라이브러리만).
    lock 없이는 두 프로세스가 같은 dict를 읽고 각자 저장해 삭제가 되살아나거나 키가 유실되고,
    둘 다 '없음' 판정 후 같은 이름의 원격 프로젝트를 중복 생성한다. 10분 넘은 lock은 죽은 프로세스로 보고 회수."""
    lock_dir = cache_path.with_name(cache_path.name + ".lock")
    deadline = time.monotonic() + timeout
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - lock_dir.stat().st_mtime > 600:
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"project cache lock timeout: {lock_dir}")
            time.sleep(0.2)
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


# 워크스페이스 이동(개인→팀 등)으로 접근 불가가 된 프로젝트는 URL이 유지된 채
# 에러 모달만 뜨는 케이스가 있어, URL 존재만으로는 생존 판정이 안 된다.
_PROJECT_ACCESS_ERROR_RE = (
    r"이 프로젝트에 액세스할 수 없습니다|can.t access this project|"
    r"don.t have access|올바른 계정으로 로그인|プロジェクトにアクセスできません"
)
_PROJECT_ID_RE = re.compile(r"(g-p-[0-9a-f]{32})", re.I)
PROJECT_OK, PROJECT_DEAD, PROJECT_AUTH, PROJECT_UNKNOWN = "ok", "dead", "auth", "unknown"


def find_visible_input(page):
    """가시적·활성 컴포저만(#prompt-textarea 우선). 에러 모달 아래 숨은 컴포저·다른 contenteditable을 통과시키지 않는다."""
    for sel in INPUT_SELECTORS:
        try:
            for el in page.query_selector_all(sel):
                if el.is_visible() and el.is_enabled():
                    return el
        except Exception:
            continue
    return None


def visible_alert_dialog_text(page) -> str:
    """가시적 에러 표면([role=dialog|alert]) 텍스트만 — 본문 전체를 보면 채팅 제목/프롬프트 속 문구에 오탐한다."""
    parts = []
    for sel in ('[role="dialog"]', '[role="alert"]'):
        try:
            for node in page.query_selector_all(sel):
                if node.is_visible():
                    parts.append(node.inner_text() or "")
        except Exception:
            continue
    return "\n".join(parts)


def project_home_state(page, url: str, probe_secs: int = 15) -> str:
    """프로젝트 URL 생존을 4상태로 판정(2초 단발 → 폴링 + 연속 안정 구간).
    ok: 그 g-p id가 URL에 유지 + 가시 컴포저 + 차단 다이얼로그 없음이 4초 연속.
    dead: id 불일치(홈 리다이렉트)·명시적 403/404·접근불가 문구 — 현 워크스페이스에서 재탐색/재생성 대상.
    auth: 로그인 벽. unknown: 지연·네트워크·UI 변경 — 캐시 삭제·프로젝트 생성 모두 금지(fail-closed).
    False 하나로 뭉개면 일시 오류에 정상 캐시를 지우고 중복 프로젝트를 만든다(2026-08-24 GPT Pro 리뷰)."""
    m = _PROJECT_ID_RE.search(url)
    if not m:
        return PROJECT_UNKNOWN  # 파싱 실패는 identity 검사 생략 사유가 아니다
    gp_id = m.group(1).lower()
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return PROJECT_UNKNOWN
    try:
        if resp is not None:
            if resp.status == 401:
                return PROJECT_AUTH
            if resp.status in (403, 404):
                return PROJECT_DEAD
    except Exception:
        pass
    deadline = time.monotonic() + probe_secs
    healthy_since = None
    final_url = ""
    while time.monotonic() < deadline:
        try:
            final_url = current_url(page)
            if _q(page, LOGIN_WALL_SELECTORS) is not None:
                return PROJECT_AUTH
            blocking = visible_alert_dialog_text(page)
            if re.search(_PROJECT_ACCESS_ERROR_RE, blocking, re.I):
                return PROJECT_DEAD
            if gp_id in final_url.lower() and not blocking and find_visible_input(page) is not None:
                if healthy_since is None:
                    healthy_since = time.monotonic()
                elif time.monotonic() - healthy_since >= 4:
                    return PROJECT_OK
            else:
                healthy_since = None
        except Exception:
            healthy_since = None
        time.sleep(0.5)
    if final_url and gp_id not in final_url.lower():
        return PROJECT_DEAD
    return PROJECT_UNKNOWN


# 다국어(사용자 ChatGPT UI 언어) 베스트에포트 — '새 프로젝트' 버튼 / '만들기' 제출 버튼.
_NEW_PROJECT_RE = r"새 프로젝트|New project|新規プロジェクト|プロジェクトを追加|Add project|Create project"
_CREATE_SUBMIT_RE = r"프로젝트 만들기|Create project|プロジェクトを作成|^Create$|^作成$|^만들기$"


def find_project_url_api(page, name: str) -> str | None:
    """현 워크스페이스의 프로젝트를 백엔드 API로 표시이름 정확 일치 조회 → 홈 URL 구성.
    실측 2026-08-25: 프로젝트가 사이드바에 a[href] 링크로 렌더되지 않고 '프로젝트' 페이지 뒤로 이동
    → DOM 탐색이 구조적으로 실패. API가 언어·가상화·접힘 무관하고 결정적이라 1순위.
    chatgpt.com 오리진 페이지에서만 동작. 실패는 None(호출자가 DOM 폴백)."""
    try:
        short = page.evaluate("""async (nm) => {
            try {
                const sess = await (await fetch('/api/auth/session', {credentials: 'include'})).json();
                if (!sess || !sess.accessToken) return null;
                const r = await fetch('/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=0',
                                      {credentials: 'include', headers: {Authorization: 'Bearer ' + sess.accessToken}});
                if (!r.ok) return null;
                const j = await r.json();
                for (const it of (j.items || [])) {
                    const g = it.gizmo && it.gizmo.gizmo;
                    if (g && g.display && g.display.name === nm && g.short_url) return g.short_url;
                }
            } catch (e) {}
            return null;
        }""", name)
        return f"{CHATGPT_URL}g/{short}/project" if short else None
    except Exception:
        return None


def find_project_url(page, name: str) -> str | None:
    """사이드바에서 '표시 이름이 정확히 name'인 프로젝트의 홈 URL을 회수(SPA 라우팅). 없으면 None.
    언어무관: 행(li)의 표시텍스트 == 이름으로 찾고(aria 로컬라이즈에 의존 안 함),
    같은 행의 '이름이 안 들어간 버튼'(=홈 버튼; 옵션버튼 aria엔 이름이 들어감)을 클릭한다.
    #2 대응: 목표가 보일 때까지 사이드바를 스크롤하며 폴링 → 가상화/지연으로 못 찾고 중복 생성하는 일 방지.
    #3 대응: 어떤 예외도 삼켜 None 반환(폴백 가능)."""
    try:
        for _ in range(12):
            # 1순위: 사이드바 안의 실제 href(클릭 휴리스틱보다 결정적) — 이름 정확 일치
            href = page.evaluate("""(nm) => {
                for (const root of document.querySelectorAll('nav, aside')) {
                    for (const a of root.querySelectorAll('a[href*="/g/g-p-"]')) {
                        const t = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (t === nm) return new URL(a.getAttribute('href'), location.origin).href;
                    }
                }
                return null;
            }""", name)
            if href:
                return href
            # 2순위(button-only UI): 사이드바 행의 표시텍스트 == 이름 → 홈 버튼 클릭(문서 전체 li는 보지 않음)
            clicked = page.evaluate("""(nm) => {
                const lis = [...document.querySelectorAll('nav li, aside li')];
                for (const li of lis) {
                    const first = ((li.innerText || '').trim().split('\\n')[0] || '').trim();
                    const btns = [...li.querySelectorAll('button[aria-label]')];
                    if (first === nm && btns.length) {
                        // 옵션버튼 aria엔 프로젝트명이 들어감 → 이름이 '안' 들어간 버튼이 홈(내비) 버튼
                        const home = btns.find(b => !((b.getAttribute('aria-label') || '').includes(nm))) || btns[0];
                        home.click();
                        return true;
                    }
                }
                return false;
            }""", name)
            if clicked:
                try:
                    page.wait_for_url("**/g/g-p-**", wait_until="commit", timeout=8000)
                except Exception:
                    pass
                time.sleep(1.2)
                u = current_url(page)  # page.url은 SPA pushState를 반영 못 함(스테일)
                return u if "/g/g-p-" in u else None
            # 가상화/접힘 대비: 한 화면씩 내려가며 재탐색(끝으로 점프하면 목록 중간을 건너뛴다)
            moved = page.evaluate("""() => { let moved = false;
                for (const el of document.querySelectorAll('nav *, aside *')) {
                    if (el.scrollHeight > el.clientHeight + 20) {
                        const next = Math.min(el.scrollHeight - el.clientHeight, el.scrollTop + Math.max(200, el.clientHeight * 0.8));
                        if (next > el.scrollTop) { el.scrollTop = next; moved = true; }
                    }
                }
                return moved; }""")
            if not moved:
                return None
            time.sleep(0.5)
    except Exception:
        return None
    return None


def create_project(page, name: str) -> str | None:
    """'새 프로젝트' 모달로 폴더명 프로젝트 생성 → 홈 URL 반환. 실패/미지원 시 None(호출자 폴백).
    제출은 다국어 텍스트 매칭 → 실패하면 Enter 폴백(언어무관)."""
    opened = page.evaluate("""(re) => { const rx = new RegExp(re, 'i');
        const b = [...document.querySelectorAll('button[aria-label]')].find(x => rx.test(x.getAttribute('aria-label') || ''));
        if (b) { b.click(); return true; } return false; }""", _NEW_PROJECT_RE)
    if not opened:
        return None  # '새 프로젝트' 버튼 없음(프로젝트 미지원 플랜/언어 불일치) → 일반 채팅 폴백
    try:
        # 모달의 유일한 visible text-input = 이름칸(컴포저는 contenteditable이라 input[type=text] 아님)
        name_input = page.locator('input[type="text"]:visible').last
        name_input.wait_for(state="visible", timeout=8000)
        name_input.click()
        name_input.fill(name)        # fill로 입력해야 제출 버튼이 enabled 된다
        time.sleep(0.4)
        submitted = page.evaluate("""(re) => { const rx = new RegExp(re, 'i');
            const btns = [...document.querySelectorAll('button')].filter(b => !b.disabled && rx.test((b.innerText || '').trim()));
            if (btns.length) { btns[btns.length - 1].click(); return true; } return false; }""", _CREATE_SUBMIT_RE)
        if not submitted:
            name_input.press("Enter")  # 텍스트 매칭 실패 시 언어무관 폴백
        page.wait_for_url("**/g/g-p-**", wait_until="commit", timeout=15000)
        time.sleep(2)
        u = current_url(page)
        return u if "/g/g-p-" in u else None
    except Exception:
        try:
            page.keyboard.press("Escape")  # 모달 닫고 폴백
        except Exception:
            pass
        return None


def ensure_project(page, name: str, cache_key: str, cache_path: Path) -> str | None:
    """프로젝트 홈 URL 확보: 캐시(절대경로 키) → 사이드바 탐색 → 생성.
    #1 대응: 캐시 키는 '절대경로'(cache_key) — 같은 폴더명의 다른 경로가 캐시를 공유하지 않는다.
    #3 대응: 함수 전체를 try/except로 감싸 어떤 예외도 None으로(호출자가 일반 채팅으로 폴백)."""
    try:
        with project_cache_lock(cache_path):
            return _ensure_project_locked(page, name, cache_key, cache_path)
    except Exception:
        return None


def _open_chat_home(page) -> bool:
    page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=30000)
    for _ in range(10):
        if find_visible_input(page) is not None:
            return True
        time.sleep(1)
    return False


def current_workspace_id(page) -> str | None:
    """활성 ChatGPT 워크스페이스 id — `_account` 쿠키(실측 2026-08-25, localStorage `_account`와 동일).
    개인↔팀 등 워크스페이스 전환이 프로젝트 접근성을 통째로 바꾸므로(2026-08-11 실사고) 캐시에 결속한다.
    실패는 None — 판정 강화용 신호일 뿐, None이면 기존 URL 검증 경로만으로 동작한다."""
    try:
        val = page.evaluate(
            """() => {
                const c = document.cookie.split('; ').find(x => x.startsWith('_account='));
                if (c) return decodeURIComponent(c.split('=').slice(1).join('='));
                try { return JSON.parse(localStorage.getItem('_account') || 'null'); } catch (e) { return null; }
            }""")
        return val or None
    except Exception:
        return None


def _cache_record(value):
    """캐시 값 하위호환 파서: 구형 문자열(url) / 신형 dict({url, workspace_id}) → (url, workspace_id)."""
    if isinstance(value, dict):
        return value.get("url"), value.get("workspace_id")
    return value, None


def _ensure_project_locked(page, name: str, cache_key: str, cache_path: Path) -> str | None:
    """정책: ok→사용 / auth→중단 / unknown→캐시 보존·생성 금지(이번 런은 일반채팅 폴백) /
    dead→현 워크스페이스에서 탐색→생성, 대체물이 검증된 뒤에만 옛 캐시 제거.
    탐색·생성으로 얻은 URL도 같은 validator를 통과해야 캐시에 들어간다(오클릭·늦은 리다이렉트 고착 방지).
    워크스페이스 결속(P2): 캐시에 workspace_id를 저장, 현재 워크스페이스와 다르면 goto 없이 즉시 dead
    (죽은 URL을 열어 에러 팝업을 띄우는 단계 자체를 생략)."""
    ws_now = current_workspace_id(page)
    cache = _load_project_cache(cache_path)
    cached_rec = cache.get(cache_key)
    cached_url, cached_ws = _cache_record(cached_rec)
    cached_state = PROJECT_UNKNOWN
    if cached_url:
        if cached_ws and ws_now and cached_ws != ws_now:
            cached_state = PROJECT_DEAD
            print(f"  ℹ️  워크스페이스 변경 감지(캐시={cached_ws[:8]}… ≠ 현재={ws_now[:8]}…) → 현 워크스페이스에서 재탐색/재생성")
        else:
            cached_state = project_home_state(page, cached_url)
            if cached_state == PROJECT_OK:
                if ws_now and cached_ws != ws_now:
                    cache[cache_key] = {"url": cached_url, "workspace_id": ws_now}  # 구형 레코드 승격
                    _save_project_cache(cache_path, cache)
                return cached_url
            if cached_state == PROJECT_AUTH:
                return None
            print(f"  ℹ️  캐시된 프로젝트 판정={cached_state}" + (" → 재탐색/재생성" if cached_state == PROJECT_DEAD else " → 캐시 보존, 이번 런은 폴백"))
            if cached_state == PROJECT_UNKNOWN:
                return None

    candidate = find_project_url_api(page, name)  # API 1순위(현 오리진 페이지에서 즉시)
    if candidate and project_home_state(page, candidate) != PROJECT_OK:
        candidate = None
    if not candidate:
        if not _open_chat_home(page):
            return None
        candidate = find_project_url(page, name)  # DOM 폴백(구 UI/API 실패 대비)
        if candidate and project_home_state(page, candidate) != PROJECT_OK:
            candidate = None
    if not candidate:
        if not _open_chat_home(page):
            return None
        candidate = create_project(page, name)
        if candidate and project_home_state(page, candidate) != PROJECT_OK:
            candidate = None

    latest = _load_project_cache(cache_path)  # lock 안이지만 재읽기 — 항상 최신 dict에 갱신
    if candidate:
        latest[cache_key] = {"url": candidate, "workspace_id": ws_now} if ws_now else candidate
        _save_project_cache(cache_path, latest)
        return candidate
    if cached_url and cached_state == PROJECT_DEAD and latest.get(cache_key) == cached_rec:
        latest.pop(cache_key, None)  # 대체물을 못 얻었어도 확정 사망 캐시는 제거(에러 팝업 반복 방지)
        _save_project_cache(cache_path, latest)
    return None


# ===========================================================================
# main
# ===========================================================================
def main():
    ap = argparse.ArgumentParser(description="repomix → 구독 ChatGPT(GPT Pro, 최신 플래그십) 분석")
    ap.add_argument("--target", default=None, help="분석 대상 폴더(생략 시 프롬프트만 = 의견 모드)")
    ap.add_argument("--include", default=None, help='repomix --include 글롭')
    ap.add_argument("--ignore", default=None, help="repomix --ignore 글롭")
    ap.add_argument("--compress", action="store_true",
                    help="tree-sitter 골격만(토큰 절감) — 본문 제거되니 정확성 리뷰엔 쓰지 마라")
    ap.add_argument("--no-line-numbers", action="store_true",
                    help="라인번호 prefix 끄기(기본 on — AI가 파일:라인 인용하도록)")
    ap.add_argument("--style", default="markdown", choices=["xml", "markdown", "plain"])
    ap.add_argument("--token-budget", type=int, default=None)
    ap.add_argument("--attach", action="store_true",
                    help="첨부 강제 — 첨부 실패 시 붙여넣기 폴백 없이 중단(기본은 작은 pack에 한해 인라인 폴백)")
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--prompt-file", default=None)
    ap.add_argument("--model", default=None, help='추론단계 선택(예: "pro")')
    ap.add_argument("--require-model", default=None,
                    help='모델명 검증(예: "GPT-5.6") — 불일치 시 전송 중단')
    ap.add_argument("--force-answer-after", type=int, default=None,
                    help="N초 후 리즈닝 중이면 '지금 답변 받기' 재시도")
    ap.add_argument("--max-wait", type=int, default=None,
                    help=f"응답 최대 대기 초(기본 {MAX_WAIT_SECS}=20분; env INSANE_REVIEW_MAX_WAIT로도 설정)")
    ap.add_argument("--browser", default=None,
                    help="자동화에 쓸 브라우저(이름: chrome/comet/brave/edge/chromium/vivaldi 또는 절대경로). "
                         "생략 시 config 저장값 → 첫 감지 브라우저. 항상 전용 프로필로 실행")
    ap.add_argument("--list-browsers", action="store_true",
                    help="이 OS에 설치된 크로미움 계열 브라우저 목록 출력(BROWSERS 라인)")
    ap.add_argument("--launch-browser", default=None, metavar="NAME|PATH",
                    help="지정 브라우저를 전용 프로필+디버그포트로 실행(빈 문자열이면 자동 선택). 성공 시 config에 저장")
    ap.add_argument("--set-launch-mode", default=None, choices=list(LAUNCH_MODES),
                    help="전용 브라우저 실행 방식을 config에 저장(최초 1회 선택). "
                         "foreground=창 뜨고 포커스 가져감 / background=창 뜨되 포커스 안 뺏음(mac) / headless=창 없음")
    ap.add_argument("--project", default=None,
                    help="채팅을 묶을 ChatGPT 프로젝트 이름(기본: 현재 폴더명). 폴더별로 채팅이 프로젝트 안에 정리됨")
    ap.add_argument("--no-project", action="store_true",
                    help="프로젝트 그룹핑 비활성화 — 일반 새 채팅으로 전송(기존 동작)")
    ap.add_argument("--pack-only", action="store_true")
    ap.add_argument("--keep-pack", action="store_true", help="전송 후 패킹 파일 보존(기본은 유지; 끄려면 --delete-pack)")
    ap.add_argument("--delete-pack", action="store_true", help="응답 회수 후 패킹 파일 삭제(시크릿 위생)")
    ap.add_argument("--out-dir", default=None,
                    help="출력 저장 폴더(기본: 현재 프로젝트의 .insane-review/; env INSANE_REVIEW_OUT)")
    ap.add_argument("--check-env", action="store_true")
    ap.add_argument("--ensure-env", action="store_true",
                    help="저장된 브라우저가 있고 CDP가 닫혀(down) 있으면 조용히 1회 자동 기동 후 점검 "
                         "(저장값-only·첫감지 폴백 없음; browser=wrong이면 자동기동 안 함)")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--council", action="store_true",
                    help="agent-council 멤버 모드: 로그는 stderr, 응답만 stdout")
    ap.add_argument("--harvest", default=None, metavar="CHAT_URL|MANIFEST",
                    help="전송 없이 기존 대화에서 완료된 응답만 회수(타임아웃 시 안내된 대화 URL 또는 manifest_*.json 경로)")
    ap.add_argument("--continue-chat", default=None, metavar="CHAT_URL|MANIFEST",
                    help="기존 대화에 후속 메시지를 보내고 그 턴만 회수(새 채팅 생성 없음)")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("prompt_args", nargs="*", help="프롬프트(위치인자 — council 호환)")
    args = ap.parse_args()

    if args.check_env:
        sys.exit(check_env(do_install=args.install))

    if args.ensure_env:
        # 저장값-only 자동기동: CDP가 '닫힘'(down)이고 저장된 브라우저가 해석되면 한 번만 띄운다.
        # browser=wrong(포트를 다른 프로세스가 점유)이거나 저장값이 없으면 자동기동하지 않고,
        # check_env가 상태만 보고한다 → 커맨드가 그때만 사용자에게 묻는다(최초 1회 온보딩).
        if not is_port_open(CDP_PORT):
            saved = _load_config().get("browser")
            if saved:
                r = resolve_browser(saved)   # 인자 지정 경로 → 첫감지 폴백 없음(저장값-only)
                if r:
                    launch_browser_exe(r[1], r[0])
        sys.exit(check_env(do_install=args.install))

    if args.list_browsers:
        bs = detect_browsers()
        print("BROWSERS " + ",".join(f"{n}={p}" for n, p in bs))
        for n, p in bs:
            print(f"  • {n}: {p}")
        if not bs:
            print("  (설치된 크로미움 계열 브라우저를 찾지 못함)")
        sys.exit(0)

    if args.set_launch_mode:
        save_launch_mode(args.set_launch_mode)
        print(f"LAUNCH_MODE {args.set_launch_mode} 저장됨 (~/.insane-review/config.json)")
        if args.set_launch_mode == "headless":
            print("  주의: ChatGPT가 헤드리스를 차단하면 로그인·전송이 실패할 수 있다. "
                  "--check-env로 login=ok 확인 후 사용할 것.")
        return 0

    if args.launch_browser is not None:
        resolved = resolve_browser(args.launch_browser or None)
        if not resolved:
            avail = ", ".join(n for n, _ in detect_browsers()) or "없음"
            sys.exit(f"❌ 브라우저를 찾지 못함 (지정='{args.launch_browser}', 감지=[{avail}])")
        name, path = resolved
        if launch_browser_exe(path, name):
            save_browser_choice(name)
            print(f"STATUS_LAUNCH ok browser={name}")
            sys.exit(0)
        sys.exit("❌ 브라우저 실행/CDP 확인 실패")

    # --require-model은 모델 검증 경로(select_model)에서만 효력 → --model 없이 단독 사용 시 검증이 통째로
    # 스킵되는 fail-open을 차단(fail-closed). 모델/추론단계를 함께 지정해야 검증이 돈다.
    if args.require_model and not args.model:
        sys.exit('❌ --require-model은 --model과 함께 써야 합니다(모델/추론단계를 선택·검증하는 경로).\n'
                 '     예: --model pro --require-model "GPT-5.6"')

    # --harvest: 전송 없이 기존 대화에서 회수만 — 패킹/프롬프트/프로젝트 진입 불필요
    harvest_url = None
    if args.harvest:
        _h = Path(args.harvest).expanduser()
        if _h.exists():
            try:
                harvest_url = json.loads(_h.read_text(encoding="utf-8")).get("chat_url")
            except Exception:
                sys.exit(f"❌ manifest 파싱 실패: {_h}")
        else:
            harvest_url = args.harvest
        if not harvest_url or not CONV_URL_RE.search(harvest_url):
            sys.exit(f"❌ --harvest 인자가 대화 URL(/c/<id>)이 아님: {args.harvest}")
        args.target = None  # 회수 모드는 전송이 없다 — 패킹 생략

    # --continue-chat: 결속된 대화에 이어서 보낸다. 새 채팅을 만들지 않으므로 컨텍스트가
    # 대화에 남고, 매 호출 전체 트랜스크립트를 다시 보낼 필요가 없다.
    continue_url = None
    if args.continue_chat:
        _c = Path(args.continue_chat).expanduser()
        if _c.exists():
            try:
                continue_url = json.loads(_c.read_text(encoding="utf-8")).get("chat_url")
            except Exception:
                sys.exit(f"❌ manifest 파싱 실패: {_c}")
        else:
            continue_url = args.continue_chat
        if not continue_url or not CONV_URL_RE.search(continue_url):
            sys.exit(f"❌ --continue-chat 인자가 대화 URL(/c/<id>)이 아님: {args.continue_chat}")
        if harvest_url:
            sys.exit("❌ --harvest(회수 전용)와 --continue-chat(전송)은 함께 쓸 수 없습니다.")
        args.target = None  # 이어 보내기는 첨부 없이 프롬프트만 보낸다

    real_stdout = sys.stdout
    if args.council:
        sys.stdout = sys.stderr

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"  출력 폴더: {out_dir}")
    # 폴더명→프로젝트URL 캐시(per-repo) — 평소엔 사이드바 안 건드리고 바로 프로젝트로 goto
    project_cache_path = out_dir / "projects.json"
    # #4: 자동 이름은 '폴더명 · 경로해시8'. 원격(ChatGPT) 프로젝트 탐색은 표시이름으로만 매칭하므로,
    # 이름에 경로 식별자가 없으면 동명 다른 폴더(/a/api, /b/api)가 같은 원격 프로젝트로 병합된다.
    # 사용자가 --project로 명시하면 그 이름 그대로(사용자 의도 존중).
    if args.project:
        project_name = args.project
    else:
        _ph = hashlib.sha256(str(Path.cwd().resolve()).encode("utf-8")).hexdigest()[:8]
        project_name = f"{Path.cwd().name} · {_ph}"
    # 캐시 키 = 절대경로::이름 — 동명 다른 폴더도, 같은 폴더의 다른 --project도 충돌하지 않음
    project_cache_key = f"{Path.cwd().resolve()}::{project_name}"
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
    if harvest_url:
        label = "harvest"
        prompt = f"(harvest) {harvest_url}"

    resolved_browser = resolve_browser(args.browser)
    bname = resolved_browser[0] if resolved_browser else (args.browser or "자동감지")
    print(f"\n[2/3] 브라우저 준비 ({bname})")
    if not ensure_browser(args.browser):
        sys.exit(1)
    # 명시적 지정(--browser)일 때만 영속화 — 자동감지 폴백을 사용자 선택처럼 굳히지 않는다.
    if args.browser and resolved_browser:
        save_browser_choice(resolved_browser[0])

    print("\n[3/3] ChatGPT 투입 & 응답 회수")
    print("  ⚠️  회수가 끝날 때까지 전용 브라우저 창을 조작하지 마세요(이탈 시 자동 복귀하지만 오염 위험)")
    response = ""
    conv_url = harvest_url or continue_url   # 결속된 대화 URL — 이후 시도는 '회수 재시도'(재전송 금지)
    base_ids_snapshot: set | None = (set() if harvest_url else None)
    continue_sent = False           # 이어 보내기는 판당 정확히 한 번 — 재시도는 회수만
    sent_unknown = False
    quota_hit = False
    manifest_path = out_dir / f"manifest_{label}_{run_tag}.json"
    # Pro는 20~60분이 정상 범위 — 명시값(--max-wait/env) 없을 때만 기본 상향
    mw_eff = args.max_wait
    if (mw_eff is None and "INSANE_REVIEW_MAX_WAIT" not in os.environ
            and args.model and args.model.strip().lower() == "pro"):
        mw_eff = PRO_MAX_WAIT_SECS
        print(f"  ⏲  Pro 추론단계 → 최대 대기 {PRO_MAX_WAIT_SECS}s 자동 상향(--max-wait/env가 우선)")
    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        if response:
            break  # 회수 경로가 continue로 성공을 들고 올라온 경우
        if attempt > 1:
            print(f"  ↻ 재시도 {attempt - 1}/{args.retries} ...")
            time.sleep(3)
        try:
            with sync_playwright() as pw:
                browser = connect_cdp(pw)
                ctx = pick_context(browser)
                if ctx is None:
                    raise RuntimeError("브라우저 context 없음 (로그인된 Comet/Chrome 필요)")
                page = ctx.new_page()
                hide_browser_if_background()  # 새 탭 생성이 앱을 앞으로 끌어올리므로 즉시 재숨김
                _guard_dialogs(ctx, page)
                try:
                    if conv_url:
                        # ── 회수 경로(재전송 없음): 결속된 대화 URL로 가서 이어서/다시 대기 ──
                        # 타임아웃·예외 후 재시도와 --harvest가 모두 이 경로 — 중복 채팅 생성 원천 차단.
                        if continue_url and not continue_sent:
                            print(f"  ➕ 이어 보내기: {conv_url}")
                        else:
                            print(f"  🔁 회수 모드(재전송 없음): {conv_url}")
                        page.goto(conv_url, wait_until="load", timeout=60000)
                        time.sleep(2)
                        if login_state(page) == "no":
                            raise RuntimeError("ChatGPT 로그인 벽 감지 — 해당 브라우저에서 chatgpt.com 로그인 확인")
                        # 이어 보내기는 이 판에서 정확히 한 번. 재시도는 전송 없이 회수만
                        # 한다 — 중복 전송은 구독 메시지를 두 번 쓰는 것이다.
                        sent_now = False
                        base_user = base_assistant = base_copy = 0
                        if continue_url and not continue_sent:
                            for _ in range(10):
                                if find_input(page):
                                    break
                                time.sleep(1)
                            if find_input(page) is None:
                                raise RuntimeError("이어 보낼 대화의 컴포저 미확인 → 전송 중단(fail-closed)")
                            base_user = count_msgs_strict(page, USER_MSG_SELECTORS)
                            base_assistant = count_msgs_strict(page, ASSISTANT_MSG_SELECTORS)
                            base_copy = count_msgs_strict(page, COPY_BTN_SELECTORS)
                            base_ids_snapshot = msg_id_set(page)
                            put_text(page, prompt)
                            if not composer_has_prompt(page, prompt):
                                clear_composer(page)
                                put_text(page, prompt)
                                if not composer_has_prompt(page, prompt):
                                    raise RuntimeError("프롬프트가 입력창에 온전히 안 들어감 → 중단(fail-closed)")
                            click_send(page)
                            continue_sent = sent_now = True
                            print("  ✓ 이어 보냄 — 새 채팅을 만들지 않았다")
                        status, text, conv_url = wait_for_turn_response(
                            page, force_after=args.force_answer_after, max_wait=mw_eff,
                            base_user=base_user, base_assistant=base_assistant,
                            base_copy=base_copy, conv_url=conv_url,
                            base_ids=base_ids_snapshot, skip_sent_check=not sent_now)
                        if status == "quota":
                            print("  ⛔ 사용량 한도 감지 — 회수 재시도 중단(한도 해제 후 --harvest 재실행)")
                            break
                        if status == "timeout":
                            print(f"  ⚠️  타임아웃 — 다음 시도도 같은 채팅 회수 재시도: {conv_url}")
                            continue
                        if status == "ok" and text and text.strip():
                            response = text
                        else:
                            print(f"  ⚠️  응답 비었거나 너무 짧음(status={status}) → 회수 재시도")
                        continue  # 회수 경로 종결(전송 경로 진입 금지) — 성공 시 루프 상단에서 break
                    else:
                        page.goto(CHATGPT_URL, wait_until="load", timeout=60000)
                        time.sleep(3)
                        for _ in range(10):
                            if find_input(page):
                                break
                            time.sleep(1)
                        _lst = login_state(page)
                        if _lst != "ok":
                            raise RuntimeError(
                                "ChatGPT 로그인 벽 감지 — 해당 브라우저에서 chatgpt.com 로그인 확인" if _lst == "no"
                                else "ChatGPT 컴포저 미확인(로딩 지연/CF 챌린지 가능) — 전용 브라우저 창 상태 확인 후 재시도")

                        # 프로젝트 그룹핑(기본 on): 현재 폴더명 프로젝트로 채팅을 정리(일반 채팅목록 오염 방지).
                        # 어떤 실패(예외 포함)에도 하드중단 X — 컴포저가 확인되는 일반 채팅으로 폴백(#3).
                        if not args.no_project:
                            proj_url = ensure_project(page, project_name, project_cache_key, project_cache_path)
                            entered = False
                            if proj_url:
                                try:
                                    # 진입도 같은 validator — id 유지+가시 컴포저+차단 없음(숨은 컴포저로 오판 금지)
                                    entered = project_home_state(page, proj_url) == PROJECT_OK
                                except Exception as pexc:
                                    print(f"  ⚠️  프로젝트 진입 예외({str(pexc)[:50]})")
                                    entered = False
                            if entered:
                                print(f"  🗂  프로젝트 '{project_name}'에 채팅 정리 → {proj_url}")
                            else:
                                # 폴백: 프로젝트 미확보/진입 실패 모두 일반 채팅으로(컴포저 보장)
                                print(f"  ⚠️  프로젝트 '{project_name}' 사용 불가 → 일반 채팅으로 진행(폴백)")
                                try:
                                    page.goto(CHATGPT_URL, wait_until="load", timeout=60000)
                                    time.sleep(2)
                                    for _ in range(10):
                                        if find_input(page):
                                            break
                                        time.sleep(1)
                                except Exception:
                                    pass

                        # Chat/Work 게이트 — 모델 스위처를 열기 '전에' 보정한다.
                        # Work 모드엔 Pro 눈금 자체가 없어 슬라이더 인덱스 계산이 무의미해진다.
                        chat_ok, seen_mode = ensure_chat_mode(page)
                        if not chat_ok and (args.model or "").lower() == "pro":
                            raise RuntimeError(
                                f"Chat 모드 전환 실패(현재='{seen_mode or '미상'}') — Work 모드엔 Pro가 없다 → 전송 중단(fail-closed)")

                        print(f"  현재 pill: {read_model_pills(page)}")
                        if args.model:
                            print(f"  모델/추론단계 선택: '{args.model}'"
                                   + (f" (모델명 검증='{args.require_model}')" if args.require_model else ""))
                            verified, v_name = select_model(page, args.model, require_model=args.require_model)
                            if not verified:
                                raise RuntimeError(f"모델/추론단계 검증 실패 (model={args.model}, require={args.require_model}) — 전송 중단")
                            verified_model_name = v_name

                        # 본문은 '첨부'가 기본. 첨부 실패 시:
                        #  - --attach면 fail-closed(중단)
                        #  - 아니면 pack이 상한 내일 때만 프롬프트에 인라인 붙여 폴백, 초과면 fail-closed(잘린 전송 방지)
                        send_prompt = prompt
                        if pack_path is not None:
                            if attach_file(page, pack_path):
                                if not args.no_project:
                                    # 같은 프로젝트의 옛 채팅/파일을 근거로 쓰는 오염 방지(2026-07-19 카운슬 P2)
                                    send_prompt = prompt + PROJECT_SCOPE_GUARD
                            else:
                                if args.attach:
                                    raise RuntimeError("코드 첨부 확인 실패 + --attach(첨부 강제) → 중단(fail-closed)")
                                send_prompt = build_paste_fallback(prompt, pack_path)
                                if send_prompt is None:
                                    raise RuntimeError("코드 첨부 실패 + pack이 커서 붙여넣기 폴백 불가 → 중단(fail-closed)")
                                print(f"  ↩︎  첨부 실패 → pack을 프롬프트에 인라인 붙여넣기 폴백({len(send_prompt):,}자, 상한 내)")

                        # 전송 직전 기준 포착(턴-스코프 결속): fail-closed 카운터 + message-id 집합(id-diff 판정용)
                        base_user = count_msgs_strict(page, USER_MSG_SELECTORS)
                        base_assistant = count_msgs_strict(page, ASSISTANT_MSG_SELECTORS)
                        base_copy = count_msgs_strict(page, COPY_BTN_SELECTORS)
                        base_ids_snapshot = msg_id_set(page)

                        put_text(page, send_prompt)
                        # 보낼 텍스트 '전체'가 입력창에 들어갔는지 검증 — 아니면 composer 비우고 1회 재입력, 그래도 불일치면 중단
                        # (첨부만/잘린 질문이 전송되어 '오염된 응답'을 성공저장하는 fail-open 차단)
                        if not composer_has_prompt(page, send_prompt):
                            clear_composer(page)
                            put_text(page, send_prompt)
                            if not composer_has_prompt(page, send_prompt):
                                raise RuntimeError("프롬프트가 입력창에 온전히 안 들어감 → 중단(첨부만/잘린 전송 방지, fail-closed)")
                        click_send(page)
                        manifest_written = False

                        def _persist_binding(url, _sp=send_prompt):
                            nonlocal manifest_written
                            if not manifest_written:
                                write_run_manifest(manifest_path, url, label, run_tag, _sp, pack_path)
                                manifest_written = True

                        status, text, conv_url = wait_for_turn_response(
                            page, force_after=args.force_answer_after, max_wait=mw_eff,
                            base_user=base_user, base_assistant=base_assistant,
                            base_copy=base_copy, base_ids=base_ids_snapshot, on_bound=_persist_binding)
                        if conv_url:
                            _persist_binding(conv_url)  # 결속 콜백이 못 돈 경로(전달된 URL) 보강 — 멱등
                        if status == "not_sent":
                            print("  ⚠️  user 턴 미생성(전송 안 됨) → 재시도(재전송)")
                            continue
                        if status == "sent_unknown_location":
                            print("  ⚠️  전송은 확인됐지만 대화 URL 포착 실패 — 중복 전송 방지를 위해 재전송하지 않고 종료")
                            sent_unknown = True
                            break
                        if status == "quota":
                            print("  ⛔ 사용량 한도 — 재시도 무의미, 중단(재전송 없음)")
                            quota_hit = True
                            break
                        if status == "timeout":
                            print("  ⚠️  타임아웃 — 미완성 응답은 성공저장 안 함(fail-closed)"
                                  + (f" → 다음 시도는 같은 채팅 회수 재시도: {conv_url}" if conv_url else " → 재시도"))
                            continue
                        if status == "ok" and text and text.strip():
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

    if quota_hit:
        hint = (f"\n   결속 채팅: {conv_url}\n   한도 해제 후 회수 시도: pack_and_ask.py --harvest '{conv_url}'"
                if conv_url else "")
        sys.exit("❌ ChatGPT 사용량 한도 도달 — 대기·재시도 중단(응답 미생성)." + hint)
    if sent_unknown:
        sys.exit("❌ 전송은 됐지만 대화 URL 미포착(sent-unknown-location) — 중복 방지 위해 재전송 안 함.\n"
                 "   ChatGPT 프로젝트에서 방금 생긴 채팅을 찾아 다음으로 회수하세요:\n"
                 "   pack_and_ask.py --harvest '<채팅URL>'")
    if not response:
        hint = (f"\n   결속 채팅: {conv_url}\n   나중에 회수: pack_and_ask.py --harvest '{conv_url}'"
                if conv_url else "")
        sys.exit("❌ 응답 회수 실패 (모든 재시도 소진)" + hint)

    # 회수 품질 경고(하드 차단 아님 — 카운슬 합의로 경고 강등): 파일-저장형/단답 응답 의심 패턴
    if len(response) < 500 and re.search(r"저장했습니다|다운로드|sandbox:/", response):
        print("  ⚠️  응답이 짧고 파일-저장형 패턴 포함 — 본문 대신 파일로 저장됐을 수 있음(채팅에서 직접 확인 권장)")

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
