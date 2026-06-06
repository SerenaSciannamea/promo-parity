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
    max_pct_off,          <- massima % sconto sui prodotti in promo (usata per parity)
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

    Supporta due schemi:
    - Schema legacy: has_active_promo, type_of_promo, avg_percentage_off, avg_unit_price
    - Schema nuovo (W20+): promo_non_prime, type_of_promo_np, percentage_off_np,
                           avg_product_unit_price  (prime/non-prime separati)
      Per la parity vs Deliveroo si usa la promo non-prime (visibile a tutti gli utenti).
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

    # -----------------------------------------------------------------------
    # Normalizzazione schema nuovo -> schema interno comune
    # -----------------------------------------------------------------------
    new_schema = "promo_non_prime" in df.columns

    if new_schema:
        # avg_unit_price
        if "avg_product_unit_price" in df.columns and "avg_unit_price" not in df.columns:
            df["avg_unit_price"] = df["avg_product_unit_price"]

        # has_active_promo: Y se la promo non-prime è attiva (ignoriamo prime-only)
        if "has_active_promo" not in df.columns:
            df["has_active_promo"] = (
                df["promo_non_prime"].str.strip().str.upper().fillna("N")
            )

        # type_of_promo: usa il tipo non-prime
        if "type_of_promo" not in df.columns:
            df["type_of_promo"] = df.get("type_of_promo_np", pd.Series("", index=df.index))
            df["type_of_promo"] = df["type_of_promo"].fillna("").str.strip().str.upper()

        # avg_percentage_off: usa la % non-prime
        # Solo per promo che hanno una % significativa (non TWO_FOR_ONE, FREE_DELIVERY)
        PCT_BASED_TYPES = {"PERCENTAGE_DISCOUNT", "BASKET_PERCENTAGE"}
        if "avg_percentage_off" not in df.columns:
            raw_pct = pd.to_numeric(
                df.get("percentage_off_np", pd.Series(0, index=df.index)),
                errors="coerce"
            ).fillna(0)
            # Azzera la % se il tipo di promo non la usa
            df["avg_percentage_off"] = raw_pct.where(
                df.get("type_of_promo", pd.Series("", index=df.index))
                  .str.upper().isin(PCT_BASED_TYPES),
                0
            )

    # Cast numerici
    numeric_cols = ["avg_unit_price", "total_product_sold", "avg_percentage_off",
                    "quantity_sold_under_promo", "promo_active_days",
                    "min_basket_size_np", "min_basket_size_p",
                    "quantity_sold_np", "quantity_sold_p",
                    "pct_store_addresses_impacted"]
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


def aggregate_store_level(df: pd.DataFrame, prime_mode: bool = False) -> pd.DataFrame:
    """
    Aggrega il DataFrame product-level a livello store × settimana.

    Parametri
    ----------
    df          : DataFrame prodotto-livello (output di load_glovo_csv)
    prime_mode  : se True usa la logica "prime-first":
                  per ogni prodotto, se esiste una promo Prime la usa;
                  altrimenti fa fallback alla promo Non-Prime.
                  Utile per calcolare la parity dal punto di vista degli utenti Prime.

    Logica:
    - best_promo_type: il tipo di promo con rank piu' basso (piu' forte) fra tutti
      i prodotti dello store in quella settimana
    - avg_pct_off: media di avg_percentage_off per i prodotti in PERCENTAGE_DISCOUNT
    - max_pct_off: massima avg_percentage_off fra i prodotti in PERCENTAGE_DISCOUNT
                   (usata come metrica principale nella parity, simmetrica con Deliveroo)
    - promo_product_count: n. prodotti distinti con has_active_promo == 'Y'
    - revenue: somma(avg_unit_price * total_product_sold) per tutti i prodotti
    - promo_revenue: revenue dei soli prodotti in promo
    - promo_coverage_pct: promo_revenue / revenue * 100
    """

    df = df.copy()

    # -----------------------------------------------------------------------
    # Prime-first override: per ogni prodotto usa la promo Prime se esiste,
    # altrimenti fallback alla Non-Prime gia' normalizzata in load_glovo_csv
    # -----------------------------------------------------------------------
    if prime_mode and "promotion_prime" in df.columns:
        has_prime = df["promotion_prime"].str.strip().str.upper().fillna("N") == "Y"

        # has_active_promo: Y se ha promo prime OPPURE non-prime
        df["has_active_promo"] = df.apply(
            lambda r: "Y" if str(r.get("promotion_prime", "N")).strip().upper() == "Y"
                      else r.get("has_active_promo", "N"),
            axis=1,
        )

        # type_of_promo: usa tipo prime se disponibile, altrimenti non-prime
        if "type_of_promo_p" in df.columns:
            df["type_of_promo"] = df.apply(
                lambda r: str(r.get("type_of_promo_p", "") or "").strip().upper()
                          if str(r.get("promotion_prime", "N")).strip().upper() == "Y"
                          else r.get("type_of_promo", ""),
                axis=1,
            )

        # avg_percentage_off: usa % prime se disponibile, altrimenti non-prime
        # Solo per promo che hanno una % significativa
        PCT_BASED_TYPES_PM = {"PERCENTAGE_DISCOUNT", "BASKET_PERCENTAGE"}
        if "percentage_off_p" in df.columns:
            def _prime_pct(r):
                if str(r.get("promotion_prime", "N")).strip().upper() == "Y":
                    t = str(r.get("type_of_promo", "") or "").upper()
                    return float(r.get("percentage_off_p") or 0) if t in PCT_BASED_TYPES_PM else 0.0
                else:
                    t = str(r.get("type_of_promo", "") or "").upper()
                    return float(r.get("avg_percentage_off") or 0) if t in PCT_BASED_TYPES_PM else 0.0
            df["avg_percentage_off"] = df.apply(_prime_pct, axis=1)

    # Calcola rank riga per riga
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
        max_pct = pct_rows["avg_percentage_off"].max() if len(pct_rows) > 0 else None

        # Min basket size per BASKET_PERCENTAGE
        basket_rows = promo_rows[promo_rows["type_of_promo"] == "BASKET_PERCENTAGE"]
        min_basket = None
        if not basket_rows.empty and "min_basket_size_np" in basket_rows.columns:
            val = pd.to_numeric(basket_rows["min_basket_size_np"], errors="coerce").max()
            if not pd.isna(val):
                min_basket = round(float(val), 0)

        total_revenue = g["revenue"].sum()
        promo_rev = g["promo_revenue"].sum()

        return pd.Series({
            "best_promo_type":      best_row["type_of_promo"] if best_row["row_rank"] < NO_PROMO_RANK else "",
            "best_promo_rank":      best_row["row_rank"],
            "avg_pct_off":          round(avg_pct, 1) if avg_pct is not None else None,
            "max_pct_off":          round(max_pct, 1) if max_pct is not None else None,
            "min_basket_size":      min_basket,
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
