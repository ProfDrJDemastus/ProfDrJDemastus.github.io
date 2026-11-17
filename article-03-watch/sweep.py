#!/usr/bin/env python3
"""Article 03 search replica, weekly sweep.

Reruns the Web of Science core and adjacent searches of the Article 03
literature review ("A Neglected Discourse") in OpenAlex for the 13 Q1 and Q2
journals, covering all years through 2026. Every run is a full search, so
records OpenAlex indexes late or enriches later are picked up. Records seen
for the first time get today's date in "first_seen".

Writes data/records.json (all matching records) and appends to data/runs.json.
Standard library only. Needs OPENALEX_API_KEY in the environment.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.openalex.org"
SELECT = ("id,doi,title,publication_date,publication_year,type,language,"
          "authorships,abstract_inverted_index,keywords")
PAGE_SIZE = 200
MAX_PAGES = 40
PAUSE = 0.12


class ApiError(RuntimeError):
    pass


def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def short_id(x):
    return str(x or "").rsplit("/", 1)[-1]


def norm_doi(doi):
    d = str(doi or "").strip().lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", d)


class OpenAlex:
    def __init__(self, key, fetch=None):
        self.key = key
        self.requests = 0
        self._fetch = fetch or self._http

    @staticmethod
    def _http(url):
        req = urllib.request.Request(url, headers={"User-Agent": "article-03-watch (GitHub Actions)"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.load(resp)

    def get(self, params):
        params = dict(params)
        if self.key:
            params["api_key"] = self.key
        url = API + "/works?" + urllib.parse.urlencode(params, safe=":,/", quote_via=urllib.parse.quote)
        safe_url = url.replace(self.key, "***") if self.key else url
        for attempt in range(5):
            self.requests += 1
            try:
                return self._fetch(url)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(3 * 2 ** attempt)
                    continue
                hint = (" Check the OPENALEX_API_KEY secret." if e.code in (401, 403, 409, 429) else "")
                raise ApiError(f"HTTP {e.code} for {safe_url}.{hint}") from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                if attempt < 4:
                    time.sleep(3 * 2 ** attempt)
                    continue
                raise ApiError(f"Network error for {safe_url}: {e}") from None
        raise ApiError(f"Gave up on {safe_url}")

    def search(self, issn, term, until):
        out, cursor, pages = [], "*", 0
        flt = (f"locations.source.issn:{issn},"
               f'title_and_abstract.search:"{term}",to_publication_date:{until}')
        while cursor and pages < MAX_PAGES:
            res = self.get({"filter": flt, "select": SELECT, "per_page": PAGE_SIZE, "cursor": cursor})
            pages += 1
            batch = (res or {}).get("results") or []
            out.extend(batch)
            cursor = ((res or {}).get("meta") or {}).get("next_cursor")
            if not batch:
                break
            time.sleep(PAUSE)
        return out, bool(cursor) and pages >= MAX_PAGES


def abstract_text(inv):
    if not inv:
        return ""
    pos = sorted((i, w) for w, idxs in inv.items() for i in idxs)
    return " ".join(w for _, w in pos)


def classify(work, streams, sust_rx):
    """Return {stream: [terms]} for patterns found in title, abstract or keywords."""
    title = work.get("title") or ""
    abstract = abstract_text(work.get("abstract_inverted_index"))
    kws = " ".join(k.get("display_name", "") for k in (work.get("keywords") or []))
    text = f"{title} \n {abstract} \n {kws}"
    found = {}
    for sid, s in streams.items():
        hits = [name for name, rx in s.items() if rx.search(text)]
        if hits:
            found[sid] = hits
    return found, bool(abstract), bool(sust_rx.search(text))


def run(oa, today=None, log=print):
    today = (today or dt.datetime.now(dt.timezone.utc).date()).isoformat()
    journals = read_json(os.path.join(HERE, "config", "journals.json"), {})["journals"]
    cfg = read_json(os.path.join(HERE, "config", "search.json"), {})
    old = {r["id"]: r for r in read_json(os.path.join(HERE, "data", "records.json"), [])}
    runs = read_json(os.path.join(HERE, "data", "runs.json"), [])
    streams = {sid: {name: re.compile(p, re.I) for name, p in s["terms"].items()}
               for sid, s in cfg["streams"].items()}
    sust_rx = re.compile(cfg["sustainability_pattern"], re.I)
    keep_types = set(cfg["keep_types"])
    until = cfg["to_publication_date"]

    records, per_journal, zero_issns, capped, retrieved = {}, {}, [], [], 0
    for j in journals:
        jcount = 0
        for issn in j["issns"]:
            issn_hits = 0
            for term in cfg["query_terms"]:
                works, was_capped = oa.search(issn, term, until)
                retrieved += len(works)
                issn_hits += len(works)
                if was_capped:
                    capped.append(f"{j['name']} ({issn}, {term})")
                for w in works:
                    wid = short_id(w.get("id"))
                    if not wid or wid in records:
                        continue
                    if (w.get("type") or "") not in keep_types:
                        continue
                    found, has_abs, mentions_sust = classify(w, streams, sust_rx)
                    if not found:
                        continue
                    doi = norm_doi(w.get("doi"))
                    prev = old.get(wid)
                    records[wid] = {
                        "id": wid,
                        "doi": doi,
                        "title": (w.get("title") or "").strip() or "(untitled record)",
                        "authors": [a["author"]["display_name"] for a in (w.get("authorships") or [])
                                    if a.get("author") and a["author"].get("display_name")][:12],
                        "journal": j["name"],
                        "journal_id": j["id"],
                        "year": w.get("publication_year"),
                        "published": w.get("publication_date") or "",
                        "type": w.get("type") or "",
                        "language": w.get("language") or "",
                        "stream": "core" if "core" in found else "adjacent",
                        "terms": found,
                        "has_abstract": has_abs,
                        "mentions_sustainability": mentions_sust,
                        "first_seen": prev["first_seen"] if prev else today,
                    }
                    jcount += 1
            if issn_hits == 0:
                zero_issns.append(f"{j['name']} ({issn})")
        per_journal[j["id"]] = jcount
        log(f"{j['name']}: {jcount} records ({oa.requests} requests so far)")

    # Keep records seen before that a later search no longer returns, flagged as such.
    for wid, prev in old.items():
        if wid not in records:
            prev["missing_since"] = prev.get("missing_since") or today
            records[wid] = prev
        else:
            records[wid].pop("missing_since", None)

    out = sorted(records.values(), key=lambda r: (r["first_seen"], r["published"]), reverse=True)
    new = sum(1 for r in out if r["first_seen"] == today) if runs else 0
    runs.append({
        "date": today, "first_run": not runs, "ok": True,
        "requests": oa.requests, "retrieved": retrieved, "records": len(out),
        "core": sum(1 for r in out if r["stream"] == "core"),
        "adjacent": sum(1 for r in out if r["stream"] == "adjacent"),
        "new": new, "per_journal": per_journal,
        "issns_with_no_results": zero_issns, "capped": capped,
    })
    write_json(os.path.join(HERE, "data", "records.json"), out)
    write_json(os.path.join(HERE, "data", "runs.json"), runs)
    log(f"Run {today}: {len(out)} records, {new} new, {oa.requests} requests.")
    return runs[-1]


def main():
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not key:
        print("::warning::OPENALEX_API_KEY is not set, the sweep may be refused.")
    try:
        run(OpenAlex(key))
    except ApiError as e:
        print(f"::error::{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
