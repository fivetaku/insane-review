[English](README.md) | [한국어](README.ko.md) | 中文 | [日本語](README.ja.md) | [Español](README.es.md)

# insane-review

<div align="center">
  <img src="assets/hero.png" width="860" alt="insane-review 电影感主视觉">
</div>

> **GPT Pro（当前旗舰模型的 Pro 推理档位 — 目前是 GPT-5.6 Sol）没有 API。这个插件照样让你在 Claude Code 里用上它。**

GPT Pro 只存在于 ChatGPT 网页版（订阅）中 — 没有官方 API，所以 Codex CLI、`omc ask`、agent-council 的 API provider 都够不着它。insane-review **通过 CDP 驱动你已登录的 ChatGPT 网页会话**：用 repomix 打包相关代码，发给 Pro，再把评审收回来。**API 成本为零** — 它跑在你现有的 ChatGPT 订阅上。

[快速开始](#快速开始) • [为什么选 insane-review](#为什么选-insane-review) • [工作原理](#工作原理) • [功能](#功能) • [调优与超时](#调优与超时) • [前置条件](#前置条件)

---

## 快速开始

### 1. 添加市场（仅需一次）

```
/plugin marketplace add https://github.com/fivetaku/gptaku_plugins.git
```

### 2. 安装

```
/plugin install insane-review
```

### 3. 重启 Claude Code

插件加载所必需。

### 4. 准备浏览器桥接（每台机器一次）

Pro 只能在网页上用，因此 insane-review 需要一个开着调试端口、已登录的真实浏览器：

```bash
# launch Comet (or Chrome) with the CDP port, then log into chatgpt.com and pick the Pro reasoning tier
open -a Comet --args --remote-debugging-port=9222

# verify everything is wired up (node/repomix, playwright, pyperclip, CDP browser)
python3 bin/pack_and_ask.py --check-env
```

### 5. 运行

```
/insane-review review the auth flow in src/auth
```

或者直接说"让 Pro 评审一下这个"/"就这个设计问问 GPT Pro" — Claude 会自行确定目标并打包。

---

## 为什么选 insane-review

- **以编程方式触达 Pro 的唯一途径** — 没有 API 可用。通过 CDP 驱动的已登录网页会话就是那座桥，除订阅费外零成本。
- **完整的相关文件集由 Claude 挑选** — 你不必手动列文件。评审发送的是**完整代码**（不用 `--compress`，它会剥掉函数体，导致虚假的"看起来没问题"结论），并对打包文件清单做审计，确保没有东西被悄悄漏掉。
- **fail-closed 的设计** — 模型不匹配、登录未验证、提示词被截断、空包、上一轮的旧回答，全部拒绝处理，而不是悄悄发送或保存。经 Pro 自己的四轮自评审加固（P0 从 6 降到 0）。
- **一个引擎，两种角色** — 你要修复/评审时它是独立评审员；也可以作为 [agent-council](references/council-setup.md) 的纯网页成员，让 Pro 与 Codex/Gemini 等模型同场辩论。
- **可追溯的引用** — 打包时带上行号，Pro 的发现会以 `file:line` 形式返回，可以直接跳转。

---

## 工作原理

```
"have Pro review this"  /  council member call
  ↓
Claude selects the COMPLETE relevant file set (full code — no --compress for reviews)
  ↓
repomix pack  (line numbers · secretlint · packed-file-list audit · token count)
  ↓
CDP-attach the logged-in ChatGPT session
Select Pro effort (flagship auto-follows; currently GPT-5.6 Sol)  → re-open menu and VERIFY (mismatch = abort, fail-closed)
  ↓
Attach pack + prompt  → confirm the prompt actually landed in the composer  → send
  ↓
Wait for THIS turn to complete (turn-scoped: new assistant node + new copy button)
Optionally cut long reasoning early with --force-answer-after
  ↓
Harvest the answer → save to  ./.insane-review/response_*.md  (atomic write)
```

输出保存在**当前项目**的 `.insane-review/` 文件夹里（与 kkirikkiri 的 `.kkirikkiri/` 同一模式），绝不会写进插件内部：

```
.insane-review/
├── pack_<target>_<ts>.md        # what was sent (chmod 600)
└── response_<target>_<ts>.md    # Pro's answer + verified model header
```

---

## 功能

### 命令

| 命令 | 作用 |
|---------|------|
| `/insane-review [target/question]` | 打包相关代码并发给 GPT Pro 评审 |
| 自然语言 | "让 Pro 评审一下这个"、"就 X 问问 GPT Pro" — 同一流程 |

### 两种模式

1. **独立评审员** — 你提出修复/评审请求 → Claude 圈定目标 → repomix 打包 → Pro 分析 → 结果落回代码。
2. **agent-council 成员** — 把 Pro 注册为纯网页 council 成员，与其他模型辩论。参见 [`references/council-setup.md`](references/council-setup.md)。

### 关键参数

| 参数 | 用途 |
|------|------|
| `--target <dir>` | 要打包的文件夹（省略则为纯提问的意见模式） |
| `--include <glob>` / `--ignore <glob>` | 收窄打包范围 |
| `--effort <档位>`（别名 `--model`） | 推理力度 — 默认 `pro`；`instant`/`medium`/`high`/`xhigh` 按原样应用，不匹配即中止 |
| `--require-model "<名称>"` | 固定模型名 — 不匹配即中止。**当前 UI 仅在 Pro 档显示模型名（如 `6`），其他档位无法读取，因此会始终中止。** 想自动跟随旗舰模型就不要指定 |
| `--prompt "..."` / `--prompt-file` | 问题 |
| `--pack-only` | 只打包（查看 token 数），不发送 |
| `--council` | council 模式 — 响应走 stdout，日志走 stderr |
| `--compress` | 仅 tree-sitter 骨架 — **评审时不要用**（会丢掉函数体） |
| `--check-env` / `--install` | 诊断 / 安装本地工具链 |

---

## 调优与超时

响应等待与打包超时都可以通过 CLI 和环境变量调节 — 这很有用，因为 Pro 的完整推理可能耗时 10–15 分钟。

| 控制项 | 默认值 | 作用 |
|---------|---------|------|
| `--max-wait <sec>` | `1200`（20 分钟） | 等待 Pro 响应的最长时间，超时即放弃（fail-closed，不保存半成品） |
| `INSANE_REVIEW_MAX_WAIT` | `1200` | `--max-wait` 的环境变量版 |
| `--force-answer-after <sec>` | 关闭 | 软截断：若 N 秒后 Pro 仍在推理，点击 **"Get answer now"**，让它**基于已完成的推理**作答 — 得到一份完整且会被保存的回答（见下文） |
| `INSANE_REVIEW_REPOMIX_TIMEOUT` | `300` | repomix 打包步骤的最大秒数 |
| `--retries <n>` | `1` | 发送/回收失败时的重试次数 |

**两种"超时"不是一回事 — 别混淆：**

- **`--force-answer-after N`（软截断，推荐用来给成本设上限）。** Pro 会推理很久；这个选项在第 N 秒点击 ChatGPT 的 *"Get answer now"*，让 Pro 停止推理并**基于到那一刻为止的推理**作答。那条回复是一轮正常、完整的对话，会像平常一样被回收和保存。用它把 council 成员限制在比如 120 秒，而不是干等 10 多分钟。
- **`--max-wait N`（硬上限，fail-closed）。** 如果 N 秒内这一轮始终没有完成，*而且*也没有触发强制作答，insane-review 会**不保存**那些流到一半的文本直接放弃 — 不完整的回答按失败处理，而不是当成结果。这是有意为之：它绝不会把被截断的评审假装成完成品交给你。

其他环境变量：

| 变量 | 默认值 | 作用 |
|------|---------|------|
| `INSANE_REVIEW_CDP_PORT` | `9222` | 浏览器远程调试端口 |
| `INSANE_REVIEW_COMET` / `INSANE_REVIEW_CHROME` | 应用默认路径 | 浏览器可执行文件路径 |
| `INSANE_REVIEW_REPOMIX_VERSION` | `1.15.0` | 固定的 repomix 版本（保证可复现） |
| `INSANE_REVIEW_OUT` | `./.insane-review` | 输出目录（也可用 `--out-dir`） |

```bash
# example: give Pro up to 25 minutes, but cut reasoning at 5 minutes if it's still thinking
INSANE_REVIEW_MAX_WAIT=1500 python3 bin/pack_and_ask.py \
  --target . --include "src/**" --model pro \
  --force-answer-after 300 --prompt "Where are the concurrency bugs?"
```

---

## 前置条件

### 必需

- [Claude Code](https://docs.anthropic.com/claude-code)
- Python 3.11+，装有 `playwright` 和 `pyperclip`
- Node.js / `npx`
- **一个订阅了 GPT Pro 的 ChatGPT 账号**，并已在以调试端口（`--remote-debugging-port=9222`）启动的 Comet/Chrome 中登录

### 自动处理的部分 vs. 需要你做的部分

| 依赖 | 首次运行行为 |
|------------|-------------------|
| **repomix** | **完全自动** — 按需通过 `npx -y repomix@<pinned>` 拉取，永远不需要手动安装 |
| **playwright / pyperclip** | 首次使用时由 `--check-env` 检查；用 `--install` 安装（执行 `pip install`）。缺依赖时正常运行不会中途崩溃，而是给出明确指引后停止（fail-closed） |
| **浏览器登录 + GPT Pro** | **手动** — 无法自动化；你需要登录 `chatgpt.com` 并选择 Pro，仅此一次 |

```bash
# one shot: checks node/repomix, playwright, pyperclip, CDP browser — and installs the pip deps if missing
python3 bin/pack_and_ask.py --check-env --install
```

### 说明

网页 UI 自动化并非 OpenAI ToS 所认可的方式，ChatGPT DOM 变更时选择器可能需要维护。仅供个人订阅用途。

---

## 许可证

MIT

---

<div align="center">

**No API. Still Pro.**

</div>
