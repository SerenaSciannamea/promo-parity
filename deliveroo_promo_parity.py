from __future__ import annotations

import argparse
import csv
import math
import random
import re
import sys
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

# Fix encoding Windows cp1252 -> UTF-8 per caratteri non ASCII nei nomi ristoranti
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import pandas as pd
from rapidfuzz import fuzz, process
from shapely import wkt

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None
from shapely.geometry import Point
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
DEFAULT_BASE_URL = (
    "https://deliveroo.it/it/restaurants/bari/centro-storico"
    "?geohash={geohash}&collection=restaurants"
)
PROMO_PATTERNS = [
    r"2\s+al\s+prezzo\s+di\s+1",
    r"spendi\s+almeno",
    r"risparmia",
    r"%",
    r"prodotti\s+selezionati",
    r"men[uù]\s+in\s+offerta",
    r"consegna\s+gratis",
    r"spese\s+di\s+consegna\s+a\s+0",
    r"consegna\s+a\s+0",
    r"offerta",
]
PROMO_REGEX = re.compile("|".join(PROMO_PATTERNS), re.IGNORECASE)
METRIC_REGEX = re.compile(
    r"(\d+[\.,]\d+\s*$|\bmin\b|\bkm\b|\bbuono\b|\bmolto\s+buono\b|consegna|€"
    r"|^\d+$"           # numero intero puro (minuti di consegna, valutazione arrotondata)
    r"|\d{1,2}:\d{2}"  # orario tipo "08:30" o "08:30 - 09:00"
    r")",
    re.IGNORECASE,
)
EDITORIAL_REGEX = re.compile(
    r"(sapori\s+decisi|nuovo|novit[aà]|sponsorizzato|sponsored|scelto\s+per\s+te"
    r"|solo\s+su\s+deliveroo|esclusiv[oa]\s+deliveroo|solo\s+deliveroo)",
    re.IGNORECASE,
)
PREORDER_REGEX = re.compile(
    r"^(preordina|pre-order|preorder|ordina\s+per|chiude\s+alle|apre\s+alle|domani|oggi)\b",
    re.IGNORECASE,
)
BLOCKED_TERMS = (
    "verifica di sicurezza",
    "non sei un robot",
    "just a moment",
    "checking your browser",
    "attention required",
    "site connection is secure",
)
EXCLUDED_PROMO_NORMALIZED = {
    "spendi almeno 10 spese di consegna a 0",
    "spese di consegna a 0",
}
# Pattern regex per escludere promo di consegna gratuita
EXCLUDED_PROMO_REGEX = re.compile(
    r"consegna\s+grat(is|uita)|spese\s+di\s+consegna\s+a\s+0|consegna\s+a\s+0"
    r"|free\s+delivery|delivery\s+grat(is|uita)",
    re.IGNORECASE,
)
GENERIC_MATCH_TOKENS = {
    "pizza", "pizzeria", "sushi", "burger", "burgers", "kebab", "poke", "pokè",
    "ristorante", "restaurant", "food", "house", "bar", "cucina", "grill", "chicken",
    "delivery", "mexican", "italiano", "italiana", "plant", "based", "smashburger",
}
RESTAURANT_FIELDS = [
    "city_code",
    "restaurant_name",
    "promotion_type",
    "source_url",
    "scraped_at_utc",
]
PRODUCT_PROMO_FIELDS = [
    "city_code",
    "restaurant_name",
    "promotion_type",
    "product_name",
    "product_description",
    "product_price",
    "source_url",
    "scraped_at_utc",
]
SAMPLE_FIELDS = [
    "city_code",
    "geohash",
    "lat",
    "lon",
    "status",
    "restaurant_count",
    "checked_url",
    "scraped_at_utc",
]


def parse_args() -> argparse.Namespace:
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Scraping promozioni Deliveroo da poligoni città")
    parser.add_argument("--polygons", type=Path, default=base_dir / "Polygons.csv")
    parser.add_argument("--output-dir", type=Path, default=base_dir / "output")
    parser.add_argument("--sample-step-km", type=float, default=2.5)
    parser.add_argument("--geohash-precision", type=int, default=7)
    parser.add_argument("--city-codes", default="", help="Lista city code separati da virgola, es. MIL,ROM")
    parser.add_argument("--max-points-per-city", type=int, default=0, help="0 = tutti i punti")
    parser.add_argument("--max-total-points", type=int, default=0, help="0 = nessun limite totale")
    parser.add_argument("--timeout", type=int, default=18)
    parser.add_argument("--load-more-clicks", type=int, default=2)
    parser.add_argument("--show", action="store_true", help="Mostra il browser durante lo scraping")
    parser.add_argument("--stores-csv", type=Path, default=None, help="CSV esportato dalla tab Stores del GSheet")
    parser.add_argument("--stores-column-index", type=int, default=1, help="Indice colonna 0-based; la colonna B è 1")
    parser.add_argument("--match-threshold", type=float, default=84.0)
    parser.add_argument("--google-sheet", default="", help="URL o ID del Google Sheet da aggiornare")
    parser.add_argument("--google-worksheet-gid", type=int, default=0, help="gid della tab del Google Sheet da aggiornare")
    parser.add_argument("--google-service-account-json", type=Path, default=None, help="JSON del service account Google con accesso in modifica al foglio")
    parser.add_argument(
        "--skip-city-after-same-results",
        type=int,
        default=4,
        help="Passa alla città successiva dopo N geohash consecutivi con contenuto identico e nessuna novità; 0 disattiva",
    )
    return parser.parse_args()


def random_pause(min_seconds: float = 0.1, max_seconds: float = 0.25) -> None:
    delay = random.uniform(min_seconds, max_seconds)
    if delay > 0:
        time.sleep(delay)


def clean_text(text: str) -> str:
    cleaned = (
        text.replace("\xa0", " ")
        .replace("\u202a", "")
        .replace("\u202c", "")
        .replace("\u202d", "")
        .replace("\u202e", "")
        .strip()
    )
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip("| ")


def normalize_name(text: str) -> str:
    text = clean_text(text).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def geohash_encode(lat: float, lon: float, precision: int = 7) -> str:
    lat_interval = [-90.0, 90.0]
    lon_interval = [-180.0, 180.0]
    geohash_chars: list[str] = []
    is_even = True
    bit = 0
    ch = 0

    while len(geohash_chars) < precision:
        if is_even:
            midpoint = (lon_interval[0] + lon_interval[1]) / 2
            if lon >= midpoint:
                ch |= 1 << (4 - bit)
                lon_interval[0] = midpoint
            else:
                lon_interval[1] = midpoint
        else:
            midpoint = (lat_interval[0] + lat_interval[1]) / 2
            if lat >= midpoint:
                ch |= 1 << (4 - bit)
                lat_interval[0] = midpoint
            else:
                lat_interval[1] = midpoint
        is_even = not is_even
        if bit < 4:
            bit += 1
        else:
            geohash_chars.append(BASE32[ch])
            bit = 0
            ch = 0
    return "".join(geohash_chars)


def parse_polygons(path: Path, selected_codes: set[str]) -> list[tuple[str, object]]:
    results: list[tuple[str, object]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if len(row) < 2:
                continue
            city_code = clean_text(row[0]).upper()
            if selected_codes and city_code not in selected_codes:
                continue
            geometry = wkt.loads(row[1])
            results.append((city_code, geometry))
    if not results:
        raise ValueError("Nessun poligono valido trovato per i city code richiesti")
    return results


def iter_points_in_geometry(geometry, step_km: float, precision: int) -> Iterable[tuple[float, float, str]]:
    minx, miny, maxx, maxy = geometry.bounds
    lat_step = step_km / 110.574
    lat = miny
    seen: set[str] = set()
    while lat <= maxy:
        cos_lat = max(0.2, math.cos(math.radians(lat)))
        lon_step = step_km / (111.320 * cos_lat)
        lon = minx
        while lon <= maxx:
            point = Point(lon, lat)
            if geometry.covers(point):
                geohash = geohash_encode(lat, lon, precision)
                if geohash not in seen:
                    seen.add(geohash)
                    yield lat, lon, geohash
            lon += lon_step
        lat += lat_step


def init_driver(show: bool) -> webdriver.Chrome:
    options = Options()
    options.page_load_strategy = "eager"
    if not show:
        options.add_argument("--headless=old")
    options.add_argument("--window-size=1440,2200")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-sync")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option(
        "prefs",
        {
            "profile.default_content_setting_values.notifications": 2,
            "profile.managed_default_content_settings.images": 2,
        },
    )
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.set_page_load_timeout(20)
    return driver


def handle_popups(driver: webdriver.Chrome) -> None:
    xpaths = [
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accetta')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continua senza')]",
    ]
    for xpath in xpaths:
        try:
            WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xpath))).click()
            random_pause(0.2, 0.5)
            return
        except Exception:
            continue


def page_is_blocked(driver: webdriver.Chrome) -> bool:
    has_cards = bool(driver.find_elements(By.CSS_SELECTOR, "a[href*='/menu/']"))
    if has_cards:
        return False
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        lowered = f"{driver.title.lower()} {body_text[:5000]}"
    except Exception:
        lowered = driver.page_source[:5000].lower()
    return any(term in lowered for term in BLOCKED_TERMS)


def wait_for_cards(driver: webdriver.Chrome, timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if page_is_blocked(driver):
            return
        cards = driver.find_elements(By.CSS_SELECTOR, "a[href*='/menu/']")
        if cards:
            return
        time.sleep(0.35)


def wait_for_menu_content(driver: webdriver.Chrome, timeout: int) -> None:
    deadline = time.time() + min(timeout, 7)
    while time.time() < deadline:
        if page_is_blocked(driver):
            return
        items = driver.find_elements(By.CSS_SELECTOR, "button, div[role='button'], li")
        if items:
            return
        time.sleep(0.25)


def scroll_and_click_load_more(driver: webdriver.Chrome, max_clicks: int) -> None:
    T = "translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    load_more_xpaths = [
        # Bottoni generici
        f"//button[contains({T}, 'carica')]",
        f"//button[contains({T}, 'load more')]",
        f"//button[contains({T}, 'mostra')]",
        f"//button[contains({T}, 'vedi altri')]",
        f"//button[contains({T}, 'visualizza tutti')]",
        f"//button[contains({T}, 'vedi tutti')]",
        # Link <a> — Deliveroo usa spesso <a> invece di <button>
        f"//a[contains({T}, 'visualizza tutti')]",
        f"//a[contains({T}, 'vedi tutti')]",
        f"//a[contains({T}, 'ristoranti disponibili')]",
        f"//a[contains({T}, 'mostra tutti')]",
    ]
    no_change_streak = 0
    for _ in range(max(max_clicks, 8)):  # max 8 scroll per geohash
        before = len(driver.find_elements(By.CSS_SELECTOR, "a[href*='/menu/']"))
        driver.execute_script("window.scrollBy(0, 800);")
        random_pause(0.3, 0.5)
        clicked = False
        for xpath in load_more_xpaths:
            try:
                button = WebDriverWait(driver, 1.0).until(EC.element_to_be_clickable((By.XPATH, xpath)))
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
                random_pause(0.1, 0.2)
                button.click()
                clicked = True
                random_pause(0.4, 0.6)
                break
            except Exception:
                continue
        after = len(driver.find_elements(By.CSS_SELECTOR, "a[href*='/menu/']"))
        if after <= before and not clicked:
            no_change_streak += 1
            if no_change_streak >= 2:  # 2 scroll senza novità → fine pagina
                break
        else:
            no_change_streak = 0


def is_metric_line(line: str) -> bool:
    return bool(METRIC_REGEX.search(line))


def is_promo_line(line: str) -> bool:
    return bool(PROMO_REGEX.search(line))


def is_editorial_line(line: str) -> bool:
    return (
        bool(EDITORIAL_REGEX.search(line))
        or bool(PREORDER_REGEX.search(line))
        or (line.startswith("'") and line.endswith("'"))
    )


def restaurant_name_from_url(url: str) -> str:
    cleaned_url = clean_text(url)
    if not cleaned_url:
        return ""
    parsed = urlparse(cleaned_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        return ""
    slug = unquote(path_parts[-1]).replace("-", " ").strip()
    slug = re.sub(r"\s+", " ", slug)
    return slug.title()


def is_placeholder_restaurant_name(text: str) -> bool:
    cleaned = clean_text(text)
    if not cleaned:
        return True
    return bool(PREORDER_REGEX.search(cleaned))


def sanitize_restaurant_name(name: str, url: str = "") -> str:
    cleaned = clean_text(name)
    if is_placeholder_restaurant_name(cleaned):
        fallback = restaurant_name_from_url(url)
        return fallback or cleaned
    return cleaned


def infer_restaurant_name(lines: list[str], href: str = "") -> str:
    clean_lines = [clean_text(line) for line in lines if clean_text(line)]

    pre_metric: list[str] = []
    for line in clean_lines:
        if is_metric_line(line):
            break
        pre_metric.append(line)

    candidates = [line for line in pre_metric if not is_promo_line(line) and not is_editorial_line(line)]
    if not candidates:
        candidates = [
            line
            for line in clean_lines
            if not is_metric_line(line) and not is_promo_line(line) and not is_editorial_line(line)
        ]

    restaurant_name = candidates[0] if candidates else ""
    return sanitize_restaurant_name(restaurant_name, href)


def infer_promotion(lines: list[str]) -> str:
    promos = []
    for line in lines:
        line = clean_text(line)
        if line and is_promo_line(line) and line not in promos:
            promos.append(line)
    return " | ".join(promos)


def is_excluded_promo(promotion_type: str) -> bool:
    normalized = normalize_name(promotion_type)
    if normalized in EXCLUDED_PROMO_NORMALIZED:
        return True
    return bool(EXCLUDED_PROMO_REGEX.search(promotion_type))


def should_collect_menu_products(promotion_type: str) -> bool:
    pt = clean_text(promotion_type)
    return bool(pt) and not is_excluded_promo(pt)


def scroll_menu_to_load_all(driver: webdriver.Chrome) -> None:
    """Scrolla il menu del ristorante fino in fondo per caricare tutti i prodotti."""
    last_height = driver.execute_script("return document.body.scrollHeight")
    no_change_streak = 0
    for _ in range(20):  # max 20 scroll sul menu
        driver.execute_script("window.scrollBy(0, 600);")
        time.sleep(0.3)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            no_change_streak += 1
            if no_change_streak >= 3:
                break
        else:
            no_change_streak = 0
            last_height = new_height
    # Torna in cima per permettere l'estrazione dall'intero DOM
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.2)


def extract_promoted_products_from_menu(driver: webdriver.Chrome, promotion_type: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    target = clean_text(promotion_type).lower()

    for element in driver.find_elements(By.CSS_SELECTOR, "button, div[role='button'], li"):
        lines = [clean_text(line) for line in element.text.splitlines() if clean_text(line)]
        if not lines:
            continue
        joined = " | ".join(lines)
        lowered = joined.lower()
        # Includi solo elementi che contengono almeno un segnale promozionale
        if not PROMO_REGEX.search(joined):
            continue

        product_name = ""
        for line in lines:
            if is_promo_line(line) or is_metric_line(line):
                continue
            product_name = line
            break
        if not product_name and lines:
            product_name = lines[0]

        if not product_name or is_promo_line(product_name):
            continue

        product_price = next((line for line in lines if "€" in line), "")
        product_description = ""
        for line in lines[1:]:
            if line == product_name or line == product_price or is_promo_line(line):
                continue
            if not is_metric_line(line):
                product_description = line
                break

        key = (normalize_name(product_name), normalize_name(product_price), normalize_name(promotion_type))
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "product_name": product_name,
                "product_description": product_description,
                "product_price": product_price,
            }
        )
    return items


def get_card_text(driver: webdriver.Chrome, anchor) -> str:
    """
    Risale al contenitore della card per ottenere il testo completo inclusi i badge
    promo (overlay sull'immagine) che sono sibling dell'anchor, non figli.
    """
    try:
        card_text = driver.execute_script(
            """
            const a = arguments[0];
            // Risali finché il contenitore ha esattamente 1 anchor /menu/
            let node = a.parentElement;
            for (let i = 0; i < 8; i++) {
                if (!node || !node.parentElement) break;
                const links = node.querySelectorAll("a[href*='/menu/']");
                if (links.length === 1) return node.innerText;
                node = node.parentElement;
            }
            return a.innerText;
            """,
            anchor,
        )
        return card_text or ""
    except Exception:
        return anchor.text or ""


def extract_cards(driver: webdriver.Chrome) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for anchor in driver.find_elements(By.CSS_SELECTOR, "a[href*='/menu/']"):
        href = clean_text(anchor.get_attribute("href") or "")
        # Usa il testo dell'intera card (include badge promo sibling all'anchor)
        text = get_card_text(driver, anchor)
        lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]
        restaurant_name = infer_restaurant_name(lines, href)
        if not restaurant_name:
            continue
        promotion_type = infer_promotion(lines)
        key = (normalize_name(restaurant_name), promotion_type.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "restaurant_name": restaurant_name,
                "promotion_type": promotion_type,
                "source_url": href,
            }
        )
    return rows


def ensure_csv(path: Path, fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()


def append_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    ensure_csv(path, fieldnames)
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writerows(rows)


def load_processed_points(*csv_paths: Path) -> set[tuple[str, str]]:
    processed: set[tuple[str, str]] = set()
    for csv_path in csv_paths:
        if not csv_path or not csv_path.exists():
            continue
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                status = clean_text(row.get("status", "")).lower()
                city_code = clean_text(row.get("city_code", "")).upper()
                geohash = clean_text(row.get("geohash", ""))
                if not city_code or not geohash:
                    continue
                if status and (status.startswith("blocked") or status.startswith("error") or status == "started"):
                    continue
                processed.add((city_code, geohash))
    return processed


def load_processed_product_targets(products_csv: Path) -> set[tuple[str, str, str]]:
    processed: set[tuple[str, str, str]] = set()
    if not products_csv.exists():
        return processed
    with products_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            processed.add(
                (
                    clean_text(row.get("city_code", "")).upper(),
                    normalize_name(row.get("restaurant_name", "")),
                    normalize_name(row.get("promotion_type", "")),
                )
            )
    return processed


def merge_promotion_values(*values: str) -> str:
    merged: list[str] = []
    for value in values:
        for piece in [clean_text(x) for x in str(value).split("|") if clean_text(x)]:
            if piece not in merged and not is_excluded_promo(piece):
                merged.append(piece)
    return " | ".join(merged)


def load_saved_restaurants(path: Path) -> OrderedDict[tuple[str, str], dict[str, str]]:
    grouped: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
    if not path.exists():
        return grouped

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            city_code = clean_text(row.get("city_code", "")).upper()
            restaurant_name = sanitize_restaurant_name(row.get("restaurant_name", ""), row.get("source_url", ""))
            promotion_type = clean_text(row.get("promotion_type", ""))
            if not city_code or not restaurant_name or not promotion_type or is_excluded_promo(promotion_type):
                continue
            key = (city_code, normalize_name(restaurant_name))
            if key not in grouped:
                grouped[key] = {
                    "city_code": city_code,
                    "restaurant_name": restaurant_name,
                    "promotion_type": promotion_type,
                    "source_url": clean_text(row.get("source_url", "")),
                    "scraped_at_utc": clean_text(row.get("scraped_at_utc", "")),
                }
            else:
                grouped[key]["promotion_type"] = merge_promotion_values(grouped[key].get("promotion_type", ""), promotion_type)
    return grouped


def write_restaurant_rows(path: Path, grouped: OrderedDict[tuple[str, str], dict[str, str]]) -> None:
    ensure_csv(path, RESTAURANT_FIELDS)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESTAURANT_FIELDS)
        writer.writeheader()
        writer.writerows(grouped.values())


def upsert_restaurant_rows(
    path: Path,
    rows: list[dict[str, str]],
    grouped: OrderedDict[tuple[str, str], dict[str, str]] | None = None,
) -> OrderedDict[tuple[str, str], dict[str, str]]:
    if grouped is None:
        grouped = load_saved_restaurants(path)
    if not rows:
        return grouped

    changed = False
    for row in rows:
        city_code = clean_text(row.get("city_code", "")).upper()
        restaurant_name = sanitize_restaurant_name(row.get("restaurant_name", ""), row.get("source_url", ""))
        promotion_type = clean_text(row.get("promotion_type", ""))
        if not city_code or not restaurant_name or not promotion_type or is_excluded_promo(promotion_type):
            continue

        key = (city_code, normalize_name(restaurant_name))
        if key not in grouped:
            grouped[key] = {
                "city_code": city_code,
                "restaurant_name": restaurant_name,
                "promotion_type": promotion_type,
                "source_url": clean_text(row.get("source_url", "")),
                "scraped_at_utc": clean_text(row.get("scraped_at_utc", "")),
            }
            changed = True
        else:
            merged_promo = merge_promotion_values(grouped[key].get("promotion_type", ""), promotion_type)
            if merged_promo != grouped[key].get("promotion_type", ""):
                grouped[key]["promotion_type"] = merged_promo
                changed = True
            if not clean_text(grouped[key].get("source_url", "")) and clean_text(row.get("source_url", "")):
                grouped[key]["source_url"] = clean_text(row.get("source_url", ""))
                changed = True
            if clean_text(row.get("scraped_at_utc", "")) and clean_text(row.get("scraped_at_utc", "")) != grouped[key].get("scraped_at_utc", ""):
                grouped[key]["scraped_at_utc"] = clean_text(row.get("scraped_at_utc", ""))
                changed = True

    if changed:
        write_restaurant_rows(path, grouped)
    return grouped


def build_restaurant_signature(rows: list[dict[str, str]]) -> tuple[tuple[str, str], ...]:
    signature: set[tuple[str, str]] = set()
    for row in rows:
        restaurant_name = normalize_name(sanitize_restaurant_name(row.get("restaurant_name", ""), row.get("source_url", "")))
        promotion_type = normalize_name(row.get("promotion_type", ""))
        if restaurant_name and promotion_type:
            signature.add((restaurant_name, promotion_type))
    return tuple(sorted(signature))


def count_new_restaurant_info(
    grouped: OrderedDict[tuple[str, str], dict[str, str]],
    rows: list[dict[str, str]],
) -> int:
    new_info_count = 0
    seen: set[tuple[str, str]] = set()
    for row in rows:
        city_code = clean_text(row.get("city_code", "")).upper()
        restaurant_name = sanitize_restaurant_name(row.get("restaurant_name", ""), row.get("source_url", ""))
        promotion_type = clean_text(row.get("promotion_type", ""))
        if not city_code or not restaurant_name or not promotion_type or is_excluded_promo(promotion_type):
            continue

        key = (city_code, normalize_name(restaurant_name))
        if key in seen:
            continue
        seen.add(key)

        if key not in grouped:
            new_info_count += 1
            continue

        merged_promo = merge_promotion_values(grouped[key].get("promotion_type", ""), promotion_type)
        if merged_promo != grouped[key].get("promotion_type", ""):
            new_info_count += 1
    return new_info_count


def aggregate_restaurants(raw_csv: Path, output_csv: Path) -> None:
    grouped: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
    if not raw_csv.exists():
        return

    with raw_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            city_code = clean_text(row.get("city_code", "")).upper()
            restaurant_name = sanitize_restaurant_name(row.get("restaurant_name", ""), row.get("source_url", ""))
            promotion_type = clean_text(row.get("promotion_type", ""))
            if not city_code or not restaurant_name or not promotion_type or is_excluded_promo(promotion_type):
                continue

            key = (city_code, normalize_name(restaurant_name))
            if key not in grouped:
                grouped[key] = {
                    "city_code": city_code,
                    "restaurant_name": restaurant_name,
                    "promotion_type": promotion_type,
                }
            else:
                existing = grouped[key]["promotion_type"]
                parts = [p for p in [existing, promotion_type] if p]
                deduped = []
                for part in parts:
                    for piece in [clean_text(x) for x in part.split("|") if clean_text(x)]:
                        if piece not in deduped:
                            deduped.append(piece)
                grouped[key]["promotion_type"] = " | ".join(deduped)

    ensure_csv(output_csv, ["city_code", "restaurant_name", "promotion_type"])
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["city_code", "restaurant_name", "promotion_type"])
        writer.writeheader()
        writer.writerows(grouped.values())


def cleanup_saved_outputs(raw_csv: Path, deduped_csv: Path, products_csv: Path) -> None:
    if raw_csv.exists():
        rows = []
        with raw_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                row["restaurant_name"] = sanitize_restaurant_name(row.get("restaurant_name", ""), row.get("source_url", ""))
                rows.append(row)
        upsert_restaurant_rows(raw_csv, rows, OrderedDict())
        aggregate_restaurants(raw_csv, deduped_csv)

    if products_csv.exists():
        cleaned_rows = []
        with products_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                row["restaurant_name"] = sanitize_restaurant_name(row.get("restaurant_name", ""), row.get("source_url", ""))
                cleaned_rows.append(row)
        with products_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=PRODUCT_PROMO_FIELDS)
            writer.writeheader()
            writer.writerows(cleaned_rows)


def extract_google_sheet_id(value: str) -> str:
    raw = clean_text(value)
    if not raw:
        return ""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
    return match.group(1) if match else raw


def sync_csv_to_google_sheet(
    csv_path: Path,
    google_sheet: str,
    worksheet_gid: int,
    service_account_json: Path | None,
) -> bool:
    if not csv_path.exists() or not google_sheet or not service_account_json:
        return False
    if not service_account_json.exists():
        raise FileNotFoundError(f"Credenziali Google non trovate: {service_account_json}")
    if gspread is None or Credentials is None:
        raise ImportError("Librerie Google Sheets non disponibili nell'ambiente Python")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_file(str(service_account_json), scopes=scopes)
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(extract_google_sheet_id(google_sheet))

    worksheet = None
    if worksheet_gid:
        try:
            worksheet = spreadsheet.get_worksheet_by_id(worksheet_gid)
        except Exception:
            worksheet = None
    if worksheet is None:
        worksheet = spreadsheet.sheet1

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        values = list(csv.reader(handle))

    worksheet.clear()
    if values:
        worksheet.update(values=values, range_name="A1")
    return True


def significant_name_tokens(text: str) -> set[str]:
    tokens = {token for token in normalize_name(text).split() if token and token not in GENERIC_MATCH_TOKENS and len(token) > 2}
    return tokens


def choose_best_deliveroo_match(glovo_name: str, choices: list[str], threshold: float) -> tuple[str, float] | None:
    cleaned = clean_text(glovo_name)
    if not cleaned or not choices:
        return None

    results = process.extract(cleaned, choices, scorer=fuzz.token_set_ratio, limit=3)
    if not results:
        return None

    best_name, best_score, *_ = results[0]
    source_norm = normalize_name(cleaned)
    best_norm = normalize_name(best_name)
    overlap = significant_name_tokens(cleaned) & significant_name_tokens(best_name)
    strong_overlap = {token for token in overlap if len(token) >= 4}

    confident = False
    if source_norm == best_norm:
        confident = True
    elif (source_norm in best_norm or best_norm in source_norm) and strong_overlap and best_score >= max(threshold, 80):
        confident = True
    elif len(strong_overlap) >= 2 and best_score >= max(threshold, 80):
        confident = True
    elif len(strong_overlap) >= 1 and best_score >= max(threshold, 88):
        confident = True
    elif best_score >= max(threshold, 95):
        confident = True

    if not confident:
        return None

    if len(results) > 1:
        second_name, second_score, *_ = results[1]
        second_overlap = significant_name_tokens(cleaned) & significant_name_tokens(second_name)
        if second_score >= best_score - 1.5 and second_overlap != overlap:
            return None

    return best_name, float(best_score)


def match_stores(stores_csv: Path, deduped_csv: Path, output_csv: Path, stores_column_index: int, threshold: float) -> None:
    if not stores_csv or not stores_csv.exists() or not deduped_csv.exists():
        return

    try:
        stores_df = pd.read_csv(stores_csv)
    except Exception:
        stores_df = pd.read_csv(stores_csv, sep=None, engine="python")

    deduped_df = pd.read_csv(deduped_csv)
    if deduped_df.empty:
        return

    target_index = min(max(stores_column_index, 0), max(len(stores_df.columns) - 1, 0))
    target_col = stores_df.columns[target_index]
    city_col = stores_df.columns[0]

    matches = []
    scores = []
    for _, row in stores_df.iterrows():
        city_code = clean_text(str(row.get(city_col, ""))).upper()
        cleaned = clean_text(str(row.get(target_col, "")))
        if not cleaned:
            matches.append("")
            scores.append("")
            continue

        city_choices = deduped_df.loc[
            deduped_df["city_code"].fillna("").astype(str).str.upper() == city_code,
            "restaurant_name",
        ].dropna().astype(str).tolist()

        best = choose_best_deliveroo_match(cleaned, city_choices, threshold)
        if not best:
            matches.append("")
            scores.append("")
            continue

        matches.append(best[0])
        scores.append(round(best[1], 1))

    stores_df.insert(target_index + 1, "Deliveroo Name", matches)
    stores_df.insert(target_index + 2, "Match Score", scores)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    stores_df.to_csv(output_csv, index=False, encoding="utf-8-sig")


def scrape_point(
    driver: webdriver.Chrome,
    city_code: str,
    lat: float,
    lon: float,
    geohash: str,
    timeout: int,
    load_more_clicks: int,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    checked_url = DEFAULT_BASE_URL.format(geohash=geohash)
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    driver.get(checked_url)
    random_pause(0.15, 0.35)
    handle_popups(driver)
    wait_for_cards(driver, timeout)

    if page_is_blocked(driver):
        return (
            {
                "city_code": city_code,
                "geohash": geohash,
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                "status": "blocked_human_check",
                "restaurant_count": "0",
                "checked_url": checked_url,
                "scraped_at_utc": scraped_at,
            },
            [],
        )

    scroll_and_click_load_more(driver, load_more_clicks)
    cards = extract_cards(driver)
    sample_row = {
        "city_code": city_code,
        "geohash": geohash,
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "status": "covered" if cards else "no_restaurants_found",
        "restaurant_count": str(len(cards)),
        "checked_url": checked_url,
        "scraped_at_utc": scraped_at,
    }

    restaurant_rows = [
        {
            "city_code": city_code,
            "restaurant_name": card["restaurant_name"],
            "promotion_type": card["promotion_type"],
            "source_url": card["source_url"] or checked_url,
            "scraped_at_utc": scraped_at,
        }
        for card in cards
        if clean_text(card.get("promotion_type", "")) and not is_excluded_promo(card.get("promotion_type", ""))
    ]
    return sample_row, restaurant_rows


def collect_promo_products_for_rows(
    driver: webdriver.Chrome,
    candidate_rows: list[dict[str, str]],
    products_csv: Path,
    timeout: int,
    processed_targets: set[tuple[str, str, str]] | None = None,
) -> set[tuple[str, str, str]]:
    ensure_csv(products_csv, PRODUCT_PROMO_FIELDS)
    if processed_targets is None:
        processed_targets = load_processed_product_targets(products_csv)

    targets: OrderedDict[tuple[str, str, str], dict[str, str]] = OrderedDict()
    for row in candidate_rows:
        promotion_type = clean_text(row.get("promotion_type", ""))
        if not should_collect_menu_products(promotion_type):
            continue
        key = (
            clean_text(row.get("city_code", "")).upper(),
            normalize_name(sanitize_restaurant_name(row.get("restaurant_name", ""), row.get("source_url", ""))),
            normalize_name(promotion_type),
        )
        if key in processed_targets or key in targets:
            continue
        targets[key] = row

    if not targets:
        return processed_targets

    for key, row in targets.items():
        source_url = clean_text(row.get("source_url", ""))
        if not source_url:
            continue
        driver.get(source_url)
        random_pause(0.1, 0.25)
        handle_popups(driver)
        wait_for_menu_content(driver, timeout)
        if page_is_blocked(driver):
            print(f"    -> menu blocked for {row.get('restaurant_name', '')}", flush=True)
            continue

        # Scrolla tutto il menu per caricare i prodotti lazy-load
        scroll_menu_to_load_all(driver)

        product_rows = []
        scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for item in extract_promoted_products_from_menu(driver, row.get("promotion_type", "")):
            product_rows.append(
                {
                    "city_code": clean_text(row.get("city_code", "")).upper(),
                    "restaurant_name": sanitize_restaurant_name(row.get("restaurant_name", ""), source_url),
                    "promotion_type": clean_text(row.get("promotion_type", "")),
                    "product_name": item["product_name"],
                    "product_description": item["product_description"],
                    "product_price": item["product_price"],
                    "source_url": source_url,
                    "scraped_at_utc": scraped_at,
                }
            )
        append_rows(products_csv, PRODUCT_PROMO_FIELDS, product_rows)
        processed_targets.add(key)
        if product_rows:
            print(f"    -> prodotti promo trovati={len(product_rows)} per {row.get('restaurant_name', '')}", flush=True)

    return processed_targets


def collect_promo_products(
    driver: webdriver.Chrome,
    raw_csv: Path,
    products_csv: Path,
    timeout: int,
    processed_targets: set[tuple[str, str, str]] | None = None,
) -> set[tuple[str, str, str]]:
    if not raw_csv.exists():
        return processed_targets or set()

    rows: list[dict[str, str]] = []
    with raw_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(row)

    return collect_promo_products_for_rows(driver, rows, products_csv, timeout, processed_targets)


def main() -> int:
    args = parse_args()
    selected_codes = {clean_text(code).upper() for code in args.city_codes.split(",") if clean_text(code)}

    if not args.polygons.exists():
        raise FileNotFoundError(f"File poligoni non trovato: {args.polygons}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_restaurants_csv = args.output_dir / "deliveroo_promo_raw.csv"
    deduped_csv = args.output_dir / "deliveroo_promo_deduped.csv"
    samples_csv = args.output_dir / "deliveroo_sample_status.csv"
    products_csv = args.output_dir / "deliveroo_promo_products.csv"
    matches_csv = args.output_dir / "stores_with_deliveroo_names.csv"

    ensure_csv(raw_restaurants_csv, RESTAURANT_FIELDS)
    ensure_csv(samples_csv, SAMPLE_FIELDS)
    ensure_csv(products_csv, PRODUCT_PROMO_FIELDS)
    cleanup_saved_outputs(raw_restaurants_csv, deduped_csv, products_csv)

    polygons = parse_polygons(args.polygons, selected_codes)
    city_points_map: OrderedDict[str, list[tuple[float, float, str]]] = OrderedDict(
        (
            city_code,
            list(iter_points_in_geometry(geometry, args.sample_step_km, args.geohash_precision)),
        )
        for city_code, geometry in polygons
    )
    processed = load_processed_points(samples_csv, raw_restaurants_csv)
    total_points_planned = sum(len(points) for points in city_points_map.values())
    already_completed = sum(
        1
        for city_code, points in city_points_map.items()
        for _, _, geohash in points
        if (city_code, geohash) in processed
    )
    remaining_points = max(total_points_planned - already_completed, 0)
    print(
        f"Geohash totali={total_points_planned} | già completati={already_completed} | rimanenti={remaining_points}",
        flush=True,
    )

    total_points_done = 0
    interrupted = False
    restaurant_index = load_saved_restaurants(raw_restaurants_csv)
    processed_product_targets = load_processed_product_targets(products_csv)
    driver = init_driver(show=args.show)
    products_driver = init_driver(show=False)
    try:
        for city_code, points in city_points_map.items():
            city_points_done = 0
            same_results_streak = 0
            last_signature: tuple[tuple[str, str], ...] | None = None
            city_already_completed = sum(1 for _, _, geohash in points if (city_code, geohash) in processed)
            city_remaining = max(len(points) - city_already_completed, 0)
            print(f"\n=== City {city_code} ===", flush=True)
            print(
                f"Geohash città: totali={len(points)} | già completati={city_already_completed} | rimanenti={city_remaining}",
                flush=True,
            )
            for lat, lon, geohash in points:
                if (city_code, geohash) in processed:
                    continue
                if args.max_points_per_city and city_points_done >= args.max_points_per_city:
                    break
                if args.max_total_points and total_points_done >= args.max_total_points:
                    break

                current_index = already_completed + total_points_done + 1
                current_remaining_before = max(total_points_planned - current_index + 1, 0)
                print(
                    f"[{current_index}/{total_points_planned}] city={city_code} geohash={geohash} lat={lat:.5f} lon={lon:.5f} | rimanenti prima={current_remaining_before}",
                    flush=True,
                )
                sample_row, restaurant_rows = scrape_point(
                    driver,
                    city_code,
                    lat,
                    lon,
                    geohash,
                    args.timeout,
                    args.load_more_clicks,
                )
                new_info_count = count_new_restaurant_info(restaurant_index, restaurant_rows)
                current_signature = build_restaurant_signature(restaurant_rows)

                append_rows(samples_csv, SAMPLE_FIELDS, [sample_row])
                restaurant_index = upsert_restaurant_rows(raw_restaurants_csv, restaurant_rows, restaurant_index)
                if new_info_count > 0:
                    processed_product_targets = collect_promo_products_for_rows(
                        products_driver,
                        restaurant_rows,
                        products_csv,
                        args.timeout,
                        processed_product_targets,
                    )

                processed.add((city_code, geohash))
                city_points_done += 1
                total_points_done += 1
                overall_completed = already_completed + total_points_done
                overall_remaining = max(total_points_planned - overall_completed, 0)
                city_completed_now = city_already_completed + city_points_done
                city_remaining_now = max(len(points) - city_completed_now, 0)

                if new_info_count == 0 and current_signature:
                    same_results_streak = same_results_streak + 1 if current_signature == last_signature else 1
                else:
                    same_results_streak = 0
                last_signature = current_signature

                print(
                    f"    -> status={sample_row['status']} restaurants={sample_row['restaurant_count']} | novità={new_info_count} | città {city_completed_now}/{len(points)} | rimanenti totali={overall_remaining} | rimanenti città={city_remaining_now}",
                    flush=True,
                )

                if sample_row["status"] == "blocked_human_check":
                    print("Controllo umano rilevato. Mi fermo senza forzare bypass.", flush=True)
                    interrupted = True
                    break

                if args.skip_city_after_same_results and same_results_streak >= args.skip_city_after_same_results:
                    print(
                        f"    -> contenuto identico per {same_results_streak} geohash consecutivi senza novità: passo alla città successiva.",
                        flush=True,
                    )
                    break

                random_pause(0.1, 0.2)

            if interrupted or (args.max_total_points and total_points_done >= args.max_total_points):
                break
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterruzione manuale rilevata. Al prossimo avvio ripartirò dai punti già presenti nel CSV.", flush=True)
    finally:
        driver.quit()
        products_driver.quit()

    aggregate_restaurants(raw_restaurants_csv, deduped_csv)

    if not interrupted:
        catchup_driver = init_driver(show=False)
        try:
            processed_product_targets = collect_promo_products(
                catchup_driver,
                raw_restaurants_csv,
                products_csv,
                args.timeout,
                processed_product_targets,
            )
        finally:
            catchup_driver.quit()

    if args.stores_csv:
        match_stores(args.stores_csv, deduped_csv, matches_csv, args.stores_column_index, args.match_threshold)

    synced_to_google = False
    if args.google_sheet:
        target_csv = matches_csv if args.stores_csv and matches_csv.exists() else deduped_csv
        try:
            synced_to_google = sync_csv_to_google_sheet(
                target_csv,
                args.google_sheet,
                args.google_worksheet_gid,
                args.google_service_account_json,
            )
        except Exception as exc:
            print(f"Sync Google Sheet non eseguita: {exc}", flush=True)

    print("\nScraping completato." if not interrupted else "\nScraping interrotto con resume attivo.", flush=True)
    print(f"Raw restaurants CSV: {raw_restaurants_csv}", flush=True)
    print(f"Deduped restaurants CSV: {deduped_csv}", flush=True)
    print(f"Promo products CSV: {products_csv}", flush=True)
    print(f"Sample status CSV: {samples_csv}", flush=True)
    if args.stores_csv:
        print(f"Store matches CSV: {matches_csv}", flush=True)
    if synced_to_google:
        print(f"Google Sheet aggiornato: {extract_google_sheet_id(args.google_sheet)}", flush=True)
    return 0 if not interrupted else 2


if __name__ == "__main__":
    raise SystemExit(main())
