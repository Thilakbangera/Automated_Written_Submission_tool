from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import FileResponse
import tempfile
import os
from typing import Optional, List

from ..services.pipeline import generate_written_submission

router = APIRouter()

@router.post(
    "/api/generate",
    summary="Generate",
    description="Generates a Written Submission DOCX strictly following the constant master template. FER and HN are handled separately. Returns a DOCX file.",
)
async def generate(
    fer: UploadFile = File(..., description="FER PDF"),
    hn: UploadFile = File(..., description="Hearing Notice PDF"),
    specification: UploadFile = File(..., description="Complete specification PDF"),
    city: str = Form("Chennai", description="Patent Office City (e.g., Chennai, Mumbai, Delhi)"),
    drawings: Optional[UploadFile] = File(None, description="Drawings PDF (optional)"),
    amended_claims: Optional[UploadFile] = File(None, description="Amended claims (PDF/DOCX/TXT) (optional)"),
    tech_solution_images: Optional[List[UploadFile]] = File(None, description="Technical solution diagram screenshots (PNG/JPG)"),
):
    with tempfile.TemporaryDirectory() as td:
        fer_path = os.path.join(td, "fer.pdf")
        hn_path = os.path.join(td, "hn.pdf")
        spec_path = os.path.join(td, "spec.pdf")

        with open(fer_path, "wb") as f:
            f.write(await fer.read())
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

        if amended_claims is not None:
            # Keep the original extension if possible to help parsers
            filename = amended_claims.filename or "amended.bin"
            ext = os.path.splitext(filename)[1].lower() or ".bin"
            amended_path = os.path.join(td, f"amended{ext}")
            with open(amended_path, "wb") as f:
                f.write(await amended_claims.read())

        out_path, out_name = generate_written_submission(
            fer_path=fer_path,
            hn_path=hn_path,
            specification_path=spec_path,
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
