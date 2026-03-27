"""Entry point for running switchboard as a module: python -m switchboard

Sub-commands:
    (default)       Start the switchboard server
    generate-key    Print a new Fernet master key for SWITCHBOARD_MASTER_KEY
    migrate-auth    Create owner user, seed OAuth client, backfill FK columns
"""

import asyncio
import sys


def _generate_key():
    from cryptography.fernet import Fernet
    print(Fernet.generate_key().decode())


def _migrate_auth(args):
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="python -m switchboard migrate-auth",
        description="Create owner user from Authelia credentials and seed OAuth client.",
    )
    parser.add_argument("--email", default=os.environ.get("SWITCHBOARD_OWNER_EMAIL"))
    parser.add_argument("--name", default=os.environ.get("SWITCHBOARD_OWNER_NAME", "Owner"))
    parser.add_argument(
        "--password-hash",
        dest="password_hash",
        default=os.environ.get("SWITCHBOARD_OWNER_PASSWORD_HASH"),
    )
    parser.add_argument("--slug", default=os.environ.get("SWITCHBOARD_INSTANCE_SLUG", "default"))
    parser.add_argument(
        "--instance-name",
        dest="instance_name",
        default=os.environ.get("SWITCHBOARD_INSTANCE_NAME", "Switchboard"),
    )

    parsed = parser.parse_args(args)

    if not parsed.email:
        parser.error("--email is required (or set SWITCHBOARD_OWNER_EMAIL)")
    if not parsed.password_hash:
        parser.error("--password-hash is required (or set SWITCHBOARD_OWNER_PASSWORD_HASH)")

    from switchboard.migrate import run_migrate_auth

    result = asyncio.run(
        run_migrate_auth(
            email=parsed.email,
            name=parsed.name,
            password_hash=parsed.password_hash,
            slug=parsed.slug,
            instance_name=parsed.instance_name,
        )
    )

    if result["status"] == "already_migrated":
        print(f"Already migrated — owner user exists (id={result['owner_id']}). No changes made.")
    else:
        print(f"\nMigration complete. Owner user id={result['owner_id']}")

    # Force exit — aiosqlite singleton connection doesn't close cleanly on asyncio.run() shutdown
    import sys as _sys
    _sys.stdout.flush()
    _sys.stderr.flush()
    import os as _os
    _os._exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "generate-key":
        _generate_key()
    elif len(sys.argv) > 1 and sys.argv[1] == "migrate-auth":
        _migrate_auth(sys.argv[2:])
    else:
        from switchboard.server.app import main
        asyncio.run(main())
