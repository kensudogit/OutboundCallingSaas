"""録音の扱い — チャンネル分離と保管。

★ デュアルチャンネルの左右を取り違えると、担当者と相手が入れ替わった
  文字起こしになり、トーク比率も逆になる。しかも「それらしい数字」が
  出るので気付きにくい。ここは固定しておく。

★ 保管は「署名付き URL でしか取れない」ことを確認する。ローカル構成でも
  同じ形にしてあるので、本番でだけ守られている状態を避けられる。
"""

from __future__ import annotations

import struct
import time

import pytest

from app.domain.wav import (
    UnsupportedAudio,
    build_dual_wav,
    split_channels,
)


def tone(value: int, samples: int) -> bytes:
    """一定振幅の PCM16。左右を見分けるために違う値を入れる。"""
    return struct.pack(f"<{samples}h", *([value] * samples))


# ---------------------------------------------------------------- チャンネル分離


def test_デュアルチャンネルは左が担当者_右が相手():
    """★ Twilio の record-from-answer-dual は左が発信側（担当者）。

    ここを取り違えると、文字起こしの話者が全部逆になる。
    """
    wav = build_dual_wav(tone(1000, 800), tone(-1000, 800))
    channels = split_channels(wav)

    assert [c.track for c in channels] == ["outbound", "inbound"]
    assert struct.unpack("<h", channels[0].pcm[:2])[0] == 1000    # 左 = 担当者
    assert struct.unpack("<h", channels[1].pcm[:2])[0] == -1000   # 右 = 相手


def test_分離後の長さが元の半分になる():
    wav = build_dual_wav(tone(500, 4000), tone(-500, 4000))
    for channel in split_channels(wav):
        assert len(channel.pcm) == 8000        # 4000 サンプル × 2 バイト
        assert channel.duration_ms == 500      # 8kHz で 4000 サンプル


def test_モノラルは話者不明として1本返す():
    """★ 話者が分からないので会話メトリクスは出さない。
    ただし文字起こしはできる——できることはやる。
    """
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(tone(100, 1000))

    channels = split_channels(buffer.getvalue())
    assert len(channels) == 1
    assert channels[0].track == "unknown"


def test_WAVでないデータは明示的に落とす():
    """再試行しても直らないので、握り潰さず SKIPPED にできるようにする。"""
    with pytest.raises(UnsupportedAudio):
        split_channels(b"this is not a wav file")


def test_往復しても中身が変わらない():
    """分離 → to_wav → 再分離で同じ PCM に戻ること。"""
    original = tone(1234, 2000)
    wav = build_dual_wav(original, tone(-1234, 2000))

    left = split_channels(wav)[0]
    restored = split_channels(left.to_wav())[0]

    assert restored.pcm == original
    assert restored.sample_rate == left.sample_rate


# ---------------------------------------------------------------- 保管


@pytest.fixture
def local_storage(tmp_path, monkeypatch):
    from app import storage

    monkeypatch.setattr(storage, "BACKEND", "local")
    monkeypatch.setattr(storage, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(storage, "URL_SECRET", "test-secret")
    storage.reset_backend()
    yield storage
    storage.reset_backend()


def test_保存して読み出せる(local_storage):
    local_storage.backend().put("t1/c1/rec.wav", b"audio-bytes", content_type="audio/wav")
    assert local_storage.backend().get("t1/c1/rec.wav") == b"audio-bytes"
    assert local_storage.backend().exists("t1/c1/rec.wav")


def test_削除できる(local_storage):
    local_storage.backend().put("t1/c1/rec.wav", b"x", content_type="audio/wav")
    local_storage.backend().delete("t1/c1/rec.wav")
    assert not local_storage.backend().exists("t1/c1/rec.wav")


def test_存在しないキーの削除は例外にしない(local_storage):
    """保存期間の削除ジョブが、既に消えたファイルで止まらないようにする。"""
    local_storage.backend().delete("t1/c1/missing.wav")


def test_署名付きURLは正しい署名でだけ通る(local_storage):
    backend = local_storage.backend()
    url = backend.presigned_url("t1/c1/rec.wav", expires_in=300)

    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    assert backend.verify(query["key"][0], int(query["expires"][0]), query["sig"][0])
    assert not backend.verify(query["key"][0], int(query["expires"][0]), "wrong-signature")


def test_期限切れの署名は通らない(local_storage):
    backend = local_storage.backend()
    expired = int(time.time()) - 10
    assert not backend.verify("t1/c1/rec.wav", expired, backend.sign("t1/c1/rec.wav", expired))


def test_別のキーの署名は使い回せない(local_storage):
    """1 つの録音の URL を持っている人が、他の録音を取れないこと。"""
    backend = local_storage.backend()
    expires = int(time.time()) + 300
    signature = backend.sign("t1/c1/mine.wav", expires)
    assert not backend.verify("t2/c9/someone-else.wav", expires, signature)


# ★ key は DB 経由で来る値でもある。パス traversal で他所のファイルを
#   読み書きされないことを確認する
@pytest.mark.parametrize("bad_key", ["../../etc/passwd", "t1/../../secret.wav"])
def test_ディレクトリtraversalを拒否する(local_storage, bad_key):
    with pytest.raises(ValueError):
        local_storage.backend().get(bad_key)


def test_保存キーはテナントごとに分かれる(local_storage):
    """バケットポリシーやライフサイクルをテナント単位で当てられるようにする。"""
    key = local_storage.storage_key(tenant_id="t-1", call_id="c-2", recording_sid="RE3")
    assert key.startswith("t-1/")
    assert key == "t-1/c-2/RE3.wav"
