#!/usr/bin/env python3
"""
Wedding Invoice Skill - extract_invoice.py (Hermes Railway port)

Extracts raw text from a PDF or image invoice.
Supports Hebrew + German + English via Tesseract OCR.

No OpenAI / paid API dependency - Hermes parses fields himself from the raw_text.

Usage:
  python3 extract_invoice.py /path/to/file.pdf
  python3 extract_invoice.py /path/to/receipt.jpg
  python3 extract_invoice.py /path/to/file.pdf --lang heb   # Hebrew-only

Output: JSON with raw_text, extraction_method, lang_detected.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def extract_text_pdftotext(path: str) -> str:
    """Try pdftotext first - works for text-embedded PDFs (most German invoices)."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def pdf_to_pngs(pdf_path: str) -> list[str]:
    """Convert every page of a PDF to PNG at 200 DPI via pdftoppm. Returns list of PNG paths."""
    out_prefix = pdf_path + "_p"
    subprocess.run(
        ["pdftoppm", "-r", "200", "-png", pdf_path, out_prefix],
        check=False, timeout=120,
    )
    pngs = []
    for suffix in ["-1", "-01", "-001"]:
        i = 1
        while True:
            candidate = f"{out_prefix}{suffix.replace('1', str(i).zfill(len(suffix)-1))}.png"
            # pdftoppm can emit -1, -01, -001 depending on total pages. Walk pattern.
            if suffix == "-1":
                candidate = f"{out_prefix}-{i}.png"
            elif suffix == "-01":
                candidate = f"{out_prefix}-{str(i).zfill(2)}.png"
            else:
                candidate = f"{out_prefix}-{str(i).zfill(3)}.png"
            if os.path.exists(candidate):
                pngs.append(candidate)
                i += 1
            else:
                break
        if pngs:
            break
    return pngs


def extract_text_tesseract(image_paths: list[str], lang: str = "heb+deu+eng") -> str:
    """Run Tesseract on each image path, concatenate with form feeds between pages."""
    pages = []
    for img in image_paths:
        try:
            r = subprocess.run(
                ["tesseract", img, "-", "-l", lang, "--psm", "6"],
                capture_output=True, text=True, timeout=120,
            )
            pages.append(r.stdout.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            pages.append(f"[tesseract error on {img}: {e}]")
    return "\n\n--- page break ---\n\n".join(pages)


def cleanup_pngs(pngs: list[str]) -> None:
    for p in pngs:
        try: os.remove(p)
        except OSError: pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="PDF or image file")
    parser.add_argument("--lang", default="heb+deu+eng", help="Tesseract language code(s)")
    args = parser.parse_args()

    path = args.path
    if not os.path.exists(path):
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    ext = Path(path).suffix.lower()

    raw_text = ""
    method = ""

    if ext == ".pdf":
        raw_text = extract_text_pdftotext(path)
        if len(raw_text) >= 50:
            method = "pdftotext"
        else:
            pngs = pdf_to_pngs(path)
            if pngs:
                raw_text = extract_text_tesseract(pngs, lang=args.lang)
                cleanup_pngs(pngs)
                method = "tesseract" if len(raw_text) >= 50 else "tesseract_partial"
            else:
                method = "pdftotext_empty"
    else:
        raw_text = extract_text_tesseract([path], lang=args.lang)
        method = "tesseract" if len(raw_text) >= 50 else "tesseract_partial"

    result = {
        "file": path,
        "extraction_method": method,
        "lang_detected": args.lang,
        "raw_text": raw_text,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
