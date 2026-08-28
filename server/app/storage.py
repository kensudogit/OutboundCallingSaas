"""録音の保管（原則 5）。

★ Twilio の Recording URL をそのままフロントに渡さない。自社のアクセス制御を
  通さずに配られる URL が存在するのは良くない。自社ストレージへコピーしてから、
  短命な署名付き URL を発行して仲介する。

★ 自社ストレージへコピーする構成にすると、保存期間の管理と削除が自分の手に入る。
  Twilio 側の録音は、コピー完了を確認してから削除する。自社 DB のレコードだけ
  消してプロバイダに実体が残るのが、最も多い漏れ。

未設定でも起動はする（発信と録音の記録までは動く）。聴取 API が 501 を返す。
"""

from __future__ import annotations

import os

BUCKET = (os.environ.get("RECORDING_BUCKET") or "").strip()
REGION = (os.environ.get("RECORDING_REGION") or "").strip()
ACCESS_KEY = (os.environ.get("RECORDING_ACCESS_KEY_ID") or "").strip()
SECRET_KEY = (os.environ.get("RECORDING_SECRET_ACCESS_KEY") or "").strip()
ENDPOINT = (os.environ.get("RECORDING_ENDPOINT") or "").strip() or None


def is_configured() -> bool:
    return bool(BUCKET and ACCESS_KEY and SECRET_KEY)


def _client():
    import boto3

    return boto3.client(
        "s3",
        region_name=REGION or None,
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
    )


def presigned_url(key: str, *, expires_in: int = 300) -> str:
    """短命な署名付き URL。既定 5 分。

    長くすると、共有された URL が独り歩きする。監査ログは呼び出し側で残す。
    """
    return _client().generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=expires_in
    )


def put_object(key: str, data: bytes, *, content_type: str = "audio/wav") -> None:
    _client().put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)


def delete_object(key: str) -> None:
    _client().delete_object(Bucket=BUCKET, Key=key)
