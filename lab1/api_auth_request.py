import json
import os

import requests

BASE_URL = "https://api.nasa.gov"
API_KEY = os.getenv("NASA_API_KEY", "hmtYnLTLanrRLFnolKLnqISkaoZCIhus1j6ziJgv")

SESSION = requests.Session()
SESSION.trust_env = False


def save(data: dict, filename: str) -> None:
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  -> сохранено в {filename}")


def request(endpoint: str, params: dict, with_auth: bool) -> requests.Response:
    params = dict(params)
    if with_auth:
        params["api_key"] = API_KEY
    resp = SESSION.get(f"{BASE_URL}{endpoint}", params=params, timeout=15)
    print(f"GET {resp.url.replace(API_KEY, '***')}")
    print(f"  HTTP {resp.status_code}; остаток лимита: "
          f"{resp.headers.get('X-RateLimit-Remaining', 'n/a')}")
    return resp


def main() -> None:
    print("1) Запрос без ключа:")
    resp = request("/planetary/apod", {}, with_auth=False)
    save(resp.json(), "nasa_apod_no_auth.json")

    print("\n2) Запрос с ключом (APOD, астрономическая картинка дня):")
    resp = request("/planetary/apod", {}, with_auth=True)
    resp.raise_for_status()
    apod = resp.json()
    save(apod, "nasa_apod_response.json")
    print(f"  Дата: {apod.get('date')}, заголовок: {apod.get('title')}")

    print("\n3) Запрос с ключом (NeoWs, околоземные астероиды за дату):")
    resp = request("/neo/rest/v1/feed",
                   {"start_date": "2026-09-14", "end_date": "2026-09-14"}, with_auth=True)
    resp.raise_for_status()
    neo = resp.json()
    save(neo, "nasa_neo_response.json")
    print(f"  Астероидов в выборке: {neo.get('element_count')}")

if __name__ == "__main__":
    main()
