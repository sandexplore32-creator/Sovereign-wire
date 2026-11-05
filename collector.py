#!/usr/bin/env python3
"""Collect local news, official publications and X posts -> Claude scores relevance
for a sovereign bond trader, summarizes and translates to English -> feed.json.

Setup:  pip install feedparser trafilatura anthropic requests
        export ANTHROPIC_API_KEY=sk-ant-...
        export X_BEARER_TOKEN=...      # optional; X posts are skipped without it
Run:    python collector.py            (cron, e.g. every 30 minutes)
Serve:  keep index.html and feed.json in one folder and host that folder.
"""
import json, os, re, datetime as dt
from urllib.parse import urlparse
import feedparser, trafilatura, anthropic, requests
from lxml import html as lh

MODEL = "claude-haiku-5-5"
MIN_SCORE = {"news": 3, "official": 2, "x": 3}   # official items get a lower bar
KEEP_DAYS, MAX_PER_COUNTRY = 7, 80
OUT, SEEN, XIDS = "feed.json", "seen.json", "x_ids.json"
X_TOKEN = os.environ.get("X_BEARER_TOKEN")

# I could not test these live. After the first run, read the per-source counts
# printed at the end: a source showing 0 has a dead URL or handle to fix.
# "page" sources watch a listing page for new links; "rss" sources use a feed.
SOURCES = {
    "argentina": {
        "news": [
            ("Ambito", "https://www.ambito.com/rss/pages/economia.xml"),
            ("El Cronista", "https://www.cronista.com/files/rss/economia.xml"),
            ("La Nacion", "https://www.lanacion.com.ar/arc/outboundfeeds/rss/category/economia/?outputType=xml"),
            ("Clarin", "https://www.clarin.com/rss/economia/"),
            ("Pagina 12", "https://www.pagina12.com.ar/rss/secciones/economia/notas"),
            ("Infobae", "https://www.infobae.com/arc/outboundfeeds/rss/category/economia/"),
            ("Buenos Aires Times", "https://www.batimes.com.ar/feed"),
        ],
        "official": [
            ("BCRA", "https://www.bcra.gob.ar/noticias/", "page"),
            ("Ministerio de Economia", "https://www.argentina.gob.ar/economia", "page"),
            ("INDEC", "https://www.indec.gob.ar", "page"),
        ],
        "x": ["BCRA", "LuisCaputoAR", "JMilei", "MinEconomia_Ar"],
    },
    "venezuela": {
        "news": [
            ("Banca y Negocios", "https://www.bancaynegocios.com/feed/"),
            ("Finanzas Digital", "https://finanzasdigital.com/feed/"),
            ("El Nacional", "https://www.elnacional.com/feed/"),
            ("Efecto Cocuyo", "https://efectococuyo.com/feed/"),
            ("TalCual", "https://talcualdigital.com/feed/"),
            ("Runrunes", "https://runrun.es/feed/"),
            ("El Pitazo", "https://elpitazo.net/feed/"),
            ("Ultimas Noticias", "https://ultimasnoticias.com.ve/feed/"),  # state-aligned: shows the government line
        ],
        "official": [
            ("BCV", "https://www.bcv.org.ve", "page"),
            ("US Treasury OFAC", "https://ofac.treasury.gov/recent-actions", "page"),  # sanctions drive Venezuelan debt
        ],
        "x": ["BCV_ORG_VE"],
    },
    "dominican-republic": {
        "news": [
            ("El Dinero", "https://eldinero.com.do/feed/"),
            ("Diario Libre", "https://www.diariolibre.com/rss/economia.xml"),
            ("Listin Diario", "https://listindiario.com/rss/economia"),
            ("Diario Financiero", "https://diariofinanciero.do/feed/"),
            ("Acento", "https://acento.com.do/feed/"),
            ("Hoy", "https://hoy.com.do/feed/"),
            ("Dominican Today", "https://dominicantoday.com/feed/"),
        ],
        "official": [
            ("Banco Central RD", "https://www.bancentral.gov.do", "page"),
            ("Hacienda", "https://hacienda.gob.do", "page"),
            ("Credito Publico", "https://www.creditopublico.gob.do", "page"),
            ("DGII", "https://dgii.gov.do", "page"),
        ],
        "x": ["BancoCentralRD", "luisabinader", "haciendaRD"],
    },
}

SYSTEM = """You are a research assistant for a trader of Latin American sovereign bonds.
The item is a news article, an official publication (central bank, ministry, statistics
or debt office), or a post on X, usually in Spanish. Decide how relevant it is to the
pricing or risk of that country's sovereign debt: fiscal policy and budgets, debt
management, auctions and issuance, monetary policy and inflation, FX and reserves, IMF
or creditor talks, ratings, sanctions, elections and political risk, major economic data.
Official announcements of auctions, rate decisions, data releases and debt operations are
highly relevant. Navigation pages, sports, crime and entertainment score 1.
Reply with ONLY a JSON object, no markdown:
{"score": 1-5, "topic": "Fiscal|Monetary|Political|External|Ratings|Debt management|Economy",
 "title_en": "...", "bullets": ["1-3 short English bullets with concrete facts and numbers"],
 "why": "one sentence on why it matters for the country's bonds"}
Do not invent facts that are not in the item."""


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def now():
    return dt.datetime.now(dt.timezone.utc)


def body_text(url, fallback=""):
    try:
        body = trafilatura.extract(trafilatura.fetch_url(url))
        if body:
            return body[:6000]
    except Exception:
        pass
    return re.sub(r"<[^>]+>", " ", fallback)[:3000]


def page_links(url):
    raw = trafilatura.fetch_url(url)
    if not raw:
        return []
    doc = lh.fromstring(raw, base_url=url)
    doc.make_links_absolute(url)
    host, out, got = urlparse(url).netloc, [], set()
    for a in doc.iter("a"):
        href, title = a.get("href"), " ".join(a.text_content().split())
        if href and len(title) >= 30 and urlparse(href).netloc == host and href not in got:
            got.add(href)
            out.append((title, href))
    return out[:25]


def x_posts(handle, ids):
    h = {"Authorization": f"Bearer {X_TOKEN}"}
    if handle not in ids:
        r = requests.get(f"https://api.twitter.com/2/users/by/username/{handle}", headers=h, timeout=20)
        ids[handle] = r.json()["data"]["id"]
    r = requests.get(f"https://api.twitter.com/2/users/{ids[handle]}/tweets", headers=h, timeout=20,
                     params={"max_results": 10, "exclude": "retweets,replies", "tweet.fields": "created_at"})
    return [(t["text"], f"https://x.com/{handle}/status/{t['id']}", t["created_at"])
            for t in r.json().get("data", [])]


def candidates(cfg, ids):
    """Yield (kind, source, title, link, text_or_None, iso_date_or_None)."""
    for name, url in cfg.get("news", []):
        for e in feedparser.parse(url).entries[:25]:
            t = e.get("published_parsed")
            yield ("news", name, e.get("title", ""), e.get("link"), e.get("summary", ""),
                   dt.datetime(*t[:6], tzinfo=dt.timezone.utc).isoformat() if t else None)
    for name, url, mode in cfg.get("official", []):
        try:
            if mode == "rss":
                for e in feedparser.parse(url).entries[:25]:
                    yield ("official", name, e.get("title", ""), e.get("link"), e.get("summary", ""), None)
            else:
                for title, link in page_links(url):
                    yield ("official", name, title, link, None, None)
        except Exception as ex:
            print(f"  {name} failed: {ex}")
    for handle in cfg.get("x", []) if X_TOKEN else []:
        try:
            for text, link, created in x_posts(handle, ids):
                yield ("x", "@" + handle, text[:110], link, text, created)
        except Exception as ex:
            print(f"  @{handle} failed (check the handle / API access): {ex}")


def analyze(client, country, kind, title, text):
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=500, system=SYSTEM,
            messages=[{"role": "user", "content": f"Country: {country}\nType: {kind}\nTitle: {title}\n\n{text}"}])
        raw = re.sub(r"^```(?:json)?|```$", "", msg.content[0].text.strip()).strip()
        return json.loads(raw)
    except Exception as e:
        print("  analysis failed:", e)
        return None


def main():
    client = anthropic.Anthropic()
    feed = load(OUT, {"countries": {}})
    seen, ids = set(load(SEEN, [])), load(XIDS, {})
    cutoff = now() - dt.timedelta(days=KEEP_DAYS)
    if not X_TOKEN:
        print("X_BEARER_TOKEN not set: skipping X posts")

    for cid, cfg in SOURCES.items():
        print(cid)
        items = feed["countries"].setdefault(cid, [])
        stats = {}
        for kind, src, title, link, text, when in candidates(cfg, ids):
            stats[(kind, src)] = stats.get((kind, src), 0) + (1 if link else 0)
            if not link or link in seen:
                continue
            seen.add(link)
            if text is None or kind == "official":
                text = body_text(link, text or "")
            r = analyze(client, cid, kind, title, text)
            if not r or int(r.get("score", 0)) < MIN_SCORE[kind]:
                continue
            items.append({"t": r["title_en"], "o": title, "u": link, "s": src, "k": kind,
                          "d": when or now().isoformat(), "sc": int(r["score"]),
                          "tp": r.get("topic", "Economy"), "b": r.get("bullets", []),
                          "w": r.get("why", "")})
        for name in [(k, n) for k in ('news', 'official') for n, *_ in cfg.get(k, [])] + [('x', '@' + h) for h in cfg.get('x', [])]:
            print(f"  {name[0]:8} {name[1]:24} {stats.get(name, 0)} items seen")
        keep = [a for a in items if dt.datetime.fromisoformat(a["d"]) > cutoff]
        keep.sort(key=lambda a: (a["sc"], a["d"]), reverse=True)
        feed["countries"][cid] = keep[:MAX_PER_COUNTRY]

    feed["updated"] = now().isoformat()
    for path, data in ((OUT, feed), (SEEN, sorted(seen)[-8000:]), (XIDS, ids)):
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
