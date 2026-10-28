#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PLUGIN_ROOT="Plugin/ChannelHub"
ZIP="dist/ChannelHub-v0.4.1.zip"
if [[ ! -f "$PLUGIN_ROOT/plugin-manifest.json" ]]; then
  echo "Missing ChannelHub manifest" >&2
  exit 1
fi
if find "$PLUGIN_ROOT" -type l | grep -q .; then
  echo "Refusing to package symbolic links" >&2
  exit 1
fi
if find "$PLUGIN_ROOT" -type f \( -name config.env -o -name .env -o -name '*.db' -o -name '*.sqlite' -o -name '*.log' -o -name '*.pem' -o -name '*.key' \) | grep -q .; then
  echo "Refusing to package credentials, databases, logs or key material" >&2
  exit 1
fi
mkdir -p dist
rm -f "$ZIP" "$ZIP.sha256"
(
  cd Plugin
  zip -q -r -X "../$ZIP" ChannelHub \
    -x 'ChannelHub/config.env' 'ChannelHub/state/*' \
       'ChannelHub/logs/*' 'ChannelHub/cache/*' 'ChannelHub/node_modules/*'
)
unzip -Z -1 "$ZIP" | grep -Fxq 'ChannelHub/plugin-manifest.json'
if unzip -Z -1 "$ZIP" | grep -E '(^|/)(config\.env|\.env)$|/(state|logs|cache|node_modules)/' ; then
  echo "Package contains disallowed runtime files" >&2
  exit 1
fi
sha256sum "$ZIP" > "$ZIP.sha256"
cat "$ZIP.sha256"
