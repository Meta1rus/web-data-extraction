import json
import os
import sys

import requests

API_URL = "https://api.github.com/users/{username}/repos"


def get_user_repos(username: str, token: str | None = None) -> list[dict]:
    session = requests.Session()
    session.trust_env = False

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    repos: list[dict] = []
    page = 1
    while True:
        resp = session.get(
            API_URL.format(username=username),
            headers=headers,
            params={"per_page": 100, "page": page, "sort": "updated"},
            timeout=10,
        )
        resp.raise_for_status()
        chunk = resp.json()
        if not chunk:
            break
        repos.extend(chunk)
        page += 1
    return repos


def main() -> None:
    username = sys.argv[1] if len(sys.argv) > 1 else "Meta1rus"
    token = os.getenv("GITHUB_TOKEN")

    repos = get_user_repos(username, token)

    out_file = f"{username}_repos.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(repos, f, ensure_ascii=False, indent=2)

    print(f"Пользователь: {username}")
    print(f"Репозиториев получено: {len(repos)}")
    print(f"Сохранено в: {out_file}\n")
    print(f"{'Название':30} {'Язык':12} {'Звёзды':>7}  URL")
    for r in repos:
        print(f"{r['name']:30} {str(r['language']):12} {r['stargazers_count']:>7}  {r['html_url']}")


if __name__ == "__main__":
    main()
