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

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

from pipeline.product_matcher import norm, norm_product, to_price, DB_PATH, MAPPING_CSV, ROO_PRODUCTS

OUT = Path(__file__).resolve().parent.parent / "data" / "store_discovery_candidates.csv"

MIN_PRODUCTS = 4      # store Deliveroo con almeno N prodotti in promo
PRICE_TOL    = 0.05   # |gap| <= -> prezzo confermato
NAME_FUZZY   = 80     # tolleranza nome prodotto: se il PREZZO coincide e il nome e'
                      # >= questa similarita', conta come stesso prodotto (piccole
                      # differenze: ordine parole, 'all'', singolare/plurale, refusi)
OUT_FIELDS = ["city_code", "deliveroo_name", "glovo_candidate", "n_overlap",
              "n_price_confirmed", "overlap_ratio", "roo_products", "confidence"]


def _glovo_index(con, city: str, week: str):
    """Due indici per la citta':
       name_idx:  norm_product(name) -> [(store, price)]      (match esatto nome)
       price_idx: euro arrotondato    -> [(store, norm, price)] (match prezzo + nome fuzzy)"""
    g = pd.read_sql(
        "SELECT DISTINCT store_name, product_name, avg_unit_price "
        "FROM glovo_products WHERE city_code=? AND week_num=?",
        con, params=[city, week],
    )
    name_idx: dict[str, list[tuple[str, float | None]]] = {}
    price_idx: dict[int, list[tuple[str, str, float]]] = {}
    for s, p, pr in zip(g.store_name, g.product_name, g.avg_unit_price):
        n = norm_product(p); price = to_price(pr)
        name_idx.setdefault(n, []).append((s, price))
        if price is not None:
            price_idx.setdefault(int(round(price)), []).append((s, n, price))
    return name_idx, price_idx


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
        name_idx, price_idx = idx_cache.setdefault(city, _glovo_index(con, city, week))
        cand: dict[str, dict] = {}
        for pname, pprice in zip(grp.product_name, grp.product_price):
            nm, rpr = norm_product(pname), to_price(pprice)
            hits: dict[str, bool] = {}   # store -> prezzo_confermato (per QUESTO prodotto)
            # 1) match esatto sul nome
            for gstore, gpr in name_idx.get(nm, []):
                ok = bool(rpr and gpr and abs(rpr - gpr) / max(rpr, gpr) <= PRICE_TOL)
                hits[gstore] = hits.get(gstore, False) or ok
            # 2) tolleranza: prezzo vicino + nome FUZZY simile (piccole differenze di nome)
            if rpr is not None:
                for b in (int(round(rpr)) - 1, int(round(rpr)), int(round(rpr)) + 1):
                    for gstore, gn, gpr in price_idx.get(b, []):
                        if gstore in hits:
                            continue
                        if abs(rpr - gpr) / max(rpr, gpr) <= PRICE_TOL and fuzz.token_sort_ratio(nm, gn) >= NAME_FUZZY:
                            hits[gstore] = True   # confermato dal prezzo per costruzione
            for gstore, ok in hits.items():
                c = cand.setdefault(gstore, {"ov": 0, "pc": 0})
                c["ov"] += 1
                if ok:
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


def _tnorm(s: str) -> str:
    """Normalizzazione stretta (senza spazi/punteggiatura): 'S.A.N.O.' -> 'sano'."""
    return norm(s).replace(" ", "")


def auto_merge(week: str | None = None) -> list[tuple[str, str, str]]:
    """Scopre i match mancanti e UNISCE in automatico solo quelli ad altissima
    confidenza (nome coincide -> contenimento, oppure prodotti fortissimi).
    I falsi positivi da nomi diversi restano fuori (revisione).
    Ritorna la lista (city, glovo_name, deliveroo_name) dei match aggiunti;
    aggiorna store_mapping.csv (con backup). NON solleva: best-effort."""
    try:
        cand = discover(week)
    except Exception as exc:
        print(f"    [store_discovery] discover fallito: {exc}")
        return []
    if cand.empty:
        return []
    mp = pd.read_csv(MAPPING_CSV, dtype=str, encoding="utf-8-sig").fillna("")
    existing = set(zip(mp["city_code"], mp["glovo_name"], mp["deliveroo_name"]))
    new_rows, added = [], []
    for _, r in cand.iterrows():
        a, b = _tnorm(r["deliveroo_name"]), _tnorm(r["glovo_candidate"])
        contained = min(len(a), len(b)) >= 4 and (a in b or b in a)
        strong = float(r["overlap_ratio"]) >= 0.7 and int(r["n_price_confirmed"]) >= 4
        if not (contained or strong):
            continue
        key = (r["city_code"], r["glovo_candidate"], r["deliveroo_name"])
        if key in existing:
            continue
        existing.add(key)
        new_rows.append({"city_code": r["city_code"], "glovo_name": r["glovo_candidate"],
                         "glovo_store_id": "", "deliveroo_name": r["deliveroo_name"],
                         "confidence": "0.95", "source": "auto_fingerprint"})
        added.append((r["city_code"], r["glovo_candidate"], r["deliveroo_name"]))
    if new_rows:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(MAPPING_CSV, MAPPING_CSV.parent / f"store_mapping_backup_{ts}.csv")
        mp2 = pd.concat([mp, pd.DataFrame(new_rows)], ignore_index=True) \
                .drop_duplicates(["city_code", "glovo_name", "deliveroo_name"], keep="first")
        mp2.to_csv(MAPPING_CSV, index=False, encoding="utf-8-sig")
    print(f"    [store_discovery] auto-merge: +{len(added)} match aggiunti al mapping "
          f"({len(cand) - len(added)} candidati incerti in revisione)")
    return added


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--merge", action="store_true", help="unisce in automatico i match ad alta confidenza")
    args = ap.parse_args()
    if args.merge:
        auto_merge()
        return
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
