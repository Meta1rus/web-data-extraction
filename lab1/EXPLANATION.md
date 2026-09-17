# Подробное руководство по Лабораторной работе № 1 (REST API)

Этот документ содержит детальное описание теории, а также **построчный и поаргументный разбор** каждого скрипта для полного понимания того, как работает алгоритм.

---

## 1. Теоретическая база (простыми словами)

* **API (Application Programming Interface):** Специальный веб-адрес (эндпоинт), который отдает не визуальную страницу сайта с картинками, а «сырые» структурированные данные.
* **JSON (JavaScript Object Notation):** Текстовый формат обмена данными. В Python он преобразуется в списки (`list`) и словари (`dict`).
* **HTTP-запрос:** Сообщение от клиента серверу. Включает в себя:
  * **URL:** Адрес назначения (например, `https://api.github.com/users/Meta1rus/repos`).
  * **Headers (Заголовки):** Метаданные (формат данных, токены авторизации).
  * **Query Parameters (Параметры запроса):** Переменные в конце URL после знака `?` (например, `?per_page=100&page=1`).
* **Коды ответов (Status Codes):**
  * `200 OK` — запрос выполнен успешно, данные переданы.
  * `403 Forbidden` — доступ запрещен (отсутствует ключ авторизации или исчерпан лимит запросов).
  * `404 Not Found` — запрашиваемый ресурс (пользователь) не найден.

---

## 2. Построчный и поаргументный разбор `github_repos.py`

### Исходный код:
```python
1:  import json
2:  import os
3:  import sys
4:  
5:  import requests
6:  
7:  API_URL = "https://api.github.com/users/{username}/repos"
```
* **Строка 1 (`import json`):** Модуль для сериализации (превращения объектов Python в строку JSON и записи в файл).
* **Строка 2 (`import os`):** Модуль операционной системы. Нужен для вызова `os.getenv()`, с помощью которого считывается переменная окружения `GITHUB_TOKEN`.
* **Строка 3 (`import sys`):** Модуль для работы со средой выполнения. Нужен для `sys.argv` — списка аргументов командной строки.
* **Строка 5 (`import requests`):** Библиотека для выполнения HTTP-запросов.
* **Строка 7 (`API_URL = ...`):** Базовый шаблон URL эндпоинта GitHub API. `{username}` — заполнитель (placeholder), в который позже подставляется имя пользователя.

---

### Функция `get_user_repos`:
```python
10: def get_user_repos(username: str, token: str | None = None) -> list[dict]:
```
* **Аргументы функции:**
  * `username` (`str`): Имя аккаунта на GitHub, чьи репозитории нужно выгрузить (например, `"Meta1rus"`).
  * `token` (`str | None`): Необязательный личный токен GitHub. Если передан — лимит запросов увеличивается с 60 до 5000 в час.

```python
11:     session = requests.Session()
12:     session.trust_env = False
```
* **Строка 11:** Создается объект `Session`. Сессия повторно использует TCP-соединение для серии запросов, что снижает накладные расходы и ускоряет работу.
* **Строка 12:** `session.trust_env = False` отключает автоматическое чтение системных настроек прокси macOS/Windows. Это защищает скрипт от падения с ошибкой `503 Service Unavailable`, если на компьютере настроен, но в данный момент не запущен VPN-клиент.

```python
14:     headers = {
15:         "Accept": "application/vnd.github+json",
16:         "X-GitHub-Api-Version": "2022-11-28",
17:     }
18:     if token:
19:         headers["Authorization"] = f"Bearer {token}"
```
* **`headers` (Словарь заголовков):**
  * `"Accept": "application/vnd.github+json"`: Сообщает серверу GitHub, что клиент ожидает ответ в официальном формате GitHub REST API.
  * `"X-GitHub-Api-Version": "2022-11-28"`: Фиксирует версию API (требование документации GitHub, чтобы логика не сломалась при будущих обновлениях API).
* **Строки 18–19:** Если токен задан, добавляется заголовок `Authorization` со схемой `Bearer`.

```python
21:     repos: list[dict] = []
22:     page = 1
```
* **Строка 21:** Инициализация пустого списка, в который будут накапливаться найденные репозитории.
* **Строка 22:** Переменная-счетчик текущей страницы для пагинации. Начинаем с первой страницы.

```python
23:     while True:
24:         resp = session.get(
25:             API_URL.format(username=username),
26:             headers=headers,
27:             params={"per_page": 100, "page": page, "sort": "updated"},
28:             timeout=10,
29:         )
```
* **Строка 23 (`while True`):** Бесконечный цикл обхода страниц, пока сервер не вернет пустой список.
* **Строки 24–29 (`session.get`):** Выполнение HTTP-запроса методом `GET`.
  * **Аргумент `url`:** `API_URL.format(username=username)` подставляет значение (например, получается `"https://api.github.com/users/Meta1rus/repos"`).
  * **Аргумент `headers`:** Передает подготовленные HTTP-заголовки.
  * **Аргумент `params`:** Словарь параметров, которые библиотека автоматически преобразует в строку запроса: `?per_page=100&page=1&sort=updated`:
    * `per_page: 100` — просим вернуть максимум (100 элементов на страницу вместо стандартных 30).
    * `page: page` — номер запрашиваемой страницы.
    * `sort: "updated"` — сортировка репозиториев по дате последнего обновления.
  * **Аргумент `timeout=10`:** Предельное время ожидания ответа сервера в секундах. Если за 10 секунд сервер не ответил, запрос прерывается (защита от зависания).

```python
30:         resp.raise_for_status()
31:         chunk = resp.json()
32:         if not chunk:
33:             break
34:         repos.extend(chunk)
35:         page += 1
36:     return repos
```
* **Строка 30:** Метод проверяет статус-код HTTP. Если сервер вернул ошибку (например, `404 Not Found` при неверном логине), немедленно выбрасывается исключение `HTTPError`.
* **Строка 31:** Десериализует текстовый ответ сервера из JSON в список словарей Python.
* **Строки 32–33:** Условие выхода из пагинации: если `chunk` пустой (`[]`), значит репозитории на сервере закончились, и цикл прерывается (`break`).
* **Строка 34:** Метод `extend()` добавляет все элементы текущей страницы в итоговый список `repos`.
* **Строка 35:** Инкремент номера страницы для следующей итерации.
* **Строка 36:** Возврат полного списка репозиториев.

---

### Функция `main()`:
```python
39: def main() -> None:
40:     username = sys.argv[1] if len(sys.argv) > 1 else "Meta1rus"
41:     token = os.getenv("GITHUB_TOKEN")
```
* **Строка 40:** Проверка аргументов командной строки. `sys.argv[0]` — имя самого скрипта, `sys.argv[1]` — первый переданный параметр (например, `python3 github_repos.py octocat`). Если параметр не передан, используется значение по умолчанию `"Meta1rus"`.
* **Строка 41:** Проверка наличия токена в переменных окружения.

```python
43:     repos = get_user_repos(username, token)
44:     out_file = f"{username}_repos.json"
45:     with open(out_file, "w", encoding="utf-8") as f:
46:         json.dump(repos, f, ensure_ascii=False, indent=2)
```
* **Строка 43:** Вызов функции сбора данных.
* **Строка 44:** Формирование имени результирующего файла (`Meta1rus_repos.json`).
* **Строки 45–46:** Открытие файла на запись с кодировкой UTF-8. 
  * `json.dump(repos, f, ensure_ascii=False, indent=2)`:
    * `repos`: записываемый объект.
    * `f`: файловый дескриптор.
    * `ensure_ascii=False`: сохранять символы кириллицы и эмодзи напрямую в UTF-8 без экранирования в `\uXXXX`.
    * `indent=2`: визуальный отступ в 2 пробела для формирования структурированного JSON.

```python
48:     print(f"Пользователь: {username}")
49:     print(f"Репозиториев получено: {len(repos)}")
50:     print(f"Сохранено в: {out_file}\n")
51:     print(f"{'Название':30} {'Язык':12} {'Звёзды':>7}  URL")
52:     for r in repos:
53:         print(f"{r['name']:30} {str(r['language']):12} {r['stargazers_count']:>7}  {r['html_url']}")
```
* **Строки 48–53:** Вывод итоговой форматированной таблицы в терминал. `r['name']:30` выделяет под название 30 колонок, `>7` выравнивает число звезд по правому краю.

---

## 3. Построчный и поаргументный разбор `api_auth_request.py`

### Исходный код:
```python
1:  import json
2:  import os
3:  import requests
4:  
5:  BASE_URL = "https://api.nasa.gov"
6:  API_KEY = os.getenv("NASA_API_KEY", "hmtYnLTLanrRLFnolKLnqISkaoZCIhus1j6ziJgv")
7:  
8:  SESSION = requests.Session()
9:  SESSION.trust_env = False
```
* **Строка 5 (`BASE_URL`):** Корневой домен открытого API NASA.
* **Строка 6 (`API_KEY`):** Персональный ключ авторизации (приоритетно из переменной окружения, иначе заданное значение).
* **Строки 8–9:** Глобальная сессия `requests` с отключенным системным прокси.

---

### Функция `save`:
```python
12: def save(data: dict, filename: str) -> None:
13:     with open(filename, "w", encoding="utf-8") as f:
14:         json.dump(data, f, ensure_ascii=False, indent=2)
15:     print(f"  -> сохранено в {filename}")
```
* **Аргументы:**
  * `data` (`dict`): Словарь с ответом сервера.
  * `filename` (`str`): Имя целевого файла для сохранения.

---

### Функция `request`:
```python
18: def request(endpoint: str, params: dict, with_auth: bool) -> requests.Response:
19:     params = dict(params)
20:     if with_auth:
21:         params["api_key"] = API_KEY
22:     resp = SESSION.get(f"{BASE_URL}{endpoint}", params=params, timeout=15)
23:     print(f"GET {resp.url.replace(API_KEY, '***')}")
24:     print(f"  HTTP {resp.status_code}; остаток лимита: "
25:           f"{resp.headers.get('X-RateLimit-Remaining', 'n/a')}")
26:     return resp
```
* **Аргументы функции:**
  * `endpoint` (`str`): Относительный путь к эндпоинту NASA (например, `"/planetary/apod"`).
  * `params` (`dict`): Словарь query-параметров запроса (например, даты).
  * `with_auth` (`bool`): Флаг включения авторизации (`True` — добавить ключ, `False` — отправить запрос без ключа).
* **Строка 19:** `params = dict(params)` создает неглубокую копию словаря, чтобы не модифицировать переданный извне объект.
* **Строки 20–21:** Если флаг `with_auth=True`, в параметры запроса подставляется ключ: `params["api_key"] = API_KEY`.
* **Строка 22:** `SESSION.get(...)` формирует GET-запрос:
  * URL: конкатенация `BASE_URL + endpoint`.
  * `params`: словарь превращается в query string `?api_key=...&...`.
  * `timeout=15`: таймаут 15 секунд.
* **Строка 23:** Вывод сформированного URL в консоль. Метод `.replace(API_KEY, '***')` маскирует секретный ключ звёздочками в логах.
* **Строки 24–25:** Чтение заголовка ответа `X-RateLimit-Remaining`, возвращающего остаток лимита запросов.

---

### Функция `main()`:
```python
29: def main() -> None:
30:     print("1) Запрос без ключа:")
31:     resp = request("/planetary/apod", {}, with_auth=False)
32:     save(resp.json(), "nasa_apod_no_auth.json")
```
* **Запрос 1:** Отправляется к астрономической картинке дня APOD **без ключа** (`with_auth=False`). Сервер возвращает статус `403 Forbidden`, тело ответа содержит ошибку `API_KEY_MISSING` и сохраняется в `nasa_apod_no_auth.json`. Это выполняет требование продемонстрировать защищенность API.

```python
34:     print("\n2) Запрос с ключом (APOD, астрономическая картинка дня):")
35:     resp = request("/planetary/apod", {}, with_auth=True)
36:     resp.raise_for_status()
37:     apod = resp.json()
38:     save(apod, "nasa_apod_response.json")
39:     print(f"  Дата: {apod.get('date')}, заголовок: {apod.get('title')}")
```
* **Запрос 2:** Запрос к тому же эндпоинту, но **с ключом** (`with_auth=True`). Сервер возвращает `200 OK` и данные снимка дня (URL изображения, описание, название). Сохраняется в `nasa_apod_response.json`.

```python
41:     print("\n3) Запрос с ключом (NeoWs, околоземные астероиды за дату):")
42:     resp = request("/neo/rest/v1/feed",
43:                    {"start_date": "2026-09-14", "end_date": "2026-09-14"}, with_auth=True)
44:     resp.raise_for_status()
45:     neo = resp.json()
46:     save(neo, "nasa_neo_response.json")
47:     print(f"  Астероидов в выборке: {neo.get('element_count')}")
```
* **Запрос 3:** Авторизованный запрос к API околоземных объектов (Near Earth Object Web Service). Передаются параметры `start_date` и `end_date`. Сервер возвращает список астероидов, сближающихся с Землей за указанные сутки. Сохраняется в `nasa_neo_response.json`.
