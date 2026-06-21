English | [한국어](README.ko.md)

# insane-review

> **GPT-5.5 Pro has no API. This plugin uses it from inside Claude Code anyway.**

GPT-5.5 Pro lives only in the ChatGPT web app (subscription) — there is no official API, so you can't reach it from Codex CLI, `omc ask`, or an agent-council API provider. insane-review drives your **logged-in ChatGPT web session over CDP**: it packs the relevant code with repomix, sends it to Pro, and harvests the review. **Zero API cost** — it runs on your existing ChatGPT plan.

[Quick Start](#quick-start) • [Why insane-review?](#why-insane-review) • [How it works](#how-it-works) • [Features](#features) • [Tuning & timeouts](#tuning--timeouts) • [Requirements](#requirements)

---

## Quick Start

### 1. Add the marketplace (once)

```
/plugin marketplace add https://github.com/fivetaku/gptaku_plugins.git
```

### 2. Install

```
/plugin install insane-review
```

### 3. Restart Claude Code

Required for the plugin to load.

### 4. Prepare the browser bridge (once per machine)

Pro is web-only, so insane-review needs a real, logged-in browser on a debug port:

```bash
# launch Comet (or Chrome) with the CDP port, then log into chatgpt.com and pick GPT-5.5 Pro
open -a Comet --args --remote-debugging-port=9222

# verify everything is wired up (node/repomix, playwright, pyperclip, CDP browser)
python3 bin/pack_and_ask.py --check-env
```

### 5. Run it

```
/insane-review review the auth flow in src/auth
```

Or just say "have Pro review this" / "ask GPT-5.5 Pro about this design" — Claude figures out the target and packs it.

---

## Why insane-review?

- **The only way to reach Pro programmatically** — no API exists. A logged-in web session driven over CDP is the bridge, and it costs nothing beyond your subscription.
- **Claude picks the complete relevant set** — you don't have to hand-list files. For reviews it sends **full code** (no `--compress`, which strips function bodies and produces false "looks fine" verdicts) and audits the packed file list so nothing is silently missing.
- **fail-closed by design** — wrong model, unverified login, truncated prompt, an empty pack, or a previous turn's answer are all refused rather than silently sent or saved. Hardened across four rounds of Pro's own self-review (6 → 0 P0s).
- **Two roles, one engine** — a standalone reviewer when you ask for a fix/review, or a web-only member of [agent-council](references/council-setup.md) so Pro can debate alongside Codex/Gemini/others.
- **Citations you can follow** — line numbers are packed in, so Pro's findings come back as `file:line` you can jump to.

---

## How it works

```
"have Pro review this"  /  council member call
  ↓
Claude selects the COMPLETE relevant file set (full code — no --compress for reviews)
  ↓
repomix pack  (line numbers · secretlint · packed-file-list audit · token count)
  ↓
CDP-attach the logged-in ChatGPT session
Select GPT-5.5 + Pro effort  → re-open menu and VERIFY (mismatch = abort, fail-closed)
  ↓
Attach pack + prompt  → confirm the prompt actually landed in the composer  → send
  ↓
Wait for THIS turn to complete (turn-scoped: new assistant node + new copy button)
Optionally cut long reasoning early with --force-answer-after
  ↓
Harvest the answer → save to  ./.insane-review/response_*.md  (atomic write)
```

Output lands in the **current project's** `.insane-review/` folder (like kkirikkiri's `.kkirikkiri/`), never inside the plugin:

```
.insane-review/
├── pack_<target>_<ts>.md        # what was sent (chmod 600)
└── response_<target>_<ts>.md    # Pro's answer + verified model header
```

---

## Features

### Commands

| Command | What it does |
|---------|-------------|
| `/insane-review [target/question]` | Pack the relevant code and send it to GPT-5.5 Pro for review |
| natural language | "have Pro review this", "ask GPT-5.5 Pro about X" — same flow |

### Two modes

1. **Standalone reviewer** — you ask for a fix/review → Claude scopes the target → repomix pack → Pro analysis → applied back.
2. **agent-council member** — register Pro as a web-only council member so it debates with other models. See [`references/council-setup.md`](references/council-setup.md).

### Key flags

| Flag | Purpose |
|------|---------|
| `--target <dir>` | Folder to pack (omit for a prompt-only opinion) |
| `--include <glob>` / `--ignore <glob>` | Narrow the packed set |
| `--model pro` | Select the reasoning effort (e.g. Pro) |
| `--require-model "GPT-5.5"` | Verify the active model name — abort send on mismatch (fail-closed) |
| `--prompt "..."` / `--prompt-file` | The question |
| `--pack-only` | Just pack (inspect token count), don't send |
| `--council` | Council mode — response on stdout, logs on stderr |
| `--compress` | tree-sitter skeleton only — **don't use for reviews** (drops function bodies) |
| `--check-env` / `--install` | Diagnose / install the local toolchain |

---

## Tuning & timeouts

Response waiting and pack timeouts are adjustable from both the CLI and the environment — useful because Pro's full reasoning can take 10–15 minutes.

| Control | Default | What it does |
|---------|---------|-------------|
| `--max-wait <sec>` | `1200` (20 min) | Max time to wait for Pro's response before giving up (fail-closed, no partial save) |
| `INSANE_REVIEW_MAX_WAIT` | `1200` | Same as `--max-wait`, via environment |
| `--force-answer-after <sec>` | off | Soft cut: if Pro is still reasoning after N sec, click **"Get answer now"** so it answers **from the reasoning it has done so far** — a complete, saved answer (see below) |
| `INSANE_REVIEW_REPOMIX_TIMEOUT` | `300` | Max seconds for the repomix packing step |
| `--retries <n>` | `1` | Re-attempts if a send/harvest fails |

**Two different "timeouts" — don't confuse them:**

- **`--force-answer-after N` (soft cut, recommended for bounding cost).** Pro reasons for a long time; this clicks ChatGPT's *"Get answer now"* at N seconds, so Pro stops reasoning and replies based on **what it has reasoned up to that point**. That reply is a normal, complete turn — it's harvested and saved like any other. Use it to cap a council member at, say, 120s instead of waiting 10+ minutes.
- **`--max-wait N` (hard ceiling, fail-closed).** If the turn never completes within N seconds *and* no answer was forced, insane-review gives up **without saving** the half-streamed text — an incomplete answer is treated as a failure, not a result. This is intentional: it never hands you a truncated review pretending to be done.

Other environment overrides:

| Variable | Default | What it does |
|----------|---------|-------------|
| `INSANE_REVIEW_CDP_PORT` | `9222` | Browser remote-debugging port |
| `INSANE_REVIEW_COMET` / `INSANE_REVIEW_CHROME` | app default path | Browser executable path |
| `INSANE_REVIEW_REPOMIX_VERSION` | `1.15.0` | Pinned repomix version (reproducibility) |
| `INSANE_REVIEW_OUT` | `./.insane-review` | Output directory (also `--out-dir`) |

```bash
# example: give Pro up to 25 minutes, but cut reasoning at 5 minutes if it's still thinking
INSANE_REVIEW_MAX_WAIT=1500 python3 bin/pack_and_ask.py \
  --target . --include "src/**" --model pro --require-model "GPT-5.5" \
  --force-answer-after 300 --prompt "Where are the concurrency bugs?"
```

---

## Requirements

### Required

- [Claude Code](https://docs.anthropic.com/claude-code)
- Python 3.11+ with `playwright` and `pyperclip`
- Node.js / `npx`
- **A subscription ChatGPT account with GPT-5.5 Pro**, logged in inside Comet/Chrome launched on the debug port (`--remote-debugging-port=9222`)

### What's auto-handled vs. what you do

| Dependency | First-run behavior |
|------------|-------------------|
| **repomix** | **Fully automatic** — pulled on demand via `npx -y repomix@<pinned>`, never needs manual install |
| **playwright / pyperclip** | Checked on first use by `--check-env`; install them with `--install` (runs `pip install`). A normal run without them stops with a clear instruction (fail-closed) rather than failing mid-way |
| **Browser login + GPT-5.5 Pro** | **Manual** — can't be automated; you log into `chatgpt.com` and select Pro once |

```bash
# one shot: checks node/repomix, playwright, pyperclip, CDP browser — and installs the pip deps if missing
python3 bin/pack_and_ask.py --check-env --install
```

### Note

Web-UI automation is not endorsed by OpenAI's ToS and selectors may need maintenance when the ChatGPT DOM changes. Intended for personal subscription use only.

---

## License

MIT

---

<div align="center">

**No API. Still Pro.**

</div>
