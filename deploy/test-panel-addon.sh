#!/usr/bin/env bash
# Verify extraction, argument forwarding, and rejection before payload execution.
set -euo pipefail
export COPYFILE_DISABLE=1
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/assets" "$work/src/invoice-system-v1.0.0/deploy"
root="$work/src/invoice-system-v1.0.0"
printf '1.0.0\n' > "$root/VERSION"
cat > "$root/deploy/panel_addon.py" <<'PY'
import os,sys
from pathlib import Path
assert sys.argv[1:]==['--check']
Path(os.environ['ADDON_SENTINEL']).write_text('verified')
PY
cp "$repo/deploy/hiddify-password-clear.py" "$repo/deploy/hiddify-invoice-compat.py" "$root/deploy/"
tar -czf "$work/assets/invoice-system-v1.0.0.tar.gz" -C "$work/src" invoice-system-v1.0.0
(cd "$work/assets" && sha256sum invoice-system-v1.0.0.tar.gz > invoice-system-v1.0.0.tar.gz.sha256)
ALLOW_NON_ROOT=1 RELEASE_TAG=v1.0.0 RELEASE_ASSET_DIR="$work/assets" ADDON_SENTINEL="$work/executed" \
  bash "$repo/deploy/panel-addon-installer.sh" --check
[[ "$(cat "$work/executed")" == verified ]]
rm "$work/executed"
printf 'corruption\n' >> "$work/assets/invoice-system-v1.0.0.tar.gz"
if ALLOW_NON_ROOT=1 RELEASE_TAG=v1.0.0 RELEASE_ASSET_DIR="$work/assets" ADDON_SENTINEL="$work/executed" \
  bash "$repo/deploy/panel-addon-installer.sh" --check > "$work/corrupt.log" 2>&1; then
  echo 'Corrupt addon archive was accepted' >&2; exit 1
fi
[[ ! -e "$work/executed" ]]
# A valid checksum does not make unsafe or inconsistent payloads executable.
for scenario in symlink traversal duplicate version manifest; do
  python3 - "$work/assets" "$root" "$scenario" <<'PYCASE'
import hashlib,io,sys,tarfile
from pathlib import Path
assets,source,scenario=sys.argv[1:]
p=Path(assets)/'invoice-system-v1.0.0.tar.gz'
root='invoice-system-v1.0.0/'
with tarfile.open(p,'w:gz') as tar:
 for name in ('VERSION','deploy/panel_addon.py','deploy/hiddify-password-clear.py','deploy/hiddify-invoice-compat.py'):
  data=(Path(source)/name).read_bytes()
  if scenario=='version' and name=='VERSION':data=b'9.0.0\n'
  member=tarfile.TarInfo(root+name);member.size=len(data)
  if scenario=='symlink' and name=='deploy/panel_addon.py':
   member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd';member.size=0;tar.addfile(member)
  else:tar.addfile(member,io.BytesIO(data))
  if scenario=='duplicate' and name=='deploy/panel_addon.py':tar.addfile(member,io.BytesIO(data))
 if scenario=='traversal':
  member=tarfile.TarInfo(root+'../../outside');member.size=0;tar.addfile(member,io.BytesIO())
filename=p.name if scenario!='manifest' else '../../unexpected'
p.with_suffix(p.suffix+'.sha256').write_text(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+filename+'\n')
PYCASE
  if ALLOW_NON_ROOT=1 RELEASE_TAG=v1.0.0 RELEASE_ASSET_DIR="$work/assets" ADDON_SENTINEL="$work/executed" \
    bash "$repo/deploy/panel-addon-installer.sh" --check > "$work/$scenario.log" 2>&1; then
    echo "Unsafe addon payload accepted: $scenario" >&2; exit 1
  fi
  [[ ! -e "$work/executed" ]]
done
echo 'panel addon verified bootstrap OK'
