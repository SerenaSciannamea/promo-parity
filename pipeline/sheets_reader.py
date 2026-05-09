"""
sheets_reader.py
Legge i dati di parity da Google Sheets per il dashboard cloud.

Funziona sia con credenziali da file (locale) che da st.secrets (Streamlit Cloud).
"""

from __future__ import annotations

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

TAB_STORE_PARITY  = "store_parity"
TAB_CITY_PARITY   = "city_parity"
TAB_STORE_MAPPING = "store_mapping"
TAB_NEEDS_REVIEW  = "needs_review"


def _get_client(service_account_info: dict | str | Path) -> "gspread.Client":
    if not HAS_GSPREAD:
        raise ImportError("Installa gspread: pip install gspread google-auth")
    if isinstance(service_account_info, (str, Path)):
        with open(service_account_info, encoding="utf-8") as f:
            info = json.load(f)
    else:
        info = dict(service_account_info)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _read_tab(sheet: "gspread.Spreadsheet", tab_name: str) -> pd.DataFrame:
    """Legge un tab e restituisce DataFrame. Restituisce DataFrame vuoto se il tab non esiste."""
    try:
        ws = sheet.worksheet(tab_name)
        records = ws.get_all_records(default_blank="")
        return pd.DataFrame(records) if records else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def read_all(
    spreadsheet_id: str,
    service_account_info: dict | str | Path,
) -> dict[str, pd.DataFrame]:
    """
    Legge tutti i tab dal Google Sheet di output.

    Returns
    -------
    {
      "store_parity" : DataFrame,
      "city_parity"  : DataFrame,
      "store_mapping": DataFrame,
      "needs_review" : DataFrame,
    }
    """
    client = _get_client(service_account_info)
    sheet  = client.open_by_key(spreadsheet_id)

    store_parity  = _read_tab(sheet, TAB_STORE_PARITY)
    city_parity   = _read_tab(sheet, TAB_CITY_PARITY)
    store_mapping = _read_tab(sheet, TAB_STORE_MAPPING)
    needs_review  = _read_tab(sheet, TAB_NEEDS_REVIEW)

    # Cast numerici
    for df, num_cols in [
        (store_parity,  ["glovo_rank", "deliveroo_rank", "glovo_pct_off",
                         "glovo_promo_products", "revenue", "promo_coverage_pct"]),
        (city_parity,   ["n_stores_total", "n_stores_matched", "n_unmatched",
                         "n_superiority", "n_parity", "n_inferiority",
                         "pct_superiority", "pct_parity", "pct_inferiority",
                         "w_superiority", "w_parity", "w_inferiority",
                         "match_coverage_pct"]),
    ]:
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

    return {
        TAB_STORE_PARITY:  store_parity,
        TAB_CITY_PARITY:   city_parity,
        TAB_STORE_MAPPING: store_mapping,
        TAB_NEEDS_REVIEW:  needs_review,
    }


def write_store_mapping(
    spreadsheet_id: str,
    service_account_info: dict | str | Path,
    mapping_df: pd.DataFrame,
) -> None:
    """
    Aggiorna il tab store_mapping su Google Sheets.
    Usato dal dashboard cloud quando un utente conferma/rifiuta un match.
    """
    from pipeline.sheets_writer import _get_client as _wc, _get_or_create_worksheet, _upsert_sheet
    client = _wc(service_account_info)
    sheet  = client.open_by_key(spreadsheet_id)
    ws     = _get_or_create_worksheet(sheet, TAB_STORE_MAPPING, mapping_df.columns.tolist())
    _upsert_sheet(ws, mapping_df, key_cols=["city_code", "glovo_name"])


def write_needs_review(
    spreadsheet_id: str,
    service_account_info: dict | str | Path,
    review_df: pd.DataFrame,
) -> None:
    """Aggiorna il tab needs_review su Google Sheets."""
    from pipeline.sheets_writer import _get_client as _wc, _get_or_create_worksheet, _upsert_sheet
    client = _wc(service_account_info)
    sheet  = client.open_by_key(spreadsheet_id)
    ws     = _get_or_create_worksheet(sheet, TAB_NEEDS_REVIEW, review_df.columns.tolist())
    _upsert_sheet(ws, review_df, key_cols=["city_code", "glovo_name"])
