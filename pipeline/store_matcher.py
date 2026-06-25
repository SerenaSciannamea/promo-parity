"""
store_matcher.py
Matching tra nomi store Glovo e nomi ristoranti Deliveroo.

Strategia a 3 livelli (in ordine di priorita'):
  1. GROUND TRUTH   — mapping manuale in data/store_mapping.csv
                      (city_code, glovo_name) -> deliveroo_name, confidence=1.0
  2. AUTO STRICT    — strip prefissi codice, normalizzazione, token_sort_ratio >= soglia
                      Solo accettato se nessun altro candidato supera soglia-10
                      confidence = score/100
  3. NEEDS REVIEW   — tutto il resto finisce in data/needs_review.csv
                      per validazione manuale prima di entrare nel ground truth

Perche' non usare solo fuzzy?
  - I nomi Glovo spesso hanno prefissi codice (es. "AKC - ") che scompaiono
    su Deliveroo
  - Gli store chain (McDonald's, Burger King) matchano tutto -> falsi positivi
  - Una soglia bassa produce molti finti match; una alta ne perde troppi

  Con questo approccio:
  - I match certi (ground truth) non vengono mai ricalcolati
  - I match automatici sono conservativi (soglia 88)
  - I dubbi vanno in revisione -> validazione umana -> entrano nel ground truth
"""

from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    from rapidfuzz import fuzz, process as rf_process
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

# ---------------------------------------------------------------------------
# Percorsi default
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
MAPPING_CSV   = BASE_DIR / "data" / "store_mapping.csv"
REVIEW_CSV    = BASE_DIR / "data" / "needs_review.csv"

MAPPING_COLS  = ["city_code", "glovo_name", "glovo_store_id", "deliveroo_name", "confidence", "source"]
REVIEW_COLS   = ["city_code", "glovo_name", "glovo_store_id", "candidate_deliveroo", "score", "reason"]

AUTO_THRESHOLD  = 88   # soglia minima per accettare un match automatico
AMBIG_GAP       = 10   # se il 2° candidato e' entro X punti dal 1°, match ambiguo
REVIEW_MIN_SCORE = 70  # score minimo per finire in coda revisione (sotto = scartato silenziosamente)

# ---------------------------------------------------------------------------
# Normalizzazione nomi
# ---------------------------------------------------------------------------
_PREFIX_RE = re.compile(r"^[A-Z]{2,5}\s*[-–]\s*")   # es. "AKC - ", "MY - "
_PUNCT_RE  = re.compile(r"[^\w\s]")
_SPACE_RE  = re.compile(r"\s+")

# Parole generiche che non aiutano il match (articoli, preposizioni, suffissi comuni)
_STOPWORDS = {
    "il", "la", "lo", "le", "gli", "i", "un", "una", "uno",
    "di", "da", "a", "in", "su", "per", "con", "tra", "fra",
    "del", "della", "dello", "dei", "degli", "delle",
    "al", "alla", "allo", "ai", "agli", "alle",
    "dal", "dalla", "dallo", "dai", "dagli", "dalle",
    "restaurant", "ristorante", "pizzeria", "trattoria",
    "bar", "cafe", "caffe", "osteria",
}


def _normalize(name: str) -> str:
    """
    Normalizza un nome store per il matching:
    1. Rimuove prefissi codice tipo "AKC - "
    2. Converte in lowercase
    3. Rimuove accenti
    4. Rimuove punteggiatura
    5. Rimuove stopword generiche
    6. Collassa spazi multipli
    """
    if not name:
        return ""
    s = str(name)
    s = _PREFIX_RE.sub("", s)                          # strip prefisso codice
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")  # rimuove accenti
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)                           # punteggiatura -> spazio
    tokens = [t for t in s.split() if t not in _STOPWORDS]
    s = " ".join(tokens)
    s = _SPACE_RE.sub(" ", s).strip()
    return s


# ---------------------------------------------------------------------------
# Caricamento / salvataggio mapping
# ---------------------------------------------------------------------------

def _ensure_csv(path: Path, cols: list[str]) -> None:
    """Crea il CSV con intestazione se non esiste."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(cols)


def load_mapping(path: Path = MAPPING_CSV) -> pd.DataFrame:
    """
    Carica il mapping ground truth.
    Restituisce DataFrame vuoto se il file non esiste ancora.
    """
    _ensure_csv(path, MAPPING_COLS)
    df = pd.read_csv(path, dtype=str).fillna("")
    # Assicura che tutte le colonne esistano
    for col in MAPPING_COLS:
        if col not in df.columns:
            df[col] = ""
    return df


def save_mapping(df: pd.DataFrame, path: Path = MAPPING_CSV) -> None:
    """Salva il mapping su CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df[MAPPING_COLS].to_csv(path, index=False, encoding="utf-8")


def load_review_queue(path: Path = REVIEW_CSV) -> pd.DataFrame:
    """Carica la coda di revisione."""
    _ensure_csv(path, REVIEW_COLS)
    return pd.read_csv(path, dtype=str).fillna("")


def save_review_queue(df: pd.DataFrame, path: Path = REVIEW_CSV) -> None:
    """Salva la coda di revisione."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df[REVIEW_COLS].to_csv(path, index=False, encoding="utf-8")


# ---------------------------------------------------------------------------
# Import iniziale da Stores.csv (one-shot)
# ---------------------------------------------------------------------------

def import_stores_csv(stores_csv: str | Path,
                      mapping_path: Path = MAPPING_CSV) -> pd.DataFrame:
    """
    Importa i match gia' verificati da Stores.csv (colonna deliveroo_name non vuota)
    nel ground truth.

    Stores.csv atteso: city_code, store_id, store_name (Glovo), deliveroo_name
    """
    df_stores = pd.read_csv(stores_csv, dtype=str).fillna("")
    df_stores.columns = [c.strip() for c in df_stores.columns]

    existing = load_mapping(mapping_path)
    existing_keys = set(zip(existing["city_code"], existing["glovo_name"]))

    new_rows = []
    for _, row in df_stores.iterrows():
        city      = row.get("city_code", "").strip()
        glovo_nm  = row.get("store_name", "").strip()
        store_id  = row.get("store_id", "").strip()
        deliv_nm  = row.get("deliveroo_name", "").strip()

        if not deliv_nm:
            continue  # non ancora matchato manualmente
        if (city, glovo_nm) in existing_keys:
            continue  # gia' nel ground truth

        new_rows.append({
            "city_code":       city,
            "glovo_name":      glovo_nm,
            "glovo_store_id":  store_id,
            "deliveroo_name":  deliv_nm,
            "confidence":      "1.0",
            "source":          "manual_stores_csv",
        })
        existing_keys.add((city, glovo_nm))

    if new_rows:
        combined = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
        save_mapping(combined, mapping_path)
        print(f"[store_matcher] Importati {len(new_rows)} match da Stores.csv nel ground truth")
    else:
        combined = existing
        print("[store_matcher] Nessun nuovo match da Stores.csv da importare")

    return combined


# ---------------------------------------------------------------------------
# Core matching logic
# ---------------------------------------------------------------------------

def _fuzzy_score(a: str, b: str) -> float:
    """token_sort_ratio tra due stringhe normalizzate."""
    if not HAS_RAPIDFUZZ:
        raise ImportError("rapidfuzz non installato. Esegui: pip install rapidfuzz")
    return fuzz.token_sort_ratio(a, b)


def match_glovo_stores(
    glovo_names: list[tuple[str, str, str]],   # [(city_code, glovo_name, store_id), ...]
    deliveroo_names: list[tuple[str, str]],     # [(city_code, restaurant_name), ...]
    mapping_path: Path = MAPPING_CSV,
    review_path:  Path = REVIEW_CSV,
    threshold:    int  = AUTO_THRESHOLD,
    ambig_gap:    int  = AMBIG_GAP,
) -> dict[tuple[str, str], list[str]]:
    """
    Esegue il matching per una lista di store Glovo contro i ristoranti Deliveroo.

    Restituisce:
        { (city_code, glovo_name): [deliveroo_name, ...] }
        Lista vuota = non trovato / in revisione / risolto come esclusiva-Glovo
        o non-su-Deliveroo. Uno store Glovo puo' essere matchato a PIU' ristoranti
        Deliveroo (stessa insegna a indirizzi diversi) -> matching 1:N.
    """
    mapping    = load_mapping(mapping_path)
    review_q   = load_review_queue(review_path)
    new_mapping_rows  = []
    new_review_rows   = []

    # Indice ground truth: (city, glovo_name_lower) -> [deliveroo_name, ...]
    # Le righe con deliveroo_name vuoto (esclusiva / non su Deliveroo) marcano lo
    # store come "gia' risolto manualmente" -> non va ri-processato col fuzzy.
    gt_index: dict[tuple[str, str], list[str]] = {}
    resolved_negative: set[tuple[str, str]] = set()
    for _, r in mapping.iterrows():
        gkey = (r["city_code"].strip(), r["glovo_name"].strip().lower())
        dn = str(r["deliveroo_name"]).strip()
        if dn:
            lst = gt_index.setdefault(gkey, [])
            if dn not in lst:
                lst.append(dn)
        else:
            resolved_negative.add(gkey)

    # Indice revisione gia' in coda: non riprocessare
    in_review = {
        (r["city_code"].strip(), r["glovo_name"].strip().lower())
        for _, r in review_q.iterrows()
    }

    # Raggruppa nomi Deliveroo per citta'
    deliv_by_city: dict[str, list[str]] = {}
    for city, rest_name in deliveroo_names:
        deliv_by_city.setdefault(city, []).append(rest_name)

    result: dict[tuple[str, str], list[str]] = {}

    for city, glovo_nm, store_id in glovo_names:
        key_lower = glovo_nm.lower()
        gt_key    = (city, key_lower)

        # ----- Livello 1: ground truth (puo' essere 1:N) -----
        if gt_key in gt_index:
            result[(city, glovo_nm)] = list(gt_index[gt_key])
            continue

        # ----- Risolto manualmente come esclusiva / non su Deliveroo -----
        if gt_key in resolved_negative:
            result[(city, glovo_nm)] = []
            continue

        # ----- Gia' in coda di revisione -----
        if gt_key in in_review:
            result[(city, glovo_nm)] = []
            continue

        # ----- Livello 2: fuzzy strict -----
        candidates = deliv_by_city.get(city, [])
        if not candidates or not HAS_RAPIDFUZZ:
            # Nessun candidato Deliveroo per questa citta': non va in revisione,
            # e' semplicemente uno store non presente su Deliveroo (o non ancora scrappato)
            result[(city, glovo_nm)] = []
            continue

        norm_glovo = _normalize(glovo_nm)
        norm_candidates = [(c, _normalize(c)) for c in candidates]

        scored = sorted(
            [(c, _fuzzy_score(norm_glovo, nc)) for c, nc in norm_candidates],
            key=lambda x: x[1],
            reverse=True,
        )

        best_name, best_score = scored[0]
        second_score = scored[1][1] if len(scored) > 1 else 0

        if best_score >= threshold and (best_score - second_score) >= ambig_gap:
            # Match automatico accettato
            result[(city, glovo_nm)] = [best_name]
            new_mapping_rows.append({
                "city_code":       city,
                "glovo_name":      glovo_nm,
                "glovo_store_id":  store_id,
                "deliveroo_name":  best_name,
                "confidence":      str(round(best_score / 100, 3)),
                "source":          "auto_fuzzy",
            })
            gt_index[gt_key] = [best_name]
        elif best_score >= REVIEW_MIN_SCORE:
            # Ambiguo o sotto soglia ma abbastanza alto da valere la revisione
            result[(city, glovo_nm)] = []
            reason = "below_threshold" if best_score < threshold else "ambiguous"
            new_review_rows.append({
                "city_code":           city,
                "glovo_name":          glovo_nm,
                "glovo_store_id":      store_id,
                "candidate_deliveroo": best_name,
                "score":               str(round(best_score, 1)),
                "reason":              reason,
            })
            in_review.add(gt_key)
        else:
            # Score troppo basso: store probabilmente non su Deliveroo, scarta silenziosamente
            result[(city, glovo_nm)] = []

    # Salva nuovi match automatici nel ground truth
    if new_mapping_rows:
        updated = pd.concat([mapping, pd.DataFrame(new_mapping_rows)], ignore_index=True)
        # Dedup sulla tripla (city, glovo, deliveroo): preserva il 1:N ma evita righe identiche
        updated = updated.drop_duplicates(
            subset=["city_code", "glovo_name", "deliveroo_name"], keep="last"
        )
        save_mapping(updated, mapping_path)
        print(f"[store_matcher] {len(new_mapping_rows)} nuovi match automatici salvati")

    # Salva nuove voci in coda di revisione
    if new_review_rows:
        updated_review = pd.concat([review_q, pd.DataFrame(new_review_rows)], ignore_index=True)
        # Dedup sulla chiave (city_code, glovo_name) - mantieni l'ultima
        updated_review = updated_review.drop_duplicates(
            subset=["city_code", "glovo_name"], keep="last"
        )
        save_review_queue(updated_review, review_path)
        print(f"[store_matcher] {len(new_review_rows)} store aggiunti alla coda di revisione")

    return result


def confirm_match(city_code: str, glovo_name: str, deliveroo_name: str,
                  mapping_path: Path = MAPPING_CSV,
                  review_path:  Path = REVIEW_CSV) -> None:
    """
    Conferma manualmente un match dalla coda di revisione.
    Sposta la voce da needs_review.csv a store_mapping.csv con confidence=1.0.
    """
    mapping = load_mapping(mapping_path)
    review  = load_review_queue(review_path)

    # Trova store_id dalla coda
    rev_row = review[
        (review["city_code"] == city_code) & (review["glovo_name"] == glovo_name)
    ]
    store_id = rev_row["glovo_store_id"].iloc[0] if len(rev_row) > 0 else ""

    # Lo store ora ha (almeno) un match: rimuovi eventuali righe "negative"
    # (esclusiva-Glovo / non-su-Deliveroo) per questo store.
    _is_glovo = (mapping["city_code"] == city_code) & (mapping["glovo_name"] == glovo_name)
    _is_neg   = mapping["deliveroo_name"].fillna("").astype(str).str.strip() == ""
    mapping   = mapping[~(_is_glovo & _is_neg)]

    new_row = pd.DataFrame([{
        "city_code":       city_code,
        "glovo_name":      glovo_name,
        "glovo_store_id":  store_id,
        "deliveroo_name":  deliveroo_name,
        "confidence":      "1.0",
        "source":          "manual_confirmed",
    }])
    # Dedup sulla TRIPLA (city, glovo, deliveroo): additivo -> un Glovo puo' avere
    # piu' Deliveroo (1:N), ma non righe duplicate.
    updated_mapping = pd.concat([mapping, new_row], ignore_index=True).drop_duplicates(
        subset=["city_code", "glovo_name", "deliveroo_name"], keep="last"
    )
    save_mapping(updated_mapping, mapping_path)

    # Rimuovi dalla coda revisione
    updated_review = review[
        ~((review["city_code"] == city_code) & (review["glovo_name"] == glovo_name))
    ]
    save_review_queue(updated_review, review_path)
    print(f"[store_matcher] Match confermato: {city_code} | {glovo_name} -> {deliveroo_name}")


def set_matches(city_code: str, glovo_name: str, deliveroo_names: list[str],
                mapping_path: Path = MAPPING_CSV,
                review_path:  Path = REVIEW_CSV) -> None:
    """
    Imposta l'insieme COMPLETO dei match Deliveroo per uno store Glovo (1:N).
    Sostituisce tutte le righe esistenti di quel Glovo (positive e negative) con
    una riga per ogni nome in `deliveroo_names`. Usato dalla UI multi-select.
    Se `deliveroo_names` e' vuoto, lo store resta senza match (UNMATCHED).
    """
    mapping = load_mapping(mapping_path)
    review  = load_review_queue(review_path)

    rev_row  = review[(review["city_code"] == city_code) & (review["glovo_name"] == glovo_name)]
    store_id = rev_row["glovo_store_id"].iloc[0] if len(rev_row) > 0 else ""

    # Rimuovi tutte le righe esistenti per questo store
    _is_glovo = (mapping["city_code"] == city_code) & (mapping["glovo_name"] == glovo_name)
    mapping   = mapping[~_is_glovo]

    clean_names = []
    for nm in deliveroo_names:
        nm = str(nm).strip()
        if nm and nm not in clean_names:
            clean_names.append(nm)

    if clean_names:
        new_rows = pd.DataFrame([{
            "city_code":       city_code,
            "glovo_name":      glovo_name,
            "glovo_store_id":  store_id,
            "deliveroo_name":  nm,
            "confidence":      "1.0",
            "source":          "manual_confirmed",
        } for nm in clean_names])
        mapping = pd.concat([mapping, new_rows], ignore_index=True).drop_duplicates(
            subset=["city_code", "glovo_name", "deliveroo_name"], keep="last"
        )

    save_mapping(mapping, mapping_path)

    updated_review = review[
        ~((review["city_code"] == city_code) & (review["glovo_name"] == glovo_name))
    ]
    save_review_queue(updated_review, review_path)
    print(f"[store_matcher] Match impostati: {city_code} | {glovo_name} -> {clean_names or '(nessuno)'}")


def remove_match(city_code: str, glovo_name: str, deliveroo_name: str,
                 mapping_path: Path = MAPPING_CSV) -> None:
    """Rimuove UN singolo match Deliveroo da uno store Glovo (1:N)."""
    mapping = load_mapping(mapping_path)
    mask = (
        (mapping["city_code"] == city_code)
        & (mapping["glovo_name"] == glovo_name)
        & (mapping["deliveroo_name"].astype(str).str.strip() == str(deliveroo_name).strip())
    )
    save_mapping(mapping[~mask], mapping_path)
    print(f"[store_matcher] Match rimosso: {city_code} | {glovo_name} -/-> {deliveroo_name}")


def reject_match(city_code: str, glovo_name: str,
                 review_path: Path = REVIEW_CSV) -> None:
    """
    Rifiuta un match dalla coda di revisione (store non presente su Deliveroo).
    Lo rimuove dalla coda e lo aggiunge al ground truth con deliveroo_name vuoto
    cosi' non viene mai piu' processato.
    """
    mapping = load_mapping()
    review  = load_review_queue(review_path)

    rev_row = review[
        (review["city_code"] == city_code) & (review["glovo_name"] == glovo_name)
    ]
    store_id = rev_row["glovo_store_id"].iloc[0] if len(rev_row) > 0 else ""

    # Segna come "non presente su Deliveroo" nel ground truth
    new_row = pd.DataFrame([{
        "city_code":       city_code,
        "glovo_name":      glovo_name,
        "glovo_store_id":  store_id,
        "deliveroo_name":  "",
        "confidence":      "1.0",
        "source":          "manual_rejected",
    }])
    updated_mapping = pd.concat([mapping, new_row], ignore_index=True).drop_duplicates(
        subset=["city_code", "glovo_name"], keep="last"
    )
    save_mapping(updated_mapping)

    updated_review = review[
        ~((review["city_code"] == city_code) & (review["glovo_name"] == glovo_name))
    ]
    save_review_queue(updated_review, review_path)
    print(f"[store_matcher] Match rifiutato: {city_code} | {glovo_name} (non su Deliveroo)")


def mark_not_on_deliveroo(city_code: str, glovo_name: str,
                          mapping_path: Path = MAPPING_CSV,
                          review_path:  Path = REVIEW_CSV) -> None:
    """
    Marca uno store come 'Non su Deliveroo' (assente dalla piattaforma,
    ma senza esclusiva commerciale con Glovo).
    Source = 'not_on_deliveroo' — distinto da 'manual_rejected' (Esclusiva Glovo).
    """
    mapping = load_mapping()
    review  = load_review_queue(review_path)

    rev_row  = review[(review["city_code"] == city_code) & (review["glovo_name"] == glovo_name)]
    store_id = rev_row["glovo_store_id"].iloc[0] if len(rev_row) > 0 else ""

    new_row = pd.DataFrame([{
        "city_code":       city_code,
        "glovo_name":      glovo_name,
        "glovo_store_id":  store_id,
        "deliveroo_name":  "",
        "confidence":      "1.0",
        "source":          "not_on_deliveroo",
    }])
    updated_mapping = pd.concat([mapping, new_row], ignore_index=True).drop_duplicates(
        subset=["city_code", "glovo_name"], keep="last"
    )
    save_mapping(updated_mapping)

    updated_review = review[
        ~((review["city_code"] == city_code) & (review["glovo_name"] == glovo_name))
    ]
    save_review_queue(updated_review, review_path)
    print(f"[store_matcher] Non su Deliveroo: {city_code} | {glovo_name}")


def mark_glovo_exclusive(city_code: str, glovo_name: str,
                         mapping_path: Path = MAPPING_CSV,
                         review_path:  Path = REVIEW_CSV) -> None:
    """
    Marca uno store come 'Esclusiva Glovo' (accordo commerciale di esclusiva).
    Source = 'manual_rejected'.
    """
    reject_match(city_code, glovo_name, review_path)
