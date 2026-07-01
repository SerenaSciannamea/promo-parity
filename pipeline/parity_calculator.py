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
from pipeline.promo_ranker import rank_deliveroo, parity_label, rank_label, NO_PROMO_RANK, extract_pct_deliveroo, extract_min_basket_deliveroo


# ---------------------------------------------------------------------------
# Store-level parity
# ---------------------------------------------------------------------------

# Severita' della parity dal punto di vista di Glovo (0 = peggiore).
# Usata per scegliere il concorrente Deliveroo piu' aggressivo nei match 1:N.
_PARITY_SEVERITY = {"INFERIORITY": 0, "PARITY": 1, "SUPERIORITY": 2}


def _aov_upgrade(rank: float, min_basket: float, aov: float | None) -> float:
    """Promo a minimum-basket (rank 3.0) -> trattala come % semplice (rank 2.0) quando
    la soglia NON e' una barriera secondo l'AOV del partner: AOV <= soglia + 1 euro
    (AOV sotto la soglia, o la supera di al massimo 1 euro)."""
    if rank == 3.0 and min_basket and aov is not None and aov <= min_basket + 1.0:
        return 2.0
    return rank


def compute_store_parity(
    glovo_store: pd.DataFrame,
    deliveroo_deduped: pd.DataFrame,
    store_match_map: dict[tuple[str, str], list[str] | str | None],
    exclusive_glovo_set:  set[tuple[str, str]] | None = None,
    not_on_deliveroo_set: set[tuple[str, str]] | None = None,
    aov_map: dict[tuple[str, str, str], float] | None = None,
) -> pd.DataFrame:
    """
    Calcola la parity per ogni store Glovo matchato.

    Parameters
    ----------
    glovo_store           : DataFrame aggregato store-level da glovo_reader
    deliveroo_deduped     : DataFrame da deliveroo_promo_deduped.csv
    store_match_map       : { (city_code, glovo_name) -> [deliveroo_name, ...] }
                            (matching 1:N; accetta anche una stringa singola per
                            retro-compatibilita'. Se >1 filiale, la parity usa il
                            concorrente Deliveroo piu' aggressivo.)
    exclusive_glovo_set   : (city_code, glovo_name) con accordo commerciale esclusiva
                            → parity='EXCLUSIVE_GLOVO'
    not_on_deliveroo_set  : (city_code, glovo_name) non presenti su Deliveroo per
                            scelta indipendente del partner
                            → parity='NOT_ON_DELIVEROO'

    Returns
    -------
    DataFrame con una riga per store x week.
    """
    rows = []

    # Indice Deliveroo: (city_code, restaurant_name_lower) -> info promo.
    # Il deduped ha piu' righe per (citta', nome) quando le FILIALI hanno promo diverse.
    # Confronto = promo piu' FORTE presente in ALMENO META' delle filiali (stores_pct>=50):
    # cosi' un outlier di 1 sola filiale (es. 40% su 3) non crea una inferiority falsa.
    # Se nessuna raggiunge il 50% -> la piu' COMUNE (poi la piu' forte). Dati vecchi senza
    # stores_pct -> ricade sulla piu' forte (retro-compatibile).
    _deliv_all: dict[tuple[str, str], list[dict]] = {}
    if deliveroo_deduped is not None and len(deliveroo_deduped) > 0:
        for _, r in deliveroo_deduped.iterrows():
            city = str(r.get("city_code", "")).strip()
            name = str(r.get("restaurant_name", "")).strip()
            promo = str(r.get("promotion_type", "")).strip()
            rk = rank_deliveroo(promo) if promo else NO_PROMO_RANK
            pc = extract_pct_deliveroo(promo) if promo else 0.0
            n_with = int(pd.to_numeric(r.get("n_stores_with_promo"), errors="coerce") or 0)
            n_tot  = int(pd.to_numeric(r.get("n_stores_total"), errors="coerce") or 0)
            spct   = pd.to_numeric(r.get("stores_pct"), errors="coerce")
            spct   = float(spct) if pd.notna(spct) else (round(100 * n_with / n_tot) if n_tot else 0.0)
            _deliv_all.setdefault((city, name.lower()), []).append(
                {"promo": promo, "rank": rk, "pct": pc,
                 "n_with": n_with, "n_tot": n_tot, "stores_pct": spct})

    deliv_index: dict[tuple[str, str], dict] = {}
    for key, cands in _deliv_all.items():
        widespread = [c for c in cands if c["stores_pct"] >= 50]
        if widespread:                       # la piu' forte tra le diffuse (>=50% filiali)
            deliv_index[key] = min(widespread, key=lambda c: (c["rank"], -c["pct"]))
        else:                                # nessuna diffusa -> la piu' comune, poi la piu' forte
            deliv_index[key] = min(cands, key=lambda c: (-c["stores_pct"], c["rank"], -c["pct"]))

    for _, row in glovo_store.iterrows():
        city        = str(row["city_code"]).strip()
        glovo_nm    = str(row["store_name"]).strip()
        week        = str(row["week_num"]).strip()
        glovo_rank  = float(row["best_promo_rank"])
        glovo_type  = str(row.get("best_promo_type", "")).strip()

        glovo_pct            = float(row.get("max_pct_off") or row.get("avg_pct_off") or 0)
        glovo_promo_products = int(row.get("promo_product_count") or 0)
        glovo_min_basket     = float(row.get("min_basket_size") or 0)

        # AOV del partner (city, store, week) -> upgrade min-basket a % semplice se non e' barriera
        aov = aov_map.get((city, glovo_nm, week)) if aov_map else None
        glovo_rank = _aov_upgrade(glovo_rank, glovo_min_basket, aov)

        # Match Deliveroo: puo' essere 1:N (stessa insegna a indirizzi diversi).
        # Le filiali dovrebbero avere la stessa promo; se differiscono confrontiamo
        # Glovo col concorrente PIU' AGGRESSIVO (peggior parity per Glovo) per non
        # nascondere una INFERIORITY.
        match_val = store_match_map.get((city, glovo_nm))
        if isinstance(match_val, str):
            deliveroo_names = [match_val] if match_val.strip() else []
        else:
            deliveroo_names = [str(n).strip() for n in (match_val or []) if str(n).strip()]

        deliveroo_nm         = ""
        deliveroo_promo      = None
        deliveroo_rank       = NO_PROMO_RANK
        deliveroo_pct        = 0.0
        deliveroo_min_basket = 0.0
        deliveroo_stores_pct = None
        deliveroo_stores_frac = ""

        if deliveroo_names:
            best = None  # tieni la filiale che da' la parity peggiore per Glovo
            for dn in deliveroo_names:
                ent     = deliv_index.get((city, dn.lower()))
                b_promo = ent["promo"] if ent else None
                b_rank  = ent["rank"] if ent else NO_PROMO_RANK
                b_pct   = ent["pct"] if ent else 0.0
                b_bask  = extract_min_basket_deliveroo(b_promo) if b_promo else 0.0
                b_rank  = _aov_upgrade(b_rank, b_bask, aov)   # stessa regola AOV lato Deliveroo
                b_par   = parity_label(
                    glovo_rank, b_rank,
                    glovo_pct_off=glovo_pct,
                    deliveroo_pct_off=b_pct,
                    glovo_promo_products=glovo_promo_products,
                    glovo_min_basket=glovo_min_basket,
                    deliveroo_min_basket=b_bask,
                )
                # ordina: peggior parity (INFERIORITY<PARITY<SUPERIORITY), poi promo
                # Deliveroo piu' forte (rank minore, pct maggiore)
                sev = _PARITY_SEVERITY.get(b_par, 2)
                sort_key = (sev, b_rank, -b_pct, dn)
                if best is None or sort_key < best[0]:
                    best = (sort_key, dn, b_promo, b_rank, b_pct, b_bask, b_par, ent)
            (_, deliveroo_nm, deliveroo_promo, deliveroo_rank,
             deliveroo_pct, deliveroo_min_basket, parity, best_ent) = best
            if best_ent and best_ent.get("n_tot"):
                deliveroo_stores_pct  = best_ent["stores_pct"]
                deliveroo_stores_frac = f"{best_ent['n_with']}/{best_ent['n_tot']}"
        elif exclusive_glovo_set and (city, glovo_nm) in exclusive_glovo_set:
            parity = "EXCLUSIVE_GLOVO"
        elif not_on_deliveroo_set and (city, glovo_nm) in not_on_deliveroo_set:
            parity = "NOT_ON_DELIVEROO"
        else:
            parity = "UNMATCHED"

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
            "glovo_pct_off":        row.get("max_pct_off") if row.get("max_pct_off") is not None else row.get("avg_pct_off"),
            "glovo_min_basket":     glovo_min_basket if glovo_min_basket else None,
            "deliveroo_pct_off":    round(deliveroo_pct, 1) if deliveroo_pct else None,
            "deliveroo_min_basket": round(deliveroo_min_basket, 1) if deliveroo_min_basket else None,
            "deliveroo_stores_pct": round(deliveroo_stores_pct) if deliveroo_stores_pct else None,
            "deliveroo_stores_frac": deliveroo_stores_frac,
            "glovo_promo_products": int(row.get("promo_product_count", 0)),
            "revenue":              float(row.get("revenue", 0)),
            "promo_coverage_pct":   float(row.get("promo_coverage_pct", 0)),
        })

    # -----------------------------------------------------------------------
    # NOT_ON_GLOVO: ristoranti Deliveroo (con promo) che nessun Glovo aggancia.
    # Lo scraper salva solo i Deliveroo CON promo, quindi questi sono i "Deliveroo-only".
    # store_parity e' Glovo-centrico: per la chiave UNIQUE(city, glovo_name, week)
    # riusiamo il nome Deliveroo come glovo_name (in app la colonna Glovo -> "—").
    # -----------------------------------------------------------------------
    matched_deliveroo = set()
    for (c, _gn), dns in store_match_map.items():
        _names = [dns] if isinstance(dns, str) else (dns or [])
        for dn in _names:
            if dn and str(dn).strip():
                matched_deliveroo.add((c, str(dn).strip().lower()))
    week_nog = ""
    if "week_num" in glovo_store.columns and len(glovo_store):
        _wk = glovo_store["week_num"].dropna()
        if len(_wk):
            week_nog = str(_wk.iloc[0]).strip()

    if deliveroo_deduped is not None and len(deliveroo_deduped) > 0:
        seen_nog: set[tuple[str, str]] = set()
        for _, drow in deliveroo_deduped.iterrows():
            city  = str(drow.get("city_code", "")).strip()
            name  = str(drow.get("restaurant_name", "")).strip()
            promo = str(drow.get("promotion_type", "")).strip()
            if not city or not name or not promo:
                continue
            key = (city, name.lower())
            if key in matched_deliveroo or key in seen_nog:
                continue
            seen_nog.add(key)
            d_rank = rank_deliveroo(promo)
            d_pct  = extract_pct_deliveroo(promo)
            d_bask = extract_min_basket_deliveroo(promo)
            rows.append({
                "city_code":            city,
                "glovo_name":           name,   # chiave: nessun Glovo reale → in app "—"
                "deliveroo_name":       name,
                "week_num":             week_nog,
                "glovo_promo_type":     "",
                "glovo_rank":           NO_PROMO_RANK,
                "glovo_rank_label":     "",
                "deliveroo_promo_text": promo,
                "deliveroo_rank":       d_rank,
                "deliveroo_rank_label": rank_label(d_rank),
                "parity":               "NOT_ON_GLOVO",
                "glovo_pct_off":        None,
                "glovo_min_basket":     None,
                "deliveroo_pct_off":    round(d_pct, 1) if d_pct else None,
                "deliveroo_min_basket": round(d_bask, 1) if d_bask else None,
                "glovo_promo_products": 0,
                "revenue":              0.0,
                "promo_coverage_pct":   0.0,
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

    for (city, week), g_all in store_parity.groupby(group_keys):
        # NOT_ON_GLOVO = Deliveroo-only: esclusi dalle metriche Glovo-centriche della citta'
        g = g_all[g_all["parity"] != "NOT_ON_GLOVO"]
        if g.empty:
            continue
        unmatched         = g[g["parity"] == "UNMATCHED"]
        exclusive_glovo   = g[g["parity"] == "EXCLUSIVE_GLOVO"]
        not_on_deliveroo  = g[g["parity"] == "NOT_ON_DELIVEROO"]
        matched           = g[~g["parity"].isin(["UNMATCHED", "EXCLUSIVE_GLOVO", "NOT_ON_DELIVEROO"])]

        n_total           = len(g)
        n_matched         = len(matched)
        n_unmatched       = len(unmatched)
        n_exclusive_glovo = len(exclusive_glovo) + len(not_on_deliveroo)  # aggregati per compatibilità
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
            "city_code":          city,
            "week_num":           week,
            "n_stores_total":     n_total,
            "n_stores_matched":   n_matched,
            "n_unmatched":        n_unmatched,
            "n_exclusive_glovo":  n_exclusive_glovo,
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
            # Esclude le esclusive Glovo dal denominatore: sono store
            # confermati come non presenti su Deliveroo, non "non ancora matchati"
            "match_coverage_pct": round(n_matched / (n_total - n_exclusive_glovo) * 100, 1)
                                   if (n_total - n_exclusive_glovo) > 0 else 0.0,
        })

    return pd.DataFrame(rows)
