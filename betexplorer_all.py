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

# Wczytanie słownika z mapowaniem nazw drużyn
try:
    with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
        slownik = json.load(f)
        fd_dict = slownik.get("FootballData_To_BetExplorer", {})
except FileNotFoundError:
    print("Brak pliku slownik_druzyn.json. Pobieram dane bez mapowania nazw.")
    fd_dict = {}

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

def fetch_football_data():
    print("Pobieram linki i statystyki z ligi_footballdata.xlsx...")
    
    # 1. Wczytanie URL-i z pliku Excel
    try:
        urls = pd.read_excel("ligi_footballdata.xlsx")["URL"].dropna().tolist()
    except FileNotFoundError:
        print("UWAGA: Nie znaleziono pliku ligi_footballdata.xlsx! Pomijam statystyki.")
        return pd.DataFrame()
    except Exception as e:
        print(f"Błąd podczas wczytywania pliku Excel: {e}")
        return pd.DataFrame()
    
    # 2. Pobieranie danych z każdego linku
    dfs = []
    for url in urls:
        try:
            df_fd = pd.read_csv(url.strip())
            dfs.append(df_fd)
        except Exception as e:
            print(f"Błąd pobierania CSV z {url}: {e}")
            
    if not dfs:
        return pd.DataFrame()
        
    # 3. Złączenie wszystkich lig w jedną dużą tabelę
    fd_master = pd.concat(dfs, ignore_index=True)
    
    # 4. Wybieramy tylko te kolumny, które nas interesują (statystyki i uśrednione kursy)
    cols_to_keep = ['Date', 'HomeTeam', 'AwayTeam', 
                    'HTHG', 'HTAG', 'HS', 'AS', 'HST', 'AST', 
                    'HC', 'AC', 'HY', 'AY', 'AvgH', 'AvgD', 'AvgA']
    
    existing_cols = [col for col in cols_to_keep if col in fd_master.columns]
    fd_master = fd_master[existing_cols]
    
    # 5. Normalizacja formatu daty
    fd_master['Date'] = pd.to_datetime(fd_master['Date'], format='mixed', dayfirst=True).dt.date
    
    return fd_master

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

fixtures_df = df[df["Type"] == "Fixture"].copy().sort_values(by=["Date", "
