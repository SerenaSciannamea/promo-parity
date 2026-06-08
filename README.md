# Promo Parity — Glovo vs Deliveroo

Dashboard settimanale per monitorare la parità promozionale tra Glovo e Deliveroo in Italia.

🔗 **Dashboard**: [promo-parity.streamlit.app](https://promo-parity.streamlit.app)

---

## Architettura

```
deliveroo_promo_parity.py   ← Scraper Deliveroo (Selenium + geohash)
pipeline/
  glovo_reader.py           ← Legge e normalizza il CSV Glovo (da BigQuery)
  promo_ranker.py           ← Gerarchia promozioni + logica parity label
  parity_calculator.py      ← Calcola store-level e city-level parity
  store_matcher.py          ← Matching fuzzy Glovo ↔ Deliveroo
  data_quality.py           ← Quality checks automatici (copertura, freshness, ecc.)
  sheets_writer.py          ← Export su Google Sheets (chunking + verifica)
  sheets_reader.py          ← Lettura dati da Sheets per dashboard cloud
  sheets_repair.py          ← Recovery manuale tab Sheets da CSV locali
  run_weekly.py             ← Orchestratore pipeline settimanale
run_scrape.ps1              ← Avvia scraper Deliveroo (auto-restart, watchdog)
run_friday.ps1              ← Avvia pipeline parity + export Sheets
run_merge_w23.ps1           ← Run one-time "merge" (accumula senza sovrascrivere)
app.py                      ← Dashboard Streamlit
```

---

## Flusso settimanale

1. **Scraping Deliveroo** (`run_scrape.ps1`)
   - Scansiona 12 città italiane con punti geohash (step 4.5 km)
   - Raccoglie nome store + tipo promozione + prodotti in promo
   - Auto-restart su crash, watchdog che rileva hang (30 min senza progressi)
   - Al termine avvia automaticamente la pipeline parity

2. **Pipeline parity** (`run_friday.ps1` → `run_weekly.py`)
   - Scarica il CSV Glovo aggiornato dal Google Sheet BigQuery sorgente
   - Scarica la tab **Mapping** AM dal foglio sorgente
   - Aggrega i prodotti Glovo a livello store
   - Fuzzy matching Glovo ↔ Deliveroo
   - Calcola la parity (SUPERIORITY / PARITY / INFERIORITY) per store e città
   - Esegue quality checks automatici
   - Salva su DB SQLite locale + CSV settimanali + Google Sheets output

### Schedule tipico

| Giorno | Orario | Azione |
|--------|--------|--------|
| Martedì | sera | `run_scrape.ps1` (primo run settimanale) |
| Venerdì | 19:30 | `run_scrape.ps1` (run finale con dati peak-time) |

---

## Gerarchia promozioni

| Rank | Tipo | Glovo | Deliveroo |
|------|------|-------|-----------|
| 1.0 | 2×1 | `TWO_FOR_ONE` | "2 al prezzo di 1" |
| 2.0 | % su prodotto | `PERCENTAGE_DISCOUNT` | "X% sconto" |
| 2.5 | Sconto fisso | `FLAT_PRODUCT` | "sconto fisso" |
| 3.0 | % su basket | `BASKET_PERCENTAGE` | "Spendi almeno X€…" |
| 4.0 | Consegna gratis | `FREE_DELIVERY` | "consegna gratuita" |
| 5.0 | Consegna scontata | `FLAT_DELIVERY` | "consegna a X€" |
| 6.0 | Nessuna promo | — | — |

**Promo dominante dello store**: è la promo presente sul **maggior numero di prodotti** in promo (ogni prodotto contato per la sua promo più forte secondo la gerarchia sopra; es. un prodotto `TWO_FOR_ONE, PERCENTAGE_DISCOUNT` conta come `TWO_FOR_ONE`). A parità di conteggio vince la più forte → uno store con molti prodotti in %off e pochi in 2×1 resta classificato come `%off`. I prodotti con più promo concorrenti mantengono la loro **% off reale** nel dettaglio prodotto (la 2×1, che non ha sconto %, non abbassa la media).

**Logica parity label** (rank uguale → tiebreaker):
- **PERCENTAGE_DISCOUNT** (rank 2.0): confronto % MAX; se uguale (±2pp) → tiebreaker su numero prodotti in promo
- **BASKET_PERCENTAGE** (rank 3.0): Opzione C — se `|basket_diff| ≤ €10` usa solo la %; se `> €10` con segnali contrastanti → PARITY

---

## Dashboard — Tab

### 🗺️ City Parity Overview
Visione sintetica per città e settimana, pesata per fatturato.
- Heatmap parity score (SUPERIORITY% − INFERIORITY%) per città × settimana
- Tabella dettaglio città con breakdown SUPERIORITY / PARITY / INFERIORITY
- Grafici distribuzione e match coverage

### 🏪 Store Detail
Tabella di tutti gli store con parity, tipo promo, % sconto, items in promo, revenue.
- Per `BASKET_PERCENTAGE`: mostra `"15% min €10"` nella colonna % OFF e `"Full menu"` negli items
- Drill-down per singolo store: prodotti Glovo + prodotti Deliveroo affiancati

### 📈 Trend
Andamento storico della distribuzione parity settimana per settimana.

### 🔗 Store Matching
Gestione del matching Glovo ↔ Deliveroo:
- **KPI**: Da matchare / Matchati / Esclusiva Glovo / Non su Deliveroo
- **Sezione 1**: Store UNMATCHED — seleziona uno store e scegli tra:
  - ✅ **Match** (inserisci nome Deliveroo)
  - 🚫 **NON su Deliveroo** (store assente dalla piattaforma, no esclusiva)
  - ⭐ **Esclusiva Glovo** (accordo commerciale di esclusiva)
- **Sezione 2**: Modifica store già classificati

> ⚠️ Differenza importante: **Esclusiva Glovo** = accordo commerciale esclusivo con Glovo. **Non su Deliveroo** = store non presente su Deliveroo ma potenzialmente su altre piattaforme.

### ★ Prime
Stessa visione di City Parity + Store Detail con logica **Prime-first**:
per ogni store usa la promo Prime se disponibile, altrimenti fallback Non-Prime.

### 🎯 Azioni
Lista prioritaria degli store in INFERIORITY ordinati per revenue decrescente.

---

## Filtri sidebar

| Filtro | Descrizione |
|--------|-------------|
| **Settimana** | Filtra per settimana ISO (es. 2026-W23) |
| **Città** | Filtra per city code (BAR, BOL, CAT, FIR, MIL, NAP, PAD, PMO, QTC, ROM, TOR, VER) |
| **👤 Responsabile AM** | Filtra tutti i dati per l'AM email — aggiorna City Parity, Store Detail, Trend e Azioni |

Il mapping AM viene letto direttamente dalla tab **Mapping** del foglio BigQuery Glovo sorgente.

---

## Avvio locale

```powershell
# Avvia solo lo scraper Deliveroo
& ".\run_scrape.ps1"

# Avvia la pipeline parity (scarica Glovo + calcola + export Sheets)
& ".\run_friday.ps1"

# Avvia la pipeline per una settimana specifica forzando il rieseguo
& ".\run_friday.ps1" -Week "2026-W23" -Force

# Run "merge" one-time (accumula senza sovrascrivere dati esistenti)
& ".\run_merge_w23.ps1"

# Riparazione manuale di un tab su Sheets
.\.venv\Scripts\python.exe -m pipeline.sheets_repair --tab glovo_products --weeks 2026-W23

# Dashboard locale
.\.venv\Scripts\python.exe -m streamlit run app.py
```

---

## Struttura dati

### Google Sheets output (`1lAsH0CaoJ3Lfp8uNaJ0-Bu3wTxlO-pn186z_coInnVs`)

| Tab | Contenuto | Settimane su Sheets |
|-----|-----------|---------------------|
| `store_parity` | Parity per store × settimana | Ultime 6 |
| `city_parity` | Aggregato per città × settimana | Ultime 6 |
| `store_parity_prime` | Parity vista Prime | Ultime 6 |
| `city_parity_prime` | Aggregato Prime per città | Ultime 6 |
| `glovo_products` | Prodotti Glovo promo-attivi | Ultima 1 (drill-down) |
| `glovo_products_prime` | Prodotti Glovo vista Prime | Ultima 1 |
| `deliveroo_products` | Prodotti Deliveroo in promo | Corrente |
| `store_mapping` | Ground truth matching Glovo ↔ Deliveroo | Storico completo |
| `needs_review` | Candidati fuzzy in attesa | Corrente |
| `manual_matches` | Match manuali inseriti dall'UI cloud | Append-only |
| `priority_actions` | Store INFERIORITY per revenue | Ultime 6 |
| `pipeline_health` | Quality check report | Ultime 6 |

> I dati storici completi sono nel DB SQLite locale (`data/promo_parity.db`) e nei CSV settimanali (`data/weekly/`).

### DB SQLite locale (`data/promo_parity.db`)

Tabelle: `store_parity`, `city_parity`, `store_parity_prime`, `city_parity_prime`, `glovo_products`, `glovo_products_prime`

### CSV locali

| File | Descrizione |
|------|-------------|
| `data/am_mapping.csv` | Mapping store → AM email |
| `data/weekly/store_parity_YYYY-Www.csv` | Archivio CSV settimanali |
| `data/promo_parity.db` | Database SQLite storico completo |
| `output/deliveroo_promo_raw.csv` | Output scraping Deliveroo (raw) |
| `output/deliveroo_promo_deduped.csv` | Output deduplicato per store |
| `output/deliveroo_promo_products.csv` | Prodotti Deliveroo in promo |

---

## Credenziali

| File | Contenuto | Git |
|------|-----------|-----|
| `.streamlit/secrets.toml` | App password + GCP service account | ❌ gitignored |
| `dogwood-sprite-400413-528afc69c595.json` | Service account GCP | ❌ gitignored |
| `secrets.ps1` | App password Gmail per notifiche | ❌ gitignored |

---

## Quality Checks automatici

Ad ogni pipeline run vengono eseguiti check automatici visibili nella tab **🚦 Salute Pipeline**:

| Check | Soglia alert |
|-------|-------------|
| Copertura città | WARNING se manca almeno 1 città su 12 |
| Variazione store count | WARNING se una città perde >20% store vs settimana precedente |
| Shift distribuzione parity | WARNING se INFERIORITY aumenta >15pp vs settimana precedente |
| Freshness CSV Deliveroo | WARNING se il file ha più di 8 giorni |
| Integrità `glovo_products` su Sheets | ERROR se scrittura fallita, WARNING se righe mancanti |
