"""録音の保管（原則 5）。

★ Twilio の Recording URL をそのままフロントに渡さない。自社のアクセス制御を
  通さずに配られる URL が存在するのは良くない。自社ストレージへコピーしてから、
  短命な署名付き URL を発行して仲介する。

★ 自社ストレージへコピーする構成にすると、保存期間の管理と削除が自分の手に入る。
  Twilio 側の録音は、コピー完了を確認してから削除する。自社 DB のレコードだけ
  消してプロバイダに実体が残るのが、最も多い漏れ。

★ バックエンドを 2 つ持つ理由。S3 の認証情報が無いと 1 行も動かない設計にすると、
  録音まわりの経路が「本番でしか動かない」ものになり、結局誰も検証しなくなる。
  local バックエンドがあれば、コピー・聴取・削除の全経路をローカルで通せる。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import time
from pathlib import Path
from typing import Protocol

from .logger import logger

BACKEND = (os.environ.get("RECORDING_STORAGE") or "local").strip()
LOCAL_DIR = Path((os.environ.get("RECORDING_LOCAL_DIR") or "./var/recordings").strip())
URL_SECRET = (os.environ.get("RECORDING_URL_SECRET") or "").strip()

BUCKET = (os.environ.get("RECORDING_BUCKET") or "").strip()
REGION = (os.environ.get("RECORDING_REGION") or "").strip()
ACCESS_KEY = (os.environ.get("RECORDING_ACCESS_KEY_ID") or "").strip()
SECRET_KEY = (os.environ.get("RECORDING_SECRET_ACCESS_KEY") or "").strip()
ENDPOINT = (os.environ.get("RECORDING_ENDPOINT") or "").strip() or None


class StorageBackend(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def presigned_url(self, key: str, *, expires_in: int) -> str: ...


# ---------------------------------------------------------------- local


class LocalStorage:
    """開発・検証用。ファイルシステムに置く。

    ★ 署名付き URL は自前で作る。ファイルパスをそのまま返すと、
      URL を知っている人が誰でも録音を取れることになり、
      S3 構成との挙動の差が「本番でだけ守られている」状態を生む。
      ローカルでも同じ形（期限付き署名）にしておくと、
      アクセス制御の検証がローカルでできる。
    """

    def __init__(self, root: Path, secret: str) -> None:
        self._root = root
        self._secret = secret or "local-dev-secret"

    def _path(self, key: str) -> Path:
        # ★ key にディレクトリ traversal が混ざらないようにする。
        #   key は自分で組み立てているが、DB 経由で来る値でもあるので念のため
        safe = key.replace("\\", "/").lstrip("/")
        if ".." in safe.split("/"):
            raise ValueError(f"不正なキー: {key}")
        return self._root / safe

    def put(self, key: str, data: bytes, *, content_type: str = "audio/wav") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def sign(self, key: str, expires_at: int) -> str:
        payload = f"{key}:{expires_at}".encode()
        return hmac.new(self._secret.encode(), payload, hashlib.sha256).hexdigest()[:32]

    def verify(self, key: str, expires_at: int, signature: str) -> bool:
        if expires_at < int(time.time()):
            return False
        return hmac.compare_digest(self.sign(key, expires_at), signature)

    def presigned_url(self, key: str, *, expires_in: int = 300) -> str:
        expires_at = int(time.time()) + expires_in
        return (
            f"/api/recordings/file?key={key}"
            f"&expires={expires_at}&sig={self.sign(key, expires_at)}"
        )


# ---------------------------------------------------------------- S3


class S3Storage:
    def __init__(self) -> None:
        import boto3

        self._client = boto3.client(
            "s3",
            region_name=REGION or None,
            endpoint_url=ENDPOINT,
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
        )

    def put(self, key: str, data: bytes, *, content_type: str = "audio/wav") -> None:
        self._client.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=BUCKET, Key=key)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=BUCKET, Key=key)
            return True
        except ClientError:
            return False

    def presigned_url(self, key: str, *, expires_in: int = 300) -> str:
        # ★ 既定 5 分。長くすると共有された URL が独り歩きする
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=expires_in
        )


# ---------------------------------------------------------------- factory

_backend: StorageBackend | None = None


def backend() -> StorageBackend:
    global _backend
    if _backend is None:
        if BACKEND == "s3":
            _backend = S3Storage()
            logger.info("録音ストレージ: S3", bucket=BUCKET)
        else:
            _backend = LocalStorage(LOCAL_DIR, URL_SECRET)
            logger.info("録音ストレージ: ローカル", dir=str(LOCAL_DIR))
    return _backend


def reset_backend() -> None:
    """テスト用。設定を差し替えた後に呼ぶ。"""
    global _backend
    _backend = None


def is_configured() -> bool:
    if BACKEND == "s3":
        return bool(BUCKET and ACCESS_KEY and SECRET_KEY)
    return True   # ローカルは常に使える


def storage_key(*, tenant_id: str, call_id: str, recording_sid: str) -> str:
    """保存先のキー。

    ★ テナントごとにプレフィックスを分ける。バケットポリシーや
      ライフサイクルルールをテナント単位で当てられるようにするため。
    """
    return f"{tenant_id}/{call_id}/{recording_sid}.wav"


def presigned_url(key: str, *, expires_in: int = 300) -> str:
    return backend().presigned_url(key, expires_in=expires_in)


def free_local_space_ok(min_free_bytes: int = 500 * 1024 * 1024) -> bool:
    """ローカル保管のときだけ。空きが尽きると録音のコピーが静かに失敗する。"""
    if BACKEND == "s3":
        return True
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(LOCAL_DIR).free >= min_free_bytes
