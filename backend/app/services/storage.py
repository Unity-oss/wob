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

import abc
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
from pydantic import SecretStr

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


class FileStorage(abc.ABC):
    """Encrypted file storage over a pluggable byte-level backend (local disk, S3/R2, ...).

    Subclasses only implement raw bytes in/out (`_get_bytes`, `_put_new_bytes`, `_replace_bytes`,
    `_delete_bytes`, `_list_keys`); the envelope encryption and the write-once/rotation logic live
    here once, so the storage provider never holds keys and every backend behaves the same way."""

    def __init__(self, keyring: KeyRing | None, *, allow_plaintext_reads: bool = False) -> None:
        self.keyring = keyring
        self.allow_plaintext_reads = allow_plaintext_reads

    @staticmethod
    def _validate_key(key: str) -> str:
        parts = PurePosixPath(key).parts
        if not parts or PurePosixPath(key).is_absolute() or ".." in parts:
            raise ValueError(f"invalid storage key: {key!r}")
        return key

    # --- raw bytes, implemented per backend -----------------------------------------------------

    @abc.abstractmethod
    def _get_bytes(self, key: str) -> bytes: ...

    @abc.abstractmethod
    def _put_new_bytes(self, key: str, blob: bytes) -> None:
        """Write a new object. Never overwrites: originals are immutable (spec §5.1)."""

    @abc.abstractmethod
    def _replace_bytes(self, key: str, blob: bytes) -> None:
        """Overwrite an existing object. Only used by key-rotation maintenance."""

    @abc.abstractmethod
    def _delete_bytes(self, key: str) -> None: ...

    @abc.abstractmethod
    def _list_keys(self) -> Iterator[str]: ...

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
        self._validate_key(key)
        self._put_new_bytes(key, self.encrypt(key, data))

    def read(self, key: str) -> bytes:
        self._validate_key(key)
        return self.decrypt(key, self._get_bytes(key))

    def delete(self, key: str) -> None:
        self._validate_key(key)
        self._delete_bytes(key)

    def keys(self) -> Iterator[str]:
        yield from self._list_keys()

    # --- maintenance (the only operations that replace a file) ---------------------------------

    def is_encrypted(self, key: str) -> bool:
        return self._get_bytes(key)[: len(MAGIC)] == MAGIC

    def encrypt_in_place(self, key: str) -> bool:
        """Encrypt a legacy plaintext file. Returns False if it was already encrypted."""
        if self.is_encrypted(key):
            return False
        self._replace_bytes(key, self.encrypt(key, self._get_bytes(key)))
        return True

    def rewrap(self, key: str) -> bool:
        """Re-wrap the file's data key with the active key. Contents are not re-encrypted."""
        blob = self._get_bytes(key)
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
        self._replace_bytes(key, header + nonce + ciphertext)
        return True


class LocalStorage(FileStorage):
    """Files on a local or mounted volume."""

    def __init__(
        self, root: Path, keyring: KeyRing | None, *, allow_plaintext_reads: bool = False
    ) -> None:
        super().__init__(keyring, allow_plaintext_reads=allow_plaintext_reads)
        self.root = root.resolve()

    def _resolve(self, key: str) -> Path:
        return self.root.joinpath(*PurePosixPath(key).parts)

    def _get_bytes(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def _put_new_bytes(self, key: str, blob: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")
        with open(tmp, "xb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)  # atomic and never overwrites: raises FileExistsError
        finally:
            tmp.unlink()

    def _replace_bytes(self, key: str, blob: bytes) -> None:
        path = self._resolve(key)
        tmp = path.with_name(path.name + ".partial")
        with open(tmp, "xb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _delete_bytes(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    def _list_keys(self) -> Iterator[str]:
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.name.endswith(".partial"):
                yield path.relative_to(self.root).as_posix()


class R2Storage(FileStorage):
    """Files in a Cloudflare R2 (S3-compatible) bucket, under an optional key prefix.

    Write-once is enforced with a conditional PutObject (If-None-Match: *), which R2 supports for
    all objects; this is weaker than the local backend's hardlink trick only in that a concurrent
    write to the same key races at the network layer instead of the filesystem, which this app
    never does (storage keys embed a freshly generated uuid per upload)."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        prefix: str = "",
        keyring: KeyRing | None,
        allow_plaintext_reads: bool = False,
    ) -> None:
        import boto3
        from botocore.config import Config

        super().__init__(keyring, allow_plaintext_reads=allow_plaintext_reads)
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}
            ),
            region_name="auto",
        )

    def _object_key(self, key: str) -> str:
        parts = PurePosixPath(key).parts
        return "/".join((self.prefix, *parts)) if self.prefix else "/".join(parts)

    def _get_bytes(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(Bucket=self.bucket, Key=self._object_key(key))[
                "Body"
            ].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise FileNotFoundError(key) from exc
            raise

    def _put_new_bytes(self, key: str, blob: bytes) -> None:
        from botocore.exceptions import ClientError

        try:
            self._client.put_object(
                Bucket=self.bucket, Key=self._object_key(key), Body=blob, IfNoneMatch="*"
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("PreconditionFailed", "412"):
                raise FileExistsError(key) from exc
            raise

    def _replace_bytes(self, key: str, blob: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=self._object_key(key), Body=blob)

    def _delete_bytes(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._object_key(key))

    def _list_keys(self) -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        kwargs = {"Bucket": self.bucket}
        if self.prefix:
            kwargs["Prefix"] = self.prefix + "/"
        keys = []
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                object_key = obj["Key"]
                relative = object_key[len(self.prefix) + 1 :] if self.prefix else object_key
                if not relative.endswith(".partial"):
                    keys.append(relative)
        yield from sorted(keys)


def build_storage(settings: Settings, *, allow_plaintext_reads: bool = False) -> FileStorage:
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
    if settings.storage_backend == "r2":
        if not (settings.r2_bucket and settings.r2_endpoint_url and settings.r2_access_key_id):
            raise StorageError(
                "STORAGE_BACKEND=r2 requires R2_BUCKET, R2_ENDPOINT_URL, R2_ACCESS_KEY_ID and "
                "R2_SECRET_ACCESS_KEY"
            )
        return R2Storage(
            bucket=settings.r2_bucket,
            endpoint_url=settings.r2_endpoint_url,
            access_key_id=settings.r2_access_key_id,
            secret_access_key=(settings.r2_secret_access_key or SecretStr("")).get_secret_value(),
            prefix=settings.r2_prefix,
            keyring=keyring,
            allow_plaintext_reads=allow_plaintext_reads,
        )
    return LocalStorage(settings.storage_dir, keyring, allow_plaintext_reads=allow_plaintext_reads)


def get_storage(settings: Annotated[Settings, Depends(get_settings)]) -> FileStorage:
    return build_storage(settings)
