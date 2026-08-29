"""環境変数の一元管理と起動時検証（Phase 1）。

★ 検証は「最初の 1 件で投げる」のではなく、全部集めてから一度に投げる。
  コンテナデプロイでは 1 件直すたびに再デプロイが必要になるため、
  6 個足りなければ 6 回デプロイし直すことになってしまう。

★ 架電特有で落としやすいのは PUBLIC_BASE_URL の形。Twilio に登録した URL と
  1 文字でも違うと署名が一致せず、Webhook が全件 403 になる。ここは値の
  存在だけでなく形まで見る。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()

_problems: list[str] = []


def _required(name: str, hint: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        _problems.append(f"{name} が設定されていません — {hint}")
        return ""
    return value


def _railway_https_origin() -> str:
    """Railway が付与する公開ホスト名から origin を組み立てる。

    PUBLIC_BASE_URL を先に要求すると、初回デプロイで URL がまだ無い／Variables
    に書き忘れている、という鶏卵になる。ドメインがあれば https で補う。
    Twilio に登録する URL と一致させる必要は、補完後も変わらない。
    """
    domain = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
    if domain:
        return f"https://{domain}"
    url = (os.environ.get("RAILWAY_STATIC_URL") or "").strip().rstrip("/")
    if url:
        return url if url.startswith("https://") else f"https://{url}"
    return ""


def _optional(name: str, fallback: str) -> str:
    value = (os.environ.get(name) or "").strip()
    return value or fallback


def _int(name: str, fallback: int) -> int:
    raw = _optional(name, str(fallback))
    try:
        return int(raw)
    except ValueError:
        _problems.append(f"{name} は整数である必要があります。実際の値: {raw!r}")
        return fallback


def _bool(name: str, fallback: bool) -> bool:
    return _optional(name, "true" if fallback else "false").lower() == "true"


def _time(name: str, fallback: str) -> time:
    raw = _optional(name, fallback)
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        _problems.append(f"{name} は HH:MM 形式。実際の値: {raw!r}")
        return time(9, 0)


# ---------------------------------------------------------------- 読み取り

APP_ENV = _optional("APP_ENV", "development")
PORT = _int("PORT", 8000)
MEDIA_PORT = _int("MEDIA_PORT", 8001)

# 必須かどうかは Twilio を使うかで決まるので、判定は下の検証セクションで行う
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").strip() or _railway_https_origin()
PUBLIC_WSS_URL = _optional("PUBLIC_WSS_URL", PUBLIC_BASE_URL.replace("https://", "wss://"))

DATABASE_URL = _required("DATABASE_URL", "アプリ用。RLS が効くロールで接続する")
DATABASE_MIGRATOR_URL = _optional("DATABASE_MIGRATOR_URL", "")
REDIS_URL = _optional("REDIS_URL", "redis://localhost:6381/0")

JWT_SECRET = _required("JWT_SECRET", "openssl rand -hex 32")
JWT_TTL_MINUTES = _int("JWT_TTL_MINUTES", 120)

# ★ この 3 つが揃って初めて「電話がかけられる」状態になる。揃っていなければ
#   起動は通し、電話まわりだけを閉じる（TWILIO_CONFIGURED = False）。
#   Twilio の契約前でも DB・認証・画面の疎通を先に確認できるようにするため。
#
# ★ ただし「1 個だけ入っている」は事故なので起動を止める。特に AUTH_TOKEN だけ
#   空だと、署名検証が空鍵の HMAC になり、誰でも Webhook を偽造できてしまう。
#   全部空（未契約）と、一部だけ空（設定ミス）は、区別して扱う。
TWILIO_ACCOUNT_SID = _optional("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = _optional("TWILIO_AUTH_TOKEN", "")
TWILIO_CALLER_ID = _optional("TWILIO_CALLER_ID", "")

_TWILIO_CORE_HINTS = {
    "TWILIO_ACCOUNT_SID": "Console のダッシュボード。AC で始まる",
    "TWILIO_AUTH_TOKEN": "★ 署名検証に使う。API Key Secret とは別物",
    "TWILIO_CALLER_ID": "購入済みまたは検証済みの発信者番号（E.164）",
}
_twilio_missing = [
    name
    for name, value in (
        ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
        ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
        ("TWILIO_CALLER_ID", TWILIO_CALLER_ID),
    )
    if not value
]
TWILIO_CONFIGURED = not _twilio_missing

if _twilio_missing and len(_twilio_missing) < len(_TWILIO_CORE_HINTS):
    _problems.extend(
        f"{name} が設定されていません — {_TWILIO_CORE_HINTS[name]}"
        for name in _twilio_missing
    )
    _problems.append(
        "Twilio の設定が中途半端です。3 つ全部を設定するか、3 つ全部を空にして"
        "（電話機能を無効にして）ください"
    )

TWILIO_API_KEY_SID = _optional("TWILIO_API_KEY_SID", "")
TWILIO_API_KEY_SECRET = _optional("TWILIO_API_KEY_SECRET", "")
TWILIO_TWIML_APP_SID = _optional("TWILIO_TWIML_APP_SID", "")
TWILIO_MACHINE_DETECTION = _optional("TWILIO_MACHINE_DETECTION", "DetectMessageEnd")

CALLING_TIMEZONE = _optional("CALLING_TIMEZONE", "Asia/Tokyo")
CALLING_HOURS_START = _time("CALLING_HOURS_START", "09:00")
CALLING_HOURS_END = _time("CALLING_HOURS_END", "20:00")
CALLING_EXCLUDE_HOLIDAYS = _bool("CALLING_EXCLUDE_HOLIDAYS", True)
MAX_ATTEMPTS_PER_DAY = _int("MAX_ATTEMPTS_PER_DAY", 3)
MAX_ATTEMPTS_TOTAL = _int("MAX_ATTEMPTS_TOTAL", 8)
RESERVATION_TTL_SECONDS = _int("RESERVATION_TTL_SECONDS", 600)
# ★ 結果登録の直後に次を鳴らすと担当者が息を継げない。間隔を挟み、
#   その間はキャンセルできるようにする
AUTO_DIAL_DELAY_SEC = _int("AUTO_DIAL_DELAY_SEC", 3)
AGENT_HEARTBEAT_TIMEOUT_SECONDS = _int("AGENT_HEARTBEAT_TIMEOUT_SECONDS", 60)
RECENT_CALL_WINDOW_SECONDS = _int("RECENT_CALL_WINDOW_SECONDS", 60)

# 障害時に発信だけを止める。アプリ全体を巻き戻さずに済む
DIALING_ENABLED = _bool("DIALING_ENABLED", True)

ASR_PROVIDER = _optional("ASR_PROVIDER", "null")
ASR_API_KEY = _optional("ASR_API_KEY", "")
ASR_LANGUAGE = _optional("ASR_LANGUAGE", "ja-JP")
ASR_SAMPLE_RATE = _int("ASR_SAMPLE_RATE", 8000)
SILENCE_THRESHOLD_MS = _int("SILENCE_THRESHOLD_MS", 700)
MIN_UTTERANCE_MS = _int("MIN_UTTERANCE_MS", 800)

LLM_API_KEY = _optional("LLM_API_KEY", "")
LLM_MODEL = _optional("LLM_MODEL", "claude-sonnet-5")
LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 120)

RECORDING_RETENTION_DAYS = _int("RECORDING_RETENTION_DAYS", 365)

CORS_ORIGIN = _optional("CORS_ORIGIN", "http://localhost:3000")

# ---------------------------------------------------------------- 値の検証

if not PUBLIC_BASE_URL and TWILIO_CONFIGURED:
    # Twilio を使わないなら公開 URL は要らない。使うなら、これが無いと
    # コールバック先を組み立てられず、署名検証の対象 URL も決まらない
    _problems.append(
        "PUBLIC_BASE_URL が設定されていません — Twilio から届く公開 URL。"
        "Railway なら Generate Domain 後に RAILWAY_PUBLIC_DOMAIN から補完される"
    )

if PUBLIC_BASE_URL:
    if not PUBLIC_BASE_URL.startswith("https://") and APP_ENV != "development":
        _problems.append(
            f"PUBLIC_BASE_URL は https:// で始まる必要があります（Twilio の要求）。"
            f"実際の値: {PUBLIC_BASE_URL}"
        )
    if PUBLIC_BASE_URL.endswith("/"):
        _problems.append(
            "PUBLIC_BASE_URL の末尾にスラッシュがあります。"
            "署名対象の URL がずれ、Webhook が全件 403 になります"
        )

if TWILIO_ACCOUNT_SID and not TWILIO_ACCOUNT_SID.startswith("AC"):
    _problems.append(
        f'TWILIO_ACCOUNT_SID は "AC" で始まります。API Key SID（SK...）と'
        f"取り違えていませんか。実際の値の先頭: {TWILIO_ACCOUNT_SID[:8]}…"
    )

if TWILIO_AUTH_TOKEN and TWILIO_AUTH_TOKEN == TWILIO_API_KEY_SECRET:
    _problems.append(
        "TWILIO_AUTH_TOKEN と TWILIO_API_KEY_SECRET が同じ値です（別物です）。"
        "署名検証には Auth Token を使います"
    )

if TWILIO_CALLER_ID and not TWILIO_CALLER_ID.startswith("+"):
    _problems.append(
        f"TWILIO_CALLER_ID は E.164 形式（+81…）にしてください。実際の値: {TWILIO_CALLER_ID}"
    )

try:
    ZoneInfo(CALLING_TIMEZONE)
except ZoneInfoNotFoundError:
    _problems.append(f"CALLING_TIMEZONE が不正です: {CALLING_TIMEZONE}")

if CALLING_HOURS_START >= CALLING_HOURS_END:
    _problems.append(
        f"CALLING_HOURS_START({CALLING_HOURS_START}) が "
        f"CALLING_HOURS_END({CALLING_HOURS_END}) 以降になっています"
    )

if ASR_PROVIDER not in ("null", "deepgram", "google", "azure"):
    _problems.append(f"ASR_PROVIDER は null / deepgram / google / azure のいずれか: {ASR_PROVIDER}")
if ASR_PROVIDER != "null" and not ASR_API_KEY:
    _problems.append(f"ASR_PROVIDER={ASR_PROVIDER} ですが ASR_API_KEY が空です")

# ---------------------------------------------------------------- 報告

if _problems:
    lines = [
        "",
        "=" * 74,
        f" 起動できません: 設定に {len(_problems)} 件の問題があります",
        "=" * 74,
        "",
        *(f"  - {p}" for p in _problems),
        "",
        "ローカル開発 : server/.env.example をコピーして server/.env を作り、値を入れる",
        "コンテナ/PaaS: 環境変数として設定する（.env ファイルは読まれない）",
        "",
        "★ PUBLIC_BASE_URL は Twilio Console に登録した URL と 1 文字も違ってはいけません。",
        "  トンネルの URL が変わったら、この変数と Twilio 側の両方を更新してください。",
        "  片方だけだと署名が一致せず、Webhook が全件 403 になります。",
        "=" * 74,
        "",
    ]
    # 一覧は stderr に直接出す。例外の message に入れるとスタックトレースに埋もれる
    # ただし Railway などはトレースだけを切り出して「上のログ」が見えなくなるので、
    # 問題文も例外メッセージに含める
    print("\n".join(lines), file=sys.stderr)
    raise RuntimeError(
        f"設定に {len(_problems)} 件の問題があります: " + " / ".join(_problems)
    )

if not TWILIO_CONFIGURED:
    # ★ 黙って縮退しない。「デプロイは成功したのに電話がかからない」を
    #   障害として調べ始める前に、ログの先頭で理由が分かるようにする
    print(
        "\n".join(
            [
                "",
                "=" * 74,
                " 電話機能は無効で起動します（Twilio 未設定）",
                "=" * 74,
                "",
                "  未設定: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_CALLER_ID",
                "",
                "  この状態での挙動:",
                "    - 発信は関門で telephony_unconfigured として拒否されます",
                "    - /voice/* の Webhook は 503 を返します（空鍵で署名検証はしません）",
                "    - DB・認証・画面・統計はそのまま使えます",
                "",
                "  3 つを設定して再起動すると、自動的に通常動作へ戻ります。",
                "=" * 74,
                "",
            ]
        ),
        file=sys.stderr,
    )


@dataclass(frozen=True)
class CallingWindowDefaults:
    """テナント設定の既定値。実際の値は tenants テーブルが持つ。"""

    timezone: str = CALLING_TIMEZONE
    start: time = CALLING_HOURS_START
    end: time = CALLING_HOURS_END
    exclude_holidays: bool = CALLING_EXCLUDE_HOLIDAYS
    weekdays: frozenset[int] = field(default_factory=lambda: frozenset({0, 1, 2, 3, 4}))
    max_attempts_per_day: int = MAX_ATTEMPTS_PER_DAY
    max_attempts_total: int = MAX_ATTEMPTS_TOTAL


CALLING_DEFAULTS = CallingWindowDefaults()


def public_url(path: str) -> str:
    """署名検証と Twilio へ渡す URL を組み立てる唯一の関数。

    ★ request.url から組み立てない。リバースプロキシの後ろでは http:// や
      内部ホスト名になり、Twilio が署名に使った公開 URL と一致しなくなる。
    """
    return f"{PUBLIC_BASE_URL.rstrip('/')}{path}"


def public_wss(path: str) -> str:
    return f"{PUBLIC_WSS_URL.rstrip('/')}{path}"
