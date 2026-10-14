"""Read-only compatibility for existing password hashes; never create hashes."""

import hashlib
import hmac


def valid_legacy_hash(encoded):
    try:
        algorithm, rounds, salt, digest = encoded.split(":")
        return (algorithm == "pbkdf2_sha256" and rounds == "600000"
                and len(salt) == 32 and len(bytes.fromhex(salt)) == 16
                and len(digest) == 64 and len(bytes.fromhex(digest)) == 32)
    except (ValueError, AttributeError):
        return False


def verify_legacy_password(password, encoded):
    if not valid_legacy_hash(encoded) or not isinstance(password, str) or len(password) > 1024:
        return False
    _, rounds, salt, digest = encoded.split(":")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
    return hmac.compare_digest(actual, digest)
