"""
app.py  —  Promo Parity Dashboard
Streamlit app per analisi settimanale Glovo vs Deliveroo.

Modalita':
  LOCALE  — legge da SQLite (data/promo_parity.db) e CSV locali
  CLOUD   — legge da Google Sheets (configurato in .streamlit/secrets.toml)

Avvio locale:
    .venv\Scripts\streamlit run app.py

Tab:
  1. City Parity   — heatmap settimana x citta'
  2. Store Detail  — drill-down per store
  3. Trend         — evoluzione parity nel tempo
  4. Store Matching — validazione match automatici
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


# ---------------------------------------------------------------------------
# Helper difensivo globale — usato ovunque al posto di df["col"]
# ---------------------------------------------------------------------------

def _col(df: pd.DataFrame, col: str, default: Any = "") -> "pd.Series":
    """
    Ritorna df[col] se la colonna esiste, altrimenti una Series piena di `default`.
    Previene KeyError quando le colonne del DataFrame non matchano quelle attese.
    """
    if col in df.columns:
        return df[col]
    return pd.Series(default, index=df.index, dtype=object)


def _safe_flags(df: pd.DataFrame, col: str, value: Any = "Y") -> "pd.Series[bool]":
    """
    Ritorna una Series booleana: True dove df[col] == value.
    Se la colonna non esiste, ritorna tutta False (nessun highlight, nessun crash).
    """
    if col in df.columns:
        return df[col].fillna("").astype(str).str.upper() == str(value).upper()
    return pd.Series(False, index=df.index)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT    = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "promo_parity.db"

PARITY_COLORS = {
    "SUPERIORITY": "#00A082",   # teal Glovo
    "PARITY":          "#F2CC38",   # giallo Glovo
    "INFERIORITY":     "#ef4444",   # rosso
    "UNMATCHED":       "#94a3b8",   # grigio
    "EXCLUSIVE_GLOVO": "#8b5cf6",   # viola
}
PARITY_ORDER = ["SUPERIORITY", "PARITY", "INFERIORITY", "UNMATCHED", "EXCLUSIVE_GLOVO"]


def _col_config_from_data(
    df: pd.DataFrame,
    px_per_char: float = 9.0,
    min_px: int = 55,
    max_px: int = 380,
) -> dict:
    """
    Genera column_config con larghezza basata sul contenuto dei dati (non degli header).
    L'header va a capo automaticamente se più largo della colonna.
    """
    cfg = {}
    for col in df.columns:
        if df.empty:
            max_len = 4
        else:
            max_len = int(df[col].astype(str).str.len().max())
        width = int(min(max(max_len * px_per_char + 20, min_px), max_px))
        cfg[col] = st.column_config.TextColumn(width=width)
    return cfg


st.set_page_config(
    page_title="Promo Parity — Glovo vs Deliveroo",
    page_icon="📊",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Password protection
# ---------------------------------------------------------------------------

def check_password() -> bool:
    """Mostra la schermata di login. Restituisce True se autenticato."""
    if st.session_state.get("authenticated"):
        return True

    import base64 as _b64mod
    _glovo_logo = ROOT / "assets" / "glovo.png"
    _roo_logo   = ROOT / "assets" / "roo.png"
    _b64_g = _b64mod.b64encode(_glovo_logo.read_bytes()).decode() if _glovo_logo.exists() else ""
    _b64_r = _b64mod.b64encode(_roo_logo.read_bytes()).decode()   if _roo_logo.exists()   else ""

    _logos_html = ""
    if _b64_g:
        _logos_html += f"<img src='data:image/png;base64,{_b64_g}' style='height:48px;width:48px;object-fit:contain'>"
    if _b64_g and _b64_r:
        _logos_html += "<span style='font-size:1.4rem;color:#cbd5e1;margin:0 10px'>×</span>"
    if _b64_r:
        _logos_html += f"<img src='data:image/png;base64,{_b64_r}' style='height:48px;width:48px;object-fit:contain'>"

    st.markdown(f"""
        <div style='display:flex;flex-direction:column;align-items:center;
                    justify-content:center;padding:80px 0 40px'>
            <div style='display:flex;align-items:center;gap:8px;margin-bottom:12px'>
                {_logos_html}
            </div>
            <h1 style='font-size:2.2rem;margin-bottom:4px'>Promo Parity</h1>
            <p style='color:#94a3b8;margin-bottom:40px'>Glovo vs Deliveroo</p>
        </div>
    """, unsafe_allow_html=True)

    col = st.columns([1, 2, 1])[1]
    with col:
        pwd = st.text_input("Password", type="password", placeholder="Inserisci la password")
        if st.button("Accedi", use_container_width=True, type="primary"):
            correct = st.secrets.get("app_password", "")
            if pwd == correct:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Password errata")
    return False

# ---------------------------------------------------------------------------
# Rilevamento modalita' (locale vs cloud)
# ---------------------------------------------------------------------------

def _is_cloud_mode() -> bool:
    """True se siamo su Streamlit Cloud (secrets configurati)."""
    try:
        return (
            "gcp_service_account" in st.secrets
            and "output_sheet_id" in st.secrets
        )
    except Exception:
        return False


def _get_service_account() -> dict:
    return dict(st.secrets["gcp_service_account"])


def _get_sheet_id() -> str:
    return st.secrets["output_sheet_id"]


# ---------------------------------------------------------------------------
# Data loading — LOCALE (SQLite + CSV)
# ---------------------------------------------------------------------------

@st.cache_resource
def _get_sqlite_conn():
    if not DB_PATH.exists():
        return None
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def _local_store_parity() -> pd.DataFrame:
    conn = _get_sqlite_conn()
    if conn is None:
        return pd.DataFrame()
    return pd.read_sql("SELECT * FROM store_parity ORDER BY week_num, city_code", conn)


def _local_city_parity() -> pd.DataFrame:
    conn = _get_sqlite_conn()
    if conn is None:
        return pd.DataFrame()
    return pd.read_sql("SELECT * FROM city_parity ORDER BY week_num, city_code", conn)


def _local_store_parity_prime() -> pd.DataFrame:
    conn = _get_sqlite_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        return pd.read_sql("SELECT * FROM store_parity_prime ORDER BY week_num, city_code", conn)
    except Exception:
        return pd.DataFrame()


def _local_city_parity_prime() -> pd.DataFrame:
    conn = _get_sqlite_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        return pd.read_sql("SELECT * FROM city_parity_prime ORDER BY week_num, city_code", conn)
    except Exception:
        return pd.DataFrame()


def _local_review_queue() -> pd.DataFrame:
    p = ROOT / "data" / "needs_review.csv"
    if not p.exists():
        return pd.DataFrame(columns=["city_code","glovo_name","glovo_store_id",
                                     "candidate_deliveroo","score","reason"])
    return pd.read_csv(p, dtype=str).fillna("")


def _local_store_mapping() -> pd.DataFrame:
    p = ROOT / "data" / "store_mapping.csv"
    if not p.exists():
        return pd.DataFrame()
    return pd.read_csv(p, dtype=str).fillna("")


def _local_unmatched() -> pd.DataFrame:
    conn = _get_sqlite_conn()
    if conn is None:
        return pd.DataFrame()
    return pd.read_sql("""
        SELECT city_code, glovo_name, revenue, week_num
        FROM store_parity
        WHERE parity = 'UNMATCHED'
          AND week_num = (SELECT MAX(week_num) FROM store_parity)
        ORDER BY city_code, revenue DESC
    """, conn)


def _local_glovo_products(city_code: str, store_name: str, week_num: str) -> pd.DataFrame:
    conn = _get_sqlite_conn()
    if conn is None:
        return pd.DataFrame()
    return pd.read_sql(
        """SELECT product_name, type_of_promo, has_active_promo,
                  avg_percentage_off, avg_unit_price, total_product_sold
           FROM glovo_products
           WHERE city_code=? AND store_name=? AND week_num=?
           ORDER BY has_active_promo DESC, avg_unit_price DESC""",
        conn, params=(city_code, store_name, week_num),
    )


def _local_glovo_products_prime(city_code: str, store_name: str, week_num: str) -> pd.DataFrame:
    conn = _get_sqlite_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        return pd.read_sql(
            """SELECT product_name,
                      type_of_promo_np, has_active_promo_np, avg_percentage_off_np,
                      type_of_promo_p,  has_active_promo_p,  avg_percentage_off_p,
                      avg_unit_price, total_product_sold
               FROM glovo_products_prime
               WHERE city_code=? AND store_name=? AND week_num=?
               ORDER BY has_active_promo_p DESC, has_active_promo_np DESC, avg_unit_price DESC""",
            conn, params=(city_code, store_name, week_num),
        )
    except Exception:
        return pd.DataFrame()


def _local_deliveroo_products_raw(city_code: str) -> pd.DataFrame:
    """Carica tutto il CSV prodotti Deliveroo per una città (filtraggio delegato a load_deliveroo_products)."""
    p = ROOT / "output" / "deliveroo_promo_products.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, dtype=str).fillna("")
    df.columns = [c.strip().lower() for c in df.columns]
    return df[df["city_code"] == city_code] if "city_code" in df.columns else df


def _local_deliveroo_products(city_code: str, restaurant_name: str) -> pd.DataFrame:
    df = _local_deliveroo_products_raw(city_code)
    if df.empty:
        return pd.DataFrame()
    mask = df["restaurant_name"] == restaurant_name
    cols = ["product_name", "product_description", "product_price", "promotion_type"]
    cols_present = [c for c in cols if c in df.columns]
    return df[mask][cols_present].drop_duplicates("product_name")


def _local_deliveroo_names() -> dict[str, list[str]]:
    p = ROOT / "output" / "deliveroo_promo_deduped.csv"
    if not p.exists():
        return {}
    df = pd.read_csv(p, dtype=str).fillna("")
    df.columns = [c.strip().lower() for c in df.columns]
    return {
        city: sorted(grp["restaurant_name"].dropna().unique().tolist())
        for city, grp in df.groupby("city_code")
    }


# ---------------------------------------------------------------------------
# Data loading — CLOUD (Google Sheets)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def _cloud_load_all() -> dict[str, pd.DataFrame]:
    from pipeline.sheets_reader import read_all
    return read_all(_get_sheet_id(), _get_service_account())


def _cloud_store_parity() -> pd.DataFrame:
    return _cloud_load_all().get("store_parity", pd.DataFrame())


def _cloud_city_parity() -> pd.DataFrame:
    return _cloud_load_all().get("city_parity", pd.DataFrame())


def _cloud_review_queue() -> pd.DataFrame:
    return _cloud_load_all().get("needs_review", pd.DataFrame())


def _cloud_store_mapping() -> pd.DataFrame:
    return _cloud_load_all().get("store_mapping", pd.DataFrame())


def _cloud_unmatched() -> pd.DataFrame:
    sp = _cloud_store_parity()
    if sp.empty or "parity" not in sp.columns:
        return pd.DataFrame()
    last_week = sp["week_num"].max()
    df = sp[(sp["parity"] == "UNMATCHED") & (sp["week_num"] == last_week)].copy()

    # Escludi store che sono gia' in store_mapping, sia matchati che
    # esplicitamente esclusi ("non su Deliveroo", deliveroo_name="")
    mapping = _cloud_load_all().get("store_mapping", pd.DataFrame())
    if not mapping.empty and "city_code" in mapping.columns:
        resolved_pairs = set(zip(mapping["city_code"], mapping["glovo_name"]))
        if resolved_pairs:
            df = df[~df.apply(
                lambda r: (r["city_code"], r["glovo_name"]) in resolved_pairs,
                axis=1,
            )]

    return df[["city_code","glovo_name","revenue","week_num"]].sort_values(
        ["city_code","revenue"], ascending=[True,False]
    )


def _cloud_glovo_products() -> pd.DataFrame:
    return _cloud_load_all().get("glovo_products", pd.DataFrame())

@st.cache_data(ttl=3600)
def _cloud_am_mapping() -> pd.DataFrame:
    """Legge il tab Mapping direttamente dal foglio sorgente Glovo."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        _GLOVO_SOURCE_ID = "1ah5GsEJaSnv-S8jYytar3Vn9tU8MD8IITfNAWtmtveE"
        sa_info = _get_service_account()
        creds   = Credentials.from_service_account_info(
            sa_info,
            scopes=["https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/drive"]
        )
        client  = gspread.authorize(creds)
        sh      = client.open_by_key(_GLOVO_SOURCE_ID)
        ws      = sh.worksheet("Mapping")
        data    = ws.get_all_records(default_blank="")
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        cols = ["city_code", "store_name", "sf_registered_am"]
        df = df[[c for c in cols if c in df.columns]].drop_duplicates()
        return df
    except Exception:
        return pd.DataFrame()


def _cloud_deliveroo_products() -> pd.DataFrame:
    return _cloud_load_all().get("deliveroo_products", pd.DataFrame())


def _cloud_store_parity_prime() -> pd.DataFrame:
    return _cloud_load_all().get("store_parity_prime", pd.DataFrame())


def _cloud_city_parity_prime() -> pd.DataFrame:
    return _cloud_load_all().get("city_parity_prime", pd.DataFrame())


def _cloud_deliveroo_names() -> dict[str, list[str]]:
    """Nel cloud usiamo i nomi Deliveroo gia' presenti nel store_parity."""
    sp = _cloud_store_parity()
    if sp.empty or "deliveroo_name" not in sp.columns:
        return {}
    result = {}
    for city, grp in sp[sp["deliveroo_name"] != ""].groupby("city_code"):
        result[city] = sorted(grp["deliveroo_name"].dropna().unique().tolist())
    return result


def _cloud_glovo_products_prime() -> pd.DataFrame:
    return _cloud_load_all().get("glovo_products_prime", pd.DataFrame())


def _cloud_priority_actions() -> pd.DataFrame:
    return _cloud_load_all().get("priority_actions", pd.DataFrame())


def _cloud_pipeline_health() -> pd.DataFrame:
    return _cloud_load_all().get("pipeline_health", pd.DataFrame())


def _local_priority_actions() -> pd.DataFrame:
    """Calcola live da store_parity SQLite: INFERIORITY ordinati per revenue."""
    conn = _get_sqlite_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        df = pd.read_sql(
            """SELECT city_code, glovo_name, deliveroo_name, parity,
                      glovo_rank_label, deliveroo_rank_label,
                      revenue, glovo_pct_off, deliveroo_pct_off, promo_coverage_pct, week_num
               FROM store_parity
               WHERE parity = 'INFERIORITY'
               ORDER BY week_num DESC, CAST(revenue AS REAL) DESC
               LIMIT 30""",
            conn,
        )
        df["action"]   = "Allinea promo Glovo a Deliveroo"
        df["priority"] = range(1, len(df) + 1)
        return df
    except Exception:
        return pd.DataFrame()


def _local_pipeline_health() -> pd.DataFrame:
    """In locale non abbiamo pipeline_health persistito: ritorna vuoto."""
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Facade: funzioni uniformi usate dall'app
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_am_mapping() -> pd.DataFrame:
    """Carica il mapping store → SF_registered_AM."""
    if _is_cloud_mode():
        return _cloud_am_mapping()
    # Locale: legge da data/am_mapping.csv
    _p = ROOT / "data" / "am_mapping.csv"
    if _p.exists():
        df = pd.read_csv(_p, dtype=str).fillna("")
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        return df
    return pd.DataFrame()


def _dedup_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rimuove colonne duplicate ereditabili da merge/Sheets. Applicato a tutti i load pubblici."""
    if df.columns.duplicated().any():
        return df.loc[:, ~df.columns.duplicated(keep="first")]
    return df


@st.cache_data(ttl=300)
def load_store_parity() -> pd.DataFrame:
    return _dedup_columns(_cloud_store_parity() if _is_cloud_mode() else _local_store_parity())


@st.cache_data(ttl=300)
def load_city_parity() -> pd.DataFrame:
    return _dedup_columns(_cloud_city_parity() if _is_cloud_mode() else _local_city_parity())


@st.cache_data(ttl=300)
def load_store_parity_prime() -> pd.DataFrame:
    return _dedup_columns(_cloud_store_parity_prime() if _is_cloud_mode() else _local_store_parity_prime())


@st.cache_data(ttl=300)
def load_city_parity_prime() -> pd.DataFrame:
    return _dedup_columns(_cloud_city_parity_prime() if _is_cloud_mode() else _local_city_parity_prime())


@st.cache_data(ttl=300)
def load_priority_actions() -> pd.DataFrame:
    return _cloud_priority_actions() if _is_cloud_mode() else _local_priority_actions()


@st.cache_data(ttl=300)
def load_pipeline_health() -> pd.DataFrame:
    return _cloud_pipeline_health() if _is_cloud_mode() else _local_pipeline_health()


def load_glovo_products_prime(city_code: str, store_name: str, week_num: str) -> pd.DataFrame:
    """Prodotti Glovo prime per uno store specifico. Non cachato (filtra live)."""
    if _is_cloud_mode():
        df = _cloud_glovo_products_prime()
        if df.empty:
            return pd.DataFrame()
        mask = (df["city_code"] == city_code) & (df["store_name"] == store_name)
        if week_num:
            mask = mask & (df["week_num"] == week_num)
        cols = ["product_name",
                "type_of_promo_np", "has_active_promo_np", "avg_percentage_off_np",
                "type_of_promo_p",  "has_active_promo_p",  "avg_percentage_off_p",
                "avg_unit_price", "total_product_sold"]
        cols_present = [c for c in cols if c in df.columns]
        return df[mask][cols_present].reset_index(drop=True)
    return _local_glovo_products_prime(city_code, store_name, week_num)


@st.cache_data(ttl=300)
def load_prime_store_counts() -> pd.DataFrame:
    """
    Restituisce (city_code, store_name, week_num) degli store che hanno
    almeno un prodotto con promo PRIME reale (has_active_promo_p = 'Y').
    In cloud mode: derivato da store_parity_prime (colonna glovo_rank_label non vuota
    e proveniente da dati prime, oppure flaggato in futuro).
    In locale: legge da glovo_products_prime via SQLite.
    """
    if _is_cloud_mode():
        # In cloud non abbiamo glovo_products_prime su Sheets;
        # usiamo store_parity_prime come proxy: store con promo_coverage_pct > 0
        spp = _cloud_store_parity_prime()
        if spp.empty:
            return pd.DataFrame()
        has_prime = spp[
            pd.to_numeric(spp.get("promo_coverage_pct", pd.Series(dtype=float)),
                          errors="coerce").fillna(0) > 0
        ][["city_code", "glovo_name", "week_num"]].copy()
        has_prime = has_prime.rename(columns={"glovo_name": "store_name"})
        return has_prime.drop_duplicates()

    conn = _get_sqlite_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        return pd.read_sql(
            """SELECT DISTINCT city_code, store_name, week_num
               FROM glovo_products_prime
               WHERE has_active_promo_p = 'Y'""",
            conn,
        )
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_delta_parity() -> pd.DataFrame:
    """
    Join store_parity × store_parity_prime: restituisce solo gli store
    dove il parity cambia tra standard e prime.
    """
    if _is_cloud_mode():
        sp  = _cloud_store_parity()
        spp = _cloud_store_parity_prime()
        if sp.empty or spp.empty:
            return pd.DataFrame()
        merged = sp.merge(
            spp[["city_code", "glovo_name", "week_num", "parity",
                 "glovo_rank_label"]].rename(columns={
                     "parity":          "prime_parity",
                     "glovo_rank_label":"prime_promo",
                 }),
            on=["city_code", "glovo_name", "week_num"],
            how="inner",
        )
        merged = merged.rename(columns={
            "parity":          "standard_parity",
            "glovo_rank_label":"standard_promo",
        })
        delta = merged[merged["standard_parity"] != merged["prime_parity"]].copy()
        cols = ["city_code", "glovo_name", "week_num",
                "standard_parity", "prime_parity",
                "standard_promo",  "prime_promo", "revenue"]
        cols_present = [c for c in cols if c in delta.columns]
        return delta[cols_present].sort_values(
            ["week_num", "city_code", "glovo_name"], ascending=[False, True, True]
        )

    conn = _get_sqlite_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        return pd.read_sql(
            """SELECT sp.city_code, sp.glovo_name, sp.week_num,
                      sp.parity               AS standard_parity,
                      spp.parity              AS prime_parity,
                      sp.glovo_rank_label     AS standard_promo,
                      spp.glovo_rank_label    AS prime_promo,
                      sp.revenue
               FROM store_parity sp
               JOIN store_parity_prime spp
                 ON sp.city_code  = spp.city_code
                AND sp.glovo_name = spp.glovo_name
                AND sp.week_num   = spp.week_num
               WHERE sp.parity != spp.parity
               ORDER BY sp.week_num DESC, sp.city_code, sp.glovo_name""",
            conn,
        )
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_review_queue() -> pd.DataFrame:
    return _cloud_review_queue() if _is_cloud_mode() else _local_review_queue()


@st.cache_data(ttl=300)
def load_store_mapping() -> pd.DataFrame:
    return _cloud_store_mapping() if _is_cloud_mode() else _local_store_mapping()


@st.cache_data(ttl=300)
def load_unmatched_stores() -> pd.DataFrame:
    return _cloud_unmatched() if _is_cloud_mode() else _local_unmatched()


@st.cache_data(ttl=300)
def load_deliveroo_names_by_city() -> dict[str, list[str]]:
    return _cloud_deliveroo_names() if _is_cloud_mode() else _local_deliveroo_names()


@st.cache_data(ttl=300)
def load_deliveroo_promo_counts() -> pd.DataFrame:
    """
    Restituisce un DataFrame con (city_code, restaurant_name, deliveroo_promo_products)
    dove deliveroo_promo_products = n. prodotti con promotion_type non vuoto.
    """
    if _is_cloud_mode():
        df = _cloud_deliveroo_products()
    else:
        p = ROOT / "output" / "deliveroo_promo_products.csv"
        if not p.exists():
            return pd.DataFrame(columns=["city_code", "restaurant_name", "deliveroo_promo_products"])
        df = pd.read_csv(p, dtype=str).fillna("")
        df.columns = [c.strip().lower() for c in df.columns]

    if df.empty or "promotion_type" not in df.columns:
        return pd.DataFrame(columns=["city_code", "restaurant_name", "deliveroo_promo_products"])

    promo_mask = df["promotion_type"].str.strip() != ""
    counts = (
        df[promo_mask]
        .groupby(["city_code", "restaurant_name"])
        .size()
        .reset_index(name="deliveroo_promo_products")
    )
    return counts


def load_glovo_products(city_code: str, store_name: str, week_num: str) -> pd.DataFrame:
    """Prodotti Glovo per uno store specifico. Non cachato (filtra live).
    Se week_num è stringa vuota, restituisce tutti i prodotti disponibili per lo store."""
    if _is_cloud_mode():
        df = _cloud_glovo_products()
        if df.empty:
            return pd.DataFrame()
        mask = (df["city_code"] == city_code) & (df["store_name"] == store_name)
        if week_num:
            mask = mask & (df["week_num"] == week_num)
        cols = ["product_name", "type_of_promo", "has_active_promo",
                "avg_percentage_off", "avg_unit_price", "total_product_sold",
                "min_basket_size_np", "week_num"]
        cols_present = [c for c in cols if c in df.columns]
        result = df[mask][cols_present]
        sort_cols = [c for c in ["has_active_promo", "avg_unit_price"] if c in result.columns]
        if sort_cols:
            ascending = [True if c == "has_active_promo" else False for c in sort_cols]
            result = result.sort_values(sort_cols, ascending=ascending)
        return result
    return _local_glovo_products(city_code, store_name, week_num)


def load_deliveroo_products(city_code: str, restaurant_name: str, week_num: str = "") -> pd.DataFrame:
    """Prodotti Deliveroo per uno store specifico. Non cachato (filtra live)."""
    if not restaurant_name:
        return pd.DataFrame()

    def _filter_and_return(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or "restaurant_name" not in df.columns or "city_code" not in df.columns:
            return pd.DataFrame()
        mask = (df["city_code"] == city_code) & (df["restaurant_name"] == restaurant_name)
        # Filtra per settimana — sempre applicato se week_num fornito
        if week_num:
            if "week_num" in df.columns:
                mask = mask & (df["week_num"] == week_num)
            elif "scraped_at_utc" in df.columns:
                def _ts_to_week(ts):
                    try:
                        dt = pd.to_datetime(ts, utc=True)
                        iso = dt.isocalendar()
                        return f"{iso[0]}-W{int(iso[1]):02d}"
                    except Exception:
                        return ""
                mask = mask & (df["scraped_at_utc"].apply(_ts_to_week) == week_num)
        cols = ["product_name", "product_description", "product_price", "promotion_type"]
        cols_present = [c for c in cols if c in df.columns]
        result = df[mask][cols_present]
        return result.drop_duplicates("product_name") if "product_name" in result.columns else result

    if _is_cloud_mode():
        return _filter_and_return(_cloud_deliveroo_products())
    return _filter_and_return(_local_deliveroo_products_raw(city_code))


# ---------------------------------------------------------------------------
# Scrittura mapping (funziona in entrambe le modalita')
# ---------------------------------------------------------------------------

def _sync_mapping_to_sheets() -> None:
    """Sovrascrive il tab store_mapping su Sheets con il CSV locale aggiornato."""
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds  = Credentials.from_service_account_info(_get_service_account(), scopes=scopes)
    gc     = gspread.authorize(creds)
    sheet  = gc.open_by_key(_get_sheet_id())

    df = pd.read_csv(ROOT / "data" / "store_mapping.csv", dtype=str).fillna("")
    headers = df.columns.tolist()
    rows    = df.values.tolist()

    try:
        ws = sheet.worksheet("store_mapping")
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title="store_mapping", rows=1, cols=len(headers))

    ws.clear()
    chunk = 5000
    all_data = [headers] + rows
    for i in range(0, len(all_data), chunk):
        ws.append_rows(all_data[i:i + chunk], value_input_option="RAW")
        if i + chunk < len(all_data):
            import time; time.sleep(1.0)

def save_confirmed_match(city: str, glovo_name: str, deliveroo_name: str) -> None:
    from pipeline.store_matcher import confirm_match, reject_match
    if _is_cloud_mode():
        # Append-only: un solo API call, quasi istantaneo
        from pipeline.sheets_reader import append_manual_match
        append_manual_match(
            _get_sheet_id(),
            _get_service_account(),
            {
                "city_code":      city,
                "glovo_name":     glovo_name,
                "glovo_store_id": "",
                "deliveroo_name": deliveroo_name,
                "confidence":     "1.0",
                "source":         "manual_cloud",
            },
        )
    else:
        if deliveroo_name:
            confirm_match(city, glovo_name, deliveroo_name)
        else:
            reject_match(city, glovo_name)


def save_rejected_match(city: str, glovo_name: str) -> None:
    save_confirmed_match(city, glovo_name, "")


def save_not_on_deliveroo(city: str, glovo_name: str) -> None:
    """Marca lo store come 'Non su Deliveroo' (assente dalla piattaforma, no esclusiva)."""
    from pipeline.store_matcher import mark_not_on_deliveroo
    if _is_cloud_mode():
        # In cloud mode: salva nel mapping locale e sincronizza su Sheets
        mark_not_on_deliveroo(city, glovo_name)
        _sync_mapping_to_sheets()
    else:
        mark_not_on_deliveroo(city, glovo_name)


def save_glovo_exclusive(city: str, glovo_name: str) -> None:
    """Marca lo store come 'Esclusiva Glovo' (accordo commerciale di esclusiva)."""
    from pipeline.store_matcher import mark_glovo_exclusive
    if _is_cloud_mode():
        mark_glovo_exclusive(city, glovo_name)
        _sync_mapping_to_sheets()
    else:
        mark_glovo_exclusive(city, glovo_name)


def _run_save(action_fn, *args, success_msg: str) -> None:
    """Esegue un'azione di salvataggio con spinner, feedback e gestione errori."""
    try:
        with st.spinner("Salvataggio in corso..."):
            action_fn(*args)
        clear_cache()
        st.session_state["last_save_msg"] = ("ok", success_msg)
    except Exception as e:
        st.session_state["last_save_msg"] = ("err", str(e))
    st.rerun()


def clear_cache():
    load_store_parity.clear()
    load_city_parity.clear()
    load_review_queue.clear()
    load_store_mapping.clear()
    load_unmatched_stores.clear()
    load_deliveroo_names_by_city.clear()
    if _is_cloud_mode():
        _cloud_load_all.clear()
        # load_glovo_products e load_deliveroo_products non sono cachate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parity_badge(label: str) -> str:
    icons = {"SUPERIORITY": "🟢", "PARITY": "🟡", "INFERIORITY": "🔴", "UNMATCHED": "⚪", "EXCLUSIVE_GLOVO": "🟣"}
    colors = {"SUPERIORITY": "#00A082", "PARITY": "#b8960a", "INFERIORITY": "#ef4444", "UNMATCHED": "#94a3b8", "EXCLUSIVE_GLOVO": "#7c3aed"}
    c = colors.get(label, "")
    style = f"color:{c};font-weight:600" if c else ""
    display = {
        "SUPERIORITY":    "SUPERIORITY",
        "PARITY":         "PARITY",
        "INFERIORITY":    "INFERIORITY",
        "UNMATCHED":      "UNMATCHED",
        "EXCLUSIVE_GLOVO": "Exclusive Glovo",
    }
    return f"{icons.get(label, '')} {display.get(label, label)}"


def metric_delta_color(val: float) -> str:
    """Per le metric card: verde se >0, rosso se <0."""
    return "normal" if val >= 0 else "inverse"


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def sidebar() -> tuple[list[str], list[str]]:
    st.sidebar.markdown(
        """
        <div style='background:#F2CC38;border-radius:10px;padding:12px 16px;margin-bottom:12px;text-align:center'>
            <span style='font-size:1.5rem;font-weight:800;color:#161717;letter-spacing:1px;font-family:Montserrat,sans-serif'>Promo Parity</span><br>
            <span style='font-size:1rem;font-weight:700;color:#161717;font-family:Montserrat,sans-serif'>Glovo vs Deliveroo</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.title("Filtri")

    city_df    = load_city_parity()
    store_df   = load_store_parity()

    if city_df.empty:
        st.sidebar.warning("Nessun dato nel DB. Esegui prima la pipeline.")
        return [], []

    all_weeks  = sorted(city_df["week_num"].unique(), reverse=True)
    all_cities = sorted(city_df["city_code"].unique())

    sel_weeks  = st.sidebar.multiselect("Settimana", all_weeks,
                                        default=all_weeks[:1] if all_weeks else [])
    sel_cities = st.sidebar.multiselect("Città", all_cities,
                                        default=all_cities)

    # Filtro AM — carica mapping e mostra selectbox
    am_df = load_am_mapping()
    sel_am = None
    if not am_df.empty and "sf_registered_am" in am_df.columns:
        all_ams = sorted(am_df["sf_registered_am"].dropna().unique())
        all_ams = [a for a in all_ams if a.strip()]
        if all_ams:
            am_options = ["Tutti"] + all_ams
            # Recupera la selezione precedente dalla session_state (evita reset ad ogni rerun)
            prev = st.session_state.get("sel_am_value", "Tutti")
            default_idx = am_options.index(prev) if prev in am_options else 0
            sel_am = st.sidebar.selectbox(
                "👤 Responsabile AM",
                options=am_options,
                index=default_idx,
                key="sel_am_value",
            )
            if sel_am == "Tutti":
                sel_am = None

    st.sidebar.divider()
    if st.sidebar.button("🔄 Aggiorna dati"):
        clear_cache()
        st.rerun()

    return sel_weeks, sel_cities, sel_am


# ---------------------------------------------------------------------------
# TAB 1 — City Parity Overview
# ---------------------------------------------------------------------------

def _recompute_city_from_stores(store_df: pd.DataFrame) -> pd.DataFrame:
    """Ricalcola city_parity da uno store_parity filtrato (es. per AM).
    Usato quando il filtro AM è attivo per avere metriche city-level corrette."""
    rows = []
    for (city, week), g in store_df.groupby(["city_code", "week_num"]):
        matched = g[~g["parity"].isin(["UNMATCHED", "EXCLUSIVE_GLOVO"])]
        n_total   = len(g)
        n_matched = len(matched)
        if n_matched == 0:
            continue
        n_sup = int((matched["parity"] == "SUPERIORITY").sum())
        n_par = int((matched["parity"] == "PARITY").sum())
        n_inf = int((matched["parity"] == "INFERIORITY").sum())
        pct_sup = round(n_sup / n_matched * 100, 1)
        pct_par = round(n_par / n_matched * 100, 1)
        pct_inf = round(n_inf / n_matched * 100, 1)
        rev = pd.to_numeric(matched["revenue"], errors="coerce").fillna(0)
        total_rev = rev.sum()
        if total_rev > 0:
            w_sup = round(rev[matched["parity"] == "SUPERIORITY"].sum() / total_rev * 100, 1)
            w_par = round(rev[matched["parity"] == "PARITY"].sum()      / total_rev * 100, 1)
            w_inf = round(rev[matched["parity"] == "INFERIORITY"].sum() / total_rev * 100, 1)
        else:
            w_sup, w_par, w_inf = pct_sup, pct_par, pct_inf
        best_w = max(w_sup, w_par, w_inf)
        label  = "SUPERIORITY" if best_w == w_sup else ("PARITY" if best_w == w_par else "INFERIORITY")
        rows.append({
            "city_code": city, "week_num": week,
            "n_stores_total": n_total, "n_stores_matched": n_matched,
            "n_superiority": n_sup, "n_parity": n_par, "n_inferiority": n_inf,
            "pct_superiority": pct_sup, "pct_parity": pct_par, "pct_inferiority": pct_inf,
            "w_superiority": w_sup, "w_parity": w_par, "w_inferiority": w_inf,
            "city_parity_label": label,
            "match_coverage_pct": round(n_matched / n_total * 100, 1) if n_total > 0 else 0.0,
        })
    return pd.DataFrame(rows)


def _am_filtered_stores(sel_am: str | None) -> set[tuple[str, str]] | None:
    """Ritorna il set (city_code, store_name) assegnati all'AM, o None se nessun filtro."""
    if not sel_am:
        return None
    am_df = load_am_mapping()
    if am_df.empty or "sf_registered_am" not in am_df.columns:
        return None
    mask = am_df["sf_registered_am"].str.strip().str.lower() == sel_am.strip().lower()
    filtered = am_df[mask]
    if filtered.empty:
        return set()
    return set(zip(filtered["city_code"].str.strip(), filtered["store_name"].str.strip()))


def tab_city_parity(sel_weeks, sel_cities, prime: bool = False, sel_am=None):
    title = "City Parity Overview — Vista Prime" if prime else "City Parity Overview"
    if prime:
        st.header(title)
    else:
        _icon = ROOT / "assets" / "promoZone.png"
        if _icon.exists():
            import base64
            _b64 = base64.b64encode(_icon.read_bytes()).decode()
            st.markdown(
                f"""<div style='display:flex;align-items:center;gap:10px;margin-bottom:4px'>
                      <img src='data:image/png;base64,{_b64}' style='width:42px;height:42px;object-fit:contain'>
                      <h2 style='margin:0;padding:0'>{title}</h2>
                    </div>""",
                unsafe_allow_html=True,
            )
        else:
            st.header(f"📊 {title}")

    if prime:
        st.info("★ **Vista Prime**: la promozione Glovo usata è quella **Prime** dove disponibile, "
                "altrimenti la Non-Prime come fallback. Confronto vs Deliveroo standard.")
    else:
        st.caption("Visione sintetica per città e settimana, pesata per fatturato Glovo")

    # [B + F] KPI comparativi Standard vs Prime + Copertura Prime
    if prime:
        std_city_df = load_city_parity()
        if not std_city_df.empty:
            st.subheader("Standard vs Prime — Confronto KPI")
            _weeks_for_kpi = sel_weeks if sel_weeks else sorted(std_city_df["week_num"].unique(), reverse=True)[:1]
            _kpi_week = _weeks_for_kpi[-1] if _weeks_for_kpi else None
            if _kpi_week:
                std_w  = std_city_df[std_city_df["week_num"] == _kpi_week]
                prim_w = load_city_parity_prime()
                prim_w = prim_w[prim_w["week_num"] == _kpi_week] if not prim_w.empty else pd.DataFrame()

                if sel_cities:
                    std_w  = std_w[std_w["city_code"].isin(sel_cities)]
                    prim_w = prim_w[prim_w["city_code"].isin(sel_cities)] if not prim_w.empty else prim_w

                if not std_w.empty and not prim_w.empty:
                    st.caption(f"Settimana: **{_kpi_week}** — totale città analizzate: {len(std_w)}")
                    _kpi_col1, _kpi_col2, _kpi_col3 = st.columns(3)
                    def _kpi_delta(prime_val, std_val, label, fmt="{:.0f}"):
                        delta = prime_val - std_val
                        st.metric(
                            label,
                            fmt.format(prime_val),
                            delta=f"{'+' if delta >= 0 else ''}{fmt.format(delta)} vs std",
                            delta_color="normal",
                        )
                    with _kpi_col1:
                        st.markdown("**🟢 SUPERIORITY**")
                        _kpi_delta(prim_w["n_superiority"].sum(), std_w["n_superiority"].sum(), "Store (Prime)")
                        _kpi_delta(prim_w["pct_superiority"].mean(), std_w["pct_superiority"].mean(),
                                   "% Revenue (Prime)", fmt="{:.1f}%")
                    with _kpi_col2:
                        st.markdown("**🟡 PARITY**")
                        _kpi_delta(prim_w["n_parity"].sum(), std_w["n_parity"].sum(), "Store (Prime)")
                        _kpi_delta(prim_w["pct_parity"].mean(), std_w["pct_parity"].mean(),
                                   "% Revenue (Prime)", fmt="{:.1f}%")
                    with _kpi_col3:
                        st.markdown("**🔴 INFERIORITY**")
                        _kpi_delta(prim_w["n_inferiority"].sum(), std_w["n_inferiority"].sum(), "Store (Prime)")
                        _kpi_delta(prim_w["pct_inferiority"].mean(), std_w["pct_inferiority"].mean(),
                                   "% Revenue (Prime)", fmt="{:.1f}%")
                    st.divider()

        # [F] Copertura Prime
        _sp_prime = load_store_parity_prime()
        if not _sp_prime.empty:
            with st.expander("Copertura Promozioni Prime", expanded=False):
                _sp_f = _sp_prime.copy()
                if sel_weeks:
                    _sp_f = _sp_f[_sp_f["week_num"].isin(sel_weeks)]
                if sel_cities:
                    _sp_f = _sp_f[_sp_f["city_code"].isin(sel_cities)]
                if not _sp_f.empty:
                    _n_total = len(_sp_f)

                    # Store con almeno un prodotto con promo PRIME reale
                    _prime_stores = load_prime_store_counts()
                    if not _prime_stores.empty:
                        _ps = _prime_stores.copy()
                        if sel_weeks:
                            _ps = _ps[_ps["week_num"].isin(sel_weeks)]
                        if sel_cities:
                            _ps = _ps[_ps["city_code"].isin(sel_cities)]
                        # Conta store che compaiono anche in _sp_f
                        _sp_f_keys = set(zip(_sp_f["city_code"], _sp_f["glovo_name"]))
                        _n_con_prime = _ps[
                            _ps.apply(lambda r: (r["city_code"], r["store_name"]) in _sp_f_keys, axis=1)
                        ]["store_name"].nunique()
                    else:
                        _n_con_prime = 0

                    _pct_prime = _n_con_prime / _n_total * 100 if _n_total > 0 else 0
                    _avg_cov   = pd.to_numeric(_sp_f.get("promo_coverage_pct", pd.Series(dtype=float)),
                                               errors="coerce").mean()
                    _fc1, _fc2, _fc3 = st.columns(3)
                    with _fc1:
                        st.metric("Store con promo Prime reale", f"{_n_con_prime} / {_n_total}",
                                  delta=f"{_pct_prime:.1f}%")
                    with _fc2:
                        st.metric("Copertura promo media (Prime)", f"{_avg_cov:.1f}%" if pd.notna(_avg_cov) else "—")
                    with _fc3:
                        _delta_df = load_delta_parity()
                        if not _delta_df.empty:
                            _d = _delta_df.copy()
                            if sel_weeks:
                                _d = _d[_d["week_num"].isin(sel_weeks)]
                            if sel_cities:
                                _d = _d[_d["city_code"].isin(sel_cities)]
                            st.metric("Store che cambiano parity con Prime", len(_d))
                        else:
                            st.metric("Store che cambiano parity con Prime", "—")
                else:
                    st.info("Nessun dato per i filtri selezionati.")

    city_df = load_city_parity_prime() if prime else load_city_parity()
    if city_df.empty:
        st.info("Nessun dato disponibile. Esegui la pipeline settimanale.")
        return

    # Se filtro AM attivo: ricalcola city_parity dagli store filtrati per AM
    _am_stores = _am_filtered_stores(sel_am)
    if _am_stores is not None:
        _sp = load_store_parity_prime() if prime else load_store_parity()
        if not _sp.empty:
            _sp_am = _sp[_sp.apply(
                lambda r: (str(r.get("city_code","")).strip(), str(r.get("glovo_name","")).strip())
                          in _am_stores, axis=1
            )]
            city_df = _recompute_city_from_stores(_sp_am)
        if sel_am:
            st.info(f"👤 Filtro AM attivo: **{sel_am}**")

    df = city_df.copy()
    if sel_weeks and "week_num" in df.columns:
        df = df[df["week_num"].isin(sel_weeks)]
    if sel_cities and "city_code" in df.columns:
        df = df[df["city_code"].isin(sel_cities)]

    if df.empty:
        st.warning("Nessun dato per i filtri selezionati.")
        return

    # ---- KPI top ----
    latest_week = df["week_num"].max()
    dfw = df[df["week_num"] == latest_week]

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        sup_cities = (dfw["city_parity_label"] == "SUPERIORITY").sum()
        st.metric("🟢 Città Superiority", sup_cities)
    with col2:
        par_cities = (dfw["city_parity_label"] == "PARITY").sum()
        st.metric("🟡 Città Parity", par_cities)
    with col3:
        inf_cities = (dfw["city_parity_label"] == "INFERIORITY").sum()
        st.metric("🔴 Città Inferiority", inf_cities)
    with col4:
        avg_cov = dfw["match_coverage_pct"].mean()
        st.metric("🔗 Match coverage medio", f"{avg_cov:.1f}%")

    st.divider()

    # ---- Heatmap città x settimana (valore = w_superiority - w_inferiority) ----
    st.subheader("Heatmap Parity Score (revenue-weighted)")
    st.caption("Score = % revenue in SUPERIORITY − % revenue in INFERIORITY  |  verde = Glovo avvantaggiata")

    pivot_data = df.copy()
    pivot_data["parity_score"] = pivot_data["w_superiority"] - pivot_data["w_inferiority"]
    pivot = pivot_data.pivot_table(
        index="city_code", columns="week_num",
        values="parity_score", aggfunc="mean"
    )
    pivot = pivot[sorted(pivot.columns, reverse=True)]

    fig_heat = go.Figure(data=go.Heatmap(
        z=pivot.values,
        x=pivot.columns.tolist(),
        y=pivot.index.tolist(),
        colorscale=[[0, "#ef4444"], [0.5, "#FFF8D0"], [1, "#00A082"]],
        zmid=0,
        text=[[f"{v:.0f}%" for v in row] for row in pivot.values],
        texttemplate="%{text}",
        colorbar=dict(title="Score"),
    ))
    fig_heat.update_layout(height=350, margin=dict(t=20, b=20))
    _city_kp = "_p" if prime else ""
    st.plotly_chart(fig_heat, use_container_width=True, key=f"fig_heat{_city_kp}")

    # ---- Tabella dettaglio ----
    st.subheader("Dettaglio per città")
    display_cols = [
        "city_code", "week_num", "city_parity_label",
        "n_stores_matched", "n_superiority", "n_parity", "n_inferiority",
        "w_superiority", "w_parity", "w_inferiority", "match_coverage_pct"
    ]
    disp = df[display_cols].copy()
    disp["city_parity_label"] = disp["city_parity_label"].apply(parity_badge)
    # Formatta le colonne % con 1 decimale e simbolo %
    for col in ["w_superiority", "w_parity", "w_inferiority", "match_coverage_pct"]:
        if col in disp.columns:
            disp[col] = pd.to_numeric(disp[col], errors="coerce") \
                .apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "")
    disp.columns = [
        "Città", "Settimana", "Parity Label",
        "Store matchati", "# Sup", "# Par", "# Inf",
        "% Sup (revenue)", "% Par (revenue)", "% Inf (revenue)", "Match coverage %"
    ]
    _PARITY_BADGE_FG = {
        "🟢 SUPERIORITY": "#00614e", "🟡 PARITY": "#7a6300",
        "🔴 INFERIORITY": "#991b1b", "⚪ UNMATCHED": "#475569",
        "🟣 Exclusive Glovo": "#5b21b6",
    }
    _PARITY_BADGE_BG = {
        "🟢 SUPERIORITY": "#d0f0ea", "🟡 PARITY": "#FFF8D0",
        "🔴 INFERIORITY": "#fee2e2", "⚪ UNMATCHED": "#f1f5f9",
        "🟣 Exclusive Glovo": "#ede9fe",
    }
    def _city_cell_style(i, col, val):
        if col == "Parity Label":
            bg = _PARITY_BADGE_BG.get(val, "")
            fg = _PARITY_BADGE_FG.get(val, "")
            if bg:
                return f"background:{bg};color:{fg};font-weight:600"
        return ""
    st.markdown(
        _products_table_html(disp, "#e8eaed", "#1a1a1a", cell_style_fn=_city_cell_style),
        unsafe_allow_html=True,
    )

    # [A] Delta View: store che cambiano parity Standard → Prime
    if prime:
        st.divider()
        st.subheader("Delta View — Store che cambiano parity con Prime")
        st.caption("Solo store dove la promozione Prime fa cambiare il risultato rispetto alla vista standard")
        delta_df = load_delta_parity()
        if not delta_df.empty:
            _dd = delta_df.copy()
            if sel_weeks:
                _dd = _dd[_dd["week_num"].isin(sel_weeks)]
            if sel_cities:
                _dd = _dd[_dd["city_code"].isin(sel_cities)]

            if not _dd.empty:
                # Filtro per direzione del cambio
                _parity_rank = {"SUPERIORITY": 0, "PARITY": 1, "INFERIORITY": 2, "UNMATCHED": 3, "EXCLUSIVE_GLOVO": 4}
                _dd["_std_rank"]   = _dd["standard_parity"].map(_parity_rank).fillna(9)
                _dd["_prime_rank"] = _dd["prime_parity"].map(_parity_rank).fillna(9)
                _dd["direzione"] = _dd.apply(
                    lambda r: "⬆️ Migliora" if r["_prime_rank"] < r["_std_rank"] else "⬇️ Peggiora", axis=1
                )
                _dir_opts = sorted(_dd["direzione"].unique().tolist())
                _dir_filter = st.multiselect(
                    "Filtra direzione", _dir_opts, default=_dir_opts, key="delta_dir_filter"
                )
                if _dir_filter:
                    _dd = _dd[_dd["direzione"].isin(_dir_filter)]

                disp_delta = _dd[["city_code", "glovo_name", "week_num",
                                   "direzione", "standard_parity", "prime_parity",
                                   "standard_promo", "prime_promo", "revenue"]].copy()
                disp_delta["revenue"] = pd.to_numeric(disp_delta["revenue"], errors="coerce") \
                    .apply(lambda x: f"{x:,.0f}€".replace(",", ".") if pd.notna(x) else "")
                disp_delta = disp_delta.rename(columns={
                    "city_code":        "Città",
                    "glovo_name":       "Store Glovo",
                    "week_num":         "Settimana",
                    "direzione":        "Direzione",
                    "standard_parity":  "Parity Standard",
                    "prime_parity":     "Parity Prime",
                    "standard_promo":   "Promo Standard",
                    "prime_promo":      "Promo Prime",
                    "revenue":          "Revenue",
                })
                _DELTA_PARITY_BG = {
                    "SUPERIORITY": "#00A082", "PARITY": "#F2CC38",
                    "INFERIORITY": "#ef4444", "UNMATCHED": "#94a3b8",
                    "EXCLUSIVE_GLOVO": "#8b5cf6",
                }
                def _delta_cell_style(i, col, val):
                    if col == "Direzione":
                        if "Migliora" in val:
                            return "background:#d9d2e9;color:#9900ff;font-weight:600"
                        if "Peggiora" in val:
                            return "background:#fee2e2;color:#991b1b;font-weight:600"
                    if col in ("Parity Standard", "Parity Prime"):
                        bg = _DELTA_PARITY_BG.get(val.strip(), "")
                        if bg:
                            return f"background:{bg};color:white;font-weight:600"
                    return ""

                st.markdown(
                    _products_table_html(
                        disp_delta, "#e8eaed", "#1a1a1a",
                        header_brands={"Store Glovo": "glovo"},
                        cell_style_fn=_delta_cell_style,
                    ),
                    unsafe_allow_html=True,
                )
                st.caption(f"{len(disp_delta)} store con cambio parity — "
                           f"{(disp_delta['Direzione'].str.contains('Migliora')).sum()} migliorano, "
                           f"{(disp_delta['Direzione'].str.contains('Peggiora')).sum()} peggiorano")
            else:
                st.info("Nessun store cambia parity con Prime per i filtri selezionati.")
        else:
            st.info("Dati delta non disponibili. Esegui la pipeline con CSV W20+.")

    # ---- Grouped bar per settimana ----
    if len(sel_weeks) > 1 or len(df["week_num"].unique()) > 1:
        st.subheader("Composizione parity per città (settimana più recente)")
        bar_df = dfw[["city_code", "w_superiority", "w_parity", "w_inferiority"]].melt(
            id_vars="city_code",
            var_name="tipo",
            value_name="pct_revenue"
        )
        bar_df["tipo"] = bar_df["tipo"].map({
            "w_superiority": "SUPERIORITY",
            "w_parity":      "PARITY",
            "w_inferiority": "INFERIORITY",
        })
        fig_bar = px.bar(
            bar_df, x="city_code", y="pct_revenue", color="tipo",
            color_discrete_map=PARITY_COLORS,
            category_orders={"tipo": ["SUPERIORITY", "PARITY", "INFERIORITY"]},
            labels={"city_code": "Città", "pct_revenue": "% Revenue", "tipo": ""},
            barmode="stack",
        )
        fig_bar.update_layout(height=350, margin=dict(t=20))
        st.plotly_chart(fig_bar, use_container_width=True, key=f"fig_bar{_city_kp}")


# ---------------------------------------------------------------------------
# TAB 2 — Store Detail
# ---------------------------------------------------------------------------

# Colonne per brand nella tabella store detail (match sul nome rinominato)
_SD_GLOVO_COLS = {
    "Glovo Restaurant", "Glovo Promo Type", "Glovo % OFF",
    "Glovo Items in Promo", "Glovo Promo Coverage",
}
_SD_DELIVEROO_COLS = {
    "Deliveroo Restaurant", "Deliveroo Promo Type", "Deliveroo % OFF",
    "Deliveroo Items in Promo", "Deliveroo Promo Detail",
}
_SD_PARITY_BG = {
    "SUPERIORITY":     "#d0f0ea",
    "PARITY":          "#FFF8D0",
    "INFERIORITY":     "#fee2e2",
    "UNMATCHED":       "#f1f5f9",
    "EXCLUSIVE_GLOVO": "#ede9fe",
}
_SD_PARITY_FG = {
    "SUPERIORITY":     "#00614e",
    "PARITY":          "#7a6300",
    "INFERIORITY":     "#991b1b",
    "UNMATCHED":       "#475569",
    "EXCLUSIVE_GLOVO": "#5b21b6",
}


def _store_table_html(df: pd.DataFrame) -> str:
    """
    Tabella HTML per store detail:
    - Header Glovo   → sfondo giallo #FFC244
    - Header Deliveroo → sfondo teal #00CCBC
    - Cella 'Comparison' → colore parity
    - Tutto centrato, righe alternate
    """
    gy, gfg = "#FFC244", "#1a1a1a"
    dy, dfg = "#00CCBC", "#ffffff"

    cols = list(df.columns)
    n = len(cols)
    col_w = f"{100 / n:.1f}%"

    # Header
    hdr = ""
    for col in cols:
        if col in _SD_GLOVO_COLS:
            bg, fg = gy, gfg
        elif col in _SD_DELIVEROO_COLS:
            bg, fg = dy, dfg
        else:
            bg, fg = "#e8eaed", "#1a1a1a"
        hdr += (
            f'<th style="background:{bg};color:{fg};text-align:center;'
            f'padding:8px 4px;font-size:12px;font-weight:600;'
            f'width:{col_w};word-break:break-word;border:1px solid #d1d5db">'
            f'{col}</th>'
        )

    # Rows
    body = ""
    for i, (_, row) in enumerate(df.iterrows()):
        bg_row = "#ffffff" if i % 2 == 0 else "#f9fafb"
        cells = ""
        for col in cols:
            val = row.get(col, "")
            try:
                if pd.isna(val):
                    val = ""
            except Exception:
                pass
            val = "" if val is None else str(val)

            if col == "Comparison":
                cb = _SD_PARITY_BG.get(val.strip(), bg_row)
                cf = _SD_PARITY_FG.get(val.strip(), "#1a1a1a")
                cell_style = f"background:{cb};color:{cf};font-weight:600"
            else:
                cell_style = f"background:{bg_row};color:#1a1a1a"

            cells += (
                f'<td style="{cell_style};text-align:center;'
                f'padding:7px 4px;font-size:12px;border:1px solid #e5e7eb;'
                f'width:{col_w};word-break:break-word">{val}</td>'
            )
        body += f"<tr>{cells}</tr>"

    return (
        '<div style="overflow-x:auto;margin-top:8px;max-height:520px;'
        'overflow-y:auto;border:1px solid #e5e7eb;border-radius:6px">'
        '<table style="width:100%;table-layout:fixed;border-collapse:collapse">'
        f"<thead style='position:sticky;top:0;z-index:1'><tr>{hdr}</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table></div>"
    )


def _products_table_html(
    df: pd.DataFrame,
    header_bg: str,
    header_fg: str,
    row_styles: "list[str] | None" = None,
    header_brands: "dict[str,str] | None" = None,
    cell_style_fn=None,
) -> str:
    """
    Tabella HTML generica con header brand-colorati e testo centrato.

    Parametri
    ---------
    header_bg / header_fg : colore di default per l'header (usato se la colonna
                            non è in header_brands)
    row_styles            : lista di CSS string per ogni riga (stile intero row)
    header_brands         : dict {col_name: "glovo"|"deliveroo"} per override
                            colore singola colonna di header
    cell_style_fn         : callable(row_i, col_name, value) -> css str | ""
                            sovrascrive il row_style per quella singola cella
    """
    _BRAND_BG = {"glovo": "#FFC244", "deliveroo": "#00CCBC"}
    _BRAND_FG = {"glovo": "#1a1a1a", "deliveroo": "#ffffff"}

    cols = list(df.columns)
    n = len(cols)
    col_w = f"{100 / n:.1f}%"

    hdr = ""
    for col in cols:
        brand = (header_brands or {}).get(col)
        if brand:
            bg, fg = _BRAND_BG.get(brand, header_bg), _BRAND_FG.get(brand, header_fg)
        else:
            bg, fg = header_bg, header_fg
        hdr += (
            f'<th style="background:{bg};color:{fg};text-align:center;'
            f'padding:8px 4px;font-size:12px;font-weight:600;'
            f'width:{col_w};word-break:break-word;border:1px solid #d1d5db">'
            f'{col}</th>'
        )

    body = ""
    for i, (_, row) in enumerate(df.iterrows()):
        default_bg = "#ffffff" if i % 2 == 0 else "#f9fafb"
        rs = row_styles[i] if (row_styles and i < len(row_styles) and row_styles[i]) \
             else f"background:{default_bg};color:#1a1a1a"
        cells = ""
        for col in cols:
            val = row.get(col, "")
            try:
                if pd.isna(val):
                    val = ""
            except Exception:
                pass
            val = "" if val is None else str(val)
            # cell_style_fn può sovrascrivere il row style per singola cella
            cell_css = cell_style_fn(i, col, val) if cell_style_fn else ""
            style = cell_css if cell_css else rs
            cells += (
                f'<td style="{style};text-align:center;'
                f'padding:7px 4px;font-size:12px;border:1px solid #e5e7eb;'
                f'width:{col_w};word-break:break-word">{val}</td>'
            )
        body += f"<tr>{cells}</tr>"

    return (
        '<div style="overflow-x:auto;margin-top:8px;max-height:380px;'
        'overflow-y:auto;border:1px solid #e5e7eb;border-radius:6px">'
        '<table style="width:100%;table-layout:fixed;border-collapse:collapse">'
        f"<thead style='position:sticky;top:0;z-index:1'><tr>{hdr}</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table></div>"
    )


def tab_store_detail(sel_weeks, sel_cities, prime: bool = False, sel_am=None):
    import base64 as _b64mod
    _icon = ROOT / "assets" / "storePhone.png"
    if not prime:
        title_suffix = ""
        if _icon.exists():
            _b64 = _b64mod.b64encode(_icon.read_bytes()).decode()
            st.markdown(
                f"""<div style='display:flex;align-items:center;gap:10px;margin-bottom:4px'>
                      <img src='data:image/png;base64,{_b64}' style='width:42px;height:42px;object-fit:contain'>
                      <h2 style='margin:0;padding:0'>Store Detail</h2>
                    </div>""",
                unsafe_allow_html=True,
            )
        else:
            st.header("🏪 Store Detail")
    else:
        # Nel tab Prime il titolo grande è già mostrato da tab_city_parity sopra
        st.subheader("Drill-down Store — Vista Prime")

    if not prime:
        st.caption("Analisi per singolo store: promo Glovo vs Deliveroo, rank e copertura")

    store_df = load_store_parity_prime() if prime else load_store_parity()
    if store_df.empty:
        st.info("Nessun dato disponibile.")
        return

    df = store_df.copy()
    if sel_cities:
        df = df[df["city_code"].isin(sel_cities)]
    if sel_weeks:
        df = df[df["week_num"].isin(sel_weeks)]

    # Filtro AM — mantieni solo gli store assegnati all'AM selezionato
    _am_stores = _am_filtered_stores(sel_am)
    if _am_stores is not None:
        df = df[df.apply(
            lambda r: (str(r.get("city_code","")).strip(), str(r.get("glovo_name","")).strip())
                      in _am_stores, axis=1
        )]
        if sel_am:
            st.info(f"👤 Filtro AM attivo: **{sel_am}** — {len(df)} store")

    # Merge conteggio prodotti in promo Deliveroo
    # Filtra restaurant_name vuoti per evitare join many-to-many sugli UNMATCHED
    roo_counts = load_deliveroo_promo_counts()
    if not roo_counts.empty and "deliveroo_name" in df.columns:
        roo_clean = roo_counts[
            roo_counts["restaurant_name"].str.strip() != ""
        ].rename(columns={"restaurant_name": "deliveroo_name"})
        if not roo_clean.empty:
            df = df.merge(roo_clean, on=["city_code", "deliveroo_name"], how="left")
            df["deliveroo_promo_products"] = df["deliveroo_promo_products"].fillna(0).astype(int)
        else:
            df["deliveroo_promo_products"] = 0
    else:
        df["deliveroo_promo_products"] = 0

    if df.empty:
        st.warning("Nessun dato per i filtri selezionati.")
        return

    # Filtri aggiuntivi
    _kp = "_p" if prime else ""   # key prefix per evitare conflitti widget standard vs prime
    col1, col2, col3 = st.columns(3)
    with col1:
        parity_filter = st.multiselect(
            "Parity", PARITY_ORDER, default=PARITY_ORDER,
            key=f"store_parity_filter{_kp}"
        )
    with col2:
        search = st.text_input("Cerca store (nome Glovo)", "", key=f"store_search{_kp}")
    with col3:
        sort_by = st.selectbox("Ordina per", ["revenue", "parity", "glovo_rank"], index=0,
                               key=f"store_sortby{_kp}")

    col4, col5 = st.columns(2)
    with col4:
        glovo_promo_opts = sorted(df["glovo_rank_label"].replace("", pd.NA).dropna().unique().tolist()) \
            if "glovo_rank_label" in df.columns else []
        glovo_promo_filter = st.multiselect(
            "Promo Glovo", glovo_promo_opts, default=[],
            placeholder="Tutte", key=f"store_glovo_promo_filter{_kp}"
        )
    with col5:
        roo_promo_opts = sorted(df["deliveroo_rank_label"].replace("", pd.NA).dropna().unique().tolist()) \
            if "deliveroo_rank_label" in df.columns else []
        roo_promo_filter = st.multiselect(
            "Promo Deliveroo", roo_promo_opts, default=[],
            placeholder="Tutte", key=f"store_roo_promo_filter{_kp}"
        )

    if parity_filter:
        df = df[df["parity"].isin(parity_filter)]
    if search:
        df = df[df["glovo_name"].str.contains(search, case=False, na=False)]
    if glovo_promo_filter:
        df = df[df["glovo_rank_label"].isin(glovo_promo_filter)]
    if roo_promo_filter:
        df = df[df["deliveroo_rank_label"].isin(roo_promo_filter)]

    df_sorted = df.sort_values(sort_by, ascending=(sort_by != "revenue"))

    import re as _re

    # Helper: estrae la % di sconto dal testo promo Deliveroo
    def _extract_roo_pct(text: str) -> str:
        if not text:
            return ""
        # Cerca pattern come "-20%", "20%", "25% di sconto"
        m = _re.search(r"-?(\d+(?:[.,]\d+)?)\s*%", text)
        return f"{m.group(1)}%" if m else ""

    # Helper: formatta il conteggio prodotti Deliveroo in promo
    def _roo_items_label(row) -> str:
        rank = str(row.get("deliveroo_rank_label", "")).lower()
        promo_text = str(row.get("deliveroo_promo_text", "")).lower()
        count = row.get("deliveroo_promo_products", 0)
        # Basket = sconto su tutto l'ordine, non su singoli prodotti
        if "basket" in rank or "spendi" in promo_text or "ordine" in promo_text:
            return "Full menu"
        if count and int(count) > 0:
            return str(int(count))
        return ""

    # Tabella principale
    display_cols = [
        "city_code", "glovo_name", "deliveroo_name", "week_num",
        "parity",
        "glovo_rank_label", "glovo_pct_off", "glovo_min_basket", "glovo_promo_products",
        "deliveroo_rank_label", "deliveroo_promo_text", "deliveroo_pct_off", "deliveroo_min_basket", "deliveroo_promo_products",
        "revenue", "promo_coverage_pct"
    ]
    available = [c for c in display_cols if c in df_sorted.columns]
    disp = df_sorted[available].copy()

    # [C] Badge Prime Boost — colonna aggiuntiva quando prime=True
    if prime:
        _parity_rank = {"SUPERIORITY": 0, "PARITY": 1, "INFERIORITY": 2, "UNMATCHED": 3, "EXCLUSIVE_GLOVO": 4}
        _std_df = load_store_parity()
        if not _std_df.empty and sel_weeks:
            _std_filtered = _std_df[_std_df["week_num"].isin(sel_weeks)]
        else:
            _std_filtered = _std_df
        _std_map = _std_filtered.set_index(["city_code", "glovo_name"])["parity"].to_dict() \
                   if not _std_filtered.empty else {}
        def _boost_label(row):
            key = (row.get("city_code", ""), row.get("glovo_name", ""))
            std_p = _std_map.get(key, "")
            prime_p = row.get("parity", "")
            sr = _parity_rank.get(std_p, 9)
            pr = _parity_rank.get(prime_p, 9)
            if pr < sr:
                return "🚀 Boost"
            if pr > sr:
                return "⬇️ Drop"
            return ""
        disp.insert(disp.columns.get_loc("parity"), "prime_boost",
                    df_sorted.apply(_boost_label, axis=1).values)

    # Formatta colonne numeriche
    if "glovo_pct_off" in disp.columns:
        def _fmt_glovo_pct(row):
            pct = pd.to_numeric(row.get("glovo_pct_off"), errors="coerce")
            if pd.isna(pct):
                return ""
            label = f"{pct:.1f}%"
            if str(row.get("glovo_rank_label", "")).strip() == "Basket %":
                basket = pd.to_numeric(row.get("glovo_min_basket"), errors="coerce")
                if pd.notna(basket) and basket > 0:
                    label += f" min €{basket:.0f}"
            return label
        disp["glovo_pct_off"] = disp.apply(_fmt_glovo_pct, axis=1)

    # Per BASKET_PERCENTAGE: tutti i prodotti sono in promo → "Full menu"
    if "glovo_promo_products" in disp.columns and "glovo_rank_label" in disp.columns:
        is_basket = disp["glovo_rank_label"].str.strip() == "Basket %"
        disp["glovo_promo_products"] = disp["glovo_promo_products"].astype(str)
        disp.loc[is_basket, "glovo_promo_products"] = "Full menu"

    # Rimuovi colonne helper usate solo per formattazione
    disp = disp.drop(columns=["glovo_min_basket", "deliveroo_min_basket"], errors="ignore")
    if "revenue" in disp.columns:
        disp["revenue"] = pd.to_numeric(disp["revenue"], errors="coerce") \
            .apply(lambda x: f"{x:,.0f}€".replace(",", ".") if pd.notna(x) else "")
    if "promo_coverage_pct" in disp.columns:
        disp["promo_coverage_pct"] = pd.to_numeric(disp["promo_coverage_pct"], errors="coerce") \
            .apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "")

    # Colonna Deliveroo % OFF — usa quella già presente nel df (dal parity_calculator)
    # oppure la estrae dal testo promo (fallback per dati vecchi senza la colonna)
    if "deliveroo_pct_off" not in disp.columns and "deliveroo_promo_text" in disp.columns:
        disp.insert(
            disp.columns.get_loc("deliveroo_promo_text"),
            "deliveroo_pct_off",
            disp["deliveroo_promo_text"].apply(_extract_roo_pct),
        )
    elif "deliveroo_pct_off" in disp.columns and "deliveroo_promo_text" in disp.columns:
        # Formatta la colonna già presente
        disp["deliveroo_pct_off"] = pd.to_numeric(disp["deliveroo_pct_off"], errors="coerce") \
            .apply(lambda x: f"{x:.1f}%" if pd.notna(x) and x > 0 else "")

    # Colonna Deliveroo Items in promo (Full menu per basket)
    if "deliveroo_promo_products" in disp.columns:
        disp["deliveroo_promo_products"] = disp.apply(_roo_items_label, axis=1)

    # Rinomina colonne
    disp = disp.rename(columns={
        "city_code":               "City Code",
        "glovo_name":              "Glovo Restaurant",
        "deliveroo_name":          "Deliveroo Restaurant",
        "week_num":                "Week",
        "prime_boost":             "★ Prime",
        "parity":                  "Comparison",
        "glovo_rank_label":        "Glovo Promo Type",
        "glovo_pct_off":           "Glovo % OFF",
        "glovo_promo_products":    "Glovo Items in Promo",
        "deliveroo_rank_label":    "Deliveroo Promo Type",
        "deliveroo_pct_off":       "Deliveroo % OFF",
        "deliveroo_promo_products":"Deliveroo Items in Promo",
        "deliveroo_promo_text":    "Deliveroo Promo Detail",
        "revenue":                 "Revenue",
        "promo_coverage_pct":      "Glovo Promo Coverage",
    })

    _MAX_HTML = 1000
    disp_html = disp.head(_MAX_HTML)
    if len(disp) > _MAX_HTML:
        st.warning(
            f"Visualizzati i primi {_MAX_HTML} store su {len(disp)}. "
            "Usa i filtri per restringere la selezione."
        )
    st.markdown(_store_table_html(disp_html), unsafe_allow_html=True)
    st.caption(f"Totale store visualizzati: {len(disp)}")

    # ---- Drill-down su singolo store ----
    st.divider()
    st.subheader("Drill-down store")

    store_names = sorted(df["glovo_name"].unique())
    sel_store   = st.selectbox("Seleziona store", ["— seleziona —"] + store_names,
                               key=f"store_sel{_kp}")

    if sel_store != "— seleziona —":
        # Filtra per città dallo stesso df filtrato (evita ambiguità tra store con stesso nome in città diverse)
        store_city_rows = df[df["glovo_name"] == sel_store]
        store_city = store_city_rows["city_code"].iloc[0] if not store_city_rows.empty else None

        store_data = store_df[
            (store_df["glovo_name"] == sel_store) &
            (store_df["city_code"] == store_city if store_city else True)
        ].sort_values("week_num")

        # "latest" = settimana selezionata nel filtro (o la più recente disponibile)
        store_data_in_filter = store_data[store_data["week_num"].isin(sel_weeks)] if sel_weeks else store_data
        latest_src = store_data_in_filter if not store_data_in_filter.empty else store_data

        # [C] Nel drill-down prime: mostra standard vs prime affiancati
        if prime:
            _std_all = load_store_parity()
            _std_store = _std_all[
                (_std_all["glovo_name"] == sel_store) &
                (_std_all["city_code"] == store_city if store_city else True)
            ].sort_values("week_num")
            _std_in_filter = _std_store[_std_store["week_num"].isin(sel_weeks)] if sel_weeks else _std_store
            _std_latest_src = _std_in_filter if not _std_in_filter.empty else _std_store
            _std_latest = _std_latest_src.iloc[-1] if not _std_latest_src.empty else None
        else:
            _std_latest = None

        c1, c2 = st.columns(2)
        with c1:
            latest = latest_src.iloc[-1]
            st.metric("Parity attuale", parity_badge(latest["parity"]))
            st.metric("Glovo promo", latest.get("glovo_rank_label", "—"))
            st.metric("Deliveroo promo", latest.get("deliveroo_rank_label", "—"))
        with c2:
            st.metric("Revenue settimana", f"€ {latest['revenue']:.0f}")
            st.metric("Prodotti in promo", int(latest.get("glovo_promo_products", 0)))
            st.metric("Copertura promo", f"{latest.get('promo_coverage_pct', 0):.1f}%")

        # [C] Badge confronto Standard vs Prime nel drill-down
        if prime and _std_latest is not None:
            _parity_rank = {"SUPERIORITY": 0, "PARITY": 1, "INFERIORITY": 2, "UNMATCHED": 3, "EXCLUSIVE_GLOVO": 4}
            _std_p   = str(_std_latest.get("parity", ""))
            _prime_p = str(latest.get("parity", ""))
            _sr = _parity_rank.get(_std_p, 9)
            _pr = _parity_rank.get(_prime_p, 9)
            if _pr < _sr:
                _boost_msg = f"🚀 **Prime Boost**: parity migliora da {parity_badge(_std_p)} → {parity_badge(_prime_p)}"
            elif _pr > _sr:
                _boost_msg = f"⬇️ **Prime Drop**: parity peggiora da {parity_badge(_std_p)} → {parity_badge(_prime_p)}"
            else:
                _boost_msg = f"↔️ **Invariato**: parity {parity_badge(_prime_p)} uguale con e senza Prime"
            st.markdown(_boost_msg, unsafe_allow_html=True)

        # ---- Trend parity ultime 4 settimane (#9) ----
        store_all_hist = load_store_parity_prime() if prime else load_store_parity()
        store_trend = (
            store_all_hist[
                (store_all_hist["glovo_name"] == sel_store) &
                (store_all_hist["city_code"] == store_city if store_city else True)
            ]
            .sort_values("week_num")
            .tail(4)
        )
        if len(store_trend) >= 1:
            st.markdown("**Trend parity — ultime 4 settimane**")
            trend_fig = go.Figure()
            for _, row in store_trend.iterrows():
                txt_color = "#7a6300" if row["parity"] == "PARITY" else "white"
                trend_fig.add_trace(go.Bar(
                    x=[row["week_num"]],
                    y=[1],
                    marker_color=PARITY_COLORS.get(row["parity"], "#94a3b8"),
                    text=row["parity"],
                    textposition="inside",
                    textfont=dict(color=txt_color, size=11),
                    showlegend=False,
                    hovertemplate=(
                        f"<b>{row['week_num']}</b><br>"
                        f"Parity: {row['parity']}<br>"
                        f"Revenue: €{row.get('revenue', 0):,.0f}"
                        "<extra></extra>"
                    ),
                ))
            trend_fig.update_layout(
                barmode="group",
                height=130,
                yaxis=dict(showticklabels=False, showgrid=False, zeroline=False, range=[0, 1.3]),
                xaxis=dict(title="", tickfont=dict(size=11)),
                margin=dict(t=10, b=30, l=10, r=10),
                plot_bgcolor="white",
                paper_bgcolor="white",
            )
            st.plotly_chart(trend_fig, use_container_width=True, key=f"trend_fig{_kp}")

        if len(store_data) > 1:
            fig_store = px.line(
                store_data, x="week_num", y="glovo_rank",
                markers=True, title="Evoluzione rank Glovo nel tempo",
                labels={"glovo_rank": "Rank Glovo (1=migliore)", "week_num": "Settimana"},
            )
            fig_store.update_yaxes(autorange="reversed", dtick=1)
            fig_store.update_layout(height=280, margin=dict(t=40))
            st.plotly_chart(fig_store, use_container_width=True, key=f"fig_store{_kp}")

        # ---- Confronto prodotti ----
        st.divider()
        _food_icon = ROOT / "assets" / "foodMainVertical.png"
        if _food_icon.exists():
            import base64 as _b64f
            _b64_food = _b64f.b64encode(_food_icon.read_bytes()).decode()
            st.markdown(
                f"""<div style='display:flex;align-items:center;gap:10px;margin-bottom:4px'>
                      <img src='data:image/png;base64,{_b64_food}' style='width:36px;height:36px;object-fit:contain'>
                      <h3 style='margin:0;padding:0'>Prodotti per store</h3>
                    </div>""",
                unsafe_allow_html=True,
            )
        else:
            st.subheader("🛒 Prodotti per store")

        city_code      = str(latest.get("city_code", ""))
        deliveroo_nm   = str(latest.get("deliveroo_name", ""))
        week_nm        = str(latest.get("week_num", ""))

        gp = load_glovo_products(city_code, sel_store, week_nm)
        dp = load_deliveroo_products(city_code, deliveroo_nm, week_nm)

        # [E] Vista Prime: carica anche i dati prime per colonna aggiuntiva
        if prime:
            gpp = load_glovo_products_prime(city_code, sel_store, week_nm)
            col_g, col_prime, col_d = st.columns([2, 2, 2])
        else:
            gpp = pd.DataFrame()
            col_g, col_d = st.columns(2)
            col_prime = None

        # Badge loghi Glovo / Deliveroo
        _glovo_logo  = ROOT / "assets" / "glovo.png"
        _roo_logo    = ROOT / "assets" / "roo.png"
        import base64 as _b64prod
        _b64_glovo = _b64prod.b64encode(_glovo_logo.read_bytes()).decode() if _glovo_logo.exists() else ""
        _b64_roo   = _b64prod.b64encode(_roo_logo.read_bytes()).decode()   if _roo_logo.exists()   else ""

        # ---- Glovo ----
        with col_g:
            if _b64_glovo:
                st.markdown(
                    f"<div style='display:inline-flex;align-items:center;gap:8px;"
                    f"background:#F2CC38;color:#161717;padding:5px 14px;"
                    f"border-radius:8px;font-weight:700;font-size:1rem'>"
                    f"<img src='data:image/png;base64,{_b64_glovo}' style='height:22px;width:22px;object-fit:contain'>"
                    f"Glovo</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown("<span style='background:#F2CC38;color:#161717;padding:4px 12px;border-radius:6px;font-weight:700'>🛵 Glovo</span>", unsafe_allow_html=True)
            st.write("")
            _glovo_body = st.empty()   # container unico: sostituisce il vecchio render senza ghost
            if gp.empty:
                # Nessun prodotto in promo per questa settimana/store
                _glovo_body.info("Nessun prodotto Glovo in promozione per questo store nella settimana selezionata.")
            else:
                with _glovo_body.container():
                    def _glovo_promo_badge(row):
                        if row.get("has_active_promo", "N") == "Y":
                            t = row.get("type_of_promo", "")
                            pct = row.get("avg_percentage_off")
                            basket = row.get("min_basket_size_np")
                            if t == "BASKET_PERCENTAGE":
                                label = f"✅ {t} ({pct:.0f}%)" if pct and pct > 0 else f"✅ {t}"
                                if basket and float(basket) > 0:
                                    label += f" min €{float(basket):.0f}"
                                return label
                            if pct and pct > 0:
                                return f"✅ {t} ({pct:.0f}%)"
                            return f"✅ {t}" if t else "✅ Promo"
                        return "—"

                    disp_g = gp.copy()
                    disp_g["promozione"] = disp_g.apply(_glovo_promo_badge, axis=1)
                    if "avg_unit_price" in disp_g.columns:
                        disp_g["avg_unit_price"] = pd.to_numeric(disp_g["avg_unit_price"], errors="coerce") \
                            .apply(lambda x: f"{x:.1f}€" if pd.notna(x) else "")
                    disp_g = disp_g.rename(columns={
                        "product_name":       "Prodotto",
                        "avg_unit_price":     "Prezzo €",
                        "total_product_sold": "Qtà venduta",
                    })
                    show_cols_g = ["Prodotto", "promozione", "Prezzo €", "Qtà venduta"]
                    show_cols_g = [c for c in show_cols_g if c in disp_g.columns]

                    if "has_active_promo" in disp_g.columns:
                        n_promo = (disp_g["has_active_promo"] == "Y").sum()
                    else:
                        try:
                            n_promo = int(float(latest.get("glovo_promo_products", 0) or 0))
                        except (ValueError, TypeError):
                            n_promo = 0
                    st.caption(f"{len(gp)} prodotti · {n_promo} in promozione")
                    _promo_flags_g = _safe_flags(gp, "has_active_promo").reindex(disp_g.index).fillna(False)
                    _g_df = disp_g[show_cols_g]
                    _g_styles = [
                        "background:#FFF8D0;color:#7a6300" if _promo_flags_g.get(idx, False) else ""
                        for idx in _g_df.index
                    ]
                    st.markdown(
                        _products_table_html(_g_df, "#FFC244", "#1a1a1a", _g_styles),
                        unsafe_allow_html=True,
                    )

        # ---- [E] Colonna Prime (solo tab prime) ----
        if prime and col_prime is not None:
            with col_prime:
                st.markdown(
                    "<div style='display:inline-flex;align-items:center;gap:8px;"
                    "background:#7c3aed;color:white;padding:5px 14px;"
                    "border-radius:8px;font-weight:700;font-size:1rem'>"
                    "★ Glovo Prime</div>",
                    unsafe_allow_html=True,
                )
                st.write("")
                if gpp.empty:
                    st.info("Dati prodotti Prime non disponibili.\nEsegui la pipeline con CSV W20+.")
                else:
                    def _prime_promo_badge(row):
                        if str(row.get("has_active_promo_p", "N")).upper() == "Y":
                            t = row.get("type_of_promo_p", "") or ""
                            pct = row.get("avg_percentage_off_p")
                            try:
                                pct = float(pct)
                            except (TypeError, ValueError):
                                pct = 0
                            if pct and pct > 0:
                                return f"⭐ {t} ({pct:.0f}%)"
                            return f"⭐ {t}" if t else "⭐ Prime"
                        elif str(row.get("has_active_promo_np", "N")).upper() == "Y":
                            t = row.get("type_of_promo_np", "") or ""
                            pct = row.get("avg_percentage_off_np")
                            try:
                                pct = float(pct)
                            except (TypeError, ValueError):
                                pct = 0
                            if pct and pct > 0:
                                return f"✅ {t} ({pct:.0f}%) [np]"
                            return f"✅ {t} [np]" if t else "✅ Promo [np]"
                        return "—"

                    disp_pp = gpp.copy()
                    disp_pp["promozione"] = disp_pp.apply(_prime_promo_badge, axis=1)
                    if "avg_unit_price" in disp_pp.columns:
                        disp_pp["avg_unit_price"] = pd.to_numeric(disp_pp["avg_unit_price"], errors="coerce") \
                            .apply(lambda x: f"{x:.1f}€" if pd.notna(x) else "")
                    disp_pp = disp_pp.rename(columns={
                        "product_name": "Prodotto",
                        "avg_unit_price": "Prezzo €",
                        "total_product_sold": "Qtà venduta",
                    })
                    show_pp = ["Prodotto", "promozione", "Prezzo €", "Qtà venduta"]
                    show_pp = [c for c in show_pp if c in disp_pp.columns]
                    n_prime = (gpp.get("has_active_promo_p", pd.Series(dtype=str)).str.upper() == "Y").sum()
                    n_np    = (gpp.get("has_active_promo_np", pd.Series(dtype=str)).str.upper() == "Y").sum()
                    st.caption(f"{len(gpp)} prodotti · {n_prime} ⭐ prime · {n_np} ✅ non-prime")
                    _promo_flags_p  = _safe_flags(gpp, "has_active_promo_p").reindex(disp_pp.index).fillna(False)
                    _promo_flags_np = _safe_flags(gpp, "has_active_promo_np").reindex(disp_pp.index).fillna(False)
                    _pp_df = disp_pp[show_pp]
                    _pp_styles = [
                        "background:#ede9fe;color:#4c1d95" if _promo_flags_p.get(idx, False)
                        else "background:#FFF8D0;color:#7a6300" if _promo_flags_np.get(idx, False)
                        else ""
                        for idx in _pp_df.index
                    ]
                    st.markdown(
                        _products_table_html(_pp_df, "#7c3aed", "#ffffff", _pp_styles),
                        unsafe_allow_html=True,
                    )

        # ---- Deliveroo ----
        with col_d:
            if _b64_roo:
                st.markdown(
                    f"<div style='display:inline-flex;align-items:center;gap:8px;"
                    f"background:#00CCBC;color:white;padding:5px 14px;"
                    f"border-radius:8px;font-weight:700;font-size:1rem'>"
                    f"<img src='data:image/png;base64,{_b64_roo}' style='height:22px;width:22px;object-fit:contain'>"
                    f"Deliveroo</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown("<span style='background:#00A082;color:white;padding:4px 12px;border-radius:6px;font-weight:700'>🛒 Deliveroo</span>", unsafe_allow_html=True)
            st.write("")
            if not deliveroo_nm:
                st.info("Store non matchato con Deliveroo.\nAssegna un match nel tab Store Matching.")
            elif dp.empty:
                roo_promo_text = str(latest.get("deliveroo_promo_text", "")).strip()
                roo_rank_label = str(latest.get("deliveroo_rank_label", "")).strip()
                if roo_promo_text and roo_promo_text not in ("", "nan", "Nessuna promo"):
                    # Promo esiste a livello ristorante ma non sono disponibili dettagli prodotto
                    # (es. 2x1 e consegna gratis non hanno prodotti specifici da elencare)
                    st.markdown(
                        f"<div style='background:#e0f7f4;border-left:4px solid #00CCBC;"
                        f"padding:12px 16px;border-radius:6px;margin-top:4px'>"
                        f"<b>Promozione rilevata:</b> {roo_promo_text}<br>"
                        f"<span style='color:#444;font-size:0.88em'>Il tipo di promo "
                        f"(<em>{roo_rank_label}</em>) si applica al ristorante nel suo complesso — "
                        f"non sono disponibili dettagli per singolo prodotto.</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.info("Nessuna promozione Deliveroo rilevata per questo store.")
            else:
                disp_d = dp.copy()
                disp_d = disp_d.rename(columns={
                    "product_name":        "Prodotto",
                    "product_description": "Descrizione",
                    "product_price":       "Prezzo",
                    "promotion_type":      "Promozione",
                })
                show_cols_d = ["Prodotto", "Promozione", "Prezzo", "Descrizione"]
                show_cols_d = [c for c in show_cols_d if c in disp_d.columns]

                has_promo_col = "Promozione" in disp_d.columns
                n_promo_d = (disp_d["Promozione"] != "").sum() if has_promo_col else 0
                st.caption(f"{len(dp)} prodotti · {n_promo_d} in promozione")
                _d_df = disp_d[show_cols_d]
                _d_styles = [
                    "background:#d0f0ea;color:#00614e"
                    if (has_promo_col and disp_d.loc[idx, "Promozione"] != "")
                    else ""
                    for idx in _d_df.index
                ]
                st.markdown(
                    _products_table_html(_d_df, "#00CCBC", "#ffffff", _d_styles),
                    unsafe_allow_html=True,
                )


# ---------------------------------------------------------------------------
# TAB 3 — Trend
# ---------------------------------------------------------------------------

def tab_trend(sel_weeks, sel_cities, sel_am=None):
    import base64 as _b64mod
    _icon = ROOT / "assets" / "growth.png"
    if _icon.exists():
        _b64 = _b64mod.b64encode(_icon.read_bytes()).decode()
        st.markdown(
            f"""<div style='display:flex;align-items:center;gap:10px;margin-bottom:4px'>
                  <img src='data:image/png;base64,{_b64}' style='width:42px;height:42px;object-fit:contain'>
                  <h2 style='margin:0;padding:0'>Trend Settimanale</h2>
                </div>""",
            unsafe_allow_html=True,
        )
    else:
        st.header("📈 Trend Settimanale")
    st.caption("Evoluzione della parity nel tempo (tutte le settimane disponibili)")

    # Se filtro AM attivo: ricalcola city_parity dagli store di quell'AM
    _am_stores_t = _am_filtered_stores(sel_am)
    if _am_stores_t is not None:
        _sp_t = load_store_parity()
        if not _sp_t.empty:
            _sp_t_am = _sp_t[_sp_t.apply(
                lambda r: (str(r.get("city_code","")).strip(), str(r.get("glovo_name","")).strip())
                          in _am_stores_t, axis=1
            )]
            city_df = _recompute_city_from_stores(_sp_t_am)
        else:
            city_df = pd.DataFrame()
        if sel_am:
            st.info(f"👤 Filtro AM attivo: **{sel_am}**")
    else:
        city_df = load_city_parity()

    if city_df.empty:
        st.info("Nessun dato disponibile.")
        return

    df = city_df.copy()
    if sel_cities and "city_code" in df.columns:
        df = df[df["city_code"].isin(sel_cities)]

    if df.empty or "week_num" not in df.columns:
        st.warning("Nessun dato per le città selezionate.")
        return

    # Aggregato Italia: media pesata per numero store
    agg = df.groupby("week_num").agg(
        w_superiority=("w_superiority", "mean"),
        w_parity=("w_parity", "mean"),
        w_inferiority=("w_inferiority", "mean"),
        n_stores_matched=("n_stores_matched", "sum"),
    ).reset_index().sort_values("week_num")

    # ---- Area chart Italia ----
    st.subheader("Composizione parity Italia (tutte le città selezionate)")
    area_df = agg.melt(
        id_vars="week_num",
        value_vars=["w_superiority", "w_parity", "w_inferiority"],
        var_name="tipo", value_name="pct"
    )
    area_df["tipo"] = area_df["tipo"].map({
        "w_superiority": "SUPERIORITY",
        "w_parity":      "PARITY",
        "w_inferiority": "INFERIORITY",
    })
    fig_area = px.area(
        area_df, x="week_num", y="pct", color="tipo",
        color_discrete_map=PARITY_COLORS,
        category_orders={"tipo": ["SUPERIORITY", "PARITY", "INFERIORITY"]},
        labels={"week_num": "Settimana", "pct": "% Revenue", "tipo": ""},
    )
    fig_area.update_layout(height=350, margin=dict(t=20))
    st.plotly_chart(fig_area, use_container_width=True)

    # ---- Week-over-week changes (#4) ----
    st.subheader("Variazione settimana su settimana")
    if len(agg) >= 2:
        last = agg.iloc[-1]
        prev = agg.iloc[-2]
        sup_d = last["w_superiority"] - prev["w_superiority"]
        par_d = last["w_parity"]      - prev["w_parity"]
        inf_d = last["w_inferiority"] - prev["w_inferiority"]
        d1, d2, d3 = st.columns(3)
        d1.metric(
            f"SUPERIORITY — {last['week_num']}",
            f"{last['w_superiority']:.1f}%",
            f"{sup_d:+.1f}pp vs {prev['week_num']}",
        )
        d2.metric(
            f"PARITY — {last['week_num']}",
            f"{last['w_parity']:.1f}%",
            f"{par_d:+.1f}pp vs {prev['week_num']}",
        )
        d3.metric(
            f"INFERIORITY — {last['week_num']}",
            f"{last['w_inferiority']:.1f}%",
            f"{inf_d:+.1f}pp vs {prev['week_num']}",
            delta_color="inverse",
        )

        # Tabella delta per città
        if len(df["city_code"].unique()) > 1:
            with st.expander("Delta per città"):
                weeks_sorted = sorted(df["week_num"].unique())
                if len(weeks_sorted) >= 2:
                    wk_last = weeks_sorted[-1]
                    wk_prev = weeks_sorted[-2]
                    df_last = df[df["week_num"] == wk_last].set_index("city_code")
                    df_prev = df[df["week_num"] == wk_prev].set_index("city_code")
                    common_cities = df_last.index.intersection(df_prev.index)
                    delta_rows = []
                    for city in sorted(common_cities):
                        delta_rows.append({
                            "Città":        city,
                            "SUP Δ (pp)":   round(df_last.loc[city, "w_superiority"] - df_prev.loc[city, "w_superiority"], 1),
                            "PAR Δ (pp)":   round(df_last.loc[city, "w_parity"]      - df_prev.loc[city, "w_parity"],      1),
                            "INF Δ (pp)":   round(df_last.loc[city, "w_inferiority"] - df_prev.loc[city, "w_inferiority"], 1),
                        })
                    delta_df = pd.DataFrame(delta_rows)

                    def _color_delta(val):
                        if not isinstance(val, (int, float)):
                            return ""
                        if val > 0:
                            return "color: #00614e; font-weight: 600"
                        if val < 0:
                            return "color: #991b1b; font-weight: 600"
                        return ""

                    def _color_inf_delta(val):
                        if not isinstance(val, (int, float)):
                            return ""
                        if val < 0:
                            return "color: #00614e; font-weight: 600"
                        if val > 0:
                            return "color: #991b1b; font-weight: 600"
                        return ""

                    styled = delta_df.style \
                        .map(_color_delta,     subset=["SUP Δ (pp)", "PAR Δ (pp)"]) \
                        .map(_color_inf_delta, subset=["INF Δ (pp)"])
                    st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.info("Serve almeno 2 settimane di dati per il confronto.")

    # ---- Line chart per città ----
    st.subheader("Parity Score per città nel tempo")
    st.caption("Score = % Superiority − % Inferiority (revenue-weighted)")

    df["parity_score"] = df["w_superiority"] - df["w_inferiority"]
    fig_line = px.line(
        df.sort_values("week_num"),
        x="week_num", y="parity_score",
        color="city_code",
        markers=True,
        labels={"week_num": "Settimana", "parity_score": "Parity Score", "city_code": "Città"},
    )
    fig_line.add_hline(y=0, line_dash="dash", line_color="gray", annotation_text="Parità")
    fig_line.update_layout(height=400, margin=dict(t=20))
    st.plotly_chart(fig_line, use_container_width=True)

    # ---- Tabella storica ----
    with st.expander("Dati storici completi"):
        df_hist = df.sort_values(["week_num", "city_code"]).copy()

        # Elimina colonna id se presente
        if "id" in df_hist.columns:
            df_hist = df_hist.drop(columns=["id"])

        # Formatta percentuali
        for col in ["pct_superiority", "pct_parity", "pct_inferiority",
                    "w_superiority", "w_parity", "w_inferiority",
                    "match_coverage_pct"]:
            if col in df_hist.columns:
                df_hist[col] = pd.to_numeric(df_hist[col], errors="coerce") \
                    .apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "")

        # Aggiungi parity_score se non già presente
        if "parity_score" not in df_hist.columns and "w_superiority" in df_hist.columns and "w_inferiority" in df_hist.columns:
            def _parse_pct(v):
                try:
                    return float(str(v).replace("%", "").strip())
                except Exception:
                    return 0.0
            df_hist["parity_score"] = df_hist.apply(
                lambda r: round(_parse_pct(r["w_superiority"]) - _parse_pct(r["w_inferiority"]), 1),
                axis=1,
            ).apply(lambda x: f"{x:+.1f}pp")

        # Rinomina colonne
        df_hist = df_hist.rename(columns={
            "city_code":          "City Code",
            "week_num":           "Week",
            "n_stores_total":     "Total Stores",
            "n_stores_matched":   "Matched",
            "n_unmatched":        "Unmatched",
            "n_superiority":      "Superiority",
            "n_parity":           "Parity",
            "n_inferiority":      "Inferiority",
            "pct_superiority":    "Superiority (%)",
            "pct_parity":         "Parity (%)",
            "pct_inferiority":    "Inferiority (%)",
            "w_superiority":      "Superiority (weight)",
            "w_parity":           "Parity (weight)",
            "w_inferiority":      "Inferiority (weight)",
            "city_parity_label":  "City Status",
            "match_coverage_pct": "Match Coverage",
            "inserted_at":        "Inserted at",
            "parity_score":       "Parity Score",
        })

        st.dataframe(df_hist, column_config=_col_config_from_data(df_hist), use_container_width=True, hide_index=True)

    # ---- Breakdown per tipo di promo (#8) ----
    st.divider()
    st.subheader("Breakdown per tipo di promo")
    st.caption("Quanti store usano ciascuna meccanica promozionale (ultima settimana disponibile, store matchati)")

    store_full = load_store_parity()
    if not store_full.empty:
        latest_wk  = store_full["week_num"].max()
        s_latest   = store_full[store_full["week_num"] == latest_wk].copy()
        if sel_cities:
            s_latest = s_latest[s_latest["city_code"].isin(sel_cities)]
        matched_s  = s_latest[~s_latest["parity"].isin(["UNMATCHED", "EXCLUSIVE_GLOVO"])]

        col_g8, col_d8 = st.columns(2)

        with col_g8:
            if "glovo_rank_label" in matched_s.columns:
                g_counts = (
                    matched_s["glovo_rank_label"]
                    .replace("", pd.NA).dropna()
                    .value_counts()
                    .reset_index()
                )
                g_counts.columns = ["Tipo Promo", "Store"]
                if not g_counts.empty:
                    fig_g8 = px.bar(
                        g_counts, x="Tipo Promo", y="Store",
                        title="Glovo — meccaniche promo",
                        color_discrete_sequence=["#F2CC38"],
                        text="Store",
                    )
                    fig_g8.update_traces(textposition="outside", marker_line_color="#c9a800", marker_line_width=1)
                    fig_g8.update_layout(height=370, margin=dict(t=50, b=10), showlegend=False,
                                         plot_bgcolor="white", yaxis_title="N. store")
                    st.plotly_chart(fig_g8, use_container_width=True)

        with col_d8:
            if "deliveroo_rank_label" in matched_s.columns:
                d_counts = (
                    matched_s["deliveroo_rank_label"]
                    .replace("", pd.NA).dropna()
                    .value_counts()
                    .reset_index()
                )
                d_counts.columns = ["Tipo Promo", "Store"]
                if not d_counts.empty:
                    fig_d8 = px.bar(
                        d_counts, x="Tipo Promo", y="Store",
                        title="Deliveroo — meccaniche promo",
                        color_discrete_sequence=["#00CCBC"],
                        text="Store",
                    )
                    fig_d8.update_traces(textposition="outside", marker_line_color="#009e91", marker_line_width=1)
                    fig_d8.update_layout(height=370, margin=dict(t=50, b=10), showlegend=False,
                                         plot_bgcolor="white", yaxis_title="N. store")
                    st.plotly_chart(fig_d8, use_container_width=True)


# ---------------------------------------------------------------------------
# TAB 4 — Store Matching
# ---------------------------------------------------------------------------

def tab_store_matching():
    import base64 as _b64mod
    _icon = ROOT / "assets" / "twoBagsYellowCheck.png"
    if _icon.exists():
        _b64 = _b64mod.b64encode(_icon.read_bytes()).decode()
        st.markdown(
            f"""<div style='display:flex;align-items:center;gap:10px;margin-bottom:4px'>
                  <img src='data:image/png;base64,{_b64}' style='width:42px;height:42px;object-fit:contain'>
                  <h2 style='margin:0;padding:0'>Store Matching</h2>
                </div>""",
            unsafe_allow_html=True,
        )
    else:
        st.header("🔗 Store Matching")

    # Feedback ultima operazione
    if "last_save_msg" in st.session_state:
        kind, msg = st.session_state.pop("last_save_msg")
        if kind == "ok":
            st.success(f"✅ {msg}")
        else:
            st.error(f"❌ Errore: {msg}")

    # Pulsante refresh manuale — utile dopo sync bulk
    if st.button("🔄 Aggiorna dati", key="refresh_matching"):
        clear_cache()
        st.rerun()

    store_df    = load_store_parity()
    mapping_df  = load_store_mapping()
    deliv_names = load_deliveroo_names_by_city()

    # ── KPI ──────────────────────────────────────────────────────────────────
    latest_week = store_df["week_num"].max() if not store_df.empty else ""
    sp = store_df[store_df["week_num"] == latest_week] if latest_week else store_df

    n_da_matchare = int((sp["parity"] == "UNMATCHED").sum()) if not sp.empty else 0
    n_matchati    = int(sp["parity"].isin(["SUPERIORITY","PARITY","INFERIORITY"]).sum()) if not sp.empty else 0
    n_exclusive   = int((mapping_df["source"] == "manual_rejected").sum()) if not mapping_df.empty else 0
    n_not_deliv   = int((mapping_df["source"] == "not_on_deliveroo").sum()) if not mapping_df.empty else 0

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("🔍 Da matchare",        n_da_matchare)
    k2.metric("✅ Matchati",            n_matchati)
    k3.metric("⭐ Esclusiva Glovo",     n_exclusive)
    k4.metric("🚫 Non su Deliveroo",   n_not_deliv)
    st.divider()

    # =========================================================================
    # SEZIONE 1 — Store UNMATCHED da gestire
    # =========================================================================
    unmatched_df = sp[sp["parity"] == "UNMATCHED"].copy() if not sp.empty else pd.DataFrame()

    st.subheader(f"🔍 Store da matchare  ({len(unmatched_df)})")
    if unmatched_df.empty:
        st.success("Nessuno store da matchare per la settimana corrente!")
    else:
        # Filtri
        col_f1, col_f2 = st.columns([1, 3])
        with col_f1:
            cities_u = ["Tutte"] + sorted(unmatched_df["city_code"].unique())
            sel_city_u = st.selectbox("Città", cities_u, key="unm_city")
        with col_f2:
            search_u = st.text_input("🔍 Cerca store Glovo", "", key="unm_search")

        df_u = unmatched_df if sel_city_u == "Tutte" else unmatched_df[unmatched_df["city_code"] == sel_city_u]
        if search_u:
            df_u = df_u[df_u["glovo_name"].str.contains(search_u, case=False, na=False)]
        df_u = df_u.sort_values("revenue", ascending=False)

        # Tabella navigabile
        disp_u = df_u[["city_code","glovo_name","revenue"]].rename(
            columns={"city_code":"Città","glovo_name":"Store Glovo","revenue":"Revenue €"}
        )
        st.dataframe(disp_u, use_container_width=True, hide_index=True, height=220)

        # Selezione store
        st.markdown("**Seleziona uno store per gestirlo:**")
        if df_u.empty:
            st.info("Nessuno store con questi filtri.")
        else:
            sel_glovo = st.selectbox(
                "Store Glovo",
                options=df_u["glovo_name"].tolist(),
                format_func=lambda n: f"{df_u[df_u['glovo_name']==n]['city_code'].iloc[0]} | {n}",
                key="unm_glovo"
            )
            sel_city_store = df_u[df_u["glovo_name"] == sel_glovo]["city_code"].iloc[0]

            st.markdown(f"**Store selezionato:** `{sel_city_store}` — `{sel_glovo}`")

            col_match, col_btn = st.columns([3, 2])
            with col_match:
                # Lista + testo libero
                city_opts   = deliv_names.get(sel_city_store, [])
                deliv_input = st.text_input(
                    "Nome Deliveroo",
                    placeholder="Scrivi il nome esatto su Deliveroo…",
                    key="unm_deliv_text"
                )
                if city_opts:
                    deliv_list = st.selectbox(
                        "…oppure scegli dalla lista scrappata",
                        options=["— scegli —"] + city_opts,
                        key="unm_deliv_list"
                    )
                    final_deliv = deliv_input.strip() or (
                        "" if deliv_list == "— scegli —" else deliv_list
                    )
                else:
                    final_deliv = deliv_input.strip()

                if st.button("✅ Match", type="primary", key="unm_match", disabled=not final_deliv):
                    _run_save(save_confirmed_match, sel_city_store, sel_glovo, final_deliv,
                              success_msg=f"Match: {sel_glovo} → {final_deliv}")

            with col_btn:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                st.markdown("&nbsp;", unsafe_allow_html=True)
                if st.button("🚫 NON su Deliveroo", key="unm_not_deliv", use_container_width=True):
                    _run_save(save_not_on_deliveroo, sel_city_store, sel_glovo,
                              success_msg=f"{sel_glovo} → Non su Deliveroo")
                st.markdown("")
                if st.button("⭐ Esclusiva Glovo", key="unm_exclusive", use_container_width=True):
                    _run_save(save_glovo_exclusive, sel_city_store, sel_glovo,
                              success_msg=f"{sel_glovo} → Esclusiva Glovo")

    st.divider()

    # =========================================================================
    # SEZIONE 2 — Modifica store già classificati (matchati / esclusivi)
    # =========================================================================
    st.subheader("✏️ Modifica store già classificati")

    if mapping_df.empty:
        st.info("Nessun mapping ancora.")
    else:
        col_f1, col_f2, col_f3 = st.columns([1, 1, 2])
        with col_f1:
            src_options = ["Tutti","Matchati","Esclusiva Glovo","Non su Deliveroo"]
            sel_src_edit = st.selectbox("Tipo", src_options, key="edit_src")
        with col_f2:
            cities_e = ["Tutte"] + sorted(mapping_df["city_code"].unique())
            sel_city_e = st.selectbox("Città", cities_e, key="edit_city")
        with col_f3:
            search_e = st.text_input("🔍 Cerca store", "", key="edit_search")

        df_e = mapping_df.copy()
        if sel_src_edit == "Matchati":
            df_e = df_e[df_e["deliveroo_name"].str.strip() != ""]
        elif sel_src_edit == "Esclusiva Glovo":
            df_e = df_e[df_e["source"] == "manual_rejected"]
        elif sel_src_edit == "Non su Deliveroo":
            df_e = df_e[df_e["source"] == "not_on_deliveroo"]
        if sel_city_e != "Tutte":
            df_e = df_e[df_e["city_code"] == sel_city_e]
        if search_e:
            df_e = df_e[df_e["glovo_name"].str.contains(search_e, case=False, na=False)]

        st.dataframe(
            df_e[["city_code","glovo_name","deliveroo_name","source"]].rename(
                columns={"city_code":"Città","glovo_name":"Store Glovo",
                         "deliveroo_name":"Store Deliveroo","source":"Tipo"}
            ),
            use_container_width=True, hide_index=True, height=200
        )

        if not df_e.empty:
            st.markdown("**Seleziona uno store per cambiarne lo status:**")
            sel_edit = st.selectbox(
                "Store da modificare",
                options=df_e["glovo_name"].tolist(),
                format_func=lambda n: f"{df_e[df_e['glovo_name']==n]['city_code'].iloc[0]} | {n}  [{df_e[df_e['glovo_name']==n]['source'].iloc[0]}]",
                key="edit_store"
            )
            edit_city = df_e[df_e["glovo_name"] == sel_edit]["city_code"].iloc[0]
            edit_src  = df_e[df_e["glovo_name"] == sel_edit]["source"].iloc[0]

            st.markdown(f"**Store:** `{edit_city}` — `{sel_edit}`  |  Status attuale: `{edit_src}`")

            col_e1, col_e2, col_e3, col_e4 = st.columns(4)
            with col_e1:
                new_deliv_name = st.text_input("Nuovo nome Deliveroo", key="edit_new_deliv",
                                               placeholder="Es: Pizzeria da Paolo")
            with col_e2:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                st.markdown("&nbsp;", unsafe_allow_html=True)
                if st.button("✅ Cambia match", key="edit_match",
                             disabled=not new_deliv_name.strip()):
                    _run_save(save_confirmed_match, edit_city, sel_edit, new_deliv_name.strip(),
                              success_msg=f"Match aggiornato: {sel_edit} → {new_deliv_name.strip()}")
            with col_e3:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                st.markdown("&nbsp;", unsafe_allow_html=True)
                if st.button("🚫 NON su Deliveroo", key="edit_not_deliv",
                             use_container_width=True):
                    _run_save(save_not_on_deliveroo, edit_city, sel_edit,
                              success_msg=f"{sel_edit} → Non su Deliveroo")
            with col_e4:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                st.markdown("&nbsp;", unsafe_allow_html=True)
                if st.button("⭐ Esclusiva Glovo", key="edit_exclusive",
                             use_container_width=True):
                    _run_save(save_glovo_exclusive, edit_city, sel_edit,
                              success_msg=f"{sel_edit} → Esclusiva Glovo")

        st.download_button(
            "📥 Esporta store_mapping.csv",
            data=mapping_df.to_csv(index=False).encode("utf-8"),
            file_name="store_mapping.csv", mime="text/csv",
        )


# ---------------------------------------------------------------------------
# TAB 6 — Pipeline: azioni prioritarie + salute pipeline
# ---------------------------------------------------------------------------

_GLOVO_YELLOW    = "#FFC244"   # giallo Glovo
_DELIVEROO_BLUE  = "#00CCBC"   # teal Deliveroo

# Mapping colonna → etichetta leggibile + brand
_PA_COLS = [
    ("priority",           "#",              None),
    ("city_code",          "Città",          None),
    ("glovo_name",         "Store Glovo",    "glovo"),
    ("deliveroo_name",     "Store Deliveroo","deliveroo"),
    ("glovo_rank_label",   "Promo Glovo",    "glovo"),
    ("deliveroo_rank_label","Promo Deliveroo","deliveroo"),
    ("revenue",            "Revenue (€)",    None),
    ("glovo_pct_off",      "% Glovo",        "glovo"),
    ("deliveroo_pct_off",  "% Deliveroo",    "deliveroo"),
    ("promo_coverage_pct", "Copertura %",    None),
    ("week_num",           "Settimana",      None),
]


def _priority_table_html(df: pd.DataFrame) -> str:
    """Genera tabella HTML con header colorati per brand e colonne a larghezza fissa uguale."""
    cols_in_df = [(col, label, brand) for col, label, brand in _PA_COLS if col in df.columns]
    n = len(cols_in_df)
    col_w = f"{100 / n:.1f}%"

    # Header
    hdr = ""
    for _, label, brand in cols_in_df:
        if brand == "glovo":
            bg, fg = _GLOVO_YELLOW, "#1a1a1a"
        elif brand == "deliveroo":
            bg, fg = _DELIVEROO_BLUE, "#ffffff"
        else:
            bg, fg = "#e8eaed", "#1a1a1a"
        hdr += (
            f'<th style="background:{bg};color:{fg};text-align:center;'
            f'padding:8px 4px;font-size:12px;font-weight:600;'
            f'width:{col_w};white-space:nowrap;border:1px solid #d1d5db">'
            f'{label}</th>'
        )

    # Rows
    body = ""
    for i, (_, row) in enumerate(df.iterrows()):
        bg_row = "#ffffff" if i % 2 == 0 else "#f9fafb"
        cells = ""
        for col, _, _ in cols_in_df:
            val = row.get(col, "")
            if pd.isna(val):
                val = "—"
            elif col == "revenue":
                try:
                    val = f"€ {float(val):,.0f}"
                except Exception:
                    pass
            elif col in ("glovo_pct_off", "deliveroo_pct_off", "promo_coverage_pct"):
                try:
                    val = f"{float(val):.1f}%"
                except Exception:
                    val = "—" if str(val).strip() == "" else val
            cells += (
                f'<td style="background:{bg_row};text-align:center;'
                f'padding:7px 4px;font-size:12px;border:1px solid #e5e7eb;'
                f'width:{col_w}">{val}</td>'
            )
        body += f"<tr>{cells}</tr>"

    return (
        '<div style="overflow-x:auto;margin-top:8px">'
        f'<table style="width:100%;table-layout:fixed;border-collapse:collapse">'
        f"<thead><tr>{hdr}</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table></div>"
    )


def tab_pipeline(sel_weeks: list[str], sel_cities: list[str], sel_am=None) -> None:
    import base64 as _b64mod
    _icon = ROOT / "assets" / "redFlag.png"
    if _icon.exists():
        _b64 = _b64mod.b64encode(_icon.read_bytes()).decode()
        st.markdown(
            f"""<div style='display:flex;align-items:center;gap:10px;margin-bottom:4px'>
                  <img src='data:image/png;base64,{_b64}' style='width:42px;height:42px;object-fit:contain'>
                  <h2 style='margin:0;padding:0'>Azioni Prioritarie</h2>
                </div>""",
            unsafe_allow_html=True,
        )
    else:
        st.header("🎯 Azioni Prioritarie")
    st.caption(
        "Store in **INFERIORITY** ordinati per revenue decrescente — "
        "quelli con impatto economico più alto da allineare subito."
    )

    df = load_priority_actions()

    if df.empty:
        st.info("Nessuna azione prioritaria disponibile. "
                "Esegui la pipeline per aggiornare i dati.")
    else:
        # Filtra per settimana/città se applicabile
        if sel_weeks and "week_num" in df.columns:
            df = df[df["week_num"].isin(sel_weeks)]
        if sel_cities and "city_code" in df.columns:
            df = df[df["city_code"].isin(sel_cities)]

        # Filtro AM — mantieni solo store dell'AM selezionato
        _am_stores_p = _am_filtered_stores(sel_am)
        if _am_stores_p is not None and "glovo_name" in df.columns:
            df = df[df.apply(
                lambda r: (str(r.get("city_code","")).strip(), str(r.get("glovo_name","")).strip())
                          in _am_stores_p, axis=1
            )]
            if sel_am:
                st.info(f"👤 Filtro AM attivo: **{sel_am}**")

        # KPI in cima
        n_stores = len(df)
        total_rev = pd.to_numeric(df.get("revenue", pd.Series(dtype=float)), errors="coerce").sum()
        k1, k2 = st.columns(2)
        k1.metric("Store prioritari", n_stores)
        k2.metric("Revenue totale a rischio", f"€ {total_rev:,.0f}" if total_rev > 0 else "n/d")

        st.divider()

        # Formatta revenue come numerico nel DF base (per download)
        disp = df.copy()
        if "revenue" in disp.columns:
            disp["revenue"] = pd.to_numeric(disp["revenue"], errors="coerce")

        # Tabella HTML stilizzata
        st.markdown(_priority_table_html(disp), unsafe_allow_html=True)

        st.download_button(
            "📥 Esporta azioni prioritarie CSV",
            data=disp[[c for c, _, _ in _PA_COLS if c in disp.columns]].to_csv(index=False).encode("utf-8"),
            file_name=f"priority_actions_{sel_weeks[-1] if sel_weeks else 'latest'}.csv",
            mime="text/csv",
        )

    # ---- Salute pipeline ----
    st.divider()
    st.header("🚦 Salute Pipeline")
    st.caption("Anomalie e check automatici eseguiti a ogni run della pipeline.")

    health = load_pipeline_health()

    if health.empty:
        st.info(
            "Nessun report di salute disponibile. "
            "Il report viene generato automaticamente a ogni esecuzione della pipeline."
        )
    else:
        # Filtra per settimana se applicabile
        if sel_weeks and "week_num" in health.columns:
            health = health[health["week_num"].isin(sel_weeks)]

        if health.empty:
            st.info("Nessun dato per i filtri selezionati.")
        else:
            latest_week = health["week_num"].max() if "week_num" in health.columns else ""
            latest = health[health["week_num"] == latest_week] if latest_week else health

            # Sommario testuale
            errors   = latest[latest["level"] == "ERROR"]   if "level" in latest.columns else pd.DataFrame()
            warnings = latest[latest["level"] == "WARNING"]  if "level" in latest.columns else pd.DataFrame()

            if len(errors) > 0:
                st.error(f"🔴 **{len(errors)} errore/i** rilevato/i nell'ultima run ({latest_week})")
            elif len(warnings) > 0:
                st.warning(f"🟡 **{len(warnings)} warning** nell'ultima run ({latest_week})")
            else:
                st.success(f"✅ Pipeline OK — nessun problema ({latest_week})")

            # Colora per livello
            level_colors = {
                "ERROR":   "background-color: #fee2e2; color: #991b1b",
                "WARNING": "background-color: #fef9c3; color: #713f12",
                "INFO":    "background-color: #f0fdf4; color: #166534",
            }

            disp_h = latest.reset_index(drop=True)
            st.dataframe(
                disp_h,
                use_container_width=True,
                hide_index=True,
                height=min(60 + 35 * len(disp_h), 500),
            )

            # Storico settimane
            with st.expander("📅 Storico completo"):
                st.dataframe(health, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not check_password():
        st.stop()


    # DB check solo in modalita' locale
    if not _is_cloud_mode() and not DB_PATH.exists():
        st.error(
            f"Database non trovato: `{DB_PATH}`\n\n"
            "Esegui prima la pipeline:\n"
            "```\n"
            "python -m pipeline.run_weekly --glovo-csv <path_al_csv_glovo>\n"
            "```"
        )
        st.stop()

    sel_weeks, sel_cities, sel_am = sidebar()

    # Icone custom nei tab via CSS injection
    import base64 as _b64mod

    def _icon_b64(name: str) -> str:
        p = ROOT / "assets" / name
        return _b64mod.b64encode(p.read_bytes()).decode() if p.exists() else ""

    _b64_promo    = _icon_b64("promoZone.png")
    _b64_store    = _icon_b64("storePhone.png")
    _b64_trend    = _icon_b64("growth.png")
    _b64_matching = _icon_b64("twoBagsYellowCheck.png")
    _b64_prime    = _icon_b64("isotypeCoinsLoyalty.png")
    _b64_azioni   = _icon_b64("redFlag.png")

    _css_tabs = """<style>
    /* ── Montserrat font ── */
    @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap');
    html { font-family: 'Montserrat', sans-serif !important; }

    /* Centra testo in tutte le celle delle tabelle */
    div[data-testid="stDataFrame"] td,
    div[data-testid="stDataFrame"] th {
        text-align: center !important;
    }

    /* Multiselect tags → teal Glovo */
    span[data-baseweb="tag"] {
        background-color: #00A082 !important;
        color: white !important;
    }
    span[data-baseweb="tag"] span {
        color: white !important;
    }
    /* X button del tag */
    span[data-baseweb="tag"] [role="presentation"] svg {
        fill: white !important;
    }
    </style>"""
    st.markdown(_css_tabs, unsafe_allow_html=True)
    _css_tabs = "<style>"
    if _b64_promo:
        _css_tabs += f"""
        div[data-testid="stTabs"] button[role="tab"]:nth-child(1)::before {{
            content:''; display:inline-block; width:18px; height:18px;
            background-image:url('data:image/png;base64,{_b64_promo}');
            background-size:contain; background-repeat:no-repeat;
            vertical-align:middle; margin-right:5px;
        }}"""
    if _b64_store:
        _css_tabs += f"""
        div[data-testid="stTabs"] button[role="tab"]:nth-child(2)::before {{
            content:''; display:inline-block; width:18px; height:18px;
            background-image:url('data:image/png;base64,{_b64_store}');
            background-size:contain; background-repeat:no-repeat;
            vertical-align:middle; margin-right:5px;
        }}"""
    if _b64_trend:
        _css_tabs += f"""
        div[data-testid="stTabs"] button[role="tab"]:nth-child(3)::before {{
            content:''; display:inline-block; width:18px; height:18px;
            background-image:url('data:image/png;base64,{_b64_trend}');
            background-size:contain; background-repeat:no-repeat;
            vertical-align:middle; margin-right:5px;
        }}"""
    if _b64_matching:
        _css_tabs += f"""
        div[data-testid="stTabs"] button[role="tab"]:nth-child(4)::before {{
            content:''; display:inline-block; width:18px; height:18px;
            background-image:url('data:image/png;base64,{_b64_matching}');
            background-size:contain; background-repeat:no-repeat;
            vertical-align:middle; margin-right:5px;
        }}"""
    if _b64_prime:
        _css_tabs += f"""
        div[data-testid="stTabs"] button[role="tab"]:nth-child(5)::before {{
            content:''; display:inline-block; width:20px; height:20px;
            background-image:url('data:image/png;base64,{_b64_prime}');
            background-size:contain; background-repeat:no-repeat;
            vertical-align:middle; margin-right:5px;
        }}"""
    if _b64_azioni:
        _css_tabs += f"""
        div[data-testid="stTabs"] button[role="tab"]:nth-child(6)::before {{
            content:''; display:inline-block; width:18px; height:18px;
            background-image:url('data:image/png;base64,{_b64_azioni}');
            background-size:contain; background-repeat:no-repeat;
            vertical-align:middle; margin-right:5px;
        }}"""
    _css_tabs += "</style>"
    st.markdown(_css_tabs, unsafe_allow_html=True)

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "City Parity",
        "Store Detail",
        "Trend",
        "Store Matching",
        "Prime",
        "Azioni",
    ])

    with tab1:
        tab_city_parity(sel_weeks, sel_cities, sel_am=sel_am)
    with tab2:
        tab_store_detail(sel_weeks, sel_cities, sel_am=sel_am)
    with tab3:
        tab_trend(sel_weeks, sel_cities, sel_am=sel_am)
    with tab4:
        tab_store_matching()
    with tab5:
        tab_city_parity(sel_weeks, sel_cities, prime=True, sel_am=sel_am)
        st.divider()
        tab_store_detail(sel_weeks, sel_cities, prime=True, sel_am=sel_am)
    with tab6:
        tab_pipeline(sel_weeks, sel_cities, sel_am=sel_am)


if __name__ == "__main__":
    main()
