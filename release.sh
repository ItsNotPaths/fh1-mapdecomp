#!/usr/bin/env bash
# Build the fh1-mapdecomp executable.
#
#   ./release.sh --local
#       Compiles src/lzxd_helper.c and bundles src/fh1_mapdecomp into a
#       single-file PyInstaller exe. Output (exe + default conf) lands in
#       ../fh1-mapdecomp-release/.
#
#   ./release.sh --public --version vX.Y.Z [--notes "text"]
#       Triggers .github/workflows/release.yml via the gh CLI.
#
# Prereqs for --local:
#   ./download-deps.sh     # populates vendor/libmspack
#   pip install pyinstaller
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
RELEASE_DIR="$(cd "$PROJECT_DIR/.." && pwd)/${PROJECT_NAME}-release"
SRC="$PROJECT_DIR/src"
VENDOR="$PROJECT_DIR/vendor"
MSPACK="$VENDOR/libmspack/libmspack/mspack"

usage() {
    cat <<EOF
usage: $(basename "$0") [--local] [--public --version vX.Y.Z [--notes "text"]]

  --local               build locally into <project>-release/ next to the project
  --public              trigger release.yml workflow via gh CLI
  --version <tag>       required when --public is used
  --notes <text>        optional release notes
EOF
}

DO_LOCAL=0
DO_PUBLIC=0
VERSION=""
NOTES=""

while [ $# -gt 0 ]; do
    case "$1" in
        --local)   DO_LOCAL=1; shift ;;
        --public)  DO_PUBLIC=1; shift ;;
        --version) VERSION="${2:-}"; shift 2 ;;
        --notes)   NOTES="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown flag: $1" >&2; usage; exit 1 ;;
    esac
done

if [ $DO_LOCAL -eq 0 ] && [ $DO_PUBLIC -eq 0 ]; then
    usage
    exit 1
fi

build_local() {
    echo "==> Local build: $PROJECT_NAME -> $RELEASE_DIR"

    if [ ! -d "$MSPACK" ]; then
        echo "error: $MSPACK missing — run ./download-deps.sh first" >&2
        exit 1
    fi
    if ! command -v pyinstaller >/dev/null 2>&1; then
        echo "error: pyinstaller not on PATH (pip install pyinstaller)" >&2
        exit 2
    fi

    mkdir -p "$RELEASE_DIR"

    local work
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' RETURN

    local cc="${CC:-cc}"
    local cflags="${CFLAGS:--O2 -Wall}"

    echo "[release] compiling lzxd_helper"
    $cc $cflags -I"$MSPACK" \
        -o "$work/lzxd_helper" \
        "$SRC/lzxd_helper.c" \
        "$MSPACK/lzxd.c" \
        "$MSPACK/system.c"

    echo "[release] running pyinstaller"
    pyinstaller \
        --clean --noconfirm \
        --onefile \
        --name fh1-mapdecomp \
        --paths "$SRC" \
        --add-binary "$work/lzxd_helper:." \
        --add-data "$SRC/fh1_mapdecomp/blender_scripts:fh1_mapdecomp/blender_scripts" \
        --collect-submodules fh1_mapdecomp \
        --collect-submodules fh1_mapdecomp.pgeo_body \
        --hidden-import fh1_mapdecomp.pgeo_body.terrain \
        --distpath "$RELEASE_DIR" \
        --workpath "$work/pyi-work" \
        --specpath "$work/pyi-spec" \
        "$SRC/fh1_mapdecomp/__main__.py"

    # Prefer the gitignored .conf.use (local debug overrides) over the
    # shipped .conf.example. CI never has .conf.use so it falls back to
    # the example automatically.
    if [ -f "$SRC/fh1-mapdecomp.conf.use" ]; then
        echo "[release] using src/fh1-mapdecomp.conf.use"
        cp "$SRC/fh1-mapdecomp.conf.use" "$RELEASE_DIR/fh1-mapdecomp.conf"
    else
        echo "[release] using src/fh1-mapdecomp.conf.example"
        cp "$SRC/fh1-mapdecomp.conf.example" "$RELEASE_DIR/fh1-mapdecomp.conf"
    fi
    [ -f "$PROJECT_DIR/README.md" ] && cp "$PROJECT_DIR/README.md" "$RELEASE_DIR/" || true
    [ -f "$PROJECT_DIR/LICENSE" ]   && cp "$PROJECT_DIR/LICENSE"   "$RELEASE_DIR/" || true

    echo "==> Local done: $RELEASE_DIR"
    ls -la "$RELEASE_DIR"
}

trigger_public() {
    if [ -z "$VERSION" ]; then
        echo "error: --public requires --version <tag>" >&2
        exit 1
    fi
    if ! command -v gh >/dev/null 2>&1; then
        echo "error: gh CLI not found; install it and run 'gh auth login'" >&2
        exit 1
    fi
    local repo
    repo=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || true)
    if [ -z "$repo" ]; then
        echo "error: not in a github repo (or gh not authenticated)" >&2
        exit 1
    fi
    local workflow="release.yml"
    echo "==> Triggering $workflow on $repo ($VERSION)"
    local old_id
    old_id=$(gh run list --workflow="$workflow" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || echo "")
    gh workflow run "$workflow" \
        --field version="$VERSION" \
        --field notes="$NOTES"
    echo "==> Waiting for run to register..."
    local new_id=""
    for _ in $(seq 1 30); do
        sleep 2
        local cur_id
        cur_id=$(gh run list --workflow="$workflow" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || echo "")
        if [ -n "$cur_id" ] && [ "$cur_id" != "$old_id" ]; then
            new_id="$cur_id"
            break
        fi
    done
    if [ -z "$new_id" ]; then
        echo "error: failed to detect new workflow run" >&2
        exit 1
    fi
    echo "==> Watching run $new_id"
    gh run watch "$new_id" --exit-status
}

[ $DO_LOCAL  -eq 1 ] && build_local
[ $DO_PUBLIC -eq 1 ] && trigger_public
