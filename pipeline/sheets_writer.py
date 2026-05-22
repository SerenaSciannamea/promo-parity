"""
sheets_writer.py
Scrive i risultati della pipeline su Google Sheets.

Il foglio di output ha 4 tab:
  - store_parity   : risultati store-level (una riga per store x week)
  - city_parity    : risultati city-level
  - store_mapping  : ground truth matching Glovo <-> Deliveroo
  - needs_review   : coda revisione match automatici

I dati vengono AGGIUNTI (append) in coda alle righe esistenti,
con deduplicazione su (city_code, glovo_name, week_num).

Requisiti:
  pip install gspread google-auth
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

try:
    import gspread
    from google.oauth2.service_account import Credentials
    HAS_GSPREAD = True
except ImportError:
    HAS_GSPREAD = False

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Tab names nel foglio Google Sheets di output
TAB_STORE_PARITY       = "store_parity"
TAB_CITY_PARITY        = "city_parity"
TAB_STORE_MAPPING      = "store_mapping"
TAB_NEEDS_REVIEW       = "needs_review"
TAB_GLOVO_PRODUCTS     = "glovo_products"
TAB_DELIVEROO_PRODUCTS = "deliveroo_products"
TAB_STORE_PARITY_PRIME = "store_parity_prime"
TAB_CITY_PARITY_PRIME  = "city_parity_prime"


def _get_client(service_account_info: dict | str | Path) -> "gspread.Client":
    """
    Crea un client gspread autenticato.

    service_account_info può essere:
      - dict  : contenuto JSON del service account (da st.secrets)
      - str   : percorso al file JSON
      - Path  : percorso al file JSON
    """
    if not HAS_GSPREAD:
        raise ImportError("Installa gspread e google-auth: pip install gspread google-auth")

    if isinstance(service_account_info, (str, Path)):
        with open(service_account_info, encoding="utf-8") as f:
            info = json.load(f)
    else:
        info = dict(service_account_info)

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_or_create_worksheet(
    sheet: "gspread.Spreadsheet",
    tab_name: str,
    headers: list[str],
) -> "gspread.Worksheet":
    """Restituisce il worksheet, creandolo con le intestazioni se non esiste."""
    try:
        ws = sheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=tab_name, rows=1, cols=len(headers))
        ws.append_row(headers)
    return ws


def _upsert_sheet(
    ws: "gspread.Worksheet",
    df: pd.DataFrame,
    key_cols: list[str],
    partition_cols: list[str] | None = None,
) -> int:
    """
    Legge le righe esistenti nel worksheet, fa upsert su key_cols,
    e riscrive il foglio con i dati aggiornati.

    partition_cols: se specificato, tutte le righe esistenti che appartengono
    alle stesse partizioni dei nuovi dati vengono cancellate prima dell'inserimento.
    Esempio: partition_cols=["week_num"] garantisce che i dati della stessa
    settimana vengano sempre sovrascritti completamente (run multipli nella
    stessa settimana non si accumulano).

    Restituisce il numero di righe scritte.
    """
    existing_data = ws.get_all_records(default_blank="")
    if existing_data:
        existing_df = pd.DataFrame(existing_data).astype(str)
    else:
        existing_df = pd.DataFrame(columns=df.columns)

    # Assicura che tutte le colonne del df siano presenti
    for col in df.columns:
        if col not in existing_df.columns:
            existing_df[col] = ""

    df_str = df.copy().astype(str)

    if partition_cols and all(p in existing_df.columns for p in partition_cols):
        # Rimuovi tutte le righe esistenti delle stesse partizioni dei nuovi dati
        new_partitions = set(df_str[partition_cols].apply(tuple, axis=1))
        existing_df = existing_df[
            ~existing_df[partition_cols].apply(tuple, axis=1).isin(new_partitions)
        ]
    elif key_cols and all(k in existing_df.columns for k in key_cols):
        # Fallback: upsert standard per chiave
        key_existing = existing_df[key_cols].apply(tuple, axis=1)
        key_new      = df_str[key_cols].apply(tuple, axis=1)
        existing_df  = existing_df[~key_existing.isin(key_new.values)]

    combined = pd.concat([existing_df, df_str], ignore_index=True)

    # Riscrivi il foglio
    ws.clear()
    headers = combined.columns.tolist()
    ws.append_row(headers)
    if len(combined) > 0:
        ws.append_rows(combined.fillna("").values.tolist())

    return len(combined)


def _replace_sheet(
    sheet: "gspread.Spreadsheet",
    tab_name: str,
    df: pd.DataFrame,
) -> int:
    """
    Sostituisce TUTTO il contenuto di un tab con df (non fa upsert).
    Usato per tab 'latest-only' come glovo_products e deliveroo_products.
    """
    try:
        ws = sheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=tab_name, rows=1, cols=len(df.columns))
    ws.clear()
    ws.append_row(df.columns.tolist())
    if len(df) > 0:
        ws.append_rows(df.fillna("").astype(str).values.tolist())
    return len(df)


def export_to_sheets(
    spreadsheet_id: str,
    service_account_info: dict | str | Path,
    store_parity: pd.DataFrame | None = None,
    city_parity:  pd.DataFrame | None = None,
    store_mapping: pd.DataFrame | None = None,
    needs_review:  pd.DataFrame | None = None,
    glovo_products: pd.DataFrame | None = None,
    deliveroo_products: pd.DataFrame | None = None,
    store_parity_prime: pd.DataFrame | None = None,
    city_parity_prime:  pd.DataFrame | None = None,
) -> dict[str, int]:
    """
    Esporta i DataFrame su Google Sheets.

    Parameters
    ----------
    spreadsheet_id       : ID del Google Sheet di output (dalla URL)
    service_account_info : credenziali service account
    store_parity         : DataFrame store-level parity
    city_parity          : DataFrame city-level parity
    store_mapping        : DataFrame ground truth mapping
    needs_review         : DataFrame coda revisione

    Returns
    -------
    dict { tab_name: n_rows_written }
    """
    client = _get_client(service_account_info)
    sheet  = client.open_by_key(spreadsheet_id)
    result = {}

    if store_parity is not None and len(store_parity) > 0:
        ws = _get_or_create_worksheet(sheet, TAB_STORE_PARITY, store_parity.columns.tolist())
        n  = _upsert_sheet(ws, store_parity, key_cols=["city_code", "glovo_name", "week_num"])
        result[TAB_STORE_PARITY] = n
        print(f"[sheets_writer] store_parity: {n} righe scritte")

    if city_parity is not None and len(city_parity) > 0:
        ws = _get_or_create_worksheet(sheet, TAB_CITY_PARITY, city_parity.columns.tolist())
        n  = _upsert_sheet(ws, city_parity, key_cols=["city_code", "week_num"])
        result[TAB_CITY_PARITY] = n
        print(f"[sheets_writer] city_parity: {n} righe scritte")

    if store_mapping is not None and len(store_mapping) > 0:
        ws = _get_or_create_worksheet(sheet, TAB_STORE_MAPPING, store_mapping.columns.tolist())
        n  = _upsert_sheet(ws, store_mapping, key_cols=["city_code", "glovo_name"])
        result[TAB_STORE_MAPPING] = n
        print(f"[sheets_writer] store_mapping: {n} righe scritte")

    if needs_review is not None and len(needs_review) > 0:
        ws = _get_or_create_worksheet(sheet, TAB_NEEDS_REVIEW, needs_review.columns.tolist())
        n  = _upsert_sheet(ws, needs_review, key_cols=["city_code", "glovo_name"])
        result[TAB_NEEDS_REVIEW] = n
        print(f"[sheets_writer] needs_review: {n} righe scritte")

    # Tab prodotti: upsert per settimana (mantiene storico settimane precedenti)
    if glovo_products is not None and len(glovo_products) > 0:
        ws = _get_or_create_worksheet(sheet, TAB_GLOVO_PRODUCTS, glovo_products.columns.tolist())
        n  = _upsert_sheet(ws, glovo_products,
                           key_cols=["city_code", "store_name", "week_num", "product_name"],
                           partition_cols=["week_num"])
        result[TAB_GLOVO_PRODUCTS] = n
        print(f"[sheets_writer] glovo_products: {n} righe scritte")

    if deliveroo_products is not None and len(deliveroo_products) > 0:
        # Filtra colonne utili — week_num incluso per filtro settimana nella dashboard
        dp_cols = ["city_code", "restaurant_name", "week_num", "product_name",
                   "product_description", "product_price", "promotion_type"]
        dp_cols_present = [c for c in dp_cols if c in deliveroo_products.columns]
        dp_df = deliveroo_products[dp_cols_present]
        ws = _get_or_create_worksheet(sheet, TAB_DELIVEROO_PRODUCTS, dp_df.columns.tolist())
        n  = _upsert_sheet(ws, dp_df,
                           key_cols=["city_code", "restaurant_name", "week_num", "product_name"],
                           partition_cols=["week_num"])
        result[TAB_DELIVEROO_PRODUCTS] = n
        print(f"[sheets_writer] deliveroo_products: {n} righe scritte")

    if store_parity_prime is not None and len(store_parity_prime) > 0:
        ws = _get_or_create_worksheet(sheet, TAB_STORE_PARITY_PRIME, store_parity_prime.columns.tolist())
        n  = _upsert_sheet(ws, store_parity_prime, key_cols=["city_code", "glovo_name", "week_num"])
        result[TAB_STORE_PARITY_PRIME] = n
        print(f"[sheets_writer] store_parity_prime: {n} righe scritte")

    if city_parity_prime is not None and len(city_parity_prime) > 0:
        ws = _get_or_create_worksheet(sheet, TAB_CITY_PARITY_PRIME, city_parity_prime.columns.tolist())
        n  = _upsert_sheet(ws, city_parity_prime, key_cols=["city_code", "week_num"])
        result[TAB_CITY_PARITY_PRIME] = n
        print(f"[sheets_writer] city_parity_prime: {n} righe scritte")

    return result
