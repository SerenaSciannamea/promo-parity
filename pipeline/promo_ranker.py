"""
promo_ranker.py
Mappa i tipi di promozione (Glovo e Deliveroo) su un rank numerico.

Gerarchia (rank piu' basso = promo piu' forte):
  1.0  -> 2x1 / TWO_FOR_ONE
  2.0  -> % sconto prodotto / PERCENTAGE_DISCOUNT
  2.5  -> sconto fisso prodotto / FLAT_PRODUCT
  3.0  -> basket % / BASKET_PERCENTAGE
  4.0  -> consegna gratis / FREE_DELIVERY
  5.0  -> consegna scontata / FLAT_DELIVERY
  6.0  -> nessuna promo

Parity (dal punto di vista Glovo):
  SUPERIORITY  -> rank Glovo < rank Deliveroo
  PARITY       -> rank Glovo == rank Deliveroo  (stessa categoria)
  INFERIORITY  -> rank Glovo > rank Deliveroo
"""

from __future__ import annotations
import re

# ---------------------------------------------------------------------------
# Gerarchia Glovo (valori esatti del campo type_of_promo nel BigQuery sheet)
# ---------------------------------------------------------------------------
GLOVO_RANK: dict[str, float] = {
    "TWO_FOR_ONE":          1.0,
    "PERCENTAGE_DISCOUNT":  2.0,
    "FLAT_PRODUCT":         2.5,
    "BASKET_PERCENTAGE":    3.0,
    "FREE_DELIVERY":        4.0,
    "FLAT_DELIVERY":        5.0,
}

# Label human-readable per il rank
RANK_LABEL: dict[float, str] = {
    1.0: "2x1",
    2.0: "% off prodotto",
    2.5: "Flat prodotto",
    3.0: "Basket %",
    4.0: "Consegna gratis",
    5.0: "Consegna scontata",
    6.0: "Nessuna promo",
}

NO_PROMO_RANK = 6.0

# ---------------------------------------------------------------------------
# Pattern Deliveroo (testo libero estratto dallo scraper)
# ---------------------------------------------------------------------------
# Ogni entry: (rank, pattern_regex)   — ordine importa: il piu' forte prima
_DELIVEROO_PATTERNS: list[tuple[float, re.Pattern]] = [
    (1.0, re.compile(r"2\s*al\s*prezzo\s*di\s*1|2x1|due\s+al\s+prezzo", re.I)),
    (2.0, re.compile(r"\d+\s*%\s*(di\s*)?sconto|prodotti\s*selezionati|fino\s*al\s*\d+\s*%", re.I)),
    (2.5, re.compile(r"sconto\s*fisso|prezzo\s*speciale|\d+[,\.]\d+\s*€\s*di\s*sconto", re.I)),
    (3.0, re.compile(r"spendi\s*(almeno|min|da)\s*[\d,\.]+|basket\s*%", re.I)),
    (4.0, re.compile(r"consegna\s*grat(is|uita)|free\s*delivery|spedizione\s*grat", re.I)),
    (5.0, re.compile(r"consegna\s*a\s*[\d,\.]+\s*€|flat\s*delivery|\d+[,\.]\d+\s*€\s*(di\s*)?consegna", re.I)),
]


def rank_glovo(type_of_promo: str | None, has_active_promo: str = "Y") -> float:
    """
    Restituisce il rank per una riga Glovo.

    Parameters
    ----------
    type_of_promo  : valore del campo type_of_promo (es. 'PERCENTAGE_DISCOUNT')
    has_active_promo: 'Y' o 'N'
    """
    if has_active_promo != "Y" or not type_of_promo:
        return NO_PROMO_RANK
    return GLOVO_RANK.get(type_of_promo.strip().upper(), NO_PROMO_RANK)


def rank_deliveroo(promotion_type: str | None) -> float:
    """
    Restituisce il rank per un testo di promozione Deliveroo.

    Parameters
    ----------
    promotion_type : testo libero estratto dallo scraper (puo' contenere '|' tra piu' promo)
    """
    if not promotion_type or str(promotion_type).strip() in ("", "nan"):
        return NO_PROMO_RANK

    # Se ci sono piu' promo separate da '|' prende il rank migliore (piu' basso)
    best = NO_PROMO_RANK
    for segment in str(promotion_type).split("|"):
        seg = segment.strip()
        for rank, pattern in _DELIVEROO_PATTERNS:
            if pattern.search(seg):
                if rank < best:
                    best = rank
                break  # pattern trovato per questo segmento, passa al prossimo
    return best


def parity_label(glovo_rank: float, deliveroo_rank: float) -> str:
    """
    Restituisce 'SUPERIORITY', 'PARITY' o 'INFERIORITY' dal punto di vista Glovo.

    Quando entrambi sono senza promo (rank 6) -> PARITY.
    """
    if glovo_rank < deliveroo_rank:
        return "SUPERIORITY"
    elif glovo_rank > deliveroo_rank:
        return "INFERIORITY"
    else:
        return "PARITY"


def rank_label(rank: float) -> str:
    """Etichetta human-readable del rank."""
    return RANK_LABEL.get(rank, f"Rank {rank}")
