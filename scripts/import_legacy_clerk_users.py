"""Import legacy Clerk development user mappings from CSV.

CSV format:

legacy_user_id,email
user_abc123,person@example.com

The email is normalized and HMAC-SHA256 hashed with
CLERK_MIGRATION_EMAIL_HASH_SECRET before it is stored. Do not commit the CSV.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from pathlib import Path

from sqlalchemy import text

from app.config import get_settings
from app.db.database import engine
from app.services.user_migration import hash_email

settings = get_settings()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import legacy Clerk user mapping CSV")
    parser.add_argument("csv_path", help="Path to CSV with legacy_user_id,email columns")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and count rows without writing to the database",
    )
    return parser.parse_args()


async def import_csv(csv_path: Path, dry_run: bool = False) -> int:
    secret = settings.clerk_migration_email_hash_secret
    if not secret:
        raise RuntimeError("CLERK_MIGRATION_EMAIL_HASH_SECRET must be configured")

    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    rows: list[dict[str, str]] = []
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"legacy_user_id", "email"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError("CSV must include legacy_user_id and email columns")

        for line_number, row in enumerate(reader, start=2):
            legacy_user_id = (row.get("legacy_user_id") or "").strip()
            email = (row.get("email") or "").strip()
            if not legacy_user_id or not email:
                raise ValueError(f"Missing legacy_user_id/email on CSV line {line_number}")

            rows.append({
                "legacy_user_id": legacy_user_id,
                "email_hash": hash_email(email, secret),
            })

    if dry_run:
        print(f"✓ Dry run validated {len(rows)} mapping rows")
        return len(rows)

    async with engine.begin() as conn:
        for row in rows:
            await conn.execute(
                text("""
                    INSERT INTO legacy_clerk_user_mappings (legacy_user_id, email_hash, updated_at)
                    VALUES (:legacy_user_id, :email_hash, CURRENT_TIMESTAMP)
                    ON CONFLICT (legacy_user_id)
                    DO UPDATE SET
                        email_hash = EXCLUDED.email_hash,
                        updated_at = CURRENT_TIMESTAMP
                """),
                row,
            )

    print(f"✓ Imported {len(rows)} legacy Clerk user mappings")
    return len(rows)


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(import_csv(Path(args.csv_path), dry_run=args.dry_run))
