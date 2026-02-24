import json
import os
import tempfile
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..services.pipeline import generate_written_submission

router = APIRouter()

@router.post(
    "/api/generate",
    summary="Generate",
    description="Generates a Written Submission DOCX from HN + specification + prior-art entries. Returns a DOCX file.",
)
async def generate(
    hn: UploadFile = File(..., description="Hearing Notice PDF"),
    specification: UploadFile = File(..., description="Complete specification PDF"),
    city: str = Form("Chennai", description="Patent Office City (e.g., Chennai, Mumbai, Delhi)"),
    prior_arts_json: str = Form(..., description="JSON array of prior arts with fields: label, abstract, summary"),
    prior_art_diagrams: Optional[List[UploadFile]] = File(None, description="Prior-art diagram images in D1..Dn order"),
    drawings: Optional[UploadFile] = File(None, description="Drawings PDF (optional)"),
    amended_claims: Optional[UploadFile] = File(None, description="Amended claims (PDF/DOCX/TXT) (optional)"),
    tech_solution_images: Optional[List[UploadFile]] = File(None, description="Technical solution diagram screenshots (PNG/JPG)"),
):
    try:
        raw_prior_arts = json.loads(prior_arts_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid prior_arts_json: {exc.msg}") from exc

    if not isinstance(raw_prior_arts, list) or not raw_prior_arts:
        raise HTTPException(status_code=422, detail="prior_arts_json must be a non-empty JSON array.")

    prior_arts_entries: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_prior_arts, start=1):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f"Prior art entry #{idx} must be a JSON object.")
        abstract = str(item.get("abstract", "")).strip()
        summary = str(item.get("summary", "")).strip()
        if not abstract or not summary:
            raise HTTPException(
                status_code=422,
                detail=f"Prior art entry #{idx} must include non-empty abstract and summary.",
            )
        prior_arts_entries.append(
            {
                "label": str(item.get("label", "")).strip(),
                "abstract": abstract,
                "summary": summary,
            }
        )

    with tempfile.TemporaryDirectory() as td:
        hn_path = os.path.join(td, "hn.pdf")
        spec_path = os.path.join(td, "spec.pdf")

        with open(hn_path, "wb") as f:
            f.write(await hn.read())
        with open(spec_path, "wb") as f:
            f.write(await specification.read())

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

        if len(prior_art_diagram_paths) < len(prior_arts_entries):
            raise HTTPException(
                status_code=422,
                detail="Each prior art entry (D1-Dn) must include one diagram image (PNG/JPG).",
            )
        for idx, entry in enumerate(prior_arts_entries):
            entry["diagram_image_path"] = prior_art_diagram_paths[idx]

        if amended_claims is not None:
            # Keep the original extension if possible to help parsers
            filename = amended_claims.filename or "amended.bin"
            ext = os.path.splitext(filename)[1].lower() or ".bin"
            amended_path = os.path.join(td, f"amended{ext}")
            with open(amended_path, "wb") as f:
                f.write(await amended_claims.read())

        out_path, out_name = generate_written_submission(
            hn_path=hn_path,
            specification_path=spec_path,
            prior_arts_entries=prior_arts_entries,
            drawings_path=drawings_path,
            amended_claims_path=amended_path,
            tech_solution_images_paths=tech_solution_image_paths,
            city=city,
        )

        return FileResponse(
            out_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=out_name,
        )
