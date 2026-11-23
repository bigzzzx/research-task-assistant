"""Local account authentication with expiring, server-side sessions."""

import asyncio
from dataclasses import dataclass
from hashlib import scrypt, sha256
from hmac import compare_digest
import re
from secrets import token_bytes, token_urlsafe
import sqlite3
from time import time
import unicodedata

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/auth")
COOKIE = "research_session"
SESSION_LIFETIME = 7 * 24 * 60 * 60
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
USERNAME = re.compile(r"[\w.-]{3,32}")


@dataclass(frozen=True)
class Account:
    id: str
    username: str
    token_hash: str
    expires_at: int

    def public(self) -> dict:
        return {"id": self.id, "username": self.username}


class Credentials(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(min_length=1, max_length=128)


def username_identity(value: str) -> tuple[str, str]:
    name = unicodedata.normalize("NFC", value.strip())
    if not USERNAME.fullmatch(name) or not any(char.isalnum() for char in name):
        raise ValueError("用户名须为 3–32 个字符，可用字母、汉字、数字、下划线、点或连字符。")
    return name, unicodedata.normalize("NFC", name.casefold())


def password_digest(password: bytes, salt: bytes) -> bytes:
    return scrypt(password, salt=salt, n=2**15, r=8, p=3, maxmem=64 * 1024**2, dklen=32)


def session_token(request: Request) -> str | None:
    token = request.cookies.get(COOKIE, "")
    return sha256(token.encode("ascii")).hexdigest() if TOKEN.fullmatch(token) else None


async def current_account(request: Request) -> Account:
    token_hash = session_token(request)
    session = request.app.state.store.session(token_hash) if token_hash else None
    if not session:
        raise HTTPException(401, "请登录后继续。")
    expected_account = request.headers.get("X-Workspace-Account") or request.query_params.get("account")
    if expected_account and expected_account != session["id"]:
        raise HTTPException(401, "当前账号已切换，请刷新工作区。")
    return Account(session["id"], session["username"], token_hash, session["expires_at"])


def revoke_session(request: Request) -> None:
    token_hash = session_token(request)
    if token_hash:
        request.app.state.store.delete_session(token_hash)
        request.app.state.events.disconnect(token_hash)


def start_session(request: Request, response: Response, user: dict) -> dict:
    revoke_session(request)
    token = token_urlsafe(32)
    request.app.state.store.create_session(
        user["id"], sha256(token.encode("ascii")).hexdigest(), int(time()) + SESSION_LIFETIME,
    )
    response.set_cookie(
        COOKIE, token, max_age=SESSION_LIFETIME, httponly=True,
        samesite="strict", secure=request.url.scheme == "https",
    )
    return {"id": user["id"], "username": user["username"]}


def credentials(body: Credentials) -> tuple[str, str, bytes]:
    try:
        name, key = username_identity(body.username)
        return name, key, body.password.encode("utf-8")
    except UnicodeError:
        raise HTTPException(422, "密码包含无法读取的字符。") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/register", status_code=201)
async def register(body: Credentials, request: Request, response: Response):
    name, key, password = credentials(body)
    if len(body.password) < 15:
        raise HTTPException(422, "请设置至少 15 个字符的密码。")

    salt = token_bytes(16)
    async with request.app.state.password_slots:
        digest = await asyncio.to_thread(password_digest, password, salt)
    try:
        encoded = "$".join(("scrypt1", salt.hex(), digest.hex()))
        user = request.app.state.store.create_user(name, key, encoded)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "此用户名已注册。") from None
    return start_session(request, response, user)


@router.post("/login")
async def login(body: Credentials, request: Request, response: Response):
    _, key, password = credentials(body)
    user = request.app.state.store.user_by_name(key)
    _, salt, expected = user["password_hash"].split("$") if user else ("", "00" * 16, "00" * 32)
    async with request.app.state.password_slots:
        digest = await asyncio.to_thread(password_digest, password, bytes.fromhex(salt))
    if not compare_digest(digest, bytes.fromhex(expected)) or not user:
        raise HTTPException(401, "用户名或密码不正确。")
    return start_session(request, response, user)


@router.get("/me")
async def me(request: Request):
    return (await current_account(request)).public()


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    revoke_session(request)
    response.delete_cookie(COOKIE, httponly=True, samesite="strict", secure=request.url.scheme == "https")
