#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command -v node >/dev/null || { echo 'Install Node.js before setting up YouTube Music.' >&2; exit 1; }
python3 -m venv "$root/runtime/youtube"
"$root/runtime/youtube/bin/python" -m pip install -r "$root/helper/requirements.txt"

# Optional: PO-token provider for signed-in playback. YouTube now serves
# SABR-only formats to anonymous/basic clients, so a signed-in account needs a
# PO token (built here) and Deno to generate it and solve the JS challenge.
pot_version="2.0.0"
if command -v git >/dev/null && command -v npm >/dev/null; then
  if [ ! -f "$root/runtime/bgutil/server/build/generate_once.js" ]; then
    rm -rf "$root/runtime/bgutil"
    if git clone --depth 1 --branch "$pot_version" \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
        "$root/runtime/bgutil" \
      && ( cd "$root/runtime/bgutil/server" && npm install && npx tsc ); then
      echo 'PO-token provider built for signed-in playback.'
    else
      echo 'Warning: could not build the PO-token provider; signed-in playback may fail.' >&2
    fi
  fi
  command -v deno >/dev/null \
    || echo 'Note: install Deno (https://deno.com) so signed-in playback can generate PO tokens.' >&2
else
  echo 'Note: git and npm are required to build the PO-token provider for signed-in playback.' >&2
fi

echo 'YouTube Music is ready. Browsing works with no Google account;'
echo 'sign in from the Account tab to reach your own library and likes.'
