"""
data_quality.py
Controlli automatici sulla qualità dei dati prodotti dalla pipeline.

Tutti i check restituiscono un QualityReport strutturato che viene:
  - stampato nel log della pipeline
  - incluso nella notifica email
  - scritto nel tab "pipeline_health" su Sheets
  - mostrato nella dashboard (sezione 🚦 Salute Pipeline)

Check implementati:
  1. Copertura città  — tutte le 12 città Glovo hanno dati Deliveroo?
  2. Conteggio store  — variazione % rispetto alla settimana precedente per città
  3. Distribuzione parity — shift anomali in SUPERIORITY/INFERIORITY
  4. Store ad alto rischio — INFERIORITY con revenue elevato (priority actions)
  5. Settimana dati   — i CSV sono della settimana corrente?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

EXPECTED_CITIES = ["BAR", "BOL", "CAT", "FIR", "MIL", "NAP", "PAD", "PMO", "QTC", "ROM", "TOR", "VER"]

# Soglie anomalia
STORE_COUNT_DROP_PCT   = 20.0   # alert se una città perde >20% store
STORE_COUNT_GAIN_PCT   = 50.0   # alert se una città guadagna >50% store (possibile duplicato)
PARITY_SHIFT_THRESHOLD = 15.0   # alert se INFERIORITY sale di >15pp vs settimana precedente
TOP_N_ACTIONS          = 30     # store prioritari da includere nelle azioni


# ---------------------------------------------------------------------------
# Strutture dati
# ---------------------------------------------------------------------------

@dataclass
class QualityIssue:
    level: str          # "ERROR" | "WARNING" | "INFO"
    check: str          # nome del check
    city_code: str      # "" = issue globale
    message: str

    def __str__(self) -> str:
        prefix = {"ERROR": "🔴", "WARNING": "🟡", "INFO": "🔵"}.get(self.level, "")
        city = f" [{self.city_code}]" if self.city_code else ""
        return f"{prefix} {self.level}{city}: {self.message}"


@dataclass
class QualityReport:
    week_num:     str
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    issues:       list[QualityIssue] = field(default_factory=list)
    metrics:      dict = field(default_factory=dict)
    priority_actions: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Shortcut
    @property
    def has_errors(self) -> bool:
        return any(i.level == "ERROR" for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.level == "WARNING" for i in self.issues)

    def add(self, level: str, check: str, message: str, city: str = ""):
        self.issues.append(QualityIssue(level=level, check=check, city_code=city, message=message))

    def summary_text(self) -> str:
        errors   = [i for i in self.issues if i.level == "ERROR"]
        warnings = [i for i in self.issues if i.level == "WARNING"]
        infos    = [i for i in self.issues if i.level == "INFO"]
        lines = [
            f"Quality Report — {self.week_num} ({self.generated_at})",
            f"  🔴 Errori: {len(errors)}  |  🟡 Warning: {len(warnings)}  |  🔵 Info: {len(infos)}",
        ]
        for issue in self.issues:
            lines.append(f"  {issue}")
        if not self.issues:
            lines.append("  ✅ Nessun problema rilevato.")
        return "\n".join(lines)

    def to_dataframe(self) -> pd.DataFrame:
        """Versione tabulare per export su Sheets (tab pipeline_health)."""
        rows = []
        for issue in self.issues:
            rows.append({
                "week_num":     self.week_num,
                "generated_at": self.generated_at,
                "level":        issue.level,
                "check":        issue.check,
                "city_code":    issue.city_code,
                "message":      issue.message,
            })
        if not rows:
            rows.append({
                "week_num":     self.week_num,
                "generated_at": self.generated_at,
                "level":        "INFO",
                "check":        "all",
                "city_code":    "",
                "message":      "Nessun problema rilevato.",
            })
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Check 1 — Copertura città Deliveroo
# ---------------------------------------------------------------------------

def check_city_coverage(
    deliveroo_df: pd.DataFrame,
    report: QualityReport,
) -> None:
    if deliveroo_df is None or deliveroo_df.empty or "city_code" not in deliveroo_df.columns:
        report.add("ERROR", "city_coverage", "Nessun dato Deliveroo disponibile.")
        return

    found = set(deliveroo_df["city_code"].str.strip().str.upper().unique())
    missing = [c for c in EXPECTED_CITIES if c not in found]

    if missing:
        report.add("WARNING", "city_coverage",
                   f"Città senza dati Deliveroo: {', '.join(missing)}. "
                   f"Risultati parziali per queste città.")
    else:
        report.add("INFO", "city_coverage", f"Tutte le {len(EXPECTED_CITIES)} città coperte.")

    report.metrics["deliveroo_cities_found"] = sorted(found & set(EXPECTED_CITIES))
    report.metrics["deliveroo_cities_missing"] = missing


# ---------------------------------------------------------------------------
# Check 2 — Variazione conteggio store vs settimana precedente
# ---------------------------------------------------------------------------

def check_store_count_delta(
    store_parity: pd.DataFrame,
    weekly_dir: Path,
    report: QualityReport,
) -> None:
    current_week = report.week_num

    # Trova la settimana precedente nei CSV locali
    prev_files = sorted(weekly_dir.glob("store_parity_2026-W*.csv"))
    prev_files = [f for f in prev_files if f.stem.replace("store_parity_", "") != current_week]
    if not prev_files:
        report.add("INFO", "store_count_delta", "Nessuna settimana precedente disponibile per confronto.")
        return

    prev_file = prev_files[-1]
    prev_week = prev_file.stem.replace("store_parity_", "")
    try:
        prev_df = pd.read_csv(prev_file, dtype=str)
    except Exception as e:
        report.add("WARNING", "store_count_delta", f"Impossibile leggere {prev_file.name}: {e}")
        return

    cur_counts  = store_parity.groupby("city_code").size().to_dict()
    prev_counts = prev_df.groupby("city_code").size().to_dict() if "city_code" in prev_df.columns else {}

    anomalies = []
    for city in EXPECTED_CITIES:
        cur  = cur_counts.get(city, 0)
        prev = prev_counts.get(city, 0)
        if prev == 0:
            continue
        delta_pct = (cur - prev) / prev * 100
        if delta_pct < -STORE_COUNT_DROP_PCT:
            anomalies.append((city, cur, prev, delta_pct))
            report.add("WARNING", "store_count_delta",
                       f"Store scesi da {prev} a {cur} ({delta_pct:+.0f}%) vs {prev_week}",
                       city=city)
        elif delta_pct > STORE_COUNT_GAIN_PCT:
            report.add("INFO", "store_count_delta",
                       f"Store aumentati da {prev} a {cur} ({delta_pct:+.0f}%) vs {prev_week}",
                       city=city)

    if not anomalies:
        report.add("INFO", "store_count_delta",
                   f"Conteggio store stabile vs {prev_week} (±<{STORE_COUNT_DROP_PCT:.0f}% per tutte le città).")
    report.metrics["store_count_comparison_week"] = prev_week


# ---------------------------------------------------------------------------
# Check 3 — Distribuzione parity (shift anomali)
# ---------------------------------------------------------------------------

def check_parity_distribution(
    store_parity: pd.DataFrame,
    weekly_dir: Path,
    report: QualityReport,
) -> None:
    if "parity" not in store_parity.columns or "city_code" not in store_parity.columns:
        return

    matched = store_parity[~store_parity["parity"].isin(["UNMATCHED", "EXCLUSIVE_GLOVO"])]
    if matched.empty:
        report.add("WARNING", "parity_distribution", "Nessuno store matchato — impossibile calcolare distribuzione.")
        return

    n_inf = (matched["parity"] == "INFERIORITY").sum()
    n_tot = len(matched)
    pct_inf = n_inf / n_tot * 100 if n_tot > 0 else 0

    report.metrics["pct_inferiority"] = round(pct_inf, 1)
    report.metrics["pct_superiority"] = round((matched["parity"] == "SUPERIORITY").sum() / n_tot * 100, 1)
    report.metrics["pct_parity"]      = round((matched["parity"] == "PARITY").sum()      / n_tot * 100, 1)

    # Confronto con settimana precedente
    prev_files = sorted(weekly_dir.glob("store_parity_2026-W*.csv"))
    prev_files = [f for f in prev_files if f.stem.replace("store_parity_", "") != report.week_num]
    if prev_files:
        try:
            prev_df = pd.read_csv(prev_files[-1], dtype=str)
            prev_matched = prev_df[~prev_df["parity"].isin(["UNMATCHED", "EXCLUSIVE_GLOVO"])] if "parity" in prev_df.columns else pd.DataFrame()
            if not prev_matched.empty:
                prev_inf_pct = (prev_matched["parity"] == "INFERIORITY").sum() / len(prev_matched) * 100
                shift = pct_inf - prev_inf_pct
                if shift > PARITY_SHIFT_THRESHOLD:
                    report.add("WARNING", "parity_distribution",
                               f"INFERIORITY aumentata di {shift:+.1f}pp vs settimana precedente "
                               f"({prev_inf_pct:.1f}% → {pct_inf:.1f}%). Verifica dati Deliveroo.")
                else:
                    report.add("INFO", "parity_distribution",
                               f"Distribuzione stabile: SUP {report.metrics['pct_superiority']}% | "
                               f"PAR {report.metrics['pct_parity']}% | INF {pct_inf:.1f}%")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Check 4 — Freshness CSV Deliveroo
# ---------------------------------------------------------------------------

def check_data_freshness(
    deliveroo_csv_path: Path,
    current_week: str,
    report: QualityReport,
) -> None:
    if not deliveroo_csv_path.exists():
        report.add("ERROR", "data_freshness", f"File Deliveroo non trovato: {deliveroo_csv_path.name}")
        return

    age_days = (datetime.now() - datetime.fromtimestamp(deliveroo_csv_path.stat().st_mtime)).days
    modified = datetime.fromtimestamp(deliveroo_csv_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    if age_days > 8:
        report.add("WARNING", "data_freshness",
                   f"CSV Deliveroo aggiornato {age_days} giorni fa ({modified}) — potrebbe non essere della settimana corrente.")
    else:
        report.add("INFO", "data_freshness", f"CSV Deliveroo aggiornato il {modified} ({age_days}gg fa).")

    report.metrics["deliveroo_csv_age_days"] = age_days


# ---------------------------------------------------------------------------
# Priority Actions — store INFERIORITY ordinati per revenue
# ---------------------------------------------------------------------------

def compute_priority_actions(
    store_parity: pd.DataFrame,
    week_num: str,
    top_n: int = TOP_N_ACTIONS,
) -> pd.DataFrame:
    """
    Genera la lista delle azioni prioritarie:
    store in INFERIORITY ordinati per revenue decrescente.
    """
    if store_parity.empty or "parity" not in store_parity.columns:
        return pd.DataFrame()

    inf = store_parity[store_parity["parity"] == "INFERIORITY"].copy()
    if inf.empty:
        return pd.DataFrame()

    # Cast revenue a numerico
    if "revenue" in inf.columns:
        inf["revenue"] = pd.to_numeric(inf["revenue"], errors="coerce").fillna(0)
    else:
        inf["revenue"] = 0

    inf = inf.sort_values("revenue", ascending=False)

    cols = ["city_code", "glovo_name", "deliveroo_name", "parity",
            "glovo_rank_label", "deliveroo_rank_label", "revenue",
            "glovo_pct_off", "deliveroo_pct_off", "promo_coverage_pct"]
    cols_present = [c for c in cols if c in inf.columns]
    result = inf[cols_present].head(top_n).copy()
    result["week_num"]  = week_num
    result["action"]    = "Allinea promo Glovo a Deliveroo"
    result["priority"]  = range(1, len(result) + 1)

    return result


# ---------------------------------------------------------------------------
# Entry point principale
# ---------------------------------------------------------------------------

def run_quality_checks(
    store_parity:      pd.DataFrame,
    deliveroo_df:      pd.DataFrame,
    week_num:          str,
    weekly_dir:        Path,
    deliveroo_csv_path: Optional[Path] = None,
) -> QualityReport:
    """
    Esegue tutti i check e ritorna un QualityReport completo.
    Chiamato da run_weekly.py dopo il calcolo della parity.
    """
    report = QualityReport(week_num=week_num)

    check_city_coverage(deliveroo_df, report)
    check_store_count_delta(store_parity, weekly_dir, report)
    check_parity_distribution(store_parity, weekly_dir, report)
    if deliveroo_csv_path:
        check_data_freshness(deliveroo_csv_path, week_num, report)

    report.priority_actions = compute_priority_actions(store_parity, week_num)

    print("\n" + report.summary_text())
    return report
