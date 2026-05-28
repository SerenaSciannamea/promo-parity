"""
sheets_repair.py
Strumento di recovery per riscrivere tab di Google Sheets da CSV locali.

USO DA LINEA DI COMANDO:
  python -m pipeline.sheets_repair --tab store_parity_prime --weeks 2026-W21 2026-W22
  python -m pipeline.sheets_repair --tab all
  python -m pipeline.sheets_repair --verify-only

Questo script è l'UNICO modo corretto per fare recovery manuale su Sheets.
Non usare mai script ad-hoc che scrivono righe senza allineamento colonne.

Garanzie:
  - Legge l'header esistente del tab (preserva id, inserted_at, ecc.)
  - Allinea i CSV locali a quell'header (colonne mancanti → stringa vuota)
  - Scrittura tramite _write_rows_chunked (chunking + verifica post-write)
  - Mostra un diff rows-attese vs rows-effettive per ogni tab
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

# Aggiungi il progetto al path
_proj = Path(__file__).parent.parent
sys.path.insert(0, str(_proj))

from pipeline.sheets_writer import (
    _get_client, _write_rows_chunked,
    TAB_STORE_PARITY, TAB_CITY_PARITY,
    TAB_STORE_PARITY_PRIME, TAB_CITY_PARITY_PRIME,
    TAB_GLOVO_PRODUCTS, TAB_DELIVEROO_PRODUCTS,
    TAB_STORE_MAPPING, TAB_NEEDS_REVIEW,
)

# ---------------------------------------------------------------------------
# Mappa tab → file CSV locali (glob pattern relativo a data/weekly o output)
# ---------------------------------------------------------------------------

WEEKLY_DIR = _proj / "data" / "weekly"
DATA_DIR   = _proj / "data"
OUTPUT_DIR = _proj / "output"

def _find_weekly_csvs(prefix: str) -> list[tuple[Path, str]]:
    """Trova tutti i CSV weekly che matchano il prefix, ordina per settimana."""
    files = sorted(WEEKLY_DIR.glob(f"{prefix}_*.csv"))
    result = []
    for f in files:
        # Estrae la settimana dal nome file: store_parity_2026-W22.csv → 2026-W22
        stem = f.stem.replace(prefix + "_", "")
        result.append((f, stem))
    return result


TAB_SOURCES: dict[str, callable] = {
    TAB_STORE_PARITY:       lambda weeks: [
        (f, w) for f, w in _find_weekly_csvs("store_parity") if not weeks or w in weeks
    ],
    TAB_CITY_PARITY:        lambda weeks: [
        (f, w) for f, w in _find_weekly_csvs("city_parity") if not weeks or w in weeks
    ],
    TAB_STORE_PARITY_PRIME: lambda weeks: [
        (f, w) for f, w in _find_weekly_csvs("store_parity_prime") if not weeks or w in weeks
    ],
    TAB_CITY_PARITY_PRIME:  lambda weeks: [
        (f, w) for f, w in _find_weekly_csvs("city_parity_prime") if not weeks or w in weeks
    ],
    TAB_GLOVO_PRODUCTS:     lambda weeks: [
        (DATA_DIR / f"glovo_auto_{w}.csv", w)
        for w in (weeks or []) if (DATA_DIR / f"glovo_auto_{w}.csv").exists()
    ],
    TAB_DELIVEROO_PRODUCTS: lambda weeks: [
        (OUTPUT_DIR / "deliveroo_promo_products.csv", weeks[-1] if weeks else "")
    ] if (OUTPUT_DIR / "deliveroo_promo_products.csv").exists() else [],
    TAB_STORE_MAPPING:      lambda weeks: [(DATA_DIR / "store_mapping.csv", "")],
    TAB_NEEDS_REVIEW:       lambda weeks: [(DATA_DIR / "needs_review.csv", "")],
}

DELIVEROO_PRODUCTS_COLS = [
    "city_code", "restaurant_name", "week_num", "product_name",
    "product_description", "product_price", "promotion_type",
]


# ---------------------------------------------------------------------------
# Core: ripara un singolo tab
# ---------------------------------------------------------------------------

def repair_tab(
    sh: "gspread.Spreadsheet",
    tab_name: str,
    csv_files: list[tuple[Path, str]],
    dry_run: bool = False,
) -> dict:
    """
    Riscrivi un tab di Sheets da CSV locali.

    Algoritmo:
      1. Legge l'header corrente del tab (preserva l'ordine e colonne extra)
      2. Per ogni CSV, allinea le colonne all'header (mancanti → "")
      3. Concatena tutti i CSV
      4. Riscrive via _write_rows_chunked (con verifica)

    Returns dict con statistiche { tab, files_loaded, rows_written, status }
    """
    import gspread as _gs

    if not csv_files:
        return {"tab": tab_name, "status": "skip", "reason": "nessun CSV trovato"}

    # Carica CSV
    frames = []
    for csv_path, week in csv_files:
        if not csv_path.exists():
            print(f"  [skip] {csv_path.name} non trovato")
            continue
        df = pd.read_csv(csv_path, dtype=str).fillna("")
        # Aggiungi week_num se richiesto e mancante
        if week and "week_num" not in df.columns:
            df["week_num"] = week
        # Filtra colonne per deliveroo_products
        if tab_name == TAB_DELIVEROO_PRODUCTS:
            present = [c for c in DELIVEROO_PRODUCTS_COLS if c in df.columns]
            df = df[present]
        frames.append(df)
        print(f"  [load] {csv_path.name}: {len(df)} righe")

    if not frames:
        return {"tab": tab_name, "status": "skip", "reason": "nessun CSV leggibile"}

    new_data = pd.concat(frames, ignore_index=True)

    # Leggi header corrente del tab Sheets (per preservare colonne extra)
    try:
        ws  = sh.worksheet(tab_name)
        hdr = ws.row_values(1)
    except _gs.WorksheetNotFound:
        # Tab non esiste: crealo con le colonne del nuovo DataFrame
        ws  = sh.add_worksheet(title=tab_name, rows=1, cols=len(new_data.columns))
        hdr = new_data.columns.tolist()

    if not hdr:
        hdr = new_data.columns.tolist()

    # Allinea DataFrame all'header del tab (garantisce nessuno sfasamento)
    aligned = pd.DataFrame(columns=hdr)
    for col in hdr:
        if col in new_data.columns:
            aligned[col] = new_data[col].values if len(new_data) > 0 else []
        else:
            aligned[col] = ""

    rows = aligned.fillna("").astype(str).values.tolist()

    if dry_run:
        print(f"  [dry-run] {tab_name}: scriverei {len(rows)} righe (header: {hdr[:5]}...)")
        return {"tab": tab_name, "status": "dry-run", "rows": len(rows)}

    n = _write_rows_chunked(ws, hdr, rows, verify=True)
    print(f"  [OK] {tab_name}: {n} righe scritte e verificate")
    return {"tab": tab_name, "status": "ok", "rows": n}


# ---------------------------------------------------------------------------
# Verifica senza scrivere
# ---------------------------------------------------------------------------

def verify_sheets(sh: "gspread.Spreadsheet", weeks: list[str]) -> None:
    """Mostra il conteggio righe per ogni tab e le settimane presenti."""
    tabs = list(TAB_SOURCES.keys())
    print(f"\n{'Tab':<30} {'Righe':>8}  Settimane")
    print("-" * 60)
    for tab_name in tabs:
        try:
            ws   = sh.worksheet(tab_name)
            data = ws.get_all_values()
            hdr  = data[0] if data else []
            rows = data[1:] if len(data) > 1 else []
            if "week_num" in hdr:
                wi = hdr.index("week_num")
                week_set = sorted(set(r[wi] for r in rows if len(r) > wi and r[wi]))
            else:
                week_set = ["(n/a)"]
            print(f"  {tab_name:<28} {len(rows):>8}  {week_set}")
        except Exception as e:
            print(f"  {tab_name:<28} {'ERRORE':>8}  {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Ripara tab Google Sheets da CSV locali"
    )
    parser.add_argument(
        "--tab", nargs="+",
        default=["all"],
        help="Tab da riparare (es. store_parity_prime city_parity_prime) oppure 'all'"
    )
    parser.add_argument(
        "--weeks", nargs="+",
        default=[],
        help="Settimane da caricare (es. 2026-W21 2026-W22). Default: tutte disponibili"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Mostra cosa verrebbe scritto senza modificare Sheets"
    )
    parser.add_argument(
        "--verify-only", action="store_true",
        help="Mostra solo il conteggio righe attuali su Sheets, non scrive nulla"
    )
    parser.add_argument(
        "--sheet-id", default="1lAsH0CaoJ3Lfp8uNaJ0-Bu3wTxlO-pn186z_coInnVs",
        help="ID del Google Sheet"
    )
    parser.add_argument(
        "--sa-json",
        default=None,
        help="Path al Service Account JSON (default: cerca in Downloads/Desktop/Documents)"
    )
    args = parser.parse_args()

    # Trova credenziali
    sa_filename = "dogwood-sprite-400413-528afc69c595.json"
    sa_candidates = [
        Path(os.environ.get("USERPROFILE", "~")) / "Downloads" / sa_filename,
        _proj / sa_filename,
        Path(os.environ.get("USERPROFILE", "~")) / "Documents" / sa_filename,
        Path(os.environ.get("USERPROFILE", "~")) / "Desktop" / sa_filename,
    ]
    if args.sa_json:
        sa_path = Path(args.sa_json)
    else:
        sa_path = next((p for p in sa_candidates if p.exists()), None)
    if not sa_path:
        print("ERRORE: credenziali Service Account non trovate.")
        sys.exit(1)
    print(f"Credenziali: {sa_path}")

    client = _get_client(sa_path)
    sh     = client.open_by_key(args.sheet_id)

    if args.verify_only:
        verify_sheets(sh, args.weeks)
        return

    tabs_to_repair = list(TAB_SOURCES.keys()) if "all" in args.tab else args.tab

    print(f"\nTab da riparare: {tabs_to_repair}")
    print(f"Settimane: {args.weeks or 'tutte disponibili'}")
    print(f"Dry-run: {args.dry_run}\n")

    results = []
    for tab_name in tabs_to_repair:
        if tab_name not in TAB_SOURCES:
            print(f"[WARN] Tab '{tab_name}' non riconosciuto, skip.")
            continue
        print(f"\n--- {tab_name} ---")
        csv_files = TAB_SOURCES[tab_name](args.weeks)
        r = repair_tab(sh, tab_name, csv_files, dry_run=args.dry_run)
        results.append(r)

    print("\n=== Riepilogo ===")
    for r in results:
        status = r.get("status", "?")
        rows   = r.get("rows", "")
        reason = r.get("reason", "")
        line   = f"  {r['tab']:<30} {status:<10}"
        if rows:
            line += f" {rows} righe"
        if reason:
            line += f" ({reason})"
        print(line)


if __name__ == "__main__":
    main()
