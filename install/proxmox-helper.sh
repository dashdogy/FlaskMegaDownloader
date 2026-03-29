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
MEGA_KEYRING="/usr/share/keyrings/meganz-archive-keyring.gpg"
MEGA_SOURCE_LIST="/etc/apt/sources.list.d/megacmd.list"
DEFAULT_LISTEN_PORT="8090"
RUNTIME_STATE_FILE_REL="data/jobs.json"
RUNTIME_STATE_BACKUP=""

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

strip_mega_lines_from_list() {
  local file="$1"
  local tmp_file

  tmp_file="$(mktemp)"
  grep -v 'https://mega.nz/linux/repo/' "${file}" > "${tmp_file}" || true

  if [[ -s "${tmp_file}" ]]; then
    cat "${tmp_file}" > "${file}"
  else
    rm -f "${file}"
  fi
  rm -f "${tmp_file}"
}

normalize_mega_apt_sources() {
  local file

  log "Normalizing any existing MEGA APT source entries."

  if [[ -f /etc/apt/sources.list ]] && grep -q 'https://mega.nz/linux/repo/' /etc/apt/sources.list; then
    strip_mega_lines_from_list /etc/apt/sources.list
    warn "Removed MEGA repository lines from /etc/apt/sources.list."
  fi

  shopt -s nullglob
  for file in /etc/apt/sources.list.d/*; do
    [[ -f "${file}" ]] || continue
    [[ "${file}" == "${MEGA_SOURCE_LIST}" ]] && continue
    if ! grep -q 'https://mega.nz/linux/repo/' "${file}"; then
      continue
    fi

    case "${file}" in
      *.list)
        strip_mega_lines_from_list "${file}"
        warn "Removed conflicting MEGA repository lines from ${file}."
        ;;
      *.sources)
        rm -f "${file}"
        warn "Removed conflicting MEGA repository source file ${file}."
        ;;
      *)
        rm -f "${file}"
        warn "Removed conflicting MEGA repository file ${file}."
        ;;
    esac
  done
  shopt -u nullglob
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

discover_makemkv_version() {
  curl -fsSL https://www.makemkv.com/download/ \
    | grep -oE 'MakeMKV v?[0-9]+\.[0-9]+\.[0-9]+' \
    | head -n 1 \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+'
}

install_bluray_dependencies() {
  local makemkv_version workdir jobs

  export DEBIAN_FRONTEND=noninteractive
  log "Installing Blu-ray remux dependencies."
  apt-get install -y mediainfo build-essential pkg-config libc6-dev libssl-dev libexpat1-dev libavcodec-dev libgl1-mesa-dev qtbase5-dev zlib1g-dev

  if command -v makemkvcon >/dev/null 2>&1; then
    log "MakeMKV CLI is already available."
    return
  fi

  makemkv_version="$(discover_makemkv_version || true)"
  if [[ -z "${makemkv_version}" ]]; then
    warn "Could not detect the latest MakeMKV version from makemkv.com. Blu-ray remuxing will stay unavailable until makemkvcon is installed."
    return
  fi

  workdir="$(mktemp -d)"
  jobs="$(nproc 2>/dev/null || echo 1)"
  log "Building MakeMKV CLI ${makemkv_version} from the official source tarballs."

  if ! (
    cd "${workdir}" && \
    curl -fsSL -O "https://www.makemkv.com/download/makemkv-oss-${makemkv_version}.tar.gz" && \
    curl -fsSL -O "https://www.makemkv.com/download/makemkv-bin-${makemkv_version}.tar.gz" && \
    tar -xzf "makemkv-oss-${makemkv_version}.tar.gz" && \
    tar -xzf "makemkv-bin-${makemkv_version}.tar.gz" && \
    cd "makemkv-oss-${makemkv_version}" && \
    ./configure --prefix=/usr --disable-gui && \
    make -j"${jobs}" && \
    make install && \
    cd "${workdir}/makemkv-bin-${makemkv_version}" && \
    mkdir -p tmp && \
    printf 'accepted\n' > tmp/eula_accepted && \
    make -j"${jobs}" && \
    make install
  ); then
    warn "Automatic MakeMKV build failed. The app will still install, but Blu-ray remuxing will remain unavailable until makemkvcon is installed manually."
    rm -rf "${workdir}"
    return
  fi

  rm -rf "${workdir}"
  ldconfig || true

  if command -v makemkvcon >/dev/null 2>&1; then
    log "MakeMKV CLI installed successfully."
    warn "If Blu-ray remux jobs later fail with beta key or registration errors, activate MakeMKV manually and rerun the helper."
    return
  fi

  warn "MakeMKV build completed but makemkvcon is still not callable. Blu-ray remuxing will remain unavailable until MakeMKV is fixed manually."
}

verify_bluray_runtime() {
  if ! command -v mediainfo >/dev/null 2>&1; then
    warn "mediainfo is not available on PATH. Blu-ray remux verification will be unavailable."
  fi

  if ! command -v makemkvcon >/dev/null 2>&1; then
    warn "makemkvcon is not available on PATH. Blu-ray remuxing will remain unavailable until MakeMKV is installed."
    return
  fi

  if runuser -u "${RUNTIME_USER}" -- env HOME="${RUNTIME_HOME}" makemkvcon --help >/dev/null 2>&1; then
    log "Verified makemkvcon is callable for ${RUNTIME_USER}."
  else
    warn "makemkvcon is installed but not callable for ${RUNTIME_USER}. Check permissions or library dependencies before using Blu-ray remuxing."
  fi
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

preserve_runtime_state_for_update() {
  local runtime_state_file="${APP_DIR}/${RUNTIME_STATE_FILE_REL}"

  if ! git -C "${APP_DIR}" ls-files --error-unmatch "${RUNTIME_STATE_FILE_REL}" >/dev/null 2>&1; then
    return
  fi

  if git -C "${APP_DIR}" diff --quiet --ignore-submodules -- "${RUNTIME_STATE_FILE_REL}" \
    && git -C "${APP_DIR}" diff --cached --quiet --ignore-submodules -- "${RUNTIME_STATE_FILE_REL}"; then
    return
  fi

  if [[ -f "${runtime_state_file}" ]]; then
    RUNTIME_STATE_BACKUP="$(mktemp)"
    cp -f "${runtime_state_file}" "${RUNTIME_STATE_BACKUP}"
    log "Temporarily backing up runtime job state before update."
  fi

  git -C "${APP_DIR}" reset --quiet HEAD -- "${RUNTIME_STATE_FILE_REL}" || true
  git -C "${APP_DIR}" checkout -- "${RUNTIME_STATE_FILE_REL}" || true
}

restore_runtime_state_after_update() {
  local runtime_state_file="${APP_DIR}/${RUNTIME_STATE_FILE_REL}"

  if [[ -z "${RUNTIME_STATE_BACKUP}" || ! -f "${RUNTIME_STATE_BACKUP}" ]]; then
    return
  fi

  install -d -m 0755 "$(dirname "${runtime_state_file}")"
  cp -f "${RUNTIME_STATE_BACKUP}" "${runtime_state_file}"
  chown "${RUNTIME_USER}:${RUNTIME_GROUP}" "${runtime_state_file}" || true
  rm -f "${RUNTIME_STATE_BACKUP}"
  RUNTIME_STATE_BACKUP=""
  log "Restored runtime job state after update."
}

prepare_checkout() {
  if [[ -d "${APP_DIR}/.git" ]]; then
    log "Updating managed checkout in ${APP_DIR}."
    verify_repo_matches
    preserve_runtime_state_for_update
    ensure_clean_checkout
    git -C "${APP_DIR}" fetch origin "${APP_BRANCH}"
    git -C "${APP_DIR}" checkout "${APP_BRANCH}"
    git -C "${APP_DIR}" pull --ff-only origin "${APP_BRANCH}"
    restore_runtime_state_after_update
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
  local port_update_result

  if [[ -f "${CONFIG_FILE}" ]]; then
    log "Keeping existing config at ${CONFIG_FILE}."
    port_update_result="$(python3 - "${CONFIG_FILE}" <<'PY'
from pathlib import Path
import re
import sys

config_path = Path(sys.argv[1])
content = config_path.read_text(encoding="utf-8")
updated = re.sub(r"(?m)^PORT\s*=\s*8080\s*$", "PORT = 8090", content, count=1)
if updated != content:
    config_path.write_text(updated, encoding="utf-8")
    print("updated-port")
PY
)"
    if [[ "${port_update_result}" == "updated-port" ]]; then
      log "Updated legacy default port in ${CONFIG_FILE} from 8080 to 8090."
    fi
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

install_service_unit() {
  local service_source="${APP_DIR}/flask-mega-downloader.service"
  [[ -f "${service_source}" ]] || die "Service file not found at ${service_source}."

  log "Installing systemd service unit."
  install -m 0644 "${service_source}" "${SERVICE_DEST}"
  systemctl daemon-reload
}

configure_service() {
  local reply

  install_service_unit

  if systemctl is-enabled --quiet "${SERVICE_NAME}" >/dev/null 2>&1; then
    log "Restarting existing enabled systemd service."
    systemctl restart "${SERVICE_NAME}"
    return
  fi

  if [[ ! -t 0 ]]; then
    log "Systemd service is not enabled yet. Enabling it automatically."
    systemctl enable "${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
    return
  fi

  read -r -p "Enable the systemd service so the app starts automatically after LXC restart? [Y/n]: " reply
  reply="${reply:-Y}"
  if [[ "${reply}" =~ ^[Yy]([Ee][Ss])?$ ]]; then
    log "Enabling systemd service for automatic startup."
    systemctl enable "${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
    return
  fi

  if systemctl is-active --quiet "${SERVICE_NAME}" >/dev/null 2>&1; then
    log "Service remains disabled, but restarting the currently running instance to apply updates."
    systemctl restart "${SERVICE_NAME}"
  else
    warn "Systemd service installed but left disabled. Enable it later with: systemctl enable --now ${SERVICE_NAME}"
  fi
}

mega_env_cmd() {
  runuser -u "${RUNTIME_USER}" -- env HOME="${RUNTIME_HOME}" "$@"
}

prompt_mega_login() {
  local reply mega_email mega_password mega_mfa

  if mega_env_cmd mega-whoami >/dev/null 2>&1; then
    log "MEGAcmd session already exists for ${RUNTIME_USER}."
    return
  fi

  warn "No MEGAcmd session found for ${RUNTIME_USER}."
  if [[ ! -t 0 ]]; then
    warn "No interactive terminal available. Skipping MEGA login prompt."
    return
  fi

  read -r -p "Log in to MEGA now for ${RUNTIME_USER}? [Y/n]: " reply
  reply="${reply:-Y}"
  if [[ ! "${reply}" =~ ^[Yy]([Ee][Ss])?$ ]]; then
    warn "Skipping MEGA login. Downloads that require a MEGA session may fail until you log in."
    return
  fi

  read -r -p "MEGA email: " mega_email
  if [[ -z "${mega_email}" ]]; then
    warn "No MEGA email provided. Skipping login."
    return
  fi

  read -r -s -p "MEGA password: " mega_password
  printf '\n'
  if [[ -z "${mega_password}" ]]; then
    warn "No MEGA password provided. Skipping login."
    return
  fi

  read -r -p "MEGA MFA code (optional, press Enter to skip): " mega_mfa

  if [[ -n "${mega_mfa}" ]]; then
    if mega_env_cmd mega-login "--auth-code=${mega_mfa}" "${mega_email}" "${mega_password}"; then
      unset mega_password
      if mega_env_cmd mega-whoami >/dev/null 2>&1; then
        log "MEGAcmd login succeeded."
        return
      fi
    fi
  elif mega_env_cmd mega-login "${mega_email}" "${mega_password}"; then
    unset mega_password
    if mega_env_cmd mega-whoami >/dev/null 2>&1; then
      log "MEGAcmd login succeeded."
      return
    fi
  fi

  unset mega_password
  warn "MEGAcmd login did not complete successfully. You can retry later with: runuser -u ${RUNTIME_USER} -- env HOME=${RUNTIME_HOME} mega-login your@email.example 'your-password'"
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
print(getattr(module, "PORT", 8090))
PY
)

  app_host="${config_values[0]:-0.0.0.0}"
  app_port="${config_values[1]:-${DEFAULT_LISTEN_PORT}}"
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
  trap restore_runtime_state_after_update EXIT
  detect_os
  log "Starting Flask Mega Downloader install/update on ${OS_FRIENDLY_NAME}."
  normalize_mega_apt_sources
  apt_install_base
  install_megacmd
  install_bluray_dependencies
  prepare_checkout
  setup_runtime_dirs
  setup_python_env
  write_default_config
  configure_service
  prompt_mega_login
  verify_bluray_runtime
  print_summary
}

main "$@"
