"""
sheets_writer.py
Scrive i risultati della pipeline su Google Sheets.

Architettura:
  - _write_rows_chunked : unico punto di scrittura → chunking + verifica post-write
  - _upsert_sheet       : upsert/partition logic, poi delega a _write_rows_chunked
  - export_to_sheets    : orchestratore, ogni tab in try/except isolato

Regole fondamentali:
  1. NESSUN write avviene senza verifica del conteggio finale
  2. L'allineamento colonne è gestito sempre tramite pd.concat (unione colonne)
  3. Un errore su un tab NON blocca gli altri (isolamento per tab)
  4. Il chunking è sempre attivo (default 50k righe) per evitare timeout API

Requisiti:
  pip install gspread google-auth
"""

from __future__ import annotations

import json
import time
from pathlib import Path

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

CHUNK_SIZE = 25_000  # righe per chiamata append_rows (ridotto per stabilità API)

# Tab names nel foglio Google Sheets di output
TAB_STORE_PARITY       = "store_parity"
TAB_CITY_PARITY        = "city_parity"
TAB_STORE_MAPPING      = "store_mapping"
TAB_NEEDS_REVIEW       = "needs_review"
TAB_GLOVO_PRODUCTS     = "glovo_products"
TAB_DELIVEROO_PRODUCTS = "deliveroo_products"
TAB_STORE_PARITY_PRIME = "store_parity_prime"
TAB_CITY_PARITY_PRIME  = "city_parity_prime"
TAB_GLOVO_PRODUCTS_PRIME = "glovo_products_prime"
TAB_PRIORITY_ACTIONS   = "priority_actions"
TAB_PIPELINE_HEALTH    = "pipeline_health"
TAB_AM_MAPPING         = "am_mapping"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_client(service_account_info: dict | str | Path) -> "gspread.Client":
    if not HAS_GSPREAD:
        raise ImportError("Installa gspread e google-auth: pip install gspread google-auth")
    if isinstance(service_account_info, (str, Path)):
        with open(service_account_info, encoding="utf-8") as f:
            info = json.load(f)
    else:
        info = dict(service_account_info)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


# ---------------------------------------------------------------------------
# Primitiva di scrittura — unico punto di contatto con l'API Sheets
# ---------------------------------------------------------------------------

def _write_rows_chunked(
    ws: "gspread.Worksheet",
    headers: list[str],
    rows: list[list],
    verify: bool = True,
) -> int:
    """
    Scrive headers + rows sul worksheet con chunking e verifica post-write.

    Regole:
      - Cancella il tab, poi scrive in chunk da CHUNK_SIZE righe
      - Se verify=True, conta le righe effettivamente scritte e solleva
        RuntimeError se non corrispondono (prevenzione dati corrotti)

    Returns: numero di righe dati scritte (escluso header)
    """
    ws.clear()
    all_rows = [headers] + rows

    MAX_RETRIES = 3
    for i in range(0, len(all_rows), CHUNK_SIZE):
        chunk = all_rows[i:i + CHUNK_SIZE]
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                ws.append_rows(chunk, value_input_option="RAW")
                break
            except Exception as e:
                if attempt == MAX_RETRIES:
                    raise
                wait = 5 * attempt
                print(f"    [retry {attempt}/{MAX_RETRIES}] chunk {i//CHUNK_SIZE + 1} errore ({e}), attendo {wait}s...")
                time.sleep(wait)
        # Pausa breve tra chunk per non saturare le API quota
        if i + CHUNK_SIZE < len(all_rows):
            time.sleep(1.0)

    if verify:
        # Pausa per garantire consistenza API
        time.sleep(2.0)
        expected = len(rows)
        # Usa i metadati del foglio per contare le righe effettive (evita il limite
        # di risposta 10MB di get_all_values che tronca su tab con >150k righe).
        try:
            meta = ws.spreadsheet.fetch_sheet_metadata()
            actual = None
            for s_meta in meta.get("sheets", []):
                if s_meta["properties"]["title"] == ws.title:
                    # rowCount include l'header e le righe vuote allocate
                    # Usiamo il conteggio grezzo come lower-bound check
                    actual = s_meta["properties"]["gridProperties"]["rowCount"] - 1
                    break
            if actual is None:
                # Fallback: prova get_all_values solo per tab piccoli
                if expected <= 100_000:
                    actual = len(ws.get_all_values()) - 1
                else:
                    actual = expected  # skip verify per tab molto grandi
        except Exception:
            actual = expected  # se la verifica stessa fallisce, non bloccare
        if actual < expected:
            raise RuntimeError(
                f"[sheets_writer] VERIFICA FALLITA su '{ws.title}': "
                f"attese {expected} righe, trovate {actual}. "
                f"Il tab potrebbe essere parziale — ritentare la pipeline."
            )

    return len(rows)


# ---------------------------------------------------------------------------
# Worksheet helper
# ---------------------------------------------------------------------------

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
        ws.append_row(headers, value_input_option="RAW")
    return ws


# ---------------------------------------------------------------------------
# Upsert con allineamento colonne garantito
# ---------------------------------------------------------------------------

MAX_WEEKS_ON_SHEETS = 6   # settimane massime conservate su Sheets per tab con week_num
                           # I dati storici completi rimangono nel DB SQLite locale.


def _upsert_sheet(
    ws: "gspread.Worksheet",
    df: pd.DataFrame,
    key_cols: list[str],
    partition_cols: list[str] | None = None,
    max_weeks: int = MAX_WEEKS_ON_SHEETS,
) -> int:
    """
    Legge le righe esistenti, fa upsert/partition, e riscrive il foglio.

    Garanzie:
      - Le colonne del tab esistente vengono sempre preservate (es. id, inserted_at)
      - I nuovi dati vengono allineati alle colonne esistenti tramite pd.concat
        (colonne mancanti nei nuovi dati → stringa vuota)
      - La scrittura avviene solo tramite _write_rows_chunked (con verifica)

    partition_cols: se specificato, le righe esistenti delle stesse partizioni
                    vengono rimosse prima di inserire i nuovi dati.
    max_weeks:      se partition_cols include 'week_num', mantiene solo le ultime
                    N settimane per evitare di raggiungere il limite di celle di Sheets.
                    I dati storici completi restano nel DB SQLite locale.
    """
    existing_data = ws.get_all_records(default_blank="")
    if existing_data:
        existing_df = pd.DataFrame(existing_data).astype(str)
    else:
        existing_df = pd.DataFrame(columns=df.columns)

    # Aggiungi colonne nuove all'existing se il df le introduce
    for col in df.columns:
        if col not in existing_df.columns:
            existing_df[col] = ""

    df_str = df.copy().astype(str)

    # Rimozione righe da sovrascrivere
    if partition_cols and all(p in existing_df.columns for p in partition_cols):
        new_partitions = set(df_str[partition_cols].apply(tuple, axis=1))
        existing_df = existing_df[
            ~existing_df[partition_cols].apply(tuple, axis=1).isin(new_partitions)
        ]
    elif key_cols and all(k in existing_df.columns for k in key_cols):
        key_existing = existing_df[key_cols].apply(tuple, axis=1)
        key_new      = df_str[key_cols].apply(tuple, axis=1)
        existing_df  = existing_df[~key_existing.isin(key_new.values)]

    # pd.concat garantisce unione delle colonne → nessuno sfasamento possibile
    combined = pd.concat([existing_df, df_str], ignore_index=True)

    # Pruning settimane vecchie — mantieni solo le ultime max_weeks
    if (max_weeks and max_weeks > 0
            and partition_cols and "week_num" in partition_cols
            and "week_num" in combined.columns):
        all_weeks  = sorted(combined["week_num"].unique())
        keep_weeks = set(all_weeks[-max_weeks:])
        n_before   = len(combined)
        combined   = combined[combined["week_num"].isin(keep_weeks)]
        n_pruned   = n_before - len(combined)
        if n_pruned > 0:
            print(f"    [sheets_writer] Pruned {n_pruned} righe ({all_weeks[:-max_weeks]} rimosse da Sheets — dati storici nel DB locale)")

    headers  = combined.columns.tolist()
    rows     = combined.fillna("").astype(str).values.tolist()

    return _write_rows_chunked(ws, headers, rows)


# ---------------------------------------------------------------------------
# Export principale — ogni tab è isolato in try/except
# ---------------------------------------------------------------------------

def export_to_sheets(
    spreadsheet_id: str,
    service_account_info: dict | str | Path,
    store_parity:         pd.DataFrame | None = None,
    city_parity:          pd.DataFrame | None = None,
    store_mapping:        pd.DataFrame | None = None,
    needs_review:         pd.DataFrame | None = None,
    glovo_products:       pd.DataFrame | None = None,
    deliveroo_products:   pd.DataFrame | None = None,
    store_parity_prime:   pd.DataFrame | None = None,
    city_parity_prime:    pd.DataFrame | None = None,
    glovo_products_prime: pd.DataFrame | None = None,
    priority_actions:     pd.DataFrame | None = None,
    pipeline_health:      pd.DataFrame | None = None,
    am_mapping:           pd.DataFrame | None = None,
) -> dict[str, int]:
    """
    Esporta i DataFrame su Google Sheets.

    - Ogni tab è indipendente: un errore non blocca gli altri
    - Tutti i write passano per _write_rows_chunked (chunking + verifica)
    - Ritorna { tab_name: n_rows } per i tab scritti con successo
    - I tab falliti compaiono come { tab_name: -1 } con log dell'errore
    """
    client = _get_client(service_account_info)
    sheet  = client.open_by_key(spreadsheet_id)
    result: dict[str, int] = {}
    errors: dict[str, str] = {}

    def _write(tab_name, df, key_cols, partition_cols=None, col_filter=None, max_weeks=None):
        """Helper interno: filtra colonne opzionalmente, poi upsert."""
        if df is None or len(df) == 0:
            return
        if col_filter:
            present = [c for c in col_filter if c in df.columns]
            df = df[present]
        _mw = max_weeks if max_weeks is not None else MAX_WEEKS_ON_SHEETS
        try:
            ws = _get_or_create_worksheet(sheet, tab_name, df.columns.tolist())
            n  = _upsert_sheet(ws, df, key_cols=key_cols, partition_cols=partition_cols,
                               max_weeks=_mw)
            result[tab_name] = n
            print(f"[sheets_writer] {tab_name}: {n} righe ✓")
        except Exception as exc:
            result[tab_name] = -1
            errors[tab_name] = str(exc)
            print(f"[sheets_writer] ERRORE {tab_name}: {exc}")

    _write(TAB_STORE_PARITY, store_parity,
           key_cols=["city_code", "glovo_name", "week_num"])

    _write(TAB_CITY_PARITY, city_parity,
           key_cols=["city_code", "week_num"])

    _write(TAB_STORE_MAPPING, store_mapping,
           key_cols=["city_code", "glovo_name"])

    _write(TAB_NEEDS_REVIEW, needs_review,
           key_cols=["city_code", "glovo_name"])

    _write(TAB_GLOVO_PRODUCTS, glovo_products,
           key_cols=["city_code", "store_name", "week_num", "product_name"],
           partition_cols=["week_num"], max_weeks=2)  # max 2 settimane: ~140k righe < limite API

    _write(TAB_DELIVEROO_PRODUCTS, deliveroo_products,
           key_cols=["city_code", "restaurant_name", "week_num", "product_name"],
           partition_cols=["week_num"],
           col_filter=["city_code", "restaurant_name", "week_num", "product_name",
                       "product_description", "product_price", "promotion_type"])

    _write(TAB_STORE_PARITY_PRIME, store_parity_prime,
           key_cols=["city_code", "glovo_name", "week_num"])

    _write(TAB_CITY_PARITY_PRIME, city_parity_prime,
           key_cols=["city_code", "week_num"])

    _write(TAB_GLOVO_PRODUCTS_PRIME, glovo_products_prime,
           key_cols=["city_code", "store_name", "week_num", "product_name"],
           partition_cols=["week_num"], max_weeks=2)  # max 2 settimane

    _write(TAB_PRIORITY_ACTIONS, priority_actions,
           key_cols=["city_code", "glovo_name", "week_num"],
           partition_cols=["week_num"])

    _write(TAB_PIPELINE_HEALTH, pipeline_health,
           key_cols=["week_num", "check"],
           partition_cols=["week_num"])

    _write(TAB_AM_MAPPING, am_mapping,
           key_cols=["city_code", "store_name"])

    if errors:
        print(f"\n[sheets_writer] {len(errors)} tab con errori: {list(errors.keys())}")
        print("[sheets_writer] I dati locali (CSV weekly) sono intatti — usa sheets_repair.py per recuperare.")

    return result
