#!/usr/bin/env bash
set -Eeuo pipefail

APP_REPO_URL="https://github.com/dashdogy/FlaskMegaDownloader.git"
APP_BRANCH="master"
APP_DIR="/opt/flask-mega-downloader"
APP_VENV="${APP_DIR}/.venv"
CONFIG_DIR="/etc/flask-mega-downloader"
CONFIG_FILE="${CONFIG_DIR}/config.py"
SERVICE_NAME="flask-mega-downloader"
SERVICE_DEST="/etc/systemd/system/${SERVICE_NAME}.service"
RUNTIME_USER="www-data"
RUNTIME_GROUP="www-data"
RUNTIME_HOME="/var/www"
MEGA_KEYRING="/usr/share/keyrings/mega.gpg"
MEGA_SOURCE_LIST="/etc/apt/sources.list.d/megacmd.list"

log() {
  printf '[INFO] %s\n' "$*"
}

warn() {
  printf '[WARN] %s\n' "$*" >&2
}

die() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "Run this script as root inside the target LXC."
  fi
}

require_systemd() {
  command -v systemctl >/dev/null 2>&1 || die "systemctl is required."
}

detect_os() {
  [[ -r /etc/os-release ]] || die "/etc/os-release is missing."
  # shellcheck disable=SC1091
  source /etc/os-release

  case "${ID:-}:${VERSION_ID:-}" in
    debian:11) MEGA_REPO="Debian_11" ;;
    debian:12) MEGA_REPO="Debian_12" ;;
    debian:13) MEGA_REPO="Debian_13" ;;
    ubuntu:20.04) MEGA_REPO="xUbuntu_20.04" ;;
    ubuntu:22.04) MEGA_REPO="xUbuntu_22.04" ;;
    ubuntu:24.04) MEGA_REPO="xUbuntu_24.04" ;;
    *) die "Unsupported OS: ${PRETTY_NAME:-unknown}. Supported: Debian 11/12/13, Ubuntu 20.04/22.04/24.04." ;;
  esac

  MEGA_REPO_URL="https://mega.nz/linux/repo/${MEGA_REPO}"
  OS_FRIENDLY_NAME="${PRETTY_NAME:-${ID:-unknown}}"
}

apt_install_base() {
  export DEBIAN_FRONTEND=noninteractive
  log "Installing base packages."
  apt-get update
  apt-get install -y ca-certificates curl git gnupg python3 python3-venv python3-pip
}

install_megacmd() {
  local tmp_key
  tmp_key="$(mktemp)"

  log "Configuring MEGAcmd APT repository for ${OS_FRIENDLY_NAME}."
  curl -fsSL "${MEGA_REPO_URL}/Release.key" -o "${tmp_key}"
  gpg --dearmor --yes --output "${MEGA_KEYRING}" "${tmp_key}"
  rm -f "${tmp_key}"
  chmod 0644 "${MEGA_KEYRING}"

  printf 'deb [signed-by=%s] %s ./\n' "${MEGA_KEYRING}" "${MEGA_REPO_URL}" > "${MEGA_SOURCE_LIST}"

  log "Installing MEGAcmd."
  apt-get update
  apt-get install -y megacmd
}

verify_repo_matches() {
  local origin_url
  origin_url="$(git -C "${APP_DIR}" remote get-url origin 2>/dev/null || true)"
  [[ "${origin_url}" == "${APP_REPO_URL}" ]] || die "Managed checkout points to '${origin_url}', expected '${APP_REPO_URL}'."
}

ensure_clean_checkout() {
  git -C "${APP_DIR}" diff --quiet --ignore-submodules -- || die "Managed checkout has tracked modifications. Aborting update."
  git -C "${APP_DIR}" diff --cached --quiet --ignore-submodules -- || die "Managed checkout has staged changes. Aborting update."
}

prepare_checkout() {
  if [[ -d "${APP_DIR}/.git" ]]; then
    log "Updating managed checkout in ${APP_DIR}."
    verify_repo_matches
    ensure_clean_checkout
    git -C "${APP_DIR}" fetch origin "${APP_BRANCH}"
    git -C "${APP_DIR}" checkout "${APP_BRANCH}"
    git -C "${APP_DIR}" pull --ff-only origin "${APP_BRANCH}"
    return
  fi

  if [[ -e "${APP_DIR}" ]]; then
    die "${APP_DIR} exists but is not a managed git checkout."
  fi

  log "Cloning application repository."
  git clone --branch "${APP_BRANCH}" --single-branch "${APP_REPO_URL}" "${APP_DIR}"
}

setup_runtime_dirs() {
  log "Ensuring runtime directories and permissions."
  install -d -m 0755 "${CONFIG_DIR}"
  install -d -m 0755 "${APP_DIR}/data"
  install -d -m 0755 /srv/mega-downloads
  install -d -m 0755 /srv/media/incoming
  install -d -m 0755 "${RUNTIME_HOME}"

  chown "${RUNTIME_USER}:${RUNTIME_GROUP}" "${RUNTIME_HOME}"
  chown -R "${RUNTIME_USER}:${RUNTIME_GROUP}" "${APP_DIR}/data" /srv/mega-downloads /srv/media/incoming
}

setup_python_env() {
  log "Creating or updating Python virtual environment."
  python3 -m venv "${APP_VENV}"
  "${APP_VENV}/bin/pip" install --upgrade pip
  "${APP_VENV}/bin/pip" install -r "${APP_DIR}/requirements.txt"
}

write_default_config() {
  if [[ -f "${CONFIG_FILE}" ]]; then
    log "Keeping existing config at ${CONFIG_FILE}."
    chmod 0644 "${CONFIG_FILE}"
    return
  fi

  log "Creating default config at ${CONFIG_FILE}."
  python3 - "${APP_DIR}/config.example.py" "${CONFIG_FILE}" <<'PY'
from pathlib import Path
import secrets
import sys

template_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])
content = template_path.read_text(encoding="utf-8")
content = content.replace("replace-this-secret", secrets.token_urlsafe(48), 1)
target_path.write_text(content, encoding="utf-8")
PY
  chmod 0644 "${CONFIG_FILE}"
}

install_service() {
  local service_source="${APP_DIR}/flask-mega-downloader.service"
  [[ -f "${service_source}" ]] || die "Service file not found at ${service_source}."

  log "Installing systemd service."
  install -m 0644 "${service_source}" "${SERVICE_DEST}"
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"
}

mega_env_cmd() {
  runuser -u "${RUNTIME_USER}" -- env HOME="${RUNTIME_HOME}" "$@"
}

prompt_mega_login() {
  if mega_env_cmd mega-whoami >/dev/null 2>&1; then
    log "MEGAcmd session already exists for ${RUNTIME_USER}."
    return
  fi

  warn "No MEGAcmd session found for ${RUNTIME_USER}."
  if [[ ! -t 0 ]]; then
    warn "No interactive terminal available. Skipping MEGA login prompt."
    return
  fi

  local reply
  read -r -p "Log in to MEGA now for ${RUNTIME_USER}? [Y/n]: " reply
  reply="${reply:-Y}"
  if [[ ! "${reply}" =~ ^[Yy]([Ee][Ss])?$ ]]; then
    warn "Skipping MEGA login. Downloads that require a MEGA session may fail until you log in."
    return
  fi

  if mega_env_cmd mega-login; then
    if mega_env_cmd mega-whoami >/dev/null 2>&1; then
      log "MEGAcmd login succeeded."
      return
    fi
  fi

  warn "MEGAcmd login did not complete successfully. You can retry later with: runuser -u ${RUNTIME_USER} -- env HOME=${RUNTIME_HOME} mega-login"
}

print_summary() {
  local app_host app_port lan_ip

  readarray -t config_values < <(python3 - "${CONFIG_FILE}" <<'PY'
from importlib.util import module_from_spec, spec_from_file_location
import sys

spec = spec_from_file_location("helper_config", sys.argv[1])
module = module_from_spec(spec)
spec.loader.exec_module(module)
print(getattr(module, "HOST", "0.0.0.0"))
print(getattr(module, "PORT", 8080))
PY
)

  app_host="${config_values[0]:-0.0.0.0}"
  app_port="${config_values[1]:-8080}"
  lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  lan_ip="${lan_ip:-127.0.0.1}"

  printf '\n'
  log "Installation/update complete."
  if [[ "${app_host}" == "0.0.0.0" || "${app_host}" == "::" ]]; then
    log "Open the app at: http://${lan_ip}:${app_port}"
  else
    log "Open the app at: http://${app_host}:${app_port}"
  fi
  log "Service status:"
  systemctl --no-pager --full status "${SERVICE_NAME}" || true
}

main() {
  require_root
  require_systemd
  detect_os
  log "Starting Flask Mega Downloader install/update on ${OS_FRIENDLY_NAME}."
  apt_install_base
  install_megacmd
  prepare_checkout
  setup_runtime_dirs
  setup_python_env
  write_default_config
  install_service
  prompt_mega_login
  print_summary
}

main "$@"
