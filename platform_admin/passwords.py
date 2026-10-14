"""Versioned password hashes; the installer passes plaintext only via stdin."""

import hashlib
import hmac
import secrets
import sys


def hash_password(password):
    if not isinstance(password, str) or not 16 <= len(password) <= 1024:
        raise ValueError("invalid_password")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"pbkdf2_sha256:600000:{salt}:{digest}"


def valid_hash(encoded):
    try:
        algorithm, rounds, salt, digest = encoded.split(":")
        return (algorithm == "pbkdf2_sha256" and rounds == "600000"
                and len(salt) == 32 and len(bytes.fromhex(salt)) == 16
                and len(digest) == 64 and len(bytes.fromhex(digest)) == 32)
    except (ValueError, AttributeError):
        return False


def verify_password(password, encoded):
    if not valid_hash(encoded) or not isinstance(password, str) or len(password) > 1024:
        return False
    _, rounds, salt, digest = encoded.split(":")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
    return hmac.compare_digest(actual, digest)


def initial_password(secret, nonce):
    if len(secret) < 32 or len(nonce) != 64 or len(bytes.fromhex(nonce)) != 32:
        raise ValueError("invalid_bootstrap_nonce")
    return hmac.new(secret.encode(), bytes.fromhex(nonce), hashlib.sha256).hexdigest()[:32]


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--derive":
            print(initial_password(sys.stdin.read(4097), sys.argv[2]))
        elif sys.argv[1:] == ["--verify"]:
            candidate, encoded = sys.stdin.read(4097).split("\n", 1)
            sys.exit(0 if verify_password(candidate, encoded) else 1)
        else:
            print(hash_password(sys.stdin.read(1025)))
    except ValueError:
        sys.exit(1)
