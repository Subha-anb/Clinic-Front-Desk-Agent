"""Retrieval over the clinic's approved documents (FAQs, policies, pre-visit instructions, schedules).

BM25 over heading-sized chunks. The corpus is a few dozen short sections, so lexical retrieval is
accurate, explainable and dependency-free; swap in embeddings if the corpus grows to hundreds of pages.
Doctor schedules are generated from config/clinic.json so there is one source of truth.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_STOP = set(
    "a an the and or of to in on at for is are am be do does did i my me you your we our it its "
    "this that can could would should will with what when where which who how there their from "
    "have has had any if as by please hi hello".split()
)
_DAYS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
_SYNONYMS = {
    "xray": "x-ray", "timing": "hours", "timings": "hours", "open": "hours", "close": "hours",
    "cost": "fees", "charges": "fees", "price": "fees", "fee": "fees", "pay": "payment",
    "scan": "ultrasound", "sonography": "ultrasound", "jewelry": "jewellery", "ring": "jewellery",
    "cancel": "cancellation", "reschedule": "rescheduling", "kids": "children", "child": "children",
    "address": "location", "located": "location", "directions": "location", "reach": "location", "parking": "parking", "report": "reports",
}


def _stem(tok: str) -> str:
    for suffix in ("ing", "es", "s", "ed"):
        if len(tok) > 4 and tok.endswith(suffix):
            return tok[: -len(suffix)]
    return tok


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", text.lower())
    out = []
    for t in toks:
        if t in _STOP:
            continue
        t = _SYNONYMS.get(t, t)
        parts = [t] if t == "x-ray" else t.split("-")
        out.extend(_stem(p) for p in parts)
    return out


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str
    title: str
    text: str

    def render(self) -> str:
        return f"[{self.source} > {self.title}]\n{self.text}"


def schedule_chunks(clinic: dict) -> list[Chunk]:
    chunks = []
    for doc in clinic["doctors"]:
        lines = [
            f"{_DAYS[d]}: {', '.join(s.replace('-', ' to ') for s in spans)}"
            for d, spans in doc["schedule"].items()
        ]
        text = (
            f"{doc['name']} ({doc['specialty']}) is available:\n" + "\n".join(lines) +
            f"\nAppointments are {doc['slot_minutes']} minutes. Book on WhatsApp by asking for a slot."
        )
        chunks.append(Chunk(f"schedule:{doc['id']}", "Doctor schedules", f"{doc['name']} schedule availability", text))
    return chunks


class KnowledgeBase:
    def __init__(self, knowledge_dir: Path, clinic: dict, k1: float = 1.4, b: float = 0.75):
        self.chunks: list[Chunk] = []
        for path in sorted(knowledge_dir.glob("*.md")):
            self.chunks.extend(self._split(path))
        self.chunks.extend(schedule_chunks(clinic))
        self.k1, self.b = k1, b
        # Titles count double: they are short, precise summaries of the section.
        self._docs = [Counter(tokenize(c.title) * 2 + tokenize(c.text)) for c in self.chunks]
        self._lens = [sum(d.values()) for d in self._docs]
        self._avg = sum(self._lens) / max(len(self._lens), 1)
        df = Counter(t for d in self._docs for t in d)
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @staticmethod
    def _split(path: Path) -> list[Chunk]:
        text = path.read_text(encoding="utf-8")
        source = re.search(r"^# (.+)$", text, re.M)
        source_name = source.group(1).strip() if source else path.stem
        parts = re.split(r"^## (.+)$", text, flags=re.M)
        chunks = []
        intro = re.sub(r"^# .+$", "", parts[0], flags=re.M).strip()
        if intro:
            chunks.append(Chunk(f"{path.stem}:intro", source_name, "About these instructions", intro))
        for title, body in zip(parts[1::2], parts[2::2]):
            slug = re.sub(r"\W+", "-", title.lower()).strip("-")
            chunks.append(Chunk(f"{path.stem}:{slug}", source_name, title.strip(), body.strip()))
        return chunks

    def search(self, query: str, k: int = 3) -> list[tuple[Chunk, float]]:
        q = tokenize(query)
        scores = []
        for i, doc in enumerate(self._docs):
            s = 0.0
            for t in q:
                tf = doc.get(t, 0)
                if tf:
                    norm = tf + self.k1 * (1 - self.b + self.b * self._lens[i] / self._avg)
                    s += self._idf[t] * tf * (self.k1 + 1) / norm
            if s > 0:
                scores.append((self.chunks[i], s))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]
