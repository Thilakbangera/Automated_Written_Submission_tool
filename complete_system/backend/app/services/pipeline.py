from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .extract import (
    extract_figure_descriptions_from_spec,
    extract_technical_advancement_from_spec,
    parse_amended_claims,
    parse_case_meta_from_fer_or_hn,
    parse_claims_from_specification,
    read_pdf_text,
)
from .template import replace_placeholders

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "templates", "ws_master_v1.docx")


def _sanitize_filename(s: str) -> str:
    for ch in ["\\", "/", ":", "*", "?", '"', "<", ">", "|", "\n", "\r", "\t"]:
        s = s.replace(ch, "_")
    s = re.sub(r"\s+", "_", s.strip())
    return s or "UNKNOWN"


def _first_nonempty(*vals: str) -> str:
    for v in vals:
        if v and v.strip():
            return v.strip()
    return ""


def _extract_hearing_duration_fallback(hn_text: str) -> str:
    txt = hn_text or ""
    m = re.search(r"\bfor\s*\(?\s*([0-9]{1,3}\s*(?:minutes?|mins?|hours?|hrs?))\s*\)?", txt, re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"Hearing\s+Duration\s*[:\-]\s*([^\n]+)", txt, re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"\bDuration\s*[:\-]\s*([0-9]{1,3}\s*(?:minutes?|mins?|hours?|hrs?)(?:\s*[0-9]{1,2}\s*(?:minutes?|mins?))?)", txt, re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(
        r"(\d{1,2}:\d{2})\s*(?:HRS|IST|AM|PM)?\s*(?:to|\-|–|—)\s*(\d{1,2}:\d{2})",
        txt,
        re.I,
    )
    if m:
        try:
            sh, sm = [int(p) for p in m.group(1).split(":")]
            eh, em = [int(p) for p in m.group(2).split(":")]
            start = sh * 60 + sm
            end = eh * 60 + em
            if end < start:
                end += 24 * 60
            mins = end - start
            if mins > 0:
                return f"{mins} minutes" if mins % 60 else f"{mins // 60} hour" + ("s" if mins // 60 != 1 else "")
        except Exception:
            pass
    return ""


def _extract_hn_dispatch_fallback(hn_text: str) -> str:
    txt = hn_text or ""
    date_re = r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}"
    lines = [ln.strip() for ln in txt.splitlines() if ln and ln.strip()]
    patterns = [
        rf"date\s+of\s+dispatch(?:\s*/\s*email)?\s*[:\-]?\s*({date_re})",
        rf"dispatch\s+date\s*[:\-]?\s*({date_re})",
        rf"\bdispatch(?:ed)?\s+on\s*[:\-]?\s*({date_re})",
    ]
    for pat in patterns:
        m = re.search(pat, txt, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()

    for ln in lines[:40]:
        low = ln.lower()
        if "hearing date" in low or "date & time" in low or "time" in low:
            continue
        if re.match(r"^date\s*[:\-]", low):
            m = re.search(date_re, ln)
            if m:
                return re.sub(r"\s+", " ", m.group(0)).strip()
        if re.fullmatch(date_re, ln):
            return re.sub(r"\s+", " ", ln).strip()

    m = re.search(rf"hearing\s+notice\s+(?:is\s+)?(?:dated|date)\s*[:\-]?\s*({date_re})", txt, re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()

    for ln in lines[:120]:
        low = ln.lower()
        if "dispatch" in low:
            m = re.search(date_re, ln)
            if m:
                return re.sub(r"\s+", " ", m.group(0)).strip()

    return ""


def _strip_line_number_artifacts(text: str) -> str:
    t = text or ""
    if not t:
        return ""
    t = re.sub(r"(?m)^\s*\d{1,3}\s*$", "", t)
    t = re.sub(r"(?m)^\s*\d{1,3}\s+(?=[A-Za-z\[\(])", "", t)

    def repl(m: re.Match) -> str:
        try:
            num = int(m.group(1))
        except Exception:
            return m.group(0)
        if 1 <= num <= 400 and num % 5 == 0:
            return " "
        return m.group(0)

    t = re.sub(r"(?<=[A-Za-z\)])\s+(\d{1,3})\s+(?=[A-Za-z\(\[])", repl, t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _is_spec_noise_line(line: str) -> bool:
    ln = re.sub(r"\s+", " ", (line or "")).strip()
    if not ln:
        return True
    low = ln.lower()
    if re.fullmatch(r"\d+\s*/\s*\d+", low):
        return True
    if re.fullmatch(r"page\s*\d+(\s*of\s*\d+)?", low):
        return True
    if re.fullmatch(r"date\s*[:\-]\s*\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", low):
        return True
    if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", low):
        return True
    if "(cid:" in low:
        return True
    return False


def _line_heading_remainder(line: str, heading_patterns: List[str]) -> Tuple[bool, str]:
    ln = re.sub(r"\s+", " ", (line or "")).strip()
    if not ln:
        return False, ""
    for pat in heading_patterns:
        m = re.match(rf"^(?:{pat})\s*:?\s*(.*)$", ln, re.I)
        if m:
            return True, (m.group(1) or "").strip()
    return False, ""


def _line_matches_heading(line: str, heading_patterns: List[str]) -> bool:
    ok, _ = _line_heading_remainder(line, heading_patterns)
    return ok


def _extract_spec_section_block(spec_text: str, start_headings: List[str], end_headings: List[str]) -> str:
    txt = spec_text or ""
    if not txt.strip():
        return ""

    lines = [ln.rstrip() for ln in txt.splitlines()]
    start_idx = -1
    start_tail = ""
    for i, ln in enumerate(lines):
        ok, tail = _line_heading_remainder(ln, start_headings)
        if ok:
            start_idx = i
            start_tail = tail
            break
    if start_idx < 0:
        return ""

    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        if _line_matches_heading(lines[j], end_headings):
            end_idx = j
            break

    block_lines = []
    if start_tail:
        block_lines.append(start_tail)
    block_lines.extend(lines[start_idx + 1 : end_idx])
    cleaned: List[str] = []
    prev_blank = False
    for ln in block_lines:
        if not (ln or "").strip():
            if cleaned and not prev_blank:
                cleaned.append("")
            prev_blank = True
            continue
        if _is_spec_noise_line(ln):
            continue
        norm = re.sub(r"[ \t]+", " ", ln).rstrip()
        norm = re.sub(r"^\s*\d{1,3}\s+(?=[A-Za-z\[\(])", "", norm)
        norm = norm.strip()
        if not norm:
            continue
        cleaned.append(norm.strip())
        prev_blank = False

    return _strip_line_number_artifacts("\n".join(cleaned).strip())


def _format_spec_block_for_ws(block: str) -> str:
    txt = _strip_line_number_artifacts(block or "")
    if not txt:
        return ""

    lines = [re.sub(r"\s+", " ", ln).strip() for ln in txt.splitlines()]
    paras: List[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur:
            p = cur.strip()
            p = re.sub(r"\s+([,.;:])", r"\1", p)
            p = re.sub(r"\(\s+", "(", p)
            p = re.sub(r"\s+\)", ")", p)
            if p:
                paras.append(p)
            cur = ""

    for ln in lines:
        if not ln:
            flush()
            continue
        if re.match(r"^\[\d{4}\]", ln):
            flush()
            cur = ln
            continue
        if cur:
            if cur.endswith("-"):
                cur = cur[:-1] + ln
            else:
                cur += " " + ln
        else:
            cur = ln
    flush()

    return "\n\n".join(paras).strip()


def _extract_tech_problem(spec_text: str) -> str:
    """Copy BACKGROUND OF INVENTION section text (without generated wording)."""
    block = _extract_spec_section_block(
        spec_text,
        start_headings=[
            r"BACKGROUND\s+OF\s+THE\s+INVENTION",
            r"BACKGROUND\s+OF\s+INVENTION",
            r"BACKGROUND",
        ],
        end_headings=[
            r"SUMMARY\s+OF\s+THE\s+INVENTION",
            r"SUMMARY",
            r"BRIEF\s+SUMMARY",
            r"OBJECTIVE\s+OF\s+THE\s+INVENTION",
            r"OBJECT\s+OF\s+INVENTION",
            r"DETAILED\s+DESCRIPTION(?:\s+OF\s+THE\s+INVENTION|\s+OF\s+INVENTION)?",
            r"BRIEF\s+DESCRIPTION(?:\s+OF\s+DRAWINGS?)?",
            r"CLAIMS?",
        ],
    )
    return _format_spec_block_for_ws(block)


def _extract_tech_solution(spec_text: str) -> str:
    """Copy SUMMARY section text (without generated wording)."""
    block = _extract_spec_section_block(
        spec_text,
        start_headings=[
            r"SUMMARY\s+OF\s+THE\s+INVENTION",
            r"SUMMARY",
            r"BRIEF\s+SUMMARY",
        ],
        end_headings=[
            r"BRIEF\s+DESCRIPTION(?:\s+OF\s+DRAWINGS?)?",
            r"DETAILED\s+DESCRIPTION(?:\s+OF\s+THE\s+INVENTION|\s+OF\s+INVENTION)?",
            r"CLAIMS?",
        ],
    )
    return _format_spec_block_for_ws(block)


def _extract_tech_effect(spec_text: str) -> str:
    """Extract technical effect as complete section/paragraph blocks from spec text."""
    txt = (spec_text or "").strip()
    if not txt:
        return ""

    explicit = _extract_spec_section_block(
        txt,
        start_headings=[
            r"TECHNICAL\s+EFFECTS?",
            r"TECHNICAL\s+ADVANTAGES?",
            r"ADVANTAGES?\s+OF\s+THE\s+INVENTION",
            r"TECHNICAL\s+CONTRIBUTION",
            r"EFFECTS?\s+OF\s+THE\s+INVENTION",
        ],
        end_headings=[
            r"BRIEF\s+DESCRIPTION(?:\s+OF\s+DRAWINGS?)?",
            r"DETAILED\s+DESCRIPTION(?:\s+OF\s+THE\s+INVENTION|\s+OF\s+INVENTION)?",
            r"CLAIMS?",
        ],
    )
    explicit_fmt = _format_spec_block_for_ws(explicit)
    if explicit_fmt:
        return explicit_fmt

    effect_kw = re.compile(
        r"\b("
        r"reduce|reduces|reduced|decrease|lower|minimi|save|faster|speed|latency|delay|time|cost|power|memory|bandwidth|overhead|complexity|errors?|noise|loss|"
        r"improv|improves|improved|enhanc|enhances|enhanced|efficient|efficiency|accurac|reliab|robust|secure|security|stability|throughput|performance|"
        r"thereby|thus|hence|as\s+a\s+result|results?\s+in|leads?\s+to|enables?|facilitates?|achieves?"
        r")\w*\b",
        re.I,
    )

    numbered = re.findall(r"(?ms)(\[\d{4}\].*?)(?=\n\s*\[\d{4}\]|\Z)", txt)
    picked: List[str] = []
    for p in numbered:
        pp = _format_spec_block_for_ws(p)
        pp = re.sub(r"(?:\s|\n)*(?:CLAIMS?|ABSTRACT|WE CLAIM|WHAT IS CLAIMED IS)\s*$", "", pp, flags=re.I).strip()
        if not pp:
            continue
        if effect_kw.search(pp):
            picked.append(pp)
        if len(picked) >= 4:
            break
    if picked:
        return "\n\n".join(picked).strip()

    return _format_spec_block_for_ws(_extract_tech_solution(spec_text))


def _extract_reply_3k(hn_text: str) -> str:
    """Capture the examiner's 3(k) reasoning from the hearing notice text."""
    m = re.search(
        r"Claims\s+1-10\s+are\s+method\s+claims.*?(?=Therefore,\s*the\s*claims\s*1-\s*10|Therefore,\s*the\s*claims\s*1-11|\Z)",
        hn_text,
        re.I | re.S,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(0)).strip()[:1800]
    m = re.search(r"prima\s+facie\s+falls\s+within\s+scope\s+of\s+clause\s*\(k\).*?(?=Therefore,|\Z)", hn_text, re.I | re.S)
    if m:
        return re.sub(r"\s+", " ", m.group(0)).strip()[:1800]
    return ""


def _agent_from_drawings(drawings_path: Optional[str]) -> str:
    if not drawings_path:
        return ""
    try:
        txt = read_pdf_text(drawings_path)
    except Exception:
        return ""
    m = re.search(r"\n\s*([A-Z][A-Za-z ]+?)\s*\n\s*Patent\s+Agent", txt)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _format_ws_date_today() -> str:
    return datetime.now().strftime("%d-%m-%Y")


def _build_claim1_features(claim1_text: str) -> str:
    """Build preamble + key features text for the left table column."""
    if not claim1_text:
        return ""
    txt = re.sub(r"\s+", " ", claim1_text).strip().rstrip(".")

    m = re.search(r"^(.*?\bcomprising\b\s*:?)\s*(.*)$", txt, re.I)
    if not m:
        return txt + "."

    preamble = m.group(1).strip()
    rest = m.group(2).strip()
    if len(rest) < 120:
        return f"{preamble}\n{rest}."

    wherein_parts = re.split(r"\s*(?=\bwherein\b)", rest, flags=re.I)
    head = wherein_parts[0].strip(" ;,")
    wherein_clauses = [p.strip(" ;,") for p in wherein_parts[1:] if p.strip()]

    features = []
    head_parts = re.split(r"\s*,\s*|\s+\band\b\s+|\s+\bhaving\b\s+", head, flags=re.I)
    head_parts = [p.strip() for p in head_parts if p and len(p.strip()) > 8]
    for hp in head_parts[:4]:
        features.append(hp)
    for wc in wherein_clauses[:4]:
        features.append(wc)

    seen = set()
    cleaned = []
    for f in features:
        key = re.sub(r"\W+", "", f.lower())
        if key and key not in seen:
            seen.add(key)
            cleaned.append(f)

    if not cleaned:
        return preamble + "."
    return preamble + "\n" + "\n".join(cleaned) + "."


def _dx_labels_from_prior_arts_text(prior_arts_text: str) -> List[str]:
    labs = []
    for ln in (prior_arts_text or "").splitlines():
        m = re.match(r"\s*(D\d+)\s*:", ln.strip(), flags=re.I)
        if m:
            d = m.group(1).upper()
            if d not in labs:
                labs.append(d)
    return labs


def _dx_range_string(labels: List[str]) -> str:
    """Return D-range text, e.g. D1-D3 or D1, D3."""
    if not labels:
        return ""
    nums = []
    for d in labels:
        try:
            nums.append(int(re.sub(r"\D", "", d)))
        except Exception:
            pass
    nums = sorted(set(nums))
    if not nums:
        return ""
    if nums == list(range(nums[0], nums[-1] + 1)) and len(nums) >= 2:
        return f"D{nums[0]}-D{nums[-1]}"
    if len(nums) == 1:
        return f"D{nums[0]}"
    return ", ".join([f"D{n}" for n in nums])


def _dx_and_string(labels: List[str]) -> str:
    """Return D-join text, e.g. D1 and D2 / D1, D2 and D3."""
    if not labels:
        return ""
    nums = []
    for d in labels:
        try:
            nums.append(int(re.sub(r"\D", "", d)))
        except Exception:
            pass
    nums = sorted(set(nums))
    ds = [f"D{n}" for n in nums] if nums else labels
    if len(ds) == 1:
        return ds[0]
    if len(ds) == 2:
        return f"{ds[0]} and {ds[1]}"
    return ", ".join(ds[:-1]) + f" and {ds[-1]}"


def _normalize_ws_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _normalize_prior_art_entries(prior_arts_entries: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
    """Normalize incoming prior-art entries and assign stable D-labels."""
    normalized: List[Dict[str, str]] = []
    used_nums: set[int] = set()
    next_num = 1

    for raw in prior_arts_entries or []:
        if not isinstance(raw, dict):
            continue

        raw_label = str(raw.get("label", "")).strip().upper()
        m = re.fullmatch(r"D(\d+)", raw_label)
        num = int(m.group(1)) if m else 0
        if num <= 0 or num in used_nums:
            while next_num in used_nums:
                next_num += 1
            num = next_num

        used_nums.add(num)
        next_num = max(next_num, num + 1)

        abstract = _normalize_ws_text(str(raw.get("abstract", "")))
        diagram_image_path = _normalize_ws_text(str(raw.get("diagram_image_path", "")))
        summary = _normalize_ws_text(str(raw.get("summary", "")))
        if not (abstract or summary or diagram_image_path):
            continue

        normalized.append(
            {
                "label": f"D{num}",
                "abstract": abstract,
                "diagram_image_path": diagram_image_path,
                "summary": summary,
            }
        )

    normalized.sort(key=lambda e: int(re.sub(r"\D", "", e["label"]) or "0"))
    return normalized


def _build_prior_arts_list_from_entries(prior_arts: List[Dict[str, str]]) -> str:
    lines = []
    for pa in prior_arts:
        desc = pa.get("summary") or pa.get("abstract") or ""
        if desc:
            lines.append(f"{pa['label']}: {desc}")
    return "\n".join(lines)


def _build_disclosure_from_entries(prior_arts: List[Dict[str, str]]) -> str:
    """Build table right-column text from provided prior-art summaries."""
    lines = []
    for pa in prior_arts:
        disclosure = pa.get("summary") or pa.get("abstract") or ""
        disclosure = disclosure[:900].strip()
        if disclosure:
            lines.append(f"{pa['label']}: {disclosure}")
    return "\n".join(lines)


def _compact_objection_chunk(chunk: str) -> str:
    def _join_wrapped(lines: List[str]) -> str:
        if not lines:
            return ""
        acc = lines[0]
        for ln in lines[1:]:
            if acc.endswith("-"):
                acc = acc[:-1] + ln
            elif acc.endswith("/") or acc.endswith("("):
                acc += ln
            else:
                acc += " " + ln
        acc = re.sub(r"\s+([,.;:])", r"\1", acc)
        acc = re.sub(r"\(\s+", "(", acc)
        acc = re.sub(r"\s+\)", ")", acc)
        return acc.strip()

    raw = [re.sub(r"[ \t]+", " ", ln).strip() for ln in (chunk or "").splitlines()]
    raw = [ln for ln in raw if ln is not None]
    if not raw:
        return ""

    heading = raw[0] if raw else ""
    body = raw[1:] if len(raw) > 1 else []
    out: List[str] = [heading] if heading else []

    para_buf: List[str] = []

    def flush_para() -> None:
        nonlocal para_buf
        if para_buf:
            out.append(_join_wrapped(para_buf))
            para_buf = []

    for ln in body:
        if not ln:
            flush_para()
            if out and out[-1] != "":
                out.append("")
            continue
        expanded = ln
        if re.search(r"following\s+objections\s*:", expanded, re.I):
            expanded = re.sub(r"\s+(D\d+\s*:)", r"\n\1", expanded, flags=re.I)
        expanded = re.sub(r"\s+((?:Similarly,\s*)?Document\s+D\d+\b)", r"\n\1", expanded, flags=re.I)

        for part in [p.strip() for p in expanded.splitlines() if p and p.strip()]:
            if para_buf and re.search(r"following\s+objections\s*:\s*$", para_buf[-1], re.I):
                flush_para()
            if re.match(r"^D\d+\s*:", part, re.I):
                flush_para()
                out.append(part)
                continue
            if re.match(r"^(?:Similarly,\s*)?Document\s+D\d+\b", part, re.I):
                flush_para()
            if re.match(r"^\d+\s*[\.\)]", part) and para_buf:
                flush_para()
            para_buf.append(part)

    flush_para()
    text = "\n".join(out).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _strip_nonpat_heading(chunk: str) -> str:
    lines = [ln.strip() for ln in (chunk or "").splitlines() if ln and ln.strip()]
    if lines and _heading_type(lines[0]) == "nonpat":
        lines = lines[1:]
    return "\n".join(lines).strip()


def _inject_reply_by_drafter_tag(chunk: str) -> str:
    lines = [ln.rstrip() for ln in (chunk or "").splitlines()]
    if not lines:
        return ""
    heading = re.sub(r"\s+", " ", lines[0]).strip()
    if not re.match(r"^(Formal\s+Requirement(?:s)?|Clarity\s+and\s+Conciseness)\b", heading, re.I):
        return chunk.strip()
    for ln in lines:
        if ln.strip().upper() == "[REPLY BY DRAFTER]":
            return chunk.strip()
    if not lines:
        return chunk.strip()
    return "\n".join(lines + ["[REPLY BY DRAFTER]"]).strip()


def _extract_objection_blocks_from_hn(hn_text: str) -> Tuple[str, str]:
    """Return (objections_without_nonpat, nonpat_objection) from HN."""
    txt = (hn_text or "").strip()
    if not txt:
        return "", ""

    lines = [ln.rstrip() for ln in txt.splitlines()]
    heading_indices = [i for i, ln in enumerate(lines) if _heading_type(ln)]
    if not heading_indices:
        return "", ""

    main_chunks: List[str] = []
    nonpat_chunks: List[str] = []
    for i, start in enumerate(heading_indices):
        end = heading_indices[i + 1] if i + 1 < len(heading_indices) else len(lines)
        section_kind = _heading_type(lines[start])
        chunk_lines = []
        for ln in lines[start:end]:
            if _is_hn_noise_line(ln):
                continue
            chunk_lines.append(ln.rstrip())
        chunk = _compact_objection_chunk("\n".join(chunk_lines).strip())
        if not chunk:
            continue
        if section_kind == "nonpat":
            chunk = _strip_nonpat_heading(chunk)
            if chunk:
                nonpat_chunks.append(chunk)
        else:
            chunk = _inject_reply_by_drafter_tag(chunk)
            main_chunks.append(chunk)

    main_block = "\n\n".join(main_chunks).strip()
    nonpat_block = "\n\n".join(nonpat_chunks).strip()
    return main_block, nonpat_block


def _extract_reply_to_objection_block_from_hn(hn_text: str) -> str:
    main_block, _ = _extract_objection_blocks_from_hn(hn_text)
    return main_block


def _heading_type(line: str) -> str:
    ln = re.sub(r"\s+", " ", (line or "").strip())
    if not ln or len(ln) > 120:
        return ""

    if re.match(r"^(?:Non[-\s]?Patentability(?:\s*u/s\s*3(?:\s*\(k\))?)?|Section\s*3(?:\s*\(k\))?)(?:\s*[:\-].*)?$", ln, re.I):
        if re.search(r"\bof\s+the\b", ln, re.I) and ":" not in ln:
            return ""
        return "nonpat"
    if re.match(
        r"^(?:"
        r"Clarity\s+and\s+Conciseness|"
        r"Definitiveness|"
        r"Definiteness|"
        r"Formal\s+Requirement(?:s)?|"
        r"Invention\s+u/s\b.*|"
        r"Other\s+Requirement(?:s)?|"
        r"Prior\s+Art|"
        r"Novelty|"
        r"Inventive\s+Step"
        r")\b.*$",
        ln,
        re.I,
    ):
        return "section"
    return ""


def _is_hn_noise_line(line: str) -> bool:
    ln = (line or "").strip()
    if not ln:
        return True
    low = ln.lower()
    if "(cid:" in low:
        return True
    if re.fullmatch(r"\d+\s*/\s*\d+", low):
        return True
    if re.fullmatch(r"page\s*\d+(\s*of\s*\d+)?", low):
        return True
    if re.search(r"assistant\s+controller\s+of\s+patents", low):
        return True
    if re.search(r"\bthe\s+following\s+objection\(s\)\s+are\s+still\s+outstanding\b", low):
        return True
    if re.fullmatch(r"date\s*[-:]\s*\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", low):
        return True
    if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", low):
        return True
    if sum(1 for ch in ln if ord(ch) > 127) > 8:
        return True
    return False


def _build_prior_art_analysis_sequence(prior_arts: List[Dict[str, str]], claim1_features: str, dx_range: str) -> List[Dict[str, str]]:
    """Build ordered prior-art sequence: abstract -> diagram -> summary for each Dn, then combined difference."""
    if not prior_arts:
        return []

    sequence: List[Dict[str, str]] = []
    for pa in prior_arts:
        abstract = _normalize_ws_text(pa.get("abstract", ""))[:1200]
        summary = _normalize_ws_text(pa.get("summary", ""))[:1200]
        diagram_path = _normalize_ws_text(pa.get("diagram_image_path", ""))

        if abstract:
            sequence.append({"kind": "text", "text": abstract})
        if diagram_path:
            sequence.append({"kind": "image", "path": diagram_path})
        if summary:
            sequence.append({"kind": "text", "text": summary})

    claim_basis = _normalize_ws_text(claim1_features)[:1400]
    focus_bits: List[str] = []
    for pa in prior_arts:
        summary = _normalize_ws_text(pa.get("summary", ""))[:260]
        if summary:
            focus_bits.append(f"{pa['label']} focuses on {summary}")
    prior_focus = "; ".join(focus_bits)
    prior_set = dx_range or ", ".join(pa["label"] for pa in prior_arts)

    diff_text = ""
    if claim_basis and prior_focus:
        diff_text = (
            f"Combined difference over {prior_set}: The claimed invention requires the combined feature set of Claim 1 ({claim_basis}). "
            f"In contrast, {prior_focus}. Accordingly, {prior_set} do not individually or in combination disclose the complete claimed combination."
        )
    elif claim_basis:
        diff_text = (
            f"Combined difference over {prior_set}: The claimed invention requires the combined feature set of Claim 1 ({claim_basis}), "
            "which is not disclosed by the cited prior arts individually or in combination."
        )

    if diff_text:
        sequence.append({"kind": "text", "text": diff_text})
    return sequence


def generate_written_submission(
    hn_path: str,
    specification_path: str,
    prior_arts_entries: Optional[List[Dict[str, Any]]] = None,
    drawings_path: Optional[str] = None,
    amended_claims_path: Optional[str] = None,
    tech_solution_images_paths: Optional[list] = None,
    city: str = "Chennai",
):
    """Generate WS using HN + specification + manually provided prior-arts."""
    hn_meta = parse_case_meta_from_fer_or_hn(hn_path)
    hn_text = read_pdf_text(hn_path)
    formal_objections_reply, nonpat_objection_reply = _extract_objection_blocks_from_hn(hn_text)
    formal_objections_reply = formal_objections_reply or ""

    spec_text = read_pdf_text(specification_path)
    fig_desc_map = extract_figure_descriptions_from_spec(spec_text)

    prior_arts = _normalize_prior_art_entries(prior_arts_entries)
    if not prior_arts:
        raise ValueError("At least one prior-art entry (D1..Dn) is required.")

    prior_arts_list = _build_prior_arts_list_from_entries(prior_arts)
    dx_labels = [pa["label"] for pa in prior_arts]
    dx_range = _dx_range_string(dx_labels)
    dx_and = _dx_and_string(dx_labels)
    d1d2_disclosure = _build_disclosure_from_entries(prior_arts)

    claims: Dict[int, str] = {}
    if amended_claims_path:
        claims = parse_amended_claims(amended_claims_path)
    if not claims:
        claims = parse_claims_from_specification(spec_text)

    ws_date = _format_ws_date_today()

    app_no = _first_nonempty(hn_meta.app_no)
    if not app_no:
        raise ValueError("Application number not found in Hearing Notice")

    controller = _first_nonempty(hn_meta.controller)
    if not controller:
        raise ValueError("Controller name not found in Hearing Notice")

    agents = "Adv. Pranav Bhat (Registered Indian Patent Agent - IN/PA 4580)"
    applicant = _first_nonempty(hn_meta.applicant)
    if not applicant:
        raise ValueError("Applicant name not found in Hearing Notice")

    dear_salutation = "Sir"

    hn_dispatch = _first_nonempty(hn_meta.hn_dispatch_date, _extract_hn_dispatch_fallback(hn_text))
    if not hn_dispatch:
        raise ValueError("Hearing Notice dispatch date not found in Hearing Notice")

    hearing_date = _first_nonempty(hn_meta.hearing_date)
    hearing_time = _first_nonempty(hn_meta.hearing_time)
    if hearing_time and re.fullmatch(r"\d{1,2}:\d{2}", hearing_time.strip()):
        hearing_time_fmt = f"{hearing_time} HRS (IST)"
    else:
        hearing_time_fmt = hearing_time or ""
    hearing_duration = _first_nonempty(getattr(hn_meta, "hearing_duration", ""), _extract_hearing_duration_fallback(hn_text))

    hearing_mode = _first_nonempty(hn_meta.hearing_mode, "Video Conferencing")
    hearing_dt_parts = [p for p in [hearing_date, hearing_time_fmt] if p]
    if hearing_duration:
        hearing_dt_parts.append(f"Duration: {hearing_duration}")
    hearing_dt = " / ".join(hearing_dt_parts)

    participants = f"1. {controller}\n2. {agents}"

    fer_reply_date = _first_nonempty(hn_meta.fer_reply_date)
    fer_date = _first_nonempty(hn_meta.fer_date, hn_meta.fer_dispatch_date)

    claim1_text = claims.get(1, "")
    claim1_features = _build_claim1_features(claim1_text)
    prior_art_analysis_sequence = _build_prior_art_analysis_sequence(prior_arts, claim1_features or claim1_text, dx_range)

    technical_adv = extract_technical_advancement_from_spec(spec_text) or ""
    tech_problem = _extract_tech_problem(spec_text) or ""
    tech_solution = _extract_tech_solution(spec_text) or ""
    tech_effect = _extract_tech_effect(spec_text) or ""
    reply_3k = _first_nonempty(nonpat_objection_reply, _extract_reply_3k(hn_text))

    claim_nos = sorted([n for n in claims.keys() if n >= 1])
    claim_max = claim_nos[-1] if claim_nos else 0
    if not claim_nos:
        raise ValueError("No claims could be parsed from amended claims or specification")
    claims_range = f"1-{claim_nos[-1]}" if claim_nos[-1] != 1 else "1"

    extra_claims_block = ""
    range_for_text = dx_range or "D1"
    for n in claim_nos:
        if n <= 10:
            continue
        ctext = (claims.get(n, "") or "").strip()
        if not ctext:
            continue
        extra_claims_block += (
            f"\n\nRegarding Claim {n}:\n"
            f"Applicant has reviewed the entire application of {range_for_text} and found that nowhere in the entire applications does {range_for_text} describe or reasonably suggest the following features \"{ctext}\". "
            f"Apart from the above, Applicant believes that dependent claim {n} is allowable not only by virtue of their dependency from patentable independent claim 1, respectively, but also by virtue of the additional features of the invention they define. "
            f"The dependent claims describe various embodiments of the invention that can be combined to form the invention. The subject matter described in the instant application are different from {range_for_text} so as the features described in dependent claim {n}."
        )

    mapping = {
        "{{WS_DATE}}": ws_date,
        "{{APP_NO}}": app_no,
        "{{FILED_ON}}": _first_nonempty(hn_meta.filed_on),
        "{{APPLICANT_NAME}}": applicant,
        "{{CONTROLLER_NAME}}": controller,
        "{{AGENT_NAMES}}": agents,
        "{{DEAR_SALUTATION}}": dear_salutation,
        "{{CITY}}": city,
        "{{HN_DISPATCH_DATE}}": hn_dispatch,
        "{{HEARING_DATE}}": hearing_date,
        "{{HEARING_MODE}}": hearing_mode,
        "{{HEARING_DATE_TIME}}": hearing_dt,
        "{{PARTICIPANTS}}": participants,
        "{{FER_REPLY_DATE}}": fer_reply_date,
        "{{FER_DATE}}": fer_date,
        "{{PRIOR_ARTS_LIST}}": prior_arts_list,
        "{{DX_RANGE}}": dx_range,
        "{{DX_AND}}": dx_and,
        "{{CLAIMS_RANGE}}": claims_range,
        "{{AMENDED_CLAIM_1}}": claim1_text,
        "{{AMENDED_CLAIM_2}}": claims.get(2, ""),
        "{{AMENDED_CLAIM_3}}": claims.get(3, ""),
        "{{AMENDED_CLAIM_4}}": claims.get(4, ""),
        "{{AMENDED_CLAIM_5}}": claims.get(5, ""),
        "{{AMENDED_CLAIM_6}}": claims.get(6, ""),
        "{{AMENDED_CLAIM_7}}": claims.get(7, ""),
        "{{AMENDED_CLAIM_8}}": claims.get(8, ""),
        "{{AMENDED_CLAIM_9}}": claims.get(9, ""),
        "{{AMENDED_CLAIM_10}}": claims.get(10, ""),
        "{{EXTRA_CLAIMS_BLOCK}}": extra_claims_block.strip(),
        "{{CLAIM1_FEATURES}}": claim1_features,
        "{{D1D2_DISCLOSURE}}": d1d2_disclosure,
        "{{TECHNICAL_ADVANCEMENT}}": technical_adv,
        "{{REPLY_3K}}": reply_3k,
        "{{TECH_PROBLEM}}": tech_problem,
        "{{TECH_SOLUTION}}": tech_solution,
        "{{TECH_EFFECT}}": tech_effect,
        "{{FORMAL_OBJECTIONS_REPLY}}": formal_objections_reply,
    }

    mapping["__CLAIM_MAX__"] = claim_max
    mapping["__FIG_DESC_MAP__"] = fig_desc_map
    mapping["__PRIOR_ART_SEQUENCE__"] = prior_art_analysis_sequence

    out_dir = os.path.join(tempfile.gettempdir(), "ws_tool_outputs")
    os.makedirs(out_dir, exist_ok=True)

    safe_app = _sanitize_filename(hn_meta.app_no or "APP")
    out_name = f"Written_Submission_{safe_app}.docx"
    out_path = os.path.join(out_dir, out_name)

    if tech_solution_images_paths:
        mapping["__TECH_SOLUTION_IMAGES__"] = tech_solution_images_paths
    replace_placeholders(TEMPLATE_PATH, out_path, mapping)
    return out_path, out_name
