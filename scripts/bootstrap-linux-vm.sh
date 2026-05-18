#!/usr/bin/env bash
set -euo pipefail

INSTALL_CODEX=1
INSTALL_DOCKER=1
INSTALL_PROGRAMBENCH=1
INSTALL_WRAPPER=1
CONFIGURE_CODEX_FAST=1
CODEX_USER="$(id -un)"

usage() {
  cat <<'EOF'
Usage:
  scripts/bootstrap-linux-vm.sh [options]

Options:
  --codex-user USER          User that will run Codex/harness commands (default: current user)
  --skip-codex              Do not install @openai/codex with npm
  --skip-docker             Do not install Docker
  --skip-programbench       Do not clone/sync sibling ../ProgramBench
  --skip-wrapper            Do not install /usr/local/bin/pb-target-exec sudo wrapper
  --skip-codex-fast-config  Do not set Codex fast mode as the VM default
  -h, --help                Show this help

Run this on a fresh Ubuntu x86_64 VM from the goalbench repo root.
After it finishes, log out/in if the script added your user to the docker group.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --codex-user)
      CODEX_USER="$2"
      shift 2
      ;;
    --skip-codex)
      INSTALL_CODEX=0
      shift
      ;;
    --skip-docker)
      INSTALL_DOCKER=0
      shift
      ;;
    --skip-programbench)
      INSTALL_PROGRAMBENCH=0
      shift
      ;;
    --skip-wrapper)
      INSTALL_WRAPPER=0
      shift
      ;;
    --skip-codex-fast-config)
      CONFIGURE_CODEX_FAST=0
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This bootstrap is for Linux VMs." >&2
  exit 1
fi

if [[ "$(uname -m)" != "x86_64" && "$(uname -m)" != "amd64" ]]; then
  echo "ProgramBench Docker images are linux/amd64; use an x86_64 VM." >&2
  exit 1
fi

if ! id -u "$CODEX_USER" >/dev/null 2>&1; then
  sudo useradd -m -s /bin/bash "$CODEX_USER"
fi

sudo apt-get update
sudo apt-get install -y \
  bash-completion \
  build-essential \
  ca-certificates \
  curl \
  git \
  gnupg \
  iproute2 \
  iptables \
  jq \
  lsb-release \
  nodejs \
  npm \
  pkg-config \
  python3 \
  python3-venv \
  ripgrep \
  rsync \
  tmux \
  unzip

if [[ "$INSTALL_DOCKER" -eq 1 ]] && ! command -v docker >/dev/null; then
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo rm -f /etc/apt/keyrings/docker.gpg
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

if command -v docker >/dev/null; then
  sudo systemctl enable --now docker
  if ! id -nG "$CODEX_USER" | tr ' ' '\n' | grep -qx docker; then
    sudo usermod -aG docker "$CODEX_USER"
    echo "added $CODEX_USER to docker group; log out/in before running Docker as that user"
  fi
fi

if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
if [[ -x "$HOME/.local/bin/uv" ]]; then
  sudo ln -sf "$HOME/.local/bin/uv" /usr/local/bin/uv
fi

if [[ "$INSTALL_CODEX" -eq 1 ]] && ! command -v codex >/dev/null; then
  sudo npm install -g @openai/codex
fi

if [[ "$CONFIGURE_CODEX_FAST" -eq 1 ]]; then
  USER_HOME="$(getent passwd "$CODEX_USER" | cut -d: -f6)"
  CODEX_HOME="$USER_HOME/.codex"
  sudo install -d -o "$CODEX_USER" -g "$CODEX_USER" "$CODEX_HOME"
  sudo tee "$CODEX_HOME/config.toml" >/dev/null <<EOF
service_tier = "fast"

[features]
goals = true
fast_mode = true

[projects."$USER_HOME"]
trust_level = "trusted"

[projects."$USER_HOME/goalbench"]
trust_level = "trusted"

[projects."$USER_HOME/pb-goal-runs"]
trust_level = "trusted"
EOF
  sudo chown "$CODEX_USER:$CODEX_USER" "$CODEX_HOME/config.toml"
  sudo install -d /etc/codex
  sudo tee /etc/codex/managed_config.toml >/dev/null <<'EOF'
approval_policy = "never"
sandbox_mode = "danger-full-access"
service_tier = "fast"

[features]
goals = true
fast_mode = true
EOF
fi

uv sync

if [[ "$INSTALL_PROGRAMBENCH" -eq 1 ]]; then
  scripts/bootstrap-programbench.sh
fi

if [[ "$INSTALL_WRAPPER" -eq 1 ]]; then
  scripts/install-target-wrapper.sh "$CODEX_USER"
fi

echo
echo "Installed versions:"
command -v docker >/dev/null && docker --version || true
command -v uv >/dev/null && uv --version || true
command -v codex >/dev/null && codex --version || echo "codex not found; install/login before running sweeps"
command -v tmux >/dev/null && tmux -V || true

echo
echo "Next checks:"
echo "  docker run --rm hello-world"
echo "  codex login"
echo "  scripts/doctor.sh configs/linux-smoke-miniswecompat-xhigh.json"
echo
echo "Start smoke:"
echo "  scripts/start-sweep-tmux.sh configs/linux-smoke-miniswecompat-xhigh.json"
