"""
fix_glovo_products_db.py
Aggiorna glovo_products nel DB SQLite con i nuovi CSV W21/W22
(schema aggiornato con min_basket_size_np/p e BASKET_PERCENTAGE fix).
"""
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.glovo_reader import load_glovo_csv

DB      = Path("data/promo_parity.db")
DATA    = Path("data")
WEEKS   = ["2026-W21", "2026-W22"]

GP_COLS = ["city_code", "store_name", "week_num", "product_name",
           "type_of_promo", "has_active_promo", "avg_percentage_off",
           "avg_unit_price", "total_product_sold",
           "min_basket_size_np", "min_basket_size_p"]

conn = sqlite3.connect(DB)

# Migrazione: aggiungi colonne se mancano
for col, typedef in [("min_basket_size_np", "REAL"), ("min_basket_size_p", "REAL")]:
    try:
        conn.execute(f"ALTER TABLE glovo_products ADD COLUMN {col} {typedef}")
        conn.commit()
        print(f"[DB] Aggiunta colonna: {col}")
    except sqlite3.OperationalError:
        pass  # colonna gia' esistente

for week in WEEKS:
    csv_path = DATA / f"glovo_auto_{week}.csv"
    if not csv_path.exists():
        print(f"[SKIP] {csv_path.name} non trovato")
        continue

    raw = load_glovo_csv(str(csv_path))
    raw["week_num"] = week
    present = [c for c in GP_COLS if c in raw.columns]
    df = raw[present].copy()

    # Rimuovi dati vecchi per questa settimana
    conn.execute("DELETE FROM glovo_products WHERE week_num=?", (week,))
    conn.commit()

    # Inserisci nuovi dati
    cols_str     = ", ".join(present)
    placeholders = ", ".join(["?"] * len(present))
    sql          = f"INSERT OR REPLACE INTO glovo_products ({cols_str}) VALUES ({placeholders})"
    records = [
        tuple(str(row[c]) if row[c] is not None else None for c in present)
        for _, row in df.iterrows()
    ]
    conn.executemany(sql, records)
    conn.commit()
    print(f"[DB] {week}: {len(records)} righe inserite in glovo_products")

conn.close()
print("[DB] Completato.")
