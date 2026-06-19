import os
import json
import gspread
import requests
import cloudscraper
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

today = datetime.now()

def split_datetime(value):
    if pd.isna(value):
        return None, ""
    value = str(value).strip()

    if value.lower().startswith("today"):
        time_part = value.replace("Today", "").strip()
        return today.date(), time_part

    if value.lower().startswith("tomorrow"):
        time_part = value.replace("Tomorrow", "").strip()
        return (today.date() + timedelta(days=1), time_part)

    if value.lower().startswith("yesterday"):
        time_part = value.replace("Yesterday", "").strip()
        return (today.date() - timedelta(days=1), time_part)

    try:
        parts = value.split()
        if len(parts) == 2:
            date_part = parts[0]
            time_part = parts[1]
            if date_part.endswith("."):
                day, month = date_part.rstrip(".").split(".")
                return (datetime(today.year, int(month), int(day)).date(), time_part)
    except: pass

    try: return (datetime.strptime(value, "%d.%m.%Y").date(), "")
    except: pass

    try:
        if value.endswith("."):
            day, month = value.rstrip(".").split(".")
            return (datetime(today.year, int(month), int(day)).date(), "")
    except: pass

    return value, ""

scrape_report = []

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache"
}

# ==========================================================
# 1. POBIERANIE Z BETEXPLORER
# ==========================================================
all_data = []

try: urls_be = pd.read_excel("ligi.xlsx")["URL"].dropna().tolist()
except: urls_be = []

for i, url in enumerate(urls_be, start=1):
    url_clean = str(url).strip()
    print(f"[{i}/{len(urls_be)}] Pobieram BetExplorer: {url_clean}")
    
    # Tarcza ochronna na złe linki
    if "/fixtures/" not in url_clean and "/results/" not in url_clean:
        scrape_report.append(["BetExplorer", url_clean, "BŁĄD: Link musi kończyć się na /fixtures/ lub /results/"])
        continue
        
    try:
        response = requests.get(url_clean, headers=headers, timeout=30)
        if response.status_code != 200:
            scrape_report.append(["BetExplorer", url_clean, f"BŁĄD: Kod {response.status_code} (Strona nie istnieje)"])
            continue
            
        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        # Bezpieczne pobieranie nazwy ligi
        try:
            if "/football/" in url_clean: league = url_clean.split("/football/")[1].replace("/fixtures/", "").replace("/results/", "").strip("/")
            else: league = url_clean.split(".com/")[1].replace("/fixtures/", "").replace("/results/", "").strip("/")
        except: league = "Unknown_League"

        rows = soup.find_all("tr")
        mecz_count = 0

        # FIXTURES
        if "/fixtures/" in url_clean:
            for row in rows:
                date_cell = row.find("td", class_="table-main__datetime")
                if not date_cell: continue
                spans = row.find_all("span")
                if len(spans) < 2: continue

                home = spans[0].get_text(strip=True)
                away = spans[1].get_text(strip=True)

                odds = []
                odds_cells = row.select("td.table-main__odds")
                for cell in odds_cells:
                    odd = cell.get("data-odd")
                    if not odd:
                        span = cell.find(attrs={"data-odd": True})
                        if span: odd = span.get("data-odd")
                    if not odd:
                        button = cell.find("button")
                        if button: odd = button.get_text(strip=True)
                    if not odd:
                        text = cell.get_text(" ", strip=True)
                        if text: odd = text
                    odds.append(odd if odd else "-")
                        
                odd1 = odds[0] if len(odds) >= 1 else "-"
                oddx = odds[1] if len(odds) >= 2 else "-"
                odd2 = odds[2] if len(odds) >= 3 else "-"

                all_data.append(["Fixture", league, date_cell.get_text(strip=True), home, away, "", odd1, oddx, odd2])
                mecz_count += 1
            scrape_report.append(["BetExplorer", url_clean, f"OK (Pobrano: {mecz_count} meczów)"])

        # RESULTS
        elif "/results/" in url_clean:
            current_date = ""
            for row in rows:
                match = row.find("a", class_="in-match")
                if not match: continue
                spans = match.find_all("span")
                if len(spans) < 2: continue

                home = spans[0].get_text(" ", strip=True)
                away = spans[1].get_text(" ", strip=True)

                score = ""
                score_cell = row.find("td", class_="h-text-center")
                if score_cell: score = score_cell.get_text(strip=True)

                odds_cells = row.select("td.table-main__odds")
                odds = []
                for cell in odds_cells:
                    odd = cell.get("data-odd")
                    if not odd:
                        span = cell.find(attrs={"data-odd": True})
                        if span: odd = span.get("data-odd")
                    odds.append(odd if odd else "-")

                odd1 = odds[0] if len(odds) >= 1 else "-"
                oddx = odds[1] if len(odds) >= 2 else "-"
                odd2 = odds[2] if len(odds) >= 3 else "-"

                date_cell = row.find("td", class_=lambda x: x and "h-text-right" in x)
                if date_cell:
                    parsed_date = date_cell.get_text(strip=True)
                    if parsed_date: current_date = parsed_date

                all_data.append(["Result", league, current_date, home, away, score, odd1, oddx, odd2])
                mecz_count += 1
            
            if mecz_count == 0:
                scrape_report.append(["BetExplorer", url_clean, "BŁĄD: Znaleziono 0 meczów (Pusty sezon / Zły link)"])
            else:
                scrape_report.append(["BetExplorer", url_clean, f"OK (Pobrano: {mecz_count} meczów)"])

    except Exception as e:
        scrape_report.append(["BetExplorer", url_clean, f"BŁĄD KRYTYCZNY: {str(e)}"])
        print("BŁĄD:", url_clean, e)

# ==========================================================
# DATAFRAME Z BETEXPLORER (Rozdzielenie i sortowanie)
# ==========================================================
df = pd.DataFrame(all_data, columns=["Type", "League", "Date", "Home", "Away", "Score", "Odd1", "OddX", "Odd2"])
df = df.drop_duplicates()

dates, times = [], []
for value in df["Date"]:
    d, t = split_datetime(value)
    dates.append(d)
    times.append(t)

df["Date"] = dates
df.insert(3, "Time", times)

fixtures_df = df[df["Type"] == "Fixture"].copy().sort_values(by=["Date", "Time"], ascending=True)
results_df = df[df["Type"] == "Result"].copy().sort_values(by=["Date"], ascending=False)

# ==========================================================
# 2. POBIERANIE Z SOCCERSTATS
# ==========================================================
dane_soccerstats_baza = []
print("Rozpoczynam pobieranie danych z SoccerStats...")

try:
    if os.path.exists("ligi_soccerstats.xlsx"):
        urls_ss = pd.read_excel("ligi_soccerstats.xlsx")["URL"].dropna().tolist()
        skaner_ss = cloudscraper.create_scraper()
        
        for i_ss, url_ss in enumerate(urls_ss, start=1):
            url_ss = str(url_ss).strip()
            nazwa_ligi = url_ss.split("league=")[1].split("&")[0] if "league=" in url_ss else f"Liga_{i_ss}"
            print(f"[{i_ss}/{len(urls_ss)}] Pobieram SoccerStats dla: {nazwa_ligi}")
            
            try:
                html_ss = skaner_ss.get(url_ss, headers={"User-Agent": headers["User-Agent"]}, timeout=30).text
                soup_ss = BeautifulSoup(html_ss, "html.parser")
                
                tabela_meczow = None
                for t in soup_ss.find_all("table"):
                    if "HT" in t.get_text() and "BTS" in t.get_text() and len(t.find_all("tr")) > 15:  
                        tabela_meczow = t
                        break
                
                if tabela_meczow:
                    wiersze_ss = tabela_meczow.find_all("tr")
                    ostatnia_data = ""
                    ss_count = 0
                    
                    for wiersz in wiersze_ss:
                        komorki = wiersz.find_all(["td", "th"])
                        if len(komorki) >= 6:
                            teksty = [k.get_text(" ", strip=True) for k in komorki]
                            wynik_index = -1
                            
                            for idx, val in enumerate(teksty):
                                if ("-" in val or ":" in val) and any(c.isdigit() for c in val) and 1 <= idx <= 5: 
                                    wynik_index = idx
                                    break
                                        
                            if wynik_index != -1:
                                wynik = teksty[wynik_index]
                                gospodarz = teksty[wynik_index - 1]
                                data = teksty[wynik_index - 2] if wynik_index >= 2 else teksty[0]
                                gosc = teksty[wynik_index + 1] if wynik_index + 1 < len(teksty) else ""
                                
                                if "HOME" in gospodarz.upper() or "GOSPODARZ" in gospodarz.upper(): continue
                                    
                                if data and len(data) > 2: ostatnia_data = data
                                else: data = ostatnia_data
                                    
                                if gospodarz and gosc and gosc != gospodarz:
                                    ht, o25, tg, bts = "-", "-", "-", "-"
                                    statystyki = [s for s in teksty[wynik_index + 2:] if s.strip()] 
                                    
                                    if len(statystyki) >= 4:
                                        ht, o25, tg, bts = statystyki[0], statystyki[1], statystyki[2], statystyki[3]
                                    elif len(statystyki) > 0:
                                        ht = statystyki[0]
                                        if len(statystyki) > 1: o25 = statystyki[1]
                                        if len(statystyki) > 2: tg = statystyki[2]
                                        
                                    wynik_czysty = wynik.replace("*", "").strip()
                                    if "-" in wynik_czysty: wynik_czysty = wynik_czysty.replace("-", ":").replace(" ", "")
                                        
                                    ht_czysty = ht.replace("*", "").strip()
                                    
                                    dane_soccerstats_baza.append([gospodarz, wynik_czysty, gosc, ht_czysty, o25.replace("*", "").strip(), tg.replace("*", "").strip(), bts.replace("*", "").strip()])
                                    ss_count += 1
                                    
                    if ss_count == 0: scrape_report.append(["SoccerStats", url_ss, "BŁĄD: Znaleziono tabelę, brak pasujących wierszy"])
                    else: scrape_report.append(["SoccerStats", url_ss, f"OK (Pobrano: {ss_count} wierszy)"])
                else:
                    scrape_report.append(["SoccerStats", url_ss, "BŁĄD: Nie znaleziono tabeli wyników na stronie"])
            except Exception as e:
                scrape_report.append(["SoccerStats", url_ss, f"BŁĄD: {str(e)}"])
                    
    if dane_soccerstats_baza:
        ss_df = pd.DataFrame(dane_soccerstats_baza, columns=["Home", "Score", "Away", "HT", "2.5+", "TG", "BTS"]).drop_duplicates()
    else:
        ss_df = pd.DataFrame()
except Exception as e:
    print("Wystąpił błąd SoccerStats:", e)
    ss_df = pd.DataFrame()

# ==========================================================
# 3. BEZPIECZNE SCALANIE DANYCH
# ==========================================================
print("Złączam dane z SoccerStats do tabeli Results...")

if os.path.exists("slownik_druzyn.json"):
    try:
        with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
            mapowanie_ss = json.load(f).get("SoccerStats_To_BetExplorer", {})
    except: mapowanie_ss = {}
else: mapowanie_ss = {}

if not ss_df.empty and not results_df.empty:
    ss_df["Home"] = ss_df["Home"].astype(str).str.strip().apply(lambda x: mapowanie_ss.get(x, x))
    ss_df["Away"] = ss_df["Away"].astype(str).str.strip().apply(lambda x: mapowanie_ss.get(x, x))
    
    ss_do_zlaczenia = ss_df.drop_duplicates(subset=["Home", "Away", "Score"])
    results_df = pd.merge(results_df, ss_do_zlaczenia, on=["Home", "Away", "Score"], how="left")
    results_df = results_df.fillna("-")

for col in ["HT", "2.5+", "TG", "BTS"]:
    if col not in results_df.columns:
        results_df[col] = "-"

# ==========================================
# 4. GOOGLE SHEETS AUTORYZACJA I WYSYŁKA
# ==========================================
scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

if os.path.exists("credentials.json"): creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
else: creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)

client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

try: fixtures_sheet = spreadsheet.worksheet("Fixtures")
except: fixtures_sheet = spreadsheet.add_worksheet(title="Fixtures", rows=1000, cols=20)

try: results_sheet = spreadsheet.worksheet("Results")
except: results_sheet = spreadsheet.add_worksheet(title="Results", rows=5000, cols=20)

try: summary_sheet = spreadsheet.worksheet("Summary")
except: summary_sheet = spreadsheet.add_worksheet(title="Summary", rows=100, cols=10)

fixtures_df = fixtures_df.fillna("-")
results_df = results_df.fillna("-")

for col in ["Odd1", "OddX", "Odd2"]:
    fixtures_df[col] = fixtures_df[col].apply(lambda x: str(x).replace(".", ",") if str(x) != "-" else "-")
    results_df[col] = results_df[col].apply(lambda x: str(x).replace(".", ",") if str(x) != "-" else "-")

print("Wysyłam Fixtures...")
fixtures_sheet.clear()
if not fixtures_df.empty: fixtures_sheet.update([fixtures_df.columns.tolist()] + fixtures_df.astype(str).values.tolist())

print("Wysyłam Results...")
results_sheet.clear()
if not results_df.empty: results_sheet.update([results_df.columns.tolist()] + results_df.astype(str).values.tolist())

print("Wysyłam Summary...")
summary_data = [
    ["==== PODSUMOWANIE OGÓLNE ====", "", ""],
    ["Ostatnia aktualizacja", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""],
    ["Fixtures", len(fixtures_df), ""],
    ["Results", len(results_df), ""],
    ["Leagues", df["League"].nunique() if not df.empty else 0, ""],
    ["", "", ""],
    ["==== RAPORT POBIERANIA Z LINKÓW ====", "", ""],
    ["System", "URL", "Status / Wynik"]
]
summary_data.extend(scrape_report)

summary_sheet.clear()
summary_sheet.update(summary_data)

print("\n" + "=" * 60)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Fixtures:", len(fixtures_df))
print("Results:", len(results_df))
print("=" * 60)
