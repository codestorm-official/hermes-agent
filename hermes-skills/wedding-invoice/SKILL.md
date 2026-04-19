---
name: wedding-invoice
description: "Process wedding invoices and receipts for Ari & Vika's wedding budget. Use when Ari or Vika sends a PDF or photo of a wedding-related invoice, receipt, or contract via Telegram. Extracts raw text via pdftotext or Tesseract OCR (Hebrew + German + English), Hermes parses structured fields via his own LLM, shows a preview for confirmation, uploads to Google Drive Rechnungen folder, and appends/upserts a row in the Wedding Budget Google Sheet. Triggers: Rechnung, invoice, receipt, Hochzeit, add to wedding sheet, book this expense."
---

# Wedding Invoice Skill (Hermes port)

## Flow

1. **Extract** raw text from the file (PDF or image) via the helper script.
2. **Parse** structured fields yourself (Hermes's own LLM pass on the raw text).
3. **Show preview** to the user for confirmation.
4. **On OK** -> upload to Drive + append/upsert in the sheet.

## Scripts

- `scripts/extract_invoice.py <file>` -> JSON with `raw_text`, `extraction_method`, `lang_detected`. No fields. You parse those yourself.
- `scripts/add_expense.py --file <file> --data '<json>' [--filename <name>]` -> Drive upload + sheet append/upsert.

## Schema reference

See `references/sheet_schema.md` for column definitions, VAT rules, and IDs. Sheet ID is hard-coded in `add_expense.py`.

## Step-by-step

### Step 1 - Extract raw text

```bash
python3 /opt/hermes-skills/wedding-invoice/scripts/extract_invoice.py /path/to/file.pdf
```

Output JSON: `{file, extraction_method, lang_detected, raw_text}`. `extraction_method` is `pdftotext` (clean PDF), `tesseract` (scanned/image with OCR), or `tesseract_partial` (OCR returned short text, best-effort).

### Step 2 - Parse fields yourself

Using the `raw_text`, extract these fields. Respect the categories in the schema. Amounts in ILS as plain numbers (no commas, no currency symbol):

- `vendor`, `description`, `category`, `payment`, `amount_ils`, `vat_ils`, `include_vat`, `final_ils`, `date`, `method`, `notes`
- `paid_by`: determine from `source.user_id`:
  - `7652652109` -> `Ari`
  - `5289484491` -> `Victoria` (Vika; sheet uses "Victoria" per schema history)
  - Otherwise ask
- `status`: `Paid` if the PDF looks like a paid receipt (Zahlungsbestaetigung, Quittung, has a payment date). `Open` for contracts/quotes/Angebote awaiting payment.

### Step 3 - Show preview

Format as a clean German summary:

```
Rechnung erkannt:
- Vendor: [vendor]
- Kategorie: [category]
- Beschreibung: [description]
- Betrag: [amount_ils] ILS ([final_ils] ILS final)
- MwSt: [vat_ils] ILS ([include_vat])
- Datum: [date]
- Zahlungsmethode: [method]
- Paid By: [paid_by]
- Status: [status]

Soll ich das so eintragen? (Felder anpassen falls noetig)
```

Wait for an explicit OK (or corrections) before proceeding. **Never append silently.**

### Step 4 - Add on confirmation

```bash
python3 /opt/hermes-skills/wedding-invoice/scripts/add_expense.py \
  --file /path/to/file.pdf \
  --data '{"category":"...","vendor":"...","amount_ils":"...","status":"Paid","paid_by":"Ari",...}' \
  --filename "2026-02-20_Vendor_Description.pdf"
```

The script uploads to Drive and appends (or upserts onto a matching Open row). Confirm both succeeded.

## File naming convention for Drive upload

`YYYY-MM-DD_Vendor_Description.ext` - use the invoice/payment date, sanitize vendor (PascalCase, no spaces), short description.

## Exchange Rate Convention

- **Always ILS-per-EUR** (e.g. `3.6134` = 1 EUR buys ILS 3.6134)
- If bank receipt shows EUR-per-ILS (value < 1, e.g. `0.267`), script auto-inverts
- EUR column (K) is always a formula, not a hardcoded value - script handles this

## Smart-upsert

`add_expense.py` finds matching Open row by `vendor + amount_ils`; if found, flips to Paid and fills payment fields. Otherwise appends a new row. Fields preserved on upsert: Category, Vendor, Description, Payment, ILS amounts, VAT. Fields updated: Exchange Rate, Rate Date, EUR Amount, Date Paid, Method, Paid By, Status -> Paid, Notes, Receipt Link.

## Notes

- Hebrew PDFs that aren't text-extractable: Tesseract OCR with `heb+deu+eng` lang pack (installed in the container). No OpenAI API involved.
- Contracts with multiple tranches: one row per tranche. Deposit/Installment/Final.
- If Tesseract returns garbage on a complex receipt, try re-OCR with `--lang heb` only or ask user for the key fields manually.
- Exchange rate: leave blank if not on invoice; user fills manually later.
