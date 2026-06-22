#!/usr/bin/env bash
# First-run setup for insane-review. Idempotent, non-blocking.
#   setup.sh            -> env checks + update-notifier hook + Python deps (once);
#                          prints "STAR_ASK <lang>" iff the user has not decided about starring.
#                          <lang> is a best-effort language code (ko/ja/en) detected from past
#                          Claude session transcripts — a fallback when the current conversation
#                          has no language signal yet.
#   setup.sh star yes   -> star both repos (own + marketplace) and record the decision.
#   setup.sh star no    -> record "declined"; star nothing.
# Browser (CDP :9222) launch and ChatGPT login are interactive and stay in the command's
# onboarding (Step 0.5) — bash can't drive them. The star question is asked by the command
# (AskUserQuestion is Claude-only); this script never stars without an explicit decision.
set -uo pipefail

PLUGIN="insane-review"
OWN_REPO="fivetaku/insane-review"
HUB_REPO="fivetaku/gptaku_plugins"

CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
HERE="$(cd "$(dirname "$0")" && pwd)"
MARKER_DIR="$HOME/.gptaku-setup"
SETUP_MARKER="$MARKER_DIR/$PLUGIN.json"
STAR_MARKER="$MARKER_DIR/$PLUGIN.star.json"
mkdir -p "$MARKER_DIR"

# --- detect a fallback UI language from past Claude session transcripts (best-effort) ---
# Counts Hangul / Kana / Latin letters in HUMAN-typed user text only (skips tool results,
# assistant turns and JSON structure, which are ASCII-heavy and would skew to English).
detect_lang() {
  command -v python3 >/dev/null 2>&1 || { echo en; return; }
  python3 - "$CONFIG_DIR/projects" 2>/dev/null <<'PY' || echo en
import sys, os, glob, json
base = sys.argv[1]
try:
    files = sorted(glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True),
                   key=os.path.getmtime, reverse=True)[:20]
except Exception:
    files = []
# Vote per message (presence of script), not per char — so a few large ASCII
# pastes (code, logs, specs) don't drown out many short typed Korean turns.
ko = ja = en = 0
msgs = 0
def vote(s):
    global ko, ja, en, msgs
    hk = hj = hl = False
    for ch in s:
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3: hk = True
        elif 0x3040 <= o <= 0x30FF: hj = True
        elif 65 <= o <= 90 or 97 <= o <= 122: hl = True
    if hk: ko += 1
    elif hj: ja += 1
    elif hl: en += 1
    if hk or hj or hl: msgs += 1
for f in files:
    if msgs >= 400: break
    try:
        fh = open(f, encoding="utf-8", errors="ignore")
    except Exception:
        continue
    for line in fh:
        if msgs >= 400: break
        try:
            m = json.loads(line).get("message")
        except Exception:
            continue
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            vote(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    vote(part.get("text", ""))
    fh.close()
if ko and ko >= ja and ko >= en: print("ko")
elif ja and ja >= ko and ja >= en: print("ja")
else: print("en")
PY
}

# --- star: record the user's decision (and star if yes). Called after the question. ---
if [ "${1:-}" = "star" ]; then
  DECISION="${2:-no}"
  ts=$(date +%s 2>/dev/null || echo 0)
  printf '{"star_decision":"%s","plugin":"%s","ts":%s}\n' "$DECISION" "$PLUGIN" "$ts" > "$STAR_MARKER"
  if [ "$DECISION" = "yes" ] && command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    for repo in "$OWN_REPO" "$HUB_REPO"; do
      gh api "user/starred/$repo" >/dev/null 2>&1 || gh api -X PUT "user/starred/$repo" >/dev/null 2>&1 || true
    done
  fi
  exit 0
fi

# --- first-run: update-notifier hook + Python deps for the web bridge (silent, once) ---
# 마커는 '핵심 셋업이 실제로 성공'했을 때만 기록한다 — 실패해도 마커를 남기면 다음 실행이
# 복구(deps 재설치·hook 등록)를 영영 건너뛴다(이전 버그). 실패 시 마커 미기록 → 다음 실행 재시도.
if [ ! -f "$SETUP_MARKER" ]; then
  SETUP_OK=1
  # update-notifier hook (best-effort): node가 있고 체크 스크립트가 '실제로 복사돼 자리잡았을' 때만 등록.
  # cp가 실패/스킵됐는데 hook을 등록하면 없는 파일을 가리키는 깨진 hook이 매 세션 에러를 낸다.
  if command -v node >/dev/null 2>&1; then
    SCRIPTS_DIR="$CONFIG_DIR/scripts"
    mkdir -p "$SCRIPTS_DIR"
    if [ -f "$HERE/gptaku-update-check.cjs" ] \
       && cp -f "$HERE/gptaku-update-check.cjs" "$SCRIPTS_DIR/gptaku-update-check.cjs" 2>/dev/null \
       && [ -f "$SCRIPTS_DIR/gptaku-update-check.cjs" ]; then
      CLAUDE_CONFIG_DIR="$CONFIG_DIR" node -e '
        const fs=require("fs"),path=require("path"),os=require("os");
        const cfg=process.env.CLAUDE_CONFIG_DIR||path.join(os.homedir(),".claude");
        const p=path.join(cfg,"settings.json");
        let d={}; try{d=JSON.parse(fs.readFileSync(p,"utf8"))}catch{}
        d.hooks=d.hooks||{};
        const ss=d.hooks.SessionStart=Array.isArray(d.hooks.SessionStart)?d.hooks.SessionStart:[];
        const has=ss.some(e=>((e&&e.hooks)||[]).some(h=>String((h&&h.command)||"").includes("gptaku-update-check")));
        if(!has){
          const cmd="node "+JSON.stringify(path.join(cfg,"scripts","gptaku-update-check.cjs"));
          ss.push({matcher:"*",hooks:[{type:"command",command:cmd,timeout:5}]});
          try{fs.writeFileSync(p,JSON.stringify(d,null,2))}catch{}
        }
      ' >/dev/null 2>&1 || true
    fi
  fi
  # Python deps for the GPT-Pro web bridge — CRITICAL(브리지가 못 돌면 무용). 설치 시도 후에도
  # import이 안 되면 SETUP_OK=0 → 마커를 안 남겨 다음 실행이 재시도한다. repomix는 npx -y로
  # 실행돼 사전설치 불필요. playwright는 pip 패키지만(CDP attach라 브라우저 다운로드 불필요).
  if command -v python3 >/dev/null 2>&1; then
    for mod in pyperclip playwright; do
      python3 -c "import $mod" >/dev/null 2>&1 || python3 -m pip install --quiet "$mod" >/dev/null 2>&1 || true
    done
    for mod in pyperclip playwright; do
      python3 -c "import $mod" >/dev/null 2>&1 || SETUP_OK=0
    done
  else
    SETUP_OK=0   # python3 없으면 브리지 의존성 설치 불가 → 마커 안 남기고 재시도
  fi
  # 핵심 셋업(deps)이 성공했을 때만 1회 마킹. 실패면 마커 미생성 → 다음 실행이 복구 재시도.
  if [ "$SETUP_OK" = "1" ]; then
    ts=$(date +%s 2>/dev/null || echo 0)
    printf '{"setup":true,"plugin":"%s","ts":%s}\n' "$PLUGIN" "$ts" > "$SETUP_MARKER"
  fi
fi

# --- star prompt signal: ask exactly once, until a decision is recorded ---
# Emit "STAR_ASK <lang>" so the command has a fallback language when the current
# conversation gives no signal yet (e.g. a bare first invocation).
[ -f "$STAR_MARKER" ] || echo "STAR_ASK $(detect_lang)"
exit 0
