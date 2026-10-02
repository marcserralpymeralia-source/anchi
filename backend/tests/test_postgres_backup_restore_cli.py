from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.backup import pg_restore  # noqa: E402


class PostgresBackupRestoreTests(unittest.TestCase):
    def _artifact(self, root: Path) -> Path:
        artifact = root / "master.dump"
        artifact.write_bytes(b"synthetic postgres archive")
        artifact.with_name(f"{artifact.name}.json").write_text(json.dumps({
            "engine": "postgresql",
            "database_identity": "pilot-source-master",
            "source_database_target": {"host": "source.example", "port": 5432, "database": "anchi_master"},
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }), encoding="utf-8")
        return artifact

    def test_restore_requires_verified_backup_and_distinct_destination(self):
        with tempfile.TemporaryDirectory() as tempdir:
            artifact = self._artifact(Path(tempdir))
            with self.assertRaisesRegex(ValueError, "identidad destino distinta"):
                pg_restore(artifact, "postgresql://pilot:secret@restore.example:5432/restore_db", target_identity="pilot-source-master")
            with self.assertRaisesRegex(ValueError, "misma base"):
                pg_restore(artifact, "postgresql://pilot:secret@source.example:5432/anchi_master", target_identity="restore-master")
            artifact.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum"):
                pg_restore(artifact, "postgresql://pilot:secret@restore.example:5432/restore_db", target_identity="restore-master")

    def test_restore_executes_single_transaction_and_validates_target_without_leaking_url(self):
        with tempfile.TemporaryDirectory() as tempdir:
            artifact = self._artifact(Path(tempdir))
            connection = Mock()
            connection.__enter__ = Mock(return_value=connection)
            connection.__exit__ = Mock(return_value=None)
            connection.execute.return_value.scalar.side_effect = [False, False, False, False, 1]
            engine = Mock()
            engine.connect.return_value = connection
            with patch("app.core.backup.subprocess.run") as run, patch("app.core.backup.create_engine", return_value=engine):
                result = pg_restore(artifact, "postgresql://pilot:secret@restore.example:5432/restore_db", target_identity="restore-master")
            args = run.call_args.args[0]
            self.assertIn("--single-transaction", args)
            self.assertNotIn("secret", str(args))
            self.assertTrue(run.call_args.kwargs["check"])
            self.assertEqual(connection.execute.call_count, 5)
            self.assertEqual(result["post_restore_validation"], "SELECT 1 passed")
            self.assertNotIn("secret", str(result))
            engine.dispose.assert_called_once()

    def test_restore_refuses_nonempty_destination_before_pg_restore(self):
        with tempfile.TemporaryDirectory() as tempdir:
            artifact = self._artifact(Path(tempdir))
            connection = Mock()
            connection.__enter__ = Mock(return_value=connection)
            connection.__exit__ = Mock(return_value=None)
            connection.execute.return_value.scalar.side_effect = [False, False, True, False]
            engine = Mock()
            engine.connect.return_value = connection
            with patch("app.core.backup.subprocess.run") as run, patch("app.core.backup.create_engine", return_value=engine):
                with self.assertRaisesRegex(ValueError, "destino PostgreSQL no está vacía"):
                    pg_restore(artifact, "postgresql://pilot:secret@restore.example:5432/restore_db", target_identity="restore-master")
            run.assert_not_called()
            engine.dispose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
