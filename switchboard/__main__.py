"""Entry point for running switchboard as a module: python -m switchboard"""

import asyncio
from switchboard.server.app import main

if __name__ == "__main__":
    asyncio.run(main())
