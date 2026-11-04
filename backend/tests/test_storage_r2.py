"""R2Storage against a fake in-memory S3 client (spec §3, §12): same contract as LocalStorage,
exercised via the shared FileStorage base, without touching a real bucket."""

import os

import pytest
from botocore.exceptions import ClientError

from app.services.storage import KeyRing, R2Storage, StorageIntegrityError, generate_key

SECRET_TEXT = b"%PDF-1.7 Jane Doe chaired the audit committee of Example Holdings."


def ring(*ids: str, active: str | None = None) -> KeyRing:
    return KeyRing.parse(",".join(f"{i}:{generate_key()}" for i in ids), active)


class FakeS3Client:
    """Just enough of the boto3 S3 client surface for R2Storage: a dict-backed bucket."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str | None = None):  # noqa: N803
        if IfNoneMatch == "*" and Key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Key] = Body

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop(Key, None)

    def get_paginator(self, _name: str) -> "FakePaginator":
        return FakePaginator(self)


class FakePaginator:
    def __init__(self, client: FakeS3Client) -> None:
        self.client = client

    def paginate(self, *, Bucket: str, Prefix: str = ""):  # noqa: N803
        contents = [{"Key": key} for key in self.client.objects if key.startswith(Prefix or "")]
        yield {"Contents": contents}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


@pytest.fixture
def store() -> R2Storage:
    s = R2Storage(
        bucket="wob-test",
        endpoint_url="https://example.r2.cloudflarestorage.com",
        access_key_id="k",
        secret_access_key="s",
        keyring=ring("k1"),
    )
    s._client = FakeS3Client()
    return s


def test_round_trip_and_nothing_readable_in_the_bucket(store: R2Storage) -> None:
    store.put("originals/fc-a/doc.pdf", SECRET_TEXT)
    raw = store._client.objects["originals/fc-a/doc.pdf"]
    assert b"Jane Doe" not in raw
    assert store.read("originals/fc-a/doc.pdf") == SECRET_TEXT


def test_files_are_write_once(store: R2Storage) -> None:
    store.put("doc.pdf", SECRET_TEXT)
    with pytest.raises(FileExistsError):
        store.put("doc.pdf", b"replacement")
    assert store.read("doc.pdf") == SECRET_TEXT


def test_key_rotation_rewraps_without_reencrypting(store: R2Storage) -> None:
    store.put("doc.pdf", SECRET_TEXT)
    before = store._client.objects["doc.pdf"]
    store.keyring = KeyRing({**store.keyring.keys, "k2": os.urandom(32)}, "k2")
    assert store.rewrap("doc.pdf") is True
    assert store.rewrap("doc.pdf") is False
    after = store._client.objects["doc.pdf"]
    assert after[-len(SECRET_TEXT) - 16 :] == before[-len(SECRET_TEXT) - 16 :]


def test_prefix_scopes_keys(store: R2Storage) -> None:
    store.prefix = "wob-awards-test"
    store.put("a.pdf", SECRET_TEXT)
    assert "wob-awards-test/a.pdf" in store._client.objects
    assert list(store.keys()) == ["a.pdf"]
    assert store.read("a.pdf") == SECRET_TEXT


def test_wrong_key_fails_authentication(store: R2Storage) -> None:
    store.put("doc.pdf", SECRET_TEXT)
    other = R2Storage(
        bucket="wob-test",
        endpoint_url="https://example.r2.cloudflarestorage.com",
        access_key_id="k",
        secret_access_key="s",
        keyring=ring("k2"),
    )
    other._client = store._client
    with pytest.raises(StorageIntegrityError):
        other.read("doc.pdf")
