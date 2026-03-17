#!/usr/bin/env python3
"""Generate VAPID keypair for Web Push notifications.

Usage:
    python3 scripts/generate_vapid.py

Output: shell export statements ready to paste into your .env or systemd unit.

After generating, add to the switchboard service environment:
  VAPID_PRIVATE_KEY=<base64url private key>
  VAPID_PUBLIC_KEY=<base64url public key>
  VAPID_CLAIM_EMAIL=mailto:you@example.com

For systemd, add to /etc/systemd/system/switchboard.service under [Service]:
  Environment="VAPID_PRIVATE_KEY=..."
  Environment="VAPID_PUBLIC_KEY=..."
  Environment="VAPID_CLAIM_EMAIL=mailto:..."
Then: systemctl daemon-reload && systemctl restart switchboard
"""

import sys

try:
    from py_vapid import Vapid
except ImportError:
    print("Error: pywebpush not installed. Run: pip install pywebpush", file=sys.stderr)
    sys.exit(1)

vapid = Vapid()
vapid.generate_keys()

private_key = vapid.private_key_urlsafe()
public_key = vapid.public_key_urlsafe()

print("# Add these to your .env or systemd service file:\n")
print(f'export VAPID_PRIVATE_KEY="{private_key}"')
print(f'export VAPID_PUBLIC_KEY="{public_key}"')
print('export VAPID_CLAIM_EMAIL="mailto:your@email.com"')
print()
print("# Keep VAPID_PRIVATE_KEY secret. VAPID_PUBLIC_KEY is sent to browsers.")
