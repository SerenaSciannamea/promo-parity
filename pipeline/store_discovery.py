# ===========================================================================
# store_discovery.py — scopre match store MANCATI via "fingerprint del menu".
#
# Lo store-matcher si basa sul NOME e perde casi come "Fra Diavolo"/"Fradiavolo",
# "Kebabam - Plana"/"Kebabam". Qui, per ogni store Deliveroo NON mappato,
# cerchiamo lo store Glovo che condivide piu' prodotti (nome+prezzo): un menu
# in comune e' una firma d'identita' molto piu' forte del nome.
#
# NON modifica store_mapping: genera SOLO candidati per la revisione umana.
# Output: data/store_discovery_candidates.csv
#
# Guardie di precisione (contro i falsi positivi da nomi generici su menu grandi):
#   - bonus PREZZO: nome+prezzo coincidenti ~ impossibile per caso;
#   - RATIO sul menu Deliveroo: 6/6 forte, 10/202 debole.
# ===========================================================================
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from pipeline.product_matcher import norm, to_price, DB_PATH, MAPPING_CSV, ROO_PRODUCTS

OUT = Path(__file__).resolve().parent.parent / "data" / "store_discovery_candidates.csv"

MIN_PRODUCTS = 4      # store Deliveroo con almeno N prodotti in promo
PRICE_TOL    = 0.05   # |gap| <= -> prezzo confermato
OUT_FIELDS = ["city_code", "deliveroo_name", "glovo_candidate", "n_overlap",
              "n_price_confirmed", "overlap_ratio", "roo_products", "confidence"]


def _glovo_index(con, city: str, week: str):
    """norm(product_name) -> list[(store_name, price)] per la citta'."""
    g = pd.read_sql(
        "SELECT DISTINCT store_name, product_name, avg_unit_price "
        "FROM glovo_products WHERE city_code=? AND week_num=?",
        con, params=[city, week],
    )
    idx: dict[str, list[tuple[str, float | None]]] = {}
    for s, p, pr in zip(g.store_name, g.product_name, g.avg_unit_price):
        idx.setdefault(norm(p), []).append((s, to_price(pr)))
    return idx


def discover(week: str | None = None) -> pd.DataFrame:
    mp = pd.read_csv(MAPPING_CSV, dtype=str, encoding="utf-8-sig").fillna("")
    roo = pd.read_csv(ROO_PRODUCTS, dtype=str).fillna("")
    roo = roo.drop_duplicates(["city_code", "restaurant_name", "product_name"])
    con = sqlite3.connect(DB_PATH)
    if not week:
        week = pd.read_sql("SELECT MAX(week_num) w FROM glovo_products", con)["w"][0]

    mapped = {(r.city_code, r.deliveroo_name) for r in mp.itertuples()}
    idx_cache: dict[str, dict] = {}
    rows = []
    for (city, dnm), grp in roo.groupby(["city_code", "restaurant_name"]):
        if (city, dnm) in mapped or len(grp) < MIN_PRODUCTS:
            continue
        idx = idx_cache.setdefault(city, _glovo_index(con, city, week))
        cand: dict[str, dict] = {}
        for pname, pprice in zip(grp.product_name, grp.product_price):
            nm, rpr = norm(pname), to_price(pprice)
            for gstore, gpr in idx.get(nm, []):
                c = cand.setdefault(gstore, {"ov": 0, "pc": 0})
                c["ov"] += 1
                if rpr and gpr and abs(rpr - gpr) / max(rpr, gpr) <= PRICE_TOL:
                    c["pc"] += 1
        if not cand:
            continue
        gstore, c = max(cand.items(), key=lambda kv: (kv[1]["pc"], kv[1]["ov"]))
        n_roo = len(grp)
        ratio = round(c["ov"] / n_roo, 2)
        # confidenza: prezzo confermato e' il segnale forte; il ratio taglia i generici
        if c["pc"] >= 3 or ratio >= 0.5:
            conf = "alta"
        elif c["ov"] >= 4 and ratio >= 0.3:
            conf = "media"
        else:
            continue
        rows.append({"city_code": city, "deliveroo_name": dnm, "glovo_candidate": gstore,
                     "n_overlap": c["ov"], "n_price_confirmed": c["pc"],
                     "overlap_ratio": ratio, "roo_products": n_roo, "confidence": conf})
    con.close()
    df = pd.DataFrame(rows, columns=OUT_FIELDS)
    return df.sort_values(["confidence", "n_price_confirmed", "overlap_ratio"],
                          ascending=[True, False, False]) if not df.empty else df


def main() -> None:
    df = discover()
    print(f"Candidati store scoperti: {len(df)}")
    if not df.empty:
        print(df["confidence"].value_counts().to_string())
        print("\nesempi (alta confidenza):")
        hi = df[df.confidence == "alta"].head(15)
        for _, r in hi.iterrows():
            print(f"  {r.city_code} | {r.deliveroo_name[:32]:32} -> {r.glovo_candidate[:28]:28} "
                  f"| overlap {r.n_overlap}/{r.roo_products} prezzo_ok={r.n_price_confirmed} ratio={r.overlap_ratio}")
        df.to_csv(OUT, index=False, encoding="utf-8-sig")
        print(f"\nScritto: {OUT}")


if __name__ == "__main__":
    main()
