"""
run_weekly.py
Orchestratore della pipeline settimanale di promo parity.

Flusso:
  1. Legge CSV Glovo (esportato da Google Sheets)
  2. Legge dati Deliveroo scraper (deliveroo_promo_deduped.csv)
  3. Importa match manuali da Stores.csv (prima esecuzione o aggiornamenti)
  4. Calcola store matching Glovo <-> Deliveroo
  5. Calcola parity store-level e city-level
  6. Salva risultati in SQLite (append storico) + CSV settimanali

Utilizzo:
  python -m pipeline.run_weekly --glovo-csv <path> [--week 2026-W20] [--stores-csv <path>]

Oppure importato come modulo e chiamato da run_friday.ps1 via Task Scheduler.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path

import pandas as pd

# Forza UTF-8 su stdout/stderr per gestire nomi store con caratteri non-ASCII su Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Aggiungi la root del progetto al path in modo da trovare pipeline/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.glovo_reader      import load_glovo_csv, aggregate_store_level
from pipeline.store_matcher     import (
    import_stores_csv,
    match_glovo_stores,
    load_mapping,
)
from pipeline.parity_calculator import compute_store_parity, compute_city_parity

# ---------------------------------------------------------------------------
# Percorsi default
# ---------------------------------------------------------------------------
DELIVEROO_DEDUPED   = ROOT / "output" / "deliveroo_promo_deduped.csv"
DELIVEROO_PRODUCTS  = ROOT / "output" / "deliveroo_promo_products.csv"
STORES_CSV          = ROOT / "Stores.csv"
DB_PATH           = ROOT / "data" / "promo_parity.db"
WEEKLY_DIR        = ROOT / "data" / "weekly"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


def init_db(conn: sqlite3.Connection) -> None:
    """Crea le tabelle se non esistono."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS store_parity (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            city_code            TEXT NOT NULL,
            glovo_name           TEXT NOT NULL,
            deliveroo_name       TEXT,
            week_num             TEXT NOT NULL,
            glovo_promo_type     TEXT,
            glovo_rank           REAL,
            glovo_rank_label     TEXT,
            deliveroo_promo_text TEXT,
            deliveroo_rank       REAL,
            deliveroo_rank_label TEXT,
            parity               TEXT,
            glovo_pct_off        REAL,
            glovo_promo_products INTEGER,
            revenue              REAL,
            promo_coverage_pct   REAL,
            inserted_at          TEXT DEFAULT (datetime('now')),
            UNIQUE(city_code, glovo_name, week_num)
        );

        CREATE TABLE IF NOT EXISTS city_parity (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            city_code           TEXT NOT NULL,
            week_num            TEXT NOT NULL,
            n_stores_total      INTEGER,
            n_stores_matched    INTEGER,
            n_unmatched         INTEGER,
            n_superiority       INTEGER,
            n_parity            INTEGER,
            n_inferiority       INTEGER,
            pct_superiority     REAL,
            pct_parity          REAL,
            pct_inferiority     REAL,
            w_superiority       REAL,
            w_parity            REAL,
            w_inferiority       REAL,
            city_parity_label   TEXT,
            match_coverage_pct  REAL,
            inserted_at         TEXT DEFAULT (datetime('now')),
            UNIQUE(city_code, week_num)
        );

        CREATE TABLE IF NOT EXISTS glovo_products (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            city_code            TEXT NOT NULL,
            store_name           TEXT NOT NULL,
            week_num             TEXT NOT NULL,
            product_name         TEXT,
            type_of_promo        TEXT,
            has_active_promo     TEXT,
            avg_percentage_off   REAL,
            avg_unit_price       REAL,
            total_product_sold   REAL,
            UNIQUE(city_code, store_name, week_num, product_name)
        );

        CREATE TABLE IF NOT EXISTS glovo_products_prime (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            city_code             TEXT NOT NULL,
            store_name            TEXT NOT NULL,
            week_num              TEXT NOT NULL,
            product_name          TEXT,
            type_of_promo_np      TEXT,
            has_active_promo_np   TEXT,
            avg_percentage_off_np REAL,
            type_of_promo_p       TEXT,
            has_active_promo_p    TEXT,
            avg_percentage_off_p  REAL,
            avg_unit_price        REAL,
            total_product_sold    REAL,
            UNIQUE(city_code, store_name, week_num, product_name)
        );

        CREATE TABLE IF NOT EXISTS store_parity_prime (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            city_code            TEXT NOT NULL,
            glovo_name           TEXT NOT NULL,
            deliveroo_name       TEXT,
            week_num             TEXT NOT NULL,
            glovo_promo_type     TEXT,
            glovo_rank           REAL,
            glovo_rank_label     TEXT,
            deliveroo_promo_text TEXT,
            deliveroo_rank       REAL,
            deliveroo_rank_label TEXT,
            parity               TEXT,
            glovo_pct_off        REAL,
            glovo_promo_products INTEGER,
            revenue              REAL,
            promo_coverage_pct   REAL,
            inserted_at          TEXT DEFAULT (datetime('now')),
            UNIQUE(city_code, glovo_name, week_num)
        );

        CREATE TABLE IF NOT EXISTS city_parity_prime (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            city_code           TEXT NOT NULL,
            week_num            TEXT NOT NULL,
            n_stores_total      INTEGER,
            n_stores_matched    INTEGER,
            n_unmatched         INTEGER,
            n_superiority       INTEGER,
            n_parity            INTEGER,
            n_inferiority       INTEGER,
            pct_superiority     REAL,
            pct_parity          REAL,
            pct_inferiority     REAL,
            w_superiority       REAL,
            w_parity            REAL,
            w_inferiority       REAL,
            city_parity_label   TEXT,
            match_coverage_pct  REAL,
            inserted_at         TEXT DEFAULT (datetime('now')),
            UNIQUE(city_code, week_num)
        );

        CREATE INDEX IF NOT EXISTS idx_store_week  ON store_parity(week_num);
        CREATE INDEX IF NOT EXISTS idx_store_city  ON store_parity(city_code);
        CREATE INDEX IF NOT EXISTS idx_city_week   ON city_parity(week_num);
        CREATE INDEX IF NOT EXISTS idx_gp_store    ON glovo_products(city_code, store_name, week_num);
        CREATE INDEX IF NOT EXISTS idx_gpp_store   ON glovo_products_prime(city_code, store_name, week_num);
        CREATE INDEX IF NOT EXISTS idx_sp_prime_week ON store_parity_prime(week_num);
        CREATE INDEX IF NOT EXISTS idx_cp_prime_week ON city_parity_prime(week_num);
    """)
    conn.commit()


def upsert_df(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """
    INSERT OR REPLACE dei dati nel database.
    Restituisce il numero di righe inserite/aggiornate.
    """
    if df.empty:
        return 0

    # Rimuovi colonna 'id' se presente (auto-increment)
    cols = [c for c in df.columns if c != "id"]
    placeholders = ", ".join(["?"] * len(cols))
    col_names    = ", ".join(cols)

    sql = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"

    records = [tuple(row[c] for c in cols) for _, row in df.iterrows()]
    conn.executemany(sql, records)
    conn.commit()
    return len(records)


# ---------------------------------------------------------------------------
# Utilita'
# ---------------------------------------------------------------------------

def current_week_num() -> str:
    """Restituisce la settimana corrente in formato ISO (es. '2026-W19')."""
    today = date.today()
    iso   = today.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def load_deliveroo_deduped(path: Path = DELIVEROO_DEDUPED) -> pd.DataFrame:
    """Carica il CSV deduplicato di Deliveroo."""
    if not path.exists():
        print(f"[run_weekly] ATTENZIONE: {path} non trovato. Deliveroo vuoto.")
        return pd.DataFrame(columns=["city_code", "restaurant_name", "promotion_type"])

    df = pd.read_csv(path, dtype=str).fillna("")
    df.columns = [c.strip().lower() for c in df.columns]
    return df


# ---------------------------------------------------------------------------
# Pipeline principale
# ---------------------------------------------------------------------------

def run_pipeline(
    glovo_csv:  str | Path,
    week_num:   str | None  = None,
    stores_csv: str | Path  = STORES_CSV,
    db_path:    Path        = DB_PATH,
    save_csv:   bool        = True,
    sheets_id:  str | None  = None,
    sheets_sa:  str | None  = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Esegue la pipeline completa per una settimana.

    Parameters
    ----------
    glovo_csv   : percorso al CSV Glovo esportato da Google Sheets
    week_num    : es. '2026-W20'. Se None usa la settimana corrente.
    stores_csv  : percorso a Stores.csv per import mapping iniziale
    db_path     : percorso al database SQLite
    save_csv    : se True salva anche i CSV settimanali in data/weekly/

    Returns
    -------
    (store_parity_df, city_parity_df)
    """
    week = week_num or current_week_num()
    print(f"\n{'='*60}")
    print(f" Pipeline Promo Parity — {week}")
    print(f"{'='*60}")

    # ---- 1. Leggi e aggrega Glovo ----
    print(f"\n[1/5] Lettura CSV Glovo: {glovo_csv}")
    glovo_raw         = load_glovo_csv(str(glovo_csv))
    glovo_store       = aggregate_store_level(glovo_raw, prime_mode=False)
    glovo_store_prime = aggregate_store_level(glovo_raw, prime_mode=True)
    # Forza week_num al valore passato (il CSV potrebbe avere settimane diverse)
    glovo_store["week_num"]       = week
    glovo_store_prime["week_num"] = week
    glovo_raw["week_num"]         = week
    has_prime_data = "promotion_prime" in glovo_raw.columns
    print(f"      {len(glovo_store)} store Glovo caricati"
          f"{'  [prime data disponibile]' if has_prime_data else ''}")

    # ---- 2. Leggi Deliveroo deduped ----
    print(f"\n[2/5] Lettura Deliveroo deduped: {DELIVEROO_DEDUPED}")
    deliveroo_df = load_deliveroo_deduped()
    print(f"      {len(deliveroo_df)} store Deliveroo caricati")

    # ---- 3. Import mapping iniziale da Stores.csv + manual_matches da Sheets ----
    print(f"\n[3/5] Import mapping da Stores.csv")
    if Path(stores_csv).exists():
        import_stores_csv(stores_csv)
    else:
        print(f"      Stores.csv non trovato, skip import")

    # Importa manual_matches da Google Sheets nel SQLite locale
    if sheets_id and sheets_sa:
        try:
            import json as _json
            import gspread as _gspread
            from google.oauth2.service_account import Credentials as _Creds
            from pipeline.store_matcher import confirm_match, reject_match
            _scopes = ["https://spreadsheets.google.com/feeds",
                       "https://www.googleapis.com/auth/drive"]
            _sa = _json.load(open(sheets_sa, encoding="utf-8")) \
                  if isinstance(sheets_sa, (str, Path)) else dict(sheets_sa)
            _creds  = _Creds.from_service_account_info(_sa, scopes=_scopes)
            _client = _gspread.authorize(_creds)
            _sheet  = _client.open_by_key(sheets_id)
            try:
                _ws  = _sheet.worksheet("manual_matches")
                _mm  = pd.DataFrame(_ws.get_all_records(default_blank=""))
            except Exception:
                _mm  = pd.DataFrame()
            if not _mm.empty and "city_code" in _mm.columns:
                # Ultima riga vince per ogni coppia (city, glovo)
                _mm = _mm.drop_duplicates(subset=["city_code", "glovo_name"], keep="last")
                imported = 0
                for _, row in _mm.iterrows():
                    city         = str(row.get("city_code", "")).strip()
                    glovo_nm     = str(row.get("glovo_name", "")).strip()
                    deliveroo_nm = str(row.get("deliveroo_name", "")).strip()
                    if city and glovo_nm:
                        if deliveroo_nm:
                            confirm_match(city, glovo_nm, deliveroo_nm)
                        else:
                            reject_match(city, glovo_nm)
                        imported += 1
                print(f"[store_matcher] {imported} manual_matches importati da Sheets")
        except Exception as e:
            print(f"[store_matcher] Avviso: import manual_matches fallito ({e})")

    # ---- 4. Store matching ----
    print(f"\n[4/5] Store matching Glovo <-> Deliveroo")
    glovo_tuples = [
        (str(r["city_code"]), str(r["store_name"]), "")
        for _, r in glovo_store.iterrows()
    ]
    deliveroo_tuples = [
        (str(r["city_code"]), str(r["restaurant_name"]))
        for _, r in deliveroo_df.iterrows()
        if r.get("restaurant_name")
    ]
    match_map = match_glovo_stores(glovo_tuples, deliveroo_tuples)

    matched_count = sum(1 for v in match_map.values() if v)
    print(f"      {matched_count}/{len(glovo_tuples)} store matchati "
          f"({round(matched_count/len(glovo_tuples)*100,1) if glovo_tuples else 0}%)")

    # ---- 5. Calcola parity ----
    print(f"\n[5/5] Calcolo parity")
    store_parity = compute_store_parity(glovo_store, deliveroo_df, match_map)
    city_parity  = compute_city_parity(store_parity)

    sup  = (store_parity["parity"] == "SUPERIORITY").sum()
    par  = (store_parity["parity"] == "PARITY").sum()
    inf  = (store_parity["parity"] == "INFERIORITY").sum()
    unm  = (store_parity["parity"] == "UNMATCHED").sum()
    print(f"      SUPERIORITY={sup}  PARITY={par}  INFERIORITY={inf}  UNMATCHED={unm}")

    # ---- 5b. Calcola parity Prime (prime-first) ----
    store_parity_prime = compute_store_parity(glovo_store_prime, deliveroo_df, match_map)
    city_parity_prime  = compute_city_parity(store_parity_prime)

    sup_p = (store_parity_prime["parity"] == "SUPERIORITY").sum()
    par_p = (store_parity_prime["parity"] == "PARITY").sum()
    inf_p = (store_parity_prime["parity"] == "INFERIORITY").sum()
    print(f"      [Prime] SUPERIORITY={sup_p}  PARITY={par_p}  INFERIORITY={inf_p}")

    # ---- Prepara glovo_products (solo colonne utili, solo store in parity) ----
    known_stores = set(zip(store_parity["city_code"], store_parity["glovo_name"]))
    gp_cols = ["city_code", "store_name", "week_num", "product_name",
               "type_of_promo", "has_active_promo",
               "avg_percentage_off", "avg_unit_price", "total_product_sold"]
    gp_cols_present = [c for c in gp_cols if c in glovo_raw.columns]
    glovo_products = glovo_raw[gp_cols_present].copy()
    glovo_products = glovo_products[
        glovo_products.apply(
            lambda r: (r["city_code"], r["store_name"]) in known_stores, axis=1
        )
    ]

    # ---- Prepara glovo_products_prime (colonne np + p affiancate, solo W20+) ----
    if has_prime_data:
        gpp_cols = ["city_code", "store_name", "week_num", "product_name",
                    "type_of_promo_np", "promo_non_prime",
                    "percentage_off_np",
                    "type_of_promo_p", "promotion_prime",
                    "percentage_off_p",
                    "avg_unit_price", "total_product_sold"]
        gpp_cols_present = [c for c in gpp_cols if c in glovo_raw.columns]
        glovo_products_prime = glovo_raw[gpp_cols_present].copy()
        glovo_products_prime = glovo_products_prime[
            glovo_products_prime.apply(
                lambda r: (r["city_code"], r["store_name"]) in known_stores, axis=1
            )
        ]
        glovo_products_prime = glovo_products_prime.rename(columns={
            "promo_non_prime":  "has_active_promo_np",
            "percentage_off_np": "avg_percentage_off_np",
            "promotion_prime":  "has_active_promo_p",
            "percentage_off_p": "avg_percentage_off_p",
        })
    else:
        glovo_products_prime = pd.DataFrame()

    # ---- Salva nel DB ----
    conn = get_connection(db_path)
    init_db(conn)
    n1 = upsert_df(conn, "store_parity",         store_parity)
    n2 = upsert_df(conn, "city_parity",          city_parity)
    n3 = upsert_df(conn, "glovo_products",       glovo_products)
    n4 = upsert_df(conn, "store_parity_prime",   store_parity_prime)
    n5 = upsert_df(conn, "city_parity_prime",    city_parity_prime)
    n6 = upsert_df(conn, "glovo_products_prime", glovo_products_prime) if not glovo_products_prime.empty else 0
    conn.close()
    print(f"\n[DB] {n1} store_parity | {n2} city_parity | {n3} glovo_products -> {db_path}")
    print(f"[DB] {n4} store_parity_prime | {n5} city_parity_prime | {n6} glovo_products_prime (vista Prime)")

    # ---- Salva CSV settimanali ----
    if save_csv:
        WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
        store_path       = WEEKLY_DIR / f"store_parity_{week}.csv"
        city_path        = WEEKLY_DIR / f"city_parity_{week}.csv"
        store_prime_path = WEEKLY_DIR / f"store_parity_prime_{week}.csv"
        city_prime_path  = WEEKLY_DIR / f"city_parity_prime_{week}.csv"
        store_parity.to_csv(store_path,             index=False, encoding="utf-8")
        city_parity.to_csv(city_path,               index=False, encoding="utf-8")
        store_parity_prime.to_csv(store_prime_path, index=False, encoding="utf-8")
        city_parity_prime.to_csv(city_prime_path,   index=False, encoding="utf-8")
        print(f"[CSV] Salvati: {store_path.name} | {city_path.name}")
        print(f"[CSV] Salvati: {store_prime_path.name} | {city_prime_path.name}")

    # ---- Export su Google Sheets (opzionale) ----
    if sheets_id and sheets_sa:
        print(f"\n[GSheets] Export su Google Sheets...")
        try:
            from pipeline.sheets_writer import export_to_sheets
            from pipeline.store_matcher import load_mapping, load_review_queue
            mapping_df = load_mapping()
            review_df  = load_review_queue()
            # Carica prodotti Deliveroo dal file raw (product-level, non deduped)
            if DELIVEROO_PRODUCTS.exists():
                dp_cols = ["city_code", "restaurant_name", "product_name",
                           "product_description", "product_price", "promotion_type"]
                deliveroo_products_raw = pd.read_csv(DELIVEROO_PRODUCTS, dtype=str).fillna("")
                dp_cols_present = [c for c in dp_cols if c in deliveroo_products_raw.columns]
                deliveroo_products = deliveroo_products_raw[dp_cols_present] if dp_cols_present else None
            else:
                deliveroo_products = None
            export_to_sheets(
                spreadsheet_id       = sheets_id,
                service_account_info = sheets_sa,
                store_parity         = store_parity,
                city_parity          = city_parity,
                store_mapping        = mapping_df            if len(mapping_df) > 0            else None,
                needs_review         = review_df             if len(review_df)  > 0            else None,
                glovo_products       = glovo_products        if len(glovo_products) > 0        else None,
                deliveroo_products   = deliveroo_products,
                store_parity_prime   = store_parity_prime    if len(store_parity_prime) > 0    else None,
                city_parity_prime    = city_parity_prime     if len(city_parity_prime)  > 0    else None,
            )
            print(f"[GSheets] Export completato")
        except Exception as e:
            print(f"[GSheets] ERRORE export: {e}")

    print(f"\n{'='*60}")
    print(f" Pipeline completata: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    return store_parity, city_parity


# ---------------------------------------------------------------------------
# Entrypoint CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Pipeline settimanale Promo Parity Glovo vs Deliveroo"
    )
    parser.add_argument(
        "--glovo-csv", required=True,
        help="Percorso al CSV Glovo esportato da Google Sheets"
    )
    parser.add_argument(
        "--week", default=None,
        help="Settimana da processare (es. 2026-W20). Default: settimana corrente"
    )
    parser.add_argument(
        "--stores-csv", default=str(STORES_CSV),
        help=f"Percorso a Stores.csv (default: {STORES_CSV})"
    )
    parser.add_argument(
        "--no-csv", action="store_true",
        help="Non salvare i CSV settimanali (solo DB)"
    )
    parser.add_argument(
        "--sheets-id", default=None,
        help="ID Google Sheet di output per export cloud (opzionale)"
    )
    parser.add_argument(
        "--sheets-sa", default=None,
        help="Percorso al file JSON service account Google (opzionale)"
    )

    args = parser.parse_args()
    run_pipeline(
        glovo_csv  = args.glovo_csv,
        week_num   = args.week,
        stores_csv = args.stores_csv,
        save_csv   = not args.no_csv,
        sheets_id  = args.sheets_id,
        sheets_sa  = args.sheets_sa,
    )


if __name__ == "__main__":
    main()
