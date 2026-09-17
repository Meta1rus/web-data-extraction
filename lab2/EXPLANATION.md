# Подробное руководство по Лабораторной работе № 2 (BeautifulSoup и Octoparse)

Этот документ содержит детальное описание теории веб-парсинга, а также **построчный и поаргументный разбор** программного скрипта `scraper.py` и визуального сценария Octoparse.

---

## 1. Построчный и поаргументный разбор `scraper.py`

### Исходный код: импорты и конфигурация
```python
1:  import argparse
2:  import re
3:  import time
4:  from urllib.parse import urljoin
5:  
6:  import pandas as pd
7:  import requests
8:  from bs4 import BeautifulSoup
```
* **Строка 1 (`argparse`):** Стандартный модуль Python для разбора флагов командной строки (например, `--pages 2`).
* **Строка 2 (`re`):** Модуль регулярных выражений (нужен для извлечения числового ID товара из ссылки вида `/product/31`).
* **Строка 3 (`time`):** Нужен для `time.sleep()` — паузы между повторными попытками и страницами.
* **Строка 4 (`urljoin`):** Функция из `urllib.parse`, которая объединяет базовый URL (`https://webscraper.io`) и относительный путь ссылки (`/product/31`) в корректный абсолютный URL.
* **Строка 6 (`pandas as pd`):** Библиотека для работы с таблицами, типами данных, очисткой и экспортом в CSV.
* **Строка 7 (`requests`):** Скачивание HTML-кода страницы.
* **Строка 8 (`BeautifulSoup`):** Поиск и извлечение данных из дерева HTML.

```python
10: BASE_URL = "https://webscraper.io"
11: CATEGORIES = {
12:     "laptops": "/test-sites/e-commerce/static/computers/laptops",
13:     "tablets": "/test-sites/e-commerce/static/computers/tablets",
14:     "touch phones": "/test-sites/e-commerce/static/phones/touch",
15: }
16: HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
17: TIMEOUT = 10
18: RETRIES = 3
19: DELAY = 0.3
20: 
21: SESSION = requests.Session()
22: SESSION.trust_env = False
```
* **`BASE_URL`:** Главный домен учебного сайта.
* **`CATEGORIES` (Словарь):** Сопоставляет название категории и её относительный путь на сайте.
* **`HEADERS`:** Заголовок `User-Agent` маскирует скрипт под обычный браузер Chrome на Windows, чтобы сервер не блокировал запрос как подозрительный бот.
* **`TIMEOUT = 10`:** Ограничение ожидания ответа сервера (10 секунд).
* **`RETRIES = 3`:** Всего попыток выполнения запроса (1 основная + 2 повторные по ТЗ).
* **`DELAY = 0.3`:** Вежливая пауза в 300 мс между запросами, чтобы не перегружать сервер.
* **`SESSION` и `trust_env = False`:** Сессия без использования системного прокси.

---

### Функция `fetch`:
```python
25: def fetch(url: str, params: dict | None = None) -> str:
```
* **Аргументы функции:**
  * `url` (`str`): Целевой веб-адрес для загрузки HTML.
  * `params` (`dict | None`): Словарь параметров строки запроса (например, `{"page": 2}`).

```python
26:     last_error: Exception | None = None
27:     for attempt in range(1, RETRIES + 1):
28:         try:
29:             resp = SESSION.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
30:             resp.raise_for_status()
31:             return resp.text
32:         except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
33:             last_error = e
34:             if attempt < RETRIES:
35:                 time.sleep(2 ** (attempt - 1))
36:     raise RuntimeError(f"Failed to fetch {url} after {RETRIES} attempts") from last_error
```
* **Строка 26:** Переменная для сохранения последней ошибки, если все попытки закончатся неудачей.
* **Строка 27:** Цикл `for attempt in range(1, 4)` выполняет до 3 попыток (1, 2, 3).
* **Строки 29–31:** 
  * `SESSION.get(...)` отправляет запрос с таймаутом 10 секунд.
  * `resp.raise_for_status()` вызывает ошибку при кодах `4xx`/`5xx`.
  * `return resp.text` — в случае успеха немедленно возвращает скачанный HTML-текст страницы.
* **Строки 32–35 (Обработка ошибок):**
  * Перехватываются три ключевых типа сетевых исключений:
    1. `requests.Timeout` — сервер не ответил за 10 секунд;
    2. `requests.ConnectionError` — проблемы с соединением или DNS;
    3. `requests.HTTPError` — сервер вернул ошибку (например, `500 Internal Server Error`).
  * `time.sleep(2 ** (attempt - 1))` — экспоненциальная задержка:
    * При ошибке на попытке 1: спим $2^0 = 1$ секунду.
    * При ошибке на попытке 2: спим $2^1 = 2$ секунды.
* **Строка 36:** Если все 3 попытки исчерпаны, выбрасывается `RuntimeError` с сохранением первопричины ошибки (`from last_error`).

---

### Функция `parse_page`:
```python
39: def parse_page(html: str, category: str) -> list[dict]:
40:     soup = BeautifulSoup(html, "html.parser")
41:     rows = []
```
* **Аргументы функции:**
  * `html` (`str`): Текст HTML-страницы.
  * `category` (`str`): Категория, к которой относятся товары на этой странице (например, `"laptops"`).
* **Строка 40:** Создание DOM-дерева страницы парсером `"html.parser"`.
* **Строка 41:** Список для сохранения извлеченных словарей товаров.

```python
42:     for card in soup.select("div.card.thumbnail"):
```
* **Строка 42:** `select("div.card.thumbnail")` — CSS-селектор для поиска **всех** карточек товаров на странице (тег `div`, имеющий оба класса `card` и `thumbnail`).

```python
43:         link = card.select_one("a.title")
44:         price = card.select_one("h4.price span[itemprop=price]")
45:         rating = card.select_one("div.ratings p[data-rating]")
46:         reviews = card.select_one("span[itemprop=reviewCount]")
47:         descr = card.select_one("p.description")
```
* Поиск элементов внутри **одной** карточки через `select_one` (возвращает первый совпавший тег):
  * Строка 43: Ссылка на товар `<a>` с классом `title`.
  * Строка 44: Тег `<span>` с атрибутом `itemprop="price"` внутри заголовка `<h4>` с классом `price`.
  * Строка 45: Тег `<p>` с атрибутом `data-rating` внутри `<div class="ratings">`.
  * Строка 46: Тег `<span>` с атрибутом `itemprop="reviewCount"` (число отзывов).
  * Строка 47: Абзац `<p>` с описанием товара (класс `description`).

```python
49:         href = link["href"] if link else None
50:         m = re.search(r"/product/(\d+)", href or "")
51:         rows.append({
52:             "product_id": m.group(1) if m else None,
53:             "title": link.get("title") if link else None,
54:             "category": category,
55:             "price_usd": price.get_text() if price else None,
56:             "rating": rating.get("data-rating") if rating else None,
57:             "reviews": reviews.get_text() if reviews else None,
58:             "url": urljoin(BASE_URL, href) if href else None,
59:             "description": descr.get_text() if descr else None,
60:         })
61:     return rows
```
* **Строка 49:** `link["href"]` извлекает относительный путь ссылки (например, `"/test-sites/e-commerce/static/product/31"`).
* **Строка 50:** Регулярное выражение `re.search(r"/product/(\d+)", href)` ищет подстроку `/product/` и захватывает одну или более цифр (`\d+`) в круглых скобках (группа 1).
* **Поля в словаре:**
  * `"product_id"`: `m.group(1)` возвращает захваченное число (например, `"31"`).
  * `"title"`: `link.get("title")` берет значение атрибута `title` (содержит полное имя товара без обрезки).
  * `"category"`: передается из аргументов (`"laptops"`, `"tablets"` или `"touch phones"`).
  * `"price_usd"`: `price.get_text()` извлекает видимый текст цены (например, `"$416.99"`).
  * `"rating"`: `rating.get("data-rating")` извлекает значение **`data-*` атрибута** (например, `"2"`).
  * `"reviews"`: `reviews.get_text()` извлекает текст количества отзывов (например, `"2"`).
  * `"url"`: `urljoin(BASE_URL, href)` преобразует ссылку в абсолютную `https://webscraper.io/test-sites/e-commerce/static/product/31`.
  * `"description"`: видимый текст описания характеристик.

---

### Функция `last_page_number`:
```python
64: def last_page_number(html: str) -> int:
65:     soup = BeautifulSoup(html, "html.parser")
66:     numbers = [int(a.get_text(strip=True)) for a in soup.select("ul.pagination a")
67:                if a.get_text(strip=True).isdigit()]
68:     return max(numbers) if numbers else 1
```
* Находит все ссылки пагинации `ul.pagination a`.
* `a.get_text(strip=True).isdigit()` отбирает только те ссылки, внутри которых написаны цифры (отсекая стрелочки `«` и `»`).
* `max(numbers)` возвращает номер последней доступной страницы (для ноутбуков это 20).

---

### Функция `scrape`:
```python
71: def scrape(max_pages: int | None) -> list[dict]:
```
* **Аргумент `max_pages`:** Ограничение числа страниц для теста (если `None`, парсятся все страницы).
* Проходит по категориям `CATEGORIES`, считывает первую страницу, определяет `total` страниц и в цикле от 2 до `total` запрашивает остальные страницы с вежливой паузой `time.sleep(DELAY)`.

---

### Функция `clean` (Очистка и типизация данных):
```python
88: def clean(rows: list[dict]) -> pd.DataFrame:
89:     df = pd.DataFrame(rows)
90:     raw_count = len(df)
```
* Создает таблицу `DataFrame` библиотеки `pandas` из списка сырых словарей.

```python
92:     for col in df.select_dtypes(include="object").columns:
93:         df[col] = df[col].str.strip()
```
* `select_dtypes(include="object")` выбирает все текстовые колонки.
* `.str.strip()` удаляет лишние пробелы и символы переноса строк в начале и в конце текста.

```python
95:     df["price_usd"] = pd.to_numeric(df["price_usd"].str.replace(r"[^\d.]", "", regex=True),
96:                                     errors="coerce")
```
* `.str.replace(r"[^\d.]", "", regex=True)`: регулярное выражение заменяет все символы, кроме цифр (`\d`) и точки (`.`), на пустую строку (удаляет знак `$`). Например, `"$416.99"` превращается в `"416.99"`.
* `pd.to_numeric(..., errors="coerce")`: переводит строку в числовой тип `float64`. Если встретится поврежденное значение, `errors="coerce"` запишет туда `NaN` вместо падения скрипта.

```python
97:     df["rating"] = pd.to_numeric(df["rating"], errors="coerce").astype("Int64")
98:     df["reviews"] = pd.to_numeric(df["reviews"], errors="coerce").astype("Int64")
99:     df["product_id"] = pd.to_numeric(df["product_id"], errors="coerce").astype("Int64")
```
* Преобразует поля в целочисленный тип `Int64` (тип с заглавной `I` поддерживает значения `NaN`, в отличие от обычного `int`).

```python
101:    df = df.drop_duplicates().reset_index(drop=True)
102:    print(f"\nСтрок до очистки: {raw_count}, после удаления дубликатов: {len(df)}")
103:    return df
```
* `df.drop_duplicates()`: находит и удаляет полностью дублирующиеся строки.
* `.reset_index(drop=True)`: перенумеровывает строки таблицы по порядку с 0.

---

### Функция `main()`:
```python
106: def main() -> None:
107:     ap = argparse.ArgumentParser()
108:     ap.add_argument("--pages", type=int, default=None, help="максимум страниц на категорию")
109:     args = ap.parse_args()
110: 
111:     df = clean(scrape(args.pages))
112:     df.to_csv("products.csv", index=False, encoding="utf-8")
```
* Настраивает аргумент `--pages`.
* Запускает скрейпинг `scrape()`, очистку `clean()` и сохраняет таблицу в `products.csv` без записи служебного столбца индекса (`index=False`).

```python
114:     pd.set_option("display.width", 200)
115:     pd.set_option("display.max_columns", None)
116:     print(f"\nЧисло строк: {len(df)}")
117:     print("\nДоля пропусков по столбцам:")
118:     print((df.isna().mean() * 100).round(2).astype(str) + " %")
119:     print("\nПервые 5 записей:")
120:     print(df.drop(columns=["description"]).head(5).to_string())
121:     print("\nТипы столбцов:")
122:     print(df.dtypes)
```
* **Строка 116:** Выводит общее число строк (147).
* **Строки 117–118:** Считает долю пропусков: `df.isna()` дает булеву маску `True/False`, метод `.mean()` считает среднюю долю пустых значений (умножаем на 100 для процентов). У нас получилось `0.0 %` по всем колонкам.
* **Строки 119–120:** `df.head(5)` выводит первые 5 записей (для компактности временно отбрасывается длинное поле `description`).
* **Строка 122:** `df.dtypes` печатает типы данных каждого столбца (`float64`, `Int64`, `object`).

---

## 2. Разбор части B: No-code сценарий Octoparse

### Как это устроено по шагам в интерфейсе:
1. **Создание задачи:** Вводится URL `https://webscraper.io/test-sites/e-commerce/static/computers/laptops`.
2. **Определение карточки (Loop Item):** Пользователь кликает на первую карточку ноутбука, затем на вторую. Octoparse распознает повторяющийся шаблон на странице и создает цикл обхода карточек.
3. **Выбор полей (Extract Data):** Внутри первой карточки кликаются элементы:
   * Заголовок (`a.title`);
   * Цена (`h4.price`);
   * Отзывы (`span[itemprop=reviewCount]`);
   * Рейтинг (настройка извлечения атрибута `data-rating`);
   * Ссылка (извлечение атрибута ссылки `href`).
4. **Настройка пагинации (Pagination):** Клик по кнопке пагинации `Next >` внизу страницы со свойством «Loop click Next Page». Цикл повторяется 20 раз.
5. **Экспорт:** Данные выгружаются в файл `octoparse_products.csv` (117 строк).
