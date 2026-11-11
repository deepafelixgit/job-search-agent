"""Download job postings from official company job boards.

Each reader returns a list of Posting objects with the same fields, so the
rest of the agent doesn't care which job board a posting came from.
Only public, official feeds are used - no LinkedIn, no Indeed, no scraping
of web pages.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from .personal import blocked_word, is_blocked
from . import http
from .robots import check as robots_check

HEADERS = {"User-Agent": "personal-job-search-agent/1.0", "Accept": "application/json"}
TIMEOUT = 60          # read timeout in seconds (the real requests use http.TIMEOUT)


@dataclass
class Posting:
    company: str
    source_id: str
    title: str
    locations: List[str]
    url: str
    description: str
    pay_top: Optional[float] = None     # top of posted base pay (USD/year), if listed
    pay_text: str = ""
    posted: Optional[str] = None        # date first published, YYYY-MM-DD (from the feed)
    # False when the feed's date is only a stand-in (a "created" or "updated" date,
    # or a rough "30+ days ago"). It is still used for the first-check window, but the
    # web app shows "Unknown" instead of guessing.
    posted_reliable: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"{self.company}|{self.source_id}"

    @property
    def location_display(self) -> str:
        return " | ".join(self.locations)


# ----------------------------------------------------------------- helpers

def html_to_text(raw: str) -> str:
    """Turn HTML (sometimes double-escaped) into readable plain text."""
    if not raw:
        return ""
    text = html.unescape(html.unescape(raw))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


_AMOUNT = r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?"
_RANGE = re.compile(
    _AMOUNT + r"\s*(?:USD|usd)?\s*(?:-|–|—|to|and)\s*(?:USD\s*)?" + r"\$?\s?(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?"
)


def _to_number(num: str, suffix: Optional[str]) -> float:
    value = float(num.replace(",", ""))
    if suffix and suffix.lower() == "k":
        value *= 1_000
    elif suffix and suffix.lower() == "m":
        value *= 1_000_000
    return value


def top_of_pay_range(text: str) -> Optional[float]:
    """Find salary ranges like '$207K – $285K' or '$132,100 - $165,100' and
    return the highest top-of-range. Hourly-looking numbers are ignored."""
    tops = []
    for m in _RANGE.finditer(text or ""):
        low = _to_number(m.group(1), m.group(2) or m.group(4))
        high = _to_number(m.group(3), m.group(4) or m.group(2))
        if 20_000 <= high <= 3_000_000 and low <= high:
            tops.append(high)
    return max(tops) if tops else None


def iso_date(value) -> Optional[str]:
    """Turn a feed's date into YYYY-MM-DD. Handles ISO text, 'June 25, 2026'
    and Lever's millisecond timestamps. Returns None if it can't be read."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).date().isoformat()
        text = str(value).strip()
        if re.match(r"\d{4}-\d{2}-\d{2}", text):
            return text[:10]
        return dt.datetime.strptime(text, "%B %d, %Y").date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _get(url: str, **params) -> dict:
    req = requests.Request("GET", url, params=params or None).prepare()
    robots_check(req.url)                     # respect the site's robots.txt
    r = http.fetch("GET", url, params=params or None, headers=HEADERS)   # paced, retried
    r.raise_for_status()
    return r.json()


# ----------------------------------------------------------------- readers

def fetch_greenhouse(company: dict) -> List[Posting]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{company['board_id']}/jobs", content="true")
    return [greenhouse_posting(company["name"], j) for j in data.get("jobs", [])]


def greenhouse_posting(name: str, j: dict) -> Posting:
    """One Greenhouse job (from the board list or the single-job feed)."""
    desc = html_to_text(j.get("content", ""))
    loc = (j.get("location") or {}).get("name", "")
    locs = [loc] if loc else [o.get("name", "") for o in j.get("offices", []) if o.get("name")]
    return Posting(
        company=name, source_id=str(j["id"]), title=j["title"].strip(),
        locations=locs, url=j.get("absolute_url", ""), description=desc,
        pay_top=top_of_pay_range(desc), posted=iso_date(j.get("first_published")),
    )


def fetch_lever(company: dict) -> List[Posting]:
    data = _get(f"https://api.lever.co/v0/postings/{company['board_id']}", mode="json")
    return [lever_posting(company["name"], j) for j in data]


def lever_posting(name: str, j: dict) -> Posting:
    """One Lever job (from the board list or the single-job feed)."""
    cats = j.get("categories") or {}
    locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
    if (j.get("workplaceType") or "").lower() == "remote" and j.get("country") == "US":
        locs = locs + ["Remote - US"]
    parts = [j.get("descriptionPlain", "")]
    for lst in j.get("lists", []):
        parts.append(lst.get("text", "") + "\n" + html_to_text(lst.get("content", "")))
    parts.append(j.get("additionalPlain", ""))
    desc = "\n\n".join(p for p in parts if p)
    pay_top = None
    sr = j.get("salaryRange") or {}
    if sr.get("max") and (sr.get("interval") or "").startswith("per-year"):
        pay_top = float(sr["max"])
    return Posting(
        company=name, source_id=j["id"], title=j.get("text", "").strip(),
        locations=locs, url=j.get("hostedUrl", ""), description=desc,
        pay_top=pay_top or top_of_pay_range(desc), posted=iso_date(j.get("createdAt")),
    )


def fetch_ashby(company: dict) -> List[Posting]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{company['board_id']}", includeCompensation="true")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", [])]
        locs = [l for l in locs if l]
        if (j.get("workplaceType") or "").lower() == "remote":
            country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "")
            if country in ("", "US", "USA", "United States") and not any("remote" in l.lower() for l in locs):
                locs.append("Remote - US" if country else "Remote")
        comp = j.get("compensation") or {}
        pay_top = None
        for c in comp.get("summaryComponents", []) or []:
            if c.get("compensationType") == "Salary" and c.get("currencyCode", "USD") == "USD" and c.get("maxValue"):
                pay_top = max(pay_top or 0, float(c["maxValue"]))
        summary = comp.get("compensationTierSummary") or ""
        desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", ""))
        out.append(Posting(
            company=company["name"], source_id=j["id"], title=j["title"].strip(),
            locations=locs, url=j.get("jobUrl", ""), description=desc,
            pay_top=pay_top or top_of_pay_range(summary) or top_of_pay_range(desc),
            pay_text=summary, posted=iso_date(j.get("publishedAt")),
        ))
    return out


def fetch_jibe(company: dict) -> List[Posting]:
    """Careers sites built on iCIMS Jibe (e.g. careers.rivian.com) publish /api/jobs."""
    base = company["url"].rstrip("/")
    out, page = [], 1
    while page <= 50:
        data = _get(f"{base}/api/jobs", page=page, limit=100)
        jobs = data.get("jobs", [])
        if not jobs:
            break
        for item in jobs:
            d = item.get("data", {})
            full = d.get("full_location") or ", ".join(x for x in [d.get("city"), d.get("state")] if x)
            locs = [l.strip() for l in full.split(";") if l.strip()]
            desc = "\n\n".join(html_to_text(d.get(k) or "") for k in ("description", "responsibilities", "qualifications"))
            slug = d.get("slug") or d.get("req_id")
            out.append(Posting(
                company=company["name"], source_id=str(d.get("req_id") or slug), title=d.get("title", "").strip(),
                locations=locs, url=f"{base}/careers-home/jobs/{slug}?lang=en-us", description=desc,
                pay_top=top_of_pay_range(desc), posted=iso_date(d.get("posted_date")),
            ))
        if page * 100 >= int(data.get("totalCount") or 0):
            break
        page += 1
    return out


def fetch_amazon_jobs(company: dict) -> List[Posting]:
    """amazon.jobs official search feed, narrowed to one business."""
    cats = [c.lower() for c in company.get("business_categories", [])]
    must = company.get("must_mention")
    if not cats and not must:
        raise ValueError(f"{company['name']}: amazon_jobs needs business_categories or must_mention, "
                         "so the search stays narrowed to one business.")
    must_re = re.compile(rf"\b{re.escape(must)}\b") if must else None
    seen, out = set(), []
    for query in ("technical program manager", "engineering program manager", "program management"):
        offset = 0
        while offset < 2000:
            data = _get("https://www.amazon.jobs/en/search.json", base_query=query,
                        result_limit=100, offset=offset, country="USA", sort="recent")
            jobs = data.get("jobs", [])
            if not jobs:
                break
            for j in jobs:
                if j["id"] in seen:
                    continue
                seen.add(j["id"])
                desc = "\n\n".join(html_to_text(j.get(k) or "") for k in
                                   ("description", "basic_qualifications", "preferred_qualifications"))
                if cats and (j.get("business_category") or "").lower() not in cats:
                    continue
                # must be IN the title or opening lines, not just listed among Amazon products
                if must_re and not must_re.search(j.get("title", "") + " " + desc[:300]):
                    continue
                if blocked_word(j.get("title", "") + " " + (j.get("company_name") or "") + " " + desc[:600]):
                    continue
                out.append(amazon_posting(company["name"], j))
            offset += 100
            if offset >= int(data.get("hits") or 0):
                break
    return out


def amazon_text(j: dict) -> str:
    return "\n\n".join(html_to_text(j.get(k) or "") for k in
                       ("description", "basic_qualifications", "preferred_qualifications"))


def amazon_posting(name: str, j: dict) -> Posting:
    """One amazon.jobs job from its search feed."""
    locs = []
    for raw in j.get("locations") or []:
        try:
            locs.append(json.loads(raw).get("normalizedLocation", ""))
        except (ValueError, AttributeError):
            pass
    locs = [l for l in locs if l] or [j.get("normalized_location", "")]
    desc = amazon_text(j)
    return Posting(
        company=name, source_id=str(j["id"]), title=j.get("title", "").strip(),
        locations=locs, url="https://www.amazon.jobs" + j.get("job_path", ""), description=desc,
        pay_top=top_of_pay_range(desc), posted=iso_date(j.get("posted_date")),
    )


READERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "jibe": fetch_jibe,
    "amazon_jobs": fetch_amazon_jobs,
}


def fetch_company(company: dict, known: frozenset = frozenset(), older_than: Optional[str] = None) -> List[Posting]:
    """known: posting IDs already dealt with. older_than: YYYY-MM-DD; postings
    published before it don't need full details (they'll only be remembered as
    seen). Both just save work for readers that need one extra request per posting."""
    if is_blocked(company.get("name", "")):
        raise ValueError(f"{company['name']} is on your blocked list (personal.blocked in settings.yaml) and was skipped.")
    from .site_readers import SITE_READERS, run_reader
    if company.get("board") in SITE_READERS:
        return run_reader(company, known, older_than)
    reader = READERS.get(company.get("board", ""))
    if reader is None:
        raise ValueError(f"{company.get('name')}: unknown board type '{company.get('board')}'. "
                         f"Use one of: {', '.join(list(READERS) + list(SITE_READERS))}")
    return reader(company)
