#!/usr/bin/env bash
# Independent, verified release bootstrap for Hiddify panel hosts.
set -euo pipefail
umask 077
REPO_SLUG="${REPO_SLUG:-Alighaemi9731/hiddify-invoice-system}"
RELEASE_TAG="${RELEASE_TAG:-latest}"
if [[ $EUID -ne 0 && "${ALLOW_NON_ROOT:-0}" != 1 ]]; then
  echo 'Run as root (sudo); this installer belongs on a Hiddify panel host.' >&2
  exit 1
fi
for command in python3 sha256sum; do command -v "$command" >/dev/null || { echo "Missing required command: $command" >&2; exit 1; }; done
if [[ "$RELEASE_TAG" == latest ]]; then
  effective="$(curl -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$REPO_SLUG/releases/latest")"
  RELEASE_TAG="${effective##*/}"
fi
[[ "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'Invalid release tag' >&2; exit 1; }
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
asset="invoice-system-$RELEASE_TAG.tar.gz"
if [[ -n "${RELEASE_ASSET_DIR:-}" ]]; then
  cp "$RELEASE_ASSET_DIR/$asset" "$RELEASE_ASSET_DIR/$asset.sha256" "$work/"
else
  base="https://github.com/$REPO_SLUG/releases/download/$RELEASE_TAG"
  curl -fL --retry 3 -o "$work/$asset" "$base/$asset"
  curl -fL --retry 3 -o "$work/$asset.sha256" "$base/$asset.sha256"
fi
# Accept exactly the expected digest/filename; a checksum file cannot select other host files.
python3 - "$work/$asset.sha256" "$asset" <<'PY'
from pathlib import Path
import re,sys
text=Path(sys.argv[1]).read_text().strip()
if not re.fullmatch(r'[0-9a-fA-F]{64} [ *]'+re.escape(sys.argv[2]),text):
 raise SystemExit('Invalid release checksum manifest')
PY
(cd "$work" && sha256sum -c "$asset.sha256")
python3 - "$work/$asset" "$work/payload" "$RELEASE_TAG" <<'PY'
from pathlib import Path,PurePosixPath
import sys,tarfile
archive,destination,tag=sys.argv[1:]
root='invoice-system-'+tag
wanted={'deploy/panel_addon.py','deploy/hiddify-password-clear.py','deploy/hiddify-invoice-compat.py','VERSION'}
dest=Path(destination);dest.mkdir(mode=0o700)
with tarfile.open(archive,'r:gz') as tar:
 for member in tar.getmembers():
  path=PurePosixPath(member.name)
  if member.name.rstrip('/')==root and member.isdir():continue
  if path.is_absolute() or '..' in path.parts or not member.name.startswith(root+'/'):
   raise SystemExit('Unsafe release archive path')
 for name in wanted:
  matches=[m for m in tar.getmembers() if m.name==root+'/'+name]
  if len(matches)!=1 or not matches[0].isfile() or matches[0].size>1_000_000:
   raise SystemExit('Missing, duplicate or unsafe addon payload')
  source=tar.extractfile(matches[0]);assert source is not None
  (dest/Path(name).name).write_bytes(source.read())
if (dest/'VERSION').read_text().strip()!=tag[1:]:raise SystemExit('Release version mismatch')
PY
python3 "$work/payload/panel_addon.py" "$@"
