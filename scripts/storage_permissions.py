"""One-shot ownership migration for app-generated storage; never mount source videos here."""

import os
from pathlib import Path

ROOTS = ("/workspace", "/completed", "/artifacts", "/cache", "/auth")


def migrate(root, uid, gid):
    """Do not follow symlinks or change file modes (especially auth credentials)."""
    changed = 0
    for folder, directories, files in os.walk(root, followlinks=False):
        for path in [Path(folder), *(Path(folder) / name for name in directories + files)]:
            try:
                before = path.lstat()
                if (before.st_uid, before.st_gid) != (uid, gid):
                    os.chown(path, uid, gid, follow_symlinks=False)
                    changed += 1
            except FileNotFoundError:
                continue  # An active worker may atomically replace a generated file.
    return changed


def main():
    uid, gid = int(os.environ["APP_UID"]), int(os.environ["APP_GID"])
    if uid <= 0 or gid <= 0:
        raise SystemExit("Set APP_UID and APP_GID to your non-root host user and group IDs")
    for name in ROOTS:
        root = Path(name)
        root.mkdir(parents=True, exist_ok=True)
        print(f"{name}: updated {migrate(root, uid, gid)} entries to {uid}:{gid}", flush=True)


if __name__ == "__main__":
    main()
