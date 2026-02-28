import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..services.pipeline import generate_written_submission

router = APIRouter()

@router.post(
    "/api/generate",
    summary="Generate",
    description=(
        "Generates a Written Submission DOCX from HN + specification + prior-art inputs. "
        "Prior arts can be provided either as text abstracts or as D1-Dn documents (PDF/DOCX, abstract auto-extracted)."
    ),
)
async def generate(
    hn: UploadFile = File(..., description="Hearing Notice PDF"),
    specification: UploadFile = File(..., description="Complete specification (PDF/DOCX)"),
    city: str = Form("Chennai", description="Patent Office City (e.g., Chennai, Mumbai, Delhi)"),
    filed_on: str = Form("", description="Filed On date (manual input from UI)"),
    prior_art_input_mode: str = Form("text", description="Prior-art mode: 'text' or 'pdf'"),
    prior_arts_json: Optional[str] = Form(
        None,
        description="For text mode: JSON array with fields label, abstract, has_diagram(optional)",
    ),
    prior_arts_meta_json: Optional[str] = Form(
        None,
        description="For pdf mode: JSON array with fields label, has_diagram(optional)",
    ),
    prior_art_pdfs: Optional[List[UploadFile]] = File(None, description="For pdf mode: prior-art files (PDF/DOCX) in D1..Dn order"),
    prior_art_diagrams: Optional[List[UploadFile]] = File(None, description="Optional prior-art diagram images"),
    drawings: Optional[UploadFile] = File(None, description="Drawings PDF (optional)"),
    amended_claims: Optional[UploadFile] = File(None, description="Amended claims (PDF/DOCX/TXT) (optional)"),
    tech_solution_images: Optional[List[UploadFile]] = File(None, description="Technical solution diagram screenshots (PNG/JPG)"),
):
    filed_on_value = (filed_on or "").strip()

    mode_raw = (prior_art_input_mode or "").strip().lower()
    if mode_raw in {"text", "pdf"}:
        mode = mode_raw
    else:
        # Backward compatibility: infer mode when client omits/uses stale values.
        mode = "pdf" if prior_art_pdfs else "text"

    raw_prior_arts: List[Dict[str, Any]] = []
    raw_prior_arts_meta: List[Dict[str, Any]] = []

    if mode == "text" and not prior_arts_json and prior_art_pdfs:
        # Backward compatibility: some clients forget prior_art_input_mode but send files.
        mode = "pdf"

    if mode == "text":
        if not prior_arts_json:
            raise HTTPException(status_code=422, detail="prior_arts_json is required for text mode.")
        try:
            parsed = json.loads(prior_arts_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid prior_arts_json: {exc.msg}") from exc
        if not isinstance(parsed, list) or not parsed:
            raise HTTPException(status_code=422, detail="prior_arts_json must be a non-empty JSON array.")
        raw_prior_arts = [x for x in parsed if isinstance(x, dict)]
        if len(raw_prior_arts) != len(parsed):
            raise HTTPException(status_code=422, detail="Each prior-art entry in prior_arts_json must be an object.")
    else:
        if not prior_art_pdfs:
            raise HTTPException(status_code=422, detail="At least one prior-art document (D1-Dn) is required in pdf mode.")
        if prior_arts_meta_json:
            try:
                parsed_meta = json.loads(prior_arts_meta_json)
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=422, detail=f"Invalid prior_arts_meta_json: {exc.msg}") from exc
            if not isinstance(parsed_meta, list):
                raise HTTPException(status_code=422, detail="prior_arts_meta_json must be a JSON array.")
            raw_prior_arts_meta = [x for x in parsed_meta if isinstance(x, dict)]
            if len(raw_prior_arts_meta) != len(parsed_meta):
                raise HTTPException(status_code=422, detail="Each prior-art meta entry must be an object.")
            if raw_prior_arts_meta and len(raw_prior_arts_meta) != len(prior_art_pdfs):
                # Backward compatibility: normalize meta length to number of uploaded files.
                if len(raw_prior_arts_meta) < len(prior_art_pdfs):
                    raw_prior_arts_meta.extend({} for _ in range(len(prior_art_pdfs) - len(raw_prior_arts_meta)))
                else:
                    raw_prior_arts_meta = raw_prior_arts_meta[: len(prior_art_pdfs)]

    prior_arts_entries: List[Dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as td:
        hn_path = os.path.join(td, "hn.pdf")
        spec_filename = specification.filename or "spec.pdf"
        spec_ext = Path(spec_filename).suffix.lower()
        if spec_ext not in {".pdf", ".docx"}:
            raise HTTPException(status_code=422, detail="Specification must be a PDF or DOCX file.")
        spec_path = os.path.join(td, f"spec{spec_ext}")

        with open(hn_path, "wb") as f:
            f.write(await hn.read())
        with open(spec_path, "wb") as f:
            f.write(await specification.read())

        if mode == "text":
            for idx, item in enumerate(raw_prior_arts, start=1):
                abstract = str(item.get("abstract", "")).strip()
                if not abstract:
                    # Backward compatibility: older payloads used summary field.
                    abstract = str(item.get("summary", "")).strip()
                if not abstract:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Prior-art entry #{idx} must include non-empty abstract in text mode.",
                    )
                label_raw = str(item.get("label", "")).strip().upper()
                label = label_raw if re.fullmatch(r"D\d+", label_raw or "") else f"D{idx}"
                prior_arts_entries.append(
                    {
                        "label": label,
                        "abstract": abstract,
                        "has_diagram": bool(item.get("has_diagram", False)),
                        "diagram_image_path": "",
                    }
                )
        else:
            for idx, pa_pdf in enumerate(prior_art_pdfs or [], start=1):
                filename = pa_pdf.filename or f"d{idx}.pdf"
                ext = Path(filename).suffix.lower()
                if ext not in {".pdf", ".docx"}:
                    raise HTTPException(status_code=422, detail=f"Prior-art file #{idx} must be a PDF or DOCX.")
                out_pdf = os.path.join(td, f"prior_art_{idx}{ext}")
                with open(out_pdf, "wb") as f:
                    f.write(await pa_pdf.read())

                label = f"D{idx}"
                has_diagram = False
                if raw_prior_arts_meta:
                    label_raw = str(raw_prior_arts_meta[idx - 1].get("label", "")).strip().upper()
                    if re.fullmatch(r"D\d+", label_raw or ""):
                        label = label_raw
                    has_diagram = bool(raw_prior_arts_meta[idx - 1].get("has_diagram", False))

                prior_arts_entries.append(
                    {
                        "label": label,
                        "prior_art_pdf_path": out_pdf,
                        "has_diagram": has_diagram,
                        "diagram_image_path": "",
                    }
                )

        drawings_path = None
        if drawings is not None:
            drawings_path = os.path.join(td, "drawings.pdf")
            with open(drawings_path, "wb") as f:
                f.write(await drawings.read())

        amended_path = None

        tech_solution_image_paths = []
        if tech_solution_images:
            for img in tech_solution_images:
                filename = img.filename or ""
                ext = os.path.splitext(filename)[1].lower()
                if ext not in [".png", ".jpg", ".jpeg"]:
                    continue
                out_img = os.path.join(td, f"tech_solution_{len(tech_solution_image_paths)+1}{ext}")
                with open(out_img, "wb") as f:
                    f.write(await img.read())
                tech_solution_image_paths.append(out_img)

        prior_art_diagram_paths: List[str] = []
        if prior_art_diagrams:
            for img in prior_art_diagrams:
                filename = img.filename or ""
                ext = os.path.splitext(filename)[1].lower()
                if ext not in [".png", ".jpg", ".jpeg"]:
                    continue
                out_img = os.path.join(td, f"prior_art_diagram_{len(prior_art_diagram_paths)+1}{ext}")
                with open(out_img, "wb") as f:
                    f.write(await img.read())
                prior_art_diagram_paths.append(out_img)

        required_flags = [bool(e.get("has_diagram", False)) for e in prior_arts_entries]
        required_count = sum(1 for x in required_flags if x)

        if required_count > 0:
            if len(prior_art_diagram_paths) < required_count:
                raise HTTPException(
                    status_code=422,
                    detail="Some prior arts were marked with diagram required, but fewer diagram images were uploaded.",
                )
            cursor = 0
            for entry in prior_arts_entries:
                if bool(entry.get("has_diagram", False)):
                    entry["diagram_image_path"] = prior_art_diagram_paths[cursor]
                    cursor += 1
        else:
            # If none explicitly marked, map diagrams in D-order up to available count.
            if len(prior_art_diagram_paths) > len(prior_arts_entries):
                raise HTTPException(
                    status_code=422,
                    detail="More prior-art diagram images were uploaded than prior-art entries.",
                )
            for idx, entry in enumerate(prior_arts_entries):
                if idx < len(prior_art_diagram_paths):
                    entry["diagram_image_path"] = prior_art_diagram_paths[idx]

        if amended_claims is not None:
            # Keep the original extension if possible to help parsers
            filename = amended_claims.filename or "amended.bin"
            ext = os.path.splitext(filename)[1].lower() or ".bin"
            amended_path = os.path.join(td, f"amended{ext}")
            with open(amended_path, "wb") as f:
                f.write(await amended_claims.read())

        try:
            out_path, out_name = generate_written_submission(
                hn_path=hn_path,
                specification_path=spec_path,
                prior_arts_entries=prior_arts_entries,
                drawings_path=drawings_path,
                amended_claims_path=amended_path,
                tech_solution_images_paths=tech_solution_image_paths,
                city=city,
                filed_on_input=filed_on_value,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return FileResponse(
            out_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=out_name,
        )
