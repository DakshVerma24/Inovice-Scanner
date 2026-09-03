import base64
import io
import json
import time
import traceback

import cv2
import fitz  # PyMuPDF
import numpy as np
import pandas as pd
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
from pyzbar.pyzbar import decode as zbar_decode

app = Flask(__name__)
CORS(app)  # allow the frontend (possibly hosted elsewhere) to call this API

MAX_FILE_SIZE_MB = 15
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB total request cap


# ---------------------------------------------------------------------------
# QR extraction (same core approach as the original script, adapted for
# in-memory PDF bytes instead of files on disk)
# ---------------------------------------------------------------------------

def pdf_page_to_image(page, scale: float) -> np.ndarray:
    matrix = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=matrix)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:  # RGBA -> BGR
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    else:  # RGB -> BGR
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    return img


def try_opencv(img: np.ndarray):
    detector = cv2.QRCodeDetector()
    data, points, _ = detector.detectAndDecode(img)
    return data if data else None


def try_pyzbar(img: np.ndarray):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    for r in zbar_decode(gray):
        if r.type == "QRCODE":
            return r.data.decode("utf-8", errors="replace")
    return None


def preprocess_variants(img: np.ndarray):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    yield img
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)
    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
    sharp = cv2.filter2D(gray, -1, kernel)
    yield cv2.cvtColor(sharp, cv2.COLOR_GRAY2BGR)


def find_qr_in_pdf_bytes(pdf_bytes: bytes):
    """Returns (qr_text, meta) or (None, meta) where meta has page/scale/decoder info."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    scales = [2, 3, 4, 1.5]

    try:
        for page_num, page in enumerate(doc, start=1):
            for scale in scales:
                base_img = pdf_page_to_image(page, scale)
                for variant in preprocess_variants(base_img):
                    for decoder_name, decoder in (("opencv", try_opencv), ("pyzbar", try_pyzbar)):
                        result = decoder(variant)
                        if result:
                            return result, {
                                "page": page_num,
                                "scale": scale,
                                "decoder": decoder_name,
                            }
        return None, {}
    finally:
        doc.close()


def decode_payload(token: str):
    parts = token.split(".")
    if len(parts) >= 2:
        try:
            payload = parts[1]
            padded = payload + "=" * (-len(payload) % 4)
            decoded_bytes = base64.urlsafe_b64decode(padded)
            return json.loads(decoded_bytes)
        except Exception:
            pass
    try:
        return json.loads(token)
    except Exception:
        return {"raw_text": token}


def process_uploaded_files(files):
    """Runs QR extraction across all uploaded files, returns list of per-file results."""
    results = []
    for f in files:
        entry = {
            "filename": f.filename,
            "status": "error",
            "raw_text": None,
            "decoded": None,
            "meta": {},
            "error": None,
        }
        try:
            pdf_bytes = f.read()
            if not pdf_bytes:
                entry["error"] = "Empty file."
                results.append(entry)
                continue

            qr_text, meta = find_qr_in_pdf_bytes(pdf_bytes)
            entry["meta"] = meta

            if not qr_text:
                entry["error"] = "No QR code found."
                results.append(entry)
                continue

            entry["raw_text"] = qr_text
            entry["status"] = "ok"

            decoded_data = decode_payload(qr_text)
            raw_data = decoded_data.get("data", decoded_data) if isinstance(decoded_data, dict) else decoded_data
            if isinstance(raw_data, str):
                try:
                    invoice_fields = json.loads(raw_data)
                except json.JSONDecodeError:
                    invoice_fields = {"raw_qr_text": qr_text}
            elif isinstance(raw_data, dict):
                invoice_fields = raw_data
            else:
                invoice_fields = {"raw_qr_text": qr_text}

            entry["decoded"] = invoice_fields

        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()

        results.append(entry)
    return results


def get_uploaded_pdfs():
    files = request.files.getlist("files")
    pdfs = [f for f in files if f and f.filename.lower().endswith(".pdf")]
    return pdfs


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/scan", methods=["POST"])
def scan():
    """Scans uploaded PDFs and returns raw + decoded results as JSON (no Excel)."""
    start = time.time()
    pdfs = get_uploaded_pdfs()
    if not pdfs:
        return jsonify({"error": "No PDF files were uploaded."}), 400

    results = process_uploaded_files(pdfs)
    elapsed = round(time.time() - start, 2)

    ok_count = sum(1 for r in results if r["status"] == "ok")
    return jsonify(
        {
            "results": results,
            "summary": {
                "total": len(results),
                "success": ok_count,
                "failed": len(results) - ok_count,
                "elapsed_seconds": elapsed,
            },
        }
    )


@app.route("/api/scan/excel", methods=["POST"])
def scan_excel():
    """Scans uploaded PDFs and returns a ready-to-download Excel workbook."""
    pdfs = get_uploaded_pdfs()
    if not pdfs:
        return jsonify({"error": "No PDF files were uploaded."}), 400

    results = process_uploaded_files(pdfs)

    records = []
    failures = []
    for r in results:
        if r["status"] == "ok" and r["decoded"]:
            row = dict(r["decoded"])
            row["Source_File"] = r["filename"]
            records.append(row)
        else:
            failures.append({"Source_File": r["filename"], "Error": r["error"] or "Unknown error"})

    if not records:
        return jsonify({"error": "No valid invoice data could be extracted from any file.", "results": results}), 422

    df = pd.DataFrame(records)
    cols = ["Source_File"] + [c for c in df.columns if c != "Source_File"]
    df = df[cols]

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="InvoiceList", index=False)
        if failures:
            pd.DataFrame(failures).to_excel(writer, sheet_name="Failed", index=False)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="invoice_data.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
