# Telegram Agentknit Controller

A private, single-user Telegram bot that drives an agentknit-powered coding
agent (e.g. `agent-deepseek-v4-flash-zen`). It can also type text into a named
tmux session. It is designed for a machine you control: it is **not** a
hardened multi-user bot or a public service.

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
  as fallback, then the text `🫡` as a last resort via `acknowledge()`).
- **`change_summary(change)`**: formats a file change notification as
  `✏️ Changed\n<basename>  +<added> -<removed>` (or without line counts
  when unavailable). Reported per unique path per turn only once.
- **Typing indicator** (`sendChatAction` typing) sent on receipt, then refreshed
  every 4 s and on every tool call.
- **File-change detection** from `tool_result` events of the `write_file`,
  `str_replace`, `t_update_file` tools; reports each unique path (`✏️ Changed`
  + basename + line counts `+added -removed` when available). Path resolution
  via: structured `files` array, then `diff_summary`, then parsing strings like
  `OK: wrote N bytes to /path` / `OK: replaced ... in /path`.
- **Cooperative cancellation** via `agentknit.CancelToken` with a per-turn
  timer (`TURN_TIMEOUT`, 180 s default); the agent reacts to interruption.
- **Friendly error messages** via `_friendly_agent_error()`: raw agentknit/HTTP
  errors are translated into concise Telegram-friendly text. Recognized errors:
  - HTTP 429 / rate limits / `FreeUsageLimitError` → *"The model's free-usage
    rate limit was hit (HTTP 429). No reply was produced — please try again
    later."*
  - Subprocess binary exits (e.g. `Binary '…' exited N: …`) → stripped of the
    noisy prefix, keeping only the underlying message.
  - All other errors → the raw message text (or *"Agent request failed."*).

### 3. Durable tasks (`/task`) — SQLite queue

- **`/task <prompt>`**: queues a long-running task executed by a `TaskWorker`
  (daemon thread), **one at a time**, on a dedicated session per task
  (isolated, accumulated context).
- **`/tasks`**: lists the 6 most recent tasks as inline buttons (status icon +
  `T-<id>`, status, human-readable duration).
- **`/task detail <id>`**: detailed view showing:
  - Status (with status icon), agent name, runtime duration, total age
  - Token usage: input tokens, output tokens, total
  - Turns and tool calls count
  - Request prompt snippet (first 800 characters)
  - Latest message/checkpoint (last 1500 characters)
  - Error message if any (last 1000 characters)
  - Contextual inline buttons: Interrupt (for running tasks) or Resume
    (for completed/failed/cancelled/paused/interrupted tasks).
- Durations are formatted as human-readable strings by `human_duration()`:
  seconds → `Xs`, minutes → `Xm Ys` or `Xm`, hours → `Xh Ym` or `Xh`,
  days → `Xd Yh` or `Xd`, months → `Xmo Yd` or `Xmo`.
- **`/task pause <id>`**: pauses a `queued` task.
- **`/task resume <id> [new prompt]`**: resumes a
  `paused`/`interrupted`/`failed`/`completed`/`cancelled` task, optionally with
  a new prompt.
- **`/task interrupt <id>`**: sends `Ctrl-C` to the tmux session to interrupt
  the agent mid-turn.
- **`/task cancel <id>`**: requests cancellation (`queued`→`cancelled`
  immediately; `running`→`cancelling`, checked at each tool boundary).
- **`/task resume_prompt <id>`**: the Resume inline button. It only prints
  the /task resume <id>  prompt to copy, so the owner stays in control
  of when a task is actually re-queued (it never resumes directly).
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
- Runtimes are built and cached lazily (`RUNTIMES` pool); the active agent is
  **pre-warmed** at startup (non-fatal on failure, so another agent can be
  selected via `/agent`).
- **Dual runtime isolation**: the interactive lane and the `TaskWorker` lane
  each use their own private runtime cache (`RUNTIMES` vs `self._runtimes`), so
  a long-running task session never collides with interactive requests.
- **Per-task session persistence**: each task saves a deep copy of its
  agentknit session after every progress update, enabling full conversation
  history on resume via `init_session(session=...)`.

### 5. Projects (`/projects`)

A **project** is a folder under `$HOME` that is its own git repository (work-tree
root) with at least one configured remote — i.e. a real working tree, not just a
subdirectory of a larger repo. Selecting a project makes it the agent's working
directory: the agent's tools (file reads/writes, `execute_shell_command`) run
with that directory as their `cwd`, and the project's `AGENTS.md` (if present)
is picked up by the session's system prompt.

- **`/projects`** (`p` shortcut): discovers git projects under `$HOME` and lists
  them as inline buttons (one per project; the active one is marked 🟢). Tapping
  a button selects it. With no active project the agent runs in `WORKSPACE`
  (`$HOME` by default).
- **`/projects <name|path>`**: selects a project by folder name (relative to
  `$HOME`), `~/path`, or absolute path. Only valid git projects with a remote
  are accepted.
- **`/projects none`** (or `off`/`clear`): clears the selection; the agent
  falls back to `WORKSPACE`.
- The active project is `chdir`'d into before every interactive request and
  every task, and **persisted** in controller state so the choice survives
  restarts. `TELEGRAM_AGENTKNIT_PROJECT` sets a default project at startup.
- Selecting a different project drops the cached interactive runtimes so the
  next request rebuilds its session in the new cwd (picking up the new
  project's `AGENTS.md`).
- **Per-task pinned project**: each task records the active project at creation
  time, so switching projects later never moves a running task's files — the
  task always runs in the project it was queued in. `/task detail` shows the
  pinned project.
- `/status` reports the active project (and its cwd).

### 6. tmux integration

- **`/tmux <text>`**: types the text + Enter into the configured tmux session,
  then a delayed screen capture (20 s, 30 lines) without blocking polling.
- **`/screen`**: lists available tmux screens as inline buttons — sessions,
  windows, and individual panes. Tapping a button captures and returns that
  screen's content (120 lines, truncated to 3800 characters). When only one
  session/window exists, the content is returned directly without a menu.
- **`/screen_show <target>`**: invoked by the inline buttons (or usable
  directly) to capture a specific tmux target (e.g. `web:1`, `web:1.0`).
  Uses the same screen capture logic as `/screen`.
- **`/status`**: tmux session presence, active agent + number configured, and
  task counts (pending, completed, failed, cancelled, total).
- **`/interrupt`**: sends `Ctrl-C` to the tmux session.
- Helper functions used by the screen picker:
  - `list_tmux_sessions()` — enumerates all available tmux sessions.
  - `list_tmux_windows(session=None)` — lists windows within a session.
  - `list_tmux_panes(window_target=None)` — lists panes within a window.

### 7. System and miscellaneous commands

- **`/restart`**: restarts the user systemd service
  `telegram-agentknit-controller.service`.
- **`/sh <shell command>`**: runs a shell command (30 s timeout, working
  directory = `WORKSPACE`), returns stdout/stderr.
- **`/start`, `/help`**: help message with an inline keyboard (Status, Screen,
  Tasks, Agent, Restart, Help).
- **One-letter shortcuts**: `h`=help, `s`=status, `v`=view screen, `i`=interrupt,
  `r`=restart, `t`=tasks (`t <prompt>` new task), `a`=agent (`a <key>` select),
  `p`=projects (`p <name>` select), `m <text>`=tmux, `x <cmd>`=shell,
  `c <prompt>`=agent.
- **Inline buttons (callback queries)**: handled by `handle_callback()`, which
  acknowledges the query then reuses the same `handle()` dispatcher.

### 8. Telegram API client

- **Long-polling** (`getUpdates`, 30 s timeout); `allowed_updates` limited to
  `message` and `callback_query`.
- **Two separate connection pools** (`POLL_API`, `SEND_API`), keep-alive, each
  guarded by an `RLock`.
- **Retry** on `409 Conflict`.
- Reactions, typing indicators, inline keyboards, callback replies.

### 9. Installation and systemd service

- **`install.sh`**: prompts/secures `BOT_TOKEN`, generates `PAIR_CODE`,
  discovers executables, installs and starts the user service. Options:
  `--bot-token`, `--pair-code`, `--force`, `--no-start`; `BOT_TOKEN`/`PAIR_CODE`
  variables for non-interactive install.
- Files installed to `~/.local/share/telegram-agentknit-controller`; secrets in
  `~/.config/telegram-agentknit-control.env`.
- **`telegram-agentknit-control.env.example`**: a documented example config file
  with all optional `TELEGRAM_AGENTKNIT_*` variables commented out.
- **`--check`**: validates the install without entering the polling loop.
  Checks performed:
  - Config file permissions (must be mode 600).
  - `agentknit` Python package import and version.
  - Agent registry configuration (spec files exist or model+endpoint set).
  - `tmux` binary availability on `PATH`.
  - Workspace directory existence.
  - Discovered projects and the active project (if any).
  - tmux session availability (warning, not fatal).
  - Telegram bot API connectivity (`getMe`).
- `loginctl enable-linger` for persistence after logout/reboot.
- systemd examples provided:
  - **User service** (`systemd/telegram-agentknit-controller.user.service`):
    installed by `install.sh`, runs as the current user via `systemctl --user`.
  - **System service** (`systemd/telegram-agentknit-controller.service.example`):
    adaptable template for system-wide installation (requires editing
    `YOUR_USER` and paths).

### 10. Configuration (environment variables)

| Variable | Default | Role |
| --- | --- | --- |
| `TELEGRAM_AGENTKNIT_CONFIG` | `~/.config/telegram-agentknit-control.env` | Secret config file. |
| `TELEGRAM_AGENTKNIT_STATE` | `~/.local/state/telegram-agentknit-control.json` | Pairing + Telegram offset state. |
| `TELEGRAM_AGENTKNIT_TMUX_SESSION` | `web` | Target tmux session. |
| `TELEGRAM_AGENTKNIT_WORKSPACE` | `~` | Working directory. |
| `TELEGRAM_AGENTKNIT_PROJECT` | *(none)* | Default project selected at startup (folder name under `~`, `~/path`, or absolute path). |
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
