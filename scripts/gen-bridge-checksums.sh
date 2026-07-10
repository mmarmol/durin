#!/usr/bin/env bash
# Regenerate the committed WhatsApp bridge checksum pin.
#
# Run this whenever BRIDGE_VERSION (durin/channels/whatsapp_bridge.py) is bumped,
# then commit the updated durin/channels/bridge_checksums.json in the same change.
# The bridge-release workflow rebuilds the binaries at tag time and fails if they
# do not match this file, so the pin must be produced from the exact source being
# released.
#
# The build is reproducible: a pinned Go toolchain plus -trimpath and
# -buildvcs=false strip the host paths and the embedded git revision, so a local
# cross-compile is byte-identical to the one CI produces for the same source.
# Requires the Go version in bridge/go.mod (currently 1.26.5) on PATH.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bridge_dir="$repo_root/bridge"
out="$repo_root/durin/channels/bridge_checksums.json"

want_go="$(sed -n 's/^go \([0-9.]*\).*/\1/p' "$bridge_dir/go.mod")"
have_go="$(go env GOVERSION | sed 's/^go//')"
if [ "$have_go" != "$want_go" ]; then
  echo "error: bridge/go.mod pins go $want_go but 'go' on PATH is $have_go." >&2
  echo "       Install go $want_go so the checksums match what CI builds." >&2
  exit 1
fi

version="$(sed -n 's/^BRIDGE_VERSION = "\(.*\)"/\1/p' \
  "$repo_root/durin/channels/whatsapp_bridge.py")"
echo "Building WhatsApp bridge $version for checksum pinning..."

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

for target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do
  goos="${target%/*}"; goarch="${target#*/}"
  asset="durin-whatsapp-bridge-${goos}-${goarch}"
  (cd "$bridge_dir" && GOOS="$goos" GOARCH="$goarch" CGO_ENABLED=0 \
    go build -trimpath -buildvcs=false \
      -ldflags "-s -w -X main.version=${version}" \
      -o "$workdir/$asset" .)
done

python3 - "$workdir" "$out" <<'PY'
import hashlib, json, pathlib, sys
workdir, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sums = {
    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
    for p in sorted(workdir.glob("durin-whatsapp-bridge-*"))
}
pathlib.Path(out).write_text(json.dumps({"sha256": sums}, indent=2) + "\n")
PY

echo "Wrote $out"
cat "$out"
