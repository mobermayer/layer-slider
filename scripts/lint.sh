#!/bin/bash

# Run the QGIS plugin repository's Python quality/security tools locally, plus
# flake8-qgis rules and a local .ui enum syntax check:
#   - flake8 (+ flake8-qgis) for code quality and QGIS-specific rules
#   - bandit                 for security issues, including low-severity findings
#   - detect-secrets         for leaked credentials / secrets
#   - ui-enums               for Qt5-compatible enum syntax in .ui files (QGIS 3)
#   - qgis4-enums            for known legacy QGIS enum syntax flagged by QGIS 4 checks
#
# All tools are installed into a local virtualenv (.venv-lint) on first run
# and reused afterwards, so no system-wide Python packages are required.
#
# Usage:
#   scripts/lint.sh                # run all checks
#   scripts/lint.sh flake8 [args]  # run only one tool, forwarding extra args
#   scripts/lint.sh bandit [args]
#   scripts/lint.sh secrets [args]
#   scripts/lint.sh ui-enums       # Qt5-compatible enum syntax in .ui files
#   scripts/lint.sh qgis4-enums    # targeted QGIS 4 enum compatibility check

set -euo pipefail

BASEDIR=$(realpath "$(dirname "$0")/..")
VENV_DIR="${BASEDIR}/.venv-lint"
STAMP="${VENV_DIR}/.deps-stamp"

FLAKE8_VERSION="7.3.0"
FLAKE8_QGIS_VERSION="2.0.1"
BANDIT_VERSION="1.9.4"
DETECT_SECRETS_VERSION="1.5.0"
STAMP_CONTENT="flake8==${FLAKE8_VERSION} flake8-qgis==${FLAKE8_QGIS_VERSION} bandit==${BANDIT_VERSION} detect-secrets==${DETECT_SECRETS_VERSION}"

TARGETS=(__init__.py src)

if [ ! -x "${VENV_DIR}/bin/flake8" ]; then
    echo "Creating lint virtualenv at ${VENV_DIR}"
    if ! python3 -m venv "${VENV_DIR}" 2>/tmp/venv-err; then
        cat /tmp/venv-err >&2
        echo >&2
        echo "Failed to create virtualenv. On Debian/Ubuntu you may need:" >&2
        echo "    sudo apt install python3-venv" >&2
        rm -f /tmp/venv-err
        exit 1
    fi
    rm -f /tmp/venv-err
fi

# Only (re)install if dependency pins changed.
if [ ! -f "${STAMP}" ] || [ "$(cat "${STAMP}")" != "${STAMP_CONTENT}" ]; then
    echo "Installing lint dependencies into ${VENV_DIR}"
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet \
        "flake8==${FLAKE8_VERSION}" \
        "flake8-qgis==${FLAKE8_QGIS_VERSION}" \
        "bandit==${BANDIT_VERSION}" \
        "detect-secrets==${DETECT_SECRETS_VERSION}"
    echo "${STAMP_CONTENT}" > "${STAMP}"
fi

cd "${BASEDIR}"

run_flake8() {
    echo "=== flake8 ==="
    # --show-source prints the offending line + caret under each finding by
    # default, making it easier to see the issue in context.
    "${VENV_DIR}/bin/flake8" --jobs 1 --max-line-length=120 --show-source "$@" "${TARGETS[@]}"
}

run_bandit() {
    echo "=== bandit ==="
    # The QGIS scanner runs Bandit without a severity threshold. Keep low
    # findings such as B110 visible because the repository treats the Bandit
    # check as blocking when it reports any finding.
    "${VENV_DIR}/bin/bandit" --quiet --recursive "$@" "${TARGETS[@]}"
}

run_secrets() {
    echo "=== detect-secrets ==="
    # Use detect-secrets-hook (the pre-commit entry point): it prints nothing
    # when clean, a human-readable report on findings, and exits non-zero on
    # any hit - unlike `detect-secrets scan`, which always dumps full JSON.
    local status=0
    # shellcheck disable=SC2046
    "${VENV_DIR}/bin/detect-secrets-hook" "$@" $(git ls-files -- "${TARGETS[@]}" 2>/dev/null || find "${TARGETS[@]}" -type f) || status=$?
    return "${status}"
}

# Qt 6 Designer writes Class::EnumType::Value in .ui files. PyQt5 uic (QGIS 3)
# emits invalid Python from that syntax. Flag any enum/set value with two "::"
# segments before the member name (e.g. Qt::Orientation::Horizontal).
run_ui_enums() {
    echo "=== ui-enums ==="
    local status=0
    local ui_file
    local matches

    while IFS= read -r -d '' ui_file; do
        matches=$(grep -nE '::[A-Za-z_][A-Za-z0-9_]*::' "${ui_file}" || true)
        if [ -n "${matches}" ]; then
            echo "${ui_file}: Qt6-style enum syntax breaks QGIS 3 / PyQt5 uic" >&2
            echo "${matches}" >&2
            echo "  Use short Qt5 form, e.g. Qt::Horizontal not Qt::Orientation::Horizontal" >&2
            status=1
        fi
    done < <(find src -name '*.ui' -print0 | sort -z)

    # if [ "${status}" -eq 0 ]; then
    #     echo "OK: no Qt6-only .ui enum syntax found"
    # fi
    return "${status}"
}

run_qgis4_enums() {
    echo "=== qgis4-enums ==="
    local matches
    matches=$(grep -RnE --include='*.py' 'QgsRasterBandStats\.All([^[:alnum:]_]|$)' src || true)
    if [ -n "${matches}" ]; then
        echo "Legacy QgsRasterBandStats.All enum syntax is incompatible with QGIS 4:" >&2
        echo "${matches}" >&2
        echo "Use the scoped Stats enum, with a fallback for older QGIS 3 releases." >&2
        return 1
    fi
}

if [ $# -eq 0 ]; then
    status=0
    run_flake8 || status=$?
    run_bandit || status=$?
    run_secrets || status=$?
    run_ui_enums || status=$?
    run_qgis4_enums || status=$?
    exit "${status}"
fi

case "$1" in
    flake8)   shift; run_flake8 "$@" ;;
    bandit)   shift; run_bandit "$@" ;;
    secrets)  shift; run_secrets "$@" ;;
    ui-enums) shift; run_ui_enums "$@" ;;
    qgis4-enums) shift; run_qgis4_enums "$@" ;;
    *)
        echo "Unknown subcommand: $1" >&2
        echo "Usage: $0 [flake8|bandit|secrets|ui-enums|qgis4-enums] [extra args...]" >&2
        exit 2
        ;;
esac
