"""Сверка наборов: браузер (data_browser.csv) ↔ дешёвая альтернатива (data_light.csv),
внутренняя согласованность (теги из ленты ↔ обход разделов тегов) и, если есть,
результат no-code (nocode/nocode_result.csv) ↔ лента браузера.

Результат: печать + metrics/compare.json. Код возврата 0 — наборы совпали; 6 — есть расхождения.
"""
from __future__ import annotations

import sys

import pandas as pd

import common as C

FIELDS = ["text", "author", "author_slug", "author_url", "goodreads_url", "tags_all", "n_tags"]


def load(name: str) -> pd.DataFrame | None:
    p = C.OUT_DIR / name
    if not p.exists():
        print(f"[compare] нет файла {name}")
        return None
    return C.typed(pd.read_csv(p, keep_default_na=False))


def diff_sets(a: pd.DataFrame, b: pd.DataFrame, key: str, la: str, lb: str, fields) -> dict:
    ka, kb = set(a[key]), set(b[key])
    both = ka & kb
    out = {"key": key, f"{la}_records": len(a), f"{lb}_records": len(b), "common_keys": len(both),
           f"only_{la}": sorted(ka - kb)[:50], f"only_{lb}": sorted(kb - ka)[:50],
           f"only_{la}_count": len(ka - kb), f"only_{lb}_count": len(kb - ka), "field_mismatches": {}}
    m = a.set_index(key).loc[sorted(both)].join(b.set_index(key).loc[sorted(both)], lsuffix="_a", rsuffix="_b")
    for f in fields:
        if f"{f}_a" not in m:
            continue
        bad = m[m[f"{f}_a"].astype(str) != m[f"{f}_b"].astype(str)]
        if len(bad):
            out["field_mismatches"][f] = {"count": int(len(bad)), "examples": [
                {"key": k, la: str(r[f"{f}_a"])[:120], lb: str(r[f"{f}_b"])[:120]}
                for k, r in bad.head(5).iterrows()]}
    out["identical"] = (not out[f"only_{la}_count"] and not out[f"only_{lb}_count"]
                        and not out["field_mismatches"])
    return out


def consistency(quotes: pd.DataFrame, pairs: pd.DataFrame) -> dict:
    """Пары «цитата × тег», ожидаемые по метатегам ленты, против найденных обходом тегов."""
    exp = {(t, q) for q, tags in zip(quotes["quote_id"], quotes["tags_all"])
           for t in str(tags).split(",") if t}
    got = set(zip(pairs["tag"], pairs["quote_id"]))
    no_tags = quotes[quotes["n_tags"].fillna(0) == 0]
    feed_tags, crawled = {t for t, _ in exp}, set(pairs["tag"])
    exp_crawled = {p for p in exp if p[0] in crawled}
    return {"tags_in_feed": len(feed_tags), "tags_crawled": len(crawled),
            "tags_not_crawled": sorted(feed_tags - crawled)[:20],
            "expected_pairs_for_crawled_tags": len(exp_crawled),
            "missing_for_crawled_tags": len(exp_crawled - got),
            "expected_pairs_from_feed": len(exp), "pairs_from_tag_pages": len(got),
            "missing_on_tag_pages": len(exp - got), "unexpected_on_tag_pages": len(got - exp),
            "examples_missing": sorted(exp - got)[:5], "examples_unexpected": sorted(got - exp)[:5],
            "feed_quotes": len(quotes), "feed_quotes_without_tags": int(len(no_tags)),
            "unique_quotes_in_pairs": int(pairs["quote_id"].nunique())}


def nocode_check(feed: pd.DataFrame) -> dict | None:
    p = C.OUT_DIR / "nocode" / "nocode_result.csv"
    if not p.exists():
        return None
    nc = pd.read_csv(p, keep_default_na=False)
    cols = {c.lower(): c for c in nc.columns}
    tcol, acol = cols.get("text"), cols.get("author")
    if not tcol or not acol:
        return {"rows": len(nc), "error": "в nocode_result.csv нет столбцов text/author"}
    nc["quote_id"] = [C.quote_id(t, a) for t, a in zip(nc[tcol], nc[acol])]
    ka, kb = set(feed["quote_id"]), set(nc["quote_id"])
    gr = cols.get("goodreads_url")
    return {"rows": int(len(nc)), "unique_quotes": len(kb), "common_with_browser_feed": len(ka & kb),
            "only_nocode": len(kb - ka), "only_browser_feed": len(ka - kb),
            "goodreads_filled_share": round(float((nc[gr].astype(str).str.len() > 0).mean()), 4) if gr else None}


def main() -> int:
    res: dict = {"generated_at": C.now_iso()}
    db, dl = load("data_browser.csv"), load("data_light.csv")
    qb, ql = load("quotes_browser.csv"), load("quotes_light.csv")
    if db is not None and dl is not None:
        res["pairs"] = diff_sets(db, dl, "record_key", "browser", "light", FIELDS + ["tag"])
    if qb is not None and ql is not None:
        res["feed"] = diff_sets(qb, ql, "quote_id", "browser", "light", FIELDS)
    if qb is not None and db is not None:
        res["consistency_browser"] = consistency(qb, db)
    if ql is not None and dl is not None:
        res["consistency_light"] = consistency(ql, dl)
    if qb is not None:
        res["nocode"] = nocode_check(qb)
    C.save_json(C.METRICS / "compare.json", res)

    for name in ("pairs", "feed"):
        r = res.get(name)
        if r:
            print(f"[{name}] браузер={r['browser_records']} light={r['light_records']} общих={r['common_keys']} "
                  f"только браузер={r['only_browser_count']} только light={r['only_light_count']} "
                  f"расхождения полей={ {k: v['count'] for k, v in r['field_mismatches'].items()} } "
                  f"→ {'СОВПАДАЮТ' if r['identical'] else 'ЕСТЬ РАСХОЖДЕНИЯ'}")
    for name in ("consistency_browser", "consistency_light"):
        r = res.get(name)
        if r:
            print(f"[{name}] тегов в ленте={r['tags_in_feed']} обойдено={r['tags_crawled']}; "
                  f"ожидалось пар по ленте={r['expected_pairs_from_feed']} найдено на страницах тегов="
                  f"{r['pairs_from_tag_pages']} (для обойдённых тегов не найдено: {r['missing_for_crawled_tags']}, "
                  f"лишних: {r['unexpected_on_tag_pages']}); цитат без тегов: {r['feed_quotes_without_tags']}")
    if res.get("nocode"):
        print(f"[nocode] {res['nocode']}")
    ok = all(res.get(n, {}).get("identical", False) for n in ("pairs", "feed"))
    print("Итог сверки:", "наборы совпадают" if ok else "есть расхождения — см. metrics/compare.json")
    return 0 if ok else 6


if __name__ == "__main__":
    sys.exit(main())
