from __future__ import annotations
"""
Promo Parity — scraper HTTP/GraphQL (PROTOTIPO, separato dall'originale Selenium).

NON sostituisce nulla: scrive in una cartella di output dedicata.
Strategia (come il vecchio scraper, ma via HTTP, 10-50x piu' veloce):
  1) POST getHomeFeed (GraphQL guest) per ogni geohash -> lista COMPLETA ristoranti
     + badge promo dal feed (promotion_tag / badges / bubble).
  2) Apre il menu SOLO degli store che hanno un badge promo nel feed.
  3) Dal menu (__NEXT_DATA__) estrae:
       - offers[] a livello store (typeName + minimumOrderValue)  -> tipo promo
       - item con priceDiscounted / offerText                     -> prodotti in promo + % off

Output (stesso schema dello scraper originale, in output_http_promo/):
  - deliveroo_promo_raw.csv         city_code, restaurant_name, promotion_type, source_url, scraped_at_utc
  - deliveroo_promo_products.csv    + product_name, product_description, product_price
  - deliveroo_sample_status.csv     city_code, geohash, lat, lon, status, restaurant_count, checked_url, scraped_at_utc
  - deliveroo_promo_offers_debug.csv  (extra: typeName, MOV, badge, pct, n_items) per costruire la mappatura
"""
import argparse
import csv
import json
import math
import os
import random
import re
import sys
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests
from shapely import wkt
from shapely.geometry import Point

# --- Watchdog interno anti-hang -------------------------------------------
# Se lo scraper non fa progressi da troppo tempo (es. un fetch di rete appeso
# che non rispetta il timeout), un thread daemon lo termina con os._exit(2):
# un HANG diventa cosi' un'USCITA pulita, che il wrapper rileva e fa il resume.
_LAST_PROGRESS = time.monotonic()


def _touch_progress() -> None:
    global _LAST_PROGRESS
    _LAST_PROGRESS = time.monotonic()


def _start_stall_watchdog(stall_seconds: float) -> None:
    def _loop():
        while True:
            time.sleep(20)
            idle = time.monotonic() - _LAST_PROGRESS
            if idle > stall_seconds:
                print(f"\n!! STALLO: nessun progresso da {idle:.0f}s (> {stall_seconds:.0f}s) "
                      f"-> esco con exit 2 per far ripartire il resume.", flush=True)
                os._exit(2)
    threading.Thread(target=_loop, daemon=True).start()


BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
GRAPHQL_ENDPOINT = "https://api.it.deliveroo.com/consumer/graphql/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
BLOCK_MARKERS = ("verifica di sicurezza", "just a moment", "attention required", "access denied", "datadome")

# Mappatura typeName Deliveroo -> categoria Promo Parity (in stile GLOVO_RANK).
# Sconosciuti: passati grezzi (loggati nel debug) per estendere la mappa.
OFFER_TYPE_MAP = {
    "BuyOneGetOneFreeOffer":        "TWO_FOR_ONE",
    "ItemSpecificPercentOffOffer":  "BASKET_PERCENTAGE",   # % off su articoli selezionati con MOV
    "FullMenuPercentOffOffer":      "BASKET_PERCENTAGE",   # % off sull'intero ordine con MOV (order-level)
    "PercentOffOffer":              "PERCENTAGE_DISCOUNT",
    "OrderLevelPercentOffOffer":    "PERCENTAGE_DISCOUNT",
    "AmountOffOffer":               "FLAT_PRODUCT",
    "FreeDeliveryOffer":            "FREE_DELIVERY",
}

RAW_FIELDS     = ["city_code", "restaurant_name", "promotion_type", "source_url", "scraped_at_utc"]
PROD_FIELDS    = ["city_code", "restaurant_name", "promotion_type", "product_name",
                  "product_description", "product_price", "product_price_discounted",
                  "source_url", "scraped_at_utc"]
DEDUP_FIELDS   = ["city_code", "restaurant_name", "promotion_type",
                  "n_stores_with_promo", "n_stores_total", "stores_pct"]
STOREIDX_FIELDS = ["city_code", "name_norm", "store_id"]   # indice di TUTTE le filiali viste (anche senza promo) = denominatore punto 5
SAMPLE_FIELDS  = ["city_code", "geohash", "lat", "lon", "status", "restaurant_count", "checked_url", "scraped_at_utc"]
DEBUG_FIELDS   = ["city_code", "restaurant_name", "feed_badge", "offer_typenames",
                  "min_order_value", "max_pct_off", "n_promo_items", "source_url", "scraped_at_utc"]


# --------------------------------------------------------------------------- #
# Geohash / poligoni (copiati dallo scraper chains)
# --------------------------------------------------------------------------- #
def geohash_encode(lat: float, lon: float, precision: int = 7) -> str:
    lat_i, lon_i = [-90.0, 90.0], [-180.0, 180.0]
    chars, even, bit, ch = [], True, 0, 0
    while len(chars) < precision:
        if even:
            mid = (lon_i[0] + lon_i[1]) / 2
            if lon >= mid: ch |= 1 << (4 - bit); lon_i[0] = mid
            else: lon_i[1] = mid
        else:
            mid = (lat_i[0] + lat_i[1]) / 2
            if lat >= mid: ch |= 1 << (4 - bit); lat_i[0] = mid
            else: lat_i[1] = mid
        even = not even
        if bit < 4: bit += 1
        else: chars.append(BASE32[ch]); bit = 0; ch = 0
    return "".join(chars)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip("| ").strip()


def parse_polygons(path: Path, selected: set[str]) -> list[tuple[str, object]]:
    out = []
    with path.open("r", encoding="utf-8-sig", newline="") as h:
        for row in csv.reader(h):
            if len(row) < 2:
                continue
            code = clean_text(row[0]).upper()
            if code in {"", "CITY_CODE", "CITY", "CODE"}:
                continue
            if selected and code not in selected:
                continue
            try:
                out.append((code, wkt.loads(row[1])))
            except Exception:
                continue
    if not out:
        raise ValueError("Nessun poligono valido")
    return out


def iter_points(geometry, step_km: float, precision: int) -> Iterable[tuple[float, float, str]]:
    minx, miny, maxx, maxy = geometry.bounds
    lat_step = step_km / 110.574
    lat = miny
    seen: set[str] = set()
    while lat <= maxy:
        lon_step = step_km / (111.320 * max(0.2, math.cos(math.radians(lat))))
        lon = minx
        while lon <= maxx:
            if geometry.covers(Point(lon, lat)):
                gh = geohash_encode(lat, lon, precision)
                if gh not in seen:
                    seen.add(gh); yield lat, lon, gh
            lon += lon_step
        lat += lat_step


def sample_points(geometry, step_km: float, precision: int) -> list[tuple[float, float, str]]:
    pts = list(iter_points(geometry, step_km, precision))
    if pts:
        return pts
    rep = geometry.representative_point()
    return [(rep.y, rep.x, geohash_encode(rep.y, rep.x, precision))]


# --------------------------------------------------------------------------- #
# Deliveroo feed (getHomeFeed con badge promo)
# --------------------------------------------------------------------------- #
GRAPHQL_QUERY = """
query getHomeFeed($location: LocationInput!, $url: String, $options: SearchOptionsInput, $uuid: String!) {
  results: search(location: $location, url: $url, options: $options, uuid: $uuid) {
    meta { restaurantCount: restaurant_count { results } }
    layoutGroups: ui_layout_groups {
      data: ui_layouts {
        __typename
        ... on UILayoutList { blocks: ui_blocks { ...card } }
        ... on UILayoutCarousel { blocks: ui_blocks { ...card } }
      }
    }
  }
}
fragment txt on UILine {
  __typename
  ... on UITextLine { spans: ui_spans { __typename ... on UISpanText { text } } }
}
fragment card on UIBlock {
  __typename
  ... on UICard {
    target { __typename ... on UITargetRestaurant { restaurant { id name links { self { href } } } } }
    uiContent: properties {
      default {
        uiLines: ui_lines { ...txt }
        bubble { uiLines: ui_lines { ...txt } }
        overlay {
          badges { text { ...txt } }
          promotionTag: promotion_tag {
            primaryTagLine: primary_tag_line { text { ...txt } }
            secondaryTagLine: secondary_tag_line { text { ...txt } }
          }
        }
      }
    }
  }
}
"""

# Badge promo dal feed (affidabili). INCLUDE "tutto il menu" = order-level (FullMenu).
# ESCLUDE "consegna/spedizione gratis" (free delivery, per policy) e banner generici.
_PROMO_KW = re.compile(r"tutto il men|articoli\s+selezionat|\d\s*%|\bsconto|2\s*al\s*prezzo|2x1|\bspendi\b|risparmi|prezzo speciale|omaggio", re.I)
_FD_KW    = re.compile(r"consegna\s*grat|spedizione\s*grat|free\s*delivery", re.I)


def build_headers() -> dict:
    return {
        "accept": "application/json", "accept-language": "it", "content-type": "application/json",
        "origin": "https://deliveroo.it", "referer": "https://deliveroo.it/", "user-agent": UA,
        "x-roo-client": "consumer-web-app", "x-roo-country": "it", "x-roo-platform": "web",
        "x-roo-external-device-id": str(uuid.uuid4()), "x-roo-guid": str(uuid.uuid4()),
        "x-roo-session-guid": str(uuid.uuid4()), "x-roo-sticky-guid": str(uuid.uuid4()),
    }


def build_body(gh12: str, collection: str = "offers") -> dict:
    return {"query": GRAPHQL_QUERY, "variables": {
        "location": {"geohash": gh12, "city_uname": "prova", "neighborhood_uname": "prova", "postcode": ""},
        "url": f"https://deliveroo.it/it/restaurants/prova/prova/?geohash={gh12}&collection={collection}",
        "options": {"query": "", "web_column_count": 3}, "uuid": str(uuid.uuid4())}}


def _line_texts(lines) -> list[str]:
    out = []
    for ln in lines or []:
        for sp in (ln.get("spans") or []):
            t = (sp.get("text") or "").strip()
            if t:
                out.append(t)
    return out


def _walk_cards(obj, found):
    if isinstance(obj, dict):
        if obj.get("__typename") == "UICard" and isinstance(obj.get("target"), dict):
            r = (obj["target"] or {}).get("restaurant")
            if r:
                found.append(obj)
        for v in obj.values():
            _walk_cards(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_cards(v, found)


def fetch_feed(session, gh12, timeout, collection="offers"):
    """Ritorna (lista store [{id,name,href,badge}], restaurant_count). 'badge' = testo promo dal feed o ''."""
    resp = session.post(GRAPHQL_ENDPOINT, headers=build_headers(), json=build_body(gh12, collection), timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"GraphQL HTTP {resp.status_code}")
    if any(m in resp.text[:2000].lower() for m in BLOCK_MARKERS):
        raise RuntimeError("blocked")
    data = resp.json()
    results = (data.get("data") or {}).get("results") or {}
    count = ((results.get("meta") or {}).get("restaurantCount") or {}).get("results")
    cards = []
    _walk_cards(results, cards)
    # Uno stesso store puo' comparire piu' volte (lista principale + caroselli tipo
    # "Netflix"/"FMCG"): una occorrenza puo' avere il badge sconto, un'altra no.
    # Aggrego i testi di TUTTE le occorrenze e poi scelgo il badge -> niente badge perso.
    agg: "OrderedDict[str, dict]" = OrderedDict()
    for c in cards:
        r = c["target"]["restaurant"]
        href = (((r.get("links") or {}).get("self") or {}).get("href")) or ""
        key = href or r.get("name")
        if not key:
            continue
        default = ((c.get("uiContent") or {}).get("default")) or {}
        texts = _line_texts(default.get("uiLines"))
        bub = default.get("bubble") or {}
        texts += _line_texts(bub.get("uiLines"))
        ov = default.get("overlay") or {}
        for b in (ov.get("badges") or []):
            texts += _line_texts([(b.get("text") or {})])
        pt = ov.get("promotionTag") or {}
        for tl in ("primaryTagLine", "secondaryTagLine"):
            line = (pt.get(tl) or {}).get("text")
            if line:
                texts += _line_texts([line])
        if key not in agg:
            agg[key] = {"id": str(r.get("id", "")), "name": clean_text(r.get("name", "")),
                        "href": href, "texts": set()}
        agg[key]["texts"].update(t for t in texts if t.strip())
    out = []
    for v in agg.values():
        # badge = uno sconto qualsiasi (anche se accanto c'e' "Consegna gratis")
        badge = next((t for t in v["texts"] if _PROMO_KW.search(t) and not _FD_KW.search(t)), "")
        out.append({"id": v["id"], "name": v["name"], "href": v["href"], "badge": clean_text(badge)})
    return out, (count or len(out))


# --------------------------------------------------------------------------- #
# Menu: __NEXT_DATA__ -> offers + prodotti in promo
# --------------------------------------------------------------------------- #
_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _collect(obj, items, offers):
    if isinstance(obj, dict):
        if isinstance(obj.get("name"), str) and isinstance(obj.get("price"), dict) \
                and "priceDiscounted" in obj and "offerText" in obj:
            items.append(obj)
        if "typeName" in obj and "minimumOrderValue" in obj:  # itemIds opzionale (order-level non li ha)
            offers.append(obj)
        for v in obj.values():
            _collect(v, items, offers)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, items, offers)


def _pct_off(price, disc) -> float:
    try:
        p = (price or {}).get("fractional"); d = (disc or {}).get("fractional")
        if p and d and p > 0:
            return round((p - d) / p * 100, 1)
    except Exception:
        pass
    return 0.0


def get_menu_promo(session, href, timeout):
    """GET menu -> (promotion_type, products[list], debug{}). Solo prodotti IN PROMO."""
    url = f"https://deliveroo.it{href}" if href.startswith("/") else href
    resp = session.get(url, headers={"user-agent": UA, "accept-language": "it"}, timeout=timeout)
    if resp.status_code != 200:
        return "", [], {}, url
    html = resp.text
    if any(m in html[:2000].lower() for m in BLOCK_MARKERS):
        raise RuntimeError("blocked")
    m = _NEXT_RE.search(html)
    if not m:
        return "", [], {}, url
    try:
        data = json.loads(m.group(1))
    except Exception:
        return "", [], {}, url
    items, offers = [], []
    _collect(data, items, offers)

    # Prodotto in promo = item REALMENTE scontato (ha priceDiscounted). Il solo offerText
    # (banner dell'offerta order-level su ogni item) NON conta -> order-level => 0 prodotti.
    promo_items = [it for it in items if it.get("priceDiscounted")]
    # dedup item per (name, price) — il menu puo' ripetere blocchi
    seen, prod = set(), []
    max_pct = 0.0
    for it in promo_items:
        nm = clean_text(it.get("name", ""))
        price = (it.get("price") or {}).get("formatted", "")
        disc = (it.get("priceDiscounted") or {}).get("formatted", "")
        k = (nm.lower(), price)
        if not nm or k in seen:
            continue
        seen.add(k)
        pct = _pct_off(it.get("price"), it.get("priceDiscounted"))
        max_pct = max(max_pct, pct)
        prod.append({
            "product_name": nm,
            "product_description": clean_text(it.get("description", ""))[:300],
            "product_price": price,
            "product_price_discounted": disc,
        })

    # Free delivery e' escluso per policy aziendale: non conta come promo.
    real_offers = [o for o in offers if o.get("typeName") != "FreeDeliveryOffer"]
    typenames = [o.get("typeName", "") for o in offers if o.get("typeName")]
    PRIORITY = {"BuyOneGetOneFreeOffer": 0, "ItemSpecificPercentOffOffer": 1, "FullMenuPercentOffOffer": 2}
    primary = sorted(real_offers, key=lambda o: PRIORITY.get(o.get("typeName", ""), 9))[0] if real_offers else None

    # Order-level (FullMenu): lo sconto e' sull'intero ordine, NON su item specifici -> 0 prodotti
    # (Deliveroo mostra priceDiscounted su ogni item solo come anteprima al raggiungimento del MOV).
    if primary and primary.get("typeName") == "FullMenuPercentOffOffer":
        prod = []

    promotion_type, mov = "", ""
    if primary:
        tn = primary.get("typeName", "")
        movf = (primary.get("minimumOrderValue") or {}).get("formatted", "")
        mov = "" if movf in ("0,00 €", "0.00 €", "") else movf
        pct = primary.get("percentageDiscount")
        if pct is None and max_pct:
            pct = max_pct
        if tn == "BuyOneGetOneFreeOffer":
            promotion_type = "2 al prezzo di 1"
        elif tn == "FullMenuPercentOffOffer":   # order-level
            promotion_type = (f"Spendi almeno {mov}, risparmia {pct:g}%" if (mov and pct)
                              else (f"{pct:g}% di sconto sull'ordine" if pct else "Sconto sull'ordine"))
        elif tn == "ItemSpecificPercentOffOffer":
            promotion_type = (f"Spendi {mov} per -{pct:g}% su articoli selezionati" if (mov and pct)
                              else (f"{pct:g}% di sconto" if pct else "Sconto su articoli selezionati"))
        elif OFFER_TYPE_MAP.get(tn) == "FREE_DELIVERY":
            promotion_type = "Consegna gratis"
        else:
            promotion_type = OFFER_TYPE_MAP.get(tn, tn)
    elif prod and max_pct:   # solo item scontati (nessun offer non-FD a livello store)
        promotion_type = f"{max_pct:g}% di sconto"

    debug = {"offer_typenames": "|".join(typenames), "min_order_value": mov,
             "max_pct_off": f"{max_pct:g}" if max_pct else "", "n_promo_items": str(len(prod))}
    return promotion_type, prod, debug, url


# --------------------------------------------------------------------------- #
def ensure_csv(path, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", encoding="utf-8-sig", newline="") as h:
            csv.DictWriter(h, fieldnames=fields).writeheader()


def append_rows(path, fields, rows):
    if not rows:
        return
    ensure_csv(path, fields)
    with path.open("a", encoding="utf-8-sig", newline="") as h:
        csv.DictWriter(h, fieldnames=fields).writerows(rows)


def load_processed(samples_csv):
    done = set()
    if not samples_csv.exists():
        return done
    with samples_csv.open("r", encoding="utf-8-sig", newline="") as h:
        for row in csv.DictReader(h):
            st = clean_text(row.get("status", "")).lower()
            if not st.startswith("block") and not st.startswith("error"):
                done.add((clean_text(row.get("city_code", "")).upper(), clean_text(row.get("geohash", ""))))
    return done


def _norm_dedup(name: str) -> str:
    s = unicodedata.normalize("NFKD", clean_text(name).lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def _load_store_totals(idx_csv: Path) -> dict:
    """(city, name_norm) -> n. TOTALE filiali distinte viste (anche senza promo). Robusto al resume."""
    totals: dict[tuple, set] = {}
    if not idx_csv.exists():
        return {}
    with idx_csv.open("r", encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            city = clean_text(r.get("city_code", "")).upper()
            nm = clean_text(r.get("name_norm", ""))
            sid = clean_text(r.get("store_id", ""))
            if not city or not nm or not sid:
                continue
            totals.setdefault((city, nm), set()).add(sid)
    return {k: len(v) for k, v in totals.items()}


def write_deduped(raw_csv: Path, ded_csv: Path, idx_csv: Path | None = None) -> int:
    """deliveroo_promo_deduped.csv: 1 riga per (citta', nome, PROMO).
    - promo identiche sullo stesso nome -> collassate (punto 2)
    - promo diverse sullo stesso nome -> righe separate (punto 3)
    - n_stores_with_promo = filiali distinte con QUELLA promo; n_stores_total = filiali totali con quel nome;
      stores_pct = percentuale (punto 5).
    """
    if not raw_csv.exists():
        return 0
    totals = _load_store_totals(idx_csv) if idx_csv else {}
    # (city, name_norm, promo) -> {display name, set(source_url distinti con quella promo)}
    grouped: OrderedDict[tuple, dict] = OrderedDict()
    with raw_csv.open("r", encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            city = clean_text(r.get("city_code", "")).upper()
            name = clean_text(r.get("restaurant_name", ""))
            promo = clean_text(r.get("promotion_type", ""))
            url = clean_text(r.get("source_url", ""))
            if not city or not name:
                continue
            nm = _norm_dedup(name)
            key = (city, nm, promo)
            if key not in grouped:
                grouped[key] = {"city_code": city, "restaurant_name": name,
                                "promotion_type": promo, "name_norm": nm, "urls": set()}
            grouped[key]["urls"].add(url or name)
    rows = []
    for g in grouped.values():
        n_with = len(g["urls"])
        n_tot = totals.get((g["city_code"], g["name_norm"]), 0) or n_with
        pct = round(100 * n_with / n_tot) if n_tot else 0
        rows.append({"city_code": g["city_code"], "restaurant_name": g["restaurant_name"],
                     "promotion_type": g["promotion_type"], "n_stores_with_promo": n_with,
                     "n_stores_total": n_tot, "stores_pct": pct})
    with ded_csv.open("w", encoding="utf-8-sig", newline="") as h:
        w = csv.DictWriter(h, fieldnames=DEDUP_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def parse_args():
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Promo Parity scraper HTTP (prototipo)")
    p.add_argument("--polygons", type=Path,
                   default=Path(r"C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena\Polygons.csv"))
    p.add_argument("--output-dir", type=Path, default=here / "output_http_promo")
    p.add_argument("--city-codes", default="", help="vuoto = TUTTI i poligoni di Polygons.csv")
    p.add_argument("--collection", default="offers",
                   help="collezione feed: 'offers' (consigliato, lista promo affidabile) o 'all-restaurants'")
    p.add_argument("--sample-step-km", type=float, default=3.0)   # griglia standard 3 km
    p.add_argument("--big-cities", default="MIL", help="citta' grandi a griglia piu' larga (--big-city-step-km) per alleggerire DataDome")
    p.add_argument("--big-city-step-km", type=float, default=4.0)  # MIL: 4 km -> meno geohash, meno carico
    p.add_argument("--geohash-precision", type=int, default=7)
    p.add_argument("--api-geohash-precision", type=int, default=12)
    p.add_argument("--max-total-points", type=int, default=0, help="0 = tutti i geohash")
    p.add_argument("--max-stores", type=int, default=0, help="cap aperture menu; 0 = nessun cap")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--min-delay", type=float, default=1.0)
    p.add_argument("--max-delay", type=float, default=2.5)
    p.add_argument("--store-delay", type=float, default=0.8)
    p.add_argument("--rest-every", type=int, default=50, help="pausa lunga ogni N menu aperti (anti-bot)")
    p.add_argument("--rest-seconds", type=float, default=30.0)
    p.add_argument("--restart-session-every", type=int, default=80, help="ricrea la sessione HTTP ogni N menu (anti-bot DataDome)")
    p.add_argument("--stall-timeout", type=float, default=240, help="anti-hang: se nessun progresso da N secondi -> auto-exit(2) per il resume")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    _start_stall_watchdog(args.stall_timeout)   # anti-hang: auto-exit(2) se si blocca
    _touch_progress()
    selected = {clean_text(c).upper() for c in args.city_codes.split(",") if clean_text(c)}
    out = args.output_dir
    raw_csv, prod_csv = out / "deliveroo_promo_raw.csv", out / "deliveroo_promo_products.csv"
    samp_csv, dbg_csv = out / "deliveroo_sample_status.csv", out / "deliveroo_promo_offers_debug.csv"
    idx_csv = out / "deliveroo_store_index.csv"
    for p, f in [(raw_csv, RAW_FIELDS), (prod_csv, PROD_FIELDS), (samp_csv, SAMPLE_FIELDS),
                 (dbg_csv, DEBUG_FIELDS), (idx_csv, STOREIDX_FIELDS)]:
        ensure_csv(p, f)

    polygons = parse_polygons(args.polygons, selected)
    big = {clean_text(c).upper() for c in args.big_cities.split(",") if clean_text(c)}
    city_points = OrderedDict(
        (c, sample_points(g, args.big_city_step_km if c in big else args.sample_step_km, args.geohash_precision))
        for c, g in polygons
    )
    city_points = OrderedDict(sorted(city_points.items(), key=lambda kv: len(kv[1])))
    processed = load_processed(samp_csv)

    session = requests.Session()
    stores_opened = 0
    seen_stores: set[str] = set()
    seen_idx: set[tuple] = set()    # (city, name_norm, store_id) gia' indicizzati in questa sessione
    n_raw = 0
    start = time.time()
    try:
        for city, points in city_points.items():
            pts = points if not args.max_total_points else points[:args.max_total_points]
            print(f"=== {city} === geohash da visitare: {len(pts)}", flush=True)
            for lat, lon, gh in pts:
                _touch_progress()
                if (city, gh) in processed:
                    continue
                if args.max_stores and stores_opened >= args.max_stores:
                    print("Raggiunto cap --max-stores, stop prototipo.", flush=True); break
                gh12 = geohash_encode(lat, lon, args.api_geohash_precision)
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    stores, rcount = fetch_feed(session, gh12, args.timeout, args.collection)
                except Exception as exc:
                    print(f"  feed errore: {exc}", flush=True)
                    append_rows(samp_csv, SAMPLE_FIELDS, [{"city_code": city, "geohash": gh, "lat": f"{lat:.6f}",
                        "lon": f"{lon:.6f}", "status": "error", "restaurant_count": "0", "checked_url": "", "scraped_at_utc": ts}])
                    continue
                # Apri il menu SOLO degli store con badge promo nel feed (esclude consegna-gratis
                # e no-promo). Il badge include "Tutto il menu" (order-level) -> niente piu' aperture inutili.
                # Indicizza TUTTE le filiali viste (anche senza promo) -> denominatore "Stores with this promo".
                idx_rows = []
                for s in stores:
                    nm = _norm_dedup(s["name"])
                    sid_all = s["id"] or s["href"]
                    if not nm or not sid_all:
                        continue
                    k = (city, nm, sid_all)
                    if k in seen_idx:
                        continue
                    seen_idx.add(k)
                    idx_rows.append({"city_code": city, "name_norm": nm, "store_id": sid_all})
                if idx_rows:
                    append_rows(idx_csv, STOREIDX_FIELDS, idx_rows)
                promo_stores = [s for s in stores if s["badge"]]
                print(f"[{gh}] badge promo={len(promo_stores)} (su {rcount} ristoranti)", flush=True)
                for s in promo_stores:
                    if args.max_stores and stores_opened >= args.max_stores:
                        break
                    sid = s["id"] or s["href"]
                    if sid in seen_stores:
                        continue
                    seen_stores.add(sid)
                    try:
                        ptype, prods, dbg, url = get_menu_promo(session, s["href"], args.timeout)
                    except Exception as exc:
                        if "block" in str(exc).lower():
                            print("  -> BLOCCO. Stop.", flush=True)
                            append_rows(samp_csv, SAMPLE_FIELDS, [{"city_code": city, "geohash": gh, "lat": f"{lat:.6f}",
                                "lon": f"{lon:.6f}", "status": "blocked", "restaurant_count": str(rcount), "checked_url": "", "scraped_at_utc": ts}])
                            raise
                        continue
                    stores_opened += 1
                    _touch_progress()   # progresso ad ogni menu aperto
                    if args.rest_every and stores_opened % args.rest_every == 0:
                        print(f"    -> respiro {args.rest_seconds:.0f}s (menu aperti={stores_opened})", flush=True)
                        time.sleep(args.rest_seconds)
                    if args.restart_session_every and stores_opened % args.restart_session_every == 0:
                        session.close(); session = requests.Session()
                        print(f"    -> sessione HTTP riavviata (anti-bot, menu aperti={stores_opened})", flush=True)
                    sts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    # Scrivi SOLO se il MENU conferma la promo (offer mappato o prodotti scontati).
                    # Il badge del feed e' solo un pre-filtro e puo' essere generico.
                    if not ptype and not prods:
                        continue
                    final_type = ptype or s["badge"]
                    append_rows(raw_csv, RAW_FIELDS, [{"city_code": city, "restaurant_name": s["name"],
                        "promotion_type": final_type, "source_url": url, "scraped_at_utc": sts}])
                    n_raw += 1
                    append_rows(prod_csv, PROD_FIELDS, [{"city_code": city, "restaurant_name": s["name"],
                        "promotion_type": final_type, "product_name": pr["product_name"],
                        "product_description": pr["product_description"], "product_price": pr["product_price"],
                        "product_price_discounted": pr.get("product_price_discounted", ""),
                        "source_url": url, "scraped_at_utc": sts} for pr in prods])
                    append_rows(dbg_csv, DEBUG_FIELDS, [{"city_code": city, "restaurant_name": s["name"],
                        "feed_badge": s["badge"], "offer_typenames": dbg.get("offer_typenames", ""),
                        "min_order_value": dbg.get("min_order_value", ""), "max_pct_off": dbg.get("max_pct_off", ""),
                        "n_promo_items": dbg.get("n_promo_items", ""), "source_url": url, "scraped_at_utc": sts}])
                    print(f"    + {s['name']} | {final_type} | prodotti={len(prods)}", flush=True)
                    time.sleep(args.store_delay)
                append_rows(samp_csv, SAMPLE_FIELDS, [{"city_code": city, "geohash": gh, "lat": f"{lat:.6f}",
                    "lon": f"{lon:.6f}", "status": "covered", "restaurant_count": str(rcount), "checked_url": gh12, "scraped_at_utc": ts}])
                processed.add((city, gh))
                time.sleep(random.uniform(args.min_delay, args.max_delay))
            if args.max_stores and stores_opened >= args.max_stores:
                break
    finally:
        session.close()
    n_ded = write_deduped(raw_csv, out / "deliveroo_promo_deduped.csv", idx_csv)
    print(f"\nFatto. store in promo scritti={n_raw} | deduped={n_ded} | menu aperti={stores_opened} | {(time.time()-start)/60:.1f} min", flush=True)
    print(f"Output in: {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
