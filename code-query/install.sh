#!/usr/bin/env bash
# install.sh — Linux / macOS 用セットアップ入口（OS だけの状態から実行できる）。
#
# 本ファイルの責務は 2 つだけ:
#   1. Python 3.11+（+ venv モジュール）と git が無ければ OS のパッケージマネージャで導入する
#   2. 以降の判断を install.py（→ kit/kit_setup.py）へ委譲する
#
# 使い方（導入先リポジトリのルートで実行）:
#   bash <kit>/install.sh
#   bash <kit>/install.sh --with-gui --with-watch
#   bash <kit>/install.sh --repo-root /path/to/other-repo --force
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIN_MAJOR=3
MIN_MINOR=11
REPO_ROOT="$(pwd)"
SKIP_PREREQ=0
PASSTHROUGH=()

while [ $# -gt 0 ]; do
    case "$1" in
        --repo-root) REPO_ROOT="$2"; shift 2 ;;
        --python) PYTHON="$2"; shift 2 ;;
        --skip-prereq) SKIP_PREREQ=1; shift ;;
        *) PASSTHROUGH+=("$1"); shift ;;
    esac
done

step() { echo "[install] $*"; }

python_ok() {
    local exe="$1"
    [ -n "$exe" ] || return 1
    command -v "$exe" >/dev/null 2>&1 || [ -x "$exe" ] || return 1
    "$exe" -c "import sys;raise SystemExit(0 if sys.version_info >= (${MIN_MAJOR}, ${MIN_MINOR}) else 1)" \
        >/dev/null 2>&1
}

# Debian / Ubuntu は venv の作成に必要な ensurepip を別パッケージ（python3-venv）で配る。
# インタプリタが在ることと `python3 -m venv` が通ることは別なので、個別に確認する。
venv_supported() {
    "$1" -c "import ensurepip" >/dev/null 2>&1
}

resolve_python() {
    local candidate
    for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 python3 python; do
        if [ -n "$candidate" ] && python_ok "$candidate"; then
            command -v "$candidate" 2>/dev/null || echo "$candidate"
            return 0
        fi
    done
    return 1
}

sudo_if_needed() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "root 権限も sudo も無いため導入できません: $*" >&2
        return 1
    fi
}

install_packages() {
    # $@ = 汎用パッケージ名（python / git）。マネージャごとに実名へ割り当てる。
    if [ "$(uname -s)" = "Darwin" ]; then
        if ! command -v brew >/dev/null 2>&1; then
            echo "Homebrew が必要です: https://brew.sh/ を参照して導入してください。" >&2
            return 1
        fi
        for pkg in "$@"; do
            case "$pkg" in
                python) step "brew install python@3.12"; brew install python@3.12 ;;
                git) step "brew install git"; brew install git ;;
            esac
        done
        return 0
    fi

    if command -v apt-get >/dev/null 2>&1; then
        sudo_if_needed apt-get update
        for pkg in "$@"; do
            case "$pkg" in
                python) step "apt-get install python3 python3-venv python3-pip"
                        sudo_if_needed apt-get install -y python3 python3-venv python3-pip ;;
                git) step "apt-get install git"; sudo_if_needed apt-get install -y git ;;
            esac
        done
    elif command -v dnf >/dev/null 2>&1; then
        for pkg in "$@"; do
            case "$pkg" in
                python) sudo_if_needed dnf install -y python3 python3-pip ;;
                git) sudo_if_needed dnf install -y git ;;
            esac
        done
    elif command -v yum >/dev/null 2>&1; then
        for pkg in "$@"; do
            case "$pkg" in
                python) sudo_if_needed yum install -y python3 python3-pip ;;
                git) sudo_if_needed yum install -y git ;;
            esac
        done
    elif command -v zypper >/dev/null 2>&1; then
        for pkg in "$@"; do
            case "$pkg" in
                python) sudo_if_needed zypper install -y python3 python3-pip ;;
                git) sudo_if_needed zypper install -y git ;;
            esac
        done
    elif command -v pacman >/dev/null 2>&1; then
        for pkg in "$@"; do
            case "$pkg" in
                python) sudo_if_needed pacman -Sy --noconfirm python python-pip ;;
                git) sudo_if_needed pacman -Sy --noconfirm git ;;
            esac
        done
    elif command -v apk >/dev/null 2>&1; then
        for pkg in "$@"; do
            case "$pkg" in
                python) sudo_if_needed apk add --no-cache python3 py3-pip ;;
                git) sudo_if_needed apk add --no-cache git ;;
            esac
        done
    else
        echo "対応するパッケージマネージャが見つかりません: $*" >&2
        return 1
    fi
}

if [ "$SKIP_PREREQ" -eq 0 ]; then
    command -v git >/dev/null 2>&1 || install_packages git
    resolve_python >/dev/null 2>&1 || install_packages python
fi

if ! PYTHON_BIN="$(resolve_python)"; then
    echo "Python ${MIN_MAJOR}.${MIN_MINOR}+ を解決できませんでした。" >&2
    exit 3
fi

NEEDS_VENV=1
for arg in ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}; do
    [ "$arg" = "--no-venv" ] && NEEDS_VENV=0
done

if [ "$NEEDS_VENV" -eq 1 ] && ! venv_supported "$PYTHON_BIN"; then
    if [ "$SKIP_PREREQ" -eq 0 ]; then
        step "python3 に ensurepip が無いため venv 用パッケージを導入します"
        install_packages python
    fi
    if ! venv_supported "$PYTHON_BIN"; then
        echo "${PYTHON_BIN} で 'python3 -m venv' を実行できません（ensurepip が不在）。" >&2
        echo "Debian / Ubuntu では python3-venv を導入してください。" >&2
        echo "依存を入れずに進めるなら --no-venv を付けて再実行してください。" >&2
        exit 3
    fi
fi

step "python: ${PYTHON_BIN}"
step "git   : $(command -v git || echo 'not found')"

exec "$PYTHON_BIN" "${SCRIPT_DIR}/install.py" \
    --kit-dir "$SCRIPT_DIR" \
    --repo-root "$REPO_ROOT" \
    --python "$PYTHON_BIN" \
    ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
