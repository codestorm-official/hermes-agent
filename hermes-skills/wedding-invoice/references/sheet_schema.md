# Wedding Budget Sheet Schema

**Sheet ID:** `1sS5fVfTnZp0vbsATmVBtLYVLj0we0LB8DBwVOYc2Vow`
**Sheet name:** `Payments`
**Drive Rechnungen folder:** `1FYdn5VHSNErWdWyC9n4EelPS0K7_XRfR`

## Columns (A-Q)

| # | Column | Notes |
|---|--------|-------|
| A | Category | WeddingPlanner, Venue, Hair & Makeup, Catering, Photography, Music, Flowers, Clothing, Transport, Accommodation, Other |
| B | Vendor | Company or person name |
| C | Description | What was purchased |
| D | Payment | Deposit 40%, Installment 30%, Final 30%, Full, etc. |
| E | Amount (ILS) | Amount before VAT (or total if no VAT), no commas |
| F | VAT (ILS) | VAT amount (17% in Israel) |
| G | Include VAT? | YES or NO |
| H | Final (ILS) | Final amount (= Amount if VAT already included) |
| I | Exchange Rate | ILS-per-EUR (e.g. 3.6134) |
| J | Rate Date | Date of exchange rate (YYYY-MM-DD) |
| K | Amount (EUR) | Formula `=IF(I>0,H/I,"")` - auto-calculated |
| L | Date Paid | YYYY-MM-DD |
| M | Method | Bank / Cash / Credit / Transfer |
| N | Paid By | Father / Ari / Victoria (= Vika) / etc. |
| O | Status | Paid / Open |
| P | Notes | Any relevant notes |
| Q | Receipt Link | Google Drive link to uploaded file |

## VAT Rules (Israel)
- Standard VAT: 17%
- If vendor charges VAT: `include_vat = YES`, `vat_ils = amount * 0.17 / 1.17`
- If vendor is private/abroad: `include_vat = NO`, `vat_ils = 0`

## Exchange Rate
- Always ILS-per-EUR convention
- If bank receipt shows EUR-per-ILS (value < 1), `add_expense.py` auto-inverts
- If not on invoice, leave blank (user fills manually later)
- Format: 4 decimal places (e.g. 3.6134)

## Paid By values
- `Ari` - user_id 7652652109
- `Victoria` - user_id 5289484491 (Vika, but sheet history uses "Victoria")
- `Father` - Ari's father
- Others: ask before inventing
