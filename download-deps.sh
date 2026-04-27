#!/usr/bin/env bash
# Fetches third-party deps into vendor/. Run once before building.
set -euo pipefail

VENDOR="$(cd "$(dirname "$0")" && pwd)/vendor"

fetch() {
    local name="$1"
    local url="$2"
    local dest="$3"
    local strip="${4:-1}"
    local filter="${5:-}"

    if [ -d "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
        echo "  already present: $(basename "$dest")"
        return
    fi

    echo "  downloading $name..."
    mkdir -p "$dest"
    if [ -n "$filter" ]; then
        curl -fsSL "$url" | tar xz --strip-components="$strip" -C "$dest" --wildcards "$filter"
    else
        curl -fsSL "$url" | tar xz --strip-components="$strip" -C "$dest"
    fi
    echo "  done."
}

echo "==> libmspack"
if [ -d "$VENDOR/libmspack" ] && [ -n "$(ls -A "$VENDOR/libmspack" 2>/dev/null)" ]; then
    echo "  already present: libmspack"
else
    echo "  cloning libmspack..."
    git clone --depth=1 "https://github.com/kyz/libmspack.git" "$VENDOR/libmspack"
    echo "  done."
fi

echo "==> xex2-unpacker"
if [ -d "$VENDOR/xex2-unpacker" ] && [ -n "$(ls -A "$VENDOR/xex2-unpacker" 2>/dev/null)" ]; then
    echo "  already present: xex2-unpacker"
else
    echo "  cloning xex2-unpacker..."
    git clone --depth=1 "https://github.com/ItsNotPaths/xex2-unpacker.git" "$VENDOR/xex2-unpacker"
    echo "  done."
fi

echo ""
echo "All deps ready."
