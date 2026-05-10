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
import io
import json
from pathlib import Path

import pandas as pd
import requests

try:
    import gspread
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build as google_build
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

    Usa l'API Drive export URL per evitare problemi con nomi di tab che
    contengono caratteri speciali (es. '[RAW]Products' dei connettori BigQuery).

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

    # Trova il worksheet giusto per nome, oppure prendi il primo
    if worksheet:
        ws = sheet.worksheet(worksheet)
    else:
        ws = sheet.get_worksheet(0)

    gid   = ws._properties["sheetId"]
    title = ws.title
    print(f"[glovo_downloader] Tab trovato: '{title}' (gid={gid})")

    # -----------------------------------------------------------------------
    # Legge tutte le righe tramite gspread batch usando lo sheetId numerico
    # (aggira il problema dei nomi con [ ] che rompono la notazione A1).
    # -----------------------------------------------------------------------
    if not creds.valid:
        creds.refresh(GoogleRequest())

    # gspread batch_get accetta ranges come lista — usiamo solo il nome del
    # foglio (senza apici), che gspread URL-encoda correttamente internamente.
    # Se anche questo fallisce, ricadiamo sulla Drive export (500 righe max).
    try:
        rows = ws.get_all_values()
    except Exception:
        # Fallback: Drive export URL (funziona con nomi speciali ma limita a ~500 righe)
        export_url = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            f"/export?format=csv&gid={gid}"
        )
        headers_req = {"Authorization": f"Bearer {creds.token}"}
        response = requests.get(export_url, headers=headers_req, timeout=120)
        if response.status_code != 200:
            raise RuntimeError(
                f"Download fallito (HTTP {response.status_code}): {response.text[:300]}"
            )
        df = pd.read_csv(io.StringIO(response.text), dtype=str).fillna("")
        if df.empty:
            raise ValueError(f"Il foglio '{title}' sembra vuoto.")
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        print(f"[glovo_downloader] Salvato (fallback Drive export): {output_path}  ({len(df)} righe, {len(df.columns)} colonne)")
        return output_path

    if not rows:
        raise ValueError(f"Il foglio '{title}' sembra vuoto.")

    headers_row = rows[0]
    data_rows   = rows[1:]
    df = pd.DataFrame(
        [r + [""] * (len(headers_row) - len(r)) for r in data_rows],
        columns=headers_row,
        dtype=str,
    ).fillna("")

    if df.empty or len(df) < 1:
        raise ValueError(
            f"Il foglio '{title}' sembra vuoto. "
            "Assicurati che il connettore BigQuery abbia fatto il refresh prima delle 20:00."
        )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"[glovo_downloader] Salvato: {output_path}  ({len(df)} righe, {len(df.columns)} colonne)")
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
