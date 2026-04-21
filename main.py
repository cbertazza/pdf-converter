import glob
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="PDF para PDF/A")

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

TEMP_DIR = Path(tempfile.gettempdir()) / "pdf_converter"
TEMP_DIR.mkdir(exist_ok=True)


def find_gs_icc_profile() -> str | None:
    patterns = [
        "/usr/share/ghostscript/*/iccprofiles/srgb.icc",
        "/usr/share/ghostscript/*/iccprofiles/default_rgb.icc",
        "/usr/share/color/icc/ghostscript/srgb.icc",
        "/usr/share/ghostscript/*/iccprofiles/sRGB.icc",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


def convert_to_pdfa(input_path: Path, output_path: Path, level: int = 2) -> tuple[bool, str]:
    icc = find_gs_icc_profile()

    cmd = [
        "gs",
        f"-dPDFA={level}",
        "-dBATCH",
        "-dNOPAUSE",
        "-dNOOUTERSAVE",
        "-sDEVICE=pdfwrite",
        "-sProcessColorModel=DeviceRGB",
        "-sPDFACompatibilityPolicy=1",
        "-dEmbedAllFonts=true",
        "-dSubsetFonts=true",
    ]

    if icc:
        cmd += [f"-sDefaultRGBProfile={icc}", f"-sOutputICCProfile={icc}"]

    cmd += [f"-sOutputFile={output_path}", str(input_path)]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    full_output = result.stdout + "\n" + result.stderr
    return result.returncode == 0, full_output


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/convert")
async def convert(file: UploadFile = File(...), level: int = 2):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Apenas arquivos PDF são aceitos.")

    if level not in (1, 2):
        raise HTTPException(status_code=400, detail="Nível deve ser 1 (PDF/A-1b) ou 2 (PDF/A-2b).")

    job_id = uuid.uuid4().hex
    input_path = TEMP_DIR / f"{job_id}_input.pdf"
    output_path = TEMP_DIR / f"{job_id}_output.pdf"

    try:
        input_path.write_bytes(await file.read())

        success, output = convert_to_pdfa(input_path, output_path, level)

        if not success or not output_path.exists():
            raise HTTPException(status_code=500, detail=f"Falha na conversão: {output[:800]}")

        original_name = Path(file.filename).stem
        download_name = f"{original_name}_PDFA-{level}b.pdf"

        return FileResponse(
            path=output_path,
            media_type="application/pdf",
            filename=download_name,
            background=None,
        )
    finally:
        if input_path.exists():
            input_path.unlink()
