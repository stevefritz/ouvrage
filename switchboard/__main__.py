"""Entry point for running switchboard as a module: python -m switchboard

Sub-commands:
    (default)       Start the switchboard server
    generate-key    Print a new Fernet master key for SWITCHBOARD_MASTER_KEY
"""

import asyncio
import sys


def _generate_key():
    from cryptography.fernet import Fernet
    print(Fernet.generate_key().decode())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "generate-key":
        _generate_key()
    else:
        from switchboard.server.app import main
        asyncio.run(main())
