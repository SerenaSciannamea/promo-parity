"""
keep_alive.py
Tiene sveglia (e all'occorrenza risveglia) l'app Streamlit Community Cloud.

Un semplice ping HTTP NON basta: Streamlit va in standby in base alle sessioni
reali dell'app (WebSocket), non alle GET sulla pagina. Qui usiamo un browser
headless che carica davvero l'app — apre la sessione — e, se trova la pagina
"Zzzz / Yes, get this app back up!", clicca per risvegliarla.
"""

from playwright.sync_api import sync_playwright

URL = "https://promo-parity.streamlit.app/"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(5_000)

    # Se l'app dorme, clicca il bottone di wake-up e aspetta il riavvio.
    try:
        btn = page.get_by_text("get this app back up", exact=False)
        if btn.count() > 0:
            btn.first.click()
            print("App addormentata -> cliccato wake-up, attendo il riavvio...")
            page.wait_for_timeout(60_000)
        else:
            print("App gia' sveglia.")
    except Exception as e:
        print("Nessun bottone wake-up trovato:", e)

    # Tieni la sessione aperta un po' per registrare attivita' reale.
    page.wait_for_timeout(20_000)
    try:
        print("OK. Titolo pagina:", page.title())
    except Exception:
        pass
    browser.close()
