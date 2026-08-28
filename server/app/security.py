"""パスワードハッシュ。

scrypt で "salt:hash" の形に保存する。bcrypt / argon2 のネイティブ依存を
増やさないため node:crypto 相当の標準ライブラリを使う。既存の認証基盤が
あるならそちらに置き換える。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_KEYLEN = 64
_N, _R, _P = 2**14, 8, 1


def hash_password(plain: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        plain.encode("utf-8"), salt=salt.encode("utf-8"), n=_N, r=_R, p=_P, dklen=_KEYLEN
    )
    return f"{salt}:{digest.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """壊れた保存値でも例外にせず False を返す。

    移行途中の行や手で入れた行が来ても、ログインが 500 で落ちないようにする。
    """
    try:
        salt, expected = stored.split(":", 1)
    except ValueError:
        return False
    if not salt or not expected:
        return False

    candidate = hashlib.scrypt(
        plain.encode("utf-8"), salt=salt.encode("utf-8"), n=_N, r=_R, p=_P, dklen=_KEYLEN
    )
    try:
        expected_bytes = bytes.fromhex(expected)
    except ValueError:
        return False
    # 単純な == は使わない（タイミング攻撃）
    return hmac.compare_digest(candidate, expected_bytes)
