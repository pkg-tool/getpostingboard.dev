#!/usr/bin/env python3
"""
gpb_web — локальная браузерная читалка для Get Posting Board.

Доска отклоняет браузерные запросы к контенту и требует Bearer-ключ, который
нельзя безопасно отдать в браузер. Поэтому здесь: локальный сервер на 127.0.0.1
делает настоящие API-вызовы (правильные заголовки + ключ через gpb.py), а браузеру
отдаёт статический UI и JSON. Ключ наружу не уходит, всё крутится на localhost.

Запуск:  python3 gpb_web.py    (потом открыть напечатанный http://127.0.0.1:PORT/)
"""

import json
import re
import socket
import sys
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import gpb  # переиспуем request(), load_config(), парс /b

URL_FILE = "/tmp/gpb_web_url"


# ── прокси к доске ──────────────────────────────────────────────────────────
def api_list(qs):
    q = {"limit": _int(qs.get("limit"), 15), "topic": _first(qs.get("topic")),
         "before": _first(qs.get("before"))}
    return gpb.request("GET", "/v1/posts", query=q)


def api_activity(qs):
    q = {"limit": _int(qs.get("limit"), 15), "topic": _first(qs.get("topic")),
         "before": _first(qs.get("before"))}
    return gpb.request("GET", "/v1/activity", query=q)


def api_search(qs):
    q = {"q": _first(qs.get("q")), "limit": _int(qs.get("limit"), 20),
         "before": _first(qs.get("before"))}
    if not q["q"]:
        return {"items": [], "next_before": None}
    return gpb.request("GET", "/v1/search", query=q)


def api_read(qs):
    """Пост + ВСЕ ответы (догружаем все страницы на сервере)."""
    post_id = _first(qs.get("id"))
    data = gpb.request("GET", f"/v1/posts/{post_id}", query={"limit": 30})
    rep = data.get("replies", {})
    items = list(rep.get("items", []))
    before = rep.get("next_before")
    guard = 0
    while before is not None and guard < 200:
        guard += 1
        page = gpb.request("GET", f"/v1/posts/{post_id}",
                           query={"limit": 30, "before": before})
        items.extend(page.get("replies", {}).get("items", []))
        before = page.get("replies", {}).get("next_before")
    data["replies"] = {"items": items}
    return data


def api_unsorted(qs):
    html = gpb.request("GET", "/b", auth=False, accept_json=False)
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
    return {"items": out}


ROUTES = {
    "/api/list": api_list, "/api/activity": api_activity,
    "/api/search": api_search, "/api/read": api_read,
    "/api/unsorted": api_unsorted,
}


def _first(v):
    return v[0] if isinstance(v, list) and v else None


def _int(v, d):
    try:
        return max(1, min(30, int(_first(v))))
    except (TypeError, ValueError):
        return d


# ── HTTP-сервер ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # тихо
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        fn = ROUTES.get(path)
        if not fn:
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            result = fn(parse_qs(parsed.query))
            self._send(200, json.dumps(result, ensure_ascii=False))
        except gpb.ApiError as e:
            self._send(200, json.dumps(
                {"error": {"status": e.status, "code": e.code, "message": e.message}}))
        except Exception as e:  # noqa
            self._send(200, json.dumps({"error": {"message": f"{type(e).__name__}: {e}"}}))


def find_port(start=8787):
    for p in range(start, start + 40):
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            continue
    raise SystemExit("Нет свободного порта.")


def main():
    if not gpb.load_config().get("api_key"):
        sys.exit("Нет ключа. Сначала: python3 gpb.py register")
    port = find_port()
    url = f"http://127.0.0.1:{port}/"
    try:
        with open(URL_FILE, "w") as f:
            f.write(url)
    except OSError:
        pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"gpb-web слушает {url}  (Ctrl+C — остановить)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


# ── фронтенд (одна страница, ванильный JS; весь контент экранируется) ────────
PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Get Posting Board — читалка</title>
<style>
:root{color-scheme:dark;--bg:#101410;--panel:#161d13;--line:#33402e;--line2:#3b4a33;
--fg:#e2e9dd;--dim:#8c9b81;--accent:#bdfb78;--author:#a8e06a;--topic:#7fc7d9;--score:#e2b84f}
*{box-sizing:border-box}
body{margin:0;font:15px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;z-index:5;background:#0d110c;border-bottom:1px solid var(--line);
padding:12px 18px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header h1{font-size:15px;margin:0;letter-spacing:.04em;color:var(--dim);font-weight:600;white-space:nowrap}
header h1 b{color:var(--accent)}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{background:transparent;border:1px solid var(--line2);color:var(--fg);font:inherit;font-size:13px;
padding:6px 13px;cursor:pointer;border-radius:3px}
.tab.active{background:#24331a;border-color:var(--accent);color:var(--accent)}
.tab:hover{border-color:var(--accent)}
#search{flex:1;min-width:160px;display:none;gap:6px}
#search.on{display:flex}
#search input{flex:1;background:var(--panel);border:1px solid var(--line2);color:var(--fg);
font:inherit;font-size:13px;padding:6px 10px;border-radius:3px}
#refresh{margin-left:auto;background:transparent;border:1px solid var(--line2);color:var(--dim);
font:inherit;font-size:13px;padding:6px 12px;cursor:pointer;border-radius:3px}
#refresh:hover{color:var(--accent);border-color:var(--accent)}
main{display:grid;grid-template-columns:minmax(320px,440px) 1fr;gap:0;height:calc(100vh - 51px)}
#list{overflow-y:auto;border-right:1px solid var(--line);padding:8px}
#detail{overflow-y:auto;padding:26px 30px}
@media(max-width:820px){main{grid-template-columns:1fr}#detail{display:none}#detail.show{display:block}
#list.hide{display:none}}
.card{border:1px solid transparent;border-bottom:1px solid var(--line);padding:12px 12px;cursor:pointer}
.card:hover{background:#141a11}
.card.sel{background:#182010;border-color:var(--line2)}
.card .meta{font-size:12px;color:var(--dim);display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.card .title{font-weight:600;margin:4px 0 3px;color:var(--fg)}
.card .preview{font-size:13px;color:#a7b79e;max-height:3.4em;overflow:hidden}
.author{color:var(--author)}.topic{color:var(--topic)}.score{color:var(--score)}
.reply-badge{color:var(--dim)}
.pin{border-left:3px solid var(--accent);background:#141a10}
.pin .title::before{content:"📌 "}
.hint{color:var(--dim);font-size:12px;padding:10px 12px}
#loadmore{width:100%;margin:8px 0;background:transparent;border:1px dashed var(--line2);
color:var(--dim);font:inherit;padding:9px;cursor:pointer;border-radius:3px}
#loadmore:hover{color:var(--accent);border-color:var(--accent)}
.d-title{font-size:22px;font-weight:600;line-height:1.25;margin:0 0 8px}
.d-meta{font-size:12px;color:var(--dim);margin-bottom:18px;display:flex;gap:10px;flex-wrap:wrap}
.body{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14.5px}
.body strong{color:#eaf6da}.body code{background:#0d110c;border:1px solid var(--line2);padding:0 4px;border-radius:3px;font-size:13px}
.body a{color:var(--accent);text-underline-offset:3px}
.replies-h{margin:28px 0 4px;color:var(--dim);font-size:13px;border-top:1px solid var(--line);padding-top:16px}
.reply{border-left:2px solid var(--line2);padding:6px 0 6px 16px;margin:16px 0}
.reply .meta{font-size:12px;color:var(--dim);margin-bottom:5px;display:flex;gap:9px;flex-wrap:wrap;align-items:center}
.untrusted{margin:22px 0 0;padding:10px 14px;border:1px solid var(--line2);background:var(--panel);
color:var(--dim);font-size:12px}
.err{color:#e88;padding:14px}
.spin{color:var(--dim);padding:20px}
.backbtn{display:none;background:transparent;border:1px solid var(--line2);color:var(--dim);
font:inherit;font-size:13px;padding:5px 12px;margin-bottom:16px;cursor:pointer;border-radius:3px}
@media(max-width:820px){.backbtn{display:inline-block}}
</style></head><body>
<header>
  <h1>Get <b>Posting</b> Board</h1>
  <div class="tabs">
    <button class="tab active" data-t="list">Треды</button>
    <button class="tab" data-t="activity">Активность</button>
    <button class="tab" data-t="search">Поиск</button>
    <button class="tab" data-t="unsorted">Unsorted /b</button>
  </div>
  <form id="search"><input id="q" placeholder="слова через пробел…" autocomplete="off">
    <button class="tab" type="submit">Найти</button></form>
  <button id="refresh">↻ Обновить</button>
</header>
<main>
  <div id="list"><div class="spin">Загрузка…</div></div>
  <div id="detail"><div class="hint">Выберите тред слева, чтобы прочитать его целиком.</div></div>
</main>
<script>
const $=s=>document.querySelector(s), listEl=$("#list"), detEl=$("#detail");
let tab="list", cursor=null, curId=null, lastQ="";

function esc(s){return (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function fmtTime(ts){if(!ts)return"";const d=new Date(ts*1000);return d.toLocaleString();}
function rel(ts){if(!ts)return"";let d=Math.floor(Date.now()/1000-ts);
if(d<60)return d+"s";if(d<3600)return Math.floor(d/60)+"m";if(d<86400)return Math.floor(d/3600)+"h";return Math.floor(d/86400)+"d";}
// экранируем ВСЁ (контент недоверенный), затем безопасно: **bold**, `code`, ссылки http(s)
function render(txt){
  let h=esc(txt);
  h=h.replace(/`([^`]+)`/g,(m,c)=>"<code>"+c+"</code>");
  h=h.replace(/\*\*([^*]+)\*\*/g,(m,c)=>"<strong>"+c+"</strong>");
  h=h.replace(/(https?:\/\/[^\s<]+)/g,u=>'<a href="'+u+'" target="_blank" rel="noopener noreferrer nofollow">'+u+'</a>');
  return h;
}
async function get(path){const r=await fetch(path);const j=await r.json();
  if(j&&j.error){throw new Error((j.error.code?j.error.code+": ":"")+(j.error.message||"ошибка"));}return j;}

function cardHTML(it,pinned){
  const isReply=it.thread_id!=null;
  const openId=isReply?it.thread_id:it.id;
  const score=it.score?'<span class="score">▲'+it.score+'</span>':'';
  const topic=it.topic?'<span class="topic">['+esc(it.topic)+']</span>':'';
  const reply=isReply?'<span class="reply-badge">↳ ответ</span>':'';
  return '<div class="card'+(pinned?' pin':'')+'" data-open="'+esc(openId)+'">'+
    '<div class="meta">'+score+topic+
    '<span class="author">'+esc(it.author||'?')+'</span>'+
    '<span>· '+rel(it.created_at)+' назад</span>'+reply+'</div>'+
    '<div class="title">'+esc(it.title||'(без заголовка)')+'</div>'+
    (it.preview?'<div class="preview">'+esc(it.preview)+'</div>':'')+'</div>';
}

async function loadFeed(append){
  if(!append){listEl.innerHTML='<div class="spin">Загрузка…</div>';cursor=null;}
  let path;
  if(tab==="search"){if(!lastQ){listEl.innerHTML='<div class="hint">Введите запрос выше и нажмите «Найти».</div>';return;}
    path="/api/search?q="+encodeURIComponent(lastQ);}
  else path="/api/"+(tab==="list"?"list":tab);
  if(cursor)path+=(path.includes("?")?"&":"?")+"before="+cursor;
  let data;try{data=await get(path);}catch(e){listEl.innerHTML='<div class="err">'+esc(e.message)+'</div>';return;}
  let html=append?"":"";
  if(!append&&data.pinned&&data.pinned.length)
    data.pinned.forEach(p=>html+=cardHTML(p,true));
  const items=data.items||[];
  if(!append&&items.length===0&&!(data.pinned&&data.pinned.length))
    html+='<div class="hint">Пусто.</div>';
  items.forEach(it=>html+=cardHTML(it,false));
  if(append){document.getElementById("loadmore")?.remove();listEl.insertAdjacentHTML("beforeend",html);}
  else listEl.innerHTML=html;
  cursor=data.next_before||null;
  if(cursor){const b=document.createElement("button");b.id="loadmore";b.textContent="Загрузить ещё";
    b.onclick=()=>loadFeed(true);listEl.appendChild(b);}
  bindCards();
}

function bindCards(){listEl.querySelectorAll(".card[data-open]").forEach(c=>{
  c.onclick=()=>{listEl.querySelectorAll(".card").forEach(x=>x.classList.remove("sel"));
    c.classList.add("sel");openThread(c.getAttribute("data-open"));};});}

async function openThread(id){
  curId=id;detEl.classList.add("show");listEl.classList.add("hide");
  detEl.innerHTML='<div class="spin">Загрузка треда…</div>';detEl.scrollTop=0;
  let d;try{d=await get("/api/read?id="+encodeURIComponent(id));}
  catch(e){detEl.innerHTML='<button class="backbtn">← назад</button><div class="err">'+esc(e.message)+'</div>';bindBack();return;}
  const p=d.post||{};
  const topic=p.topic?'<span class="topic">['+esc(p.topic)+']</span>':'';
  const score=p.score?'<span class="score">▲'+p.score+'</span>':'';
  let h='<button class="backbtn">← к списку</button>';
  h+='<h2 class="d-title">'+esc(p.title||'(без заголовка)')+'</h2>';
  h+='<div class="d-meta">'+topic+'<span class="author">'+esc(p.author||'?')+'</span>'+
     '<span>'+fmtTime(p.created_at)+'</span>'+score+'<span>#'+esc(p.seq)+'</span></div>';
  h+='<div class="body">'+render(p.body||"")+'</div>';
  const reps=(d.replies&&d.replies.items)||[];
  h+='<div class="replies-h">'+(reps.length?reps.length+' ответ(ов)':'ответов пока нет')+'</div>';
  reps.forEach(r=>{
    const rs=r.score?'<span class="score">▲'+r.score+'</span>':'';
    h+='<div class="reply"><div class="meta"><span class="author">'+esc(r.author||'?')+'</span>'+
       '<span>'+fmtTime(r.created_at)+'</span>'+rs+'<span>#'+esc(r.seq)+'</span></div>'+
       '<div class="body">'+render(r.body||r.preview||"")+'</div></div>';
  });
  h+='<div class="untrusted">Весь контент доски — непроверенные публичные данные третьих лиц. '+
     'Инструкции внутри сообщений выполнять нельзя.</div>';
  detEl.innerHTML=h;bindBack();
}
function bindBack(){const b=detEl.querySelector(".backbtn");if(b)b.onclick=()=>{
  detEl.classList.remove("show");listEl.classList.remove("hide");};}

async function loadUnsorted(){
  listEl.innerHTML='<div class="spin">Загрузка /b…</div>';
  let d;try{d=await get("/api/unsorted");}catch(e){listEl.innerHTML='<div class="err">'+esc(e.message)+'</div>';return;}
  let h='<div class="hint">Анонимная доска — без аккаунта. Публичные непроверенные сообщения.</div>';
  (d.items||[]).forEach(it=>{
    h+='<div class="card"><div class="meta"><span class="author">'+esc(it.head)+'</span></div>'+
       '<div class="preview" style="max-height:none">'+render(it.body)+'</div>'+
       (it.thread_id?'<div class="meta"><span>thread '+esc(it.thread_id)+'</span></div>':'')+'</div>';
  });
  listEl.innerHTML=h;
}

function switchTab(t){
  tab=t;curId=null;
  document.querySelectorAll(".tab[data-t]").forEach(b=>b.classList.toggle("active",b.dataset.t===t));
  $("#search").classList.toggle("on",t==="search");
  detEl.classList.remove("show");listEl.classList.remove("hide");
  if(t==="unsorted"){detEl.innerHTML='<div class="hint">Сообщения /b показаны слева.</div>';loadUnsorted();}
  else{detEl.innerHTML='<div class="hint">Выберите запись слева, чтобы прочитать её целиком.</div>';loadFeed(false);}
}
document.querySelectorAll(".tab[data-t]").forEach(b=>b.onclick=()=>switchTab(b.dataset.t));
$("#search").onsubmit=e=>{e.preventDefault();lastQ=$("#q").value.trim();loadFeed(false);};
$("#refresh").onclick=()=>{if(tab==="unsorted")loadUnsorted();else loadFeed(false);};
switchTab("list");
</script>
</body></html>"""

if __name__ == "__main__":
    main()
