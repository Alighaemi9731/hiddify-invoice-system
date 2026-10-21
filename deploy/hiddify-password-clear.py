#!/usr/bin/env python3
"""Install an owner-only Hiddify v12 admin-password-clear form extension.

Run as root on a Hiddify host; --check is read-only. Unknown source layouts fail
closed. Keep this script in an ExecStartPre hook to survive panel package updates.
"""
from __future__ import annotations

import argparse
import ast
import glob
import os
from pathlib import Path
import shutil

MARKER = "# invoice-system: explicit password clear v1"
ANCHOR = "        if model.new_password:"
BLOCK = '''        # invoice-system: explicit password clear v1
        if request.form.get("invoice_clear_password") == "1":
            if g.account.mode != AdminMode.super_admin or model.mode == AdminMode.super_admin:
                raise ValidationError("Only the owner can clear a non-owner admin password")
            model.password = ""
            model.new_password = ""
'''


def patched_source(source: str) -> str:
    if MARKER in source:
        if BLOCK not in source:
            raise ValueError("Existing extension differs; inspect before upgrading")
        return source
    if source.count(ANCHOR) != 1 or "model.password=model.new_password" not in source.replace(" ", ""):
        raise ValueError("Unrecognized Hiddify admin form; refusing to patch")
    result = source.replace(ANCHOR, BLOCK + ANCHOR, 1)
    ast.parse(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    paths = [args.path] if args.path else [Path(p) for p in glob.glob(
        "/opt/hiddify-manager/.venv*/lib/python*/site-packages/hiddifypanel/panel/admin/AdminstratorAdmin.py"
    )]
    if not paths:
        raise SystemExit("Hiddify admin form not found")
    for path in paths:
        source = path.read_text()
        result = patched_source(source)
        if not args.check and result != source:
            backup = path.with_suffix(".py.invoice-original")
            if not backup.exists():
                shutil.copy2(path, backup)
            stat = path.stat()
            temp = path.with_suffix(".py.invoice-new")
            temp.write_text(result)
            os.chmod(temp, stat.st_mode)
            os.chown(temp, stat.st_uid, stat.st_gid)
            temp.replace(path)
        print(f"{'compatible' if args.check else 'installed'}: {path}")


if __name__ == "__main__":
    main()
