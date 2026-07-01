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

    # ---- Migrazioni strutturali (aggiunge colonne nuove senza ricreare il DB) ----
    _migrations = [
        ("city_parity",       "n_exclusive_glovo",  "INTEGER DEFAULT 0"),
        ("city_parity_prime", "n_exclusive_glovo",  "INTEGER DEFAULT 0"),
        ("store_parity",      "deliveroo_pct_off",  "REAL"),
        ("store_parity_prime","deliveroo_pct_off",  "REAL"),
        ("glovo_products",    "min_basket_size_np",  "REAL"),
        ("glovo_products",    "min_basket_size_p",   "REAL"),
        ("store_parity",      "glovo_min_basket",    "REAL"),
        ("store_parity",      "deliveroo_min_basket","REAL"),
        ("store_parity_prime","glovo_min_basket",    "REAL"),
        ("store_parity_prime","deliveroo_min_basket","REAL"),
        ("store_parity",      "deliveroo_stores_pct",  "REAL"),
        ("store_parity",      "deliveroo_stores_frac", "TEXT"),
        ("store_parity_prime","deliveroo_stores_pct",  "REAL"),
        ("store_parity_prime","deliveroo_stores_frac", "TEXT"),
        ("store_parity",      "parity_basis", "TEXT DEFAULT 'store'"),
        ("store_parity_prime","parity_basis", "TEXT DEFAULT 'store'"),
    ]
    for _tbl, _col, _typedef in _migrations:
        try:
            conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_typedef}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # colonna già esistente


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
                from pipeline.store_matcher import mark_not_on_deliveroo, set_matches
                # Ricostruisce lo stato finale per (city, glovo) rispettando l'ordine di
                # append: un negativo (esclusiva / non-su-Deliveroo) resetta lo store;
                # i positivi si ACCUMULANO -> matching 1:N.
                _state: dict[tuple[str, str], dict] = {}
                for _, row in _mm.iterrows():
                    city         = str(row.get("city_code", "")).strip()
                    glovo_nm     = str(row.get("glovo_name", "")).strip()
                    if not city or not glovo_nm:
                        continue
                    deliveroo_nm = str(row.get("deliveroo_name", "")).strip()
                    src          = str(row.get("source", "")).strip().lower()
                    k = (city, glovo_nm)
                    if deliveroo_nm:
                        s = _state.get(k)
                        if not s or s["mode"] == "neg":
                            _state[k] = {"mode": "pos", "names": [deliveroo_nm], "neg": ""}
                        elif deliveroo_nm not in s["names"]:
                            s["names"].append(deliveroo_nm)
                    else:
                        _neg = "not_deliveroo" if ("not_deliveroo" in src or "not_on_deliveroo" in src) else "exclusive"
                        _state[k] = {"mode": "neg", "names": [], "neg": _neg}

                imported = 0
                for (city, glovo_nm), s in _state.items():
                    if s["mode"] == "pos":
                        set_matches(city, glovo_nm, s["names"])
                    elif s["neg"] == "not_deliveroo":
                        mark_not_on_deliveroo(city, glovo_nm)
                    else:
                        reject_match(city, glovo_nm)
                    imported += 1
                print(f"[store_matcher] {imported} manual_matches importati da Sheets (1:N)")
        except Exception as e:
            print(f"[store_matcher] Avviso: import manual_matches fallito ({e})")

    # ---- 3b. Scarica AM mapping dal foglio Glovo sorgente ----
    _GLOVO_SOURCE_SHEET_ID = "1ah5GsEJaSnv-S8jYytar3Vn9tU8MD8IITfNAWtmtveE"
    am_mapping_df = pd.DataFrame()
    _am_mapping_path = ROOT / "data" / "am_mapping.csv"
    if sheets_id and sheets_sa:
        try:
            import json as _json2
            import gspread as _gs2
            from google.oauth2.service_account import Credentials as _Creds2
            _scopes2 = ["https://spreadsheets.google.com/feeds",
                        "https://www.googleapis.com/auth/drive"]
            _sa2 = _json2.load(open(sheets_sa, encoding="utf-8")) \
                   if isinstance(sheets_sa, (str, Path)) else dict(sheets_sa)
            _creds2  = _Creds2.from_service_account_info(_sa2, scopes=_scopes2)
            _client2 = _gs2.authorize(_creds2)
            _glovo_sh = _client2.open_by_key(_GLOVO_SOURCE_SHEET_ID)
            _am_ws    = _glovo_sh.worksheet("Mapping")
            _am_data  = _am_ws.get_all_records(default_blank="")
            if _am_data:
                am_mapping_df = pd.DataFrame(_am_data)
                am_mapping_df.columns = [c.strip().lower().replace(" ", "_")
                                          for c in am_mapping_df.columns]
                # Mantieni solo le colonne utili
                _am_cols = ["city_code", "store_name", "sf_registered_am", "region"]
                am_mapping_df = am_mapping_df[[c for c in _am_cols if c in am_mapping_df.columns]]
                am_mapping_df = am_mapping_df.drop_duplicates(subset=["city_code", "store_name"])
                am_mapping_df.to_csv(_am_mapping_path, index=False, encoding="utf-8-sig")
                print(f"[AM] Mapping scaricato: {len(am_mapping_df)} store → {_am_mapping_path.name}")
        except Exception as _ame:
            print(f"[AM] Avviso: download mapping AM fallito ({_ame})")
    if am_mapping_df.empty and _am_mapping_path.exists():
        am_mapping_df = pd.read_csv(_am_mapping_path, dtype=str).fillna("")
        print(f"[AM] Mapping caricato da file locale: {len(am_mapping_df)} store")

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

    # ---- 4b. Store discovery: recupera match mancati via fingerprint del menu ----
    # Colleghi Deliveroo NON mappati al loro store Glovo confrontando i prodotti
    # (nome+prezzo). Auto-merge solo altissima confidenza; il resto va in revisione.
    try:
        from pipeline.store_discovery import auto_merge as _sd_auto_merge
        for _c, _g, _d in _sd_auto_merge():
            _k = (str(_c).strip(), str(_g).strip())
            cur = match_map.get(_k)
            if isinstance(cur, list):
                if _d not in cur:
                    cur.append(_d)
            elif cur:
                match_map[_k] = [cur, _d]
            else:
                match_map[_k] = [_d]
    except Exception as _sde:
        print(f"      [store_discovery] saltato: {_sde}")

    # ---- 5. Calcola parity ----
    print(f"\n[5/5] Calcolo parity")

    # Costruisce i set degli store senza match Deliveroo, distinti per tipo
    _mapping_df = load_mapping()
    exclusive_glovo_set:  set[tuple[str, str]] = set()  # accordo commerciale
    not_on_deliveroo_set: set[tuple[str, str]] = set()  # scelta indipendente del partner
    if not _mapping_df.empty and "source" in _mapping_df.columns:
        _no_deliv = _mapping_df["deliveroo_name"].fillna("").str.strip() == ""
        _excl_rows = _mapping_df[(_mapping_df["source"] == "manual_rejected") & _no_deliv]
        _nod_rows  = _mapping_df[(_mapping_df["source"] == "not_on_deliveroo") & _no_deliv]
        exclusive_glovo_set  = set(zip(_excl_rows["city_code"].str.strip(), _excl_rows["glovo_name"].str.strip()))
        not_on_deliveroo_set = set(zip(_nod_rows["city_code"].str.strip(),  _nod_rows["glovo_name"].str.strip()))
    # Uno store con almeno un match Deliveroo (1:N) non e' esclusiva ne' non-su-Deliveroo
    _matched_keys = {(str(c).strip(), str(g).strip()) for (c, g), v in match_map.items() if v}
    exclusive_glovo_set  -= _matched_keys
    not_on_deliveroo_set -= _matched_keys
    print(f"      Exclusive Glovo: {len(exclusive_glovo_set)} | Non su Deliveroo: {len(not_on_deliveroo_set)} store")

    store_parity = compute_store_parity(glovo_store, deliveroo_df, match_map, exclusive_glovo_set, not_on_deliveroo_set)

    # ---- 5a. Product-parity: dove TUTTI i promo Deliveroo sono nel nostro menu (matchati
    # al 100%, >=3), il verdetto per-prodotto SOSTITUISCE quello store-level. Altrove
    # (o categorie strutturali) resta lo store-level. Colonna parity_basis = product/store.
    store_parity["parity_basis"] = "store"
    try:
        from pipeline.product_matcher import build_matches as _pm_build
        _pp = _pm_build(glovo_df=glovo_raw)[1]
        if _pp is not None and len(_pp) > 0:
            _r = {"INFERIORITY": 0, "PARITY": 1, "SUPERIORITY": 2}   # 1:N -> tieni il peggiore per Glovo
            _pp = _pp.assign(_r=_pp["parity_product"].map(_r)).sort_values("_r")
            _pv = {(str(c).strip(), str(gn).strip()): v
                   for (c, gn), v in _pp.groupby(["city_code", "glovo_name"]).first()["parity_product"].items()}
            _STRUCT = {"SUPERIORITY", "PARITY", "INFERIORITY"}

            def _apply_pp(row):
                pv = _pv.get((str(row["city_code"]).strip(), str(row["glovo_name"]).strip()))
                if pv and row["parity"] in _STRUCT:
                    return pd.Series([pv, "product"])
                return pd.Series([row["parity"], "store"])
            store_parity[["parity", "parity_basis"]] = store_parity.apply(_apply_pp, axis=1)
            print(f"      product-parity applicato a {(store_parity['parity_basis'] == 'product').sum()} store "
                  f"(100% promo Deliveroo nel menu Glovo)")
    except Exception as _ppe:
        print(f"      [product-parity] saltato: {_ppe}")

    city_parity  = compute_city_parity(store_parity)

    sup  = (store_parity["parity"] == "SUPERIORITY").sum()
    par  = (store_parity["parity"] == "PARITY").sum()
    inf  = (store_parity["parity"] == "INFERIORITY").sum()
    unm  = (store_parity["parity"] == "UNMATCHED").sum()
    excl = (store_parity["parity"] == "EXCLUSIVE_GLOVO").sum()
    nod  = (store_parity["parity"] == "NOT_ON_DELIVEROO").sum()
    print(f"      SUPERIORITY={sup}  PARITY={par}  INFERIORITY={inf}  UNMATCHED={unm}  EXCLUSIVE_GLOVO={excl}  NOT_ON_DELIVEROO={nod}")

    # ---- 5b. Calcola parity Prime (prime-first) ----
    store_parity_prime = compute_store_parity(glovo_store_prime, deliveroo_df, match_map, exclusive_glovo_set, not_on_deliveroo_set)
    store_parity_prime["parity_basis"] = "store"   # il product-parity si applica alla vista standard
    city_parity_prime  = compute_city_parity(store_parity_prime)

    sup_p = (store_parity_prime["parity"] == "SUPERIORITY").sum()
    par_p = (store_parity_prime["parity"] == "PARITY").sum()
    inf_p = (store_parity_prime["parity"] == "INFERIORITY").sum()
    print(f"      [Prime] SUPERIORITY={sup_p}  PARITY={par_p}  INFERIORITY={inf_p}")

    # ---- Prepara glovo_products (solo colonne utili, solo store in parity) ----
    known_stores = set(zip(store_parity["city_code"], store_parity["glovo_name"]))
    gp_cols = ["city_code", "store_name", "week_num", "product_name",
               "type_of_promo", "has_active_promo",
               "avg_percentage_off", "avg_unit_price", "total_product_sold",
               "min_basket_size_np", "min_basket_size_p"]
    gp_cols_present = [c for c in gp_cols if c in glovo_raw.columns]
    glovo_products = glovo_raw[gp_cols_present].copy()
    glovo_products = glovo_products[
        glovo_products.apply(
            lambda r: (r["city_code"], r["store_name"]) in known_stores, axis=1
        )
    ]

    # Per il tab Sheets: solo prodotti con promo attiva per restare entro il
    # limite di risposta delle API (get_all_records tronca oltre ~185k righe).
    # Il DB locale mantiene tutti i prodotti (nessun limite SQLite).
    glovo_products_sheets = glovo_products[
        glovo_products.get("has_active_promo", pd.Series(dtype=str)).str.upper() == "Y"
    ].copy() if "has_active_promo" in glovo_products.columns else glovo_products.copy()

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
    # Replace-by-week per store_parity/_prime: cancella le righe della settimana prima
    # di reinserirle, cosi' un ri-run non lascia residui. Serve perche' un NOT_ON_GLOVO
    # che diventa matchato ha chiave (glovo_name) diversa e non verrebbe sovrascritto
    # dal solo upsert -> resterebbe come duplicato stantio.
    for _tbl, _df in (("store_parity", store_parity), ("store_parity_prime", store_parity_prime)):
        if _df is not None and not _df.empty and "week_num" in _df.columns:
            for _wk in _df["week_num"].dropna().astype(str).unique():
                conn.execute(f"DELETE FROM {_tbl} WHERE week_num = ?", (_wk,))
    conn.commit()
    n1 = upsert_df(conn, "store_parity",         store_parity)
    n2 = upsert_df(conn, "city_parity",          city_parity)
    n3 = upsert_df(conn, "glovo_products",       glovo_products)
    n4 = upsert_df(conn, "store_parity_prime",   store_parity_prime)
    n5 = upsert_df(conn, "city_parity_prime",    city_parity_prime)
    n6 = upsert_df(conn, "glovo_products_prime", glovo_products_prime) if not glovo_products_prime.empty else 0
    conn.close()
    print(f"\n[DB] {n1} store_parity | {n2} city_parity | {n3} glovo_products -> {db_path}")
    print(f"[DB] {n4} store_parity_prime | {n5} city_parity_prime | {n6} glovo_products_prime (vista Prime)")

    # ---- Quality checks automatici ----
    print(f"\n[QC] Avvio quality checks...")
    try:
        from pipeline.data_quality import run_quality_checks
        quality_report = run_quality_checks(
            store_parity      = store_parity,
            deliveroo_df      = deliveroo_df,
            week_num          = week,
            weekly_dir        = WEEKLY_DIR,
            deliveroo_csv_path= DELIVEROO_DEDUPED,
        )
    except Exception as _qc_err:
        print(f"[QC] ERRORE quality checks: {_qc_err}")
        quality_report = None

    # ---- Scrivi report leggibile da run_friday.ps1 per email ----
    REPORT_PATH = ROOT / "data" / "last_pipeline_report.txt"
    if quality_report is not None:
        try:
            lines = [quality_report.summary_text(), ""]
            pa = quality_report.priority_actions
            if not pa.empty:
                lines.append("── Top 5 azioni prioritarie ──")
                top5 = pa.head(5)
                for _, r in top5.iterrows():
                    rev   = f"€{float(r.get('revenue',0)):,.0f}" if r.get('revenue') else "n/d"
                    g_pct = r.get('glovo_pct_off', '')
                    d_pct = r.get('deliveroo_pct_off', '')
                    gap   = f"  ({g_pct}% Glovo vs {d_pct}% Deliveroo)" if g_pct and d_pct else ""
                    lines.append(f"  {int(r.get('priority',0))}. [{r.get('city_code','')}] "
                                 f"{r.get('glovo_name','')} — {rev}{gap}")
            REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
        except Exception as _rp_err:
            print(f"[QC] Impossibile scrivere report file: {_rp_err}")

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

        if quality_report is not None:
            if not quality_report.priority_actions.empty:
                pa_path = WEEKLY_DIR / f"priority_actions_{week}.csv"
                quality_report.priority_actions.to_csv(pa_path, index=False, encoding="utf-8")
                print(f"[CSV] Salvato: {pa_path.name}")
            ph_path = WEEKLY_DIR / f"pipeline_health_{week}.csv"
            quality_report.to_dataframe().to_csv(ph_path, index=False, encoding="utf-8")
            print(f"[CSV] Salvato: {ph_path.name}")

    # ---- Export su Google Sheets (opzionale) ----
    if sheets_id and sheets_sa:
        print(f"\n[GSheets] Export su Google Sheets...")
        try:
            from pipeline.sheets_writer import export_to_sheets
            from pipeline.store_matcher import load_review_queue
            mapping_df = load_mapping()
            review_df  = load_review_queue()
            # Carica prodotti Deliveroo dal file raw (product-level, non deduped)
            if DELIVEROO_PRODUCTS.exists():
                deliveroo_products_raw = pd.read_csv(DELIVEROO_PRODUCTS, dtype=str).fillna("")
                # Deriva week_num da scraped_at_utc se non presente
                if "week_num" not in deliveroo_products_raw.columns:
                    if "scraped_at_utc" in deliveroo_products_raw.columns:
                        def _ts_to_week(ts):
                            try:
                                dt = pd.to_datetime(ts, utc=True)
                                iso = dt.isocalendar()
                                return f"{iso[0]}-W{int(iso[1]):02d}"
                            except Exception:
                                return week
                        deliveroo_products_raw["week_num"] = deliveroo_products_raw["scraped_at_utc"].apply(_ts_to_week)
                    else:
                        deliveroo_products_raw["week_num"] = week
                dp_cols = ["city_code", "restaurant_name", "week_num", "product_name",
                           "product_description", "product_price", "promotion_type"]
                dp_cols_present = [c for c in dp_cols if c in deliveroo_products_raw.columns]
                deliveroo_products = deliveroo_products_raw[dp_cols_present] if dp_cols_present else None
                # Dedup: le catene hanno N filiali con lo STESSO menu -> righe prodotto
                # duplicate (16 filiali x 3 prodotti = 48). Teniamo 1 riga per
                # (citta', store, settimana, prodotto) -> conteggi corretti + meno celle.
                if deliveroo_products is not None:
                    _dk = [c for c in ["city_code", "restaurant_name", "week_num", "product_name"]
                           if c in deliveroo_products.columns]
                    if _dk:
                        _n0 = len(deliveroo_products)
                        deliveroo_products = deliveroo_products.drop_duplicates(_dk, keep="first")
                        print(f"    [deliveroo_products] dedup filiali: {_n0} -> {len(deliveroo_products)} righe")
            else:
                deliveroo_products = None
            _priority_df = quality_report.priority_actions if quality_report is not None else None
            _health_df   = quality_report.to_dataframe()   if quality_report is not None else None

            sheets_result = export_to_sheets(
                spreadsheet_id        = sheets_id,
                service_account_info  = sheets_sa,
                store_parity          = store_parity,
                city_parity           = city_parity,
                store_mapping         = mapping_df             if len(mapping_df) > 0              else None,
                needs_review          = review_df              if len(review_df)  > 0              else None,
                glovo_products        = glovo_products_sheets  if len(glovo_products_sheets) > 0   else None,
                deliveroo_products    = deliveroo_products,
                store_parity_prime    = store_parity_prime     if len(store_parity_prime) > 0      else None,
                city_parity_prime     = city_parity_prime      if len(city_parity_prime)  > 0      else None,
                glovo_products_prime  = glovo_products_prime   if not glovo_products_prime.empty   else None,
                priority_actions      = _priority_df           if _priority_df is not None and len(_priority_df) > 0 else None,
                pipeline_health       = _health_df             if _health_df   is not None and len(_health_df)   > 0 else None,
                am_mapping            = am_mapping_df          if not am_mapping_df.empty else None,
            )
            print(f"[GSheets] Export completato")

            # ---- Check 6: integrità glovo_products su Sheets ----
            if quality_report is not None:
                from pipeline.data_quality import check_sheets_products_integrity
                check_sheets_products_integrity(
                    sheets_result  = sheets_result,
                    expected_rows  = len(glovo_products_sheets),
                    report         = quality_report,
                )
                # Aggiorna pipeline_health su Sheets con il nuovo issue
                _updated_health = quality_report.to_dataframe()
                from pipeline.sheets_writer import export_to_sheets as _exp
                try:
                    _exp(
                        spreadsheet_id       = sheets_id,
                        service_account_info = sheets_sa,
                        pipeline_health      = _updated_health,
                    )
                except Exception as _he:
                    print(f"[QC] Impossibile aggiornare pipeline_health: {_he}")

            # ---- Auto-repair tab falliti ----
            failed_tabs = [t for t, n in sheets_result.items() if n == -1]
            if failed_tabs:
                print(f"[GSheets] {len(failed_tabs)} tab falliti, avvio auto-repair: {failed_tabs}")
                try:
                    from pipeline.sheets_repair import repair_tab, TAB_SOURCES
                    from pipeline.sheets_writer import _get_client
                    _client_r = _get_client(sheets_sa)
                    _sh_r     = _client_r.open_by_key(sheets_id)
                    for _tab in failed_tabs:
                        if _tab in TAB_SOURCES:
                            _csvs = TAB_SOURCES[_tab]([week])
                            repair_tab(_sh_r, _tab, _csvs)
                        else:
                            print(f"[auto-repair] Tab '{_tab}' non in TAB_SOURCES, skip.")
                except Exception as _re:
                    print(f"[auto-repair] ERRORE: {_re}")
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
