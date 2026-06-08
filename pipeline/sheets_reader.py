"""
sheets_reader.py
Legge i dati di parity da Google Sheets per il dashboard cloud.

Funziona sia con credenziali da file (locale) che da st.secrets (Streamlit Cloud).
"""

from __future__ import annotations

import datetime
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

TAB_STORE_PARITY       = "store_parity"
TAB_CITY_PARITY        = "city_parity"
TAB_STORE_MAPPING      = "store_mapping"
TAB_NEEDS_REVIEW       = "needs_review"
TAB_MANUAL_MATCHES     = "manual_matches"      # append-only, scritto dall'UI
TAB_GLOVO_PRODUCTS     = "glovo_products"
TAB_DELIVEROO_PRODUCTS = "deliveroo_products"
TAB_STORE_PARITY_PRIME = "store_parity_prime"
TAB_CITY_PARITY_PRIME  = "city_parity_prime"
TAB_GLOVO_PRODUCTS_PRIME = "glovo_products_prime"
TAB_PRIORITY_ACTIONS   = "priority_actions"
TAB_PIPELINE_HEALTH    = "pipeline_health"
TAB_AM_MAPPING         = "am_mapping"

MANUAL_COLS = [
    "city_code", "glovo_name", "glovo_store_id",
    "deliveroo_name", "confidence", "source", "updated_at",
]


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


def _get_or_create_manual_ws(sheet: "gspread.Spreadsheet") -> "gspread.Worksheet":
    """Restituisce il tab manual_matches, creandolo con l'header se necessario."""
    try:
        ws = sheet.worksheet(TAB_MANUAL_MATCHES)
        # Controlla che abbia almeno l'header
        try:
            first_row = ws.row_values(1)
            if not first_row:
                ws.append_row(MANUAL_COLS)
        except Exception:
            ws.append_row(MANUAL_COLS)
        return ws
    except Exception:
        # Tab non esiste: crealo
        ws = sheet.add_worksheet(TAB_MANUAL_MATCHES, rows=2000, cols=len(MANUAL_COLS))
        ws.append_row(MANUAL_COLS)
        return ws


def append_manual_match(
    spreadsheet_id: str,
    service_account_info: dict | str | Path,
    row: dict,
) -> None:
    """
    Appende UNA sola riga al tab manual_matches (append, mai riscrittura).
    Velocissimo: un solo API call di scrittura.

    row deve contenere almeno: city_code, glovo_name, deliveroo_name
    deliveroo_name = "" significa "non presente su Deliveroo"
    """
    client = _get_client(service_account_info)
    sheet  = client.open_by_key(spreadsheet_id)
    ws     = _get_or_create_manual_ws(sheet)

    values = [
        row.get("city_code", ""),
        row.get("glovo_name", ""),
        row.get("glovo_store_id", ""),
        row.get("deliveroo_name", ""),
        str(row.get("confidence", "1.0")),
        row.get("source", "manual_cloud"),
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ]
    ws.append_row(values, value_input_option="RAW")


def read_all(
    spreadsheet_id: str,
    service_account_info: dict | str | Path,
) -> dict[str, pd.DataFrame]:
    """
    Legge tutti i tab dal Google Sheet di output e applica i match manuali.

    Returns
    -------
    {
      "store_parity" : DataFrame,
      "city_parity"  : DataFrame,
      "store_mapping": DataFrame,   # gia' con override da manual_matches
      "needs_review" : DataFrame,   # gia' filtrato (rimossi i gia'-confermati)
    }
    """
    client = _get_client(service_account_info)
    sheet  = client.open_by_key(spreadsheet_id)

    store_parity  = _read_tab(sheet, TAB_STORE_PARITY)
    city_parity   = _read_tab(sheet, TAB_CITY_PARITY)
    store_mapping = _read_tab(sheet, TAB_STORE_MAPPING)
    needs_review  = _read_tab(sheet, TAB_NEEDS_REVIEW)
    manual_matches = _read_tab(sheet, TAB_MANUAL_MATCHES)

    # -------------------------------------------------------------------------
    # Merge manual_matches → store_mapping
    # I match manuali hanno priorita'; prendiamo l'ultimo per ogni (city, glovo)
    # -------------------------------------------------------------------------
    if not manual_matches.empty and "city_code" in manual_matches.columns:
        # Prendi l'ultimo record per coppia (city_code, glovo_name)
        mm = manual_matches.copy()
        mm = mm.drop_duplicates(subset=["city_code", "glovo_name"], keep="last")
        mm = mm[["city_code", "glovo_name", "glovo_store_id",
                 "deliveroo_name", "confidence", "source"]]

        if not store_mapping.empty and "city_code" in store_mapping.columns:
            # Rimuovi righe che verranno sovrascritte, MA preserva le Exclusive Glovo
            # (source=manual_rejected): un rifiuto fuzzy via UI non deve cancellare
            # un'esclusiva marcata in bulk.
            key_set = set(zip(mm["city_code"], mm["glovo_name"]))
            exclusive_keys = set(
                zip(
                    store_mapping.loc[store_mapping["source"] == "manual_rejected", "city_code"],
                    store_mapping.loc[store_mapping["source"] == "manual_rejected", "glovo_name"],
                )
            )
            mask = [
                (r["city_code"], r["glovo_name"]) not in key_set
                or r.get("source", "") == "manual_rejected"
                for _, r in store_mapping.iterrows()
            ]
            store_mapping = store_mapping[mask]
            # Aggiungi solo le righe di manual_matches che NON sono già Exclusive Glovo
            mm_filtered = mm[~mm.apply(
                lambda r: (r["city_code"], r["glovo_name"]) in exclusive_keys, axis=1
            )]
            store_mapping = pd.concat([store_mapping, mm_filtered], ignore_index=True)
        else:
            store_mapping = mm

        # Rimuovi da needs_review le coppie gia' risolte manualmente
        if not needs_review.empty and "city_code" in needs_review.columns:
            resolved = set(zip(mm["city_code"], mm["glovo_name"]))
            mask_nr = [
                (r["city_code"], r["glovo_name"]) not in resolved
                for _, r in needs_review.iterrows()
            ]
            needs_review = needs_review[mask_nr]

    # -------------------------------------------------------------------------
    # Applica override manuali a store_parity
    # Gli store marcati come Esclusiva Glovo o Non su Deliveroo nel mapping
    # devono risultare EXCLUSIVE_GLOVO in store_parity senza aspettare il
    # prossimo run della pipeline.
    # -------------------------------------------------------------------------
    if not store_parity.empty and not store_mapping.empty and "parity" in store_parity.columns:
        exclusive_sources = {"manual_rejected", "not_on_deliveroo"}
        excl_keys = set(
            zip(
                store_mapping.loc[store_mapping["source"].isin(exclusive_sources), "city_code"],
                store_mapping.loc[store_mapping["source"].isin(exclusive_sources), "glovo_name"],
            )
        )
        if excl_keys:
            mask_excl = store_parity.apply(
                lambda r: (r.get("city_code", ""), r.get("glovo_name", "")) in excl_keys,
                axis=1,
            )
            store_parity.loc[mask_excl, "parity"] = "EXCLUSIVE_GLOVO"
            # Pulisci anche deliveroo_name e deliveroo_rank per gli store esclusivi
            if "deliveroo_name" in store_parity.columns:
                store_parity.loc[mask_excl, "deliveroo_name"] = ""

    # -------------------------------------------------------------------------
    # Cast numerici
    # -------------------------------------------------------------------------
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

    glovo_products       = _read_tab(sheet, TAB_GLOVO_PRODUCTS)
    deliveroo_products   = _read_tab(sheet, TAB_DELIVEROO_PRODUCTS)
    store_parity_prime   = _read_tab(sheet, TAB_STORE_PARITY_PRIME)
    city_parity_prime    = _read_tab(sheet, TAB_CITY_PARITY_PRIME)
    glovo_products_prime = _read_tab(sheet, TAB_GLOVO_PRODUCTS_PRIME)
    priority_actions     = _read_tab(sheet, TAB_PRIORITY_ACTIONS)
    pipeline_health      = _read_tab(sheet, TAB_PIPELINE_HEALTH)
    am_mapping           = _read_tab(sheet, TAB_AM_MAPPING)

    # Cast numerici prodotti Glovo
    for col in ["avg_percentage_off", "avg_unit_price", "total_product_sold"]:
        if col in glovo_products.columns:
            glovo_products[col] = pd.to_numeric(glovo_products[col], errors="coerce")

    # Cast numerici Prime
    prime_num_cols = ["glovo_rank", "deliveroo_rank", "glovo_pct_off",
                      "glovo_promo_products", "revenue", "promo_coverage_pct"]
    for col in prime_num_cols:
        if col in store_parity_prime.columns:
            store_parity_prime[col] = pd.to_numeric(store_parity_prime[col], errors="coerce")

    city_prime_num_cols = ["n_stores_total", "n_stores_matched", "n_unmatched",
                           "n_superiority", "n_parity", "n_inferiority",
                           "pct_superiority", "pct_parity", "pct_inferiority",
                           "w_superiority", "w_parity", "w_inferiority",
                           "match_coverage_pct"]
    for col in city_prime_num_cols:
        if col in city_parity_prime.columns:
            city_parity_prime[col] = pd.to_numeric(city_parity_prime[col], errors="coerce")

    # Cast numerici priority_actions
    for col in ["revenue", "glovo_pct_off", "promo_coverage_pct", "priority"]:
        if col in priority_actions.columns:
            priority_actions[col] = pd.to_numeric(priority_actions[col], errors="coerce")

    return {
        TAB_STORE_PARITY:       store_parity,
        TAB_CITY_PARITY:        city_parity,
        TAB_STORE_MAPPING:      store_mapping,
        TAB_NEEDS_REVIEW:       needs_review,
        TAB_GLOVO_PRODUCTS:     glovo_products,
        TAB_DELIVEROO_PRODUCTS: deliveroo_products,
        TAB_STORE_PARITY_PRIME: store_parity_prime,
        TAB_CITY_PARITY_PRIME:  city_parity_prime,
        TAB_GLOVO_PRODUCTS_PRIME: glovo_products_prime,
        TAB_PRIORITY_ACTIONS:     priority_actions,
        TAB_PIPELINE_HEALTH:      pipeline_health,
        TAB_AM_MAPPING:           am_mapping,
    }


def write_store_mapping(
    spreadsheet_id: str,
    service_account_info: dict | str | Path,
    mapping_df: pd.DataFrame,
) -> None:
    """
    Aggiorna il tab store_mapping su Google Sheets.
    Usato dal pipeline settimanale (non dall'UI cloud — usa append_manual_match).
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
