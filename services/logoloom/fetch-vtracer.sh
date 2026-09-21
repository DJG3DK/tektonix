#!/usr/bin/env bash
# Fetch the vectorizer that logo_trace_image needs.
#
# Separate from `npm install` and not run by install.sh, because it is a
# 3MB binary for one tool of five, from a different project than the rest of
# this directory. Without it the other four logo tools work and that one says
# what is missing.
#
# It lands in vendor/ next to bridge.mjs rather than on the system PATH: it is
# a dependency of one service, and putting it in /usr/local/bin would make
# removing it somebody's archaeology later. bridge.mjs prepends that directory
# to PATH for its own children and nothing else.
set -euo pipefail

VERSION="1.0.0-alpha.4"
SHA256="2058f611b48ed49497f78883bde47435531e9e17e3a79428432d1528bdb12e2a"
URL="https://github.com/visioncortex/vtracer/releases/download/${VERSION}/vtracer-x86_64-unknown-linux-musl.tar.gz"

cd "$(dirname "$0")"
mkdir -p vendor

if [ -x vendor/vtracer ]; then
    echo "vtracer already here: $(vendor/vtracer --version)"
    exit 0
fi

case "$(uname -s)/$(uname -m)" in
    Linux/x86_64) ;;
    *)  echo "This script fetches the x86_64 Linux build; you are on $(uname -s)/$(uname -m)." >&2
        echo "Install vtracer yourself (cargo install vtracer) and put it on PATH." >&2
        exit 1 ;;
esac

echo "fetching vtracer ${VERSION}…"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -sSL --fail -o "$tmp/v.tgz" "$URL"

# Checked, not trusted: this is a binary that will run on your machine, and a
# release asset can be replaced after the fact.
actual="$(sha256sum "$tmp/v.tgz" | cut -d' ' -f1)"
if [ "$actual" != "$SHA256" ]; then
    echo "checksum mismatch — expected $SHA256, got $actual. Nothing installed." >&2
    exit 1
fi

tar xzf "$tmp/v.tgz" -C vendor vtracer
chmod +x vendor/vtracer
echo "installed: $(vendor/vtracer --version)"
