"""
backfill_deliveroo_products.py
-------------------------------
Carica su Google Sheets i prodotti Deliveroo delle settimane archiviate,
aggiungendo week_num derivato dal nome della cartella di archivio.

Uso:
    python backfill_deliveroo_products.py --sa <path_service_account.json> --sheet-id <id>

Il nome della cartella di archivio (es. 2026-W20) viene usato come week_num,
che e' piu' affidabile di scraped_at_utc per l'attribuzione settimana.
"""

import argparse
import json
from pathlib import Path

import pandas as pd

ROOT       = Path(__file__).resolve().parent
ARCHIVE    = ROOT / "output" / "archive"
DP_COLS    = ["city_code", "restaurant_name", "week_num",
              "product_name", "product_description", "product_price", "promotion_type"]


def load_archive_products() -> pd.DataFrame:
    """Legge tutti i CSV archiviati e li unisce con week_num dal nome cartella."""
    frames = []
    if not ARCHIVE.exists():
        print("Nessuna cartella archive trovata.")
        return pd.DataFrame()

    for week_dir in sorted(ARCHIVE.iterdir()):
        if not week_dir.is_dir():
            continue
        week_label = week_dir.name   # es. "2026-W20"
        csv_path   = week_dir / "deliveroo_promo_products.csv"
        if not csv_path.exists():
            print(f"  [{week_label}] Nessun file prodotti trovato — skip")
            continue

        df = pd.read_csv(csv_path, dtype=str).fillna("")
        df.columns = [c.strip().lower() for c in df.columns]

        # Aggiungi o sovrascrivi week_num col nome cartella (piu' affidabile di scraped_at_utc)
        df["week_num"] = week_label

        # Filtra colonne utili
        cols_present = [c for c in DP_COLS if c in df.columns]
        df = df[cols_present].drop_duplicates()

        print(f"  [{week_label}] {len(df)} righe prodotti trovate")
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def upload_to_sheets(df: pd.DataFrame, sheet_id: str, sa_path: str) -> None:
    from pipeline.sheets_writer import _get_client, _get_or_create_worksheet, _upsert_sheet, TAB_DELIVEROO_PRODUCTS

    print(f"\nCaricamento {len(df)} righe su Google Sheets tab '{TAB_DELIVEROO_PRODUCTS}'...")
    client = _get_client(sa_path)
    sheet  = client.open_by_key(sheet_id)
    ws     = _get_or_create_worksheet(sheet, TAB_DELIVEROO_PRODUCTS, df.columns.tolist())
    n      = _upsert_sheet(ws, df, key_cols=["city_code", "restaurant_name", "week_num", "product_name"])
    print(f"Fatto. Righe totali nel tab: {n}")


def main():
    parser = argparse.ArgumentParser(description="Backfill prodotti Deliveroo storici su Google Sheets")
    parser.add_argument("--sa",       required=True, help="Path al JSON service account Google")
    parser.add_argument("--sheet-id", required=True, help="ID del Google Sheet di output")
    args = parser.parse_args()

    print("=== Backfill prodotti Deliveroo settimane archiviate ===\n")
    df = load_archive_products()

    if df.empty:
        print("Nessun dato da caricare.")
        return

    print(f"\nTotale righe da caricare: {len(df)}")
    print(f"Settimane trovate: {sorted(df['week_num'].unique())}\n")

    upload_to_sheets(df, args.sheet_id, args.sa)


if __name__ == "__main__":
    main()
