# Promo parity Deliveroo

Questo tool:

- legge i poligoni dal file `Polygons.csv`
- campiona un punto ogni 2 km dentro ogni area
- converte ogni punto in geohash
- apre Deliveroo usando il geohash
- cattura `nome ristorante` e `tipologia di sconto`
- salva una sola riga per store dentro ogni città
- non salva il geohash nei CSV ristorante finali
- salva i CSV finali nella cartella `output`

## File prodotti

- `output/deliveroo_promo_raw.csv` → dettaglio completo per punto
- `output/deliveroo_promo_deduped.csv` → file finale deduplicato per città
- `output/deliveroo_sample_status.csv` → log di avanzamento e resume
- `output/stores_with_deliveroo_names.csv` → matching nomi Glovo/Deliveroo, se passi un CSV della tab Stores
- `output/deliveroo_promo_products.csv` → prodotti in promozione per gli store con sconti su prodotti selezionati o 2 al prezzo di 1

## Avvio rapido

PowerShell:

```powershell
& ".\.venv\Scripts\python.exe" ".\deliveroo_promo_parity.py" --polygons ".\Polygons.csv" --sample-step-km 2 --show
```

Per limitare il test iniziale a Roma e Milano:

```powershell
& ".\.venv\Scripts\python.exe" ".\deliveroo_promo_parity.py" --city-codes "ROM,MIL" --sample-step-km 2 --max-points-per-city 5 --show
```

Per fare anche il matching con i nomi della tab Stores, esporta il GSheet in CSV e poi lancia:

```powershell
& ".\.venv\Scripts\python.exe" ".\deliveroo_promo_parity.py" --stores-csv ".\Stores.csv" --stores-column-index 1 --show
```

## Nota su Google Sheet

Il link del GSheet richiede autenticazione Google. Il tool può aggiornare direttamente il foglio se gli passi un JSON di service account con accesso in modifica; altrimenti continua a preparare il CSV locale come backup.

## Comportamento in caso di blocco

Se compare un controllo umano o interrompi manualmente il run, lo script si ferma in modo pulito e al riavvio riparte leggendo i geohash già presenti nel CSV di avanzamento.
