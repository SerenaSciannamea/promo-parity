"""
parity_calculator.py
Calcola la promo parity Glovo vs Deliveroo a livello store e citta'.

Output store-level (una riga per city_code x store x week):
    city_code, glovo_name, deliveroo_name, week_num,
    glovo_promo_type, glovo_rank,
    deliveroo_promo_type, deliveroo_rank,
    parity,               <- SUPERIORITY | PARITY | INFERIORITY
    glovo_pct_off,        <- % media sconto Glovo (quando disponibile)
    glovo_promo_products, <- n. prodotti in promo su Glovo
    deliveroo_promo_text, <- testo raw promo Deliveroo
    revenue,              <- fatturato Glovo (proxy peso)
    promo_coverage_pct,   <- % revenue sotto promo su Glovo

Output city-level (una riga per city_code x week):
    city_code, week_num,
    n_stores_matched,
    n_superiority, n_parity, n_inferiority, n_unmatched,
    pct_superiority, pct_parity, pct_inferiority,   <- su stores matchati
    w_superiority, w_parity, w_inferiority,          <- stesso ma pesato per revenue
    city_parity_label   <- etichetta dominante (soglia: >=50% pesata)
"""

from __future__ import annotations

import pandas as pd
from pipeline.promo_ranker import rank_deliveroo, parity_label, rank_label, NO_PROMO_RANK, extract_pct_deliveroo


# ---------------------------------------------------------------------------
# Store-level parity
# ---------------------------------------------------------------------------

def compute_store_parity(
    glovo_store: pd.DataFrame,
    deliveroo_deduped: pd.DataFrame,
    store_match_map: dict[tuple[str, str], str | None],
) -> pd.DataFrame:
    """
    Calcola la parity per ogni store Glovo matchato.

    Parameters
    ----------
    glovo_store      : DataFrame aggregato store-level da glovo_reader
                       (city_code, store_name, week_num, best_promo_rank, ...)
    deliveroo_deduped: DataFrame da deliveroo_promo_deduped.csv
                       (city_code, restaurant_name, promotion_type, scraped_at_utc)
    store_match_map  : { (city_code, glovo_name) -> deliveroo_name | None }

    Returns
    -------
    DataFrame con una riga per store x week.
    """
    rows = []

    # Indice Deliveroo: (city_code, restaurant_name_lower) -> promotion_type
    deliv_index: dict[tuple[str, str], str] = {}
    if deliveroo_deduped is not None and len(deliveroo_deduped) > 0:
        for _, r in deliveroo_deduped.iterrows():
            city = str(r.get("city_code", "")).strip()
            name = str(r.get("restaurant_name", "")).strip()
            promo = str(r.get("promotion_type", "")).strip()
            deliv_index[(city, name.lower())] = promo

    for _, row in glovo_store.iterrows():
        city        = str(row["city_code"]).strip()
        glovo_nm    = str(row["store_name"]).strip()
        week        = str(row["week_num"]).strip()
        glovo_rank  = float(row["best_promo_rank"])
        glovo_type  = str(row.get("best_promo_type", "")).strip()

        # Cerca il match Deliveroo
        deliveroo_nm   = store_match_map.get((city, glovo_nm))
        deliveroo_promo = None
        deliveroo_rank  = NO_PROMO_RANK

        if deliveroo_nm:
            deliveroo_promo = deliv_index.get((city, deliveroo_nm.lower()))
            if deliveroo_promo is not None:
                deliveroo_rank = rank_deliveroo(deliveroo_promo)

        deliveroo_pct = extract_pct_deliveroo(deliveroo_promo) if deliveroo_promo else 0.0
        glovo_pct     = float(row.get("avg_pct_off") or 0)

        parity = parity_label(
            glovo_rank, deliveroo_rank,
            glovo_pct_off=glovo_pct,
            deliveroo_pct_off=deliveroo_pct,
        ) if deliveroo_nm else "UNMATCHED"

        rows.append({
            "city_code":            city,
            "glovo_name":           glovo_nm,
            "deliveroo_name":       deliveroo_nm or "",
            "week_num":             week,
            "glovo_promo_type":     glovo_type,
            "glovo_rank":           glovo_rank,
            "glovo_rank_label":     rank_label(glovo_rank),
            "deliveroo_promo_text": deliveroo_promo or "",
            "deliveroo_rank":       deliveroo_rank,
            "deliveroo_rank_label": rank_label(deliveroo_rank),
            "parity":               parity,
            "glovo_pct_off":        row.get("avg_pct_off"),
            "glovo_promo_products": int(row.get("promo_product_count", 0)),
            "revenue":              float(row.get("revenue", 0)),
            "promo_coverage_pct":   float(row.get("promo_coverage_pct", 0)),
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# City-level parity (pesata per revenue)
# ---------------------------------------------------------------------------

def compute_city_parity(store_parity: pd.DataFrame) -> pd.DataFrame:
    """
    Aggrega la parity a livello citta' x settimana.
    Peso = revenue Glovo per store.
    """
    rows = []

    group_keys = ["city_code", "week_num"]

    for (city, week), g in store_parity.groupby(group_keys):
        matched  = g[g["parity"] != "UNMATCHED"]
        unmatched = g[g["parity"] == "UNMATCHED"]

        n_total       = len(g)
        n_matched     = len(matched)
        n_unmatched   = len(unmatched)
        n_sup  = int((matched["parity"] == "SUPERIORITY").sum())
        n_par  = int((matched["parity"] == "PARITY").sum())
        n_inf  = int((matched["parity"] == "INFERIORITY").sum())

        # Percentuali semplici (su store matchati)
        pct_sup = round(n_sup / n_matched * 100, 1) if n_matched > 0 else 0.0
        pct_par = round(n_par / n_matched * 100, 1) if n_matched > 0 else 0.0
        pct_inf = round(n_inf / n_matched * 100, 1) if n_matched > 0 else 0.0

        # Peso per revenue (stores senza revenue = 0 -> contributo nullo)
        total_rev = matched["revenue"].sum()
        if total_rev > 0:
            w_sup = round(matched.loc[matched["parity"] == "SUPERIORITY", "revenue"].sum() / total_rev * 100, 1)
            w_par = round(matched.loc[matched["parity"] == "PARITY",      "revenue"].sum() / total_rev * 100, 1)
            w_inf = round(matched.loc[matched["parity"] == "INFERIORITY", "revenue"].sum() / total_rev * 100, 1)
        else:
            w_sup = pct_sup
            w_par = pct_par
            w_inf = pct_inf

        # Etichetta dominante (su peso revenue)
        best_w = max(w_sup, w_par, w_inf)
        if best_w == w_sup:
            city_label = "SUPERIORITY"
        elif best_w == w_par:
            city_label = "PARITY"
        else:
            city_label = "INFERIORITY"

        rows.append({
            "city_code":        city,
            "week_num":         week,
            "n_stores_total":   n_total,
            "n_stores_matched": n_matched,
            "n_unmatched":      n_unmatched,
            "n_superiority":    n_sup,
            "n_parity":         n_par,
            "n_inferiority":    n_inf,
            "pct_superiority":  pct_sup,
            "pct_parity":       pct_par,
            "pct_inferiority":  pct_inf,
            "w_superiority":    w_sup,   # pesato per revenue
            "w_parity":         w_par,
            "w_inferiority":    w_inf,
            "city_parity_label": city_label,
            "match_coverage_pct": round(n_matched / n_total * 100, 1) if n_total > 0 else 0.0,
        })

    return pd.DataFrame(rows)
