#!/usr/bin/env python3
"""
scripts/chunk_and_embed.py

Chunks and embeds earnings-call transcripts into transcript_chunks.
Handles two transcript formats:
  - JSON-wrapped  :  "transcript": "Speaker: text\nSpeaker: text\n..."
  - Plain paragraph:  paragraphs separated by blank lines (no speaker labels)
"""

import sys
import os
import re
import json
import time
import requests
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))

from backend.app.db.database import SessionLocal
from backend.app.db.models import Company, EarningsCall, TranscriptChunk

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL  = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
MAX_TOKENS   = 500
EMBED_BATCH  = 10
OVERLAP_SENTS = 2   # sentences from prev chunk to prepend in Q&A

# Operator name aliases (not always literally "Operator")
OPERATOR_ALIASES = {
    "operator", "leila", "megan", "jayla", "angie", "moderator",
    "conference operator", "host",
}

# Known executive title keywords (longest match wins)
EXEC_TITLE_KW = [
    ("Chief Executive Officer",      "CEO"),
    ("CEO",                           "CEO"),
    ("Chief Financial Officer",       "CFO"),
    ("CFO",                           "CFO"),
    ("Chief Operating Officer",       "COO"),
    ("Chief Revenue Officer",         "CRO"),
    ("Chief Technology Officer",      "CTO"),
    ("Chief Engineering",             "CTO"),
    ("President and",                 "President"),
    ("President,",                    "President"),
    ("Executive Vice President",      "EVP"),
    ("Investor Relations",            "IR"),
    ("Head of Investor",              "IR"),
    ("Finance and Strategy",          "IR"),
]


# ---------------------------------------------------------------------------
# Transcript loading
# ---------------------------------------------------------------------------
def load_transcript_text(fpath: Path) -> str:
    """Read file and return the raw transcript string."""
    raw = fpath.read_text(encoding="utf-8", errors="replace").strip()

    # JSON-fragment format:  "transcript": "..."
    if '"transcript"' in raw[:120]:
        # wrap into valid JSON object and parse
        try:
            data = json.loads("{" + raw.rstrip(",") + "}")
            return data.get("transcript", raw)
        except Exception:
            pass
        # fallback: regex extract
        m = re.search(r'"transcript"\s*:\s*"(.*?)"\s*$', raw, re.DOTALL)
        if m:
            return m.group(1).encode("raw_unicode_escape").decode("unicode_escape")

    return raw   # plain text


# ---------------------------------------------------------------------------
# Parsing into speaker turns
# ---------------------------------------------------------------------------
def _token_count(text: str) -> int:
    return int(len(text.split()) * 1.3)


def parse_turns(text: str) -> list:
    """
    Returns list of dicts: {speaker, text}.
    Detects whether transcript uses 'Speaker: ...' per-line format or
    blank-line-separated paragraphs.
    """
    lines = [l for l in text.split("\n") if l.strip()]

    # Heuristic: if >= 3 of the first 15 non-empty lines look like "Name: text"
    # treat as speaker-prefixed format.
    spk_re = re.compile(r'^([A-Z][^:\n]{1,45}):\s+(.+)')
    speaker_line_count = sum(1 for l in lines[:15] if spk_re.match(l.strip()))

    if speaker_line_count >= 3:
        return _parse_speaker_format(lines, spk_re)
    else:
        return _parse_paragraph_format(text)


def _parse_speaker_format(lines: list, spk_re) -> list:
    turns = []
    cur_speaker = "Unknown"
    cur_parts   = []

    for line in lines:
        line = line.strip()
        m = spk_re.match(line)
        if m:
            if cur_parts:
                turns.append({"speaker": cur_speaker,
                               "text": " ".join(cur_parts).strip()})
            cur_speaker = m.group(1).strip()
            cur_parts   = [m.group(2).strip()]
        else:
            if line:
                cur_parts.append(line)

    if cur_parts:
        turns.append({"speaker": cur_speaker,
                       "text": " ".join(cur_parts).strip()})
    return [t for t in turns if t["text"]]


def _parse_paragraph_format(text: str) -> list:
    paras = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    return [{"speaker": "Unknown", "text": p} for p in paras]


# ---------------------------------------------------------------------------
# Role map extraction
# ---------------------------------------------------------------------------
def build_role_map(turns: list) -> dict:
    """Scan intro turns for 'Name, Title' patterns -> {name: role_str}."""
    intro = " ".join(t["text"] for t in turns[:6])
    role_map = {}

    # Try each exec title keyword
    for full_title, short in EXEC_TITLE_KW:
        # pattern: "Name, [our] Title" or "Title, Name"
        for pat in [
            rf'([A-Z][a-z]+ (?:[A-Z][a-z]+ )?[A-Z][a-z]+),?\s+(?:our\s+)?{re.escape(full_title)}',
            rf'{re.escape(full_title)}[,\s]+([A-Z][a-z]+ (?:[A-Z][a-z]+ )?[A-Z][a-z]+)',
        ]:
            for m in re.finditer(pat, intro, re.IGNORECASE):
                name = m.group(1).strip()
                if name not in role_map:
                    role_map[name] = short
    return role_map


def classify_role(speaker: str, role_map: dict, in_qa: bool) -> str:
    if speaker.lower() in OPERATOR_ALIASES:
        return "Operator"
    # exact name match
    if speaker in role_map:
        return role_map[speaker]
    # partial (first/last name) match
    for mapped_name, role in role_map.items():
        parts = mapped_name.split()
        if any(p == speaker or speaker in parts for p in parts):
            return role
    # In Q&A, if unrecognized -> Analyst; in prepared remarks -> Executive
    return "Analyst" if in_qa else "Executive"


# ---------------------------------------------------------------------------
# Q&A boundary detection
# ---------------------------------------------------------------------------
_QA_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'q\s*&\s*a',
        r'question[- ]and[- ]answer',
        r'begin.{0,25}q\s*&\s*a',
        r'open.{0,20}(floor|line).{0,20}question',
        r'(queue|take).{0,15}first question',
        r'we will now.{0,25}question',
        r'begin the question',
        r'first question will come from',
        r'first question is from',
        r'our first question',
        r'question and answer session',
    ]
]

def find_qa_start(turns: list) -> int:
    """Return index of first turn that signals Q&A, or len(turns) if none found."""
    for i, t in enumerate(turns):
        if any(p.search(t["text"]) for p in _QA_PATTERNS):
            return i
    return len(turns)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def last_n_sentences(text: str, n: int = OVERLAP_SENTS) -> str:
    """Return the last n sentences from text."""
    sents = re.split(r'(?<=[.!?])\s+', text.strip())
    return " ".join(sents[-n:]) if sents else text[-300:]


def split_long_turn(text: str, max_tok: int = MAX_TOKENS) -> list:
    """Split text into sub-chunks <= max_tok tokens at line/sentence boundaries."""
    if _token_count(text) <= max_tok:
        return [text]

    # Try newline-level split first
    lines = [l for l in text.split("\n") if l.strip()]
    chunks, cur, cur_tok = [], [], 0
    for line in lines:
        lt = _token_count(line)
        if cur and cur_tok + lt > max_tok:
            chunks.append(" ".join(cur))
            cur, cur_tok = [], 0
        cur.append(line)
        cur_tok += lt
    if cur:
        chunks.append(" ".join(cur))

    # If any chunk is still too long, split by sentence
    final = []
    for c in chunks:
        if _token_count(c) <= max_tok:
            final.append(c)
            continue
        sents = re.split(r'(?<=[.!?])\s+', c)
        cur, cur_tok = [], 0
        for s in sents:
            st = _token_count(s)
            if cur and cur_tok + st > max_tok:
                final.append(" ".join(cur))
                cur, cur_tok = [], 0
            cur.append(s)
            cur_tok += st
        if cur:
            final.append(" ".join(cur))

    return final if final else [text]


def build_chunks(turns: list, ticker: str, quarter: str,
                 fiscal_year: int, call_date, role_map: dict) -> list:
    """
    Convert speaker turns into chunk records.
    Returns list of dicts ready for DB insertion.
    """
    qa_idx   = find_qa_start(turns)
    chunks   = []
    idx      = 0
    prev_qa_text = ""    # for Q&A overlap

    for ti, turn in enumerate(turns):
        in_qa      = (ti >= qa_idx)
        section    = "Q&A" if in_qa else "Prepared Remarks"
        source_type = "transcript_qa" if in_qa else "transcript_prepared"
        speaker    = turn["speaker"]
        role       = classify_role(speaker, role_map, in_qa)

        sub_texts = split_long_turn(turn["text"])

        for si, sub_text in enumerate(sub_texts):
            # Build overlap prefix for Q&A answers (not questions, not operator)
            overlap_prefix = ""
            if in_qa and prev_qa_text and role not in ("Operator", "Analyst"):
                overlap_prefix = (
                    "[Context from analyst question: "
                    + last_n_sentences(prev_qa_text)
                    + "]\n\n"
                )

            # Enriched content = metadata header + optional overlap + raw text
            header = (
                f"[{ticker} | {quarter} FY{fiscal_year} | {call_date} "
                f"| {section} | {speaker} | {role}]"
            )
            enriched = header + "\n\n" + overlap_prefix + sub_text

            chunk_id = f"{ticker}_{quarter}_{fiscal_year}_chunk_{idx:03d}"

            chunks.append({
                "id":              chunk_id,
                "chunk_index":     idx,
                "content":         sub_text,
                "enriched_content": enriched,
                "source_type":     source_type,
                "section":         section,
                "speaker_name":    speaker,
                "speaker_role":    role,
                "token_count":     _token_count(sub_text),
            })
            idx += 1

            # Track last analyst question for overlap
            if in_qa and role == "Analyst":
                prev_qa_text = sub_text
            elif in_qa and role not in ("Operator",):
                prev_qa_text = ""   # reset after exec response

    return chunks


# ---------------------------------------------------------------------------
# Ollama embedding
# ---------------------------------------------------------------------------
def check_ollama() -> bool:
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        return r.ok
    except Exception:
        return False


def embed_text(text: str) -> list | None:
    """Call Ollama embeddings API; return float list or None on error."""
    try:
        r = requests.post(
            f"{OLLAMA_HOST}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=60,
        )
        if r.ok:
            return r.json().get("embedding")
        print(f"    [WARN] Ollama {r.status_code}: {r.text[:80]}")
        return None
    except Exception as e:
        print(f"    [WARN] Ollama error: {e}")
        return None


def embed_batch(chunks: list) -> int:
    """Embed enriched_content for each chunk in batches. Returns count embedded."""
    embedded = 0
    for i in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[i: i + EMBED_BATCH]
        print(f"    Embedding batch {i // EMBED_BATCH + 1} "
              f"({len(batch)} chunks, indices {i}-{i+len(batch)-1}) ...",
              end=" ", flush=True)
        batch_ok = 0
        for ch in batch:
            vec = embed_text(ch["enriched_content"])
            if vec is not None:
                ch["embedding"] = vec
                batch_ok += 1
        print(f"{batch_ok}/{len(batch)} ok")
        embedded += batch_ok
    return embedded


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    db = SessionLocal()

    # Check Ollama
    ollama_up = check_ollama()
    if not ollama_up:
        print(f"[WARN] Ollama not reachable at {OLLAMA_HOST}. "
              "Chunks will be saved WITHOUT embeddings.")
    else:
        print(f"[OK] Ollama reachable at {OLLAMA_HOST}, model={EMBED_MODEL}")

    ec_rows = (
        db.query(EarningsCall)
        .filter(EarningsCall.status == "downloaded")
        .order_by(EarningsCall.id)
        .all()
    )
    print(f"Found {len(ec_rows)} earnings call(s) with status='downloaded'.\n")

    summary = []

    for ec in ec_rows:
        company = db.get(Company, ec.company_id)
        ticker  = company.ticker
        label   = f"{ticker} {ec.fiscal_quarter} FY{ec.fiscal_year}"
        print(f"--- {label} ---")

        # Skip if already chunked
        existing = (
            db.query(TranscriptChunk)
            .filter_by(earnings_call_id=ec.id)
            .first()
        )
        if existing:
            print(f"  SKIP  already has chunks in DB")
            summary.append({"label": label, "status": "skipped",
                             "total": 0, "prepared": 0, "qa": 0, "embedded": 0})
            continue

        # Load transcript text
        if not ec.raw_transcript_path:
            print(f"  SKIP  no raw_transcript_path")
            summary.append({"label": label, "status": "no_path",
                             "total": 0, "prepared": 0, "qa": 0, "embedded": 0})
            continue

        fpath = Path(ec.raw_transcript_path)
        if not fpath.exists():
            print(f"  SKIP  file not found: {fpath}")
            summary.append({"label": label, "status": "file_missing",
                             "total": 0, "prepared": 0, "qa": 0, "embedded": 0})
            continue

        try:
            text = load_transcript_text(fpath)
        except Exception as e:
            print(f"  ERROR loading transcript: {e}")
            summary.append({"label": label, "status": f"load_error",
                             "total": 0, "prepared": 0, "qa": 0, "embedded": 0})
            continue

        print(f"  Loaded {len(text):,} chars from {fpath.name}")

        # Parse turns
        turns = parse_turns(text)
        print(f"  Parsed {len(turns)} speaker turn(s)")

        # Build role map from intro
        role_map = build_role_map(turns)
        if role_map:
            print(f"  Role map: "
                  + ", ".join(f"{n}={r}" for n, r in list(role_map.items())[:4]))

        # Build chunks
        chunks = build_chunks(
            turns, ticker, ec.fiscal_quarter,
            ec.fiscal_year, ec.call_date, role_map,
        )
        n_prep = sum(1 for c in chunks if c["source_type"] == "transcript_prepared")
        n_qa   = sum(1 for c in chunks if c["source_type"] == "transcript_qa")
        print(f"  Built {len(chunks)} chunks  ({n_prep} prepared, {n_qa} Q&A)")

        # Embed
        n_embedded = 0
        if ollama_up and chunks:
            n_embedded = embed_batch(chunks)

        # Insert into DB
        for ch in chunks:
            tc = TranscriptChunk(
                id               = ch["id"],
                earnings_call_id = ec.id,
                chunk_index      = ch["chunk_index"],
                content          = ch["content"],
                enriched_content = ch["enriched_content"],
                source_type      = ch["source_type"],
                section          = ch["section"],
                speaker_name     = ch["speaker_name"],
                speaker_role     = ch["speaker_role"],
                token_count      = ch["token_count"],
                embedding        = ch.get("embedding"),
            )
            db.add(tc)

        # Update earnings_call status
        ec.status = "embedded" if (n_embedded == len(chunks) and ollama_up) else "chunked"
        db.commit()

        print(f"  Saved {len(chunks)} chunks, {n_embedded} embedded. "
              f"EC status -> '{ec.status}'")
        summary.append({
            "label":    label,
            "status":   ec.status,
            "total":    len(chunks),
            "prepared": n_prep,
            "qa":       n_qa,
            "embedded": n_embedded,
        })
        print()

    db.close()

    # Summary table
    print("=" * 72)
    print(f"{'Company/Quarter':<28} {'Status':<10} "
          f"{'Total':>6} {'Prep':>5} {'Q&A':>5} {'Embed':>6}")
    print("-" * 72)
    for s in summary:
        print(f"  {s['label']:<26} {s['status']:<10} "
              f"{s['total']:>6} {s['prepared']:>5} {s['qa']:>5} {s['embedded']:>6}")
    total_chunks = sum(s["total"]    for s in summary)
    total_embed  = sum(s["embedded"] for s in summary)
    print("=" * 72)
    print(f"  Total chunks: {total_chunks}   Embedded: {total_embed}")


if __name__ == "__main__":
    main()
