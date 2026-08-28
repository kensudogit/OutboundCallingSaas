"""最小限の構造化ログ。

架電システムのログには、出してはいけないものが 2 種類ある。

  1. 認証情報（Auth Token、API Key、JWT）
  2. 個人情報（電話番号、氏名、通話の文字起こし）

2 が普通の Web アプリと違う点。電話番号をそのままログに流すと、ログ基盤が
個人データの保管場所になり、保存期間もアクセス制御も設計外のところで決まって
しまう。番号は末尾 4 桁だけ残す形にマスクして、突き合わせは call_id で行う。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

REDACT_KEYS = frozenset(
    {
        "auth_token", "authToken", "api_key", "apiKey", "api_key_secret",
        "password", "password_hash", "passwordHash",
        "token", "jwt", "authorization", "access_token", "accessToken",
        "signature", "x_twilio_signature",
        "asr_api_key", "llm_api_key",
    }
)

# 末尾 4 桁だけ残す。完全に消すと、苦情の問い合わせで通話を特定できなくなる
MASK_KEYS = frozenset(
    {"phone", "phone_e164", "to", "from", "from_", "caller_id", "to_number", "from_number"}
)

# 文字起こし・会話内容は原則ログに出さない
DROP_KEYS = frozenset({"transcript", "text", "suggestion", "utterance", "note"})

_E164 = re.compile(r"\+\d{6,15}")


def mask_phone(value: str) -> str:
    """+819012345678 → +81******5678"""
    if len(value) <= 6:
        return "***"
    return value[:3] + "*" * (len(value) - 7) + value[-4:]


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 4 or value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        # キー名に頼らず、値そのものが E.164 に見えたらマスクする。
        # 「message に番号を埋め込んでしまった」経路を拾うための保険
        return _E164.sub(lambda m: mask_phone(m.group()), value)
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth + 1) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in REDACT_KEYS:
                out[k] = "[REDACTED]"
            elif k in DROP_KEYS:
                out[k] = f"[{len(str(v))} chars omitted]"
            elif k in MASK_KEYS and isinstance(v, str):
                out[k] = mask_phone(v)
            else:
                out[k] = _redact(v, depth + 1)
        return out
    return str(value)


Level = Literal["debug", "info", "warn", "error"]


def _emit(level: Level, message: str, context: dict[str, Any] | None) -> None:
    line: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "msg": message,
    }
    if context:
        line.update(_redact(context))  # type: ignore[arg-type]
    print(json.dumps(line, ensure_ascii=False, default=str), flush=True)


class Logger:
    def debug(self, message: str, **context: Any) -> None:
        _emit("debug", message, context)

    def info(self, message: str, **context: Any) -> None:
        _emit("info", message, context)

    def warn(self, message: str, **context: Any) -> None:
        _emit("warn", message, context)

    def error(self, message: str, **context: Any) -> None:
        _emit("error", message, context)


logger = Logger()
