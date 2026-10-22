"""AES-CTR crypto: roundtrip and arbitrary-range decryption math."""
import os

import pytest

from app.media.crypto import (
    new_key, decrypt_file, encrypt_file, iter_decrypted, plaintext_size,
    NONCE_BYTES, BLOCK_SIZE,
)


@pytest.fixture()
def encrypted(tmp_path):
    key = new_key()
    plaintext = bytes(range(256)) * 300 + b'tail-bytes'  # not block-aligned
    src = tmp_path / 'plain.bin'
    dst = tmp_path / 'plain.bin.enc'
    src.write_bytes(plaintext)
    encrypt_file(str(src), str(dst), key)
    return str(dst), key, plaintext


def test_roundtrip(encrypted):
    enc_path, key, plaintext = encrypted
    assert b''.join(iter_decrypted(enc_path, key)) == plaintext


def test_decrypt_file_writes_the_whole_plaintext(encrypted, tmp_path):
    enc_path, key, plaintext = encrypted
    out = tmp_path / 'out.bin'
    out.write_bytes(b'stale leftovers from an earlier attempt' * 1000)
    decrypt_file(enc_path, str(out), key)
    assert out.read_bytes() == plaintext


def test_plaintext_size(encrypted):
    enc_path, _, plaintext = encrypted
    assert plaintext_size(enc_path) == len(plaintext)
    assert os.path.getsize(enc_path) == len(plaintext) + NONCE_BYTES


def test_ciphertext_differs_from_plaintext(encrypted):
    enc_path, _, plaintext = encrypted
    with open(enc_path, 'rb') as f:
        f.seek(NONCE_BYTES)
        assert f.read(len(plaintext)) != plaintext


@pytest.mark.parametrize('start,end', [
    (0, 1),
    (0, BLOCK_SIZE),
    (1, BLOCK_SIZE - 1),           # inside first block
    (BLOCK_SIZE, 2 * BLOCK_SIZE),  # block-aligned
    (BLOCK_SIZE - 1, BLOCK_SIZE + 1),  # straddles a block boundary
    (7, 7777),
    (76799, 76810),                # end of file (76810 bytes total)
    (76809, 76810),                # last byte
])
def test_arbitrary_ranges(encrypted, start, end):
    enc_path, key, plaintext = encrypted
    got = b''.join(iter_decrypted(enc_path, key, start, end))
    assert got == plaintext[start:end]


def test_range_beyond_eof_is_clamped(encrypted):
    enc_path, key, plaintext = encrypted
    got = b''.join(iter_decrypted(enc_path, key, len(plaintext) - 5, len(plaintext) + 100))
    assert got == plaintext[-5:]


def test_empty_and_invalid_ranges(encrypted):
    enc_path, key, plaintext = encrypted
    assert b''.join(iter_decrypted(enc_path, key, 10, 10)) == b''
    assert b''.join(iter_decrypted(enc_path, key, 20, 10)) == b''
    assert b''.join(iter_decrypted(enc_path, key, -1, 10)) == b''


def test_wrong_key_garbles(encrypted):
    enc_path, _, plaintext = encrypted
    assert b''.join(iter_decrypted(enc_path, new_key())) != plaintext
