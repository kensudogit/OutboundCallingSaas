"""設定の起動時検証を、実際に import させて確かめる。

config.py は import 時に評価されるので、同一プロセスでは「未設定で起動する」
状態を再現できない（conftest が既に値を入れている）。子プロセスで環境を
組み立て直して確かめる。

★ ここで見たいのは 2 つだけ。
    - Twilio が全部空なら起動する（縮退モード）
    - Twilio が一部だけ空なら起動しない（設定ミス）
  後者を通してしまうと、Auth Token だけ空のまま本番が上がり、
  署名検証が空鍵の HMAC になる。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]

# ★ 空文字を明示的に渡す。未指定にすると load_dotenv が server/.env を拾い、
#   開発者の手元にだけ値があって結果が変わる
BASE_ENV = {
    "APP_ENV": "test",
    "PUBLIC_BASE_URL": "https://example.test",
    "DATABASE_URL": "postgresql://app_user:app_password@localhost:5434/calling",
    "JWT_SECRET": "test-jwt-secret-do-not-use-outside-tests",
    "ASR_PROVIDER": "null",
    "TWILIO_ACCOUNT_SID": "",
    "TWILIO_AUTH_TOKEN": "",
    "TWILIO_CALLER_ID": "",
    # PUBLIC_BASE_URL="" を試す回で、実行環境の Railway 変数から補完されないように
    "RAILWAY_PUBLIC_DOMAIN": "",
    "RAILWAY_STATIC_URL": "",
}

PROBE = (
    "import json, app.config as c;"
    "print(json.dumps({'twilio': c.TWILIO_CONFIGURED}))"
)


def boot(**overrides: str) -> subprocess.CompletedProcess[str]:
    # ★ 親の環境ごと渡す。削ると APPDATA が消えて user site-packages が
    #   解決できなくなり、依存が入っていないだけの失敗になる。
    #   結果を左右する変数は BASE_ENV で明示的に上書きする
    env = {
        **os.environ,
        "PYTHONPATH": str(SERVER_DIR),
        "PYTHONIOENCODING": "utf-8",
        **BASE_ENV,
        **overrides,
    }
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=SERVER_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_Twilioが全部空でも起動する():
    """契約前でも DB・認証・画面の疎通を先に確認できるようにするため。"""
    result = boot()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"twilio": False}


def test_縮退起動は理由をログに出す():
    """「デプロイは通ったのに電話がかからない」を障害として調べ始めさせない。"""
    result = boot()
    assert "電話機能は無効で起動します" in result.stderr


def test_Twilioが揃っていれば通常起動する():
    result = boot(
        TWILIO_ACCOUNT_SID="ACtest00000000000000000000000000",
        TWILIO_AUTH_TOKEN="test-auth-token-0123456789",
        TWILIO_CALLER_ID="+815012345678",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"twilio": True}


# ★ 最も重要。Auth Token だけ空で起動できてしまうと、署名検証が空鍵になる
@pytest.mark.parametrize(
    "missing",
    ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_CALLER_ID"],
)
def test_Twilioが一部だけ空なら起動しない(missing):
    full = {
        "TWILIO_ACCOUNT_SID": "ACtest00000000000000000000000000",
        "TWILIO_AUTH_TOKEN": "test-auth-token-0123456789",
        "TWILIO_CALLER_ID": "+815012345678",
    }
    full[missing] = ""
    result = boot(**full)
    assert result.returncode != 0
    assert missing in result.stderr
    assert "中途半端" in result.stderr


def test_Twilio未設定ならPUBLIC_BASE_URLは要らない():
    """Twilio を使わないなら公開 URL は不要。これを必須のままにすると、
    ドメインを取る前の初回デプロイが通らない。"""
    result = boot(PUBLIC_BASE_URL="")
    assert result.returncode == 0, result.stderr


def test_Twilioを使うならPUBLIC_BASE_URLが要る():
    result = boot(
        PUBLIC_BASE_URL="",
        TWILIO_ACCOUNT_SID="ACtest00000000000000000000000000",
        TWILIO_AUTH_TOKEN="test-auth-token-0123456789",
        TWILIO_CALLER_ID="+815012345678",
    )
    assert result.returncode != 0
    assert "PUBLIC_BASE_URL" in result.stderr
