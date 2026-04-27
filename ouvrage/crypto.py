"""Fernet symmetric encryption helpers for sensitive credential fields.

Master key is read from the OUVRAGE_MASTER_KEY environment variable.
Generate a key with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Or via CLI:
    python -m ouvrage generate-key
"""
import os

from cryptography.fernet import Fernet


def get_master_key() -> bytes:
    """Read master key from secure copy, env var, or Docker secret. Raises RuntimeError if none exist."""
    key = os.environ.get("OUVRAGE_MASTER_KEY")
    if not key:
        # Secure copy (service-user only, worker can't read)
        for secret_path in ("/data/.secrets/master_key", "/run/secrets/master_key"):
            if os.path.isfile(secret_path):
                try:
                    with open(secret_path) as f:
                        key = f.read().strip()
                    break
                except PermissionError:
                    continue
    if not key:
        raise RuntimeError(
            "OUVRAGE_MASTER_KEY env var or /run/secrets/master_key required. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return key.encode()


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns a Fernet token string."""
    f = Fernet(get_master_key())
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a Fernet token string. Raises InvalidToken if key is wrong or data is corrupt."""
    f = Fernet(get_master_key())
    return f.decrypt(ciphertext.encode()).decode()


def is_fernet_token(value: str) -> bool:
    """Return True if value looks like a Fernet-encrypted token (starts with 'gAAAAA')."""
    return isinstance(value, str) and value.startswith("gAAAAA")


def maybe_encrypt(value: str) -> str:
    """Encrypt value only if it is not already a Fernet token. Used for migration."""
    if is_fernet_token(value):
        return value
    return encrypt_value(value)
