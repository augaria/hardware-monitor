#!/bin/bash
# Hardware Monitor — Agent Installer / Updater
#
# First install (interactive — download first, then run; the script reads
# stdin for prompts, so `curl | sudo bash` will not work):
#   curl -fLO https://raw.githubusercontent.com/augaria/hardware-monitor/main/scripts/install.sh
#   sudo bash install.sh
#
# Update existing install (non-interactive):
#   sudo bash install.sh --update
#
# Force-skip NVIDIA even if GPU is detected:
#   sudo bash install.sh --no-nvidia

set -euo pipefail

# ── Constants ─────────────────────────────────────────────────────────────────

PACKAGE_BASE_URL="git+https://github.com/augaria/hardware-monitor.git#subdirectory=agent"
PACKAGE_NAME="hardware-monitor-agent"
CONDA_ENV_NAME="hardware_monitor"
SERVICE_NAME="hardware-monitor-agent"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
DEFAULT_THRESHOLDS_FILE="/etc/hardware-monitor/thresholds.conf"

# ── Colors ────────────────────────────────────────────────────────────────────

if [[ -t 1 ]]; then
  BOLD='\033[1m'
  DIM='\033[2m'
  GREEN='\033[0;32m'
  YELLOW='\033[0;33m'
  RED='\033[0;31m'
  CYAN='\033[0;36m'
  RESET='\033[0m'
else
  BOLD=''; DIM=''; GREEN=''; YELLOW=''; RED=''; CYAN=''; RESET=''
fi

info()    { echo -e "  ${CYAN}•${RESET} $*"; }
success() { echo -e "  ${GREEN}✓${RESET} $*"; }
warn()    { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
error()   { echo -e "  ${RED}✗${RESET} $*" >&2; }
step()    { echo -e "\n${BOLD}${CYAN}▶ $*${RESET}"; }
banner()  {
  echo ""
  echo -e "${BOLD}${CYAN}  ╔══════════════════════════════════════╗"
  echo -e "  ║     Hardware Monitor Agent Setup     ║"
  echo -e "  ╚══════════════════════════════════════╝${RESET}"
  echo ""
}

# ── Argument parsing ──────────────────────────────────────────────────────────

MODE="install"   # install | update
NO_NVIDIA=false
THRESHOLDS_FILE=""           # set via flag or interactive prompt; "" = use server defaults
THRESHOLDS_FILE_EXPLICIT=false   # true once --thresholds-file is parsed (skip prompt)
while [[ $# -gt 0 ]]; do
  case $1 in
    --update)    MODE="update";   shift ;;
    --no-nvidia) NO_NVIDIA=true;  shift ;;
    --thresholds-file)
      THRESHOLDS_FILE="${2:-}"; THRESHOLDS_FILE_EXPLICIT=true; shift 2 ;;
    --thresholds-file=*)
      THRESHOLDS_FILE="${1#*=}"; THRESHOLDS_FILE_EXPLICIT=true; shift ;;
    -h|--help)
      echo "Usage: $0 [--update] [--no-nvidia] [--thresholds-file PATH]"
      echo "  (no args)              Interactive first-time install"
      echo "  --update               Non-interactive: reinstall package + restart service"
      echo "  --no-nvidia            Skip NVIDIA GPU support even if a GPU is detected"
      echo "  --thresholds-file PATH Per-agent alert threshold overrides (skip prompt)"
      echo "                         Pass empty string to use central server defaults only"
      exit 0 ;;
    *) error "Unknown argument: $1"; exit 1 ;;
  esac
done

# ── Root check ────────────────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
  error "This script must be run as root (use: sudo bash $0)"
  exit 1
fi

# ── Locate conda ──────────────────────────────────────────────────────────────

find_conda() {
  # Try the invoking user's home dir first (sudo preserves $HOME in most distros)
  local user_home="${HOME}"
  # If sudo, try the real user's home
  if [[ -n "${SUDO_USER:-}" ]]; then
    user_home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
  fi

  for candidate in \
      "${user_home}/miniconda3" \
      "${user_home}/anaconda3" \
      "${user_home}/miniforge3" \
      "/opt/conda" \
      "/opt/miniconda3" \
      "/opt/anaconda3"; do
    if [[ -x "${candidate}/bin/conda" ]]; then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

banner

step "Checking prerequisites"

CONDA_BASE=""
if command -v conda &>/dev/null; then
  CONDA_BASE=$(conda info --base 2>/dev/null || true)
fi
if [[ -z "$CONDA_BASE" ]]; then
  CONDA_BASE=$(find_conda || true)
fi

if [[ -z "$CONDA_BASE" ]]; then
  warn "conda not found. Please install Miniconda or Anaconda first."
  warn "  https://docs.conda.io/en/latest/miniconda.html"
  exit 1
fi
success "conda found at ${CONDA_BASE}"

CONDA_BIN="${CONDA_BASE}/bin/conda"
ENV_BIN="${CONDA_BASE}/envs/${CONDA_ENV_NAME}/bin"
AGENT_CMD="${ENV_BIN}/hardware-monitor-agent"

# ── UPDATE mode ───────────────────────────────────────────────────────────────

if [[ "$MODE" == "update" ]]; then
  step "Update mode — reading existing configuration"

  if [[ ! -f "$SERVICE_FILE" ]]; then
    error "No existing service file found at ${SERVICE_FILE}"
    error "Run without --update to perform a first-time install."
    exit 1
  fi

  # Parse args from ExecStart line
  EXEC_LINE=$(grep "^ExecStart=" "$SERVICE_FILE" | head -1)
  SERVER_URL=$(echo "$EXEC_LINE"  | grep -oP '(?<=--server )\S+' || true)
  MACHINE_NAME=$(echo "$EXEC_LINE" | grep -oP '(?<=--name )\S+'  || true)
  INTERVAL=$(echo "$EXEC_LINE"    | grep -oP '(?<=--interval )\S+' || true)
  # Preserve existing --thresholds path unless the operator passed --thresholds-file explicitly
  if [[ "$THRESHOLDS_FILE_EXPLICIT" != "true" ]]; then
    THRESHOLDS_FILE=$(echo "$EXEC_LINE" | grep -oP '(?<=--thresholds )\S+' || true)
  fi

  info "  Server     : ${SERVER_URL:-<not found>}"
  info "  Name       : ${MACHINE_NAME:-<not found>}"
  info "  Interval   : ${INTERVAL:-<not found>}s"
  info "  Thresholds : ${THRESHOLDS_FILE:-<server defaults>}"

  if [[ -z "$SERVER_URL" || -z "$MACHINE_NAME" ]]; then
    error "Could not parse existing config from service file."
    error "Edit ${SERVICE_FILE} manually or reinstall."
    exit 1
  fi
fi

# ── INSTALL mode: prompt for config ──────────────────────────────────────────

if [[ "$MODE" == "install" ]]; then
  step "Configuration"

  while true; do
    read -rp "  Central server URL (e.g. http://192.168.1.100:5000): " SERVER_URL
    [[ -n "$SERVER_URL" ]] && break
    warn "Server URL is required."
  done

  DEFAULT_NAME=$(hostname)
  read -rp "  Machine name [default: ${DEFAULT_NAME}]: " MACHINE_NAME
  MACHINE_NAME="${MACHINE_NAME:-$DEFAULT_NAME}"

  read -rp "  Report interval in seconds [default: 60]: " INTERVAL
  INTERVAL="${INTERVAL:-60}"

  if [[ "$THRESHOLDS_FILE_EXPLICIT" != "true" ]]; then
    echo ""
    echo -e "  ${DIM}Per-agent alert threshold overrides let this machine use stricter${RESET}"
    echo -e "  ${DIM}or looser limits than the central server's defaults. Skip to use${RESET}"
    echo -e "  ${DIM}the server defaults for every metric.${RESET}"
    read -rp "  Configure per-agent threshold overrides? [y/N]: " WANT_THRESH
    if [[ "$WANT_THRESH" =~ ^[Yy]$ ]]; then
      read -rp "  Path to thresholds file [default: ${DEFAULT_THRESHOLDS_FILE}]: " THRESHOLDS_FILE
      THRESHOLDS_FILE="${THRESHOLDS_FILE:-$DEFAULT_THRESHOLDS_FILE}"
    fi
  fi

  echo ""
  echo -e "  ${DIM}Server     : ${SERVER_URL}${RESET}"
  echo -e "  ${DIM}Name       : ${MACHINE_NAME}${RESET}"
  echo -e "  ${DIM}Interval   : ${INTERVAL}s${RESET}"
  echo -e "  ${DIM}Thresholds : ${THRESHOLDS_FILE:-<server defaults>}${RESET}"
  echo ""
  read -rp "  Proceed? [Y/n]: " CONFIRM
  CONFIRM="${CONFIRM:-Y}"
  if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
  fi
fi

# ── Threshold file: create from template if requested but missing ────────────

if [[ -n "$THRESHOLDS_FILE" && ! -f "$THRESHOLDS_FILE" ]]; then
  step "Creating threshold override template"
  THRESHOLDS_DIR=$(dirname "$THRESHOLDS_FILE")
  mkdir -p "$THRESHOLDS_DIR"
  cat > "$THRESHOLDS_FILE" <<'THRESH_EOF'
# Hardware Monitor — per-agent alert threshold overrides
#
# Any key you leave commented out (or blank) falls back to the central
# server's default for that metric, so you only need to specify the ones
# you want to change.
#
# Format: KEY=VALUE  (one per line, '#' starts a comment)
# Units : % for usage, °C for temperature.
# Apply : edit this file, then `sudo systemctl restart hardware-monitor-agent`.

#ALERT_CPU_USAGE=85
#ALERT_CPU_TEMP=80
#ALERT_MEMORY_USAGE=85
#ALERT_MOTHERBOARD_TEMP=70
#ALERT_GPU_USAGE=90
#ALERT_GPU_TEMP=80
#ALERT_GPU_MEMORY=85
#ALERT_DISK_TEMP=55

# UI: arrays smaller than this many GB auto-collapse on the dashboard
# (along with detected SSD-cache arrays). Falls back to server default.
#HIDE_ARRAYS_BELOW_GB=10
THRESH_EOF
  chmod 644 "$THRESHOLDS_FILE"
  success "Wrote template to ${THRESHOLDS_FILE} (all keys commented — edit to enable)"
fi

# ── Create conda environment (skip if already exists) ─────────────────────────

step "Conda environment"

if "${CONDA_BIN}" env list | grep -qE "^${CONDA_ENV_NAME}\s"; then
  success "Environment '${CONDA_ENV_NAME}' already exists — skipping creation"
else
  info "Creating conda environment '${CONDA_ENV_NAME}' with Python 3.12..."
  "${CONDA_BIN}" create -n "${CONDA_ENV_NAME}" python=3.12 -y -q
  success "Created '${CONDA_ENV_NAME}'"
fi

# ── Detect NVIDIA GPU ─────────────────────────────────────────────────────────

step "Detecting hardware"

NVIDIA_EXTRA=""
if [[ "$NO_NVIDIA" == "true" ]]; then
  info "NVIDIA support skipped (--no-nvidia)"
elif command -v nvidia-smi &>/dev/null; then
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || true)
  success "NVIDIA GPU detected${GPU_NAME:+: ${GPU_NAME}} — will install nvidia-ml-py3"
  NVIDIA_EXTRA="[nvidia]"
else
  info "No NVIDIA GPU detected — skipping nvidia-ml-py3"
fi

# ── Install / reinstall package from GitHub ───────────────────────────────────

step "Installing hardware-monitor-agent package"
info "Fetching latest from GitHub and rebuilding..."
"${ENV_BIN}/pip" install --quiet --force-reinstall --root-user-action=ignore "${PACKAGE_NAME}${NVIDIA_EXTRA} @ ${PACKAGE_BASE_URL}"
success "Package installed${NVIDIA_EXTRA:+ (with NVIDIA support)}"

if [[ ! -x "$AGENT_CMD" ]]; then
  error "Entry point not found after install: ${AGENT_CMD}"
  exit 1
fi
success "Entry point: ${AGENT_CMD}"

# ── Write systemd service ─────────────────────────────────────────────────────

step "Systemd service"
info "Writing ${SERVICE_FILE}..."

EXEC_ARGS="--server ${SERVER_URL} --name ${MACHINE_NAME} --interval ${INTERVAL}"
if [[ -n "$THRESHOLDS_FILE" ]]; then
  EXEC_ARGS="${EXEC_ARGS} --thresholds ${THRESHOLDS_FILE}"
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Hardware Monitor Agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=${AGENT_CMD} ${EXEC_ARGS}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" --quiet

if systemctl is-active --quiet "${SERVICE_NAME}"; then
  info "Restarting service..."
  systemctl restart "${SERVICE_NAME}"
  success "Service restarted"
else
  info "Starting service..."
  systemctl start "${SERVICE_NAME}"
  success "Service started"
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${GREEN}  ╔══════════════════════════════════════╗"
echo -e "  ║         Installation complete        ║"
echo -e "  ╚══════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${DIM}Machine    : ${MACHINE_NAME}${RESET}"
echo -e "  ${DIM}Server     : ${SERVER_URL}${RESET}"
echo -e "  ${DIM}Interval   : ${INTERVAL}s${RESET}"
echo -e "  ${DIM}Thresholds : ${THRESHOLDS_FILE:-<server defaults>}${RESET}"
echo ""
info "Useful commands:"
echo -e "    ${DIM}systemctl status  ${SERVICE_NAME}${RESET}"
echo -e "    ${DIM}journalctl -u ${SERVICE_NAME} -f${RESET}"
echo -e "    ${DIM}systemctl stop    ${SERVICE_NAME}${RESET}"
if [[ -n "$THRESHOLDS_FILE" ]]; then
  echo -e "    ${DIM}edit ${THRESHOLDS_FILE} && systemctl restart ${SERVICE_NAME}${RESET}"
fi
echo ""
info "To update the agent: sudo bash install.sh --update"
echo ""
