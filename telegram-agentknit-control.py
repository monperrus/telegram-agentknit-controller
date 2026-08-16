#!/usr/bin/env python3
"""Private Telegram controller for tmux and a local agentknit-powered agent."""
import http.client
import json
import os
import select
import shutil
import stat
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse

import agentknit

HOME = os.path.expanduser("~")
CONFIG_PATH = os.environ.get("TELEGRAM_AGENTKNIT_CONFIG",
    os.path.join(HOME, ".config", "telegram-agentknit-control.env"))
STATE_PATH = os.environ.get("TELEGRAM_AGENTKNIT_STATE",
    os.path.join(HOME, ".local", "state", "telegram-agentknit-control.json"))
SESSION = os.environ.get("TELEGRAM_AGENTKNIT_TMUX_SESSION", "web")
WORKSPACE = os.environ.get("TELEGRAM_AGENTKNIT_WORKSPACE", HOME)
AGENT_SPEC = os.environ.get("TELEGRAM_AGENTKNIT_SPEC", "")
# Optional default project (an absolute path, or a folder name under $HOME)
# selected at startup. A project is a working directory for the agent: a folder
# under $HOME that is a git repository with at least one configured remote.
DEFAULT_PROJECT = os.environ.get("TELEGRAM_AGENTKNIT_PROJECT", "")
AGENTKNIT_MODEL = os.environ.get("TELEGRAM_AGENTKNIT_MODEL", "")
AGENTKNIT_ENDPOINT = os.environ.get("TELEGRAM_AGENTKNIT_ENDPOINT", "")
SYSTEM_PROMPT_SUPPLEMENT = os.environ.get("TELEGRAM_AGENTKNIT_SYSTEM_PROMPT_SUPPLEMENT", "")

TURN_TIMEOUT = int(os.environ.get("TELEGRAM_AGENTKNIT_TURN_TIMEOUT", "180"))
TASK_STATE_PATH = os.environ.get("TELEGRAM_AGENTKNIT_TASK_STATE",
    os.path.join(HOME, ".local", "state", "telegram-agentknit-tasks.sqlite3"))
TASK_TIMEOUT = int(os.environ.get("TELEGRAM_AGENTKNIT_TASK_TIMEOUT", "3600"))
TASK_MAX_QUEUE = int(os.environ.get("TELEGRAM_AGENTKNIT_TASK_MAX_QUEUE", "20"))

# ── Agent registry ───────────────────────────────────────────────────────────
# The controller can talk to more than one agentknit-backed model. Each agent is
# a named spec (key, label, spec_path or model+endpoint, optional keyring/ key
# injection and system-prompt supplement). The active agent is selectable at
# runtime via the /agent command and persisted in the controller state.
#
# Override the built-in defaults with TELEGRAM_AGENTKNIT_AGENTS, a JSON array of
# agent spec objects, e.g.:
#   [{"key":"deepseek","model":"...","endpoint":"..."},
#    {"key":"glm-5.2","model":"glm-5.2","endpoint":"https://api.z.ai/api/coding/paas/v4",
#     "keyring_service":"z.ai","keyring_username":"api_key"}]
AGENTS = {}            # key -> normalised agent spec dict
AGENT_ORDER = []       # insertion order of agent keys (stable listings)
ACTIVE_AGENT = ""      # currently selected agent key
RUNTIMES = {}          # key -> AgentknitRuntime (lazily built & cached)
RUNTIMES_LOCK = threading.Lock()
# Direct Telegram messages share a single Agentknit session.  They must be
# processed end-to-end in arrival order; locking only run_turn() leaves a race
# in session preparation and event-handler registration.
INTERACTIVE_REQUEST_LOCK = threading.Lock()

# ── Project registry ─────────────────────────────────────────────────────────
# A "project" is a folder under $HOME that is a git repository with at least one
# configured remote. Each project is a candidate working directory for the agent
# (the agent's tools run with that project as their cwd). The active project is
# selectable at runtime via /projects and persisted in the controller state.
#
# ACTIVE_PROJECT is an absolute path (the selected project's cwd) or "" when no
# project is selected — in that case the agent uses WORKSPACE ($HOME by default).
ACTIVE_PROJECT = ""    # currently selected project path (absolute), or ""


def config():
    values = {}
    with open(CONFIG_PATH, encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
                # Export to environment so module-level os.environ.get() calls
                # can pick up values placed in the config file.
                os.environ[key] = value
    for required in ("BOT_TOKEN", "PAIR_CODE"):
        if not values.get(required):
            raise RuntimeError(f"Missing {required} in {CONFIG_PATH}")
    return values


CFG = {}
API_HOST = "api.telegram.org"
API_PREFIX = ""


def _is_agent_api_error(text):
    """Return True if *text* is a terminal API/subprocess error (not a warning).

    agentknit emits the same ``error`` event both for recoverable warnings
    (e.g. "Compaction failed: …", after which the turn keeps going) and for
    terminal failures (API errors, HTTP 429 rate-limits, the model binary
    exiting non-zero).  Only the latter should flip a turn into a failure.
    """
    low = (text or "").lower()
    return ("api error" in low or "http error" in low or "exited 1" in low
            or "429" in (text or "") or "too many requests" in low
            or "freesusagelimit" in low or "rate limit" in low)


def _friendly_agent_error(text):
    """Turn a raw agentknit/HTTP error into a concise Telegram-friendly message."""
    raw = text or ""
    low = raw.lower()
    if ("429" in raw or "too many requests" in low
            or "freesusagelimit" in low or "rate limit" in low):
        return ("The model's free-usage rate limit was hit (HTTP 429). "
                "No reply was produced — please try again later.")
    msg = raw
    # Drop the noisy "API error: Binary '…' exited N: " prefix when present.
    if msg.startswith("API error: Binary ") and " exited " in msg:
        marker = msg.find(" exited ")
        colon = msg.find(":", marker)
        if colon != -1:
            msg = msg[colon + 1:].strip()
    return msg or "Agent request failed."


class AgentknitRuntime:
    """
    Wraps agentknit programmatic API for use by the Telegram controller.

    - Loads the agent spec JSON on init via load_specification()
    - Creates an agentknit client via create_client()
    - Maintains an in-memory session dict
    - run() executes a turn and returns the final reply
    - Supports file-change notifications (via tool_result events)
    - Supports agent-message streaming (via content_delta / final_answer events)
    - Supports cancellation (via CancelToken)
    - Supports limiting accumulated context to the last N turns
      (general conversation threads the last 5 messages for context;
       task conversations are isolated per task, not accumulated).
    """

    def __init__(self, spec_path=None, model=None, endpoint=None,
                 system_prompt_supplement="", schema_overrides=None):
        self.spec_path = spec_path
        self.model = model
        self.endpoint = endpoint
        self.system_prompt_supplement = system_prompt_supplement
        # Extra keys (e.g. keyring_service / keyring_username / key_env) merged
        # into the loaded schema so credentials can be resolved per provider.
        self.schema_overrides = schema_overrides or {}
        self.schema = None
        self.client = None
        self.session = None
        self.lock = threading.RLock()
        # Track turns in the current session so we can limit context.
        self._turn_count = 0

    def _load_schema(self):
        """Load the agent spec schema from file or model+endpoint."""
        if self.schema is not None:
            return self.schema
        if self.spec_path:
            with open(self.spec_path, encoding="utf-8") as f:
                self.schema = json.load(f)
        elif self.model and self.endpoint:
            self.schema = agentknit.load_specification(self.model, self.endpoint)
        else:
            raise RuntimeError(
                "AgentknitRuntime requires TELEGRAM_AGENTKNIT_SPEC or "
                "TELEGRAM_AGENTKNIT_MODEL + TELEGRAM_AGENTKNIT_ENDPOINT"
            )
        # Merge provider-specific overrides (keyring/key_env hints, etc.) once,
        # before the schema is cached and the client is created.
        if self.schema_overrides:
            for key, value in self.schema_overrides.items():
                self.schema.setdefault(key, value)
        return self.schema

    def _ensure_client(self):
        """Create or return the agentknit client."""
        if self.client is not None:
            return self.client
        schema = self._load_schema()
        self.client = agentknit.create_client(schema)
        return self.client

    def _model_name(self):
        """Return the model name from the schema."""
        schema = self._load_schema()
        model = schema.get("model") or self.model or "default-model"
        return model

    def start(self):
        """Create a fresh session."""
        schema = self._load_schema()
        self._ensure_client()
        self.session = agentknit.init_session(
            schema,
            non_interactive=True,
            system_prompt_supplement=self.system_prompt_supplement,
            # Use a unique cache key per session start so prefix caching
            # is effective across turns within the same session.
            cache_key=f"telegram-agentknit-{int(time.time())}",
        )
        self._turn_count = 0

    def stop(self):
        """Clean up the current session."""
        self.session = None

    def _reset(self):
        """Create a new session, discarding the old one."""
        self.stop()
        self._turn_count = 0
        self.start()

    def run(self, prompt, on_file_changes=None, on_agent_message=None,
            timeout=TURN_TIMEOUT, cancelled=None, fresh_session=False,
            max_context_turns=0, session=None, on_result=None,
            on_tool_call=None):
        """
        Run a turn via agentknit.run_turn() with event subscriptions.

        If *fresh_session* is True, a new session is created before the turn,
        so no prior conversation context leaks into this request.

        If *max_context_turns* is > 0, the session is automatically reset when
        the number of user turns reaches that limit, keeping only the most
        recent exchanges as context.  This is used for general conversation
        (threads the last 5 messages for context).

        If *session* is provided, it restores that saved session via
        init_session(session=...) and replaces the current session (used by
        TaskWorker to resume a previous task with full conversation history).

        Durable tasks set *fresh_session* to True so each task conversation is
        isolated, not accumulated across tasks.

        If *on_result* is provided, it is called with the SessionResult after
        each turn completes (useful for capturing usage stats).

        If *on_tool_call* is provided, it is called with the tool name
        before each tool dispatch (useful for sending typing indicators).

        Returns the final reply text.
        """
        if session is not None:
            # Restore the saved session properly via agentknit's init_session
            # with the session= parameter.  This opens a fresh log file, resets
            # compaction state, and validates the session dict structure.
            self._ensure_client()
            # Strip any stale event handlers from the saved session before
            # restoration — fresh handlers will be subscribed below.
            session.pop("_event_handlers", None)
            self.session = agentknit.init_session(
                self._load_schema(),
                non_interactive=True,
                session=session,
                system_prompt_supplement=self.system_prompt_supplement,
                cache_key=f"telegram-agentknit-{int(time.time())}",
            )
            self._turn_count = 0
        elif fresh_session or self.session is None:
            self.start()
            self._turn_count = 0
        elif max_context_turns > 0 and self._turn_count >= max_context_turns:
            # Rolling context window: reset session when we've had enough turns.
            self.start()
            self._turn_count = 0

        client = self._ensure_client()
        model = self._model_name()
        cancel_token = agentknit.CancelToken()

        # Register event handlers on the session.
        file_lock = threading.Lock()
        reported_paths = set()

        def on_tool_result(event_type, data):
            """Detect file-writing tools and notify."""
            if event_type == "tool_result" and on_file_changes:
                name = data.get("name", "")
                # The agentknit tools that write files are named:
                #   "write_file"    (t_write)
                #   "str_replace"   (t_update)
                #   "t_update_file" (t_update_file alias)
                if name not in ("write_file", "str_replace", "t_update_file"):
                    return
                with file_lock:
                    path = None
                    added = 0
                    removed = 0
                    # Prefer the structured metadata that newer agentknit
                    # versions include in the event payload.
                    files_list = data.get("files")
                    diff_summary = data.get("diff_summary")
                    if files_list and len(files_list) > 0:
                        path = files_list[0]
                    if diff_summary and isinstance(diff_summary, dict):
                        added = diff_summary.get("added", 0)
                        removed = diff_summary.get("removed", 0)
                    # Fallback: parse the result string for a file path.
                    if not path:
                        result = data.get("result", "")
                        for line in result.splitlines():
                            line = line.strip()
                            if line.startswith("OK: wrote ") or line.startswith("OK: replaced "):
                                # "OK: wrote 123 bytes to /path"
                                # "OK: replaced 1 occurrence(s) (5 line(s), ...) in /path"
                                words = line.split()
                                if words and words[-1]:
                                    maybe_path = words[-1].rstrip(".")
                                    if os.path.sep in maybe_path or maybe_path.startswith("/"):
                                        path = maybe_path
                                        break
                    if path and path not in reported_paths:
                        reported_paths.add(path)
                        on_file_changes([{"path": path,
                                          "added": added,
                                          "removed": removed}])

        def on_content_delta(event_type, data):
            """Forward streaming text deltas to the agent message callback."""
            if event_type == "content_delta" and on_agent_message:
                text = data.get("text", "")
                if text:
                    on_agent_message(text)

        def on_tool_call_event(event_type, data):
            """Notify caller before a tool is dispatched."""
            if event_type == "tool_call" and on_tool_call:
                name = data.get("name", "")
                if name:
                    on_tool_call(name)

        # Track the last terminal API/subprocess error seen so the run() call
        # can surface it as a failure rather than returning an empty reply.
        # agentknit returns SessionResult with final_reply=None after a fatal
        # error (HTTP 429, binary exit 1, etc.), so without this the caller
        # would see an empty "" answer and a misleading "completed" message.
        error_holder = {}

        def on_error(event_type, data):
            text = data.get("text") or ""
            if _is_agent_api_error(text):
                error_holder["error"] = text

        # Subscribe to events.
        # Note: we do NOT subscribe to "final_answer" here because the
        # complete answer text is already returned from run_turn() below.
        # Subscribing would cause the answer to be sent twice — once via
        # on_agent_message (through the content_delta stream) and once
        # from the return value.
        agentknit.subscribe(self.session, "tool_result", on_tool_result)
        agentknit.subscribe(self.session, "content_delta", on_content_delta)
        agentknit.subscribe(self.session, "tool_call", on_tool_call_event)
        agentknit.subscribe(self.session, "error", on_error)

        # Set up cancellation.
        cancel_timer = None
        timer_lock = threading.Lock()
        timeout_reached = threading.Event()

        def cancel_on_timeout():
            with timer_lock:
                if cancel_token:
                    timeout_reached.set()
                    cancel_token.cancel()

        cancel_timer = threading.Timer(timeout, cancel_on_timeout)
        cancel_timer.daemon = True
        cancel_timer.start()

        # External cancellation check.
        def is_cancelled():
            if cancelled and cancelled():
                with timer_lock:
                    if cancel_token:
                        cancel_token.cancel()
                return True
            return False

        try:
            with self.lock:
                result = agentknit.run_turn(
                    client,
                    model,
                    self.session,
                    prompt,
                    cancel=cancel_token,
                )

            final_reply = result.final_reply or ""
            # If the model hit a terminal API/subprocess error (e.g. the
            # DeepSeek free-tier 429 "FreeUsageLimitError" rate limit), the
            # turn returns with final_reply=None and no exception.  Surface it
            # so callers can report a failure instead of an empty "completed"
            # reply (which previously read as a misleading "quota used").
            if not final_reply and error_holder.get("error"):
                raise RuntimeError(error_holder["error"])
            self._turn_count += 1
            # Notify caller with the full result for stats tracking.
            if on_result:
                on_result(result)
            return final_reply
        except KeyboardInterrupt:
            # The agentknit run_turn raises KeyboardInterrupt when the
            # CancelToken is triggered (cooperative cancellation).  This
            # happens when the timeout timer fires or an external cancel
            # request is made.  Convert to a clear timeout error if the
            # maximum time was reached, so the caller can send a
            # descriptive message to the user.
            self._reset()
            if timeout_reached.is_set():
                raise TimeoutError(
                    f"The request was terminated after reaching the maximum "
                    f"allowed time of {timeout} seconds."
                )
            raise
        except Exception as error:
            # If the subprocess binary crashed or timed out, reset for next time.
            self._reset()
            raise
        finally:
            cancel_timer.cancel()
            # Unsubscribe event handlers to avoid accumulation.
            try:
                agentknit.unsubscribe(self.session, "tool_result", on_tool_result)
                agentknit.unsubscribe(self.session, "content_delta", on_content_delta)
                agentknit.unsubscribe(self.session, "tool_call", on_tool_call_event)
                agentknit.unsubscribe(self.session, "error", on_error)
            except Exception:
                pass


def _glm_supplement():
    """System-prompt supplement for the GLM agent: explicit environment paths.

    Mirrors the standalone `agent-glm-5.2` wrapper, countering glm-5.2's habit
    of assuming HOME is /home/user.
    """
    return (
        "Environment paths (do NOT assume or guess these — use the values below):\n"
        f"- HOME: {HOME}\n"
        f"- Current working directory: {WORKSPACE}\n"
        f"Never assume the home directory is /home/user; it is {HOME}."
    )


def default_agents():
    """Built-in agent registry: the existing DeepSeek agent + GLM-5.2.

    DeepSeek is derived from the single-agent env config (TELEGRAM_AGENTKNIT_*)
    for backward compatibility; GLM-5.2 points at the z.ai coding endpoint and
    resolves its key via keyring (service "z.ai", username "api_key").
    """
    ds_model = AGENTKNIT_MODEL or (
        "run:///" + os.path.join(HOME, "bin",
                                 "opencode-free-deepseek-v4-flash-completions.py"))
    ds_endpoint = AGENTKNIT_ENDPOINT or ds_model
    return [
        {
            "key": "deepseek",
            "label": "DeepSeek V4 Flash",
            "spec_path": AGENT_SPEC or None,
            "model": ds_model,
            "endpoint": ds_endpoint,
        },
        {
            "key": "glm-5.2",
            "label": "GLM-5.2",
            "model": "glm-5.2",
            "endpoint": "https://api.z.ai/api/coding/paas/v4",
            "keyring_service": "z.ai",
            "keyring_username": "api_key",
            "system_prompt_supplement": _glm_supplement(),
        },
    ]


def normalize_agent_spec(item):
    """Validate a user/default agent spec dict and fill in defaults."""
    key = item.get("key") or item.get("name")
    if not key:
        raise ValueError("agent spec is missing a 'key'/'name'")
    spec = {
        "key": key,
        "label": item.get("label") or key,
        "spec_path": item.get("spec_path") or None,
        "model": item.get("model") or "",
        "endpoint": item.get("endpoint") or "",
        "keyring_service": item.get("keyring_service") or None,
        "keyring_username": item.get("keyring_username") or None,
        "key_env": item.get("key_env") or None,
        "system_prompt_supplement": item.get("system_prompt_supplement") or "",
    }
    if not (spec["spec_path"] or (spec["model"] and spec["endpoint"])):
        raise ValueError(
            f"agent '{key}' needs 'spec_path' or both 'model' and 'endpoint'"
        )
    return spec


def load_agents():
    """Populate the AGENTS registry from the env override or built-in defaults."""
    global AGENTS, AGENT_ORDER
    AGENTS = {}
    AGENT_ORDER = []
    override = os.environ.get("TELEGRAM_AGENTKNIT_AGENTS", "").strip()
    if override:
        try:
            items = json.loads(override)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"TELEGRAM_AGENTKNIT_AGENTS is not valid JSON: {error}")
        if not isinstance(items, list) or not items:
            raise RuntimeError(
                "TELEGRAM_AGENTKNIT_AGENTS must be a non-empty JSON array")
    else:
        items = default_agents()
    for item in items:
        spec = normalize_agent_spec(item)
        if spec["key"] in AGENTS:
            raise RuntimeError(f"duplicate agent key: {spec['key']}")
        AGENTS[spec["key"]] = spec
        AGENT_ORDER.append(spec["key"])


def resolve_agent(agent_key=None):
    """Return a valid agent key, defaulting to ACTIVE_AGENT then the first agent."""
    key = agent_key or ACTIVE_AGENT
    if key and key in AGENTS:
        return key
    return AGENT_ORDER[0] if AGENT_ORDER else None


def set_active_agent(agent_key, state):
    """Switch the active agent and persist the choice in *state*."""
    global ACTIVE_AGENT
    if agent_key not in AGENTS:
        return False
    ACTIVE_AGENT = agent_key
    state["agent"] = agent_key
    write_state(state)
    return True


def build_runtime(spec):
    """Construct (but not start) an AgentknitRuntime from an agent spec dict."""
    parts = []
    if SYSTEM_PROMPT_SUPPLEMENT:
        parts.append(SYSTEM_PROMPT_SUPPLEMENT)
    if spec.get("system_prompt_supplement"):
        parts.append(spec["system_prompt_supplement"])
    supplement = "\n\n".join(parts)
    overrides = {}
    for field in ("keyring_service", "keyring_username", "key_env"):
        if spec.get(field):
            overrides[field] = spec[field]
    return AgentknitRuntime(
        spec_path=spec.get("spec_path"),
        model=spec.get("model") or None,
        endpoint=spec.get("endpoint") or None,
        system_prompt_supplement=supplement,
        schema_overrides=overrides or None,
    )


def get_agent_runtime(agent_key=None, cache=None):
    """Return the lazily built & cached AgentknitRuntime for an agent, or None.

    On first use the runtime is constructed and started; a start failure is
    reported and None is returned without caching a broken runtime, so the next
    request can retry (and other agents remain usable via /agent).

    *cache* selects which runtime pool to use. The interactive lane uses the
    shared RUNTIMES pool; the TaskWorker passes its own private dict so its
    long-lived task session never collides with interactive sessions.
    """
    key = resolve_agent(agent_key)
    if not key:
        return None
    if cache is None:
        cache = RUNTIMES
    with RUNTIMES_LOCK:
        runtime = cache.get(key)
        if runtime is not None:
            return runtime
        spec = AGENTS[key]
        try:
            runtime = build_runtime(spec)
            runtime.start()
        except Exception as error:
            print(f"telegram-agentknit-control: agent '{key}' runtime failed: "
                  f"{error}", file=sys.stderr, flush=True)
            return None
        cache[key] = runtime
        return runtime


# ── Projects ─────────────────────────────────────────────────────────────────
# A "project" is a folder under $HOME that is a git repository with at least one
# configured remote. Selecting a project makes it the agent's working directory
# (cwd): the agent's tools (file reads/writes, shell commands) run with that
# directory as their cwd, and the project's AGENTS.md (if any) is picked up by
# the session's system prompt. The choice is persisted across restarts.

PROJECTS_LOCK = threading.Lock()


def _repo_toplevel(path):
    """Return the absolute git work-tree root containing *path*, or None.

    Uses git's own notion of the work-tree top level so that a directory that
    merely sits *inside* a larger repo (e.g. every subdirectory of a repo at /)
    is not mistaken for a repo of its own.
    """
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        top = result.stdout.strip()
        return os.path.abspath(top) if top else None
    except Exception:
        return None


def _git_remote(path):
    """Return the first fetch remote URL for a git repo at *path*, else None."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            if url:
                return url
        # Fall back to enumerating all remotes (origin might be named otherwise).
        result = subprocess.run(
            ["git", "-C", path, "remote", "-v"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            # "origin\thttps://...\t(fetch)"
            parts = line.split("\t")
            if len(parts) >= 2 and parts[-1].endswith("(fetch)"):
                return parts[1].rsplit("\t", 1)[-1].split()[0]
    except Exception:
        pass
    return None


def _is_git_project(path):
    """True if *path* is itself a git work-tree root with at least one remote.

    A folder that merely lives *inside* another repo does not count — *path*
    must be the repo's top level (so e.g. subdirectories of a repo at / are
    not mistaken for projects).
    """
    if not os.path.isdir(path):
        return False
    if _repo_toplevel(path) != os.path.abspath(path):
        return False
    return _git_remote(path) is not None


def discover_projects():
    """Return a list of known projects (folders under $HOME that are git repos).

    Each entry is a dict: {"name": <basename>, "path": <abs path>,
    "remote": <remote url>}. The list is sorted by name and includes the current
    working directory's project if applicable. Discovery scans the immediate
    subdirectories of $HOME (shallow); nested repos are not enumerated.
    """
    projects = []
    seen = set()
    candidates = [HOME]
    try:
        for entry in os.listdir(HOME):
            candidates.append(os.path.join(HOME, entry))
    except OSError:
        pass
    for path in candidates:
        if path in seen:
            continue
        if not os.path.isdir(path):
            continue
        # Only count a folder as a project if it is *itself* a git work-tree
        # root with a remote (so subdirectories of a larger repo are skipped).
        if not _is_git_project(path):
            continue
        remote = _git_remote(path)
        if remote:
            seen.add(path)
            projects.append({
                "name": os.path.basename(path) or path,
                "path": os.path.abspath(path),
                "remote": remote,
            })
    projects.sort(key=lambda p: p["name"].lower())
    return projects


def project_cwd():
    """The directory the agent should run in: the active project or WORKSPACE."""
    return ACTIVE_PROJECT or WORKSPACE


def active_project_name():
    """Human-readable name for the active project, or '' when none is set."""
    if ACTIVE_PROJECT:
        return os.path.basename(ACTIVE_PROJECT) or ACTIVE_PROJECT
    return ""


def _apply_project_cwd():
    """chdir into the active project (or WORKSPACE) so the agent's tools inherit it."""
    target = project_cwd()
    try:
        os.chdir(target)
    except OSError as error:
        print(f"telegram-agentknit-control: cannot chdir to project '{target}': "
              f"{error}", file=sys.stderr, flush=True)


def _reset_interactive_sessions():
    """Drop cached interactive runtimes so sessions rebuild in the new cwd.

    Selecting a different project changes the cwd (and thus the project's
    AGENTS.md picked up by init_session), so previously-built interactive
    sessions are discarded. Per-task sessions in the TaskWorker are left
    untouched (tasks are pinned to their creation-time project implicitly).
    """
    with RUNTIMES_LOCK:
        # Stop and forget interactive runtimes; they are lazily rebuilt on use.
        for runtime in list(RUNTIMES.values()):
            try:
                runtime.stop()
            except Exception:
                pass
        RUNTIMES.clear()


def set_active_project(path, state):
    """Select a project by absolute path and apply it as the agent's cwd.

    *path* may be absolute, relative to HOME, or just a folder name under HOME.
    Returns True on success, False if *path* is not a valid git project.
    """
    global ACTIVE_PROJECT
    with PROJECTS_LOCK:
        if path.startswith("~/"):
            path = os.path.join(HOME, path[2:])
        if not os.path.isabs(path):
            # Try as a folder name under HOME first, then relative to cwd.
            under_home = os.path.join(HOME, path)
            if os.path.isdir(under_home):
                path = under_home
            else:
                path = os.path.abspath(path)
        if not _is_git_project(path):
            return False
        ACTIVE_PROJECT = os.path.abspath(path)
        state["project"] = ACTIVE_PROJECT
        write_state(state)
        _apply_project_cwd()
        _reset_interactive_sessions()
        return True


def handle_projects_command(chat_id, message_id, command, state):
    """Implement /projects [list|<name>|<path>|none].

    With no argument (or /projects list) show the discovered projects as inline
    buttons (the active one is marked 🟢). With a name/path, switch to it.
    Use `/projects none` to clear the selection and fall back to WORKSPACE.
    """
    argument = command[len("/projects"):].strip()
    if argument.lower() in ("none", "off", "clear"):
        global ACTIVE_PROJECT
        ACTIVE_PROJECT = ""
        state.pop("project", None)
        write_state(state)
        _apply_project_cwd()
        _reset_interactive_sessions()
        reply(chat_id, f"📂 No project selected. Using workspace: `{project_cwd()}`")
        return
    if argument.lower() in ("", "list"):
        projects = discover_projects()
        if not projects:
            reply(chat_id, "No projects found. A project is a git repository "
                           "(with a remote) under $HOME.")
            return
        rows = []
        for p in projects:
            mark = "🟢 " if p["path"] == ACTIVE_PROJECT else ""
            rows.append([{
                "text": f"{mark}{p['name']}",
                "callback_data": f"/projects {p['path']}",
            }])
        active = active_project_name()
        header = ("📂 *Select a project* — it becomes the agent's cwd.\n\n"
                  f"Active: *{active or '(workspace)'}* (`{project_cwd()}`)")
        reply_with_buttons(chat_id, header, rows)
        return
    # Otherwise treat the argument as a project name/path to select.
    if set_active_project(argument, state):
        reply(chat_id, f"🟢 Active project: *{active_project_name()}*\n"
                       f"cwd: `{project_cwd()}`")
    else:
        reply(chat_id, f"Not a git project with a remote: {argument}\n"
                       f"Use /projects to list available projects.")


class TelegramApi:
    """Small keep-alive Telegram client; one instance per request lane."""

    def __init__(self):
        self.connection = None
        self.lock = threading.RLock()

    def _connect(self):
        if self.connection is None:
            self.connection = http.client.HTTPSConnection(API_HOST, timeout=40)
        return self.connection

    def _discard_connection(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = None

    def call(self, method, payload=None):
        data = urllib.parse.urlencode(payload or {}).encode()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(data)),
            "Connection": "keep-alive",
        }
        with self.lock:
            for attempt in range(3):
                try:
                    connection = self._connect()
                    connection.request("POST", API_PREFIX + method, data, headers)
                    response = connection.getresponse()
                    body = response.read()
                    description = ""
                    try:
                        body_json = json.loads(body)
                        description = body_json.get("description", "")
                    except Exception:
                        description = body.decode(errors="replace")[:200]
                    if response.status == 409:
                        # Conflict — another request is in flight.  Retry after
                        # a short delay so the previous request completes.
                        self._discard_connection()
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    if response.status >= 400:
                        raise RuntimeError(f"Telegram API returned HTTP {response.status}: {description}")
                    result = json.loads(body)
                    if not result.get("ok"):
                        raise RuntimeError(result.get("description", "Telegram API request failed"))
                    return result["result"]
                except (http.client.HTTPException, OSError):
                    self._discard_connection()
                    if attempt >= 2:
                        raise


POLL_API = TelegramApi()
SEND_API = TelegramApi()


def api(method, payload=None):
    client = POLL_API if method == "getUpdates" else SEND_API
    return client.call(method, payload)


def read_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {"offset": 0}


def write_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    temporary = STATE_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file)
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE_PATH)


class TaskCancelled(Exception):
    """Raised when a durable task is cancelled by its Telegram owner."""


class TaskStore:
    """Small, durable, single-user task queue backed by SQLite."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self.connection:
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    checkpoint TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    agent TEXT NOT NULL DEFAULT '',
                    tokens_input INTEGER NOT NULL DEFAULT 0,
                    tokens_output INTEGER NOT NULL DEFAULT 0,
                    turns INTEGER NOT NULL DEFAULT 0,
                    tool_calls INTEGER NOT NULL DEFAULT 0
                )
            """)
            # Add new columns if migrating from an older schema.
            for col, decl in (
                ('tokens_input', 'INTEGER NOT NULL DEFAULT 0'),
                ('tokens_output', 'INTEGER NOT NULL DEFAULT 0'),
                ('turns', 'INTEGER NOT NULL DEFAULT 0'),
                ('tool_calls', 'INTEGER NOT NULL DEFAULT 0'),
                ('agent', 'TEXT NOT NULL DEFAULT ""'),
                ('project', 'TEXT NOT NULL DEFAULT ""'),
            ):
                try:
                    self.connection.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError:
                    pass  # column already exists
            self.connection.execute("UPDATE tasks SET status = 'interrupted', updated_at = ? WHERE status = 'running'", (int(time.time()),))

    def _row(self, row):
        return dict(row) if row else None

    def create(self, chat_id, prompt, agent=None, project=None):
        now = int(time.time())
        with self.lock, self.connection:
            queued = self.connection.execute("SELECT count(*) FROM tasks WHERE status IN ('queued', 'running', 'cancelling')").fetchone()[0]
            if queued >= TASK_MAX_QUEUE:
                raise RuntimeError(f"task queue is full ({TASK_MAX_QUEUE})")
            cursor = self.connection.execute("INSERT INTO tasks (chat_id, prompt, status, agent, project, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?, ?, ?)", (str(chat_id), prompt, agent or "", project or "", now, now))
            return self.get(cursor.lastrowid)

    def get(self, task_id):
        with self.lock:
            return self._row(self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def recent(self, limit=10):
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            # Return the most recent <limit> tasks, newest-first so the
            # most recent tasks appear at the top of the list.
            return [self._row(row) for row in rows]

    def claim(self):
        now = int(time.time())
        with self.lock, self.connection:
            # First try to claim a queued task.
            row = self.connection.execute("SELECT * FROM tasks WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                # If nothing queued, pick up an interrupted task and run it again.
                row = self.connection.execute("SELECT * FROM tasks WHERE status = 'interrupted' ORDER BY id LIMIT 1").fetchone()
                if not row:
                    return None
                # Set interrupted tasks straight to running (it will start fresh).
                self.connection.execute(
                    "UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ? AND status = 'interrupted'",
                    (now, row['id']))
                return self.get(row['id'])
            self.connection.execute("UPDATE tasks SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ? AND status = 'queued'", (now, now, row['id']))
            return self.get(row['id'])

    def checkpoint(self, task_id, text):
        with self.lock, self.connection:
            self.connection.execute("UPDATE tasks SET checkpoint = ?, updated_at = ? WHERE id = ?", (text[-8000:], int(time.time()), task_id))

    def finish(self, task_id, status, checkpoint="", error=""):
        with self.lock, self.connection:
            # If no explicit checkpoint is provided, keep the existing one
            # (the previously accumulated text from progress callbacks).
            if not checkpoint:
                existing = self.connection.execute("SELECT checkpoint FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if existing and existing['checkpoint']:
                    checkpoint = existing['checkpoint']
            self.connection.execute("UPDATE tasks SET status = ?, checkpoint = ?, error = ?, finished_at = ?, updated_at = ? WHERE id = ?", (status, checkpoint[-8000:], error[-4000:], int(time.time()), int(time.time()), task_id))

    def update_stats(self, task_id, tokens_input=0, tokens_output=0, turns=0, tool_calls=0):
        """Update cumulative usage statistics for a task."""
        with self.lock, self.connection:
            self.connection.execute("""
                UPDATE tasks
                SET tokens_input = tokens_input + ?,
                    tokens_output = tokens_output + ?,
                    turns = turns + ?,
                    tool_calls = tool_calls + ?,
                    updated_at = ?
                WHERE id = ?
            """, (tokens_input, tokens_output, turns, tool_calls, int(time.time()), task_id))

    def cancel(self, task_id):
        with self.lock, self.connection:
            task = self.get(task_id)
            if not task or task['status'] in ('completed', 'failed', 'cancelled'):
                return task, False
            status = 'cancelled' if task['status'] in ('queued', 'paused', 'interrupted') else 'cancelling'
            self.connection.execute("UPDATE tasks SET status = ?, updated_at = ?, finished_at = CASE WHEN ? = 'cancelled' THEN ? ELSE finished_at END WHERE id = ?", (status, int(time.time()), status, int(time.time()), task_id))
            return self.get(task_id), True

    def pause(self, task_id):
        with self.lock, self.connection:
            task = self.get(task_id)
            if not task or task['status'] != 'queued':
                return task, False
            self.connection.execute("UPDATE tasks SET status = 'paused', updated_at = ? WHERE id = ?", (int(time.time()), task_id))
            return self.get(task_id), True

    def resume(self, task_id, new_prompt=None):
        with self.lock, self.connection:
            task = self.get(task_id)
            if not task or task['status'] not in ('paused', 'interrupted', 'failed', 'completed', 'cancelled'):
                return task, False
            now = int(time.time())
            if new_prompt:
                self.connection.execute("UPDATE tasks SET status = 'queued', prompt = ?, error = '', finished_at = NULL, updated_at = ? WHERE id = ?", (new_prompt, now, task_id))
            else:
                self.connection.execute("UPDATE tasks SET status = 'queued', error = '', finished_at = NULL, updated_at = ? WHERE id = ?", (now, task_id))
            return self.get(task_id), True

    def cancelling(self, task_id):
        task = self.get(task_id)
        return bool(task and task['status'] == 'cancelling')

    def counts(self):
        """Return a dict with counts of tasks grouped by status."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT status, count(*) as cnt FROM tasks GROUP BY status"
            ).fetchall()
            return {row['status']: row['cnt'] for row in rows}


class TaskWorker:
    """Runs at most one durable agentknit task, preventing invisible lock queues.

    Maintains per-task sessions so each task has its own conversation context
    and can be resumed with full history.
    """

    def __init__(self, store):
        self.store = store
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._run, name="telegram-agentknit-task-worker", daemon=True)
        # Per-agent runtime cache private to the worker so durable task
        # sessions never collide with the interactive lane's sessions.
        self._runtimes = {}
        # Per-task sessions dict keyed by (agent_key, task_id) -> session copy.
        self._task_sessions = {}

    def start(self):
        self.thread.start()
        self.wake.set()

    def notify(self):
        self.wake.set()

    def _run(self):
        while True:
            task = self.store.claim()
            if not task:
                self.wake.wait(30)
                self.wake.clear()
                continue
            task_id, chat_id = task['id'], task['chat_id']
            # Resolve the agent for this task from the task record (persisted
            # at creation time), falling back to the current ACTIVE_AGENT.
            agent_key = task.get('agent') or ACTIVE_AGENT or None
            runtime = get_agent_runtime(agent_key, cache=self._runtimes)
            if runtime is None:
                self.store.finish(task_id, 'failed', error='Agentknit runtime is not available.')
                reply(chat_id, f"Task T-{task_id} failed: Agentknit runtime is not available.\nUse /agent to select a configured agent and restart.")
                continue
            # Run the task in its pinned project's cwd (persisted at creation
            # time) if one was recorded, otherwise the active project/workspace.
            # This pins a task to the project selected when it was queued so a
            # later /projects switch never moves a running task's files.
            task_project = task.get('project') or ACTIVE_PROJECT or ""
            if task_project and os.path.isdir(task_project):
                try:
                    os.chdir(task_project)
                except OSError as error:
                    print(f"telegram-agentknit-control: task T-{task_id} cannot "
                          f"chdir to '{task_project}': {error}", file=sys.stderr, flush=True)
            else:
                _apply_project_cwd()
            # typing_stop is always defined before try so the except/finally blocks
            # are safe even if send_typing raises.
            typing_stop = threading.Event()
            try:
                # Show typing indicator immediately so the user knows the task is running.
                send_typing(chat_id)

                # Periodic typing while the task is active.
                def keep_typing():
                    while not typing_stop.wait(4):
                        try:
                            send_typing(chat_id)
                        except Exception:
                            break

                typing_thread = threading.Thread(target=keep_typing, daemon=True)
                typing_thread.start()

                # Determine if this is a resume (already started before) or a fresh task.
                is_resume = task['started_at'] is not None
                saved_session = self._task_sessions.get(task_id)

                def changed(changes):
                    for change in changes:
                        reply(chat_id, f"Task T-{task_id}\n{change_summary(change)}")

                # Accumulate the streaming response so the checkpoint
                # always contains the full text seen so far, not just
                # the latest content-delta piece.
                _accumulated = []

                def progress(text):
                    _accumulated.append(text)
                    full = "".join(_accumulated)
                    self.store.checkpoint(task_id, full)
                    # Save session checkpoint after each progress update.
                    if runtime and runtime.session:
                        import copy
                        saved = copy.deepcopy(runtime.session)
                        saved.pop("_event_handlers", None)  # clean for safe restore
                        self._task_sessions[task_id] = saved

                def notify_tool_call(name):
                    """Send typing indicator on each tool call so the user sees activity."""
                    send_typing(chat_id)

                # Capture usage stats from each turn.
                def capture_usage(result):
                    usage = result.usage or {}
                    tokens_in = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
                    tokens_out = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
                    # Count tool calls and turns from messages.
                    msgs = result.messages or []
                    turn_count = 0
                    tool_call_count = 0
                    for msg in msgs:
                        role = msg.get("role", "")
                        if role == "user":
                            turn_count += 1
                        elif role == "assistant":
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict) and part.get("type") == "tool_use":
                                        tool_call_count += 1
                            if msg.get("tool_calls"):
                                tool_call_count += len(msg["tool_calls"])
                    self.store.update_stats(task_id, tokens_in, tokens_out, turn_count, tool_call_count)

                if is_resume and saved_session is not None:
                    # Resume with the saved session so conversation continues.
                    answer = runtime.run(task['prompt'], on_file_changes=changed,
                                              on_agent_message=progress, timeout=TASK_TIMEOUT,
                                              cancelled=lambda: self.store.cancelling(task_id),
                                              fresh_session=False,
                                              session=saved_session,
                                              on_result=capture_usage,
                                              on_tool_call=notify_tool_call)
                else:
                    # New task or no saved session available — start fresh.
                    answer = runtime.run(task['prompt'], on_file_changes=changed,
                                              on_agent_message=progress, timeout=TASK_TIMEOUT,
                                              cancelled=lambda: self.store.cancelling(task_id),
                                              fresh_session=True,
                                              on_result=capture_usage,
                                              on_tool_call=notify_tool_call)
                # Save final session for potential future resume.
                if runtime and runtime.session:
                    import copy
                    saved = copy.deepcopy(runtime.session)
                    saved.pop("_event_handlers", None)  # clean for safe restore
                    self._task_sessions[task_id] = saved
                self.store.finish(task_id, 'completed', answer)
                reply(chat_id, f"Task T-{task_id} completed.\n{answer}")
            except TaskCancelled:
                typing_stop.set()
                self.store.finish(task_id, 'cancelled', 'Cancelled by user.')
                self._task_sessions.pop(task_id, None)
                reply(chat_id, f"Task T-{task_id} cancelled.")
            except TimeoutError as error:
                typing_stop.set()
                message = str(error)
                self.store.finish(task_id, 'failed', error=message)
                print(f"telegram-agentknit-control: task T-{task_id} timed out: {message}", file=sys.stderr, flush=True)
                reply(chat_id, f"Task T-{task_id} timed out: {message}")
            except Exception as error:
                typing_stop.set()
                message = _friendly_agent_error(str(error))
                self.store.finish(task_id, 'failed', error=message)
                print(f"telegram-agentknit-control: task T-{task_id} failed: {message}", file=sys.stderr, flush=True)
                reply(chat_id, f"Task T-{task_id} failed: {message}\nUse /task detail {task_id} to review and resume.")
            finally:
                typing_stop.set()


TASKS = None
TASK_WORKER = None


def tmux(*args, input_text=None):
    return subprocess.run(["tmux", *args], input=input_text, text=True, capture_output=True, timeout=15)


def screen(lines=120, target=None):
    """Capture the content of a tmux pane/window.

    *target* is a tmux target specifier (e.g. "web:1", "web:1.0").
    If None, defaults to the configured SESSION.
    """
    target = target or SESSION
    result = tmux("capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", target)
    if result.returncode:
        return "tmux session is unavailable: " + result.stderr.strip()
    output = result.stdout.strip() or "(terminal is blank)"
    return output[-3800:]


def list_tmux_windows(session=None):
    """Return a list of windows in a tmux session.

    Each entry is a dict with 'index' (int) and 'name' (str).
    Returns an empty list if the session is unavailable.
    """
    target = session or SESSION
    result = tmux("list-windows", "-t", target, "-F",
                  "#{window_index}:#{window_name}")
    if result.returncode:
        return []
    windows = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if ":" in line:
            idx, name = line.split(":", 1)
            windows.append({"index": int(idx), "name": name})
    return windows


def list_tmux_panes(window_target=None):
    """Return a list of panes in a tmux window.

    Each entry is a dict with 'index' (int) and 'title' (str).
    *window_target* is like "session:window" (e.g. "web:1").
    Defaults to the current session's active window.
    Returns an empty list if unavailable.
    """
    target = window_target or SESSION
    # Use a pipe as a separator because tmux format variables should never
    # contain a pipe character. If the pane has no title, show its index.
    result = tmux("list-panes", "-t", target, "-F",
                  "#{pane_index}|#{pane_title}")
    if result.returncode:
        return []
    panes = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if "|" in line:
            idx, title = line.split("|", 1)
            panes.append({"index": int(idx), "title": title})
    return panes


def list_tmux_sessions():
    """Return a list of available tmux session names."""
    result = tmux("list-sessions", "-F", "#{session_name}")
    if result.returncode:
        return []
    return [s.strip() for s in result.stdout.strip().splitlines() if s.strip()]


def delayed_screen(chat_id):
    """Give interactive tools time to respond, without pausing Telegram polling."""
    time.sleep(20)
    try:
        reply(chat_id, screen(lines=30))
    except Exception as error:
        print(f"telegram-agentknit-control: delayed reply failed: {error}", file=sys.stderr, flush=True)


def change_summary(change):
    """Return a compact filename (and line counts if available) from a change."""
    path = change.get("path", "")
    added = change.get("added", 0)
    removed = change.get("removed", 0)
    if added or removed:
        return f"✏️ Changed\n{os.path.basename(path)}  +{added} -{removed}"
    return f"✏️ Changed\n{os.path.basename(path)}"


def run_remote_control(chat_id, prompt):
    """Process one direct request without racing the shared chat session."""
    with INTERACTIVE_REQUEST_LOCK:
        _run_remote_control(chat_id, prompt)


def _run_remote_control(chat_id, prompt):
    """Run a Telegram request using the active agent's AgentknitRuntime instance.

    General conversation uses a rolling context window of the last 5 messages
    (fresh_session=False, max_context_turns=5) so the agent has a bit of
    conversation history without accumulating indefinitely.

    Runs in the active project's cwd (see /projects) so the agent's tools and
    the project's AGENTS.md apply to this request.
    """
    runtime = get_agent_runtime()
    if runtime is None:
        reply(chat_id, "No agent runtime is available. Use /agent to select a configured agent and restart.")
        return
    # Ensure the agent runs in the active project's cwd (selected via /projects).
    _apply_project_cwd()
    # typing_stop is always defined before try so the finally block is safe.
    typing_stop = threading.Event()
    try:
        # Show typing indicator immediately so the user knows the bot is working.
        send_typing(chat_id)

        # Periodically re-send the typing indicator while the agent is running
        # (the Telegram typing indicator lasts ~5 seconds, so keep refreshing).
        def keep_typing():
            while not typing_stop.wait(4):
                try:
                    send_typing(chat_id)
                except Exception:
                    break

        typing_thread = threading.Thread(target=keep_typing, daemon=True)
        typing_thread.start()

        def notify_file_changes(changes):
            for change in changes:
                reply(chat_id, change_summary(change))

        def notify_tool_call(name):
            """Send typing indicator on each tool call so the user sees activity."""
            send_typing(chat_id)

        answer = runtime.run(
            prompt,
            on_file_changes=notify_file_changes,
            on_agent_message=None,
            fresh_session=False,
            max_context_turns=5,
            on_tool_call=notify_tool_call,
        )

        if answer.strip():
            reply(chat_id, answer)
    except KeyboardInterrupt:
        # Should not happen in normal operation (the runtime converts
        # timeout cancellations to TimeoutError), but guard against a
        # bare KeyboardInterrupt just in case.
        print("telegram-agentknit-control: agentknit request interrupted", file=sys.stderr, flush=True)
        reply(chat_id, "The request was interrupted.")
    except TimeoutError as error:
        # Timeout due to max time — send a clear message to the user.
        print(f"telegram-agentknit-control: agentknit request timed out: {error}", file=sys.stderr, flush=True)
        reply(chat_id, str(error))
    except Exception as error:
        print(f"telegram-agentknit-control: agentknit request failed: {error}", file=sys.stderr, flush=True)
        try:
            reply(chat_id, _friendly_agent_error(str(error)))
        except Exception as reply_error:
            print(f"telegram-agentknit-control: failure reply failed: {reply_error}", file=sys.stderr, flush=True)
    finally:
        typing_stop.set()


def send_terminal(text):
    typed = tmux("send-keys", "-t", SESSION, "-l", text)
    entered = tmux("send-keys", "-t", SESSION, "Enter")
    return typed.returncode == 0 and entered.returncode == 0


def reply(chat_id, text):
    try:
        api("sendMessage", {"chat_id": chat_id, "text": text[:4096]})
    except Exception as error:
        print(f"telegram-agentknit-control: reply failed: {error}", file=sys.stderr, flush=True)


def send_typing(chat_id):
    """Tell Telegram the bot is typing (shows a typing indicator for ~5s).

    Call this before / during a long operation so the user sees the bot
    is working.  The indicator automatically clears when a message is sent
    or after ~5 seconds.
    """
    try:
        api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception as error:
        print(f"telegram-agentknit-control: sendChatAction failed: {error}", file=sys.stderr, flush=True)


def inline_keyboard(buttons):
    """Build an inline keyboard markup dict from a list of button rows.

    Each row is a list of dicts with keys 'text' and 'callback_data' or 'url'.
    Example:
        inline_keyboard([
            [{"text": "🔍 Status", "callback_data": "/status"},
             {"text": "📺 Screen", "callback_data": "/screen"}],
            [{"text": "❓ Help", "callback_data": "/help"}],
        ])
    """
    return json.dumps({
        "inline_keyboard": [
            [{"text": btn.get("text", ""), **({k: v for k, v in btn.items() if k != "text"})}
             for btn in row]
            for row in buttons
        ]
    })


def reply_with_buttons(chat_id, text, buttons):
    """Send a message with an inline keyboard."""
    try:
        api("sendMessage", {
            "chat_id": chat_id,
            "text": text[:4096],
            "reply_markup": inline_keyboard(buttons),
        })
    except Exception as error:
        print(f"telegram-agentknit-control: reply_with_buttons failed: {error}", file=sys.stderr, flush=True)


def acknowledge(chat_id, message_id):
    """Mark an accepted request without adding a separate chat message."""
    if message_id is None or message_id <= 0:
        print(f"telegram-agentknit-control: acknowledge skipped (invalid message_id={message_id})", file=sys.stderr, flush=True)
        return
    for reaction_emoji in ("🫡", "👍"):
        try:
            api("setMessageReaction", {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": json.dumps([{"type": "emoji", "emoji": reaction_emoji}], ensure_ascii=False),
            })
            return  # Success
        except Exception as error:
            print(f"telegram-agentknit-control: acknowledge reaction with {reaction_emoji} failed: {error}", file=sys.stderr, flush=True)
    # Last resort: send a text acknowledgment
    try:
        api("sendMessage", {
            "chat_id": chat_id,
            "text": "🫡",
        })
    except Exception as last_error:
        print(f"telegram-agentknit-control: text acknowledgment also failed: {last_error}", file=sys.stderr, flush=True)


def start_remote_control(chat_id, message_id, prompt):
    """Acknowledge promptly, then run the request asynchronously."""
    try:
        acknowledge(chat_id, message_id)
    except Exception as error:
        print(f"telegram-agentknit-control: reaction failed: {error}", file=sys.stderr, flush=True)
    threading.Thread(target=run_remote_control, args=(chat_id, prompt), daemon=True).start()


def agent_label(agent_key):
    """Human-readable label for an agent key (with a fallback)."""
    spec = AGENTS.get(agent_key) if AGENTS else None
    return spec.get("label", agent_key) if spec else agent_key


def handle_agent_command(chat_id, message_id, command, state):
    """Implement /agent [list|<key>].

    With no argument (or /agent list) show the registered agents as inline
    buttons plus the currently active one. With a key, switch to that agent.
    """
    if not AGENTS:
        reply(chat_id, "No agents configured. Set TELEGRAM_AGENTKNIT_AGENTS or the single-agent env vars and restart.")
        return
    argument = command[len("/agent"):].strip()
    if argument.lower() in ("", "list"):
        rows = []
        for key in AGENT_ORDER:
            spec = AGENTS[key]
            mark = "🟢 " if key == ACTIVE_AGENT else ""
            rows.append([{
                "text": f"{mark}{spec.get('label', key)}",
                "callback_data": f"/agent {key}",
            }])
        reply_with_buttons(
            chat_id,
            f"🤖 *Select agent*\n\nActive: *{agent_label(ACTIVE_AGENT)}*",
            rows,
        )
        return
    key = argument
    if key not in AGENTS:
        reply(chat_id, f"Unknown agent: {key}\nAvailable: {', '.join(AGENT_ORDER)}")
        return
    if set_active_agent(key, state):
        reply(chat_id, f"🟢 Active agent: *{agent_label(key)}*")
    else:
        reply(chat_id, "Unable to switch agent.")


def human_duration(seconds):
    """Convert a number of seconds to a short human-readable string."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    seconds %= 60
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours = minutes // 60
    minutes %= 60
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days = hours // 24
    hours %= 24
    if days < 30:
        return f"{days}d {hours}h" if hours else f"{days}d"
    months = days // 30
    days %= 30
    return f"{months}mo {days}d" if days else f"{months}mo"


def task_summary(task, detail=False):
    if not task:
        return "Task not found."
    age = max(0, int(time.time()) - task['created_at'])
    text = f"T-{task['id']} {task['status']} ({human_duration(age)})"
    if detail:
        text += f"\nRequest: {task['prompt'][:700]}"
        if task['checkpoint']:
            text += f"\nLatest update: {task['checkpoint'][-1800:]}"
        if task['error']:
            text += f"\nError: {task['error'][-1000:]}"
    return text


def task_detail_text(task):
    """Build a detailed stats view for a single task."""
    if not task:
        return "Task not found."
    lines = [f"📋 *Task T-{task['id']}*"]
    lines.append(f"Status: *{task['status']}*")

    # Agent that ran (or is running) this task.
    task_agent = task.get('agent') or ''
    if task_agent:
        lines.append(f"Agent: *{agent_label(task_agent)}*")

    # Project the task is pinned to (if any).
    task_project = task.get('project') or ''
    if task_project:
        lines.append(f"Project: *{os.path.basename(task_project) or task_project}* (`{task_project}`)")

    # Time stats.
    created = task.get('created_at', 0)
    started = task.get('started_at')
    finished = task.get('finished_at')
    now = int(time.time())
    if started:
        duration = (finished or now) - started
        lines.append(f"Runtime: {human_duration(duration)}")
    total_age = now - created
    lines.append(f"Age: {human_duration(total_age)}")

    # Token usage.
    tokens_in = task.get('tokens_input', 0)
    tokens_out = task.get('tokens_output', 0)
    if tokens_in or tokens_out:
        lines.append(f"Tokens: {tokens_in} in / {tokens_out} out (total {tokens_in + tokens_out})")

    # Turns and tool calls.
    turns = task.get('turns', 0)
    tool_calls = task.get('tool_calls', 0)
    if turns:
        lines.append(f"Turns: {turns}")
    if tool_calls:
        lines.append(f"Tool calls: {tool_calls}")

    # Prompt snippet.
    prompt = task.get('prompt', '')
    if prompt:
        lines.append(f"\n*Request:*\n{prompt[:800]}")

    # Latest checkpoint / last message — always show, even if empty.
    checkpoint = task.get('checkpoint', '')
    if checkpoint:
        lines.append(f"\n*Latest message:*\n{checkpoint[-1500:]}")
    else:
        lines.append(f"\n*Latest message:* _(no response yet)_")

    # Error if any.
    error = task.get('error', '')
    if error:
        lines.append(f"\n*Error:* {error[:1000]}")

    return "\n".join(lines)


def task_id_from(command, prefix):
    value = command[len(prefix):].strip()
    if value.isdigit() and int(value) > 0:
        return int(value)
    return None


def start_task(chat_id, message_id, prompt):
    try:
        acknowledge(chat_id, message_id)
    except Exception as error:
        print(f"telegram-agentknit-control: reaction failed: {error}", file=sys.stderr, flush=True)
    try:
        # Persist the currently active agent on the task so the worker uses
        # the same model for the lifetime of the task even if the user later
        # switches agents for interactive requests. Persist the active project
        # too so the task always runs in the project that was selected at
        # creation time.
        task = TASKS.create(chat_id, prompt,
                            agent=ACTIVE_AGENT or None,
                            project=ACTIVE_PROJECT or None)
        TASK_WORKER.notify()
        proj = active_project_name()
        proj_tag = f", project {proj}" if proj else ""
        reply(chat_id, f"Task T-{task['id']} queued ({ACTIVE_AGENT}{proj_tag}). Use /task detail {task['id']} to follow it.")
    except Exception as error:
        reply(chat_id, f"Unable to queue task: {error}")


def permitted(chat_id, state):
    return str(state.get("chat_id", "")) == str(chat_id)


def handle_callback(callback_query, state):
    """Handle an inline keyboard button callback."""
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
    data = callback_query.get("data", "")
    callback_id = callback_query.get("id")
    if not chat_id or not data:
        return
    # Answer the callback query to remove the loading indicator.
    try:
        api("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception as error:
        print(f"telegram-agentknit-control: answerCallbackQuery failed: {error}", file=sys.stderr, flush=True)
    # Treat the callback data as a command.
    fake_message = {
        "chat": {"id": chat_id},
        "text": data,
        "message_id": callback_query.get("message", {}).get("message_id", 0),
    }
    handle(fake_message, state)


# ── One-letter shortcuts ─────────────────────────────────────────────────────
# Typing a single letter is equivalent to the full command. When the shortcut is
# followed by extra text, that text becomes the command's argument. This lets
# you drive the whole controller from the keyboard without typing slashes.
#
# Each entry maps a letter to a (bare, with_args) pair:
#   * `bare`        — used when the letter is typed alone.
#   * `with_args`   — prepended to the arguments when extra text is given.
#
# Mnemonics: h=help, s=status, v=view screen, t=tasks (or new TasK), i=interrupt,
# r=restart, a=agent, m=tMux, x=eXecute shell, c=Converse with agent.
SHORTCUTS = {
    "h": ("/help", "/help"),
    "s": ("/status", "/status"),
    "v": ("/screen", "/screen"),
    "i": ("/interrupt", "/interrupt"),
    "r": ("/restart", "/restart"),
    "t": ("/tasks", "/task"),   # bare lists tasks; `t <prompt>` queues a new task
    "a": ("/agent", "/agent"),  # bare lists agents; `a <key>` selects one
    "m": ("/tmux", "/tmux"),    # `m <text>` types into tmux
    "x": ("/sh", "/sh"),        # `x <cmd>` runs a shell command
    "c": ("/rc", "/rc"),        # `c <prompt>` is the same as bare text -> agent
    "p": ("/projects", "/projects"),  # bare lists projects; `p <name>` selects one
}


def expand_shortcut(command):
    """Rewrite a one-letter shortcut into its full command, if applicable.

    A shortcut is a single letter (case-insensitive), optionally followed by a
    space and arguments, e.g. ``h`` -> ``/help``, ``t fix the bug`` ->
    ``/task fix the bug``. Anything that is not exactly one letter as its first
    token (e.g. ``hello``, ``/status``, multi-word text) is returned unchanged.
    """
    if not command:
        return command
    parts = command.split(None, 1)
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if len(head) != 1 or not head.isalpha():
        return command
    mapping = SHORTCUTS.get(head.lower())
    if mapping is None:
        return command
    bare, prefix = mapping
    return f"{prefix} {rest}".strip() if rest else bare


def handle(message, state, update=None):
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text")
    if chat_id is None or not text:
        return
    command = text.strip()

    # One-letter shortcut: "h" == "/help", "t fix bug" == "/task fix bug", etc.
    command = expand_shortcut(command)

    if not state.get("chat_id"):
        if command.startswith("/pair ") and command[6:].strip() == CFG["PAIR_CODE"]:
            state["chat_id"] = chat_id
            reply_with_buttons(chat_id, "✅ *Paired successfully!* Send text for a direct agentknit request, or use the buttons below.", [
                [{"text": "🔍 Status", "callback_data": "/status"},
                 {"text": "📺 Screen", "callback_data": "/screen"}],
                [{"text": "📋 Tasks", "callback_data": "/tasks"},
                 {"text": "🤖 Agent", "callback_data": "/agent"}],
                [{"text": "📂 Projects", "callback_data": "/projects"},
                 {"text": "❓ Help", "callback_data": "/help"}],
            ])
        else:
            reply(chat_id, "This private bot needs pairing. Send: /pair <your pairing code>")
        return
    if not permitted(chat_id, state):
        return
    if command in ("/start", "/help"):
        reply_with_buttons(chat_id, "🤖 *Telegram Agentknit Controller*\n\nSend text directly for an agentknit request, or use the buttons below.\n\n*One-letter shortcuts* (type the letter alone, or `letter <args>`):\n`h` help · `s` status · `v` view screen · `i` interrupt · `r` restart\n`t` list tasks / `t <prompt>` new task · `a` agents / `a <key>` select\n`p` projects / `p <name>` select project · `m <text>` type in tmux\n`x <cmd>` run shell · `c <prompt>` ask agent", [
            [{"text": "🔍 Status", "callback_data": "/status"},
             {"text": "📺 Screen", "callback_data": "/screen"}],
            [{"text": "📋 Tasks", "callback_data": "/tasks"},
             {"text": "🤖 Agent", "callback_data": "/agent"}],
            [{"text": "📂 Projects", "callback_data": "/projects"},
             {"text": "❓ Help", "callback_data": "/help"}],
        ])
    elif command == "/tasks":
        tasks = TASKS.recent(6)
        if not tasks:
            reply(chat_id, "No tasks yet.")
        else:
            # Show the 6 most recent tasks (newest first).
            # Build a button row per task showing ID, status, and human-readable duration.
            rows = []
            now = int(time.time())
            for t in tasks:
                status_icon = {
                    "queued": "⏳", "running": "🔄", "completed": "✅",
                    "failed": "❌", "cancelled": "🚫", "cancelling": "🛑",
                    "paused": "⏸️", "interrupted": "⚠️",
                }.get(t['status'], "❓")
                # Compute human-readable duration.
                if t['status'] in ('completed', 'failed', 'cancelled'):
                    if t.get('started_at') and t.get('finished_at'):
                        dur = t['finished_at'] - t['started_at']
                    elif t.get('finished_at'):
                        dur = t['finished_at'] - t['created_at']
                    else:
                        dur = now - t['created_at']
                elif t['status'] == 'running':
                    if t.get('started_at'):
                        dur = now - t['started_at']
                    else:
                        dur = now - t['created_at']
                else:
                    dur = now - t['created_at']
                dur_str = human_duration(dur)
                rows.append([
                    {"text": f"{status_icon} T-{t['id']} {t['status']} ({dur_str})",
                     "callback_data": f"/task detail {t['id']}"}
                ])
            reply_with_buttons(chat_id, "📋 *Tasks* — tap a task for details:", rows)
    elif command == "/task":
        reply(chat_id, "Usage: /task <prompt>, /task detail <id>, /task resume <id> [new prompt]")
    elif command.startswith("/task detail "):
        task_id = task_id_from(command, "/task detail ")
        if task_id:
            task = TASKS.get(task_id)
            if task:
                buttons = []
                # Interrupt for running tasks.
                if task['status'] in ('running',):
                    buttons.append([{"text": "⚡ Interrupt", "callback_data": f"/task interrupt {task_id}"}])
                # Resume for any finished or paused task.
                elif task['status'] in ('completed', 'failed', 'cancelled'):
                    buttons.append([{"text": "▶️ Resume", "callback_data": f"/task resume_prompt {task_id}"}])
                elif task['status'] in ('paused', 'interrupted'):
                    buttons.append([{"text": "▶️ Resume", "callback_data": f"/task resumex {task_id}"}])
                if buttons:
                    reply_with_buttons(chat_id, task_detail_text(task), buttons)
                else:
                    reply(chat_id, task_detail_text(task))
            else:
                reply(chat_id, "Task not found.")
        else:
            reply(chat_id, "Usage: /task detail <id>")
    elif command.startswith("/task pause "):
        acknowledge(chat_id, message.get("message_id"))
        task_id = task_id_from(command, "/task pause ")
        task, changed = TASKS.pause(task_id) if task_id else (None, False)
        reply(chat_id, f"Task T-{task_id} paused." if changed else "Only queued tasks can be paused.")
    elif command.startswith("/task resume "):
        acknowledge(chat_id, message.get("message_id"))
        # /task resume <id> [new prompt ...]
        remainder = command[len("/task resume "):].strip()
        parts = remainder.split(None, 1)  # split into id and optional new prompt
        if parts and parts[0].isdigit():
            task_id = int(parts[0])
            new_prompt = parts[1] if len(parts) > 1 else None
            task, changed = TASKS.resume(task_id, new_prompt=new_prompt)
            if changed:
                TASK_WORKER.notify()
                msg = f"Task T-{task_id} queued for resume."
                if new_prompt:
                    msg += f" New prompt provided."
                reply(chat_id, msg)
            else:
                reply(chat_id, "Only paused, interrupted, failed, or completed tasks can be resumed.")
        else:
            reply(chat_id, "Usage: /task resume <id> [new prompt]")
    elif command.startswith("/task resumex "):
        acknowledge(chat_id, message.get("message_id"))
        # Inline callback: simple resume with no extra prompt.
        task_id = task_id_from(command, "/task resumex ")
        task, changed = TASKS.resume(task_id) if task_id else (None, False)
        if changed:
            TASK_WORKER.notify()
            reply(chat_id, f"Task T-{task_id} queued for resume.")
        else:
            reply(chat_id, "Only paused, interrupted, failed, or completed tasks can be resumed.")
    elif command.startswith("/task resume_prompt "):
        # Inline callback for completed/failed tasks: ask for a follow-up prompt.
        task_id = task_id_from(command, "/task resume_prompt ")
        if task_id:
            reply(chat_id, f"Send your follow-up prompt for task T-{task_id} as:\n`/task resume {task_id} <your message>`")
        else:
            reply(chat_id, "Task not found.")
    elif command.startswith("/task interrupt "):
        acknowledge(chat_id, message.get("message_id"))
        task_id = task_id_from(command, "/task interrupt ")
        if task_id:
            # Interrupt the tmux session (Ctrl-C) to break the agent mid-turn.
            result = tmux("send-keys", "-t", SESSION, "C-c")
            if result.returncode == 0:
                reply(chat_id, f"⚡ Interrupted task T-{task_id}.")
            else:
                reply(chat_id, "Unable to reach tmux to interrupt.")
        else:
            reply(chat_id, "Usage: /task interrupt <id>")
    elif command.startswith("/task cancel "):
        acknowledge(chat_id, message.get("message_id"))
        task_id = task_id_from(command, "/task cancel ")
        task, changed = TASKS.cancel(task_id) if task_id else (None, False)
        if changed:
            TASK_WORKER.notify()
        reply(chat_id, f"Task T-{task_id} cancellation requested." if changed else "Task cannot be cancelled.")
    elif command.startswith("/task "):
        prompt = command[6:].strip()
        if prompt:
            start_task(chat_id, message["message_id"], prompt)
        else:
            reply(chat_id, "Usage: /task <prompt>")
    elif command == "/screen":
        # Build a list of available tmux screens (sessions, windows, panes)
        # and present them as inline buttons. Tapping a button shows the
        # captured content of that target.
        screens = []

        # 1. If there are multiple tmux sessions, list each as a top-level
        #    screen option (capturing the whole session shows the active pane).
        sessions = list_tmux_sessions()
        if len(sessions) > 1:
            for s in sessions:
                screens.append({
                    "target": s,
                    "label": f"🖥️ Session: {s}",
                })
            # Still show windows from the default session below.
        # 2. List windows from the primary session.
        windows = list_tmux_windows()
        for w in windows:
            target = f"{SESSION}:{w['index']}"
            # Check if this window has more than one pane.
            panes = list_tmux_panes(target)
            if len(panes) > 1:
                # Show each pane as a separate screen option.
                for p in panes:
                    pane_target = f"{target}.{p['index']}"
                    label = f"📟 {SESSION}:{w['index']}.{p['index']}"
                    if p.get('title'):
                        label += f" {p['title']}"
                    screens.append({
                        "target": pane_target,
                        "label": label,
                    })
            else:
                # Single-pane window: show as one option.
                label = f"📟 {SESSION}:{w['index']}"
                if w.get('name'):
                    label += f" {w['name']}"
                screens.append({
                    "target": target,
                    "label": label,
                })
        if not screens:
            # Fallback: just show the current screen content.
            reply(chat_id, screen())
        else:
            rows = []
            for s in screens:
                rows.append([{
                    "text": s["label"],
                    "callback_data": f"/screen_show {s['target']}",
                }])
            reply_with_buttons(
                chat_id,
                "📺 *Select a screen to view:*",
                rows,
            )
    elif command.startswith("/screen_show"):
        target = command[len("/screen_show"):].strip()
        if target:
            reply(chat_id, screen(target=target))
        else:
            # Bare /screen_show without a target: show the current/default screen.
            reply(chat_id, screen())
    elif command == "/status":
        counts = TASKS.counts()
        pending = sum(counts.get(s, 0) for s in ('queued', 'running', 'cancelling', 'paused', 'interrupted'))
        completed = counts.get('completed', 0)
        failed = counts.get('failed', 0)
        cancelled = counts.get('cancelled', 0)
        tmux_ok = tmux("has-session", "-t", SESSION).returncode == 0
        proj_name = active_project_name()
        lines = [
            f"📊 *Controller Status*",
            f"tmux session '{SESSION}': {'✅' if tmux_ok else '❌'}",
            f"Active agent: *{agent_label(ACTIVE_AGENT)}* ({len(AGENT_ORDER)} configured)",
            f"Active project: *{proj_name or '(workspace)'}* (`{project_cwd()}`)",
            f"",
            f"*Tasks:*",
            f"  Pending: {pending}",
            f"  Completed: {completed}",
            f"  Failed: {failed}",
            f"  Cancelled: {cancelled}",
            f"  Total: {sum(counts.values())}",
        ]
        reply(chat_id, "\n".join(lines))
    elif command == "/interrupt":
        result = tmux("send-keys", "-t", SESSION, "C-c")
        reply(chat_id, "Sent Ctrl-C." if result.returncode == 0 else "Unable to reach tmux.")
    elif command == "/restart":
        try:
            reply(chat_id, "🔄 *Restarting controller...*")
            # Give Telegram a moment to deliver the message before we die.
            time.sleep(1)
            subprocess.run(
                ["systemctl", "--user", "restart", "telegram-agentknit-controller.service"],
                timeout=15, capture_output=True, text=True,
            )
        except Exception as error:
            reply(chat_id, f"Restart failed: {error}")
    elif command == "/tmux":
        reply(chat_id, "Usage: /tmux <text>")
    elif command.startswith("/tmux "):
        acknowledge(chat_id, message.get("message_id"))
        if send_terminal(command[6:]):
            threading.Thread(target=delayed_screen, args=(chat_id,), daemon=True).start()
        else:
            reply(chat_id, "Unable to reach the tmux session.")
    elif command == "/sh":
        reply(chat_id, "Usage: /sh <shell command>")
    elif command.startswith("/sh "):
        acknowledge(chat_id, message.get("message_id"))
        try:
            result = subprocess.run(
                command[4:].strip(),
                shell=True, capture_output=True, text=True, timeout=30,
                cwd=project_cwd(),
            )
            output = result.stdout or ""
            if result.stderr:
                output += "\n" + result.stderr
            out = output.strip() or "(no output)"
            reply(chat_id, out[-4000:])
        except subprocess.TimeoutExpired:
            reply(chat_id, "Command timed out after 30s.")
        except Exception as error:
            reply(chat_id, f"Command failed: {error}")
    elif command == "/rc":
        reply(chat_id, "Usage: /rc <prompt>")
    elif command.startswith("/rc "):
        prompt = command[4:].strip()
        if prompt:
            start_remote_control(chat_id, message["message_id"], prompt)
        else:
            reply(chat_id, "Usage: /rc <prompt>")
    elif command == "/agent" or command.startswith("/agent "):
        handle_agent_command(chat_id, message.get("message_id"), command, state)
    elif command == "/projects" or command.startswith("/projects "):
        handle_projects_command(chat_id, message.get("message_id"), command, state)
    elif command.startswith("/"):
        reply(chat_id, "Unknown command. Use /help.")
    else:
        start_remote_control(chat_id, message["message_id"], text)


def executable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK) or bool(shutil.which(path))


def check_requirements():
    """Check the local setup without entering the Telegram polling loop."""
    problems = []

    try:
        mode = stat.S_IMODE(os.stat(CONFIG_PATH).st_mode)
        if mode & 0o077:
            problems.append(f"config file is mode {mode:03o}, expected 600: {CONFIG_PATH}")
        else:
            print(f"OK: protected config file: {CONFIG_PATH}")
    except OSError as error:
        problems.append(f"cannot inspect config file {CONFIG_PATH}: {error}")

    # Check agentknit is importable.
    try:
        import agentknit
        print(f"OK: agentknit {agentknit.__version__}")
    except ImportError as error:
        problems.append(f"agentknit import failed: {error}")

    # Check agent registry configuration.
    if not AGENTS:
        problems.append("no agents configured (set TELEGRAM_AGENTKNIT_AGENTS or the single-agent TELEGRAM_AGENTKNIT_MODEL + TELEGRAM_AGENTKNIT_ENDPOINT)")
    else:
        for key in AGENT_ORDER:
            spec = AGENTS[key]
            label = spec.get("label", key)
            if spec.get("spec_path"):
                if os.path.isfile(spec["spec_path"]):
                    print(f"OK: agent '{key}' ({label}) spec file: {spec['spec_path']}")
                else:
                    problems.append(f"agent '{key}' spec file not found: {spec['spec_path']}")
            else:
                print(f"OK: agent '{key}' ({label}) model={spec.get('model')}, endpoint={spec.get('endpoint')}")
        print(f"OK: {len(AGENT_ORDER)} agent(s) configured, active={ACTIVE_AGENT or '(none)'}")

    if shutil.which("tmux"):
        print("OK: tmux is on PATH")
    else:
        problems.append("tmux is not on PATH")
    if os.path.isdir(WORKSPACE):
        print(f"OK: workspace: {WORKSPACE}")
    else:
        problems.append(f"workspace is not a directory: {WORKSPACE}")

    # Report discovered projects and the active project selection, if any.
    try:
        projects = discover_projects()
        print(f"OK: {len(projects)} project(s) found")
        for p in projects[:20]:
            mark = " (active)" if p["path"] == ACTIVE_PROJECT else ""
            print(f"     - {p['name']}  [{p['remote']}]{mark}")
        if ACTIVE_PROJECT:
            if os.path.isdir(ACTIVE_PROJECT):
                print(f"OK: active project: {ACTIVE_PROJECT}")
            else:
                problems.append(f"active project is not a directory: {ACTIVE_PROJECT}")
    except Exception as error:
        print(f"WARN: project discovery failed: {error}", file=sys.stderr)

    result = tmux("has-session", "-t", SESSION)
    if result.returncode:
        print(f"WARN: tmux session '{SESSION}' is unavailable", file=sys.stderr)
    else:
        print(f"OK: tmux session '{SESSION}'")
    try:
        bot = api("getMe")
        print(f"OK: Telegram bot: @{bot.get('username', '(no username)')}")
    except Exception as error:
        problems.append(f"Telegram API preflight failed: {error}")

    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if problems:
        return 1
    print("Setup check passed.")
    return 0


def main():
    global CFG, API_PREFIX, TASKS, TASK_WORKER, AGENT_SPEC, AGENTKNIT_MODEL, AGENTKNIT_ENDPOINT, SYSTEM_PROMPT_SUPPLEMENT, ACTIVE_AGENT, ACTIVE_PROJECT
    try:
        CFG = config()
    except (OSError, RuntimeError) as error:
        print(f"telegram-agentknit-control: configuration failed: {error}", file=sys.stderr)
        return 1
    # Pick up any TELEGRAM_AGENTKNIT_* values that were placed in the config
    # file (config() exports them to the environment).
    AGENT_SPEC = os.environ.get("TELEGRAM_AGENTKNIT_SPEC", AGENT_SPEC)
    AGENTKNIT_MODEL = os.environ.get("TELEGRAM_AGENTKNIT_MODEL", AGENTKNIT_MODEL)
    AGENTKNIT_ENDPOINT = os.environ.get("TELEGRAM_AGENTKNIT_ENDPOINT", AGENTKNIT_ENDPOINT)
    SYSTEM_PROMPT_SUPPLEMENT = os.environ.get("TELEGRAM_AGENTKNIT_SYSTEM_PROMPT_SUPPLEMENT", SYSTEM_PROMPT_SUPPLEMENT)
    API_PREFIX = f"/bot{CFG['BOT_TOKEN']}/"
    try:
        # Build the agent registry (defaults or TELEGRAM_AGENTKNIT_AGENTS override).
        load_agents()
    except RuntimeError as error:
        print(f"telegram-agentknit-control: agent registry failed: {error}", file=sys.stderr)
        return 1
    if len(sys.argv) == 2 and sys.argv[1] == "--check":
        return check_requirements()
    if len(sys.argv) > 1:
        print("Usage: telegram-agentknit-control.py [--check]", file=sys.stderr)
        return 2
    # Restore the previously selected agent from state, defaulting to the first.
    state = read_state()
    saved_agent = state.get("agent") if isinstance(state, dict) else None
    if saved_agent and saved_agent in AGENTS:
        ACTIVE_AGENT = saved_agent
    elif AGENT_ORDER:
        ACTIVE_AGENT = AGENT_ORDER[0]
    # Restore the previously selected project (cwd) from state, falling back to
    # the TELEGRAM_AGENTKNIT_PROJECT default. A persisted state value wins so
    # /projects selections survive restarts.
    candidate_project = ""
    saved_project = state.get("project") if isinstance(state, dict) else None
    if saved_project and _is_git_project(saved_project):
        candidate_project = saved_project
    elif DEFAULT_PROJECT:
        if set_active_project(DEFAULT_PROJECT, state):
            candidate_project = ACTIVE_PROJECT
    if candidate_project:
        ACTIVE_PROJECT = candidate_project
        _apply_project_cwd()
    try:
        TASKS = TaskStore(TASK_STATE_PATH)
        TASK_WORKER = TaskWorker(TASKS)
        TASK_WORKER.start()
    except Exception as error:
        print(f"telegram-agentknit-control: task worker failed: {error}", file=sys.stderr)
        return 1
    # Pre-warm the active agent's runtime so misconfiguration is surfaced at
    # startup (non-fatal: the bot still starts so other agents can be selected).
    try:
        prewarm = get_agent_runtime(ACTIVE_AGENT)
        if prewarm is None:
            print(f"telegram-agentknit-control: active agent '{ACTIVE_AGENT}' "
                  f"runtime unavailable; use /agent to select another.",
                  file=sys.stderr, flush=True)
    except Exception as error:
        print(f"telegram-agentknit-control: agentknit runtime failed: {error}", file=sys.stderr, flush=True)
    try:
        api("getMe")
    except Exception as error:
        print(f"telegram-agentknit-control: sender preflight failed: {error}", file=sys.stderr, flush=True)
    while True:
        try:
            updates = api("getUpdates", {"offset": state.get("offset", 0), "timeout": 30, "allowed_updates": json.dumps(["message", "callback_query"])})
            for update in updates:
                state["offset"] = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback(update["callback_query"], state)
                else:
                    handle(update.get("message", {}), state)
                write_state(state)
        except KeyboardInterrupt:
            return
        except Exception as error:
            print(f"telegram-agentknit-control: {error}", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
