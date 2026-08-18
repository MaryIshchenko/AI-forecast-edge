"""Back up the Postgres database to a git-tracked SQL dump.

Runs pg_dump inside the Postgres Docker container (matches the server's
exact version, so no host pg_dump install is needed) and writes a plain-SQL
dump to backups/db_backup.sql, overwriting it each run -- git history is the
version history here, not a pile of timestamped files. Commit the result and
`git push` to get the data off this laptop.

The dump includes --clean --if-exists, so it's a self-contained restore: it
drops and recreates every table before reloading data, so it works whether
the target database is empty or already has the old schema in it.

Restore with:
    docker exec -i ai-forecast-edge-db psql -U postgres -d ai_forecast_edge < backups/db_backup.sql

Usage:
    python src/backup_db.py
    python src/backup_db.py --out backups/snapshot.sql
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEFAULT_CONTAINER = "ai-forecast-edge-db"
DEFAULT_DB_NAME = "ai_forecast_edge"
DEFAULT_DB_USER = "postgres"
DEFAULT_OUT = "backups/db_backup.sql"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help="Docker container running Postgres")
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    parser.add_argument("--db-user", default=DEFAULT_DB_USER)
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output .sql file path")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "docker", "exec", args.container,
        "pg_dump", "-U", args.db_user, "--no-owner", "--no-privileges", "--clean", "--if-exists",
        args.db_name,
    ]
    print(f"Running: {' '.join(cmd)} > {out_path}")
    with out_path.open("w") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        out_path.unlink(missing_ok=True)
        print(f"pg_dump failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    size_kb = out_path.stat().st_size / 1024
    print(f"Wrote {out_path} ({size_kb:.1f} KB)")
    print("Next: git add backups/ && git commit && git push -- to back this up off this laptop.")


if __name__ == "__main__":
    main()
