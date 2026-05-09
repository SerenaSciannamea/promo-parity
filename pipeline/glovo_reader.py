"""
glovo_reader.py
Legge il CSV esportato da Google Sheets (BigQuery data connector) e aggrega
i dati a livello store per settimana.

Input:  CSV con colonne:
    city_code, store_name, week_num, product_name, avg_unit_price,
    total_product_sold, has_active_promo, type_of_promo,
    avg_percentage_off, quantity_sold_under_promo, promo_active_days

Output: DataFrame a livello store con:
    city_code, store_name, week_num,
    best_promo_type, best_promo_rank,
    avg_pct_off,          <- media % sconto sui prodotti in promo (quando disponibile)
    promo_product_count,  <- n. prodotti distinti in promo
    total_sold,           <- tot pezzi venduti (proxy fatturato)
    revenue,              <- avg_unit_price * total_product_sold (somma)
    promo_revenue,        <- revenue generata sotto promo
    promo_coverage_pct    <- % revenue sotto promo
"""

from __future__ import annotations

import pandas as pd
from pipeline.promo_ranker import rank_glovo, NO_PROMO_RANK


def load_glovo_csv(path: str) -> pd.DataFrame:
    """
    Carica il CSV Glovo e restituisce il DataFrame grezzo con typing corretto.
    Gestisce encoding utf-8 e utf-8-sig (BOM da Google Sheets).
    """
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(path, encoding=enc, dtype=str)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"Impossibile leggere il file con encoding noto: {path}")

    # Normalizza nomi colonne (strip spazi, lowercase)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # Cast numerici
    numeric_cols = ["avg_unit_price", "total_product_sold", "avg_percentage_off",
                    "quantity_sold_under_promo", "promo_active_days"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # Normalizza has_active_promo -> uppercase
    if "has_active_promo" in df.columns:
        df["has_active_promo"] = df["has_active_promo"].str.strip().str.upper().fillna("N")

    # Normalizza type_of_promo
    if "type_of_promo" in df.columns:
        df["type_of_promo"] = df["type_of_promo"].str.strip().str.upper().fillna("")

    return df


def aggregate_store_level(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggrega il DataFrame product-level a livello store × settimana.

    Logica:
    - best_promo_type: il tipo di promo con rank piu' basso (piu' forte) fra tutti
      i prodotti dello store in quella settimana
    - avg_pct_off: media di avg_percentage_off per i prodotti in PERCENTAGE_DISCOUNT
    - promo_product_count: n. prodotti distinti con has_active_promo == 'Y'
    - revenue: somma(avg_unit_price * total_product_sold) per tutti i prodotti
    - promo_revenue: revenue dei soli prodotti in promo
    - promo_coverage_pct: promo_revenue / revenue * 100
    """

    # Calcola rank riga per riga
    df = df.copy()
    df["row_rank"] = df.apply(
        lambda r: rank_glovo(r.get("type_of_promo", ""), r.get("has_active_promo", "N")),
        axis=1,
    )

    # Revenue per riga
    df["revenue"] = df["avg_unit_price"] * df["total_product_sold"]
    df["promo_revenue"] = df["revenue"].where(df["has_active_promo"] == "Y", 0)

    group_keys = ["city_code", "store_name", "week_num"]

    def agg_store(g: pd.DataFrame) -> pd.Series:
        best_idx = g["row_rank"].idxmin()
        best_row = g.loc[best_idx]

        promo_rows = g[g["has_active_promo"] == "Y"]

        # % off: media fra i prodotti con PERCENTAGE_DISCOUNT o BASKET_PERCENTAGE
        pct_rows = promo_rows[
            promo_rows["type_of_promo"].isin(["PERCENTAGE_DISCOUNT", "BASKET_PERCENTAGE"])
            & (promo_rows["avg_percentage_off"] > 0)
        ]
        avg_pct = pct_rows["avg_percentage_off"].mean() if len(pct_rows) > 0 else None

        total_revenue = g["revenue"].sum()
        promo_rev = g["promo_revenue"].sum()

        return pd.Series({
            "best_promo_type":      best_row["type_of_promo"] if best_row["row_rank"] < NO_PROMO_RANK else "",
            "best_promo_rank":      best_row["row_rank"],
            "avg_pct_off":          round(avg_pct, 1) if avg_pct is not None else None,
            "promo_product_count":  int(promo_rows["product_name"].nunique()),
            "total_sold":           int(g["total_product_sold"].sum()),
            "revenue":              round(total_revenue, 2),
            "promo_revenue":        round(promo_rev, 2),
            "promo_coverage_pct":   round(promo_rev / total_revenue * 100, 1) if total_revenue > 0 else 0.0,
        })

    result = df.groupby(group_keys, sort=False).apply(agg_store).reset_index()
    return result


def read_and_aggregate(csv_path: str) -> pd.DataFrame:
    """Pipeline completa: legge CSV e restituisce store-level DataFrame."""
    raw = load_glovo_csv(csv_path)
    return aggregate_store_level(raw)
