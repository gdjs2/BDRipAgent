import os

import pytest

from scripts.storage_permissions import migrate


@pytest.mark.skipif(os.geteuid() != 0, reason="Ownership migration requires root")
def test_migration_changes_generated_owner_without_following_links_or_loosening_modes(tmp_path):
    root = tmp_path / "generated"
    root.mkdir()
    secret = root / "auth.json"
    secret.write_text("private fixture")
    secret.chmod(0o600)
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    (root / "link").symlink_to(source)
    assert migrate(root, 1004, 1005) == 3
    assert migrate(root, 1004, 1005) == 0
    assert (secret.stat().st_uid, secret.stat().st_gid) == (1004, 1005)
    assert secret.stat().st_mode & 0o777 == 0o600
    assert (root / "link").lstat().st_uid == 1004
    assert source.stat().st_uid == 0 and source.read_bytes() == b"source"
