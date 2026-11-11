"""Score one posting by hand: from a link, or from a pasted job description.

Reading a link (official job-system feeds first, the same readers the daily run uses):
  Every job system and career site the
  daily readers support               -> that one posting from the site's own job data
  Any other page                          -> the job details the page publishes for search engines
                                             (schema.org "JobPosting"); nothing is pulled out of the
                                             page layout, so a description is never guessed
  LinkedIn, Indeed                        -> never fetched: paste the description instead
Every request respects robots.txt and goes through agent/http.py (pacing and retries).

A posting is only scored when its title, company and a full description (150+ words) were read,
and it doesn't look expired. Otherwise the person is asked to paste the description.

Filters are NOT applied to manual postings; each filter the posting would fail is shown as a
warning instead. The block list (personal.blocked in settings.yaml) still applies.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import List, Optional
from urllib.parse import parse_qs, urlsplit

from . import db
from .config import load_settings, resume_path
from .personal import blocked_host, blocked_word, is_blocked
from .fetchers import (HEADERS, Posting, amazon_posting, greenhouse_posting, html_to_text, iso_date,
                       lever_posting, top_of_pay_range)
from .filters import Filter

MIN_WORDS = 150
NEVER_FETCH = ("linkedin.com", "lnkd.in", "indeed.com")
EXPIRED = re.compile(r"no longer (accepting applications|available|open|active)|position (has been|is) filled|"
                     r"job (you are looking for )?(is|has been) (closed|removed|expired)|this job has expired|"
                     r"sign in to (view|see) (this|the) job", re.I)
BLOCKED_MSG = "is on your blocked list (personal.blocked in settings.yaml)"


class NeedsPaste(Exception):
    """The posting can't be read reliably from the link: ask for the pasted description."""


class Blocked(Exception):
    """The posting is from a company on the hard block list."""


# ------------------------------------------------------------------ helpers

def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def never_fetch(url: str) -> bool:
    h = _host(url)
    return any(_on(h, d) for d in NEVER_FETCH) or bool(re.match(r"^(?:[\w-]+\.)*indeed\.[a-z.]+$", h))


def normalize_url(url: str) -> str:
    """For spotting the same link twice: no scheme, 'www.', trailing slash or tracking parameters."""
    parts = urlsplit(url.strip())
    keep = {k: v for k, v in parse_qs(parts.query).items() if k.lower() in ("gh_jid", "ashby_jid", "jobid", "pid", "id")}
    q = "&".join(f"{k}={v[0]}" for k, v in sorted(keep.items()))
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host + parts.path.rstrip("/") + ("?" + q if q else "")


def words(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def _settings_company(settings: dict, **match) -> Optional[dict]:
    for c in settings.get("companies", []):
        if all(str(c.get(k, "")).lower() == str(v).lower() for k, v in match.items()):
            return c
    return None


def _name(settings: dict, fallback: str, **match) -> str:
    c = _settings_company(settings, **match)
    return c["name"] if c else fallback


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ------------------------------------------------------------------ site readers (one posting each)

def _read_greenhouse(settings, board: str, jid: str) -> Posting:
    from .fetchers import _get
    j = _get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}", pay_transparency="true")
    name = _name(settings, j.get("company_name") or "", board="greenhouse", board_id=board)
    p = greenhouse_posting(name, j)
    for rng in j.get("pay_input_ranges") or []:          # pay listed outside the description
        top = rng.get("max_cents")
        if top and (rng.get("currency_type") or "USD") == "USD":
            p.pay_top = max(p.pay_top or 0, top / 100)
    return p


def _read_lever(settings, board: str, jid: str) -> Posting:
    from .fetchers import _get
    j = _get(f"https://api.lever.co/v0/postings/{board}/{jid}")
    return lever_posting(_name(settings, "", board="lever", board_id=board), j)


def _read_ashby(settings, board: str, jid: str) -> Posting:
    from .fetchers import fetch_ashby
    company = _settings_company(settings, board="ashby", board_id=board) or {"name": "", "board_id": board}
    for p in fetch_ashby(company):
        if p.source_id == jid:
            return p
    raise NeedsPaste("That posting isn't on the company's Ashby job board any more (it may have closed).")


def _read_workday(settings, url: str) -> Posting:
    from .site_readers import _request
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = [x for x in parts.path.split("/") if x]
    if "job" not in path:
        raise NeedsPaste("That Workday link doesn't point to a single job.")
    k = path.index("job")
    if _on(host, "myworkdaysite.com") and "recruiting" in path:
        r = path.index("recruiting")
        tenant, site = path[r + 1], path[r + 2]
    else:
        tenant, site = host.split(".")[0], path[k - 1]
    data = _request("GET", f"https://{host}/wday/cxs/{tenant}/{site}/{'/'.join(path[k:])}")
    info = data.get("jobPostingInfo") or {}
    org = re.sub(r"^\d+\s+", "", (data.get("hiringOrganization") or {}).get("name", ""))
    name = _name(settings, org, board="workday", host=host)
    desc = html_to_text(info.get("jobDescription", ""))
    locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
    return Posting(company=name, source_id=info.get("jobReqId") or info.get("jobPostingId", ""),
                   title=(info.get("title") or "").strip(),
                   locations=[re.sub(r"^([A-Z]{2})-([A-Z]{2})-", r"\1, \2, ", l) for l in locs if l],
                   url=info.get("externalUrl") or url, description=desc, pay_top=top_of_pay_range(desc),
                   posted=iso_date(info.get("startDate")))


def _read_apple(settings, url: str) -> Posting:
    from .site_readers import _apple_data, _apple_location, _request, _strings
    d = (_apple_data(_request("GET", url, html=True)).get("jobDetails") or {}).get("jobsData") or {}
    parts = [d.get(k) or "" for k in ("jobSummary", "description", "minimumQualifications",
                                      "preferredQualifications")]
    parts += list(_strings(d.get("postingFooters") or []))          # pay and benefits text
    desc = "\n\n".join(html_to_text(t) for t in parts if t)
    return Posting(company=_name(settings, "Apple", board="apple"),
                   source_id=str(d.get("positionId") or d.get("jobNumber") or ""),
                   title=(d.get("postingTitle") or "").strip(),
                   locations=[_apple_location(l) for l in d.get("locations") or []], url=url,
                   description=desc, pay_top=top_of_pay_range(desc), posted=iso_date(d.get("postDateInGMT")))


def _read_google(settings, url: str) -> Posting:
    from .site_readers import _request, google_listed_under, google_posting
    html = _request("GET", url, html=True)
    m = re.search(r"AF_initDataCallback\(\{key: 'ds:0'.*?data:(.*?), sideChannel: \{\}\}\);</script>", html, re.S)
    if not m:
        raise NeedsPaste("Google's careers page didn't include the job details the agent reads.")
    j = (json.loads(m.group(1)) or [None])[0]
    if not j:
        raise NeedsPaste("That Google job wasn't found (it may have closed).")
    under = google_listed_under(j) or "Google"
    company = None
    for c in settings.get("companies", []):
        if c.get("board") == "google" and (c.get("only_company") or "Google").lower() == under.lower():
            company = c["name"]
    return google_posting(company or under, j)


def _read_amazon(settings, url: str) -> Posting:
    from .fetchers import _get
    m = re.search(r"/jobs/(\d+)", urlsplit(url).path)
    if not m:
        raise NeedsPaste("That amazon.jobs link doesn't point to a single job.")
    data = _get("https://www.amazon.jobs/en/search.json", base_query=m.group(1), result_limit=10)
    j = next((x for x in data.get("jobs", []) if str(x.get("id_icims")) == m.group(1)), None)
    if not j:
        raise NeedsPaste("That job wasn't found on amazon.jobs (it may have closed).")
    head = j.get("title", "") + " " + (j.get("company_name") or "") + " " + html_to_text(j.get("description") or "")[:600]
    word = blocked_word(head)
    if word:
        raise Blocked(f"This posting mentions {word}, which {BLOCKED_MSG}.")
    # Name it after the amazon_jobs company in settings that matches (same rules as the daily reader)
    desc = html_to_text(j.get("description") or "")
    for c in settings.get("companies", []):
        if c.get("board") != "amazon_jobs":
            continue
        cats = [x.lower() for x in c.get("business_categories") or []]
        must = c.get("must_mention")
        if (cats and (j.get("business_category") or "").lower() in cats) or \
                (must and re.search(rf"\b{re.escape(must)}\b", j.get("title", "") + " " + desc[:300])):
            return amazon_posting(c["name"], j)
    name = (j.get("company_name") or "").strip() or _host(url)
    if is_blocked(name):
        raise Blocked(f"This posting ({name}) {BLOCKED_MSG}.")
    return amazon_posting(name, j)


def _read_eightfold(settings, company: dict, url: str) -> Posting:
    from .site_readers import _request
    parts = urlsplit(url)
    m = re.search(r"/job/(\d+)", parts.path) or re.search(r"(?:^|&)pid=(\d+)", parts.query)
    if not m:
        raise NeedsPaste("That link doesn't point to a single job.")
    sid, base, domain = m.group(1), company["url"].rstrip("/"), company["domain"]
    if company.get("api") == "pcsx":
        d = _request("GET", f"{base}/api/pcsx/position_details",
                     params={"position_id": sid, "domain": domain, "hl": "en"}).get("data") or {}
        desc, title = html_to_text(d.get("jobDescription", "")), d.get("name", "")
    else:
        d = _request("GET", f"{base}/api/apply/v2/jobs/{sid}", params={"domain": domain})
        desc, title = html_to_text(d.get("job_description", "")), d.get("name", "")
    return Posting(company=company["name"], source_id=sid, title=(title or "").strip(),
                   locations=list(d.get("locations") or [d.get("location", "")]), url=url,
                   description=desc, pay_top=top_of_pay_range(desc))


def _read_oracle(settings, company: dict, url: str) -> Posting:
    from .site_readers import _request
    m = re.search(r"/job/(\d+)", url)
    if not m:
        raise NeedsPaste("That link doesn't point to a single job.")
    sid = m.group(1)
    d = _request("GET", f"{company['host'].rstrip('/')}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
                 params={"expand": "all", "onlyData": "true", "finder": f'ById;Id="{sid}",siteNumber={company["site"]}'})
    it = (d.get("items") or [{}])[0]
    desc = "\n\n".join(html_to_text(it.get(k) or "") for k in
                       ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                        "ExternalQualificationsStr", "CorporateDescriptionStr")).strip()
    locs = [it.get("PrimaryLocation", "")] + [l.get("Name", "") for l in it.get("secondaryLocations") or []]
    return Posting(company=company["name"], source_id=sid, title=(it.get("Title") or "").strip(),
                   locations=[l for l in locs if l], url=url, description=desc, pay_top=top_of_pay_range(desc),
                   posted=iso_date(it.get("ExternalPostedStartDate")))


def _from_list(company: dict, sid: str) -> Posting:
    """Readers whose list already includes full descriptions (Atlassian, Workable)."""
    from .site_readers import run_reader
    for p in run_reader(company):
        if p.source_id == sid:
            return p
    raise NeedsPaste("That posting isn't on the company's job board any more (it may have closed).")


def _job_posting_ld(html: str) -> Optional[dict]:
    for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for x in items:
            t = x.get("@type") if isinstance(x, dict) else None
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                return x
    return None


def _ld_location(loc) -> str:
    a = (loc or {}).get("address") or {}
    if isinstance(a, str):
        return a
    country = a.get("addressCountry")
    country = country.get("name", "") if isinstance(country, dict) else (country or "")
    return ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"), country) if x)


def _read_page(settings, url: str, company_hint: str = "") -> Posting:
    """Any other careers page: only its published schema.org JobPosting details are used."""
    from .site_readers import _request
    ld = _job_posting_ld(_request("GET", url, html=True))
    if not ld:
        raise NeedsPaste("This page doesn't publish its job details in a form the agent can read reliably.")
    org = ld.get("hiringOrganization") or {}
    org = org.get("name", "") if isinstance(org, dict) else str(org)
    locs_raw = ld.get("jobLocation") or []
    locs = [_ld_location(l) for l in (locs_raw if isinstance(locs_raw, list) else [locs_raw])]
    if str(ld.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        locs.append("Remote")
    desc = html_to_text(ld.get("description", ""))
    pay_top = None
    sal = ld.get("baseSalary") or {}
    val = sal.get("value") if isinstance(sal, dict) else None
    if isinstance(val, dict) and (sal.get("currency") or "USD") == "USD" and \
            str(val.get("unitText", "YEAR")).upper() == "YEAR":
        top = val.get("maxValue") or val.get("value")
        try:
            pay_top = float(top) if top else None
        except (TypeError, ValueError):
            pay_top = None
    valid = iso_date(ld.get("validThrough"))
    if valid and valid < dt.date.today().isoformat():
        raise NeedsPaste(f"This posting says it closed on {valid}.")
    ident = ld.get("identifier")
    sid = str(ident.get("value", "")) if isinstance(ident, dict) else str(ident or "")
    return Posting(company=company_hint or org, source_id=sid or "manual-" + _hash(normalize_url(url)),
                   title=html_to_text(ld.get("title", "")).strip(), locations=[l for l in locs if l], url=url,
                   description=desc, pay_top=pay_top or top_of_pay_range(desc), posted=iso_date(ld.get("datePosted")))


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def read_link(url: str, settings: dict, company_hint: str = "") -> Posting:
    """The posting at `url`, read from the site's own job data. Raises NeedsPaste or Blocked."""
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise NeedsPaste("That doesn't look like a web link (it should start with https://).")
    if never_fetch(url):
        raise NeedsPaste("The agent never reads LinkedIn or Indeed pages. Please paste the job description instead.")
    host, parts = _host(url), urlsplit(url)
    path = [x for x in parts.path.split("/") if x]
    q = parse_qs(parts.query)
    if blocked_host(host):
        raise Blocked(f"This career site {BLOCKED_MSG}.")
    try:
        if _on(host, "greenhouse.io"):
            if "for" in q and "token" in q:                      # embed links
                return _read_greenhouse(settings, q["for"][0], q["token"][0])
            if "jobs" in path:
                k = path.index("jobs")
                return _read_greenhouse(settings, path[k - 1], path[k + 1])
        if "gh_jid" in q:                                        # Greenhouse job on the company's own site
            for c in settings.get("companies", []):
                if c.get("board") == "greenhouse" and (_slug(c.get("board_id")) in _slug(host)
                                                       or _slug(c["name"]) in _slug(host)):
                    return _read_greenhouse(settings, c["board_id"], q["gh_jid"][0])
        if _on(host, "lever.co") and len(path) >= 2:
            return _read_lever(settings, path[0], path[1])
        if _on(host, "ashbyhq.com") and len(path) >= 2:
            return _read_ashby(settings, path[0], path[1])
        if "ashby_jid" in q:
            for c in settings.get("companies", []):
                if c.get("board") == "ashby" and (_slug(c.get("board_id")) in _slug(host)
                                                  or _slug(c["name"]) in _slug(host)):
                    return _read_ashby(settings, c["board_id"], q["ashby_jid"][0])
        if _on(host, "myworkdayjobs.com") or _on(host, "myworkdaysite.com"):
            return _read_workday(settings, url)
        if host == "jobs.apple.com":
            return _read_apple(settings, url)
        if _on(host, "google.com") and "/careers/" in parts.path:
            return _read_google(settings, url)
        if _on(host, "amazon.jobs"):
            return _read_amazon(settings, url)
        if _on(host, "atlassian.com") and "careers" in path:
            return _from_list(_settings_company(settings, board="atlassian") or
                              {"name": "Atlassian", "board": "atlassian"}, path[-1])
        if host == "apply.workable.com" and "j" in path:
            account = path[0]
            company = _settings_company(settings, board="workable", board_id=account) or \
                {"name": company_hint, "board": "workable", "board_id": account}
            return _from_list(company, path[path.index("j") + 1])
        for c in settings.get("companies", []):
            base = _host(c.get("url") or c.get("host") or "")
            if base and host == base and c.get("board") == "eightfold":
                return _read_eightfold(settings, c, url)
            if base and host == base and c.get("board") == "oracle":
                return _read_oracle(settings, c, url)
            if base and host == base and c.get("board") == "jibe" and "jobs" in path:
                # iCIMS Jibe (AMD, Rivian): the job page's published details, under the daily run's ID
                p = _read_page(settings, url, c["name"])
                p.source_id = path[path.index("jobs") + 1]
                return p
        return _read_page(settings, url, company_hint)
    except (NeedsPaste, Blocked):
        raise
    except Exception as e:      # network error, page format changed, robots.txt says no...
        raise NeedsPaste(f"The agent couldn't read that posting ({str(e)[:200]}).")


# ------------------------------------------------------------------ checks, warnings, scoring

def check_reliable(p: Posting, pasted: bool) -> None:
    """Never score a guessed or partial description."""
    if is_blocked(p.company):
        raise Blocked(f"{p.company} {BLOCKED_MSG}, so it wasn't scored.")
    if not p.title:
        raise NeedsPaste("The job title couldn't be read." if not pasted else "Please enter the job title.")
    if not p.company:
        raise NeedsPaste("The agent couldn't tell which company this is. Type the company name and try again."
                         if not pasted else "Please enter the company name.")
    n = words(p.description)
    if n < MIN_WORDS:
        raise NeedsPaste(f"Only {n} words of description were found, which looks partial. "
                         + ("Please paste the whole job description." if pasted else
                            "Please paste the job description instead."))
    if not pasted and EXPIRED.search(p.description[:800] + " " + p.description[-800:]):
        raise NeedsPaste("This page looks like an expired or sign-in page, not an open job.")


def from_paste(text: str, company: str, title: str, location: str, pay: str, url: str) -> Posting:
    pay_top = None
    m = re.sub(r"[^\d.kK]", "", pay or "")
    if m:
        try:
            pay_top = float(m[:-1]) * 1000 if m[-1:] in "kK" else float(m)
        except ValueError:
            pay_top = None
    desc = text.strip()
    sid = "manual-" + _hash(normalize_url(url) if url else desc)
    return Posting(company=company.strip(), source_id=sid, title=title.strip(),
                   locations=[location.strip()] if location.strip() else [], url=url.strip(), description=desc,
                   pay_top=pay_top or top_of_pay_range(desc))


def warnings_for(settings: dict, company: str, title: str, location: str, pay_top, score) -> List[str]:
    """Each daily filter this posting would have failed (shown, never applied)."""
    f = Filter(settings)
    out = []
    if not f.title_re.search(title or ""):
        out.append("Not a TPM/EPM title (your role_titles list)")
    bad = f.exclude_re.search(title or "")
    if bad:
        out.append(f"Title contains an excluded word: {bad.group(0)}")
    senior = bool(f.senior_re.search(title or ""))
    if not senior and not (pay_top is not None and pay_top >= f.min_pay):
        out.append(f"Not senior-level, and pay is below ${f.min_pay:,.0f} or not listed")
    if not f.any_location_ok([location or ""]):
        out.append("Outside your location list" if location else "Location not listed")
    names = {c.get("name", "").strip().lower() for c in settings.get("companies", [])}
    if (company or "").strip().lower() not in names:
        out.append("Company isn't on your company list")
    cutoff = int(settings.get("display", {}).get("min_score_to_show", 0) or 0)
    if score is not None and score < cutoff:
        out.append(f"Below your score cutoff ({cutoff})")
    return out


class _Row:
    """A saved posting in the shape the scorer expects."""
    def __init__(self, row):
        self.company, self.title, self.pay_top, self.description = row["company"], row["title"], row["pay_top"], row["description"]
        self.location_display = row["location"] or ""


def _score(settings: dict, p) -> dict:
    from .resume import resume_text
    from .scorer import score_posting
    return score_posting(settings["models"]["scoring"], resume_text(resume_path(settings)), p)


def find_existing(url: str = "", uid: str = ""):
    if uid:
        rows = db.get_postings([uid])
        if rows:
            return rows[0]
    if url:
        return db.posting_by_url(normalize_url, url)
    return None


def score_new(settings: dict, p: Posting) -> str:
    """Score and save a manual posting under today's date; returns its uid."""
    result = _score(settings, p)
    now = dt.datetime.now()
    db.save_manual_posting(p, result["score"], result["reason"], now.date().isoformat(),
                           now.isoformat(timespec="seconds"))
    return p.uid


def rescore(settings: dict, uid: str) -> None:
    row = db.get_postings([uid])[0]
    result = _score(settings, _Row(row))
    db.update_score(uid, result["score"], result["reason"])


def submit(url: str, text: str, company: str = "", title: str = "", location: str = "", pay: str = "",
           settings: Optional[dict] = None) -> dict:
    """The 'Score a posting' box. Returns {"uid", "existing": bool} or raises NeedsPaste / Blocked."""
    settings = settings or load_settings()
    url, text = (url or "").strip(), (text or "").strip()
    if not url and not text:
        raise NeedsPaste("Please enter a job posting link or paste the job description.")
    if text:
        if is_blocked(company):
            raise Blocked(f"{company} {BLOCKED_MSG}, so it wasn't scored.")
        if url and blocked_host(_host(url)):
            raise Blocked(f"This career site {BLOCKED_MSG}.")
        existing = find_existing(url=url) if url else None
        p = from_paste(text, company, title, location, pay, url)
        existing = existing or find_existing(uid=p.uid)
        if existing:
            return {"uid": existing["uid"], "existing": True}
        check_reliable(p, pasted=True)
    else:
        existing = find_existing(url=url)
        if existing:
            return {"uid": existing["uid"], "existing": True}
        p = read_link(url, settings, company_hint=company.strip())
        if company.strip() and not p.company:
            p.company = company.strip()
        existing = find_existing(uid=p.uid)
        if existing:
            return {"uid": existing["uid"], "existing": True}
        check_reliable(p, pasted=False)
    return {"uid": score_new(settings, p), "existing": False}
