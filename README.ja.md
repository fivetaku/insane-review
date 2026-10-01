[English](README.md) | [한국어](README.ko.md) | [中文](README.zh.md) | 日本語 | [Español](README.es.md)

# insane-review

<div align="center">
  <img src="assets/hero.png" width="860" alt="insane-review シネマティックヒーロー">
</div>

> **GPT Pro（現時点の最新フラッグシップの Pro 推論ティア — 現在は GPT-5.6 Sol）に API はない。それでもこのプラグインは Claude Code の中から使う。**

GPT Pro は ChatGPT ウェブアプリ（サブスクリプション）にしか存在しない — 公式 API がないため、Codex CLI・`omc ask`・agent-council の API プロバイダからは届かない。insane-review は**ログイン済みの ChatGPT ウェブセッションを CDP で操作**する：関連コードを repomix でパッキングして Pro に送り、レビューを回収する。**API コストはゼロ** — 手持ちの ChatGPT プランの上で動く。

[クイックスタート](#クイックスタート) • [なぜ insane-review？](#なぜ-insane-review) • [動作の仕組み](#動作の仕組み) • [機能](#機能) • [チューニングとタイムアウト](#チューニングとタイムアウト) • [動作要件](#動作要件)

---

## クイックスタート

### 1. マーケットプレイスを追加（初回のみ）

```
/plugin marketplace add https://github.com/fivetaku/gptaku_plugins.git
```

### 2. インストール

```
/plugin install insane-review
```

### 3. Claude Code を再起動

プラグインの読み込みに必要です。

### 4. ブラウザブリッジを準備（マシンごとに 1 回）

Pro はウェブ専用のため、insane-review にはデバッグポートで起動したログイン済みの実ブラウザが必要です：

```bash
# launch Comet (or Chrome) with the CDP port, then log into chatgpt.com and pick the Pro reasoning tier
open -a Comet --args --remote-debugging-port=9222

# verify everything is wired up (node/repomix, playwright, pyperclip, CDP browser)
python3 bin/pack_and_ask.py --check-env
```

### 5. 実行

```
/insane-review review the auth flow in src/auth
```

または「Pro にこれをレビューさせて」「この設計について GPT Pro に聞いて」と自然に言うだけ — 対象の特定とパッキングは Claude がやります。

---

## なぜ insane-review？

- **Pro にプログラムから届く唯一の道** — API は存在しない。CDP で操作するログイン済みウェブセッションだけがブリッジであり、サブスクリプション以外のコストはかからない。
- **完全な関連ファイル一式の選定は Claude が担当** — ファイルを手で列挙する必要はない。レビューには**フルコード**を送る（`--compress` は関数本体を削り、誤った「問題なし」判定を招くため使わない）。パッキングされたファイル一覧を監査し、静かな抜け漏れを許さない。
- **設計思想は fail-closed** — モデル不一致、未検証のログイン、途切れたプロンプト、空のパック、前のターンの回答は、黙って送信・保存される代わりにすべて拒否される。Pro 自身のセルフレビュー 4 ラウンドで強化済み（P0 6 → 0）。
- **1 つのエンジンに 2 つの役割** — 修正/レビュー依頼に応える単独レビュアー、または [agent-council](references/council-setup.md) のウェブ専用メンバーとして Pro が Codex/Gemini などと議論する。
- **たどれる根拠** — 行番号ごとパッキングするため、Pro の指摘は `file:line` 形式で返り、そのままジャンプできる。

---

## 動作の仕組み

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

出力は**現在のプロジェクト**の `.insane-review/` フォルダに保存されます（kkirikkiri の `.kkirikkiri/` と同じパターン）。プラグイン内部には決して保存されません：

```
.insane-review/
├── pack_<target>_<ts>.md        # what was sent (chmod 600)
└── response_<target>_<ts>.md    # Pro's answer + verified model header
```

---

## 機能

### コマンド

| コマンド | 動作 |
|---------|------|
| `/insane-review [target/question]` | 関連コードをパッキングして GPT Pro にレビューを依頼 |
| 自然言語 | 「Pro にこれをレビューさせて」「X について GPT Pro に聞いて」 — 同じフロー |

### 2 つのモード

1. **単独レビュアー** — 修正/レビューを依頼 → Claude が対象を絞り込み → repomix パック → Pro が分析 → 反映。
2. **agent-council メンバー** — Pro をウェブ専用の council メンバーとして登録し、他モデルと議論させる。[`references/council-setup.md`](references/council-setup.md) を参照。

### 主なフラグ

| フラグ | 用途 |
|------|------|
| `--target <dir>` | パッキングするフォルダ（省略時はプロンプトのみの意見モード） |
| `--include <glob>` / `--ignore <glob>` | パッキング範囲を絞る |
| `--effort <段階>`（別名 `--model`） | 推論エフォート — 既定は `pro`。`instant`/`medium`/`high`/`xhigh` はそのまま適用し、不一致なら中止 |
| `--require-model "<名前>"` | モデル名を固定 — 不一致なら中止。**現在の UI は Pro 段階でのみモデル名（例: `6`）を表示し、他の段階では読み取れないため常に中止される。** フラッグシップ自動追従には指定しない |
| `--prompt "..."` / `--prompt-file` | 質問 |
| `--pack-only` | パッキングのみ（トークン数の確認）、送信しない |
| `--council` | council モード — 応答は stdout、ログは stderr |
| `--compress` | tree-sitter のスケルトンのみ — **レビューには使わない**（関数本体が消える） |
| `--check-env` / `--install` | ローカルツールチェーンの診断 / インストール |

---

## チューニングとタイムアウト

応答待ちとパッキングのタイムアウトは CLI と環境変数の両方から調整できます — Pro の完全な推論には 10〜15 分かかることがあるためです。

| コントロール | デフォルト | 動作 |
|---------|---------|------|
| `--max-wait <sec>` | `1200`（20 分） | Pro の応答を待つ最大時間。超えたら断念（fail-closed、部分保存なし） |
| `INSANE_REVIEW_MAX_WAIT` | `1200` | `--max-wait` の環境変数版 |
| `--force-answer-after <sec>` | off | ソフトカット：N 秒経っても推論中なら **"Get answer now"** をクリックし、**それまでに推論した内容にもとづいて**回答させる — 完結した回答として保存される（下記参照） |
| `INSANE_REVIEW_REPOMIX_TIMEOUT` | `300` | repomix パッキング工程の最大秒数 |
| `--retries <n>` | `1` | 送信/回収の失敗時の再試行回数 |

**2 つの「タイムアウト」は別物 — 混同しないこと：**

- **`--force-answer-after N`（ソフトカット、コストの上限設定に推奨）。** Pro は長時間推論する。このオプションは N 秒で ChatGPT の *"Get answer now"* をクリックし、推論を止めて**その時点までに推論した内容で**回答させる。その回答は通常の完結したターンであり、いつも通り回収・保存される。council メンバーを 10 分以上待つ代わりに、たとえば 120 秒で区切るために使う。
- **`--max-wait N`（ハードシーリング、fail-closed）。** N 秒以内にターンが完了せず、*force-answer もかかっていなければ*、insane-review はストリーミング途中のテキストを**保存せずに**断念する — 不完全な回答は結果ではなく失敗として扱う。これは意図的な設計だ：途切れたレビューを「完了」のふりをして渡すことは決してない。

その他の環境変数：

| 変数 | デフォルト | 動作 |
|------|---------|------|
| `INSANE_REVIEW_CDP_PORT` | `9222` | ブラウザのリモートデバッグポート |
| `INSANE_REVIEW_COMET` / `INSANE_REVIEW_CHROME` | アプリのデフォルトパス | ブラウザ実行ファイルのパス |
| `INSANE_REVIEW_REPOMIX_VERSION` | `1.15.0` | repomix のピン留めバージョン（再現性） |
| `INSANE_REVIEW_OUT` | `./.insane-review` | 出力ディレクトリ（`--out-dir` でも可） |

```bash
# example: give Pro up to 25 minutes, but cut reasoning at 5 minutes if it's still thinking
INSANE_REVIEW_MAX_WAIT=1500 python3 bin/pack_and_ask.py \
  --target . --include "src/**" --model pro \
  --force-answer-after 300 --prompt "Where are the concurrency bugs?"
```

---

## 動作要件

### 必須

- [Claude Code](https://docs.anthropic.com/claude-code)
- Python 3.11+ と `playwright`・`pyperclip`
- Node.js / `npx`
- **GPT Pro が使えるサブスクリプション ChatGPT アカウント**。デバッグポート（`--remote-debugging-port=9222`）で起動した Comet/Chrome 内でログイン済みであること

### 自動処理されるもの vs. 自分でやること

| 依存関係 | 初回実行時の挙動 |
|------------|-------------------|
| **repomix** | **完全自動** — `npx -y repomix@<pinned>` で必要時に取得。手動インストールは一切不要 |
| **playwright / pyperclip** | 初回使用時に `--check-env` でチェック。`--install` でインストール（`pip install` を実行）。未導入のまま通常実行すると、途中で壊れる代わりに明確な指示を出して停止する（fail-closed） |
| **ブラウザログイン + GPT Pro** | **手動** — 自動化不可。`chatgpt.com` へのログインと Pro の選択を 1 回だけ行う |

```bash
# one shot: checks node/repomix, playwright, pyperclip, CDP browser — and installs the pip deps if missing
python3 bin/pack_and_ask.py --check-env --install
```

### 注意

ウェブ UI の自動化は OpenAI の ToS が推奨するものではなく、ChatGPT の DOM が変わればセレクタの保守が必要になる場合があります。個人のサブスクリプション利用のみを想定しています。

---

## ライセンス

MIT

---

<div align="center">

**No API. Still Pro.**

</div>
