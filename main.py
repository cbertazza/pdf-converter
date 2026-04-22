import io
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pypdf import PdfWriter

app = FastAPI(title="Conversor de Arquivos")

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

TEMP_DIR = Path(tempfile.gettempdir()) / "pdf_converter"
TEMP_DIR.mkdir(exist_ok=True)

ALLOWED_IMAGES = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


def new_path(suffix: str) -> Path:
    return TEMP_DIR / f"{uuid.uuid4().hex}{suffix}"


def run_pdfa(input_path: Path, output_path: Path, level: int = 2) -> tuple[bool, str]:
    cmd = [
        "ocrmypdf",
        "--skip-text",
        "--tesseract-timeout", "0",
        "--optimize", "0",
        "--invalidate-digital-signatures",
        "--output-type", f"pdfa-{level}",
        "-l", "por+eng",
        str(input_path),
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    success = output_path.exists() and output_path.stat().st_size > 0
    return success, result.stdout + "\n" + result.stderr


def images_to_pdf(image_paths: list[Path], output_path: Path) -> None:
    imgs = [Image.open(p).convert("RGB") for p in image_paths]
    imgs[0].save(output_path, save_all=True, append_images=imgs[1:])
    for img in imgs:
        img.close()


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((BASE_DIR / "static" / "index.html").read_text(encoding="utf-8"))


@app.post("/convert")
async def pdf_to_pdfa(file: UploadFile = File(...), level: int = 2):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Apenas arquivos PDF são aceitos.")
    if level not in (1, 2):
        raise HTTPException(400, "Nível deve ser 1 ou 2.")

    inp = new_path("_in.pdf")
    out = new_path("_out.pdf")
    try:
        inp.write_bytes(await file.read())
        ok, msg = run_pdfa(inp, out, level)
        if not ok:
            raise HTTPException(500, f"Falha: {msg[:800]}")
        stem = Path(file.filename).stem
        return FileResponse(out, media_type="application/pdf", filename=f"{stem}_PDFA-{level}b.pdf")
    finally:
        if inp.exists():
            inp.unlink()


@app.post("/image-to-pdf")
async def image_to_pdf(files: List[UploadFile] = File(...)):
    temps = []
    out = new_path("_out.pdf")
    try:
        for f in files:
            ext = Path(f.filename).suffix.lower()
            if ext not in ALLOWED_IMAGES:
                raise HTTPException(400, f"Formato não suportado: {ext}")
            tmp = new_path(ext)
            tmp.write_bytes(await f.read())
            temps.append(tmp)
        images_to_pdf(temps, out)
        return FileResponse(out, media_type="application/pdf", filename="imagens.pdf")
    finally:
        for t in temps:
            if t.exists():
                t.unlink()


@app.post("/image-to-pdfa")
async def image_to_pdfa(files: List[UploadFile] = File(...), level: int = 2):
    temps = []
    mid = new_path("_mid.pdf")
    out = new_path("_out.pdf")
    try:
        for f in files:
            ext = Path(f.filename).suffix.lower()
            if ext not in ALLOWED_IMAGES:
                raise HTTPException(400, f"Formato não suportado: {ext}")
            tmp = new_path(ext)
            tmp.write_bytes(await f.read())
            temps.append(tmp)
        images_to_pdf(temps, mid)
        ok, msg = run_pdfa(mid, out, level)
        if not ok:
            raise HTTPException(500, f"Falha: {msg[:800]}")
        return FileResponse(out, media_type="application/pdf", filename=f"imagens_PDFA-{level}b.pdf")
    finally:
        for t in temps:
            if t.exists():
                t.unlink()
        if mid.exists():
            mid.unlink()


@app.post("/pdf-to-image")
async def pdf_to_image(file: UploadFile = File(...), format: str = "jpg"):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Apenas arquivos PDF são aceitos.")
    if format not in ("jpg", "png"):
        raise HTTPException(400, "Formato deve ser jpg ou png.")

    inp = new_path("_in.pdf")
    out_dir = TEMP_DIR / uuid.uuid4().hex
    out_dir.mkdir()

    try:
        inp.write_bytes(await file.read())
        device = "jpeg" if format == "jpg" else "png16m"
        ext = "jpg" if format == "jpg" else "png"

        subprocess.run([
            "gs", "-dBATCH", "-dNOPAUSE", "-dSAFER",
            f"-sDEVICE={device}", "-r150",
            f"-sOutputFile={out_dir}/page_%03d.{ext}",
            str(inp),
        ], capture_output=True, timeout=120)

        pages = sorted(out_dir.glob(f"*.{ext}"))
        if not pages:
            raise HTTPException(500, "Falha ao extrair imagens.")

        stem = Path(file.filename).stem

        if len(pages) == 1:
            return FileResponse(pages[0], media_type=f"image/{format}", filename=f"{stem}.{ext}")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in pages:
                zf.write(p, p.name)
        buf.seek(0)
        return StreamingResponse(
            buf, media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{stem}_paginas.zip"'},
        )
    finally:
        if inp.exists():
            inp.unlink()
        for f in out_dir.glob("*"):
            f.unlink()
        if out_dir.exists():
            out_dir.rmdir()


@app.post("/merge-pdf")
async def merge_pdf(files: List[UploadFile] = File(...), pdfa: int = 0):
    # pdfa: 0 = PDF comum, 1 = PDF/A-1b, 2 = PDF/A-2b
    if len(files) < 2:
        raise HTTPException(400, "Envie pelo menos 2 PDFs.")
    if pdfa not in (0, 1, 2):
        raise HTTPException(400, "pdfa deve ser 0, 1 ou 2.")

    temps = []
    merged = new_path("_merged.pdf")
    out = new_path("_out.pdf") if pdfa else merged
    try:
        writer = PdfWriter()
        for f in files:
            if not f.filename.lower().endswith(".pdf"):
                raise HTTPException(400, f"{f.filename} não é um PDF.")
            tmp = new_path("_in.pdf")
            tmp.write_bytes(await f.read())
            temps.append(tmp)
            writer.append(str(tmp))
        with open(merged, "wb") as fp:
            writer.write(fp)

        if pdfa:
            ok, msg = run_pdfa(merged, out, pdfa)
            if not ok:
                raise HTTPException(500, f"Falha na conversão PDF/A: {msg[:800]}")
            suffix = f"_PDFA-{pdfa}b"
        else:
            suffix = ""

        filename = f"unificado{suffix}.pdf"
        return FileResponse(out, media_type="application/pdf", filename=filename)
    finally:
        for t in temps:
            if t.exists():
                t.unlink()
        if pdfa and merged.exists():
            merged.unlink()
