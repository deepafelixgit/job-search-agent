"""Readers for companies that run their own career sites (big tech) or use
job systems other than Greenhouse / Lever / Ashby.

These read the same job data each company's official careers page loads in
your browser. They are not documented public feeds, so a company can change
them without notice - if a reader breaks, the daily run says so and the other
companies keep working. Every request first checks the site's robots.txt.

To save work, readers that need one extra request per posting (for the full
description) only make it for postings that (a) look like program-manager
roles, (b) haven't been seen before and (c) aren't older than the first-check
window.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional

import requests

from .fetchers import HEADERS, TIMEOUT, Posting, html_to_text, iso_date, top_of_pay_range
from . import http
from .robots import check as robots_check

QUERIES = ["technical program manager", "engineering program manager", "program management"]
WORKDAY_QUERIES = ["program manager"]     # Workday search is loose; this catches TPM, EPM and variants
# A title must look like this before we spend a request on its details.
# (Broader than the real role filter in settings.yaml, so nothing is missed.)
TITLE_HINT = re.compile(r"program manag|\bTPM\b|\bEPM\b", re.I)
PAUSE = 0.5          # seconds between detail requests, to be polite


_local = threading.local()        # per-company pause (companies are read in parallel)


def _request(method: str, url: str, *, params=None, json_body=None, html=False):
    time.sleep(getattr(_local, "pause", PAUSE))     # never hit a site in rapid bursts
    prepared = requests.Request(method, url, params=params).prepare()
    robots_check(prepared.url)
    headers = dict(HEADERS)
    if html:
        headers["Accept"] = "text/html"
    # paced, limited and retried (dropped connections, timeouts, 429/5xx): see agent/http.py
    r = http.fetch(method, url, params=params, json_body=json_body, headers=headers)
    r.raise_for_status()
    return r.text if html else r.json()


def _want_details(title: str, uid: str, posted: Optional[str], known, older_than) -> bool:
    if not TITLE_HINT.search(title or "") or uid in known:
        return False
    return not (older_than and posted and posted < older_than)


def _strings(obj, min_len: int = 40) -> Iterable[str]:
    """All longer text values inside nested JSON (used to collect descriptions)."""
    if isinstance(obj, str):
        if len(obj) >= min_len:
            yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v, min_len)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, min_len)


def _epoch_date(ts) -> Optional[str]:
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ------------------------------------------------------------------ Workday
# settings: board: workday, host: nvidia.wd5.myworkdayjobs.com, tenant: nvidia, site: NVIDIAExternalCareerSite

def _workday_list_date(text: str) -> Optional[str]:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 5 Days Ago' / 'Posted 30+ Days Ago' -> rough date."""
    t = (text or "").lower()
    today = dt.date.today()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - dt.timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*days", t)
    return (today - dt.timedelta(days=int(m.group(1)))).isoformat() if m else None


def fetch_workday(company: dict, known=frozenset(), older_than=None) -> List[Posting]:
    host, tenant, site = company["host"], company["tenant"], company["site"]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    out: Dict[str, Posting] = {}
    for query in WORKDAY_QUERIES:
        empty_pages, total = 0, 0
        for page in range(25):                         # at most 500 results per search
            data = _request("POST", f"{base}/jobs", json_body={
                "appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": query})
            items = data.get("jobPostings") or []
            if not items:
                break
            hits = 0
            for j in items:
                title = (j.get("title") or "").strip()
                if not TITLE_HINT.search(title):
                    continue
                hits += 1
                sid = (j.get("bulletFields") or [None])[0] or j.get("externalPath", "")
                uid = f"{company['name']}|{sid}"
                if uid in out:
                    continue
                rough = _workday_list_date(j.get("postedOn", ""))
                p = Posting(company=company["name"], source_id=sid, title=title,
                            locations=[j.get("locationsText", "")],
                            url=f"https://{host}/{site}{j.get('externalPath', '')}", description="",
                            posted=rough, posted_reliable=False)
                if _want_details(title, uid, rough, known, older_than):
                    info = (_request("GET", f"{base}{j['externalPath']}").get("jobPostingInfo") or {})
                    desc = html_to_text(info.get("jobDescription", ""))
                    locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
                    p.locations = [re.sub(r"^([A-Z]{2})-([A-Z]{2})-", r"\1, \2, ", l) for l in locs if l]
                    p.description, p.pay_top = desc, top_of_pay_range(desc)
                    exact = iso_date(info.get("startDate"))
                    p.posted, p.posted_reliable = (exact, True) if exact else (rough, False)
                    p.url = info.get("externalUrl") or p.url
                out[uid] = p
            empty_pages = 0 if hits else empty_pages + 1
            total = total or int(data.get("total") or 0)     # Workday sends the total on page 1 only
            if empty_pages >= 3 or (page + 1) * 20 >= total:
                break
    return list(out.values())


# ------------------------------------------------------------------ Eightfold
# settings: board: eightfold, url: https://explore.jobs.netflix.net, domain: netflix.com, api: v2 | pcsx

def fetch_eightfold(company: dict, known=frozenset(), older_than=None) -> List[Posting]:
    base, domain = company["url"].rstrip("/"), company["domain"]
    pcsx = company.get("api") == "pcsx"
    out: Dict[str, Posting] = {}
    for query in QUERIES:
        empty_pages, total = 0, 0
        for page in range(40):
            if pcsx:
                data = (_request("GET", f"{base}/api/pcsx/search",
                                 params={"domain": domain, "query": query, "start": page * 10}).get("data") or {})
            else:
                data = _request("GET", f"{base}/api/apply/v2/jobs",
                                params={"domain": domain, "query": query, "start": page * 10, "num": 10})
            items = data.get("positions") or []
            if not items:
                break
            hits = 0
            for j in items:
                title = (j.get("name") or "").strip()
                if not TITLE_HINT.search(title):
                    continue
                hits += 1
                sid = str(j.get("id"))
                uid = f"{company['name']}|{sid}"
                if uid in out:
                    continue
                if pcsx:
                    posted, reliable = _epoch_date(j.get("postedTs")), bool(j.get("postedTs"))
                else:   # v2 has only created/updated times; the later one is the closer stand-in
                    posted, reliable = _epoch_date(max(j.get("t_create") or 0, j.get("t_update") or 0) or None), False
                url = j.get("canonicalPositionUrl") or (base + j.get("positionUrl", f"/careers/job/{sid}"))
                p = Posting(company=company["name"], source_id=sid, title=title,
                            locations=list(j.get("locations") or [j.get("location", "")]), url=url,
                            description="", posted=posted, posted_reliable=reliable)
                if _want_details(title, uid, posted, known, older_than):
                    if pcsx:
                        d = _request("GET", f"{base}/api/pcsx/position_details",
                                     params={"position_id": sid, "domain": domain, "hl": "en"}).get("data") or {}
                        desc = html_to_text(d.get("jobDescription", ""))
                        p.url = d.get("publicUrl") or p.url
                    else:
                        d = _request("GET", f"{base}/api/apply/v2/jobs/{sid}", params={"domain": domain})
                        desc = html_to_text(d.get("job_description", ""))
                        # The real posting date is on the job's own page ("datePosted")
                        job_page = _request("GET", url, html=True)
                        m = re.search(r'"datePosted"\s*:\s*"([^"]+)"', job_page)
                        if m and iso_date(m.group(1)):
                            p.posted, p.posted_reliable = iso_date(m.group(1)), True
                    p.description, p.pay_top = desc, top_of_pay_range(desc)
                    p.locations = list(d.get("locations") or p.locations)
                out[uid] = p
            empty_pages = 0 if hits else empty_pages + 1
            total = total or int(data.get("count") or 0)
            if empty_pages >= 3 or (total and (page + 1) * 10 >= total):
                break
    return list(out.values())


# ------------------------------------------------------------------ Oracle Cloud
# settings: board: oracle, host: https://eeho.fa.us2.oraclecloud.com, site: CX_45001

def fetch_oracle(company: dict, known=frozenset(), older_than=None) -> List[Posting]:
    host, site = company["host"].rstrip("/"), company["site"]
    api = f"{host}/hcmRestApi/resources/latest"
    out: Dict[str, Posting] = {}
    for query in QUERIES:
        for page in range(12):
            finder = (f"findReqs;siteNumber={site},keyword={query},limit=25,offset={page * 25},"
                      f"sortBy=POSTING_DATES_DESC")
            data = _request("GET", f"{api}/recruitingCEJobRequisitions",
                            params={"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                                    "finder": finder})
            reqs = ((data.get("items") or [{}])[0].get("requisitionList")) or []
            if not reqs:
                break
            for j in reqs:
                title = (j.get("Title") or "").strip()
                if not TITLE_HINT.search(title):
                    continue
                sid = str(j.get("Id"))
                uid = f"{company['name']}|{sid}"
                if uid in out:
                    continue
                posted = iso_date(j.get("PostedDate"))
                locs = [j.get("PrimaryLocation", "")] + [l.get("Name", "") for l in j.get("secondaryLocations") or []]
                p = Posting(company=company["name"], source_id=sid, title=title, locations=[l for l in locs if l],
                            url=f"{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{sid}", description="",
                            posted=posted)
                if _want_details(title, uid, posted, known, older_than):
                    d = _request("GET", f"{api}/recruitingCEJobRequisitionDetails",
                                 params={"expand": "all", "onlyData": "true",
                                         "finder": f'ById;Id="{sid}",siteNumber={site}'})
                    it = (d.get("items") or [{}])[0]
                    desc = "\n\n".join(html_to_text(it.get(k) or "") for k in
                                       ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                                        "ExternalQualificationsStr", "CorporateDescriptionStr"))
                    p.description, p.pay_top = desc.strip(), top_of_pay_range(desc)
                out[uid] = p
            # newest first: once a whole page is older than the window, stop
            dates = [iso_date(j.get("PostedDate")) for j in reqs]
            if older_than and all(d and d < older_than for d in dates):
                break
    return list(out.values())


# ------------------------------------------------------------------ Atlassian
# settings: board: atlassian   (Atlassian only publishes an "updated" date, used as the posting date)

def fetch_atlassian(company: dict, known=frozenset(), older_than=None) -> List[Posting]:
    data = _request("GET", "https://www.atlassian.com/endpoint/careers/listings")
    out = []
    for j in data:
        updated = (j.get("portalJobPost") or {}).get("updatedDate", "")
        try:
            posted = dt.datetime.strptime(updated, "%Y-%m-%d %I:%M %p").date().isoformat()
        except ValueError:
            posted = iso_date(updated)
        desc = "\n\n".join(html_to_text(t) for k, t in j.items()
                           if isinstance(t, str) and k not in ("title", "category") and "<" in t)
        out.append(Posting(company=company["name"], source_id=str(j.get("id")), title=(j.get("title") or "").strip(),
                           locations=[re.sub(r"\s+", " ", l) for l in j.get("locations") or []],
                           url=f"https://www.atlassian.com/company/careers/details/{j.get('id')}",
                           description=desc, pay_top=top_of_pay_range(desc), posted=posted,
                           posted_reliable=False))      # "updated" date only
    return out


# ------------------------------------------------------------------ Workable
# settings: board: workable, board_id: huggingface

def fetch_workable(company: dict, known=frozenset(), older_than=None) -> List[Posting]:
    data = _request("GET", f"https://apply.workable.com/api/v1/widget/accounts/{company['board_id']}",
                    params={"details": "true"})
    out = []
    for j in data.get("jobs") or []:
        locs = [", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x)
                for l in j.get("locations") or []]
        if not locs:
            locs = [", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)]
        if j.get("telecommuting") and any("United States" in l for l in locs):
            locs.append("Remote - US")
        desc = html_to_text(j.get("description", ""))
        out.append(Posting(company=company["name"], source_id=j.get("shortcode", ""), title=(j.get("title") or "").strip(),
                           locations=[l for l in locs if l], url=j.get("url", ""), description=desc,
                           pay_top=top_of_pay_range(desc), posted=iso_date(j.get("published_on"))))
    return out


# ------------------------------------------------------------------ Rippling
# settings: board: rippling, board_id: rippling

def fetch_rippling(company: dict, known=frozenset(), older_than=None) -> List[Posting]:
    base = f"https://api.rippling.com/platform/api/ats/v1/board/{company['board_id']}/jobs"
    out = []
    for j in _request("GET", base):
        title = (j.get("name") or "").strip()
        sid = j.get("uuid", "")
        uid = f"{company['name']}|{sid}"
        loc = (j.get("workLocation") or {}).get("label", "")
        p = Posting(company=company["name"], source_id=sid, title=title, locations=[loc] if loc else [],
                    url=j.get("url", ""), description="")
        if _want_details(title, uid, None, known, None):       # the list has no dates
            d = _request("GET", f"{base}/{sid}")
            desc = "\n\n".join(html_to_text(t) for t in _strings(d.get("description") or {}))
            p.description, p.pay_top = desc, top_of_pay_range(desc)
            p.posted, p.posted_reliable = iso_date(d.get("createdOn")), False   # created, not published
            p.locations = list(d.get("workLocations") or p.locations)
            for rng in d.get("payRangeDetails") or []:
                top = rng.get("rangeEnd") or rng.get("max")
                if isinstance(top, (int, float)) and top > 20000:
                    p.pay_top = max(p.pay_top or 0, float(top))
        out.append(p)
    return out


# ------------------------------------------------------------------ Apple
# settings: board: apple   (jobs.apple.com; reads the data the search page itself carries)

def _apple_data(html: str) -> dict:
    m = re.search(r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((\".*?\")\);", html, re.S)
    if not m:
        raise ValueError("jobs.apple.com page format changed - the Apple reader needs an update")
    return json.loads(json.loads(m.group(1))).get("loaderData") or {}


def _apple_location(loc: dict) -> str:
    name = loc.get("name") or loc.get("city") or ""
    inner = re.search(r"\(([^)]+)\)", name)           # "Santa Clara Valley (Cupertino)" -> "Cupertino"
    name = inner.group(1) if inner else name
    country = loc.get("countryName") or ""
    state = loc.get("stateProvince") or ""
    return ", ".join(x for x in (name, state, country) if x)


def fetch_apple(company: dict, known=frozenset(), older_than=None) -> List[Posting]:
    out: Dict[str, Posting] = {}
    for query in QUERIES:
        for page in range(1, 16):                       # newest first, 20 per page
            loader = _apple_data(_request("GET", "https://jobs.apple.com/en-us/search", html=True, params={
                "search": query, "sort": "newest", "location": "united-states-USA", "page": page}))
            items = (loader.get("search") or {}).get("searchResults") or []
            if not items:
                break
            all_old_or_known = True
            for j in items:
                sid = str(j.get("positionId") or j.get("id"))
                uid = f"{company['name']}|{sid}"
                posted = iso_date(j.get("postDateInGMT"))
                if uid not in known and not (older_than and posted and posted < older_than):
                    all_old_or_known = False
                title = (j.get("postingTitle") or "").strip()
                if not TITLE_HINT.search(title) or uid in out:
                    continue
                url = f"https://jobs.apple.com/en-us/details/{sid}/{j.get('transformedPostingTitle', '')}"
                p = Posting(company=company["name"], source_id=sid, title=title,
                            locations=[_apple_location(l) for l in j.get("locations") or []],
                            url=url, description=j.get("jobSummary") or "", posted=posted)
                if _want_details(title, uid, posted, known, older_than):
                    detail = _apple_data(_request("GET", url, html=True)).get("jobDetails") or {}
                    desc = "\n\n".join(html_to_text(t) for t in _strings(detail.get("jobsData") or {}))
                    p.description = desc or p.description
                    p.pay_top = top_of_pay_range(desc)
                out[uid] = p
            if all_old_or_known:                         # everything newer was already handled
                break
    return list(out.values())


# ------------------------------------------------------------------ Google
# settings: board: google, locations: [...]
# Google's robots.txt allows the first page of results but not later pages, so
# the reader looks at the 20 newest matches for each search and each location.

def _google_jobs(html: str) -> list:
    m = re.search(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel: \{\}\}\);</script>", html, re.S)
    if not m:
        raise ValueError("Google careers page format changed - the Google reader needs an update")
    data = json.loads(m.group(1))
    return data[0] or []


def fetch_google(company: dict, known=frozenset(), older_than=None) -> List[Posting]:
    """Optional settings:
      only_company: DeepMind          -> search only that Alphabet company's jobs on Google's site
      skip_companies: [DeepMind]      -> leave out jobs listed under its own company entry
    """
    out: Dict[str, Posting] = {}
    only = company.get("only_company")
    skip = {c.lower() for c in company.get("skip_companies") or []}
    for query in QUERIES:
        for location in company.get("locations") or ["Mountain View, CA, USA"]:
            params = {"q": f'"{query}"', "location": location, "sort_by": "date"}
            if only:
                params["company"] = only
            jobs = _google_jobs(_request("GET", "https://www.google.com/about/careers/applications/jobs/results",
                                         html=True, params=params))
            for j in jobs:
                listed_under = google_listed_under(j)
                if listed_under.lower() in skip or (only and listed_under.lower() != only.lower()):
                    continue
                p = google_posting(company["name"], j)
                out[p.source_id] = p
    return list(out.values())


def google_listed_under(j: list) -> str:
    """The Alphabet company a Google careers job is listed under (Google, DeepMind, Wing...)."""
    return str(j[7] if len(j) > 7 and j[7] else "")


def google_posting(name: str, j: list) -> Posting:
    """One job in Google careers' page data (search results or a job's own page)."""
    def part(i):
        try:
            v = j[i]
            return v[1] if isinstance(v, list) and len(v) > 1 and isinstance(v[1], str) else ""
        except IndexError:
            return ""
    sid = str(j[0])
    desc = "\n\n".join(html_to_text(part(i)) for i in (10, 3, 4, 19) if part(i))
    locs = [l[0] for l in (j[9] or []) if l and l[0]] if len(j) > 9 else []
    posted = _epoch_date((j[12] or [None])[0]) if len(j) > 12 and j[12] else None
    return Posting(company=name, source_id=sid, title=(j[1] or "").strip(), locations=locs,
                   url=f"https://www.google.com/about/careers/applications/jobs/results/{sid}",
                   description=desc, pay_top=top_of_pay_range(desc), posted=posted,
                   posted_reliable=False)   # Google doesn't label or show this date


SITE_READERS: Dict[str, Callable] = {
    "workday": fetch_workday,
    "eightfold": fetch_eightfold,
    "oracle": fetch_oracle,
    "atlassian": fetch_atlassian,
    "workable": fetch_workable,
    "rippling": fetch_rippling,
    "apple": fetch_apple,
    "google": fetch_google,
}


def run_reader(company: dict, known=frozenset(), older_than=None) -> List[Posting]:
    """Entry point used by fetchers.fetch_company. settings may set 'pause' (seconds
    between requests) for sites that are strict about request speed."""
    _local.pause = float(company.get("pause", PAUSE))
    return SITE_READERS[company["board"]](company, known, older_than)
