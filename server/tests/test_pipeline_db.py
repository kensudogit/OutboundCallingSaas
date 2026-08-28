"""録音 → コピー → 全文文字起こし → メトリクス の通し検証（実 DB）。

★ 各ジョブは単体では正しくても、繋いだときに落ちる。特に確認したいのは 3 点。

    1. コピーが済むまで聴取 API が Twilio の URL を漏らさないか
    2. ジョブを 2 回走らせても文字起こしが二重にならないか（冪等）
    3. 扱えない録音が無限に再試行されないか

Twilio への HTTP はモックする。実際に録音を取りに行くと、テストが
ネットワークと課金に依存してしまう。
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from app.domain.wav import build_dual_wav

ADMIN_DSN = os.environ.get(
    "TEST_ADMIN_DSN", "postgresql://migrator:migrator_password@localhost:5434/calling"
)


async def _connect():
    try:
        return await asyncpg.connect(ADMIN_DSN, timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB に接続できないためスキップします: {exc}")


def tone(value: int, samples: int) -> bytes:
    import struct

    return struct.pack(f"<{samples}h", *([value] * samples))


@pytest.fixture
async def admin():
    conn = await _connect()
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def local_storage(tmp_path, monkeypatch):
    from app import storage

    monkeypatch.setattr(storage, "BACKEND", "local")
    monkeypatch.setattr(storage, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(storage, "URL_SECRET", "test-secret")
    storage.reset_backend()
    yield storage
    storage.reset_backend()


@pytest.fixture
async def a_call(admin):
    """テナント・通話・録音を 1 セット作る。"""
    suffix = f"{uuid.uuid4().int % 10**6:06d}"
    tenant_id = await admin.fetchval(
        "insert into tenants (name, company_name) values ($1, $1) returning id", f"t-{suffix}"
    )
    list_id = await admin.fetchval(
        "insert into contact_lists (tenant_id, name) values ($1, 'l') returning id", tenant_id
    )
    contact_id = await admin.fetchval(
        "insert into contacts (tenant_id, list_id, phone_e164) values ($1, $2, $3) returning id",
        tenant_id, list_id, f"+8190{suffix}00",
    )
    call_id = await admin.fetchval(
        """
        insert into calls (tenant_id, contact_id, provider_call_sid, status, duration_sec)
        values ($1, $2, $3, 'COMPLETED', 12) returning id
        """,
        tenant_id, contact_id, f"CA{uuid.uuid4().hex}",
    )
    recording_sid = f"RE{uuid.uuid4().hex}"
    recording_id = await admin.fetchval(
        """
        insert into recordings
          (tenant_id, call_id, provider_recording_sid, provider_url, channels, expires_at)
        values ($1, $2, $3, $4, 2, now() + interval '365 days') returning id
        """,
        tenant_id, call_id, recording_sid,
        f"https://api.twilio.com/2010-04-01/Accounts/AC/Recordings/{recording_sid}",
    )

    yield {
        "tenant_id": tenant_id, "call_id": call_id,
        "recording_id": recording_id, "recording_sid": recording_sid,
    }

    await admin.execute("delete from tenants where id = $1", tenant_id)


# ---------------------------------------------------------------- コピー


def test_取得URLには必ずwavを付ける():
    """★ 拡張子を付けないと Twilio は mp3 で返す。mp3 だとチャンネル分離が
    壊れて話者が混ざるので、必ず .wav にする。
    """
    from app.jobs.recordings import recording_url

    base = "https://api.twilio.com/2010-04-01/Accounts/AC/Recordings/RE1"
    assert recording_url(base) == f"{base}.wav"
    assert recording_url(f"{base}.wav") == f"{base}.wav", "二重に付けない"


async def test_録音をコピーしてプロバイダ側を消す(admin, a_call, local_storage, monkeypatch):
    from app.jobs import recordings as job

    audio = build_dual_wav(tone(1000, 8000), tone(-1000, 8000))
    purged: list[str] = []

    async def fake_download(url: str) -> bytes:
        return audio

    async def fake_purge(sid: str) -> None:
        purged.append(sid)

    monkeypatch.setattr(job, "_download", fake_download)
    monkeypatch.setattr(job, "_purge_from_provider", fake_purge)

    result = await job.copy_pending(limit=10)
    assert result["copied"] >= 1

    row = await admin.fetchrow(
        "select storage_key, size_bytes, provider_purged_at, provider_url "
        "from recordings where id = $1",
        a_call["recording_id"],
    )
    assert row["storage_key"] is not None
    assert row["size_bytes"] == len(audio)
    # ★ コピーが済んでからプロバイダ側を消す。順序が逆だと、コピー失敗で録音を失う
    assert a_call["recording_sid"] in purged
    assert row["provider_purged_at"] is not None
    assert row["provider_url"] is None, "プロバイダの URL は消しておく"
    assert local_storage.backend().get(row["storage_key"]) == audio


async def test_取得に失敗したら次回に持ち越す(admin, a_call, local_storage, monkeypatch):
    """まだ Twilio 側で生成中のことがある。失敗しても状態を壊さない。"""
    from app.jobs import recordings as job

    async def fail(url: str) -> bytes:
        raise RuntimeError("404 not ready")

    monkeypatch.setattr(job, "_download", fail)
    result = await job.copy_pending(limit=10)
    assert result["failed"] >= 1

    row = await admin.fetchrow(
        "select storage_key, provider_url from recordings where id = $1", a_call["recording_id"]
    )
    assert row["storage_key"] is None
    assert row["provider_url"] is not None, "次回拾えるよう URL を残す"


async def test_コピー済みだが消し損ねたものを拾い直す(admin, a_call, local_storage, monkeypatch):
    from app.jobs import recordings as job

    await admin.execute(
        "update recordings set storage_key = 'k/v.wav' where id = $1", a_call["recording_id"]
    )

    purged: list[str] = []

    async def fake_purge(sid: str) -> None:
        purged.append(sid)

    monkeypatch.setattr(job, "_purge_from_provider", fake_purge)
    assert await job.purge_orphans(limit=10) >= 1
    assert a_call["recording_sid"] in purged


# ---------------------------------------------------------------- 文字起こし


async def _copied(admin, a_call, storage_module, audio: bytes) -> str:
    key = storage_module.storage_key(
        tenant_id=str(a_call["tenant_id"]),
        call_id=str(a_call["call_id"]),
        recording_sid=a_call["recording_sid"],
    )
    storage_module.backend().put(key, audio, content_type="audio/wav")
    await admin.execute(
        "update recordings set storage_key = $2 where id = $1", a_call["recording_id"], key
    )
    return key


async def test_全文文字起こしとメトリクスが保存される(
    admin, a_call, local_storage, monkeypatch
):
    from app.jobs import transcribe as job
    from app.realtime.batch_asr import BatchSegment

    await _copied(admin, a_call, local_storage, build_dual_wav(tone(900, 8000), tone(-900, 8000)))

    async def fake_asr(channel):
        if channel.track == "outbound":
            return [BatchSegment("outbound", 0, 3000, "お世話になっております", 0.9)]
        return [BatchSegment("inbound", 3200, 8200, "はい、どういったご用件でしょうか", 0.9)]

    monkeypatch.setattr(job, "transcribe_channel", fake_asr)

    result = await job.run_once(limit=10)
    assert result["done"] >= 1

    segments = await admin.fetch(
        "select track, text from transcript_segments where call_id = $1 and source = 'batch' "
        "order by started_ms",
        a_call["call_id"],
    )
    assert [s["track"] for s in segments] == ["outbound", "inbound"]

    metrics = await admin.fetchrow(
        "select * from call_conversation_metrics where call_id = $1", a_call["call_id"]
    )
    assert metrics["agent_talk_ms"] == 3000
    assert metrics["contact_talk_ms"] == 5000
    assert metrics["agent_turns"] == 1

    state = await admin.fetchval(
        "select state from transcription_jobs where recording_id = $1", a_call["recording_id"]
    )
    assert state == "DONE"


# ★ ジョブは再実行される。文字起こしが二重にならないこと
async def test_2回実行しても文字起こしが二重にならない(
    admin, a_call, local_storage, monkeypatch
):
    from app.jobs import transcribe as job
    from app.realtime.batch_asr import BatchSegment

    await _copied(admin, a_call, local_storage, build_dual_wav(tone(900, 8000), tone(-900, 8000)))

    async def fake_asr(channel):
        return [BatchSegment(channel.track, 0, 1000, "テスト", 0.9)]

    monkeypatch.setattr(job, "transcribe_channel", fake_asr)

    await job.process_one(a_call["recording_id"], a_call["tenant_id"])
    await job.process_one(a_call["recording_id"], a_call["tenant_id"])

    count = await admin.fetchval(
        "select count(*) from transcript_segments where call_id = $1 and source = 'batch'",
        a_call["call_id"],
    )
    assert count == 2, "2 チャンネル分の 2 行のまま（4 行に増えていない）"


async def test_リアルタイム版の文字起こしは消さない(
    admin, a_call, local_storage, monkeypatch
):
    """★ 精度はバッチが上だが、リアルタイム版は「担当者が何を見ていたか」の
    記録として要る。バッチが上書きで消してしまわないこと。
    """
    from app.jobs import transcribe as job
    from app.realtime.batch_asr import BatchSegment

    await admin.execute(
        """
        insert into transcript_segments
          (tenant_id, call_id, source, track, started_ms, ended_ms, text, expires_at)
        values ($1, $2, 'realtime', 'inbound', 0, 1000, 'リアルタイム版',
                now() + interval '365 days')
        """,
        a_call["tenant_id"], a_call["call_id"],
    )
    await _copied(admin, a_call, local_storage, build_dual_wav(tone(900, 8000), tone(-900, 8000)))

    async def fake_asr(channel):
        return [BatchSegment(channel.track, 0, 1000, "バッチ版", 0.9)]

    monkeypatch.setattr(job, "transcribe_channel", fake_asr)
    await job.process_one(a_call["recording_id"], a_call["tenant_id"])

    kept = await admin.fetchval(
        "select count(*) from transcript_segments where call_id = $1 and source = 'realtime'",
        a_call["call_id"],
    )
    assert kept == 1


# ★ 再試行しても直らないものを無限に拾い直さない
async def test_扱えない録音はSKIPPEDにして拾い直さない(
    admin, a_call, local_storage, monkeypatch
):
    from app.jobs import transcribe as job

    await _copied(admin, a_call, local_storage, b"not a wav at all")

    state = await job.process_one(a_call["recording_id"], a_call["tenant_id"])
    assert state == "SKIPPED"

    stored = await admin.fetchval(
        "select state from transcription_jobs where recording_id = $1", a_call["recording_id"]
    )
    assert stored == "SKIPPED"

    # 次回の claim で拾われないこと
    claimed = await job._claim(10)
    assert a_call["recording_id"] not in [r["recording_id"] for r in claimed]


async def test_モノラルではメトリクスを出さない(admin, a_call, local_storage, monkeypatch):
    """★ 話者が分からないのに数字を出すと、それらしく見えて誤解を招く。
    不確かな数字を出すより、出さないほうがよい。
    """
    import io
    import wave

    from app.jobs import transcribe as job
    from app.realtime.batch_asr import BatchSegment

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(tone(500, 8000))
    await _copied(admin, a_call, local_storage, buffer.getvalue())

    async def fake_asr(channel):
        return [BatchSegment(channel.track, 0, 1000, "モノラル", 0.9)]

    monkeypatch.setattr(job, "transcribe_channel", fake_asr)
    await job.process_one(a_call["recording_id"], a_call["tenant_id"])

    metrics = await admin.fetchrow(
        "select * from call_conversation_metrics where call_id = $1", a_call["call_id"]
    )
    assert metrics is None


# ---------------------------------------------------------------- 保存期間


async def test_保存期間を過ぎた録音は実体ごと消える(admin, a_call, local_storage):
    """★ 自社 DB のレコードだけ消してストレージに実体が残るのが最も多い漏れ。

    「消す仕組みを最初に作る」が原則 5 の核心なので、実際に消えることを
    確認する。ここが動かないと、消してよいか判断できないデータが
    数 TB 溜まった状態から考えることになる。
    """
    from app.jobs.maintenance import purge_recordings

    key = await _copied(admin, a_call, local_storage, b"RIFF-dummy")
    assert local_storage.backend().exists(key)

    await admin.execute(
        "update recordings set expires_at = now() - interval '1 day' where id = $1",
        a_call["recording_id"],
    )

    assert await purge_recordings() >= 1
    assert not local_storage.backend().exists(key), "ストレージの実体が残っている"

    deleted_at = await admin.fetchval(
        "select deleted_at from recordings where id = $1", a_call["recording_id"]
    )
    assert deleted_at is not None


async def test_期限内の録音は消えない(admin, a_call, local_storage):
    from app.jobs.maintenance import purge_recordings

    key = await _copied(admin, a_call, local_storage, b"RIFF-dummy")
    await purge_recordings()
    assert local_storage.backend().exists(key)
