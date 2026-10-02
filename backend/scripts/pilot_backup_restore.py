from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.backup import backup_database, pg_restore, restore_sqlite, verify_sqlite_backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup/restore aislado para el rehearsal del piloto ANCHI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="Crear backup SQLite o PostgreSQL usando la herramienta estándar")
    backup.add_argument("--database-url", required=True)
    backup.add_argument("--output", required=True)
    backup.add_argument("--environment", required=True)
    backup.add_argument("--identity", required=True)

    restore = subparsers.add_parser("restore-sqlite", help="Restaurar un backup SQLite en una base aislada")
    restore.add_argument("--input", required=True)
    restore.add_argument("--database-url", required=True)

    restore_pg = subparsers.add_parser("restore-postgres", help="Restaurar PostgreSQL en una base vacía distinta y validar conexión")
    restore_pg.add_argument("--input", required=True)
    restore_pg.add_argument("--database-url", required=True)
    restore_pg.add_argument("--target-identity", required=True, help="Identidad operativa distinta de la identidad fuente del sidecar")

    verify = subparsers.add_parser("verify-sqlite", help="Validar checksum e integridad de un backup SQLite")
    verify.add_argument("--input", required=True)

    args = parser.parse_args()
    if args.command == "backup":
        result = backup_database(
            args.database_url,
            args.output,
            environment=args.environment,
            database_identity=args.identity,
        )
    elif args.command == "restore-sqlite":
        result = restore_sqlite(args.input, args.database_url)
    elif args.command == "restore-postgres":
        result = pg_restore(args.input, args.database_url, target_identity=args.target_identity)
    else:
        result = verify_sqlite_backup(args.input)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
