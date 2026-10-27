# app/services/dadata.py
from __future__ import annotations
import os
import requests
from typing import Optional, Dict, Any
from app.core.config import EXTERNAL_INTEGRATIONS_DISABLED

DADATA_API_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
DADATA_API_TOKEN = os.getenv("DADATA_API_TOKEN", os.getenv("DADATA_TOKEN", ""))

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Token {DADATA_API_TOKEN}",
}

def get_company_by_inn(inn: str, timeout: float = 5.0) -> Dict[str, Any]:
    """
    Возвращает нормализованные поля для нашей регистрации:
    success, short_name, full_name, inn, ogrn, ceo_name, kpp, status,
    legal_address, actual_address, email, phone, raw
    """
    if EXTERNAL_INTEGRATIONS_DISABLED:
        return {"success": False, "error": "Внешние интеграции отключены в этом экземпляре"}
    inn = (inn or "").strip()
    if not inn.isdigit() or len(inn) not in (10, 12):
        return {"success": False, "error": "ИНН должен состоять из 10 или 12 цифр"}

    if not DADATA_API_TOKEN:
        return {"success": False, "error": "DADATA_API_TOKEN не задан в окружении"}

    try:
        resp = requests.post(DADATA_API_URL, headers=HEADERS, json={"query": inn}, timeout=timeout)
        resp.raise_for_status()
        js = resp.json() or {}
        sugg = (js.get("suggestions") or [])
        if not sugg:
            return {"success": False, "error": "Организация/ИП не найдены по ИНН"}

        item = sugg[0]
        data = item.get("data", {}) or {}
        # Руководитель / ФИО
        ceo_name = (
            (data.get("management") or {}).get("name")
            or (data.get("fio") or {}).get("full")
            or " ".join(filter(None, [
                (data.get("fio") or {}).get("surname"),
                (data.get("fio") or {}).get("name"),
                (data.get("fio") or {}).get("patronymic"),
            ]))
        )

        emails = data.get("emails") or []
        phones = data.get("phones") or []
        address = data.get("address") or {}

        status = (data.get("state") or {}).get("status")  # ACTIVE / LIQUIDATING / LIQUIDATED / ...
        # Мы можем пометить в своей БД как 'ACTIVE'/'INACTIVE', но сохраним оригинал в raw при желании

        return {
            "success": True,
            "short_name": item.get("value"),
            "full_name": item.get("unrestricted_value"),
            "inn": data.get("inn"),
            "ogrn": data.get("ogrn") or data.get("ogrnip"),
            "kpp": data.get("kpp"),
            "ceo_name": ceo_name,
            "status": status or "ACTIVE",
            "legal_address": address.get("value"),
            "actual_address": address.get("unrestricted_value"),
            "email": emails[0] if emails else None,
            "phone": phones[0] if phones else None,
            "raw": item,  # на всякий случай
        }
    except requests.RequestException as e:
        return {"success": False, "error": f"Dadata request error: {e}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {e}"}

# совместимость со старым именем
def get_party_by_inn(inn: str, timeout: float = 5.0):
    """
    Back-compat wrapper: раньше код вызывал get_party_by_inn,
    теперь ядро использует get_company_by_inn. Возвращаем тот же словарь.
    """
    return get_company_by_inn(inn, timeout=timeout)
