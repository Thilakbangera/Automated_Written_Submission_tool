from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from typing import Optional, Dict

from .extract import (
    parse_amended_claims,
    parse_case_meta_from_fer_or_hn,
    build_prior_arts_list,
    read_pdf_text,
    parse_claims_from_specification,
    extract_technical_advancement_from_spec,
    extract_figure_descriptions_from_spec,
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


def _extract_tech_problem(spec_text: str) -> str:
    """Extract a short 'technical problem' block from specification.

    Must not be empty across cases. Keep concise (no overfilling).
    """
    txt = spec_text or ""
    # Prefer explicit headings
    for pat in [
        r"TECHNICAL\s+PROBLEM(.*?)(?=TECHNICAL\s+SOLUTION|SUMMARY|OBJECT|\[\d{4}\])",
        r"BACKGROUND\s+OF\s+THE\s+INVENTION(.*?)(?=SUMMARY|OBJECT|BRIEF\s+DESCRIPTION|\[\d{4}\])",
    ]:
        mm = re.search(pat, txt, re.I | re.S)
        if mm:
            block = re.sub(r"\s+", " ", mm.group(1)).strip()
            return block[:1200]

    # Otherwise extract a few sentences containing problem/need/drawback/limitation.
    flat = re.sub(r"\s+", " ", txt)
    sents = re.split(r"(?<=[.!?])\s+", flat)
    picks = []
    for s in sents:
        if re.search(r"\b(problem|drawback|need|limitation|challenge|deficien)\w*\b", s, re.I):
            picks.append(s.strip())
        if len(picks) >= 3:
            break
    return " ".join(picks)[:800]

def _extract_tech_solution(spec_text: str) -> str:
    m = re.search(r"SUMMARY(.*?)(?=BRIEF DESCRIPTION|\[0012\])", spec_text, re.I | re.S)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""

def _extract_tech_effect(spec_text: str) -> str:
    """
    Extract TECHNICAL EFFECT from Complete Specification (CS) robustly.

    Strategy (in order):
    1) If CS explicitly has a section like TECHNICAL EFFECT / ADVANTAGES / TECHNICAL ADVANTAGE / CONTRIBUTION,
       extract that section.
    2) Else, find high-signal paragraphs (often in Detailed Description) containing benefit language
       (reduces/improves/enhances/thereby/results in/enables/etc.).
    3) Else, pick only effect-like sentences from SUMMARY (NOT the whole SUMMARY), so it won't match TECH_SOLUTION.
    """
    txt = (spec_text or "").strip()
    if not txt:
        return ""

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip()

    # Keep tech_solution normalization to avoid accidental identical content
    tech_solution = ""
    m_sol = re.search(r"SUMMARY(.*?)(?=BRIEF DESCRIPTION|\[0012\])", txt, re.I | re.S)
    if m_sol:
        tech_solution = norm(m_sol.group(1))

    # 1) Explicit section headings (best when available)
    explicit_heads = [
        r"TECHNICAL\s+EFFECT",
        r"TECHNICAL\s+ADVANTAGES?",
        r"ADVANTAGES?\s+OF\s+THE\s+INVENTION",
        r"TECHNICAL\s+CONTRIBUTION",
        r"EFFECTS?\s+OF\s+THE\s+INVENTION",
    ]
    for h in explicit_heads:
        m = re.search(
            rf"{h}\s*[:\-]?\s*(.*?)(?=\n\s*[A-Z][A-Z \t/&\-]{{6,}}\s*[:\-]?\s*\n|\Z)",
            txt,
            re.I | re.S,
        )
        if m:
            out = norm(m.group(1))[:1200]
            if out and norm(out) != norm(tech_solution):
                return out

    # Helper: paragraph candidates
    paras = re.findall(r"\[\d{4}\].*?(?=\[\d{4}\]|\Z)", txt, re.S)
    if not paras:
        # fallback: rough paragraph split
        paras = [p for p in re.split(r"\n\s*\n+", txt) if p.strip()]

    # 2) Rank paragraphs by "effect" signals (works even if no explicit headings)
    # Include both metric-like words and causal words (thereby/thus/results in/enables)
    effect_kw = re.compile(
        r"\b("
        r"reduce|reduces|reduced|decrease|lower|minimi|save|faster|speed|latency|delay|time|cost|power|memory|bandwidth|overhead|complexity|errors?|noise|loss|"
        r"improv|improves|improved|enhanc|enhances|enhanced|efficient|efficiency|accurac|reliab|robust|secure|security|stability|throughput|performance|"
        r"thereby|thus|hence|as\s+a\s+result|results?\s+in|leads?\s+to|enables?|facilitates?|achieves?"
        r")\w*\b",
        re.I,
    )

    scored = []
    for p in paras:
        pp = norm(p)
        if not pp:
            continue
        # Avoid very figure-heavy paras
        if re.search(r"\bFIG\.|\bFIGS\.|\bReferring\b", pp, re.I) and not effect_kw.search(pp):
            continue
        hits = len(effect_kw.findall(pp))
        if hits:
            scored.append((hits, pp))

    scored.sort(key=lambda x: x[0], reverse=True)

    if scored:
        # Join top 1–2 strong paras (keeps it “effect-y” and not huge)
        top = " ".join([scored[0][1]] + ([scored[1][1]] if len(scored) > 1 and scored[1][0] >= 2 else []))
        top = top[:1200]
        if norm(top) != norm(tech_solution):
            return top

    # 3) FINAL fallback: effect-like sentences from SUMMARY only (avoid being identical to tech_solution)
    m_sum = re.search(r"SUMMARY(.*?)(?=BRIEF DESCRIPTION|\[\d{4}\]|\Z)", txt, re.I | re.S)
    summary = norm(m_sum.group(1)) if m_sum else ""
    if not summary:
        return ""

    sents = re.split(r"(?<=[.!?])\s+", summary)
    picks = [s.strip() for s in sents if s.strip() and effect_kw.search(s)]

    # Take up to 3 effect-like sentences
    out = " ".join(picks[:3]).strip()[:900]

    # If still empty, take last 1–2 sentences (often contain "thereby/thus")
    if not out:
        tail = [s.strip() for s in sents[-2:] if s.strip()]
        out = " ".join(tail).strip()[:900]

    # Last check to avoid identical with tech_solution
    if norm(out) == norm(tech_solution):
        # Take only last sentence as a “difference maker”
        last = sents[-1].strip() if sents else ""
        out = last[:600].strip()

    return out





def _extract_reply_3k(fer_or_hn_text: str) -> str:
    # Capture the examiner's 3(k) reasoning (copy from notice) so WS has no blanks.
    m = re.search(r"Claims\s+1-10\s+are\s+method\s+claims.*?(?=Therefore,\s*the\s*claims\s*1-\s*10|Therefore,\s*the\s*claims\s*1-11|\Z)", fer_or_hn_text, re.I | re.S)
    if m:
        return re.sub(r"\s+", " ", m.group(0)).strip()[:1800]
    # FER variant
    m = re.search(r"prima\s+facie\s+falls\s+within\s+scope\s+of\s+clause\s*\(k\).*?(?=Therefore,|\Z)", fer_or_hn_text, re.I | re.S)
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
    # Typical: "Arun Kishore Narasani" then "Patent Agent"
    m = re.search(r"\n\s*([A-Z][A-Za-z ]+?)\s*\n\s*Patent\s+Agent", txt)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _format_ws_date_today() -> str:
    # WS top date must be "present day of generating WS" (V3.1 requirement)
    return datetime.now().strftime("%d-%m-%Y")


def _build_claim1_features(claim1_text: str) -> str:
    """Build 'preamble + features' text for the Applicant claimed feature table.

    Works for both software/method and hardware/apparatus style claims.
    Goal (V4): preamble line + a few key feature lines, without overfilling.
    """
    if not claim1_text:
        return ""
    txt = re.sub(r"\s+", " ", claim1_text).strip().rstrip(".")

    # Normalize preamble and find 'comprising'
    m = re.search(r"^(.*?\bcomprising\b\s*:?)\s*(.*)$", txt, re.I)
    if not m:
        return txt + "."

    preamble = m.group(1).strip()
    rest = m.group(2).strip()

    if len(rest) < 120:
        return f"{preamble}\n{rest}."

    # Split into clauses using common legal separators; prefer 'wherein' clauses
    wherein_parts = re.split(r"\s*(?=\bwherein\b)", rest, flags=re.I)
    head = wherein_parts[0].strip(" ;,")
    wherein_clauses = [p.strip(" ;,") for p in wherein_parts[1:] if p.strip()]

    features = []

    # Break the head into key components (apparatus-friendly)
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



def _dx_labels_from_prior_arts_text(prior_arts_text: str) -> list[str]:
    """Extract D# labels present in a prior-arts list text."""
    import re
    labs = []
    for ln in (prior_arts_text or "").splitlines():
        m = re.match(r"\s*(D\d+)\s*:", ln.strip(), flags=re.I)
        if m:
            d = m.group(1).upper()
            if d not in labs:
                labs.append(d)
    return labs

def _dx_range_string(labels: list[str]) -> str:
    """Return 'D1-D2' style range if contiguous, else 'D1, D2, D4' style."""
    import re
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
    # contiguous range?
    if nums == list(range(nums[0], nums[-1] + 1)) and len(nums) >= 2:
        return f"D{nums[0]}-D{nums[-1]}"
    if len(nums) == 1:
        return f"D{nums[0]}"
    return ", ".join([f"D{n}" for n in nums])

def _dx_and_string(labels: list[str]) -> str:
    """Return 'D1 and D2' / 'D1, D2 and D3' style."""
    import re
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


def _clean_prior_arts_list_text(prior_arts_text: str) -> str:
    """Keep only clean D# prior-art lines for WS (drops Fig./noise/duplicates).

    This is intentionally conservative: it filters the prior-art list shown under 'REPLY TO OBJECTION'
    and does not affect other sections.
    """
    import re
    dx_re = re.compile(r"^\s*(D\d+)\s*:\s*(.+?)\s*$", re.I)
    date_re = re.compile(r"(\d{2}/\d{2}/\d{4})")
    def norm(s: str) -> str:
        s = re.sub(r"[\x00-\x1F\x7F]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    store = {}
    for raw in (prior_arts_text or "").splitlines():
        ln = norm(raw)
        if not ln:
            continue
        m = dx_re.match(ln)
        if not m:
            continue
        dx = m.group(1).upper()
        rest = m.group(2).strip()

        # Drop obvious noise lines (fig refs, non-English garbage, etc.)
        if re.search(r"\bfig\.?\b", rest, re.I):
            continue
        if sum(1 for ch in rest if ord(ch) > 127) > 5:
            continue

        md = date_re.search(rest)
        date = md.group(1) if md else ""

        # Remove 'whole document...' tails if present
        rest2 = re.sub(r"\bwhole\s+doc(?:ument)?\b.*$", "", rest, flags=re.I).strip().rstrip(",;")

        # If rest contains date without parentheses, split doc/title from date
        doc_part = rest2
        if date and "(" not in rest2:
            doc_part = rest2.split(date)[0].strip().rstrip(",;")

        # Choose best line per Dx: prefer with date; else longer doc_part
        prev = store.get(dx)
        if prev is None:
            store[dx] = (doc_part, date)
        else:
            prev_doc, prev_date = prev
            if not prev_date and date:
                store[dx] = (doc_part, date)
            elif prev_date == date and len(doc_part) > len(prev_doc):
                store[dx] = (doc_part, date)

    def dx_key(k: str) -> int:
        return int(re.sub(r"\D", "", k) or "0")

    out = []
    for dx in sorted(store.keys(), key=dx_key):
        doc_part, date = store[dx]
        if not doc_part:
            continue
        if date:
            out.append(f"{dx}: {doc_part} ({date})")
        else:
            out.append(f"{dx}: {doc_part}")
    return "\n".join(out) if out else (prior_arts_text or "")


def generate_written_submission(
    fer_path: str,
    hn_path: str,
    specification_path: str,
    drawings_path: Optional[str] = None,
    amended_claims_path: Optional[str] = None,
    tech_solution_images_paths: Optional[list] = None,
    city: str = "Chennai",
):
    """Generate WS.

    FER and HN are treated separately (V3.1 requirement).
    - Applicant name must come from HN.
    - Hearing dispatch/date/time must come from HN.
    - FER date/reply date must come from FER (if available), else fallback to HN text.
    - Patent office city can be provided as a parameter, defaults to "Chennai".
    """
    fer_meta = parse_case_meta_from_fer_or_hn(fer_path)
    hn_meta = parse_case_meta_from_fer_or_hn(hn_path)

    spec_text = read_pdf_text(specification_path)

    fig_desc_map = extract_figure_descriptions_from_spec(spec_text)

    # Claims: prefer amended_claims upload, else use claims from specification.
    claims: Dict[int, str] = {}
    if amended_claims_path:
        claims = parse_amended_claims(amended_claims_path)
    if not claims:
        claims = parse_claims_from_specification(spec_text)

    # 1) WS Date = today (not from docs)
    ws_date = _format_ws_date_today()

    # Mandatory metadata: do not silently leave blanks.
    app_no = _first_nonempty(hn_meta.app_no, fer_meta.app_no)
    if not app_no:
        raise ValueError("Application number not found in HN or FER")

    # 2) Controller name: MUST come from HN (Assistant Controller)
    controller = _first_nonempty(hn_meta.controller)
    if not controller:
        raise ValueError("Controller name not found in Hearing Notice")

    # 3) Applicant patent agent name is constant for all
    agents = "Pranav Bhat"

    # 4) Applicant must come from HN
    applicant = _first_nonempty(hn_meta.applicant)
    if not applicant:
        raise ValueError("Applicant name not found in Hearing Notice")

    # Salutation: in your gold WS it's "Dear Sir,". Keep deterministic.
    dear_salutation = "Sir"

    # 5) Hearing notice date must be dispatch date from HN (never from FER date)
    hn_dispatch = _first_nonempty(hn_meta.hn_dispatch_date)
    if not hn_dispatch:
        raise ValueError("Hearing Notice dispatch date not found in Hearing Notice")

    # 6) Hearing date and time from HN
    hearing_date = _first_nonempty(hn_meta.hearing_date)
    hearing_time = _first_nonempty(hn_meta.hearing_time)
    # If HN doesn't have time token, keep '11:30 HRS (IST)' style when possible
    if hearing_time and re.fullmatch(r"\d{1,2}:\d{2}", hearing_time.strip()):
        hearing_time_fmt = f"{hearing_time} HRS (IST)"
    else:
        hearing_time_fmt = hearing_time or ""

    hearing_mode = _first_nonempty(hn_meta.hearing_mode, "Video Conferencing")
    hearing_dt = " / ".join([p for p in [hearing_date, hearing_time_fmt] if p])

    # Participants: Controller name must be the HN Assistant Controller name; agent is constant.
    participants = (
        f"1. {controller}\n"
        f"2. {applicant}\n"
        f"3. {agents}"
    )

    # FER dates
    fer_reply_date = _first_nonempty(fer_meta.fer_reply_date, hn_meta.fer_reply_date)
    fer_date = _first_nonempty(fer_meta.fer_date, fer_meta.fer_dispatch_date, hn_meta.fer_date)

    # Prior arts must be taken from FER first; fallback to HN if FER has none.
    prior_arts_list = build_prior_arts_list(fer_meta) or build_prior_arts_list(hn_meta)
    prior_arts_list_clean = _clean_prior_arts_list_text(prior_arts_list)
    dx_labels = _dx_labels_from_prior_arts_text(prior_arts_list)
    dx_range = _dx_range_string(dx_labels)
    dx_and = _dx_and_string(dx_labels)

    
    # Disclosures for right table column: take from FER first (as required), else HN.
    # Supports D1..Dn if your meta exposes attributes like d1_disclosure, d2_disclosure, ...
    parts = []

    # 1) Prefer FER disclosures
    for i in range(1, 50):  # bump if you can have more than D49
        v = getattr(fer_meta, f"d{i}_disclosure", None)
        if v and str(v).strip():
            parts.append(f"D{i}: {str(v).strip()}")

    # 2) Fallback to HN disclosures only if FER had none
    if not parts:
        for i in range(1, 50):
            v = getattr(hn_meta, f"d{i}_disclosure", None)
            if v and str(v).strip():
                parts.append(f"D{i}: {str(v).strip()}")

    # 3) LAST fallback: if disclosures are missing, derive from the cleaned prior-art list so the table isn't blank
    if not parts:
        for ln in (prior_arts_list_clean or prior_arts_list or "").splitlines():
            ln = ln.strip()
            if re.match(r"^D\d+\s*:", ln, flags=re.I):
                parts.append(ln)

    d1d2_disclosure = "\n".join(parts) if parts else ""

    claim1_text = claims.get(1, "")
    claim1_features = _build_claim1_features(claim1_text)

    # Minimal, file-derived supporting blocks (no overfill)
    technical_adv = extract_technical_advancement_from_spec(spec_text) or ""
    tech_problem = _extract_tech_problem(spec_text) or ""
    tech_solution = _extract_tech_solution(spec_text) or ""
    tech_effect = _extract_tech_effect(spec_text) or ""
    reply_3k = _extract_reply_3k(read_pdf_text(hn_path) + "\n" + read_pdf_text(fer_path)) or ""

    # Claims range and extra claims block
    claim_nos = sorted([n for n in claims.keys() if n >= 1])
    claim_max = claim_nos[-1] if claim_nos else 0
    if not claim_nos:
        raise ValueError("No claims could be parsed from amended claims or specification")
    claims_range = f"1-{claim_nos[-1]}" if claim_nos[-1] != 1 else "1"

    extra_claims_block = ""
    dx_labels = _dx_labels_from_prior_arts_text(prior_arts_list) if 'prior_arts_list' in locals() else []
    dx_range = _dx_range_string(dx_labels) or "D1-D2"
    for n in claim_nos:
        if n <= 10:
            continue
        ctext = (claims.get(n, "") or "").strip()
        if not ctext:
            continue
        extra_claims_block += (
            f"\n\nRegarding Claim {n}:\n"
            f"Applicant has reviewed the entire application of {dx_range} and found that nowhere in the entire applications does {dx_range} describe or reasonably suggest the following features \"{ctext}\". "
            f"Apart from the above, Applicant believes that dependent claim {n} is allowable not only by virtue of their dependency from patentable independent claim 1, respectively, but also by virtue of the additional features of the invention they define. "
            f"The dependent claims describe various embodiments of the invention that can be combined to form the invention. The subject matter described in the instant application are different from {dx_range} so as the features described in dependent claim {n}."
        )

    mapping = {
        "{{WS_DATE}}": ws_date,
        "{{APP_NO}}": app_no,
        "{{FILED_ON}}": _first_nonempty(hn_meta.filed_on, fer_meta.filed_on),
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
        "{{PRIOR_ARTS_LIST}}": prior_arts_list_clean,
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
    }

    # Helper metadata for template post-processing
    mapping["__CLAIM_MAX__"] = claim_max
    mapping["__FIG_DESC_MAP__"] = fig_desc_map

    out_dir = os.path.join(tempfile.gettempdir(), "ws_tool_outputs")
    os.makedirs(out_dir, exist_ok=True)

    safe_app = _sanitize_filename((hn_meta.app_no or fer_meta.app_no) or "APP")
    out_name = f"Written_Submission_{safe_app}.docx"
    out_path = os.path.join(out_dir, out_name)

    # Pass optional Technical Solution diagram screenshots (do not affect anything else)
    if tech_solution_images_paths:
        mapping["__TECH_SOLUTION_IMAGES__"] = tech_solution_images_paths
    replace_placeholders(TEMPLATE_PATH, out_path, mapping)
    return out_path, out_name
