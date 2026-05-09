"""
glovo_downloader.py
Scarica automaticamente il foglio Glovo da Google Sheets come CSV.

Utilizzo:
  python -m pipeline.glovo_downloader \\
      --sheet-id  <ID_GOOGLE_SHEET_GLOVO> \\
      --worksheet <nome_tab>              \\  # opzionale, default: primo foglio
      --sa-json   <path_service_account> \\
      --output    <path_output.csv>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

try:
    import gspread
    from google.oauth2.service_account import Credentials
    HAS_GSPREAD = True
except ImportError:
    HAS_GSPREAD = False

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def download_glovo_csv(
    sheet_id: str,
    service_account_info: dict | str | Path,
    output_path: Path,
    worksheet: str = "",
) -> Path:
    """
    Scarica il foglio Glovo da Google Sheets e lo salva come CSV.

    Parameters
    ----------
    sheet_id             : ID del Google Sheet (dalla URL)
    service_account_info : credenziali service account (dict, path stringa o Path)
    output_path          : dove salvare il CSV
    worksheet            : nome del tab da scaricare (default: primo tab)

    Returns
    -------
    Path del file CSV salvato
    """
    if not HAS_GSPREAD:
        raise ImportError("Installa gspread: pip install gspread google-auth")

    if isinstance(service_account_info, (str, Path)):
        with open(service_account_info, encoding="utf-8") as f:
            info = json.load(f)
    else:
        info = dict(service_account_info)

    creds  = Credentials.from_service_account_info(info, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet  = client.open_by_key(sheet_id)

    if worksheet:
        ws = sheet.worksheet(worksheet)
    else:
        ws = sheet.get_worksheet(0)

    print(f"[glovo_downloader] Scaricando tab '{ws.title}' dal foglio {sheet_id}...")
    all_values = ws.get_all_values()

    if not all_values or len(all_values) < 2:
        raise ValueError(
            f"Il foglio '{ws.title}' sembra vuoto o ha solo l'header. "
            "Assicurati che il connettore BigQuery abbia fatto il refresh prima delle 20:00."
        )

    df = pd.DataFrame(all_values[1:], columns=all_values[0])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"[glovo_downloader] Salvato: {output_path}  ({len(df)} righe)")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scarica il CSV Glovo da Google Sheets"
    )
    parser.add_argument("--sheet-id",  required=True,
                        help="ID del Google Sheet Glovo")
    parser.add_argument("--worksheet", default="",
                        help="Nome del tab da scaricare (default: primo tab)")
    parser.add_argument("--sa-json",   required=True, type=Path,
                        help="Path al JSON del service account")
    parser.add_argument("--output",    required=True, type=Path,
                        help="Path del CSV di output")
    args = parser.parse_args()

    download_glovo_csv(
        sheet_id             = args.sheet_id,
        service_account_info = args.sa_json,
        output_path          = args.output,
        worksheet            = args.worksheet,
    )


if __name__ == "__main__":
    main()
