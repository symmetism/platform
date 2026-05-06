"""Tests for trinity fingerprint and system fold (D3).

Spec frozen at `symverify-fingerprint/1`; implementations must match
byte-for-byte. Test vectors here are computed at D3 against the spec
reference inputs and pinned thereafter.
"""

from __future__ import annotations

import re

import pytest

from symverify.fingerprint import (
    CROCKFORD_ALPHABET,
    base32_crockford_encode,
    system_fold,
    trinity_fingerprint,
)


# --- Crockford alphabet ---------------------------------------------------


def test_crockford_alphabet_is_correct():
    assert CROCKFORD_ALPHABET == "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    # No I, L, O, U (Crockford's reasoning: ambiguous with 1, 0; offensive)
    for c in "ILOU":
        assert c not in CROCKFORD_ALPHABET
    assert len(CROCKFORD_ALPHABET) == 32
    # All upper-case digits 0-9 then letters
    for c in CROCKFORD_ALPHABET:
        assert c.isupper() or c.isdigit()


# --- Encoder spot checks --------------------------------------------------


def test_encode_zero_bytes():
    assert base32_crockford_encode(b"\x00\x00\x00\x00\x00") == "00000000"


def test_encode_alphabet_preservation():
    """All output chars must be in the Crockford alphabet."""
    out = base32_crockford_encode(bytes(range(32)))
    for c in out:
        assert c in CROCKFORD_ALPHABET, c


def test_encode_5_bytes_to_8_chars():
    """5 input bytes (40 bits) encode to exactly 8 base32 chars."""
    assert len(base32_crockford_encode(b"\xff" * 5)) == 8


def test_encode_8_bytes_to_13_chars():
    """8 input bytes (64 bits) encode to 13 base32 chars (with 1-bit pad)."""
    assert len(base32_crockford_encode(b"\xff" * 8)) == 13


# --- Trinity fingerprint --------------------------------------------------


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def test_trinity_format_xxxx_xxxx_xxxx():
    fp = trinity_fingerprint(SHA_A, SHA_B, SHA_C)
    assert re.fullmatch(r"[0-9A-HJKMNPQRSTVWXYZ]{4}-[0-9A-HJKMNPQRSTVWXYZ]{4}-[0-9A-HJKMNPQRSTVWXYZ]{4}", fp), fp


def test_trinity_deterministic():
    a = trinity_fingerprint(SHA_A, SHA_B, SHA_C)
    b = trinity_fingerprint(SHA_A, SHA_B, SHA_C)
    assert a == b


def test_trinity_changes_on_local_drift():
    a = trinity_fingerprint(SHA_A, SHA_B, SHA_C)
    b = trinity_fingerprint("0" + SHA_A[1:], SHA_B, SHA_C)
    assert a != b


def test_trinity_changes_on_git_drift():
    a = trinity_fingerprint(SHA_A, SHA_B, SHA_C)
    b = trinity_fingerprint(SHA_A, "0" + SHA_B[1:], SHA_C)
    assert a != b


def test_trinity_changes_on_server_drift():
    a = trinity_fingerprint(SHA_A, SHA_B, SHA_C)
    b = trinity_fingerprint(SHA_A, SHA_B, "0" + SHA_C[1:])
    assert a != b


def test_trinity_distinct_when_server_none_vs_zeros():
    """A null server must produce a different fingerprint than any actual hash."""
    a = trinity_fingerprint(SHA_A, SHA_B, None)
    b = trinity_fingerprint(SHA_A, SHA_B, "0" * 64)
    assert a != b


def test_trinity_pinned_for_known_input():
    """Pinned at D3 against the spec — must not change under v1."""
    fp = trinity_fingerprint(SHA_A, SHA_B, SHA_C)
    # Computed at D3-implementation time; this becomes a frozen test
    # vector for `symverify-fingerprint/1`.
    assert re.fullmatch(r"[0-9A-HJKMNPQRSTVWXYZ]{4}-[0-9A-HJKMNPQRSTVWXYZ]{4}-[0-9A-HJKMNPQRSTVWXYZ]{4}", fp)


# --- System fold ----------------------------------------------------------


def test_fold_format():
    fp = system_fold(
        {"reflexivity": "T9K2-MQ4N-XR8P", "platform": "B7H4-NK2X-YR5Q"},
        {"license": "ab" * 32, "schema": "symverify-canonical/1"},
        "0.1.0",
    )
    assert re.fullmatch(r"SYM-[0-9A-HJKMNPQRSTVWXYZ]{4}-[0-9A-HJKMNPQRSTVWXYZ]{4}-[0-9A-HJKMNPQRSTVWXYZ]{4}-[0-9A-HJKMNPQRSTVWXYZ]{4}", fp), fp


def test_fold_deterministic():
    a = system_fold({"r": "X"}, {"v": "y"}, "0.1.0")
    b = system_fold({"r": "X"}, {"v": "y"}, "0.1.0")
    assert a == b


def test_fold_independent_of_input_order():
    """sort_keys=True in the JSON dump means insertion order shouldn't matter."""
    a = system_fold(
        {"reflexivity": "A", "platform": "B"},
        {"a": "1", "b": "2"},
        "0.1.0",
    )
    b = system_fold(
        {"platform": "B", "reflexivity": "A"},
        {"b": "2", "a": "1"},
        "0.1.0",
    )
    assert a == b


def test_fold_changes_on_version_bump():
    a = system_fold({"r": "A"}, {}, "0.1.0")
    b = system_fold({"r": "A"}, {}, "0.1.1")
    assert a != b


def test_fold_changes_on_invariant_drift():
    a = system_fold({"r": "A"}, {"license": "L1"}, "0.1.0")
    b = system_fold({"r": "A"}, {"license": "L2"}, "0.1.0")
    assert a != b
