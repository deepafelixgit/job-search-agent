"""Read the evidence files and split them into short, labeled passages.

Every passage knows:
  source        the original file it came from (what you'd open to check it)
  doc_type      manager review | peer review | self review | resume | verified metrics
  period        the review period, e.g. "H1 2021", "2023 Mid-year", "2024 Year-end"
  period_note   how the period was worked out, if it wasn't printed on the page
  location      "page 12" for PDFs, or the section heading (and line numbers) otherwise
  text          the passage itself (a few sentences)

The files are only ever opened for reading. Nothing in evidence/ is changed.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import docx
from pypdf import PdfReader

TARGET_WORDS = 90        # aim for passages of about this many words
MAX_WORDS = 140

TRANSCRIPTION_FILE = "Performance_Review_pages_18_and_29_transcribed.md"


@dataclass
class Passage:
    pid: str
    source: str
    doc_type: str
    period: str
    location: str
    text: str
    period_note: str = ""
    extra: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        if not d["period_note"]:
            d.pop("period_note")
        if not d["extra"]:
            d.pop("extra")
        return d


@dataclass
class Document:
    file: str
    doc_type: str
    periods: List[str]
    passages: List[Passage]
    text_check: str = ""          # e.g. "real text on 30 of 32 pages"
    notes: List[str] = field(default_factory=list)


# ------------------------------------------------------------------ periods

def period_name(year: int, half: int) -> str:
    """Meta's naming changed in 2022: H1/H2 before, Mid-year/Year-end after."""
    if year <= 2021 or (year == 2022 and half == 1):
        return f"H{half} {year}"
    return f"{year} {'Mid-year' if half == 1 else 'Year-end'}"


def canonical_period(text: str) -> Optional[str]:
    """Turn any spelling ("Year-end 2022", "2022 Year-end", "H1 2021", "Mid-year 2023") into one form."""
    t = text.strip()
    m = re.search(r"\bH([12])\s*(20\d\d)\b", t) or re.search(r"\b(20\d\d)\s*H([12])\b", t)
    if m:
        a, b = m.groups()
        half, year = (int(a), int(b)) if len(a) == 1 else (int(b), int(a))
        return period_name(year, half)
    m = re.search(r"(Mid-?year|Year-?end)\s*(20\d\d)|(20\d\d)\s*(Mid-?year|Year-?end)", t, re.I)
    if m:
        word = (m.group(1) or m.group(4)).lower()
        year = int(m.group(2) or m.group(3))
        return period_name(year, 1 if word.startswith("mid") else 2)
    return None


def period_from_submitted(date: dt.date, review_year: Optional[int] = None,
                          year_end_until_month: int = 4) -> Optional[str]:
    """A review submitted Jun-Sep covers the first half of that year; one submitted
    in Nov/Dec covers that year's second half, and Jan..year_end_until_month the year
    before. Manager reviews arrive as late as April; peer feedback only until February."""
    if 6 <= date.month <= 9:
        year, half = date.year, 1
    elif date.month >= 11:
        year, half = date.year, 2
    elif date.month <= year_end_until_month:
        year, half = date.year - 1, 2
    else:
        return None
    if review_year and review_year != year:
        return None
    return period_name(year, half)


# ------------------------------------------------------------------ chunking helpers

def _boilerplate() -> List["re.Pattern"]:
    """Page headers and footers in the review PDFs. The candidate's name comes from settings.yaml."""
    from .candidate import name
    full = re.escape(name()) if name() else r"[A-Z][a-z]+ [A-Z][a-z]+"
    first = re.escape(name().split()[0]) if name() else r"[A-Z][a-z]+"
    return [
        re.compile(r"Confidential\s*Your self-review and feedback about you can be downloaded.*?mentioned\.\s*", re.S),
        re.compile(full + r"\s*\n?\s*Technical Program Manager @ [^\n]*?(?:20\d\d|H[12] 20\d\d)?\s*Page \d+ of \d+"),
        re.compile(r"Page \d+ of \d+"),
        re.compile(r"Shared with " + first),
    ]


def clean(text: str) -> str:
    for rx in _boilerplate():
        text = rx.sub(" ", text)
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def split_sentences(text: str) -> List[str]:
    text = re.sub(r"\s*\n\s*(?=[•\-*]\s)", "\n", text)          # keep bullets on their own
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(•])|\n(?=[•\-*]\s)|\n{2,}", text)
    return [re.sub(r"\s+", " ", p).strip() for p in parts if p and p.strip()]


def chunk(text: str) -> List[str]:
    """Group sentences into passages of about TARGET_WORDS words."""
    out, cur, n = [], [], 0
    for s in split_sentences(text):
        words = len(s.split())
        if words > MAX_WORDS:                      # one very long sentence: cut it
            if cur:
                out.append(" ".join(cur))
                cur, n = [], 0
            w = s.split()
            for i in range(0, len(w), TARGET_WORDS):
                out.append(" ".join(w[i:i + TARGET_WORDS]))
            continue
        if n + words > MAX_WORDS or (n >= TARGET_WORDS and cur):
            out.append(" ".join(cur))
            cur, n = [], 0
        cur.append(s)
        n += words
    if cur:
        out.append(" ".join(cur))
    return [c for c in out if len(c.split()) >= 4]


HEADING_RX = re.compile(
    r"^(?:[A-Z][A-Z &/()\-]{3,60}|.{6,120}\?|(?:H[12] 20\d\d )?Calibration Notes|Rating|Performance Signal|"
    r"Areas of Focus|Performance Review|Peer Review)$")


# ------------------------------------------------------------------ manager reviews (combined PDF)

def read_transcription(folder: Path) -> Dict[int, dict]:
    """Pages typed up from image-only PDF pages: {page_number: {text, submitted}}."""
    f = folder / TRANSCRIPTION_FILE
    if not f.exists():
        return {}
    pages: Dict[int, dict] = {}
    current = None
    for line in f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^##\s+Page\s+(\d+):\s*(.*)$", line)
        if m:
            current = int(m.group(1))
            sub = re.search(r"submitted (\d{4}-\d{2}-\d{2})", m.group(2))
            pages[current] = {"lines": [], "submitted": sub.group(1) if sub else None,
                              "heading": m.group(2).strip()}
            continue
        if current is not None and line.strip() != "---":
            pages[current]["lines"].append(line)
    for p in pages.values():
        p["text"] = "\n".join(p.pop("lines")).strip()
    return pages


def read_manager_reviews(path: Path, transcribed: Dict[int, dict]) -> Document:
    reader = PdfReader(str(path))
    raw = [(p.extract_text() or "") for p in reader.pages]
    with_text = sum(1 for t in raw if len(t.strip()) > 50)
    image_only = [i + 1 for i, t in enumerate(raw) if len(t.strip()) <= 50]

    # 1. find where each exported review starts ("Page 1 of N") and its period
    sections: List[Tuple[int, int]] = []          # (first_page_index, page_count)
    for i, t in enumerate(raw):
        m = re.search(r"Page 1 of (\d+)", t)
        if m:
            sections.append((i, int(m.group(1))))
    page_period: Dict[int, Tuple[str, str]] = {}
    notes = []
    for first, count in sections:
        pages = range(first, min(first + count, len(raw)))
        header = raw[first]
        printed = canonical_period(header[:600])
        year_m = re.search(r"\b(20\d\d)\s*Page 1 of", re.sub(r"\s+", " ", header))
        year = int(year_m.group(1)) if year_m else None
        submitted = None
        for i in pages:
            s = re.search(r"Submitted (\d{4}-\d{2}-\d{2})", raw[i])
            if s:
                submitted = s.group(1)
                break
            if (i + 1) in transcribed and transcribed[i + 1].get("submitted"):
                submitted = transcribed[i + 1]["submitted"]
                break
        if printed:
            period, note = printed, ""
        elif submitted:
            period = period_from_submitted(dt.date.fromisoformat(submitted), year) or f"{year} (half unclear)"
            note = f"page header shows only the year {year}; half worked out from the submitted date {submitted}"
        else:
            period, note = f"{year or 'unknown year'} (half unclear)", "no period or submitted date found"
        for i in pages:
            page_period[i] = (period, note)

    # 2. make passages page by page, so every passage cites one exact page
    passages: List[Passage] = []
    for i, t in enumerate(raw):
        page_no = i + 1
        extra = {}
        if len(t.strip()) <= 50 and page_no in transcribed:
            t = transcribed[page_no]["text"]
            extra = {"text_from": f"word-for-word transcription of this image-only page ({TRANSCRIPTION_FILE})"}
        period, note = page_period.get(i, ("unknown", "page is not inside a recognised review section"))
        section = ""
        for block_heading, block_text in _blocks(clean(t)):
            section = block_heading or section
            for k, piece in enumerate(chunk(block_text)):
                loc = f"page {page_no}" + (f" · {section}" if section else "")
                passages.append(Passage(pid=f"perf-p{page_no}-{len(passages)}", source=path.name,
                                        doc_type="manager review", period=period, location=loc,
                                        text=piece, period_note=note, extra=dict(extra)))
    still_missing = [p for p in image_only if p not in transcribed]
    if image_only:
        notes.append(f"Pages {', '.join(map(str, image_only))} are images (no extractable text)"
                     + (f"; pages {', '.join(str(p) for p in image_only if p in transcribed)} are covered by "
                        f"{TRANSCRIPTION_FILE}" if any(p in transcribed for p in image_only) else "")
                     + (f"; pages {still_missing} have no text at all" if still_missing else "") + ".")
    periods = sorted({p for p, _ in page_period.values()}, key=_period_sort)
    return Document(file=path.name, doc_type="manager review", periods=periods, passages=passages,
                    text_check=f"real text on {with_text} of {len(raw)} pages (not a scan)", notes=notes)


def _blocks(text: str) -> List[Tuple[str, str]]:
    """Split text at heading-looking lines -> [(heading, body)]."""
    blocks, heading, body = [], "", []
    for line in text.splitlines():
        is_md_heading = line.lstrip().startswith("#")
        s = line.strip().lstrip("#").strip()
        if s and (is_md_heading or HEADING_RX.match(s)) and len(s.split()) <= 20:
            if body:
                blocks.append((heading, "\n".join(body)))
                body = []
            heading = s
        else:
            body.append(line)
    if body:
        blocks.append((heading, "\n".join(body)))
    return blocks


# ------------------------------------------------------------------ peer reviews (combined PDF)

# "Jane Smith • Software Engineer" - a name of 2-6 capitalised words, then a job title
NAME = r"[A-Z][\w'.\-]+(?: (?:[A-Z(][\w'.\-()]*|[a-z]{1,3}))*"
REVIEWER_RX = re.compile(rf"^\s*({NAME})\s*•\s*([A-Z][^\n?]{{2,70}})$", re.M)


def read_peer_reviews(path: Path) -> Document:
    reader = PdfReader(str(path))
    raw = [(p.extract_text() or "") for p in reader.pages]
    with_text = sum(1 for t in raw if len(t.strip()) > 50)
    passages: List[Passage] = []
    periods = set()
    reviewer, role, period, note = "", "", "unknown", ""
    for i, t in enumerate(raw):
        page_no = i + 1
        # walk the page in pieces: a new reviewer or a "Submitted" date changes the labels
        pieces = re.split(rf"(?=^\s*{NAME}\s*•\s*[A-Z][^\n?]{{2,70}}$)|(?=Submitted \d{{4}}-\d{{2}}-\d{{2}})", t, flags=re.M)
        for piece in pieces:
            m = REVIEWER_RX.match(piece)
            if m:
                reviewer, role = m.group(1).strip(), m.group(2).strip()
                piece = piece[m.end():]
            s = re.match(r"\s*Submitted (\d{4}-\d{2}-\d{2})", piece)
            if s:
                d = dt.date.fromisoformat(s.group(1))
                inferred = period_from_submitted(d, year_end_until_month=2)
                if inferred:
                    period, note = inferred, f"inferred from the submitted date {s.group(1)} (not printed on the page)"
                else:
                    period, note = f"off-cycle feedback ({s.group(1)})", "submitted outside the usual review months; period not stated"
                piece = piece[s.end():]
            body = clean(piece)
            if page_no == 1:
                printed = canonical_period(body)
                if printed:
                    period, note = printed, ""
            for block_heading, block_text in _blocks(body):
                for c in chunk(block_text):
                    periods.add(period)
                    loc = f"page {page_no}" + (f" · {block_heading}" if block_heading else "")
                    passages.append(Passage(
                        pid=f"peer-p{page_no}-{len(passages)}", source=path.name, doc_type="peer review",
                        period=period, location=loc, text=c, period_note=note,
                        extra={"reviewer": f"{reviewer} ({role})"} if reviewer else {}))
    return Document(file=path.name, doc_type="peer review", periods=sorted(periods, key=_period_sort),
                    passages=passages, text_check=f"real text on {with_text} of {len(raw)} pages (not a scan)",
                    notes=["Review periods are inferred from each reviewer's submitted date; the PDF doesn't print them."])


# ------------------------------------------------------------------ markdown / text / docx

def read_self_reviews(path: Path) -> Document:
    lines = path.read_text(encoding="utf-8").splitlines()
    passages: List[Passage] = []
    period, section, buf, start = "unknown", "", [], 1
    periods = []

    def flush(end_line: int):
        nonlocal buf
        text = "\n".join(buf).strip()
        if text:
            for c in chunk(text):
                passages.append(Passage(pid=f"self-{len(passages)}", source=path.name, doc_type="self review",
                                        period=period, location=f"{section or 'intro'} (lines {start}-{end_line})",
                                        text=c))
        buf = []

    for n, line in enumerate(lines, 1):
        if line.startswith("## "):
            flush(n - 1)
            period = canonical_period(line) or line[3:].strip()
            periods.append(period)
            section, start = "", n + 1
        elif line.startswith("### "):
            flush(n - 1)
            section, start = line[4:].strip().rstrip(":"), n + 1
        elif line.startswith("# "):
            continue
        else:
            if not buf:
                start = n
            buf.append(line)
    flush(len(lines))
    return Document(file=path.name, doc_type="self review", periods=periods, passages=passages,
                    text_check="text file")


def read_resume(path: Path) -> Document:
    d = docx.Document(str(path))
    passages: List[Passage] = []
    section, sub = "", ""
    for p in d.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        bold = any(r.bold for r in p.runs if r.text.strip())
        if t.isupper() and len(t) < 40:
            section, sub = t.title(), ""
            continue
        if bold and "\t" in p.text:                      # company / role header line
            sub = re.sub(r"\s*\t+\s*", " · ", t)
            continue
        loc = section + (f" · {sub}" if sub else "")
        for c in chunk(t):
            passages.append(Passage(pid=f"resume-{len(passages)}", source=path.name, doc_type="resume",
                                    period="current", location=loc or "header", text=c))
    return Document(file=path.name, doc_type="resume", periods=["current"], passages=passages,
                    text_check="Word document")


def read_plain(path: Path, doc_type: str) -> Document:
    text = path.read_text(encoding="utf-8", errors="replace")
    passages = [Passage(pid=f"{path.stem}-{i}", source=path.name, doc_type=doc_type, period="unknown",
                        location="text", text=c) for i, c in enumerate(chunk(text))]
    return Document(file=path.name, doc_type=doc_type, periods=["unknown"], passages=passages, text_check="text file")


def _period_sort(p: str):
    m = re.search(r"(20\d\d)", p)
    year = int(m.group(1)) if m else 9999
    half = 2 if re.search(r"H2|Year-end", p) else 1
    return (year, half, p)


# ------------------------------------------------------------------ load everything

def load_documents(folder: Path) -> Tuple[List[Document], List[str]]:
    """Read every supported file in evidence/. Returns (documents, skipped-file notes).
    verified_metrics.txt is handled by metrics.py, but its items are also searchable."""
    from .metrics import load_metrics            # local import to avoid a cycle

    docs, skipped = [], []
    transcribed = read_transcription(folder)
    for path in sorted(folder.iterdir()):
        name = path.name
        if name.startswith(".") or path.is_dir():
            continue
        low = name.lower()
        try:
            if name == TRANSCRIPTION_FILE:
                continue                                  # merged into the performance review
            if low.endswith(".pdf") and "performance review" in low:
                docs.append(read_manager_reviews(path, transcribed))
            elif low.endswith(".pdf") and "peer review" in low:
                docs.append(read_peer_reviews(path))
            elif low.endswith(".pdf"):
                reader = PdfReader(str(path))
                text = "\n".join((p.extract_text() or "") for p in reader.pages)
                docs.append(Document(file=name, doc_type="other", periods=["unknown"],
                                     passages=[Passage(f"{path.stem}-{i}", name, "other", "unknown", "pdf", c)
                                               for i, c in enumerate(chunk(clean(text)))]))
            elif low.endswith(".md") and "self" in low:
                docs.append(read_self_reviews(path))
            elif low.endswith(".docx") and "resume" in low:
                docs.append(read_resume(path))
            elif low == "verified_metrics.txt":
                docs.append(load_metrics(path).as_document())
            elif low.endswith((".md", ".txt")):
                docs.append(read_plain(path, "other"))
            elif low.endswith(".docx"):
                text = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
                docs.append(Document(file=name, doc_type="other", periods=["unknown"],
                                     passages=[Passage(f"{path.stem}-{i}", name, "other", "unknown", "docx", c)
                                               for i, c in enumerate(chunk(text))]))
            else:
                skipped.append(f"{name}: not a supported file type (PDF, .docx, .md, .txt)")
        except Exception as e:                            # one bad file shouldn't stop the others
            skipped.append(f"{name}: could not be read ({e})")
    return docs, skipped
