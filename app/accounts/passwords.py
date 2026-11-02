"""Password hashing with Argon2id, and the rules for new passwords."""

from __future__ import annotations

import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# OWASP's recommended minimum for Argon2id (19 MiB of memory, 2 passes, 1 lane): slow for an attacker who steals
# the hashes, and light enough for a small server.
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)

MIN_LENGTH = 10
MAX_LENGTH = 128

# Very common passwords that are long enough to pass the length rule
COMMON = {
    "1234567890", "0123456789", "0987654321", "12345678910", "123456789012", "1q2w3e4r5t", "1qaz2wsx3edc",
    "qwertyuiop", "asdfghjkl;", "zxcvbnm,./", "qwerty12345", "qwerty123456", "password12", "password123",
    "password1234", "password@123", "password!", "passw0rd123", "iloveyou12", "iloveyou123", "welcome123",
    "welcome@123", "admin12345", "admin123456", "admin@1234", "administrator", "letmein123", "changeme123",
    "abcdefghij", "abcd123456", "abc1234567", "india@1234", "india12345", "mumbai@123", "secret1234",
    "doctor1234", "doctor@123", "clinic@123", "hospital123", "monkey12345", "dragon12345", "football123",
    "baseball123", "sunshine123", "princess123", "trustno1234", "superman123", "11111111111", "00000000000",
}


def _normal(password: str) -> str:
    # the same password typed on different keyboards or phones gives the same hash
    return unicodedata.normalize("NFKC", password)


def hash_password(password: str) -> str:
    return _hasher.hash(_normal(password))


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, _normal(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


_dummy_hash: str | None = None


def verify_nothing(password: str) -> None:
    """The same work as a real check, so an unknown username takes as long as a wrong password."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _hasher.hash("timing-only, never a real password")
    verify_password(_dummy_hash, password)


def password_problem(password: str, username: str = "") -> str | None:
    """Why a new password isn't allowed, or None when it is fine."""
    password = _normal(password)
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    lowered = password.lower()
    if lowered in COMMON or len(set(lowered)) < 4:
        return "This password is too easy to guess. Try a few unrelated words, or a longer mix."
    if username and username.lower() in lowered and len(password) < 16:
        return "Don't build the password around the username (or make it at least 16 characters)."
    return None
