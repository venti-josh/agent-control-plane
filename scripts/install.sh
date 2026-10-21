#!/bin/sh
# Install a released ACP wheel. This entire function is parsed before piped execution.
acp_install_main() (
    set -eu
    umask 077
    case "$(uname -s)" in Darwin|Linux) ;; *) echo 'ACP supports macOS and Linux.' >&2; exit 1 ;; esac
    command -v curl >/dev/null 2>&1 || { echo 'curl is required.' >&2; exit 1; }
    acp_tmp=$(mktemp -d "${TMPDIR:-/tmp}/acp-install.XXXXXX")
    trap 'rm -rf "$acp_tmp"' EXIT
    trap 'exit 1' HUP INT TERM
    acp_repo='https://github.com/venti-josh/agent-control-plane'
    # The release builder pins this value in the published installer.
    acp_default_version=''
    acp_version=${ACP_VERSION:-$acp_default_version}
    acp_base=${ACP_RELEASE_BASE_URL:-$acp_repo/releases/latest/download}
    if [ -z "$acp_version" ]; then
        curl -fLsS --retry 3 "$acp_base/VERSION" -o "$acp_tmp/VERSION"
        acp_version=$(cat "$acp_tmp/VERSION")
    fi
    if ! printf '%s\n' "$acp_version" | awk 'NR == 1 && /^[0-9]+\.[0-9]+\.[0-9]+$/ {ok=1} END {exit !(NR == 1 && ok)}'; then
        echo 'Invalid ACP release version; expected X.Y.Z.' >&2; exit 1
    fi
    if [ -z "${ACP_RELEASE_BASE_URL:-}" ]; then
        acp_base="$acp_repo/releases/download/v$acp_version"
    fi
    acp_wheel="acp-$acp_version-py3-none-any.whl"
    for acp_asset in "$acp_wheel" constraints.txt SHA256SUMS; do
        curl -fLsS --retry 3 "$acp_base/$acp_asset" -o "$acp_tmp/$acp_asset"
    done
    for acp_asset in "$acp_wheel" constraints.txt; do
        acp_expected=$(awk -v name="$acp_asset" '$2 == name {print $1}' "$acp_tmp/SHA256SUMS")
        if command -v sha256sum >/dev/null 2>&1; then
            acp_actual=$(sha256sum "$acp_tmp/$acp_asset" | awk '{print $1}')
        else
            acp_actual=$(shasum -a 256 "$acp_tmp/$acp_asset" | awk '{print $1}')
        fi
        if [ "${#acp_expected}" -ne 64 ] || [ "$acp_expected" != "$acp_actual" ]; then
            echo "Release checksum mismatch: $acp_asset" >&2; exit 1
        fi
    done
    acp_uv=$(command -v uv || true)
    if [ -z "$acp_uv" ] || ! "$acp_uv" --version | awk '{split($2,v,"."); exit !(v[1]>0 || v[2]>13 || (v[2]==13 && v[3]>=1))}'; then
        acp_uv_dir="${ACP_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/acp}/installer/uv"
        mkdir -p "$acp_uv_dir"
        acp_uv_dir=$(cd "$acp_uv_dir" && pwd)
        curl -fLsS --retry 3 https://astral.sh/uv/0.13.1/install.sh -o "$acp_tmp/uv-install.sh"
        UV_UNMANAGED_INSTALL="$acp_uv_dir" sh "$acp_tmp/uv-install.sh"
        acp_uv="$acp_uv_dir/uv"
    fi
    acp_bin_dir=$("$acp_uv" tool dir --bin)
    if [ -x "$acp_bin_dir/acp" ]; then
        if ! acp_status=$("$acp_bin_dir/acp" status); then
            echo 'Cannot check the existing ACP service; resolve the error before upgrading.' >&2; exit 1
        fi
        # The top-level key only: the nested comet object has its own "running".
        if printf '%s\n' "$acp_status" | grep -q '^  "running": true'; then
            echo 'Run acp stop before reinstalling or upgrading.' >&2; exit 1
        fi
    fi
    "$acp_uv" tool install --python 3.14 --reinstall --constraints "$acp_tmp/constraints.txt" "$acp_tmp/$acp_wheel"
    # Setup detects the claude and codex CLIs and installs their activity hooks; its JSON says what it found.
    acp_setup=$("$acp_bin_dir/acp" setup)
    printf '%s\n' "$acp_setup"
    # Comet, the macOS overlay, is compiled now so `acp start` can open it at once. It needs the
    # Apple command line tools; without them (or with ACP_OVERLAY=0) the install still succeeds.
    if [ "$(uname -s)" = Darwin ] && [ "${ACP_OVERLAY:-1}" != 0 ]; then
        if acp_comet=$("$acp_bin_dir/acp" overlay build 2>"$acp_tmp/comet.log" </dev/null); then
            # The result is json.dumps(indent=2): one key per line, and the skip reason has no quotes.
            acp_comet_skipped=$(printf '%s\n' "$acp_comet" | sed -n 's/^ *"skipped": "\(.*\)",*$/\1/p')
            if [ -n "$acp_comet_skipped" ]; then printf 'Comet: %s\n' "$acp_comet_skipped"
            else printf 'Comet is built; it opens with acp start (ACP_OVERLAY=0 opts out).\n'; fi
        else
            printf 'Comet could not be built: %s\nRun acp overlay build to see the compiler output.\n' "$(head -n 1 "$acp_tmp/comet.log")"
        fi
    fi
    printf '\nACP %s installed.\nRun: %s/acp start\nThen: %s/acp open\n' "$acp_version" "$acp_bin_dir" "$acp_bin_dir"
    case "$acp_setup" in *'"trust_required": true'*)
        printf 'Codex hooks were installed or changed: open /hooks in the Codex CLI, review and trust the ACP handlers, then start a new turn.\n' ;;
    esac
    printf 'To run tasks in the Codex app, open Settings → Codex app and copy the setup prompt.\n'
    case ":$PATH:" in *":$acp_bin_dir:"*) ;; *) printf 'Add %s to PATH to use the short acp command.\n' "$acp_bin_dir" ;; esac
)
acp_install_main "$@"
