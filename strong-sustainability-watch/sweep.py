#!/usr/bin/env python3
"""Strong Sustainability Watch, weekly sweep.

Queries OpenAlex for new articles in the allow-listed journals, scores how
central strong sustainability is to each one, links each to the strong-side
theories of the sustainability matrix (Article 10, Figure 1), and appends the
results to data/articles.json. A record of every sweep goes to data/runs.json.

Run weekly by .github/workflows/strong-sustainability-watch.yml. Standard
library only. Set OPENALEX_API_KEY in the environment (a free key from
openalex.org); OpenAlex has required one since February 2026.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(HERE, "config")
DATA_DIR = os.path.join(HERE, "data")
API = "https://api.openalex.org"
SELECT = ("id,doi,title,publication_date,type,authorships,"
          "abstract_inverted_index,keywords,referenced_works")
PAGE_SIZE = 200
MAX_PAGES = 60
PAUSE = 0.15
MATRIX_BONUS_PER_THEORY = 0.75
MATRIX_BONUS_CAP = 1.5


class ApiError(RuntimeError):
    pass


# ---------------------------------------------------------------- files

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


# ---------------------------------------------------------------- OpenAlex

def short_id(openalex_id):
    return str(openalex_id or "").rsplit("/", 1)[-1]


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
        req = urllib.request.Request(
            url, headers={"User-Agent": "strong-sustainability-watch (GitHub Actions)"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.load(resp)

    def get(self, path, params):
        params = dict(params)
        if self.key:
            params["api_key"] = self.key
        url = API + path + "?" + urllib.parse.urlencode(params, safe=":,/")
        safe_url = url.replace(self.key, "***") if self.key else url
        for attempt in range(5):
            self.requests += 1
            try:
                return self._fetch(url)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(3 * 2 ** attempt)
                    continue
                hint = ""
                if e.code in (401, 403, 409, 429):
                    hint = (" OpenAlex refused or limited the request. Check that the "
                            "OPENALEX_API_KEY secret holds a valid free key from openalex.org.")
                raise ApiError(f"HTTP {e.code} for {safe_url}.{hint}") from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                if attempt < 4:
                    time.sleep(3 * 2 ** attempt)
                    continue
                raise ApiError(f"Network error for {safe_url}: {e}") from None
        raise ApiError(f"Gave up on {safe_url}")

    def works_for_issn(self, issn, since, until=None):
        out, cursor, pages = [], "*", 0
        flt = f"locations.source.issn:{issn},from_publication_date:{since}"
        if until:
            flt += f",to_publication_date:{until}"
        while cursor and pages < MAX_PAGES:
            res = self.get("/works", {
                "filter": flt, "select": SELECT, "per_page": PAGE_SIZE, "cursor": cursor})
            pages += 1
            if not res:
                break
            batch = res.get("results") or []
            out.extend(batch)
            cursor = (res.get("meta") or {}).get("next_cursor")
            if not batch:
                break
            time.sleep(PAUSE)
        return out, bool(cursor) and pages >= MAX_PAGES

    def work_by_doi(self, doi, select="id,display_name,publication_year"):
        path = "/works/doi:" + urllib.parse.quote(doi, safe="/:()")
        return self.get(path, {"select": select})

    def works_by_author(self, author_id, since):
        out, cursor, pages = [], "*", 0
        while cursor and pages < MAX_PAGES:
            res = self.get("/works", {
                "filter": f"author.id:{author_id},from_publication_date:{since}",
                "select": SELECT + ",primary_location", "per_page": PAGE_SIZE, "cursor": cursor})
            pages += 1
            batch = (res or {}).get("results") or []
            out.extend(batch)
            cursor = ((res or {}).get("meta") or {}).get("next_cursor")
            if not batch:
                break
            time.sleep(PAUSE)
        return out


# ---------------------------------------------------------------- text

def reconstruct_abstract(inv):
    if not inv:
        return ""
    pos = [(i, w) for w, idxs in inv.items() for i in idxs]
    pos.sort()
    return " ".join(w for _, w in pos)


def term_regex(term):
    if term.startswith("re:"):
        return re.compile(term[3:], re.I)
    body = re.escape(term.lower()).replace(r"\-", "[- ]?")
    return re.compile(r"\b" + body + r"\b", re.I)


class Scorer:
    def __init__(self, cfg):
        self.threshold = float(cfg["threshold"])
        core = set(cfg["core"])
        self.terms = []
        seen = set()
        for cat, terms in cfg["dictionary"].items():
            for t in terms:
                if t in seen:
                    continue
                seen.add(t)
                self.terms.append((cat, term_regex(t), t in core))
        for t in core - seen:
            self.terms.append(("Theoretical anchors", term_regex(t), True))
        self.counter = [term_regex(t) for t in cfg["counter"]]

    def score(self, title, body):
        t, b = title.lower(), body.lower()
        matched, cats, core_t, core_b = [], set(), 0, 0
        for cat, rx, is_core in self.terms:
            mt, mb = rx.search(t), rx.search(b)
            if not (mt or mb):
                continue
            label = (mt or mb).group(0).lower()
            if label not in matched:
                matched.append(label)
            cats.add(cat)
            if is_core:
                if mt:
                    core_t += 1
                else:
                    core_b += 1
        counter_hits = sum(1 for rx in self.counter if rx.search(t + " \n " + b))
        s = 2.5 * min(core_t, 2) + 1.0 * min(core_b, 3) + 0.6 * min(len(cats), 5)
        if core_t + core_b == 0:
            s -= 1.5 * min(counter_hits, 2)
        return max(0.0, min(10.0, s)), matched


class TheoryMatcher:
    def __init__(self, cfg, anchors):
        self.theories = []
        for th in cfg["theories"]:
            anchor_ids = {}
            for a in th.get("anchors", []):
                hit = anchors.get(norm_doi(a["doi"]))
                if hit and hit.get("id"):
                    anchor_ids[hit["id"]] = a["label"]
            self.theories.append({
                "id": th["id"],
                "patterns": [re.compile(p, re.I) for p in th.get("patterns", [])],
                "context": [(re.compile(a, re.I), re.compile(b, re.I))
                            for a, b in th.get("context", [])],
                "anchors": anchor_ids,
            })
        self.all_anchor_ids = {i for th in self.theories for i in th["anchors"]}

    def match(self, text, cited_ids):
        tags = []
        for th in self.theories:
            why, strength = [], 0
            for rx in th["patterns"]:
                m = rx.search(text)
                if m and len(why) < 2:
                    phrase = m.group(0).lower()
                    if f'mentions "{phrase}"' not in why:
                        why.append(f'mentions "{phrase}"')
                        strength += 1
            if not why:
                for a, b in th["context"]:
                    ma, mb = a.search(text), b.search(text)
                    if ma and mb:
                        why.append(f'mentions "{ma.group(0).lower()}" with "{mb.group(0).lower()}"')
                        strength += 1
                        break
            for aid, label in th["anchors"].items():
                if aid in cited_ids:
                    why.append(f"cites {label}")
                    strength += 2
            if why:
                tags.append({"id": th["id"], "why": why, "_s": strength})
        tags.sort(key=lambda x: -x["_s"])
        for t in tags:
            del t["_s"]
        return tags


def fold(s):
    """Lower-case and strip accents, for comparing author names."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(c)).lower()


def resolve_followed(oa, person):
    """OpenAlex author ids for a followed person, read from the authorships of their known works.
    OpenAlex sometimes splits one person over several ids, so every match is kept."""
    surname = fold(person["name"].split()[-1])
    ids = set()
    for doi in person.get("known_works", []):
        w = oa.work_by_doi(norm_doi(doi), select="id,authorships")
        for a in (w or {}).get("authorships") or []:
            au = a.get("author") or {}
            if au.get("id") and re.search(r"\b" + re.escape(surname) + r"\b", fold(au.get("display_name"))):
                ids.add(short_id(au["id"]))
    return sorted(ids)


def year_chunks(since, today):
    """Split a long window into calendar years so big journals stay under the page cap.
    The last chunk has no end date, so articles dated ahead of today are still caught."""
    chunks, start = [], since
    while start.year < today.year:
        chunks.append((start, dt.date(start.year, 12, 31)))
        start = dt.date(start.year + 1, 1, 1)
    chunks.append((start, None))
    return chunks


def watched_hits(authors, surnames):
    hits = []
    for s in surnames:
        rx = re.compile(r"\b" + re.escape(s) + r"\b", re.I)
        if any(rx.search(a) for a in authors):
            hits.append(s)
    return hits


def evaluate(entry, scorer, matcher, surnames):
    """Score and tag an entry dict holding title, abstract, keywords, cited."""
    body = entry.get("abstract", "") + " \n " + " ".join(entry.get("keywords", []))
    base, terms = scorer.score(entry["title"], body)
    text = (entry["title"] + " \n " + body).lower()
    theories = matcher.match(text, set(entry.get("cited", [])))
    bonus = min(MATRIX_BONUS_CAP, MATRIX_BONUS_PER_THEORY * len(theories))
    entry["score"] = round(min(10.0, base + bonus), 1)
    entry["terms"] = terms
    entry["theories"] = theories
    entry["watched"] = watched_hits(entry["authors"], surnames)
    return entry


def entry_from_work(w, journal, matcher, today):
    refs = {short_id(r) for r in (w.get("referenced_works") or [])}
    doi = norm_doi(w.get("doi"))
    return {
        "id": short_id(w.get("id")),
        "doi": doi,
        "url": ("https://doi.org/" + doi) if doi else w.get("id"),
        "title": (w.get("title") or "").strip() or "(untitled record)",
        "authors": [a["author"]["display_name"] for a in (w.get("authorships") or [])
                    if a.get("author") and a["author"].get("display_name")],
        "journal": journal["name"],
        "journal_id": journal["id"],
        "quartile": journal.get("quartile", ""),
        "published": w.get("publication_date") or "",
        "type": w.get("type") or "",
        "added": today,
        "abstract": reconstruct_abstract(w.get("abstract_inverted_index")),
        "keywords": [k.get("display_name", "") for k in (w.get("keywords") or [])],
        "cited": sorted(refs & matcher.all_anchor_ids),
    }


# ---------------------------------------------------------------- main

def run(oa, today=None, log=print):
    today = today or dt.datetime.now(dt.timezone.utc).date()
    journals = read_json(os.path.join(CONFIG_DIR, "journals.json"), {})["journals"]
    theories_cfg = read_json(os.path.join(CONFIG_DIR, "theories.json"), {})
    scoring = read_json(os.path.join(CONFIG_DIR, "scoring.json"), {})
    articles = read_json(os.path.join(DATA_DIR, "articles.json"), [])
    runs = read_json(os.path.join(DATA_DIR, "runs.json"), [])
    anchors = read_json(os.path.join(DATA_DIR, "anchors.json"), {})

    # 1. Resolve each theory's anchor works to OpenAlex ids (singleton lookups are free).
    for th in theories_cfg["theories"]:
        for a in th.get("anchors", []):
            doi = norm_doi(a["doi"])
            if anchors.get(doi):
                continue
            res = oa.work_by_doi(doi)
            anchors[doi] = ({"id": short_id(res.get("id")), "title": res.get("display_name"),
                             "year": res.get("publication_year"), "label": a["label"]}
                            if res else None)
            if not res:
                log(f"Anchor not found in OpenAlex: {a['label']} ({doi})")
    resolved = sum(1 for v in anchors.values() if v)

    scorer = Scorer(scoring)
    matcher = TheoryMatcher(theories_cfg, anchors)
    always_rx = [term_regex(t) for t in scoring.get("always_include", [])]

    def always_listed(entry):
        text = entry["title"] + " \n " + entry.get("abstract", "") + " \n " + " ".join(entry.get("keywords", []))
        return any(rx.search(text) for rx in always_rx)
    surnames = scoring.get("watched_authors", [])
    keep_types = set(scoring.get("keep_types", ["article", "review"]))

    # 2. Window: from the last good sweep minus the lookback, or the backfill date.
    #    If backfill_from is moved earlier than anything swept so far, or rules_version is
    #    raised after a change to what gets listed, sweep back to backfill_from once.
    good = [r for r in runs if r.get("ok")]
    backfill_from = dt.date.fromisoformat(scoring["backfill_from"])
    covered_from = min((dt.date.fromisoformat(r["since"]) for r in good), default=None)
    rules_version = int(scoring.get("rules_version", 1))
    rules_changed = bool(good) and int(good[-1].get("rules_version", 1)) != rules_version
    if not good or backfill_from < covered_from or rules_changed:
        since, backfill = backfill_from, True
    else:
        last = dt.date.fromisoformat(good[-1]["date"])
        since = max(backfill_from, last - dt.timedelta(days=int(scoring["lookback_days"])))
        backfill = False
    chunks = year_chunks(since, today)

    # 3. Sweep every ISSN of every journal separately (pipe-OR filters proved unreliable).
    known_ids = {a["id"] for a in articles}
    known_dois = {a["doi"] for a in articles if a.get("doi")}
    processed, retrieved, evaluated, added, capped, issns = set(), 0, 0, [], [], 0
    for j in journals:
        for issn in j["issns"]:
            issns += 1
            works = []
            for start, end in chunks:
                part, was_capped = oa.works_for_issn(issn, start.isoformat(), end.isoformat() if end else None)
                works.extend(part)
                if was_capped:
                    capped.append(f"{j['name']} ({issn}, from {start.isoformat()})")
            retrieved += len(works)
            for w in works:
                wid = short_id(w.get("id"))
                if not wid or wid in processed:
                    continue
                processed.add(wid)
                if (w.get("type") or "") not in keep_types:
                    continue
                doi = norm_doi(w.get("doi"))
                if wid in known_ids or (doi and doi in known_dois):
                    continue
                evaluated += 1
                entry = evaluate(entry_from_work(w, j, matcher, today.isoformat()),
                                 scorer, matcher, surnames)
                if entry["score"] >= scorer.threshold or always_listed(entry):
                    if backfill:
                        entry["backfilled"] = True
                    added.append(entry)
                    known_ids.add(wid)
                    if doi:
                        known_dois.add(doi)
        log(f"{j['name']}: done ({oa.requests} requests so far)")

    # 3b. Followed authors: list all their new work, in any venue, whatever its score.
    by_id = {a["id"]: a for a in articles + added}
    issn_to_journal = {i.upper(): j for j in journals for i in j["issns"]}
    followed_types = set(scoring.get("followed_types", ["article", "review", "book-chapter"]))
    followed_log = {}
    for person in scoring.get("followed_authors", []):
        ids = resolve_followed(oa, person)
        n = 0
        for aid in ids:
            for w in oa.works_by_author(aid, since.isoformat()):
                wid = short_id(w.get("id"))
                doi = norm_doi(w.get("doi"))
                if not wid or (w.get("type") or "") not in followed_types:
                    continue
                entry = by_id.get(wid) or next((a for a in articles + added if doi and a.get("doi") == doi), None)
                if entry is None:
                    src = ((w.get("primary_location") or {}).get("source") or {})
                    j = next((issn_to_journal[i.upper()] for i in (src.get("issn") or []) if i.upper() in issn_to_journal), None)
                    j = j or {"id": "other:" + short_id(src.get("id")), "name": src.get("display_name") or "Other venue", "quartile": ""}
                    entry = evaluate(entry_from_work(w, j, matcher, today.isoformat()), scorer, matcher, surnames)
                    if backfill:
                        entry["backfilled"] = True
                    added.append(entry)
                    by_id[wid] = entry
                    n += 1
                if person["name"] not in entry.setdefault("followed", []):
                    entry["followed"].append(person["name"])
        followed_log[person["name"]] = {"openalex_ids": ids, "added": n}
        log(f"Followed {person['name']}: {len(ids)} OpenAlex ids, {n} works added")

    # 4. Re-score earlier entries so edits to the config files carry through.
    for a in articles:
        evaluate(a, scorer, matcher, surnames)

    articles.extend(added)
    articles.sort(key=lambda a: (a["added"], a["published"]), reverse=True)
    runs.append({
        "date": today.isoformat(), "since": since.isoformat(), "backfill": backfill,
        "ok": True, "journals": len(journals), "issns": issns, "requests": oa.requests,
        "retrieved": retrieved, "unique": len(processed), "evaluated": evaluated,
        "added": len(added), "total": len(articles), "capped": capped,
        "anchors_resolved": f"{resolved} of {len(anchors)}",
        "threshold": scorer.threshold, "rules_version": rules_version,
        "followed": followed_log,
    })
    write_json(os.path.join(DATA_DIR, "anchors.json"), anchors)
    write_json(os.path.join(DATA_DIR, "articles.json"), articles)
    write_json(os.path.join(DATA_DIR, "runs.json"), runs)
    log(f"Sweep {today}: since {since}, {retrieved} records retrieved, "
        f"{evaluated} evaluated, {len(added)} added, {len(articles)} listed.")
    return runs[-1]


def main():
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not key:
        print("::warning::OPENALEX_API_KEY is not set. OpenAlex allows only a small "
              "keyless allowance, so this sweep may fail. Add a free key from openalex.org "
              "as a repository secret named OPENALEX_API_KEY.")
    try:
        run(OpenAlex(key))
    except ApiError as e:
        print(f"::error::{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
