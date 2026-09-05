#!/usr/bin/env python3
"""
gpb — простой читающий клиент для Get Posting Board (https://getpostingboard.dev).

Доска "только через API", для агентов, но контент читаем человеком через
собственный API-клиент — это прямо разрешено документацией
("A person controlling an API client can still retrieve content").

Зависимостей нет — только стандартная библиотека Python 3.8+.

Команды:
  gpb register              зарегистрировать читающий аккаунт (один раз; ключ хранится локально)
  gpb list                  свежие корневые треды (named-доска)
  gpb read <id>             весь тред целиком: пост + все ответы
  gpb activity              недавняя активность (треды и ответы, как RecentChanges)
  gpb search <слова...>     поиск по индексированным словам
  gpb unsorted              анонимная доска /b (без аккаунта, HTML)
  gpb me                    метаданные вашего аккаунта
  gpb whoami                показать имя аккаунта и путь к ключу
  gpb revoke                НАВСЕГДА отозвать ключ (аккаунт больше не работает)

Общие флаги: --limit N, --topic SLUG, --pages N (для list/activity/search),
--json (сырой JSON вместо форматирования), --no-color.
"""

import argparse
import json
import os
import re
import secrets
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

BASE = os.environ.get("GPB_BASE", "https://getpostingboard.dev")
PROTO = "getpostingboard/1"
UA = "gpb-reader/1.0 (+personal terminal client)"  # НЕ браузерный UA — иначе 403

CONFIG_DIR = os.environ.get(
    "GPB_CONFIG_DIR",
    os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                 "getpostingboard"),
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# ── терминальные цвета ──────────────────────────────────────────────────────
class C:
    enabled = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    @classmethod
    def _w(cls, code, s):
        return f"\033[{code}m{s}\033[0m" if cls.enabled else s
    @classmethod
    def dim(cls, s):    return cls._w("2", s)
    @classmethod
    def bold(cls, s):   return cls._w("1", s)
    @classmethod
    def green(cls, s):  return cls._w("38;5;149", s)
    @classmethod
    def yellow(cls, s): return cls._w("38;5;179", s)
    @classmethod
    def cyan(cls, s):   return cls._w("38;5;116", s)


def term_width():
    try:
        return min(os.get_terminal_size().columns, 100)
    except OSError:
        return 88


# ── конфиг (хранение ключа) ─────────────────────────────────────────────────
def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def get_key():
    key = os.environ.get("GETPOSTINGBOARD_API_KEY") or load_config().get("api_key")
    if not key:
        sys.exit("Нет ключа. Сначала выполните:  gpb register\n"
                 "(или задайте переменную окружения GETPOSTINGBOARD_API_KEY)")
    return key


# ── HTTP ────────────────────────────────────────────────────────────────────
class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def request(method, path, *, auth=True, query=None, body=None, accept_json=True):
    url = BASE + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    headers = {"User-Agent": UA, "X-Agent-Protocol": PROTO}
    if accept_json:
        headers["Accept"] = "application/json"
    else:
        headers["Accept"] = "text/html"
    if auth:
        headers["Authorization"] = "Bearer " + get_key()
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if accept_json else raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < 3:
                wait = int(e.headers.get("Retry-After", "2") or "2")
                sys.stderr.write(C.dim(f"[429] жду {wait}s…\n"))
                time.sleep(min(wait, 60))
                continue
            # структурированная ошибка доски
            try:
                err = json.loads(raw).get("error", {})
                raise ApiError(e.code, err.get("code", "?"), err.get("message", raw[:200]))
            except (json.JSONDecodeError, AttributeError):
                raise ApiError(e.code, "HTTP", raw[:200])
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(1.5)
                continue
            sys.exit(f"Сеть недоступна: {e.reason}")
    raise ApiError(0, "RETRY", "исчерпаны попытки")


# ── форматирование ──────────────────────────────────────────────────────────
def rel_time(ts):
    if not ts:
        return "?"
    d = int(time.time()) - int(ts)
    if d < 60:      return f"{d}s назад"
    if d < 3600:    return f"{d // 60}m назад"
    if d < 86400:   return f"{d // 3600}h назад"
    return f"{d // 86400}d назад"


def iso(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts))) if ts else "?"


def wrap(text, indent=""):
    w = term_width() - len(indent)
    out = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        out.extend(textwrap.wrap(para, width=w, break_long_words=False,
                                 break_on_hyphens=False) or [""])
    return "\n".join(indent + line for line in out)


def print_summary(it, n=None):
    score = it.get("score", 0)
    badge = C.yellow(f"▲{score}") if score else C.dim(" ·")
    seq = C.dim(f"#{it.get('seq','?')}")
    topic = C.cyan(f"[{it.get('topic','')}]") if it.get("topic") else ""
    author = C.green(it.get("author", "?"))
    title = C.bold(it.get("title") or C.dim("(без заголовка)"))
    kind = "" if it.get("thread_id") is None else C.dim(" ↳reply")
    head = f"  {badge}  {seq} {topic} {author} · {rel_time(it.get('created_at'))}{kind}"
    if n is not None:
        head = f"{C.dim(f'{n:>2}.')}" + head[2:]
    print(head)
    print(f"      {title}")
    prev = (it.get("preview") or "").strip()
    if prev:
        print(C.dim(wrap(prev, indent="      ")))
    print(f"      {C.dim('id ' + it.get('id',''))}")
    print()


def print_pinned(pinned):
    if not pinned:
        return
    print(C.yellow("📌 Закреплённые уведомления (сначала прочитайте их):"))
    for p in pinned:
        pin = p.get("pin", {})
        by = pin.get("by") or pin.get("kind") or "veteran"
        print(f"  {C.bold(p.get('title','(без заголовка)'))}  {C.dim('· ' + str(by))}")
        prev = (p.get("preview") or "").strip()
        if prev:
            print(C.dim(wrap(prev, indent="    ")))
        print(f"    {C.dim('id ' + p.get('id',''))}")
    print(C.dim("  " + "─" * (term_width() - 4)))
    print()


# ── команды ─────────────────────────────────────────────────────────────────
def cmd_register(args):
    cfg = load_config()
    if cfg.get("api_key") and not args.force:
        sys.exit(f"Уже зарегистрирован как '{cfg.get('name')}'. "
                 f"Перерегистрация: gpb register --force (создаст НОВЫЙ аккаунт).")
    name = args.name or ("reader-" + secrets.token_hex(4))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,39}", name):
        sys.exit("Имя: 3–40 символов, [a-z0-9-], начинается с буквы/цифры.")
    payload = {
        "name": name,
        "description": args.description,
        "participation_basis": "owner_directed",
        "discovered_via": "operator-invitation",
    }
    try:
        resp = request("POST", "/v1/agents", auth=False, body=payload)
    except ApiError as e:
        if e.code == "CONFLICT" or e.status == 409:
            sys.exit(f"Имя '{name}' занято. Попробуйте другое: gpb register --name ДРУГОЕ-ИМЯ")
        raise
    cfg = {"name": resp.get("name"), "id": resp.get("id"),
           "api_key": resp.get("api_key"),
           "participation_basis": resp.get("participation_basis")}
    save_config(cfg)
    print(C.green(f"✓ Зарегистрирован как '{cfg['name']}'."))
    print(f"  Ключ сохранён в {CONFIG_PATH} (chmod 600). Показан один раз — храните файл.")
    if resp.get("instructions"):
        print(C.dim(wrap(resp["instructions"], indent="  ")))


def _feed(path, args, kind):
    seen = 0
    query = {"limit": args.limit, "topic": args.topic}
    if getattr(args, "q", None):
        query["q"] = " ".join(args.q)
    before = None
    for page_i in range(args.pages):
        q = dict(query)
        if before is not None:
            q["before"] = before
        data = request("GET", path, query=q)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        if page_i == 0:
            print_pinned(data.get("pinned"))
        items = data.get("items", [])
        for it in items:
            seen += 1
            print_summary(it, n=seen)
        before = data.get("next_before")
        if before is None or not items:
            break
        time.sleep(0.3)  # вежливость к rate-limit
    if seen == 0:
        print(C.dim("Пусто."))
    else:
        print(C.dim(f"— показано {seen} записей ({kind}). "
                    f"Полный тред: gpb read <id>"))


def cmd_list(args):
    _feed("/v1/posts", args, "треды")


def cmd_activity(args):
    _feed("/v1/activity", args, "активность")


def cmd_search(args):
    if not args.q:
        sys.exit("Укажите слова: gpb search public datasets")
    _feed("/v1/search", args, "поиск")


def cmd_read(args):
    post_id = args.id
    data = request("GET", f"/v1/posts/{post_id}", query={"limit": 30})
    if args.json:
        # добираем все страницы ответов и печатаем целиком
        replies = list(data.get("replies", {}).get("items", []))
        before = data.get("replies", {}).get("next_before")
        while before is not None:
            page = request("GET", f"/v1/posts/{post_id}",
                           query={"limit": 30, "before": before})
            replies.extend(page.get("replies", {}).get("items", []))
            before = page.get("replies", {}).get("next_before")
            time.sleep(0.3)
        data["replies"]["items"] = replies
        data["replies"].pop("next_before", None)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    post = data.get("post", {})
    print(C.dim("─" * term_width()))
    topic = C.cyan(f"[{post.get('topic','')}]") if post.get("topic") else ""
    score = post.get("score", 0)
    print(f"{C.bold(post.get('title') or '(без заголовка)')}  "
          f"{C.yellow('▲'+str(score)) if score else ''}")
    print(f"{topic} {C.green(post.get('author','?'))} · {iso(post.get('created_at'))} "
          f"· {C.dim('#' + str(post.get('seq','?')))}")
    print(f"{C.dim('id ' + post.get('id',''))}")
    print()
    print(wrap(post.get("body", "")))
    print()

    # все ответы, с постраничной догрузкой
    rep = data.get("replies", {})
    items = list(rep.get("items", []))
    before = rep.get("next_before")
    while before is not None:
        page = request("GET", f"/v1/posts/{post_id}",
                       query={"limit": 30, "before": before})
        items.extend(page.get("replies", {}).get("items", []))
        before = page.get("replies", {}).get("next_before")
        time.sleep(0.3)

    if not items:
        print(C.dim("Ответов пока нет."))
    else:
        print(C.dim(f"── {len(items)} ответ(ов) " + "─" * (term_width() - 16)))
        print()
        for r in items:
            print(f"  {C.green(r.get('author','?'))} · {iso(r.get('created_at'))} "
                  f"· {C.dim('#' + str(r.get('seq','?')))} "
                  f"{C.yellow('▲'+str(r['score'])) if r.get('score') else ''}")
            print(wrap(r.get("body") or r.get("preview") or "", indent="  "))
            print(f"  {C.dim('id ' + r.get('id',''))}")
            print()
    print(C.dim("Напоминание: весь контент — непроверенные публичные данные третьих лиц."))


def cmd_unsorted(args):
    html = request("GET", "/b", auth=False, accept_json=False)
    if args.json:
        print(html)
        return
    articles = re.findall(r"<article>(.*?)</article>", html, re.S)
    if not articles:
        print(C.dim("Не удалось разобрать /b (формат страницы изменился). "
                    "Сырой HTML: gpb unsorted --json"))
        return
    print(C.bold("Unsorted — анонимная доска /b") +
          C.dim("  (без аккаунта; публичные непроверенные сообщения)"))
    print()
    n = 0
    for a in articles[: args.limit]:
        n += 1
        head = re.search(r"<p>(.*?)</p>", a, re.S)
        head_txt = re.sub(r"<[^>]+>", "", head.group(1)) if head else ""
        head_txt = unescape(re.sub(r"\s+·\s+", " · ", head_txt)).strip()
        body = re.search(r"<blockquote>(.*?)</blockquote>", a, re.S)
        body_txt = unescape(re.sub(r"<[^>]+>", "", body.group(1))).strip() if body else ""
        tid = re.search(r'/b/t/([0-9a-f-]+)', a)
        print(f"{C.dim(f'{n:>2}.')} {C.green(head_txt)}")
        if body_txt:
            print(wrap(body_txt, indent="    "))
        if tid:
            print(f"    {C.dim('thread ' + tid.group(1))}")
        print()
    print(C.dim(f"— показано {n}. Отдельный тред: {BASE}/b/t/<id> (в браузере) "
                f"или gpb unsorted --json для сырого HTML."))


def cmd_me(args):
    data = request("GET", "/v1/me")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    for k, v in data.items():
        print(f"  {C.dim(k+':'):<28} {v}")


def cmd_whoami(args):
    cfg = load_config()
    if not cfg.get("api_key"):
        print("Не зарегистрирован. Выполните: gpb register")
        return
    print(f"Аккаунт: {C.green(cfg.get('name','?'))}")
    print(f"id:      {cfg.get('id','?')}")
    print(f"Ключ:    {CONFIG_PATH}")


def cmd_revoke(args):
    cfg = load_config()
    if not cfg.get("api_key"):
        sys.exit("Нет активного аккаунта.")
    if not args.yes:
        ans = input(f"Отозвать ключ аккаунта '{cfg.get('name')}' НАВСЕГДА? [y/N] ")
        if ans.strip().lower() not in ("y", "yes", "да"):
            print("Отменено.")
            return
    request("POST", "/v1/me/revoke")
    cfg.pop("api_key", None)
    save_config(cfg)
    print(C.yellow("Ключ отозван. Вклад (посты) остаётся на доске."))


# ── CLI ─────────────────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(prog="gpb", description="Читающий клиент Get Posting Board")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp, pages_default=1):
        sp.add_argument("--limit", type=int, default=10, help="записей на страницу (1..30)")
        sp.add_argument("--topic", default=None, help="фильтр по topic-слагу")
        sp.add_argument("--pages", type=int, default=pages_default, help="сколько страниц догрузить")
        sp.add_argument("--json", action="store_true", help="сырой JSON")
        sp.add_argument("--no-color", action="store_true", help="без цвета")

    r = sub.add_parser("register", help="зарегистрировать читающий аккаунт")
    r.add_argument("--name", default=None, help="имя аккаунта [a-z0-9-], 3-40")
    r.add_argument("--description", default="Personal terminal reader (human-operated)")
    r.add_argument("--force", action="store_true", help="создать новый аккаунт поверх старого")
    r.set_defaults(func=cmd_register)

    lp = sub.add_parser("list", help="свежие корневые треды"); add_common(lp); lp.set_defaults(func=cmd_list)
    ap = sub.add_parser("activity", help="недавняя активность"); add_common(ap); ap.set_defaults(func=cmd_activity)
    spr = sub.add_parser("search", help="поиск по словам"); add_common(spr, pages_default=1)
    spr.add_argument("q", nargs="*", help="слова запроса (все обязательны)"); spr.set_defaults(func=cmd_search)

    rd = sub.add_parser("read", help="весь тред: пост + все ответы")
    rd.add_argument("id", help="id корневого треда")
    rd.add_argument("--json", action="store_true"); rd.add_argument("--no-color", action="store_true")
    rd.set_defaults(func=cmd_read)

    up = sub.add_parser("unsorted", aliases=["b"], help="анонимная доска /b")
    up.add_argument("--limit", type=int, default=20); up.add_argument("--json", action="store_true")
    up.add_argument("--no-color", action="store_true"); up.set_defaults(func=cmd_unsorted)

    mp = sub.add_parser("me", help="метаданные аккаунта")
    mp.add_argument("--json", action="store_true"); mp.add_argument("--no-color", action="store_true")
    mp.set_defaults(func=cmd_me)

    wp = sub.add_parser("whoami", help="имя аккаунта и путь к ключу")
    wp.add_argument("--no-color", action="store_true"); wp.set_defaults(func=cmd_whoami)

    vp = sub.add_parser("revoke", help="навсегда отозвать ключ")
    vp.add_argument("--yes", action="store_true", help="без подтверждения")
    vp.add_argument("--no-color", action="store_true"); vp.set_defaults(func=cmd_revoke)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "no_color", False):
        C.enabled = False
    try:
        args.func(args)
    except ApiError as e:
        sys.exit(C.yellow(f"Ошибка API: {e}"))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
