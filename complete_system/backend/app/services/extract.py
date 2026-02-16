from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import pdfplumber

try:
    from docx import Document  # type: ignore
except Exception:  # pragma: no cover
    Document = None


@dataclass
class PriorArt:
    label: str
    docno: str
    date: str = ""


@dataclass
class CaseMeta:
    app_no: str = ""
    filed_on: str = ""
    applicant: str = ""
    controller: str = ""
    agents: str = ""
    fer_dispatch_date: str = ""   # FER email/dispatch date (top page)
    hn_dispatch_date: str = ""
    hearing_date: str = ""
    hearing_time: str = ""
    hearing_mode: str = "Video Conferencing"
    fer_date: str = ""            # Sometimes same as fer_dispatch_date
    fer_reply_date: str = ""
    prior_arts: List[PriorArt] = None
    d1_disclosure: str = ""
    d2_disclosure: str = ""


def read_pdf_text(path: str) -> str:
    parts: List[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            parts.append(t)
    return "\n".join(parts)


def read_docx_text(path: str) -> str:
    if Document is None:
        return ""
    doc = Document(path)
    parts: List[str] = []
    for p in doc.paragraphs:
        if p.text:
            parts.append(p.text)
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if p.text:
                        parts.append(p.text)
    return "\n".join(parts)


def read_text_any(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return read_pdf_text(path)
    if ext == ".docx":
        return read_docx_text(path)
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _find_date(patterns: List[str], text: str) -> str:
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return _clean(m.group(1))
    return ""


DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")


def _normalize_date(s: str) -> str:
    s = _clean(s)
    if not s:
        return ""
    s = s.replace("-", "/")
    m = DATE_RE.search(s)
    if not m:
        return ""
    d, mo, y = m.group(1), m.group(2), m.group(3)
    if len(y) == 2:
        y = "20" + y
    return f"{int(d):02d}/{int(mo):02d}/{y}"


def _canonical_docno(s: str) -> str:
    s = (s or "").upper()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[()\-;:,]", "", s)
    return s


def _parse_prior_arts_from_text(text: str) -> List[PriorArt]:
    """Parse D1..Dn prior-arts from FER/HN text across IPO formats and de-duplicate."""
    lines = (text or "").splitlines()
    arts: List[PriorArt] = []
    i = 0
    dx_head = re.compile(r"^\s*(D\d+)\s*[:\-]?\s*(.*)$", re.I)
    stop_re = re.compile(
        r"^\s*(FORMAL\s+REQUIREMENT|REPLY\s+TO\s+OBJECTION|NOVELTY|INVENTIVE|NON[-\s]*PATENT|CLAIM|HEARING|NAME\s+OF\s+THE\s+CONTROLLER)\b",
        re.I,
    )

    while i < len(lines):
        raw = lines[i]
        m = dx_head.match(raw)
        if not m:
            i += 1
            continue

        label = _clean(m.group(1)).upper()
        rest = (m.group(2) or "").strip()

        block_parts = [rest] if rest else []
        j = i + 1
        while j < len(lines) and len(block_parts) < 6:
            nxt = lines[j]
            if dx_head.match(nxt):
                break
            if stop_re.match(nxt):
                break
            if _clean(nxt):
                block_parts.append(_clean(nxt))
            j += 1

        block = re.sub(r"\s+", " ", " ".join(block_parts)).strip()

        date = ""
        md = re.search(
            r"Publication\s*Date\s*[:\-]*\s*([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4})",
            block,
            re.I,
        )
        if md:
            date = _normalize_date(md.group(1))
        if not date:
            md = re.search(r"\(([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4})\)", block)
            if md:
                date = _normalize_date(md.group(1))
        if not date:
            date = _normalize_date(block)

        docno = block
        if date:
            mdt = re.search(r"[0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4}", docno)
            if mdt:
                docno = docno[: mdt.start()].strip()
            docno = re.sub(r"Publication\s*Date\s*[:\-]*\s*$", "", docno, flags=re.I).strip()

        docno = re.sub(r"\bwhole\s+doc(?:ument)?\b.*$", "", docno, flags=re.I).strip()
        docno = docno.strip(" ;,")

        if docno:
            arts.append(PriorArt(label=label, docno=docno, date=date))

        i = j

    dedup: Dict[Tuple[str, str], PriorArt] = {}
    for pa in arts:
        key = (pa.label.upper(), _canonical_docno(pa.docno))
        prev = dedup.get(key)
        if prev is None:
            dedup[key] = pa
        else:
            if (not prev.date) and pa.date:
                dedup[key] = pa
            elif prev.date == pa.date and len(pa.docno) > len(prev.docno):
                dedup[key] = pa

    vals = list(dedup.values())
    vals.sort(key=lambda p: int(re.sub(r"\D", "", p.label) or "9999"))
    return vals


def _extract_disclosures(text: str, prior_arts: List[PriorArt]) -> Dict[str, str]:
    """Extract short disclosure strings for each Dx, robust to IPO phrasing."""
    t = text or ""
    out: Dict[str, str] = {}

    def clip(s: str) -> str:
        s = _clean(s)
        return s[:900]

    for pa in prior_arts or []:
        lab = pa.label.upper()

        m = re.search(
            rf"(?:Document\s+)?{re.escape(lab)}\s*[:\-]?\s*(?:{re.escape(pa.docno)}\s*)?(?:\([^)]+\)\s*)?.*?\b(discloses|describes|teaches|is\s+related\s+to)\b\s*(.*?)(?=(?:Document\s+)?D\d+\b|Therefore\b|Thus\b|Hence\b|In\s+view\b|\Z)",
            t,
            re.I | re.S,
        )
        if m:
            out[lab] = clip(m.group(2))
            continue

        m = re.search(
            rf"\b{re.escape(lab)}\b.*?\b(discloses|describes|teaches)\b\s*(.*?)(?=(?:Document\s+)?D\d+\b|Therefore\b|Thus\b|Hence\b|\Z)",
            t,
            re.I | re.S,
        )
        if m:
            out[lab] = clip(m.group(2))
            continue

        if pa.docno:
            doc_pat = re.escape(pa.docno)
            m = re.search(
                rf"{doc_pat}.*?\b(discloses|describes|teaches|is\s+related\s+to)\b\s*(.*?)(?=(?:Document\s+)?D\d+\b|{doc_pat}\b|Therefore\b|Thus\b|Hence\b|\Z)",
                t,
                re.I | re.S,
            )
            if m:
                out[lab] = clip(m.group(2))
                continue

    return out


def parse_case_meta_from_fer_or_hn(pdf_path: str) -> CaseMeta:
    text = read_pdf_text(pdf_path)
    meta = CaseMeta(prior_arts=[])

    # Application number
    for pat in [
        r"Indian\s+Patent\s+Application\s+No\s*[:\-]?\s*([0-9][0-9A-Z/\-]*)",
        r"Indian\s+Patent\s+Application\s+No\.?\s*[:\-]?\s*([0-9][0-9A-Z/\-]*)",
        r"Application\s+Number\s*[:\-]?\s*([0-9][0-9A-Z/\-]*)",
        r"Application\s*No\.?\s*[/:-]?\s*([0-9][0-9A-Z/\-]*)",
        r"POD/Application\s*No\s*/\s*([0-9][0-9A-Z/\-]*)",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            meta.app_no = _clean(m.group(1))
            break

    # Filed on / Date of Filing
    meta.filed_on = _find_date(
        [
            r"Date\s+of\s+Filing\s*[:\-]?\s*([0-9]{1,2}[\-/][0-9]{1,2}[\-/][0-9]{2,4})",
            r"Filed\s*on\s*[:\-]?\s*([0-9]{1,2}[\-/][0-9]{1,2}[\-/][0-9]{2,4})",
        ],
        text,
    )

    # Applicant
    m = re.search(r"Name\s+of\s+the\s+Applicant\s*[:\-]?\s*(.+)", text, re.I)
    if not m:
        m = re.search(r"\bApplicant\s*[:\-]?\s*(.+)", text, re.I)
    if m:
        first = _clean(m.group(1))
        cont = ""
        after = text[m.end():]
        m2 = re.match(r"\s*\n\s*([^\n:]{4,})\n", after)
        if m2:
            cand = _clean(m2.group(1))
            if cand and not re.search(r"\b(controller|address|date|application|hearing|ref)\b", cand, re.I):
                cont = cand
        meta.applicant = _clean(" ".join([x for x in [first, cont] if x]))

    # Controller (expanded to cover IPO variants like:
    # "Saroj Kumar\nDeputy Controller of Patents & Designs"
    m = re.search(
        r"\n\s*([A-Za-z][A-Za-z .]{2,})\s*\n\s*"
        r"(?:Assistant|Deputy|Joint|Senior\s+Joint|Controller)\s+Controller\s+of\s+"
        r"(?:Patents?(?:\s*&\s*Designs)?|Patents?\s+and\s+Designs)\b",
        text,
        re.I,
    )
    if m:
        meta.controller = _clean(m.group(1)).title()
    else:
        m = re.search(r"Controller\s+Name\s*[:\-]?\s*(.+)", text, re.I)
        if m:
            meta.controller = _clean(m.group(1))
        else:
            meta.controller = ""

    # Agent
    m = re.search(r"To\s*\n\s*([A-Z][A-Z ]+NARASANI)", text, re.I)
    if m:
        meta.agents = _clean(m.group(1).title())
    m = re.search(r"Registerd\s+Address\s+For\s+Service\s*:?\s*([A-Z][^,\n]+NARASANI)", text, re.I)
    if m:
        meta.agents = _clean(m.group(1))

    # Dispatch dates
    meta.fer_dispatch_date = _find_date(
        [
            r"Date\s+of\s+Dispatch.*?:\s*([0-9]{1,2}[\-/][0-9]{1,2}[\-/][0-9]{2,4})",
            r"Date\s+of\s+Dispatch/Email.*?:\s*([0-9]{1,2}[\-/][0-9]{1,2}[\-/][0-9]{2,4})",
        ],
        text,
    )

    meta.hn_dispatch_date = _find_date(
        [
            r"hearing\s+notice\s+dated\s*([0-9]{1,2}[\-/][0-9]{1,2}[\-/][0-9]{2,4})",
            r"Date\s+of\s+Dispatch\s*:?\s*([0-9]{1,2}[\-/][0-9]{1,2}[\-/][0-9]{2,4})",
            r"\bDate\s*[-:]\s*([0-9]{1,2}[\-/][0-9]{1,2}[\-/][0-9]{2,4})\b",
        ],
        text,
    )

    # Hearing date/time/mode
    m = re.search(r"Hearing\s+Location\s*:\s*(.+)", text, re.I)
    if m:
        meta.hearing_mode = _clean(m.group(1))
    m = re.search(
        r"Hearing\s+Date\s*&\s*Time\s*:\s*([0-9]{1,2}[\-/][0-9]{1,2}[\-/][0-9]{2,4})\s*/\s*([^\n]+)",
        text,
        re.I,
    )
    if m:
        meta.hearing_date = _clean(m.group(1))
        tm = re.search(r"(\d{1,2}:\d{2})", m.group(2))
        meta.hearing_time = tm.group(1) if tm else _clean(m.group(2))

    # FER date + reply date
    meta.fer_date = _find_date(
        [r"FER\s+dated\s*([0-9]{1,2}[\-/][0-9]{1,2}[\-/][0-9]{2,4})"],
        text,
    ) or meta.fer_dispatch_date

    meta.fer_reply_date = _find_date(
        [r"reply\s+of\s+the\s+applicant\s+dated\s*([0-9]{1,2}[\-/][0-9]{1,2}[\-/][0-9]{2,4})"],
        text,
    )

    # Prior arts + disclosures
    meta.prior_arts = _parse_prior_arts_from_text(text)

    disclosures = _extract_disclosures(text, meta.prior_arts or [])
    for lab, disc in disclosures.items():
        try:
            num = int(re.sub(r"\D", "", lab))
            setattr(meta, f"d{num}_disclosure", disc)
        except Exception:
            pass

    meta.d1_disclosure = getattr(meta, "d1_disclosure", "") or disclosures.get("D1", "")
    meta.d2_disclosure = getattr(meta, "d2_disclosure", "") or disclosures.get("D2", "")

    return meta

def build_prior_arts_list(meta: CaseMeta) -> str:
    lines: List[str] = []
    for pa in (meta.prior_arts or []):
        if pa.date:
            lines.append(f"{pa.label}: {pa.docno} ({pa.date})")
        else:
            lines.append(f"{pa.label}: {pa.docno}")
    return "\n".join(lines) if lines else ""


def parse_claims_from_specification(spec_text: str) -> Dict[int, str]:
    idx = spec_text.lower().find("claims")
    tail = spec_text[idx:] if idx != -1 else spec_text
    claims: Dict[int, str] = {}
    for m in re.finditer(r"(?ms)^\s*(\d{1,2})\.\s+(.*?)(?=^\s*\d{1,2}\.\s+|\Z)", tail):
        no = int(m.group(1))
        if 1 <= no <= 50:
            claims[no] = _clean(m.group(2))
    return claims


def parse_amended_claims(path: str) -> Dict[int, str]:
    text = read_text_any(path)
    if not text:
        return {}

    claims: Dict[int, str] = {}
    numbered = list(re.finditer(r"(?ms)^\s*(\d{1,2})\.\s+(.*?)(?=^\s*\d{1,2}\.\s+|\Z)", text))
    for m in numbered:
        no = int(m.group(1))
        if 1 <= no <= 200:
            claims[no] = _clean(m.group(2))

    if 1 not in claims:
        m = re.search(
            r"Claim\s*1\s+has\s+been\s+amended\s+to\s+recite\s*:\s*(.*?)(?=\n\s*TECHNICAL\s+ADVANCEMENT\s*:|\Z)",
            text,
            re.I | re.S,
        )
        if m:
            claims[1] = _clean(m.group(1))

    return claims


def extract_technical_advancement_from_spec(spec_text: str) -> str:
    m = re.search(r"\[0034\](.*?)(?=\[0039\]|\[0040\]|Referring\s+now\s+to\s+the\s+drawings|\Z)", spec_text, re.S)
    if m:
        return _clean(m.group(1))
    m = re.search(r"OBJECT\s+OF\s+INVENTION(.*?)(?=SUMMARY|\[0009\])", spec_text, re.I | re.S)
    if m:
        return _clean(m.group(1))
    return _clean(spec_text[:800])


def extract_figure_descriptions_from_spec(spec_text: str) -> Dict[int, str]:
    txt = spec_text or ""
    out: Dict[int, str] = {}
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in txt.splitlines()]
    fig_re = re.compile(r"\bFIG\.?\s*(\d+)(?:[A-Z])?\b\s*(?:is|illustrates|shows|depicts|represents)?\s*[:\-]?\s*(.*)$", re.I)
    for ln in lines:
        m = fig_re.search(ln)
        if not m:
            continue
        num = int(m.group(1))
        desc = (m.group(2) or "").strip()
        if not desc:
            continue
        desc = desc.strip().strip(";").strip()
        if num not in out or (len(desc) > len(out[num])):
            out[num] = desc[:300]
    return out
