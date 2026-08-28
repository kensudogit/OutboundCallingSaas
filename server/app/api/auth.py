"""認証（デモ用の最小実装）。

既存の認証基盤があるなら、この module だけを差し替えれば他は変更不要。
架電側が必要としているのは「req の user.tenant_id と user.id が信頼できること」だけ。

★ tenant_id が信頼できないと RLS の前提が崩れる。JWT の payload をそのまま
  信じる実装なので、署名検証を外す・秘密鍵を共有するといった変更は
  他テナントのデータを読ませることに直結する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..config import JWT_SECRET, JWT_TTL_MINUTES
from ..db.engine import admin_tx, tenant_tx
from ..security import verify_password

router = APIRouter(prefix="/api", tags=["auth"])


@dataclass(frozen=True)
class AuthUser:
    id: str
    tenant_id: str
    email: str
    role: str


class LoginRequest(BaseModel):
    email: str
    password: str


def issue_token(user: AuthUser) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user.id,
            "tid": user.tenant_id,
            "email": user.email,
            "role": user.role,
            "iat": now,
            "exp": now + timedelta(minutes=JWT_TTL_MINUTES),
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def decode_token(token: str) -> AuthUser:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="unauthorized") from exc

    if not payload.get("sub") or not payload.get("tid"):
        raise HTTPException(status_code=401, detail="unauthorized")

    return AuthUser(
        id=payload["sub"],
        tenant_id=payload["tid"],
        email=payload.get("email", ""),
        role=payload.get("role", "agent"),
    )


async def current_user(request: Request) -> AuthUser:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    return decode_token(header[7:])


async def current_manager(user: AuthUser = Depends(current_user)) -> AuthUser:
    """リストの一括操作など、事故が大きい操作に使う。"""
    if user.role not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="forbidden")
    return user


@router.post("/auth/login")
async def login(body: LoginRequest):
    # ログイン前はテナントが未確定なので RLS を迂回して引く。
    # ここが admin_tx を使う数少ない正当な箇所
    async with admin_tx() as conn:
        row = await conn.fetchrow(
            "select * from users where email = $1 and is_active", body.email
        )

    # ユーザーの存在有無を応答で区別しない
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid_credentials")

    user = AuthUser(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        email=row["email"],
        role=row["role"],
    )
    return {
        "token": issue_token(user),
        "user": {"id": user.id, "email": user.email, "displayName": row["display_name"],
                 "role": user.role},
    }


@router.get("/auth/me")
async def me(user: AuthUser = Depends(current_user)):
    async with tenant_tx(user.tenant_id) as conn:
        row = await conn.fetchrow("select * from users where id = $1", user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "displayName": row["display_name"],
        "role": row["role"],
    }
