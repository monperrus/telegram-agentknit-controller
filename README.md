# Telegram Agentknit Controller

A private, single-user Telegram bot that drives an agentknit-powered coding
agent (e.g. `agent-deepseek-v4-flash-zen`) instead of a Codex app-server. It
can also type text into a named tmux session. It is designed for a machine you
control: it is **not** a hardened multi-user bot or a public service.

Inspired by [telegram-codex-controller](https://github.com/monperrus/telegram-codex-controller).

---

## Feature list

### 1. Security and pairing

- **Single-user bot**: only the first chat to present the correct pairing code
  is authorized. All other chats are silently ignored (`permitted()`).
- **One-time pairing code** generated at install time
  (`openssl rand -hex 32` or `secrets.token_hex(32)`), via the `/pair <code>`
  command.
- **Config file mode 600** (`~/.config/telegram-agentknit-control.env`),
  written by the installer under `umask 077`.
- **No webhooks**: long-polling only, no public HTTP endpoint.
- **No multi-tenant**.

### 2. Interactive agent requests (agentknit)

- **Plain text** or **`/rc <prompt>`**: sends the prompt to the active agent via
  `AgentknitRuntime.run()`.
- **Rolling context window**: `fresh_session=False`, `max_context_turns=5`
  (the last 5 exchanges are kept as context).
- **Immediate acknowledgment** via a reaction (`setMessageReaction`, 🫡 then 👍
  as fallback, then the text `🫡` as a last resort).
- **Typing indicator** (`sendChatAction` typing) sent on receipt, then refreshed
  every 4 s and on every tool call.
- **File-change detection** from `tool_result` events of the `write_file`,
  `str_replace`, `t_update_file` tools; reports each unique path (`✏️ Changed`
  + basename + line counts `+added -removed` when available). Path resolution
  via: structured `files` array, then `diff_summary`, then parsing strings like
  `OK: wrote N bytes to /path` / `OK: replaced ... in /path`.
- **Cooperative cancellation** via `agentknit.CancelToken` with a per-turn
  timer (`TURN_TIMEOUT`, 180 s default); the agent reacts to interruption.
- **Friendly error messages**: raw agentknit/HTTP errors (429 rate limits, API
  errors, binary exits) are translated into concise Telegram-friendly text.

### 3. Durable tasks (`/task`) — SQLite queue

- **`/task <prompt>`**: queues a long-running task executed by a `TaskWorker`
  (daemon thread), **one at a time**, on a dedicated session per task
  (isolated, accumulated context).
- **`/tasks`**: lists the 6 most recent tasks as inline buttons (status icon +
  `T-<id>`, status, human-readable duration).
- **`/task detail <id>`**: detailed view (status, agent, runtime, age, input/
  output tokens, turns, tool calls, prompt snippet, latest message/checkpoint,
  error) with contextual buttons (Interrupt / Resume).
- **`/task pause <id>`**: pauses a `queued` task.
- **`/task resume <id> [new prompt]`**: resumes a
  `paused`/`interrupted`/`failed`/`completed`/`cancelled` task, optionally with
  a new prompt.
- **`/task interrupt <id>`**: sends `Ctrl-C` to the tmux session to interrupt
  the agent mid-turn.
- **`/task cancel <id>`**: requests cancellation (`queued`→`cancelled`
  immediately; `running`→`cancelling`, checked at each tool boundary).
- **`/task resume_prompt <id>` / `/task resumex <id>`**: resume variants
  triggered from inline buttons.
- **Persistent SQLite queue** (`TaskStore`):
  - Lifecycle: `queued → running → completed / failed / cancelled`
    (intermediate states `paused`, `interrupted`, `cancelling`).
  - **Concurrency cap** `TASK_MAX_QUEUE` (20 default) on
    `queued`/`running`/`cancelling` tasks.
  - **Crash recovery**: on startup, any `running` task is marked `interrupted`
    for explicit review/resume.
  - **Checkpointing**: the agent's last message is saved in the `checkpoint`
    column (visible via `/task detail`).
  - **Cumulative usage stats** per task: `tokens_input`, `tokens_output`,
    `turns`, `tool_calls`, `agent`.
  - **Automatic schema migration** via `ALTER TABLE` for new columns.
  - **Session resume**: the agentknit session is saved per task and restored via
    `init_session(session=...)` on resume.

### 4. Multi-model agent registry (`/agent`)

- Multiple selectable agentknit agents, switchable at runtime.
- Defined via **`TELEGRAM_AGENTKNIT_AGENTS`** (JSON array); otherwise two
  built-in agents: **DeepSeek** (from `MODEL`/`ENDPOINT` or the local binary)
  and **GLM-5.2** (endpoint `https://api.z.ai/api/coding/paas/v4`, key via
  keyring `z.ai`/`api_key`, with a system-prompt supplement carrying environment
  paths).
- Each agent: `key`, `label`, and either `spec_path` or `model`+`endpoint`;
  optional fields `keyring_service`+`keyring_username`, `key_env`,
  `system_prompt_supplement`.
- **`/agent`**: lists agents as inline buttons (the active one is marked 🟢);
  **`/agent <key>`**: selects the active agent.
- Choice **persisted** in controller state and restored on restart.
- **Per-task pinned agent**: changing the active agent mid-run does not affect
  a task already queued or running.
- Runtimes are built and cached lazily; the active agent is **pre-warmed** at
  startup (non-fatal on failure, so another agent can be selected via `/agent`).

### 5. tmux integration

- **`/tmux <text>`**: types the text + Enter into the configured tmux session,
  then a delayed screen capture (20 s, 30 lines) without blocking polling.
- **`/screen`**: recent output of the tmux session (120 lines, truncated to
  3800 characters).
- **`/status`**: tmux session presence, active agent + number configured, and
  task counts (pending, completed, failed, cancelled, total).
- **`/interrupt`**: sends `Ctrl-C` to the tmux session.

### 6. System and miscellaneous commands

- **`/restart`**: restarts the user systemd service
  `telegram-agentknit-controller.service`.
- **`/sh <shell command>`**: runs a shell command (30 s timeout, working
  directory = `WORKSPACE`), returns stdout/stderr.
- **`/start`, `/help`**: help message with an inline keyboard (Status, Screen,
  Tasks, Agent, Restart, Help).
- **One-letter shortcuts**: `h`=help, `s`=status, `v`=view screen, `i`=interrupt,
  `r`=restart, `t`=tasks (`t <prompt>` new task), `a`=agent (`a <key>` select),
  `m <text>`=tmux, `x <cmd>`=shell, `c <prompt>`=agent.
- **Inline buttons (callback queries)**: handled by `handle_callback()`, which
  acknowledges the query then reuses the same `handle()` dispatcher.

### 7. Telegram API client

- **Long-polling** (`getUpdates`, 30 s timeout); `allowed_updates` limited to
  `message` and `callback_query`.
- **Two separate connection pools** (`POLL_API`, `SEND_API`), keep-alive, each
  guarded by an `RLock`.
- **Retry** on `409 Conflict`.
- Reactions, typing indicators, inline keyboards, callback replies.

### 8. Installation and systemd service

- **`install.sh`**: prompts/secures `BOT_TOKEN`, generates `PAIR_CODE`,
  discovers executables, installs and starts the user service. Options:
  `--bot-token`, `--pair-code`, `--force`, `--no-start`; `BOT_TOKEN`/`PAIR_CODE`
  variables for non-interactive install.
- Files installed to `~/.local/share/telegram-agentknit-controller`; secrets in
  `~/.config/telegram-agentknit-control.env`.
- **`--check`**: validates the install without entering the polling loop (config
  file permissions, agentknit importability, agent registry, tmux, workspace,
  tmux session, Telegram bot).
- `loginctl enable-linger` for persistence after logout/reboot.
- systemd examples provided (user service and an adaptable system service).

### 9. Configuration (environment variables)

| Variable | Default | Role |
| --- | --- | --- |
| `TELEGRAM_AGENTKNIT_CONFIG` | `~/.config/telegram-agentknit-control.env` | Secret config file. |
| `TELEGRAM_AGENTKNIT_STATE` | `~/.local/state/telegram-agentknit-control.json` | Pairing + Telegram offset state. |
| `TELEGRAM_AGENTKNIT_TMUX_SESSION` | `web` | Target tmux session. |
| `TELEGRAM_AGENTKNIT_WORKSPACE` | `~` | Working directory. |
| `TELEGRAM_AGENTKNIT_SPEC` | *(none)* | Path to an agent spec JSON file (single-agent). |
| `TELEGRAM_AGENTKNIT_MODEL` | *(none)* | Model name (with `ENDPOINT`). |
| `TELEGRAM_AGENTKNIT_ENDPOINT` | *(none)* | Endpoint URL or `run://` binary path. |
| `TELEGRAM_AGENTKNIT_AGENTS` | *(built-in pair)* | JSON array of selectable agent specs. |
| `TELEGRAM_AGENTKNIT_SYSTEM_PROMPT_SUPPLEMENT` | *(none)* | Extra text appended to the system prompt. |
| `TELEGRAM_AGENTKNIT_TURN_TIMEOUT` | `180` | Timeout (s) for an interactive turn. |
| `TELEGRAM_AGENTKNIT_TASK_TIMEOUT` | `3600` | Timeout (s) for a durable task. |
| `TELEGRAM_AGENTKNIT_TASK_MAX_QUEUE` | `20` | Max number of queued tasks. |

The only required values in the `.env` file are `BOT_TOKEN` and `PAIR_CODE`.

## Prerequisites

- Python 3.9+ (stdlib only + `agentknit`).
- `tmux` on the `PATH`.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- A local install of agentknit (`pip install agentknit`).
- An agent spec JSON file, or a model name + endpoint URL.
- A tmux session (default: `web`) and a workspace for the agent.

## Installation

```sh
git clone <repository-url>
cd telegram-agentknit-controller
./install.sh
# or non-interactively:
BOT_TOKEN='your-token' ./install.sh
```

Validate without entering the polling loop:

```sh
~/.local/share/telegram-agentknit-controller/telegram-agentknit-control.py --check
```

Persistence after logout:

```sh
loginctl enable-linger "$USER"
```
