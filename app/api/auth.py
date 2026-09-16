"""Проверка подлинности данных Telegram Mini App.

Telegram передаёт в WebApp строку initData, подписанную ключом бота.
Проверка подписи — единственное, что отделяет приватные сигналы заказчика
от любого, кто узнает адрес приложения. Поэтому она обязательна и
выполняется на каждый запрос к API.

Алгоритм описан в документации Telegram:
  secret = HMAC_SHA256(key="WebAppData", msg=bot_token)
  hash   = HMAC_SHA256(key=secret, msg=data_check_string)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from app.config import settings

log = logging.getLogger("api.auth")

# Подпись считается устаревшей через сутки — защита от переигрывания
MAX_AGE_SECONDS = 86400


def verify_init_data(init_data: str, bot_token: str) -> dict | None:
    """Проверяет подпись и возвращает разобранные поля, либо None."""
    if not init_data or not bot_token:
        return None

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(
        f"{key}={pairs[key]}" for key in sorted(pairs)
    )
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256
    ).digest()
    computed = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        return None

    auth_date = pairs.get("auth_date")
    if auth_date:
        try:
            if time.time() - int(auth_date) > MAX_AGE_SECONDS:
                log.warning("initData просрочена")
                return None
        except ValueError:
            return None

    user_raw = pairs.get("user")
    if user_raw:
        try:
            pairs["user"] = json.loads(user_raw)
        except json.JSONDecodeError:
            pairs["user"] = {}
    return pairs


def current_user_id(auth: dict) -> int | None:
    """Кто прислал запрос. В режиме разработки — первый из списка."""
    user = (auth or {}).get("user") or {}
    uid = user.get("id")
    if uid:
        return int(uid)
    if auth.get("dev"):
        owners = settings.owner_id_list
        return owners[0] if owners else None
    return None


async def require_owner(
    x_telegram_init_data: str = Header(default=""),
) -> dict:
    """Зависимость FastAPI: пускает только владельца бота."""
    if settings.webapp_dev_mode:
        return {"user": {"id": 0, "first_name": "dev"}, "dev": True}

    data = verify_init_data(x_telegram_init_data, settings.bot_token)
    if data is None:
        raise HTTPException(status_code=401, detail="Подпись Telegram не подтверждена")

    user = data.get("user") or {}
    user_id = user.get("id")
    owners = settings.owner_id_list
    if owners and user_id not in owners:
        raise HTTPException(status_code=403, detail="Доступ только для владельца")

    return data
