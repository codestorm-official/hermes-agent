#!/usr/bin/env python3
"""
Wedding Invoice Skill - add_expense.py (Hermes Railway port)

Uploads a receipt to Google Drive and smart-upserts a row in the Wedding Budget sheet.
Uses Ari's existing Google OAuth tokens stored on the Railway volume.

Smart-upsert logic:
  1. Search for existing Open row matching vendor + amount_ils
  2. If found -> update that row with payment details (status -> Paid, EUR, date, method, receipt)
  3. If not found -> append new row

Usage:
  python3 add_expense.py --file /path/to/receipt.pdf --data '{...json...}'
"""

import argparse
import json
import os
import sys
import mimetypes
import re
import requests
from datetime import date

# On Railway, tokens live on the /data volume so they persist across redeploys.
TOKENS_PATH = os.environ.get(
    "GOOGLE_TOKENS_PATH", "/data/.hermes/google_tokens.json"
)
SHEET_ID   = "1sS5fVfTnZp0vbsATmVBtLYVLj0we0LB8DBwVOYc2Vow"
FOLDER_ID  = "1FYdn5VHSNErWdWyC9n4EelPS0K7_XRfR"
SHEET_NAME = "Payments"

# Column indices (0-based) matching sheet schema:
# A=Category B=Vendor C=Description D=Payment E=Amount(ILS) F=VAT(ILS) G=IncludeVAT
# H=Final(ILS) I=ExchangeRate J=RateDate K=Amount(EUR) L=DatePaid M=Method
# N=PaidBy O=Status P=Notes Q=ReceiptLink
COL = {
    "category":0, "vendor":1, "description":2, "payment":3,
    "amount_ils":4, "vat_ils":5, "include_vat":6, "final_ils":7,
    "exchange_rate":8, "rate_date":9, "amount_eur":10, "date_paid":11,
    "method":12, "paid_by":13, "status":14, "notes":15, "receipt_link":16,
}

ILS_FMT = {"type": "NUMBER", "pattern": '"\u20aa"#,##0'}
EUR_FMT = {"type": "NUMBER", "pattern": '"\u20ac"#,##0.00'}
ILS_COLS = [4, 5, 7]   # E=Amount(ILS), F=VAT(ILS), H=Final(ILS)
EUR_COLS = [10]        # K=Amount(EUR)


def load_token() -> str:
    with open(TOKENS_PATH) as f:
        tokens = json.load(f)
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": tokens["client_id"],
        "client_secret": tokens["client_secret"],
        "refresh_token": tokens["refresh_token"],
        "grant_type": "refresh_token",
    })
    new = r.json()
    if "access_token" in new:
        tokens["access_token"] = new["access_token"]
        with open(TOKENS_PATH, "w") as f:
            json.dump(tokens, f)
    return tokens["access_token"]


def upload_to_drive(token: str, file_path: str, filename: str | None = None) -> tuple[str, str]:
    mime, _ = mimetypes.guess_type(file_path)
    mime = mime or "application/octet-stream"
    fname = filename or os.path.basename(file_path)
    metadata = json.dumps({"name": fname, "parents": [FOLDER_ID]})
    with open(file_path, "rb") as fh:
        content = fh.read()
    r = requests.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "metadata": ("metadata", metadata, "application/json; charset=UTF-8"),
            "file": (fname, content, mime),
        },
    )
    r.raise_for_status()
    data = r.json()
    return data["id"], data.get("webViewLink", f"https://drive.google.com/file/d/{data['id']}/view")


def get_sheet_id(token: str) -> int:
    r = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}?fields=sheets.properties",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    for s in r.json().get("sheets", []):
        if s["properties"]["title"] == SHEET_NAME:
            return s["properties"]["sheetId"]
    return 0


def get_all_rows(token: str) -> list[list[str]]:
    r = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{SHEET_NAME}!A:Q",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json().get("values", [])


def find_matching_open_row(rows: list[list[str]], vendor: str, amount_ils):
    def norm_amount(v):
        return str(v).replace(",", "").replace(".", "").strip()

    vendor_lower = (vendor or "").lower()
    vendor_words = [w for w in vendor_lower.split() if len(w) > 3]
    target_amt = norm_amount(amount_ils)

    for i, row in enumerate(rows):
        if i == 0:
            continue
        while len(row) < 17:
            row.append("")

        if row[COL["status"]].strip() != "Open":
            continue

        row_vendor = (row[COL["vendor"]] or "").lower()
        row_amt = norm_amount(row[COL["amount_ils"]])
        vendor_match = any(w in row_vendor for w in vendor_words) if vendor_words else False
        amt_match = (row_amt == target_amt)

        if vendor_match and amt_match:
            return i, row
    return None


def to_number(val):
    try:
        return float(str(val).replace(",", "").replace("\u20aa", "").replace("\u20ac", "").strip())
    except Exception:
        return None


def normalize_rate(rate_val):
    try:
        r = float(str(rate_val).replace(",", "").strip())
        if 0 < r < 1:
            return round(1 / r, 4)
        return r
    except Exception:
        return None


def normalize_row_numbers(row: list, row_num_1based: int | None = None) -> list:
    row = list(row)
    for col in ILS_COLS:
        if col < len(row) and row[col]:
            n = to_number(row[col])
            if n is not None:
                row[col] = n
    if len(row) > 8 and row[8]:
        new_rate = normalize_rate(row[8])
        if new_rate:
            row[8] = new_rate
    while len(row) < 17:
        row.append("")
    if row_num_1based and row[8]:
        row[10] = f"=IF(I{row_num_1based}>0,H{row_num_1based}/I{row_num_1based},\"\")"
    elif row[8]:
        row[10] = ""
    return row


def apply_currency_format(token: str, sheet_id_num: int, row_index_0based: int) -> None:
    reqs = []
    for col in ILS_COLS:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sheet_id_num,
                      "startRowIndex": row_index_0based, "endRowIndex": row_index_0based + 1,
                      "startColumnIndex": col, "endColumnIndex": col + 1},
            "cell": {"userEnteredFormat": {"numberFormat": ILS_FMT}},
            "fields": "userEnteredFormat.numberFormat",
        }})
    for col in EUR_COLS:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sheet_id_num,
                      "startRowIndex": row_index_0based, "endRowIndex": row_index_0based + 1,
                      "startColumnIndex": col, "endColumnIndex": col + 1},
            "cell": {"userEnteredFormat": {"numberFormat": EUR_FMT}},
            "fields": "userEnteredFormat.numberFormat",
        }})
    r = requests.post(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"requests": reqs},
    )
    r.raise_for_status()


def update_row(token: str, sheet_id_num: int, row_index_0based: int, updated_row: list) -> dict:
    row_num = row_index_0based + 1
    updated_row = normalize_row_numbers(updated_row, row_num_1based=row_num)
    range_str = f"{SHEET_NAME}!A{row_num}:Q{row_num}"
    r = requests.put(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{range_str}"
        "?valueInputOption=USER_ENTERED",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"range": range_str, "values": [updated_row]},
    )
    r.raise_for_status()
    apply_currency_format(token, sheet_id_num, row_index_0based)
    return r.json()


def append_row(token: str, sheet_id_num: int, row: list) -> dict:
    row = normalize_row_numbers(row, row_num_1based=None)
    row[10] = ""
    r = requests.post(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{SHEET_NAME}!A:Q:append"
        "?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"values": [row]},
    )
    r.raise_for_status()
    result = r.json()
    updated_range = result.get("updates", {}).get("updatedRange", "")
    try:
        m = re.search(r"(\d+):", updated_range.split("!")[1])
        if m:
            row_num = int(m.group(1))
            if row[8]:
                r2 = requests.put(
                    f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{SHEET_NAME}!K{row_num}"
                    "?valueInputOption=USER_ENTERED",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"range": f"{SHEET_NAME}!K{row_num}",
                          "values": [[f"=IF(I{row_num}>0,H{row_num}/I{row_num},\"\")"]]},
                )
                r2.raise_for_status()
            apply_currency_format(token, sheet_id_num, row_num - 1)
    except Exception as e:
        print(f"Warning: post-append formula/format: {e}", file=sys.stderr)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Path to receipt file (PDF/image)")
    parser.add_argument("--data", required=True, help="JSON string with expense fields")
    parser.add_argument("--filename", help="Override filename in Drive")
    args = parser.parse_args()

    d = json.loads(args.data)
    token = load_token()
    sheet_id_num = get_sheet_id(token)

    receipt_link = d.get("receipt_link", "")
    if args.file and os.path.isfile(args.file):
        _, receipt_link = upload_to_drive(token, args.file, args.filename)
        print(f"Uploaded: {receipt_link}")

    rows = get_all_rows(token)
    match = find_matching_open_row(rows, d.get("vendor", ""), d.get("amount_ils", ""))

    if match:
        row_idx, existing_row = match
        updated = list(existing_row)
        while len(updated) < 17:
            updated.append("")
        if d.get("exchange_rate"): updated[COL["exchange_rate"]] = d["exchange_rate"]
        if d.get("rate_date"):     updated[COL["rate_date"]]     = d["rate_date"]
        if d.get("amount_eur"):    updated[COL["amount_eur"]]    = d["amount_eur"]
        if d.get("date_paid"):     updated[COL["date_paid"]]     = d["date_paid"]
        if d.get("method"):        updated[COL["method"]]        = d["method"]
        if d.get("paid_by"):       updated[COL["paid_by"]]       = d["paid_by"]
        updated[COL["status"]] = "Paid"
        if d.get("notes"):         updated[COL["notes"]]         = d["notes"]
        if receipt_link:           updated[COL["receipt_link"]]  = receipt_link

        update_row(token, sheet_id_num, row_idx, updated)
        print(f"Row {row_idx+1} updated (Open -> Paid): {existing_row[COL['vendor']]} | ILS {existing_row[COL['amount_ils']]}")
        print(json.dumps({"action": "updated", "row_index": row_idx+1, "receipt_link": receipt_link}, indent=2))
    else:
        row = [
            d.get("category", ""), d.get("vendor", ""), d.get("description", ""),
            d.get("payment", ""), d.get("amount_ils", ""), d.get("vat_ils", ""),
            d.get("include_vat", ""), d.get("final_ils", d.get("amount_ils", "")),
            d.get("exchange_rate", ""), d.get("rate_date", ""), d.get("amount_eur", ""),
            d.get("date_paid", str(date.today())), d.get("method", ""), d.get("paid_by", ""),
            d.get("status", "Paid"), d.get("notes", ""), receipt_link,
        ]
        append_row(token, sheet_id_num, row)
        print(f"New row appended (no matching Open row found)")
        print(json.dumps({"action": "appended", "row": row, "receipt_link": receipt_link}, indent=2))


if __name__ == "__main__":
    main()
