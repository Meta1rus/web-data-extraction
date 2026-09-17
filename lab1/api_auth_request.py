"""
Лабораторная 1, задание 2.
Запрос к открытому API, требующему авторизацию, и сохранение ответа в файл.

Каталог ProgrammableWeb (programmableweb.com) закрыт в 2023 году, поэтому API
выбрано из аналогичного каталога открытых API: https://github.com/public-apis/public-apis

Выбранное API: NASA Open APIs — https://api.nasa.gov
Тип авторизации: API key (ключ передаётся в query-параметре api_key).
Ключ выдаётся бесплатно после регистрации на api.nasa.gov; для тестов NASA
предоставляет публичный ключ DEMO_KEY с уменьшенным лимитом (30 запросов/час).

Скрипт:
  1) делает запрос БЕЗ ключа, чтобы показать, что API действительно требует авторизацию;
  2) делает запрос С ключом (проходит авторизацию);
  3) сохраняет оба ответа в JSON-файлы.

Запуск: python3 api_auth_request.py
Свой ключ можно передать через переменную окружения NASA_API_KEY.
"""
import json
import os

import requests

BASE_URL = "https://api.nasa.gov"
API_KEY = os.getenv("NASA_API_KEY", "hmtYnLTLanrRLFnolKLnqISkaoZCIhus1j6ziJgv")

SESSION = requests.Session()
SESSION.trust_env = False  # Игнорировать системные настройки прокси


def save(data: dict, filename: str) -> None:
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  -> сохранено в {filename}")


def request(endpoint: str, params: dict, with_auth: bool) -> requests.Response:
    params = dict(params)
    if with_auth:
        params["api_key"] = API_KEY          # авторизация: API key в query-строке
    resp = SESSION.get(f"{BASE_URL}{endpoint}", params=params, timeout=15)
    print(f"GET {resp.url.replace(API_KEY, '***')}")
    print(f"  HTTP {resp.status_code}; остаток лимита: "
          f"{resp.headers.get('X-RateLimit-Remaining', 'n/a')}")
    return resp


def main() -> None:
    # 1. Без авторизации — ожидаем ошибку 403 (API_KEY_MISSING)
    print("1) Запрос без ключа:")
    resp = request("/planetary/apod", {}, with_auth=False)
    save(resp.json(), "nasa_apod_no_auth.json")

    # 2. С авторизацией — Astronomy Picture of the Day
    print("\n2) Запрос с ключом (APOD, астрономическая картинка дня):")
    resp = request("/planetary/apod", {}, with_auth=True)
    resp.raise_for_status()
    apod = resp.json()
    save(apod, "nasa_apod_response.json")
    print(f"  Дата: {apod.get('date')}, заголовок: {apod.get('title')}")

    # 3. Ещё один авторизованный запрос — NeoWs: околоземные астероиды за день
    print("\n3) Запрос с ключом (NeoWs, околоземные астероиды за дату):")
    resp = request("/neo/rest/v1/feed",
                   {"start_date": "2026-09-14", "end_date": "2026-09-14"}, with_auth=True)
    resp.raise_for_status()
    neo = resp.json()
    save(neo, "nasa_neo_response.json")
    print(f"  Астероидов в выборке: {neo.get('element_count')}")

if __name__ == "__main__":
    main()
