"""Write-once file storage with envelope encryption at rest (spec §3, §12).

Every file gets its own random 256-bit data key (DEK). The file is encrypted with AES-256-GCM
under the DEK, and the DEK is wrapped (AES-256-GCM) with a key-encryption key (KEK) from
FILE_ENCRYPTION_KEYS. Both encryptions are bound to the storage key as associated data, so a
file moved or copied to another path (another candidate's folder) fails to decrypt, and any
modification fails authentication.

On-disk layout:
    MAGIC | key-id length (1 byte) | key id | wrap nonce (12) | wrapped DEK (48) | nonce (12)
    | ciphertext + tag

KEKs live outside the database and the storage (environment / secrets manager). Rotation
re-wraps the small DEK headers without re-encrypting contents.
"""

import base64
import binascii
import os
import secrets
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Annotated

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends

from app.config import Settings, get_settings

MAGIC = b"WOBENC1\n"
NONCE = 12
WRAPPED_DEK = 32 + 16


class StorageError(Exception):
    pass


class StorageIntegrityError(StorageError):
    """The stored file could not be authenticated: tampered, moved, or wrong key."""


class KeyRing:
    def __init__(self, keys: dict[str, bytes], active: str) -> None:
        if active not in keys:
            raise StorageError(f"active file key {active!r} is not in FILE_ENCRYPTION_KEYS")
        self.keys = keys
        self.active = active

    @classmethod
    def parse(cls, spec: str, active: str | None) -> "KeyRing":
        """FILE_ENCRYPTION_KEYS="id1:base64key,id2:base64key"; keys are 32 random bytes."""
        keys: dict[str, bytes] = {}
        for item in filter(None, (part.strip() for part in spec.split(","))):
            key_id, _, encoded = item.partition(":")
            try:
                key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            except (binascii.Error, ValueError) as exc:
                raise StorageError(f"file key {key_id!r} is not valid base64") from exc
            if not key_id or len(key_id.encode()) > 32 or len(key) != 32:
                raise StorageError(f"file key {key_id!r} must be an id and 32 bytes of base64")
            keys[key_id] = key
        if not keys:
            raise StorageError("FILE_ENCRYPTION_KEYS is empty")
        return cls(keys, active or next(iter(keys)))


def generate_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _file_aad(storage_key: str) -> bytes:
    return b"wob-file:" + storage_key.encode()


def _dek_aad(key_id: str, storage_key: str) -> bytes:
    return b"wob-dek:" + key_id.encode() + b":" + storage_key.encode()


class LocalStorage:
    """Files on a local or mounted volume. An S3-compatible backend would implement the same
    put/read/delete interface; encryption stays here, so the storage provider never holds keys."""

    def __init__(
        self, root: Path, keyring: KeyRing | None, *, allow_plaintext_reads: bool = False
    ) -> None:
        self.root = root.resolve()
        self.keyring = keyring
        self.allow_plaintext_reads = allow_plaintext_reads

    def _resolve(self, key: str) -> Path:
        parts = PurePosixPath(key).parts
        if not parts or PurePosixPath(key).is_absolute() or ".." in parts:
            raise ValueError(f"invalid storage key: {key!r}")
        return self.root.joinpath(*parts)

    # --- encryption ----------------------------------------------------------------------------

    def encrypt(self, storage_key: str, data: bytes) -> bytes:
        if self.keyring is None:
            raise StorageError("no file encryption key configured; refusing to store plaintext")
        key_id = self.keyring.active
        dek = AESGCM.generate_key(bit_length=256)
        wrap_nonce, nonce = os.urandom(NONCE), os.urandom(NONCE)
        wrapped = AESGCM(self.keyring.keys[key_id]).encrypt(
            wrap_nonce, dek, _dek_aad(key_id, storage_key)
        )
        ciphertext = AESGCM(dek).encrypt(nonce, data, _file_aad(storage_key))
        header = MAGIC + bytes([len(key_id)]) + key_id.encode() + wrap_nonce + wrapped + nonce
        return header + ciphertext

    @staticmethod
    def _parse(blob: bytes) -> tuple[str, bytes, bytes, bytes, bytes, int]:
        offset = len(MAGIC)
        id_length = blob[offset]
        offset += 1
        key_id = blob[offset : offset + id_length].decode()
        offset += id_length
        wrap_nonce = blob[offset : offset + NONCE]
        offset += NONCE
        wrapped = blob[offset : offset + WRAPPED_DEK]
        offset += WRAPPED_DEK
        nonce = blob[offset : offset + NONCE]
        offset += NONCE
        return key_id, wrap_nonce, wrapped, nonce, blob[offset:], offset

    def _unwrap(self, storage_key: str, key_id: str, wrap_nonce: bytes, wrapped: bytes) -> bytes:
        if self.keyring is None or key_id not in self.keyring.keys:
            raise StorageIntegrityError(f"file key {key_id!r} is not available")
        try:
            return AESGCM(self.keyring.keys[key_id]).decrypt(
                wrap_nonce, wrapped, _dek_aad(key_id, storage_key)
            )
        except InvalidTag as exc:
            raise StorageIntegrityError(f"{storage_key}: failed authentication") from exc

    def decrypt(self, storage_key: str, blob: bytes) -> bytes:
        if not blob.startswith(MAGIC):
            if self.allow_plaintext_reads:
                return blob
            raise StorageIntegrityError(f"{storage_key}: stored without encryption")
        try:
            key_id, wrap_nonce, wrapped, nonce, ciphertext, _ = self._parse(blob)
        except (IndexError, UnicodeDecodeError) as exc:
            raise StorageIntegrityError(f"{storage_key}: damaged header") from exc
        dek = self._unwrap(storage_key, key_id, wrap_nonce, wrapped)
        try:
            return AESGCM(dek).decrypt(nonce, ciphertext, _file_aad(storage_key))
        except InvalidTag as exc:
            raise StorageIntegrityError(f"{storage_key}: failed authentication") from exc

    # --- file operations -----------------------------------------------------------------------

    def put(self, key: str, data: bytes) -> None:
        """Store a new file. Never overwrites: originals are immutable (spec §5.1)."""
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_new(path, self.encrypt(key, data))

    @staticmethod
    def _write_new(path: Path, blob: bytes) -> None:
        tmp = path.with_name(path.name + ".partial")
        with open(tmp, "xb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)  # atomic and never overwrites: raises FileExistsError
        finally:
            tmp.unlink()

    def read(self, key: str) -> bytes:
        return self.decrypt(key, self._resolve(key).read_bytes())

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    def keys(self) -> Iterator[str]:
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.name.endswith(".partial"):
                yield path.relative_to(self.root).as_posix()

    # --- maintenance (the only operations that replace a file) ---------------------------------

    def _replace(self, key: str, blob: bytes) -> None:
        path = self._resolve(key)
        tmp = path.with_name(path.name + ".partial")
        with open(tmp, "xb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def is_encrypted(self, key: str) -> bool:
        with open(self._resolve(key), "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC

    def encrypt_in_place(self, key: str) -> bool:
        """Encrypt a legacy plaintext file. Returns False if it was already encrypted."""
        if self.is_encrypted(key):
            return False
        self._replace(key, self.encrypt(key, self._resolve(key).read_bytes()))
        return True

    def rewrap(self, key: str) -> bool:
        """Re-wrap the file's data key with the active key. Contents are not re-encrypted."""
        blob = self._resolve(key).read_bytes()
        if not blob.startswith(MAGIC):
            raise StorageIntegrityError(f"{key}: stored without encryption")
        key_id, wrap_nonce, wrapped, nonce, ciphertext, _ = self._parse(blob)
        if key_id == self.keyring.active:
            return False
        dek = self._unwrap(key, key_id, wrap_nonce, wrapped)
        active = self.keyring.active
        new_wrap_nonce = os.urandom(NONCE)
        new_wrapped = AESGCM(self.keyring.keys[active]).encrypt(
            new_wrap_nonce, dek, _dek_aad(active, key)
        )
        header = MAGIC + bytes([len(active)]) + active.encode() + new_wrap_nonce + new_wrapped
        self._replace(key, header + nonce + ciphertext)
        return True


def build_storage(settings: Settings, *, allow_plaintext_reads: bool = False) -> LocalStorage:
    keyring = None
    if settings.file_encryption_keys is not None:
        keyring = KeyRing.parse(
            settings.file_encryption_keys.get_secret_value(), settings.file_encryption_active_key
        )
    elif not settings.allow_unencrypted_storage:
        raise StorageError(
            "FILE_ENCRYPTION_KEYS is not set. Generate one with `python -m app.cli "
            "generate-file-key`; files are never stored unencrypted."
        )
    return LocalStorage(settings.storage_dir, keyring, allow_plaintext_reads=allow_plaintext_reads)


def get_storage(settings: Annotated[Settings, Depends(get_settings)]) -> LocalStorage:
    return build_storage(settings)
