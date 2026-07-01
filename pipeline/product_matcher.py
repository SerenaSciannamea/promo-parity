# ===========================================================================
# product_matcher.py — match prodotto Glovo <-> Deliveroo (v1)
#
# Ancorato agli store GIA' matchati (store_mapping.csv): per ogni coppia
# (store Glovo, store Deliveroo) confronta i prodotti DI QUEI due store.
# Sorgente Deliveroo = solo prodotti IN PROMO (cio' che lo scraper cattura).
#
# Logica di match (concordata):
#   - Il NOME e' l'ancora d'identita' (token_sort_ratio).
#   - Il PREZZO non scarta mai: e' (a) tie-breaker soft tra candidati simili,
#     (b) bonus di confidence quando coincide, (c) OUTPUT (gap prezzo).
#
# Output:
#   data/product_match.csv         -> match automatici (auto)
#   data/product_match_review.csv  -> da rivedere a mano (nome medio, niente conferma)
# ===========================================================================
from __future__ import annotations

import argparse
import re
import sqlite3
import unicodedata
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process as rfp

from pipeline.promo_ranker import (
    rank_glovo, rank_deliveroo, parity_label, extract_pct_deliveroo, NO_PROMO_RANK,
)

BASE_DIR     = Path(__file__).resolve().parent.parent
DB_PATH      = BASE_DIR / "data" / "promo_parity.db"
MAPPING_CSV  = BASE_DIR / "data" / "store_mapping.csv"
ROO_PRODUCTS = BASE_DIR / "output" / "deliveroo_promo_products.csv"
OUT_MATCH    = BASE_DIR / "data" / "product_match.csv"
OUT_REVIEW   = BASE_DIR / "data" / "product_match_review.csv"

# Soglie
NAME_AUTO  = 88     # nome >= -> match automatico (a prescindere dal prezzo)
NAME_MIN   = 60     # nome >= -> candidato; sotto = nessun match
PRICE_TOL  = 0.03   # |gap| <= -> "prezzo confermato" / rescue del nome medio
MIN_UNION  = 3      # prodotti-in-promo (unione) minimi per usare il verdetto product-based

OUT_FIELDS = [
    "city_code", "glovo_name", "deliveroo_name",
    "roo_product", "glovo_product", "name_score", "match_type",
    "roo_promo", "roo_price", "roo_price_discounted", "roo_pct_off",
    "glovo_has_promo", "glovo_pct_off", "glovo_price", "glovo_revenue",
    "price_gap_pct", "price_confirmed", "product_parity",
]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s).lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


# Codice prodotto iniziale, specifico per piattaforma (Glovo 'K012 -', Deliveroo 'N35 -'):
# va tolto o lo stesso prodotto non matcha mai tra le due piattaforme.
_PRODUCT_CODE = re.compile(r"^\s*[a-z]{0,3}\d+\s*[-–—.:]\s*", re.I)


def norm_product(s: str) -> str:
    """Normalizza un NOME PRODOTTO togliendo il codice iniziale ('K012 - Miura' -> 'miura')."""
    return norm(_PRODUCT_CODE.sub("", str(s).strip()))


def to_price(s) -> float | None:
    s = re.sub(r"[^0-9.,]", "", str(s))
    if not s:
        return None
    if "," in s and "." in s:        # 1.234,56 -> 1234.56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:                    # 20,53 -> 20.53
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def roo_pct(price: float | None, disc: float | None) -> float | None:
    """% sconto Deliveroo dal prezzo pieno vs scontato (per prodotto)."""
    if price and disc and price > 0 and disc < price:
        return round((price - disc) / price * 100, 1)
    return None


def _product_parity(roo_p: float | None, glovo_promo: bool, glovo_p: float | None) -> str:
    """Confronto promo sullo STESSO prodotto. Ancora = Deliveroo e' in promo."""
    if not glovo_promo:
        return "GLOVO_NO_PROMO"           # Deliveroo promuove, Glovo no -> gap da colmare su Glovo
    if roo_p is None or glovo_p is None:
        return "ENTRAMBI_PROMO"           # entrambi in promo, % non confrontabile
    if glovo_p + 0.5 >= roo_p:
        return "GLOVO_>=_DELIVEROO"
    return "DELIVEROO_PIU_FORTE"


def _best_candidate(q_norm: str, q_price: float | None,
                    g_norm: list[str], g_price: list[float | None]):
    """Miglior candidato Glovo: nome primario, prezzo come tie-breaker soft.
    Tra i candidati con score entro 3 punti dal migliore, preferisce il prezzo piu' vicino."""
    cands = rfp.extract(q_norm, g_norm, scorer=fuzz.token_sort_ratio, limit=5)
    if not cands:
        return None
    top = cands[0][1]
    near = [c for c in cands if c[1] >= top - 3 and c[1] >= NAME_MIN]
    if not near:
        return cands[0]  # comunque restituiscilo, decidera' la soglia a valle
    if q_price is not None and len(near) > 1:
        def pdiff(c):
            gp = g_price[c[2]]
            return abs(q_price - gp) / max(q_price, gp) if gp else 9.9
        near.sort(key=lambda c: (pdiff(c), -c[1]))
    return near[0]


def build_matches(week: str | None = None):
    """Ritorna (match_df, parity_df).
    match_df  : 1 riga per prodotto Deliveroo-in-promo (match/review/unmatched) -> drill-down.
    parity_df : 1 riga per store con verdetto product-based BILANCIATO (unione promo,
                pesata per revenue Glovo) + copertura per la soglia di sostituzione.
    """
    mp = pd.read_csv(MAPPING_CSV, dtype=str, encoding="utf-8-sig").fillna("")
    roo = pd.read_csv(ROO_PRODUCTS, dtype=str).fillna("")
    # I prodotti Deliveroo arrivano per-filiale: tieni 1 riga per (citta', store, prodotto).
    roo = roo.drop_duplicates(subset=["city_code", "restaurant_name", "product_name"], keep="first")
    con = sqlite3.connect(DB_PATH)
    if not week:
        week = pd.read_sql("SELECT MAX(week_num) w FROM glovo_products", con)["w"][0]

    roo_by = roo.groupby(["city_code", "restaurant_name"])
    rows: list[dict] = []
    parity_rows: list[dict] = []
    for _, m in mp.iterrows():
        city, gnm, dnm = m["city_code"], m["glovo_name"], m["deliveroo_name"]
        if not dnm or (city, dnm) not in roo_by.groups:
            continue
        g = pd.read_sql(
            "SELECT DISTINCT product_name, has_active_promo, type_of_promo, avg_percentage_off, avg_unit_price, total_product_sold "
            "FROM glovo_products WHERE city_code=? AND store_name=? AND week_num=?",
            con, params=[city, gnm, week],
        )
        if g.empty:
            continue
        g_norm  = [norm_product(x) for x in g["product_name"]]
        g_price = [to_price(x) for x in g["avg_unit_price"]]
        g_promo = [str(x).upper().startswith("Y") for x in g["has_active_promo"]]
        g_pct   = [to_price(x) if str(x).strip() else None for x in g["avg_percentage_off"]]
        # rank promo per prodotto Glovo (2x1=1, %off=2, basket=3, no-promo=6)
        g_rank  = [rank_glovo(str(t), "Y") if p else NO_PROMO_RANK
                   for t, p in zip(g["type_of_promo"], g_promo)]
        g_rev   = []
        for i in range(len(g)):
            sold = to_price(g.iloc[i]["total_product_sold"])
            g_rev.append(round(sold * g_price[i], 2) if (sold is not None and g_price[i]) else None)

        roo_by_gi: dict[int, tuple[float, float]] = {}   # gi -> (rank, pct) Deliveroo piu' forte (match AUTO)
        n_roo = n_auto = 0   # prodotti Deliveroo-in-promo totali / matchati (auto)
        for _, pr in roo_by.get_group((city, dnm)).iterrows():
            n_roo += 1
            rprice = to_price(pr["product_price"])
            rdisc  = to_price(pr.get("product_price_discounted", ""))
            rpct   = roo_pct(rprice, rdisc)
            best = _best_candidate(norm_product(pr["product_name"]), rprice, g_norm, g_price)
            if not best or best[1] < NAME_MIN:
                rows.append(_row(city, gnm, dnm, pr, rprice, rdisc, rpct,
                                 None, None, best[1] if best else 0, "unmatched"))
                continue
            gi = best[2]
            grow = g.iloc[gi]
            gprice = g_price[gi]
            gap = None
            if rprice and gprice:
                gap = round((rprice - gprice) / gprice * 100, 1)   # +% = piu' caro su Deliveroo
            confirmed = gap is not None and abs(gap) <= PRICE_TOL * 100
            mtype = "auto" if (best[1] >= NAME_AUTO or (best[1] >= NAME_MIN and confirmed)) else "review"
            rows.append(_row(city, gnm, dnm, pr, rprice, rdisc, rpct, grow, gap,
                             best[1], mtype, gprice, confirmed))
            if mtype == "auto":
                n_auto += 1
                r_rank = rank_deliveroo(pr.get("promotion_type", ""))
                r_pct  = rpct or extract_pct_deliveroo(pr.get("promotion_type", "")) or 0.0
                cur = roo_by_gi.get(gi)
                if cur is None or (r_rank, -r_pct) < (cur[0], -cur[1]):
                    roo_by_gi[gi] = (r_rank, r_pct)

        # ---- Verdetto PRODUCT-PARITY: solo se TUTTI i prodotti in promo di Deliveroo
        # sono nel nostro menu (matchati al 100%). Confronto per prodotto con parity_label
        # (RANK-aware), ancorato sui promo Deliveroo, pesato per revenue Glovo.
        # Se non tutti matchano -> nessun verdetto product -> fallback store-level. ----
        if n_roo >= MIN_UNION and n_auto == n_roo and roo_by_gi:
            w_g = w_d = w_tie = 0.0
            for gi, (dr, dpct) in roo_by_gi.items():
                w    = g_rev[gi] if g_rev[gi] else (g_price[gi] or 1.0)
                gr   = g_rank[gi] if g_promo[gi] else NO_PROMO_RANK
                gpct = g_pct[gi] or 0.0
                lab = parity_label(gr, dr, glovo_pct_off=gpct, deliveroo_pct_off=dpct)
                if lab == "SUPERIORITY":
                    w_g += w
                elif lab == "INFERIORITY":
                    w_d += w
                else:
                    w_tie += w
            if (w_g + w_d + w_tie) > 0:
                share = (w_g + 0.5 * w_tie) / (w_g + w_d + w_tie)
                parity = "SUPERIORITY" if share >= 0.60 else ("PARITY" if share >= 0.45 else "INFERIORITY")
                parity_rows.append({
                    "city_code": city, "glovo_name": gnm, "deliveroo_name": dnm,
                    "n_roo_promo": n_roo, "glovo_rev_share": round(share, 3),
                    "parity_product": parity, "enough": True,
                })
    con.close()
    return pd.DataFrame(rows, columns=OUT_FIELDS), pd.DataFrame(parity_rows)


def _row(city, gnm, dnm, pr, rprice, rdisc, rpct, grow, gap, score, mtype,
         gprice=None, confirmed=False) -> dict:
    glovo_promo = bool(grow is not None and str(grow["has_active_promo"]).upper().startswith("Y"))
    gpct = None
    grev = None
    if grow is not None:
        gpct = to_price(grow["avg_percentage_off"]) if str(grow["avg_percentage_off"]).strip() else None
        sold = to_price(grow["total_product_sold"])
        if sold is not None and gprice is not None:
            grev = round(sold * gprice, 2)        # revenue prodotto su Glovo = venduti x prezzo
    return {
        "city_code": city, "glovo_name": gnm, "deliveroo_name": dnm,
        "roo_product": pr["product_name"],
        "glovo_product": grow["product_name"] if grow is not None else "",
        "name_score": round(score), "match_type": mtype,
        "roo_promo": pr.get("promotion_type", ""), "roo_price": rprice,
        "roo_price_discounted": rdisc, "roo_pct_off": rpct,
        "glovo_has_promo": "Y" if glovo_promo else ("N" if grow is not None else ""),
        "glovo_pct_off": gpct, "glovo_price": gprice, "glovo_revenue": grev,
        "price_gap_pct": gap, "price_confirmed": confirmed,
        "product_parity": _product_parity(rpct, glovo_promo, gpct) if grow is not None else "",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Match prodotto Glovo<->Deliveroo (v1)")
    ap.add_argument("--week", default="", help="settimana Glovo (default: ultima)")
    ap.add_argument("--no-write", action="store_true", help="solo riepilogo, niente CSV")
    args = ap.parse_args()

    df, parity = build_matches(args.week or None)
    auto   = df[df["match_type"] == "auto"]
    review = df[df["match_type"] == "review"]
    unm    = df[df["match_type"] == "unmatched"]
    tot = len(df)
    print(f"Prodotti Deliveroo-in-promo (store matchati): {tot}")
    print(f"  AUTO   : {len(auto)} ({100*len(auto)/tot:.0f}%)")
    print(f"  REVIEW : {len(review)} ({100*len(review)/tot:.0f}%)")
    print(f"  no match: {len(unm)} ({100*len(unm)/tot:.0f}%)")
    if len(auto):
        conf = auto["price_confirmed"].sum()
        print(f"  di cui prezzo confermato: {conf} ({100*conf/len(auto):.0f}% degli auto)")

    if len(parity):
        good = parity[parity["enough"]]
        print(f"\nVerdetto product-based (bilanciato): {len(parity)} store, di cui {len(good)} sopra soglia (>= {MIN_UNION} prodotti)")
        for k, v in good["parity_product"].value_counts().items():
            print(f"   {k:12}: {v}")

    if not args.no_write:
        auto.to_csv(OUT_MATCH, index=False, encoding="utf-8-sig")
        pd.concat([review, unm]).to_csv(OUT_REVIEW, index=False, encoding="utf-8-sig")
        parity.to_csv(BASE_DIR / "data" / "product_parity.csv", index=False, encoding="utf-8-sig")
        print(f"\nScritti:\n  {OUT_MATCH}\n  {OUT_REVIEW}\n  {BASE_DIR / 'data' / 'product_parity.csv'}")


if __name__ == "__main__":
    main()
