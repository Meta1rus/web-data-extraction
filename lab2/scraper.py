import argparse
import re
import time
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://webscraper.io"
CATEGORIES = {
    "laptops": "/test-sites/e-commerce/static/computers/laptops",
    "tablets": "/test-sites/e-commerce/static/computers/tablets",
    "touch phones": "/test-sites/e-commerce/static/phones/touch",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TIMEOUT = 10
RETRIES = 3
DELAY = 0.3

SESSION = requests.Session()
SESSION.trust_env = False


def fetch(url: str, params: dict | None = None) -> str:
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = SESSION.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
            last_error = e
            if attempt < RETRIES:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"Failed to fetch {url} after {RETRIES} attempts") from last_error


def parse_page(html: str, category: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for card in soup.select("div.card.thumbnail"):
        link = card.select_one("a.title")
        price = card.select_one("h4.price span[itemprop=price]")
        rating = card.select_one("div.ratings p[data-rating]")
        reviews = card.select_one("span[itemprop=reviewCount]")
        descr = card.select_one("p.description")

        href = link["href"] if link else None
        m = re.search(r"/product/(\d+)", href or "")
        rows.append({
            "product_id": m.group(1) if m else None,
            "title": link.get("title") if link else None,
            "category": category,
            "price_usd": price.get_text() if price else None,
            "rating": rating.get("data-rating") if rating else None,
            "reviews": reviews.get_text() if reviews else None,
            "url": urljoin(BASE_URL, href) if href else None,
            "description": descr.get_text() if descr else None,
        })
    return rows


def last_page_number(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    numbers = [int(a.get_text(strip=True)) for a in soup.select("ul.pagination a")
               if a.get_text(strip=True).isdigit()]
    return max(numbers) if numbers else 1


def scrape(max_pages: int | None) -> list[dict]:
    rows: list[dict] = []
    for category, path in CATEGORIES.items():
        url = urljoin(BASE_URL, path)
        print(f"[{category}] страница 1")
        html = fetch(url)
        rows += parse_page(html, category)

        total = last_page_number(html)
        if max_pages:
            total = min(total, max_pages)
        for page in range(2, total + 1):
            time.sleep(DELAY)
            print(f"[{category}] страница {page}/{total}")
            rows += parse_page(fetch(url, params={"page": page}), category)
    return rows


def clean(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    raw_count = len(df)

    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].str.strip()

    df["price_usd"] = pd.to_numeric(df["price_usd"].str.replace(r"[^\d.]", "", regex=True),
                                    errors="coerce")
    df["rating"] = pd.to_numeric(df["rating"], errors="coerce").astype("Int64")
    df["reviews"] = pd.to_numeric(df["reviews"], errors="coerce").astype("Int64")
    df["product_id"] = pd.to_numeric(df["product_id"], errors="coerce").astype("Int64")

    df = df.drop_duplicates().reset_index(drop=True)
    print(f"\nСтрок до очистки: {raw_count}, после удаления дубликатов: {len(df)}")
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=None, help="максимум страниц на категорию")
    args = ap.parse_args()

    df = clean(scrape(args.pages))
    df.to_csv("products.csv", index=False, encoding="utf-8")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", None)
    print(f"\nЧисло строк: {len(df)}")
    print("\nДоля пропусков по столбцам:")
    print((df.isna().mean() * 100).round(2).astype(str) + " %")
    print("\nПервые 5 записей:")
    print(df.drop(columns=["description"]).head(5).to_string())
    print("\nТипы столбцов:")
    print(df.dtypes)
    print("\nСохранено в products.csv")


if __name__ == "__main__":
    main()
