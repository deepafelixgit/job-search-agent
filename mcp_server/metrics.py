"""verified_metrics.txt: the authoritative source for numbers.

Structure of the file (as the candidate wrote it):
  - "Rules for any AI using this file:" followed by "- " rule lines
  - sections like  === QUALITY AND RELIABILITY ===
  - inside a section, an optional program line (e.g. "Example Product pairing (internal: Codename)")
    followed by "- " items; indented lines continue the item above
  - === PHRASES TO AVOID ===  more rules
  - === CODENAMES (internal -> use in outbound documents) ===  "Codename -> Product Name"
Lines marked [CONFIRM] must not be used until the candidate resolves them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class MetricItem:
    section: str
    program: str
    text: str
    line: int
    needs_confirmation: bool = False


@dataclass
class Metrics:
    file: str
    rules: List[str] = field(default_factory=list)
    phrases_to_avoid: List[str] = field(default_factory=list)
    codenames: Dict[str, str] = field(default_factory=dict)      # internal -> commercial
    items: List[MetricItem] = field(default_factory=list)

    # ---------------------------------------------------------- lookups
    def programs(self) -> List[str]:
        seen = []
        for it in self.items:
            if it.program not in seen:
                seen.append(it.program)
        return seen

    def expand_codenames(self, text: str) -> str:
        """Add the commercial name next to any internal codename, so searches find both."""
        out = text
        for code, name in self.codenames.items():
            word = code.split(" (")[0]
            if re.search(rf"\b{re.escape(word)}\b", text, re.I):
                out += f" {name}"
        return out

    def codenames_used_any_case(self, text: str) -> Dict[str, str]:
        return {code.split(" (")[0]: name.split(";")[0].split(",")[0].strip()
                for code, name in self.codenames.items()
                if re.search(rf"\b{re.escape(code.split(' (')[0])}\b", text, re.I)}

    def codenames_used(self, text: str) -> Dict[str, str]:
        return {code.split(" (")[0]: name for code, name in self.codenames.items()
                if re.search(rf"\b{re.escape(code.split(' (')[0])}\b", text)}

    def lookup(self, program: Optional[str]) -> List[MetricItem]:
        if not program:
            return list(self.items)
        # 1. whole-name matches: the query itself, or the commercial name of a codename
        phrases = [program.strip().lower()] + [n.lower() for n in self.codenames_used_any_case(program).values()]
        phrase_hits = [it for it in self.items
                       if any(re.search(rf"(?<![a-z0-9]){re.escape(ph)}(?![a-z0-9])",
                                        f"{it.section} {it.program} {it.text}".lower()) for ph in phrases if ph)]
        if phrase_hits:
            return phrase_hits
        # 2. otherwise, items sharing the most words
        q = self.expand_codenames(program).lower()
        words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 1 and w not in _STOP]
        scored = []
        for it in self.items:
            hay = f"{it.section} {it.program} {it.text}".lower()
            hits = sum(1 for w in set(words) if re.search(rf"\b{re.escape(w)}", hay))
            in_header = sum(1 for w in set(words) if re.search(rf"\b{re.escape(w)}", f"{it.section} {it.program}".lower()))
            if hits:
                scored.append((in_header * 2 + hits, it))
        if not scored:
            return []
        best = max(s for s, _ in scored)
        return [it for s, it in scored if s >= max(1, best * 0.5)]

    def as_document(self):
        from .documents import Document, Passage
        passages = [Passage(pid=f"metric-{i}", source=self.file, doc_type="verified metrics",
                            period="2020-2026 (Meta)", location=f"{it.section} › {it.program} (line {it.line})",
                            text=f"{it.program}: {it.text}",
                            extra={"status": "NOT USABLE until confirmed ([CONFIRM])"} if it.needs_confirmation else {})
                    for i, it in enumerate(self.items)]
        return Document(file=self.file, doc_type="verified metrics", periods=["2020-2026 (Meta)"],
                        passages=passages, text_check="text file",
                        notes=["Authoritative for numbers: overrides any other document."])


_STOP = {"the", "and", "for", "of", "in", "on", "to", "a", "an", "with", "my", "metrics", "metric", "program"}


def load_metrics(path: Path) -> Metrics:
    m = Metrics(file=path.name)
    section, program = "", ""
    mode = "header"
    current: Optional[MetricItem] = None
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            current = None
            continue
        sec = re.match(r"^===\s*(.+?)\s*===$", line.strip())
        if sec:
            section, program, current = sec.group(1), "", None
            low = section.lower()
            mode = "codenames" if low.startswith("codenames") else "avoid" if "avoid" in low else "items"
            continue
        if mode == "header":
            if line.lstrip().startswith("- "):
                m.rules.append(line.lstrip()[2:].strip())
            elif line.startswith((" ", "\t")) and m.rules:
                m.rules[-1] += " " + line.strip()
            continue
        if mode == "codenames":
            if line.lstrip().startswith("- ") and "->" in line:
                left, right = line.lstrip()[2:].split("->", 1)
                m.codenames[left.strip()] = right.strip()
                current = MetricItem("codenames", left.strip(), right.strip(), n)
            elif line.startswith((" ", "\t")) and current is not None:
                m.codenames[current.program] += " " + line.strip()
            continue
        if mode == "avoid":
            if line.lstrip().startswith("- "):
                m.phrases_to_avoid.append(line.lstrip()[2:].strip())
            elif line.startswith((" ", "\t")) and m.phrases_to_avoid:
                m.phrases_to_avoid[-1] += " " + line.strip()
            continue
        # items mode
        if line.lstrip().startswith("- "):
            text = line.lstrip()[2:].strip()
            current = MetricItem(section, program or section, text, n,
                                 needs_confirmation="[CONFIRM]" in text)
            m.items.append(current)
        elif line.startswith((" ", "\t")) and current is not None:
            current.text += " " + line.strip()            # continuation of the item above
            current.needs_confirmation |= "[CONFIRM]" in line
        else:
            program, current = line.strip(), None         # a program heading line
    return m
