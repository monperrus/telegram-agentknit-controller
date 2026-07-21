#!/usr/bin/env bash
# Install the controller for the current user, without placing secrets in the repository.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --bot-token TOKEN  Telegram token (otherwise prompted for securely)
  --pair-code CODE   Pairing code (otherwise generated securely)
  --force            Replace an existing config file
  --no-start         Install files and service, but do not start it
  -h, --help         Show this help
EOF
}

bot_token=${BOT_TOKEN:-}
pair_code=${PAIR_CODE:-}
force=0
start=1
while (($#)); do
  case "$1" in
    --bot-token) bot_token=${2:?--bot-token needs a value}; shift 2 ;;
    --pair-code) pair_code=${2:?--pair-code needs a value}; shift 2 ;;
    --force) force=1; shift ;;
    --no-start) start=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for command in python3 tmux systemctl; do
  command -v "$command" >/dev/null || { echo "Missing required command: $command" >&2; exit 1; }
done

# Check that agentknit is installed.
python3 -c 'import agentknit' 2>/dev/null || {
  echo 'The agentknit Python package is required: pip install agentknit' >&2
  exit 1
}

if [[ -z "$bot_token" ]]; then
  read -r -s -p 'Telegram bot token: ' bot_token
  echo
fi
[[ -n "$bot_token" ]] || { echo 'A Telegram bot token is required.' >&2; exit 1; }

if [[ -z "$pair_code" ]]; then
  if command -v openssl >/dev/null; then
    pair_code=$(openssl rand -hex 32)
  else
    pair_code=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  fi
fi

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}
state_dir=${XDG_STATE_HOME:-"$HOME/.local/state"}
data_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}/telegram-agentknit-controller
unit_dir=$config_dir/systemd/user
config_file=$config_dir/telegram-agentknit-control.env

if [[ -e "$config_file" && "$force" -ne 1 ]]; then
  echo "Refusing to overwrite existing config: $config_file" >&2
  echo 'Use --force only if you intend to replace its bot token and pairing code.' >&2
  exit 1
fi

install -d -m 700 "$config_dir" "$state_dir" "$data_dir" "$unit_dir"
install -m 755 "$source_dir/telegram-agentknit-control.py" "$data_dir/telegram-agentknit-control.py"
install -m 644 "$source_dir/systemd/telegram-agentknit-controller.user.service" "$unit_dir/telegram-agentknit-controller.service"
(umask 077; printf 'BOT_TOKEN=%s\nPAIR_CODE=%s\n' "$bot_token" "$pair_code" >"$config_file")

systemctl --user daemon-reload
systemctl --user enable telegram-agentknit-controller.service
if [[ "$start" -eq 1 ]]; then
  systemctl --user restart telegram-agentknit-controller.service
  "$data_dir/telegram-agentknit-control.py" --check
fi

echo
echo 'Installed Telegram Agentknit Controller.'
echo "Pair from Telegram with: /pair $pair_code"
echo 'Logs: systemctl --user status telegram-agentknit-controller.service'
echo 'To keep it running after logout: loginctl enable-linger' "${USER}"
