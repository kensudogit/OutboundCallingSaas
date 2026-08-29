"""録音を Twilio から自社ストレージへ移す（原則 5）。

    python -m app.jobs.recordings

★ 「コピーしてから消す」の順序を守る。先に Twilio 側を消すと、コピーに
  失敗したときに録音が完全に失われる。逆に、コピーしただけで Twilio 側を
  消さないと、自社の保存期間の設定が意味を持たなくなる（プロバイダに
  実体が残り続ける）。これが最も多い漏れ。

★ 各段は途中で失敗してよい。次回の実行が続きから拾う。
  そのために状態を DB の列（storage_key / provider_purged_at）で持つ。
"""

from __future__ import annotations

import argparse
import asyncio
import base64

from .. import storage
from ..config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_CONFIGURED
from ..db.engine import admin_tx, close_pool, init_pool
from ..logger import logger

# 1 回の実行で扱う件数。長時間 1 トランザクションを掴まないための上限
BATCH_SIZE = 50


def recording_url(url: str) -> str:
    """取得する URL を決める。

    ★ 拡張子を付けないと Twilio 側の既定形式（mp3）で返る。デュアルチャンネルの
      左右を保ったまま扱いたいので .wav を明示する。mp3 で受けると
      チャンネル分離が壊れ、話者が混ざる。
    """
    return url if url.endswith(".wav") else f"{url}.wav"


async def _download(url: str) -> bytes:
    """Twilio から録音を取る。

    ★ Recording URL には Account SID / Auth Token の Basic 認証が要る。
    """
    import httpx

    credentials = base64.b64encode(
        f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()
    ).decode()

    target = recording_url(url)
    async with httpx.AsyncClient(timeout=60.0) as client:
        res = await client.get(target, headers={"Authorization": f"Basic {credentials}"})
        res.raise_for_status()
        return res.content


async def _purge_from_provider(recording_sid: str) -> None:
    """Twilio 側の録音を削除する。

    ★ コピーが済んでいることを確認してから呼ぶ。
    """
    import httpx

    credentials = base64.b64encode(
        f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()
    ).decode()
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}"
        f"/Recordings/{recording_sid}.json"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.delete(url, headers={"Authorization": f"Basic {credentials}"})
        # 404 は既に消えている。成功として扱う
        if res.status_code not in (204, 404):
            res.raise_for_status()


async def copy_pending(*, limit: int = BATCH_SIZE, purge: bool = True) -> dict[str, int]:
    """未コピーの録音を自社ストレージへ移す。"""
    if not TWILIO_CONFIGURED:
        # 認証情報が無いと取得も削除も 401 になる。空振りの再試行で
        # failed だけが積み上がると、本物の失敗が埋もれる
        logger.warn("Twilio が未設定のため録音のコピーをスキップします")
        return {"copied": 0, "purged": 0, "failed": 0}

    if not storage.is_configured():
        logger.warn("録音ストレージが未設定のためコピーをスキップします")
        return {"copied": 0, "purged": 0, "failed": 0}

    if not storage.free_local_space_ok():
        # ★ 空きが尽きると put が静かに失敗し、録音だけが欠ける。先に止める
        logger.error("録音保管先の空き容量が不足しています。コピーを中止します")
        return {"copied": 0, "purged": 0, "failed": 0}

    async with admin_tx() as conn:
        rows = await conn.fetch(
            """
            select id, tenant_id, call_id, provider_recording_sid, provider_url
              from recordings
             where storage_key is null
               and deleted_at is null
               and provider_url is not null
             order by created_at
             limit $1
            """,
            limit,
        )

    copied = purged = failed = 0

    for row in rows:
        key = storage.storage_key(
            tenant_id=str(row["tenant_id"]),
            call_id=str(row["call_id"]),
            recording_sid=row["provider_recording_sid"],
        )
        try:
            audio = await _download(row["provider_url"])
        except Exception as exc:  # noqa: BLE001
            # まだ Twilio 側で生成中のことがある。次回また拾う
            logger.warn(
                "録音の取得に失敗しました（次回再試行）",
                recording_id=str(row["id"]),
                err=str(exc),
            )
            failed += 1
            continue

        try:
            storage.backend().put(key, audio, content_type="audio/wav")
        except Exception as exc:  # noqa: BLE001
            logger.error("録音の保存に失敗しました", recording_id=str(row["id"]), err=str(exc))
            failed += 1
            continue

        async with admin_tx() as conn:
            await conn.execute(
                "update recordings set storage_key = $2, size_bytes = $3 where id = $1",
                row["id"], key, len(audio),
            )
        copied += 1
        logger.info(
            "録音をコピーしました",
            recording_id=str(row["id"]), key=key, bytes=len(audio),
        )

        # ★ コピーが済んでから Twilio 側を消す。順序を逆にしない
        if not purge:
            continue
        try:
            await _purge_from_provider(row["provider_recording_sid"])
        except Exception as exc:  # noqa: BLE001
            # 消せなくてもコピーは済んでいる。次回の purge_orphans が拾う
            logger.warn(
                "プロバイダ側の録音削除に失敗しました",
                recording_id=str(row["id"]), err=str(exc),
            )
            continue

        async with admin_tx() as conn:
            await conn.execute(
                "update recordings set provider_purged_at = now(), provider_url = null "
                "where id = $1",
                row["id"],
            )
        purged += 1

    return {"copied": copied, "purged": purged, "failed": failed}


async def purge_orphans(*, limit: int = BATCH_SIZE) -> int:
    """コピー済みだがプロバイダ側に残っている録音を消す。

    copy_pending の purge が失敗したものを拾い直す経路。
    """
    if not TWILIO_CONFIGURED:
        return 0

    async with admin_tx() as conn:
        rows = await conn.fetch(
            """
            select id, provider_recording_sid from recordings
             where storage_key is not null
               and provider_purged_at is null
               and provider_url is not null
             order by created_at limit $1
            """,
            limit,
        )

    purged = 0
    for row in rows:
        try:
            await _purge_from_provider(row["provider_recording_sid"])
        except Exception as exc:  # noqa: BLE001
            logger.warn("プロバイダ側の録音削除に失敗", id=str(row["id"]), err=str(exc))
            continue
        async with admin_tx() as conn:
            await conn.execute(
                "update recordings set provider_purged_at = now(), provider_url = null "
                "where id = $1",
                row["id"],
            )
        purged += 1

    if purged:
        logger.info("プロバイダ側の録音を削除しました", count=purged)
    return purged


async def main() -> None:
    parser = argparse.ArgumentParser(description="録音の自社ストレージへのコピー")
    parser.add_argument("--limit", type=int, default=BATCH_SIZE)
    parser.add_argument(
        "--no-purge", action="store_true", help="Twilio 側の録音を消さない（検証用）"
    )
    args = parser.parse_args()

    await init_pool()
    try:
        result = await copy_pending(limit=args.limit, purge=not args.no_purge)
        result["purged_orphans"] = await purge_orphans(limit=args.limit)
        print(result)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
