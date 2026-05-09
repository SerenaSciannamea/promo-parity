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

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT    = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "promo_parity.db"

PARITY_COLORS = {
    "SUPERIORITY": "#00A082",   # teal Glovo
    "PARITY":      "#F2CC38",   # giallo Glovo
    "INFERIORITY": "#ef4444",   # rosso
    "UNMATCHED":   "#94a3b8",   # grigio
}
PARITY_ORDER = ["SUPERIORITY", "PARITY", "INFERIORITY", "UNMATCHED"]

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

    st.markdown("""
        <div style='display:flex;flex-direction:column;align-items:center;
                    justify-content:center;padding:80px 0 40px'>
            <h1 style='font-size:2.2rem;margin-bottom:4px'>🛵 Promo Parity</h1>
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


def _local_deliveroo_products(city_code: str, restaurant_name: str) -> pd.DataFrame:
    p = ROOT / "output" / "deliveroo_promo_products.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, dtype=str).fillna("")
    df.columns = [c.strip().lower() for c in df.columns]
    mask = (df["city_code"] == city_code) & (df["restaurant_name"] == restaurant_name)
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


def _cloud_deliveroo_products() -> pd.DataFrame:
    return _cloud_load_all().get("deliveroo_products", pd.DataFrame())


def _cloud_deliveroo_names() -> dict[str, list[str]]:
    """Nel cloud usiamo i nomi Deliveroo gia' presenti nel store_parity."""
    sp = _cloud_store_parity()
    if sp.empty or "deliveroo_name" not in sp.columns:
        return {}
    result = {}
    for city, grp in sp[sp["deliveroo_name"] != ""].groupby("city_code"):
        result[city] = sorted(grp["deliveroo_name"].dropna().unique().tolist())
    return result


# ---------------------------------------------------------------------------
# Facade: funzioni uniformi usate dall'app
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_store_parity() -> pd.DataFrame:
    return _cloud_store_parity() if _is_cloud_mode() else _local_store_parity()


@st.cache_data(ttl=300)
def load_city_parity() -> pd.DataFrame:
    return _cloud_city_parity() if _is_cloud_mode() else _local_city_parity()


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


def load_glovo_products(city_code: str, store_name: str, week_num: str) -> pd.DataFrame:
    """Prodotti Glovo per uno store specifico. Non cachato (filtra live)."""
    if _is_cloud_mode():
        df = _cloud_glovo_products()
        if df.empty:
            return pd.DataFrame()
        mask = (
            (df["city_code"] == city_code)
            & (df["store_name"] == store_name)
            & (df["week_num"] == week_num)
        )
        cols = ["product_name", "type_of_promo", "has_active_promo",
                "avg_percentage_off", "avg_unit_price", "total_product_sold"]
        cols_present = [c for c in cols if c in df.columns]
        return df[mask][cols_present].sort_values(
            ["has_active_promo", "avg_unit_price"],
            ascending=[True, False],
        )
    return _local_glovo_products(city_code, store_name, week_num)


def load_deliveroo_products(city_code: str, restaurant_name: str) -> pd.DataFrame:
    """Prodotti Deliveroo per uno store specifico. Non cachato (filtra live)."""
    if not restaurant_name:
        return pd.DataFrame()
    if _is_cloud_mode():
        df = _cloud_deliveroo_products()
        if df.empty:
            return pd.DataFrame()
        mask = (df["city_code"] == city_code) & (df["restaurant_name"] == restaurant_name)
        cols = ["product_name", "product_description", "product_price", "promotion_type"]
        cols_present = [c for c in cols if c in df.columns]
        return df[mask][cols_present].drop_duplicates("product_name") if "product_name" in df.columns else df[mask][cols_present]
    return _local_deliveroo_products(city_code, restaurant_name)


# ---------------------------------------------------------------------------
# Scrittura mapping (funziona in entrambe le modalita')
# ---------------------------------------------------------------------------

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
    icons = {"SUPERIORITY": "🟢", "PARITY": "🟡", "INFERIORITY": "🔴", "UNMATCHED": "⚪"}
    colors = {"SUPERIORITY": "#00A082", "PARITY": "#b8960a", "INFERIORITY": "#ef4444", "UNMATCHED": "#94a3b8"}
    c = colors.get(label, "")
    style = f"color:{c};font-weight:600" if c else ""
    return f"{icons.get(label, '')} {label}"


def metric_delta_color(val: float) -> str:
    """Per le metric card: verde se >0, rosso se <0."""
    return "normal" if val >= 0 else "inverse"


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def sidebar() -> tuple[list[str], list[str]]:
    st.sidebar.markdown(
        """
        <div style='background:#F2CC38;border-radius:10px;padding:10px 16px;margin-bottom:12px;text-align:center'>
            <span style='font-size:1.6rem;font-weight:800;color:#161717;letter-spacing:1px'>🛵 Promo Parity</span><br>
            <span style='font-size:0.75rem;color:#161717;opacity:0.7'>Glovo vs Deliveroo</span>
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

    st.sidebar.divider()
    if st.sidebar.button("🔄 Aggiorna dati"):
        clear_cache()
        st.rerun()

    return sel_weeks, sel_cities


# ---------------------------------------------------------------------------
# TAB 1 — City Parity Overview
# ---------------------------------------------------------------------------

def tab_city_parity(sel_weeks, sel_cities):
    _icon = ROOT / "assets" / "promoZone.png"
    if _icon.exists():
        import base64
        _b64 = base64.b64encode(_icon.read_bytes()).decode()
        st.markdown(
            f"""<div style='display:flex;align-items:center;gap:10px;margin-bottom:4px'>
                  <img src='data:image/png;base64,{_b64}' style='width:42px;height:42px;object-fit:contain'>
                  <h2 style='margin:0;padding:0'>City Parity Overview</h2>
                </div>""",
            unsafe_allow_html=True,
        )
    else:
        st.header("📊 City Parity Overview")
    st.caption("Visione sintetica per città e settimana, pesata per fatturato Glovo")

    city_df = load_city_parity()
    if city_df.empty:
        st.info("Nessun dato disponibile. Esegui la pipeline settimanale.")
        return

    df = city_df.copy()
    if sel_weeks:
        df = df[df["week_num"].isin(sel_weeks)]
    if sel_cities:
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
    st.plotly_chart(fig_heat, use_container_width=True)

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
    st.dataframe(disp, use_container_width=True, hide_index=True)

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
        st.plotly_chart(fig_bar, use_container_width=True)


# ---------------------------------------------------------------------------
# TAB 2 — Store Detail
# ---------------------------------------------------------------------------

def tab_store_detail(sel_weeks, sel_cities):
    import base64 as _b64mod
    _icon = ROOT / "assets" / "storePhone.png"
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
    st.caption("Analisi per singolo store: promo Glovo vs Deliveroo, rank e copertura")

    store_df = load_store_parity()
    if store_df.empty:
        st.info("Nessun dato disponibile.")
        return

    df = store_df.copy()
    if sel_cities:
        df = df[df["city_code"].isin(sel_cities)]
    if sel_weeks:
        df = df[df["week_num"].isin(sel_weeks)]

    if df.empty:
        st.warning("Nessun dato per i filtri selezionati.")
        return

    # Filtri aggiuntivi
    col1, col2, col3 = st.columns(3)
    with col1:
        parity_filter = st.multiselect(
            "Parity", PARITY_ORDER, default=PARITY_ORDER,
            key="store_parity_filter"
        )
    with col2:
        search = st.text_input("Cerca store (nome Glovo)", "")
    with col3:
        sort_by = st.selectbox("Ordina per", ["revenue", "parity", "glovo_rank"], index=0)

    if parity_filter:
        df = df[df["parity"].isin(parity_filter)]
    if search:
        df = df[df["glovo_name"].str.contains(search, case=False, na=False)]

    df_sorted = df.sort_values(sort_by, ascending=(sort_by != "revenue"))

    # Tabella principale
    display_cols = [
        "city_code", "glovo_name", "deliveroo_name", "week_num",
        "parity",
        "glovo_rank_label", "glovo_pct_off", "glovo_promo_products",
        "deliveroo_rank_label", "deliveroo_promo_text",
        "revenue", "promo_coverage_pct"
    ]
    available = [c for c in display_cols if c in df_sorted.columns]
    disp = df_sorted[available].copy()

    # Formatta glovo_pct_off: 25.000000 → "25.0%"
    if "glovo_pct_off" in disp.columns:
        disp["glovo_pct_off"] = pd.to_numeric(disp["glovo_pct_off"], errors="coerce") \
            .apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "")

    def color_parity(val):
        colors = {
            "SUPERIORITY": "background-color: #d0f0ea; color: #00614e",
            "PARITY":      "background-color: #FFF8D0; color: #7a6300",
            "INFERIORITY": "background-color: #fee2e2; color: #991b1b",
            "UNMATCHED":   "background-color: #f1f5f9; color: #475569",
        }
        return colors.get(val, "")

    st.dataframe(
        disp.style.map(color_parity, subset=["parity"]),
        use_container_width=True,
        hide_index=True,
        height=500,
    )
    st.caption(f"Totale store visualizzati: {len(disp)}")

    # ---- Drill-down su singolo store ----
    st.divider()
    st.subheader("Drill-down store")

    store_names = sorted(df["glovo_name"].unique())
    sel_store   = st.selectbox("Seleziona store", ["— seleziona —"] + store_names)

    if sel_store != "— seleziona —":
        store_data = store_df[store_df["glovo_name"] == sel_store].sort_values("week_num")

        c1, c2 = st.columns(2)
        with c1:
            latest = store_data.iloc[-1]
            st.metric("Parity attuale", parity_badge(latest["parity"]))
            st.metric("Glovo promo", latest.get("glovo_rank_label", "—"))
            st.metric("Deliveroo promo", latest.get("deliveroo_rank_label", "—"))
        with c2:
            st.metric("Revenue settimana", f"€ {latest['revenue']:.0f}")
            st.metric("Prodotti in promo", int(latest.get("glovo_promo_products", 0)))
            st.metric("Copertura promo", f"{latest.get('promo_coverage_pct', 0):.1f}%")

        if len(store_data) > 1:
            fig_store = px.line(
                store_data, x="week_num", y="glovo_rank",
                markers=True, title="Evoluzione rank Glovo nel tempo",
                labels={"glovo_rank": "Rank Glovo (1=migliore)", "week_num": "Settimana"},
            )
            fig_store.update_yaxes(autorange="reversed", dtick=1)
            fig_store.update_layout(height=280, margin=dict(t=40))
            st.plotly_chart(fig_store, use_container_width=True)

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
        dp = load_deliveroo_products(city_code, deliveroo_nm)

        col_g, col_d = st.columns(2)

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
            if gp.empty:
                st.info("Dati prodotti Glovo non ancora disponibili.\nVerranno caricati al prossimo run della pipeline.")
            else:
                def _glovo_promo_badge(row):
                    if row.get("has_active_promo", "N") == "Y":
                        t = row.get("type_of_promo", "")
                        pct = row.get("avg_percentage_off")
                        if pct and pct > 0:
                            return f"✅ {t} ({pct:.0f}%)"
                        return f"✅ {t}" if t else "✅ Promo"
                    return "—"

                disp_g = gp.copy()
                disp_g["promozione"] = disp_g.apply(_glovo_promo_badge, axis=1)
                disp_g = disp_g.rename(columns={
                    "product_name":       "Prodotto",
                    "avg_unit_price":     "Prezzo €",
                    "total_product_sold": "Qtà venduta",
                })
                show_cols_g = ["Prodotto", "promozione", "Prezzo €", "Qtà venduta"]
                show_cols_g = [c for c in show_cols_g if c in disp_g.columns]

                def _hl_promo_g(row):
                    bg = "background-color: #fef9c3" if row.get("has_active_promo", "N") == "Y" else ""
                    return [bg] * len(row)

                n_promo = (disp_g.get("has_active_promo", pd.Series(dtype=str)) == "Y").sum() if "has_active_promo" in disp_g.columns else 0
                st.caption(f"{len(gp)} prodotti · {n_promo} in promozione")
                st.dataframe(
                    disp_g[show_cols_g].style.apply(
                        lambda row: ["background-color: #FFF8D0; color:#7a6300" if gp.loc[row.name, "has_active_promo"] == "Y" else "" for _ in row],
                        axis=1,
                    ),
                    use_container_width=True,
                    hide_index=True,
                    height=350,
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
                st.info("Dati prodotti Deliveroo non ancora disponibili.\nVerranno caricati al prossimo run della pipeline.")
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
                st.dataframe(
                    disp_d[show_cols_d].style.apply(
                        lambda row: [
                            "background-color: #d0f0ea; color:#00614e" if has_promo_col and disp_d.loc[row.name, "Promozione"] != "" else ""
                            for _ in row
                        ],
                        axis=1,
                    ),
                    use_container_width=True,
                    hide_index=True,
                    height=350,
                )


# ---------------------------------------------------------------------------
# TAB 3 — Trend
# ---------------------------------------------------------------------------

def tab_trend(sel_weeks, sel_cities):
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

    city_df = load_city_parity()
    if city_df.empty:
        st.info("Nessun dato disponibile.")
        return

    df = city_df.copy()
    if sel_cities:
        df = df[df["city_code"].isin(sel_cities)]

    if df.empty:
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
        st.dataframe(df.sort_values(["week_num", "city_code"]), use_container_width=True, hide_index=True)


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
    st.caption("Tre sezioni: candidati da confermare · matching manuale · ground truth")

    # Mostra feedback dell'ultima operazione di salvataggio
    if "last_save_msg" in st.session_state:
        kind, msg = st.session_state.pop("last_save_msg")
        if kind == "ok":
            st.success(f"✅ {msg}")
        else:
            st.error(f"❌ Errore durante il salvataggio: {msg}")

    review_df   = load_review_queue()
    unmatched   = load_unmatched_stores()
    mapping_df  = load_store_mapping()
    deliv_names = load_deliveroo_names_by_city()

    # KPI top
    k1, k2, k3 = st.columns(3)
    k1.metric("⏳ Da revisionare",  len(review_df))
    k2.metric("❓ Non matchati",    len(unmatched))
    k3.metric("✅ Ground truth",    len(mapping_df))
    st.divider()

    # =========================================================================
    # SEZIONE 1 — Candidati fuzzy da confermare / rifiutare
    # =========================================================================
    with st.expander(f"⏳ Candidati automatici da revisionare  ({len(review_df)} store)", expanded=len(review_df) > 0):
        if review_df.empty:
            st.success("Nessun candidato in coda — tutto a posto!")
        else:
            # Filtro città
            cities_r = sorted(review_df["city_code"].unique())
            sel_city_r = st.selectbox("Città", ["Tutte"] + cities_r, key="rev_city")
            df_r = review_df if sel_city_r == "Tutte" else review_df[review_df["city_code"] == sel_city_r]

            st.dataframe(
                df_r[["city_code","glovo_name","candidate_deliveroo","score","reason"]],
                use_container_width=True, hide_index=True
            )

            st.markdown("**Conferma o rifiuta un candidato:**")
            sel_idx = st.selectbox(
                "Seleziona store",
                options=range(len(df_r)),
                format_func=lambda i: f"{df_r.iloc[i]['city_code']} | {df_r.iloc[i]['glovo_name']}  (score {df_r.iloc[i]['score']})",
                key="rev_sel"
            )
            sel_row = df_r.iloc[sel_idx]

            col_info, col_action = st.columns([2, 1])
            with col_info:
                st.write(f"**Glovo:** `{sel_row['glovo_name']}`")
                st.write(f"**Candidato Deliveroo:** `{sel_row['candidate_deliveroo']}`  — score **{sel_row['score']}**")

                # Dropdown con tutti i nomi Deliveroo della città per correzione rapida
                city_options = deliv_names.get(sel_row["city_code"], [])
                default_idx  = city_options.index(sel_row["candidate_deliveroo"]) \
                               if sel_row["candidate_deliveroo"] in city_options else 0
                deliv_choice = st.selectbox(
                    "Scegli dalla lista scrappata",
                    options=["— Non in lista (scrivi sotto) —"] + city_options,
                    index=default_idx + 1 if sel_row["candidate_deliveroo"] in city_options else 0,
                    key="rev_choice"
                )
                # Campo libero per nomi non in lista (store senza promo attiva)
                custom_name = st.text_input(
                    "Oppure scrivi il nome Deliveroo manualmente",
                    placeholder="Es: Pizzeria da Paolo  (lascia vuoto se usi la lista sopra)",
                    key="rev_custom"
                )
                st.caption("💡 Usa il testo libero se lo store è su Deliveroo ma non ha promo attive e quindi non compare nella lista scrappata. Verrà registrato con **nessuna promozione** per questa settimana.")

            with col_action:
                st.write("")
                st.write("")
                # Determina il nome finale: testo libero ha priorità sul dropdown
                final_choice = custom_name.strip() if custom_name.strip() else (
                    "" if deliv_choice == "— Non in lista (scrivi sotto) —" else deliv_choice
                )
                if st.button("✅ Conferma", type="primary", key="rev_confirm"):
                    if not final_choice:
                        _run_save(save_rejected_match, sel_row["city_code"], sel_row["glovo_name"],
                                  success_msg="Marcato come non presente su Deliveroo")
                    else:
                        label = " (nessuna promo attiva)" if custom_name.strip() else ""
                        _run_save(save_confirmed_match, sel_row["city_code"], sel_row["glovo_name"], final_choice,
                                  success_msg=f"Match confermato: {sel_row['glovo_name']} → {final_choice}{label}")

                if st.button("❌ Non su Deliveroo", key="rev_reject"):
                    _run_save(save_rejected_match, sel_row["city_code"], sel_row["glovo_name"],
                              success_msg=f"{sel_row['glovo_name']} escluso (non su Deliveroo)")

    # =========================================================================
    # SEZIONE 2 — Matching manuale store UNMATCHED
    # =========================================================================
    with st.expander(f"❓ Matching manuale store non matchati  ({len(unmatched)} store)", expanded=False):
        if unmatched.empty:
            st.success("Tutti gli store sono matchati!")
        else:
            st.caption("Questi store Glovo non hanno ancora un corrispettivo Deliveroo. "
                       "Seleziona la città, cerca lo store, scegli il nome Deliveroo dalla lista.")

            # Filtri
            col_f1, col_f2 = st.columns([1, 2])
            with col_f1:
                cities_u = sorted(unmatched["city_code"].unique())
                sel_city_u = st.selectbox("Città", cities_u, key="unm_city")
            with col_f2:
                search_u = st.text_input("🔍 Cerca per nome store Glovo", "", key="unm_search")

            df_u = unmatched[unmatched["city_code"] == sel_city_u]
            if search_u:
                df_u = df_u[df_u["glovo_name"].str.contains(search_u, case=False, na=False)]

            # Tabella store unmatched con revenue per prioritizzare
            st.dataframe(
                df_u[["glovo_name","revenue"]].rename(columns={"glovo_name":"Store Glovo","revenue":"Revenue €"}),
                use_container_width=True, hide_index=True, height=220
            )

            st.markdown("**Assegna un match manuale:**")
            if len(df_u) == 0:
                st.info("Nessuno store trovato con questi filtri.")
            else:
                col_s1, col_s2 = st.columns(2)
                with col_s1:
                    sel_glovo = st.selectbox(
                        "Store Glovo da matchare",
                        options=df_u["glovo_name"].tolist(),
                        key="unm_glovo"
                    )
                with col_s2:
                    city_opts = deliv_names.get(sel_city_u, [])
                    sel_deliv = st.selectbox(
                        "Scegli dalla lista scrappata",
                        options=["— Non in lista (scrivi sotto) —"] + city_opts,
                        key="unm_deliv"
                    )
                    if not city_opts:
                        st.caption(f"⚠️ Nessun ristorante scrappato per {sel_city_u} — usa il campo testo.")

                # Campo testo libero sotto i due selectbox
                custom_deliv = st.text_input(
                    "Oppure scrivi il nome Deliveroo manualmente",
                    placeholder="Es: Pizzeria da Paolo  (lascia vuoto se usi la lista sopra)",
                    key="unm_custom"
                )
                st.caption("💡 Usa il testo libero se lo store è su Deliveroo ma non ha promo attive questa settimana. Verrà registrato con **nessuna promozione** nel calcolo di parity.")

                # Nome finale: testo libero ha priorità
                final_deliv = custom_deliv.strip() if custom_deliv.strip() else (
                    "" if sel_deliv == "— Non in lista (scrivi sotto) —" else sel_deliv
                )

                col_btn1, col_btn2 = st.columns(2)
                with col_btn1:
                    if st.button("✅ Salva match", type="primary", key="unm_save"):
                        if not final_deliv:
                            _run_save(save_rejected_match, sel_city_u, sel_glovo,
                                      success_msg=f"{sel_glovo} marcato come non presente su Deliveroo")
                        else:
                            label = " (nessuna promo attiva)" if custom_deliv.strip() else ""
                            _run_save(save_confirmed_match, sel_city_u, sel_glovo, final_deliv,
                                      success_msg=f"Match salvato: {sel_glovo} → {final_deliv}{label}")
                with col_btn2:
                    if st.button("❌ Non su Deliveroo", key="unm_reject"):
                        _run_save(save_rejected_match, sel_city_u, sel_glovo,
                                  success_msg=f"{sel_glovo} escluso (non su Deliveroo)")
                        clear_cache(); st.rerun()

            st.info("💡 I match salvati qui entrano nel **ground truth** e vengono usati automaticamente dalle pipeline successive — non servono più revisioni.")

    # =========================================================================
    # SEZIONE 3 — Ground truth (mapping confermati)
    # =========================================================================
    with st.expander(f"✅ Ground truth — mapping confermati  ({len(mapping_df)} store)", expanded=False):
        if mapping_df.empty:
            st.info("Nessun mapping confermato ancora.")
        else:
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                sources = mapping_df["source"].unique().tolist() if "source" in mapping_df.columns else []
                sel_src = st.multiselect("Fonte", sources, default=sources, key="gt_source")
            with col_f2:
                search_gt = st.text_input("🔍 Cerca store", "", key="gt_search")

            disp = mapping_df[mapping_df["source"].isin(sel_src)] if sel_src else mapping_df
            if search_gt:
                disp = disp[disp["glovo_name"].str.contains(search_gt, case=False, na=False)]

            st.dataframe(disp, use_container_width=True, hide_index=True, height=350)
            st.caption(
                f"**{len(disp)}** visualizzati  |  "
                f"**{(mapping_df['deliveroo_name'] != '').sum()}** con match  |  "
                f"**{(mapping_df['deliveroo_name'] == '').sum()}** esclusi (non su Deliveroo)"
            )

        if not mapping_df.empty:
            st.download_button(
                "📥 Esporta store_mapping.csv",
                data=mapping_df.to_csv(index=False).encode("utf-8"),
                file_name="store_mapping.csv",
                mime="text/csv",
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not check_password():
        st.stop()

    # Header
    st.title("🛵 Promo Parity — Glovo vs Deliveroo")

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

    sel_weeks, sel_cities = sidebar()

    # Icone custom nei tab via CSS injection
    import base64 as _b64mod

    def _icon_b64(name: str) -> str:
        p = ROOT / "assets" / name
        return _b64mod.b64encode(p.read_bytes()).decode() if p.exists() else ""

    _b64_promo   = _icon_b64("promoZone.png")
    _b64_store   = _icon_b64("storePhone.png")
    _b64_trend   = _icon_b64("growth.png")
    _b64_matching = _icon_b64("twoBagsYellowCheck.png")

    _css_tabs = """<style>
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
    _css_tabs += "</style>"
    st.markdown(_css_tabs, unsafe_allow_html=True)

    tab1, tab2, tab3, tab4 = st.tabs([
        "City Parity",
        "Store Detail",
        "Trend",
        "Store Matching",
    ])

    with tab1:
        tab_city_parity(sel_weeks, sel_cities)
    with tab2:
        tab_store_detail(sel_weeks, sel_cities)
    with tab3:
        tab_trend(sel_weeks, sel_cities)
    with tab4:
        tab_store_matching()


if __name__ == "__main__":
    main()
