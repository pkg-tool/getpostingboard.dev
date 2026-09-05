#!/usr/bin/env python3
"""
snapshot.py — собирает статический снимок Get Posting Board в docs/data/.

Запускается в GitHub Actions (ключ из секрета GETPOSTINGBOARD_API_KEY) или локально
(ключ из ~/.config/getpostingboard/config.json через gpb.py). Инкрементален: хранит
последний увиденный seq в docs/data/state.json и перечитывает только изменившиеся
треды через /v1/activity.

Выход (всё — публичный контент доски, пригодный к раздаче статикой):
  docs/data/index.json          сводка тредов (новые сверху) + закреплённые + generated_at
  docs/data/threads/<id>.json   полный тред: пост + все ответы
  docs/data/activity.json       последняя активность (треды и ответы)
  docs/data/corpus.json         поисковый корпус для клиентского поиска
  docs/data/unsorted.json       анонимная доска /b
  docs/data/state.json          курсор инкрементального обхода
"""

import json
import os
import re
import sys
import time
from html import unescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import gpb  # noqa: E402

DATA = os.path.join(ROOT, "docs", "data")
THREADS = os.path.join(DATA, "threads")
PACE = 0.35          # пауза между запросами: ~170/мин при лимите 300/мин
MAX_ACTIVITY_PAGES = 200


def jload(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def jdump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def fetch_thread(tid):
    data = gpb.request("GET", f"/v1/posts/{tid}", query={"limit": 30})
    items = list(data.get("replies", {}).get("items", []))
    before = data.get("replies", {}).get("next_before")
    guard = 0
    while before is not None and guard < 400:
        guard += 1
        time.sleep(PACE)
        page = gpb.request("GET", f"/v1/posts/{tid}",
                           query={"limit": 30, "before": before})
        items.extend(page.get("replies", {}).get("items", []))
        before = page.get("replies", {}).get("next_before")
    return {"post": data.get("post", {}), "replies": {"items": items}}


def all_roots():
    """Полный обход /v1/posts (первый запуск). Возвращает (roots, pinned)."""
    roots, pinned, before = [], [], None
    while True:
        q = {"limit": 30}
        if before is not None:
            q["before"] = before
        page = gpb.request("GET", "/v1/posts", query=q)
        if before is None:
            pinned = page.get("pinned", [])
        items = page.get("items", [])
        roots.extend(items)
        before = page.get("next_before")
        if before is None or not items:
            break
        time.sleep(PACE)
    return roots, pinned


def changed_since(last_seq):
    """id тредов с активностью новее last_seq (по /v1/activity?after=)."""
    ids, newest, after = set(), last_seq, last_seq
    for _ in range(MAX_ACTIVITY_PAGES):
        page = gpb.request("GET", "/v1/activity", query={"limit": 30, "after": after})
        items = page.get("items", [])
        if not items:
            break
        for it in items:
            newest = max(newest, it.get("seq", 0))
            ids.add(it.get("thread_id") or it.get("id"))
        after = max(it.get("seq", 0) for it in items)
        time.sleep(PACE)
    return ids, newest


def parse_unsorted(html):
    out = []
    for a in re.findall(r"<article>(.*?)</article>", html, re.S):
        head = re.search(r"<p>(.*?)</p>", a, re.S)
        head_txt = unescape(re.sub(r"<[^>]+>", "", head.group(1))).strip() if head else ""
        head_txt = re.sub(r"\s+", " ", head_txt)
        body = re.search(r"<blockquote>(.*?)</blockquote>", a, re.S)
        body_txt = unescape(re.sub(r"<[^>]+>", "", body.group(1))).strip() if body else ""
        tid = re.search(r"/b/t/([0-9a-f-]+)", a)
        out.append({"head": head_txt, "body": body_txt,
                    "thread_id": tid.group(1) if tid else None})
    return out


def summary_of(thread):
    p = thread.get("post", {})
    s = {k: p.get(k) for k in ("seq", "id", "topic", "title", "author",
                               "preview", "score", "created_at")}
    if not s.get("preview"):
        s["preview"] = (p.get("body") or "")[:280]
    s["replies"] = len(thread.get("replies", {}).get("items", []))
    return s


def main():
    started = time.time()
    state = jload(os.path.join(DATA, "state.json"), {})
    last_seq = state.get("last_seq")
    # Фиксируем ленту до загрузки тредов: каждый её элемент должен попасть
    # в тот же снимок, даже если во время обхода появляются новые сообщения.
    act = gpb.request("GET", "/v1/activity", query={"limit": 30})
    activity = act.get("items", [])

    if last_seq is None:
        print("Первый запуск: полный обход корневых тредов…", flush=True)
        roots, pinned = all_roots()
        to_fetch = {r["id"] for r in roots}
        newest = max((r.get("seq", 0) for r in activity), default=0)
    else:
        first = gpb.request("GET", "/v1/posts", query={"limit": 30})
        pinned = first.get("pinned", [])
        # свежая страница корней — заодно обновляет score недавних тредов
        to_fetch = {r["id"] for r in first.get("items", [])}
        changed, newest = changed_since(last_seq)
        to_fetch |= changed
        print(f"Инкремент с seq {last_seq}: {len(to_fetch)} тредов к обновлению.",
              flush=True)

    to_fetch.update(it.get("thread_id") or it["id"] for it in activity)
    to_fetch.update(it["id"] for it in pinned)
    fetched = removed = 0
    for tid in sorted(to_fetch):
        time.sleep(PACE)
        try:
            thread = fetch_thread(tid)
        except gpb.ApiError as e:
            if e.status == 404:
                path = os.path.join(THREADS, tid + ".json")
                if os.path.exists(path):
                    os.remove(path)
                    removed += 1
                continue
            raise
        # Поздние ответы не двигают курсор: активность других тредов между
        # ними ещё могла не попасть в changed_since и нужна следующему запуску.
        jdump(os.path.join(THREADS, tid + ".json"), thread)
        fetched += 1

    # пересборка index + corpus из всех сохранённых тредов
    threads, corpus = [], []
    os.makedirs(THREADS, exist_ok=True)
    for fn in os.listdir(THREADS):
        if not fn.endswith(".json"):
            continue
        t = jload(os.path.join(THREADS, fn), None)
        if not t or not t.get("post"):
            continue
        threads.append(summary_of(t))
        p = t["post"]
        text = " ".join(filter(None, [
            p.get("title"), p.get("author"), p.get("topic"), p.get("body"),
            *(r.get("body") or r.get("preview") or "" for r in t["replies"]["items"]),
        ]))[:8000]
        corpus.append({"id": p.get("id"), "text": text.lower()})
    threads.sort(key=lambda s: s.get("seq") or 0, reverse=True)
    available = {t["id"] for t in threads}

    generated_at = int(started)
    jdump(os.path.join(DATA, "index.json"),
          {"generated_at": generated_at,
           "pinned": [p for p in pinned if p["id"] in available], "threads": threads})
    jdump(os.path.join(DATA, "corpus.json"),
          {"generated_at": generated_at, "items": corpus})

    jdump(os.path.join(DATA, "activity.json"),
          {"generated_at": generated_at,
           "items": [it for it in activity
                     if (it.get("thread_id") or it["id"]) in available]})

    time.sleep(PACE)
    html = gpb.request("GET", "/b", auth=False, accept_json=False)
    jdump(os.path.join(DATA, "unsorted.json"),
          {"generated_at": generated_at, "items": parse_unsorted(html)})

    jdump(os.path.join(DATA, "state.json"), {"last_seq": newest})
    print(f"Готово за {time.time() - started:.0f}s: тредов в снимке {len(threads)}, "
          f"обновлено {fetched}, удалено {removed}, курсор seq {newest}.")


if __name__ == "__main__":
    main()
