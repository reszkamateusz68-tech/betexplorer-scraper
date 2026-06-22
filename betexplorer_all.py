import os
import json
import re
import time
import random
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
    except:
        pass

    try:
        return (datetime.strptime(value, "%d.%m.%Y").date(), "")
    except:
        pass

    try:
        if value.endswith("."):
            day, month = value.rstrip(".").split(".")
            return (datetime(today.year, int(month), int(day)).date(), "")
    except:
        pass

    return value, ""

# LISTA NA LOGI DO ZAKŁADKI SUMMARY
scrape_report = []

# ==========================================================
# FUNKCJA POBIERAJĄCA FOOTBALL-DATA Z ZACHOWANIEM WSZYSTKICH KOLUMN
# ==========================================================
def fetch_football_data(raport):
    print("Pobieram statystyki z ligi_footballdata.xlsx...")
    try:
        urls = pd.read_excel("ligi_footballdata.xlsx")["URL"].dropna().tolist()
    except Exception as e:
        print(f"Błąd Excela (Football-Data): {e}")
        raport.append(["Football-Data", "ligi_footballdata.xlsx", f"BŁĄD Excela: {e}"])
        return pd.DataFrame()

    dfs = []
    for url in urls:
        url_clean = str(url).strip()
        try:
            df_fd = pd.read_csv(url_clean, on_bad_lines='skip')
            df_fd = df_fd.dropna(subset=['HomeTeam'])
            dfs.append(df_fd)
            raport.append(["Football-Data", url_clean, f"OK (Pobrano: {len(df_fd)} wierszy)"])
        except Exception as e:
            print(f"Błąd pobierania CSV z {url_clean}: {e}")
            raport.append(["Football-Data", url_clean, f"BŁĄD: {e}"])

    if not dfs:
        return pd.DataFrame()

    fd_master = pd.concat(dfs, ignore_index=True)

    # Zachowujemy pełny zestaw surowych danych
    cols_to_keep = ['Date', 'HomeTeam', 'AwayTeam',
                    'HTHG', 'HTAG', 'HS', 'AS', 'HST', 'AST',
                    'HC', 'AC', 'HY', 'AY', 'AvgH', 'AvgD', 'AvgA']

    existing_cols = [col for col in cols_to_keep if col in fd_master.columns]
    fd_master = fd_master[existing_cols]

    return fd_master

# ==========================================
# 1. POBIERANIE Z BETEXPLORER 
# ==========================================
try: urls = pd.read_excel("ligi.xlsx")["URL"].dropna().tolist()
except: urls = []

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache"
}

all_data = []

for i, url in enumerate(urls, start=1):
    url_clean = str(url).strip()
    print(f"[{i}/{len(urls)}] Pobieram BetExplorer: {url_clean}")
    
    if "/fixtures/" not in url_clean and "/results/" not in url_clean:
        scrape_report.append(["BetExplorer", url_clean, "BŁĄD: Zły format linku"])
        continue

    try:
        time.sleep(random.uniform(2, 5))
        response = requests.get(url_clean, headers=headers, timeout=30)
        
        bypass_used = False
        if response.status_code in [429, 403]:
            print(f"   -> Wykryto limit (Kod {response.status_code}). Uruchamiam system omijający...")
            time.sleep(5)
            scraper_be = cloudscraper.create_scraper()
            response = scraper_be.get(url_clean, headers=headers, timeout=30)
            bypass_used = True

        if response.status_code != 200:
            print(f"   -> POMINIĘTO: Kod błędu {response.status_code}")
            scrape_report.append(["BetExplorer", url_clean, f"BŁĄD: Kod {response.status_code}"])
            continue

        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        
        league = url_clean.split("/football/")[1].replace("/fixtures/", "").replace("/results/", "")
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

        # RESULTS
        elif "/results/" in url_clean:
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
                    if not odd:
                        text = cell.get_text(" ", strip=True)
                        if text: odd = text
                    odds.append(odd if odd else "-")

                odd1 = odds[0] if len(odds) >= 1 else "-"
                oddx = odds[1] if len(odds) >= 2 else "-"
                odd2 = odds[2] if len(odds) >= 3 else "-"

                date = ""
                date_cell = row.find("td", class_=lambda x: x and "h-text-right" in x)
                if date_cell: date = date_cell.get_text(strip=True)

                all_data.append(["Result", league, date, home, away, score, odd1, oddx, odd2])
                mecz_count += 1
                
        status_msg = f"OK (Pobrano: {mecz_count} meczów)"
        if bypass_used: status_msg += " [Zadziałał Bypass 429]"
        if mecz_count == 0: status_msg = "BŁĄD: Znaleziono 0 meczów"
        
        scrape_report.append(["BetExplorer", url_clean, status_msg])

    except Exception as e:
        print("BŁĄD:", url_clean, e)
        scrape_report.append(["BetExplorer", url_clean, f"BŁĄD KRYTYCZNY: {e}"])

# ==========================================
# 2. DATAFRAME I INTELIGENTNE USUWANIE DUPLIKATÓW
# ==========================================
df = pd.DataFrame(all_data, columns=["Type", "League", "Date", "Home", "Away", "Score", "Odd1", "OddX", "Odd2"])
df = df.drop_duplicates()

dates, times = [], []
for value in df["Date"]:
    d, t = split_datetime(value)
    dates.append(d)
    times.append(t)

df["Date"] = dates
df.insert(3, "Time", times)

fixtures_df = df[df["Type"] == "Fixture"].copy()
results_df = df[df["Type"] == "Result"].copy()

fixtures_df['HasOdds'] = fixtures_df['Odd1'].astype(str).apply(lambda x: 1 if x.strip() not in ["", "-", "nan"] else 0)
fixtures_df = fixtures_df.sort_values(by=["Date", "Time", "Home", "Away", "HasOdds"], ascending=[True, True, True, True, False])
fixtures_df = fixtures_df.drop_duplicates(subset=["Date", "Home", "Away"]).drop(columns=["HasOdds"])

results_df['HasOdds'] = results_df['Odd1'].astype(str).apply(lambda x: 1 if x.strip() not in ["", "-", "nan"] else 0)
results_df = results_df.sort_values(by=["Date", "Home", "Away", "HasOdds"], ascending=[False, True, True, False])
results_df = results_df.drop_duplicates(subset=["Date", "Home", "Away"]).drop(columns=["HasOdds"])


# ==========================================================
# 3. POBIERANIE Z SOCCERSTATS
# ==========================================================
dane_soccerstats_baza = []
print("Rozpoczynam pobieranie danych z SoccerStats...")

try:
    if os.path.exists("ligi_soccerstats.xlsx"):
        urls_ss = pd.read_excel("ligi_soccerstats.xlsx")["URL"].dropna().tolist()
        skaner_ss = cloudscraper.create_scraper()
        
        for i_ss, url_ss in enumerate(urls_ss, start=1):
            url_ss_clean = str(url_ss).strip()
            nazwa_ligi = url_ss_clean.split("league=")[1].split("&")[0] if "league=" in url_ss_clean else f"Liga_{i_ss}"
            print(f"[{i_ss}/{len(urls_ss)}] Pobieram SoccerStats dla: {nazwa_ligi}")
            
            try:
                time.sleep(random.uniform(1, 3))
                html_ss = skaner_ss.get(url_ss_clean, headers=headers, timeout=30).text
                soup_ss = BeautifulSoup(html_ss, "html.parser")
                
                tabela_meczow = None
                for t in soup_ss.find_all("table"):
                    if "HT" in t.get_text() and "BTS" in t.get_text() and len(t.find_all("tr")) > 15:
                        tabela_meczow = t
                        break
                
                ss_count = 0
                if tabela_meczow:
                    wiersze_ss = tabela_meczow.find_all("tr")
                    for wiersz in wiersze_ss:
                        komorki = wiersz.find_all(["td", "th"])
                        if len(komorki) >= 6:
                            teksty = [k.get_text(" ", strip=True) for k in komorki]
                            wynik_index = -1
                            for idx, val in enumerate(teksty):
                                if ("-" in val or ":" in val) and any(c.isdigit() for c in val):
                                    if 1 <= idx <= 5: 
                                        wynik_index = idx
                                        break
                                            
                            if wynik_index != -1:
                                wynik = teksty[wynik_index]
                                gospodarz = teksty[wynik_index - 1]
                                gosc = teksty[wynik_index + 1] if wynik_index + 1 < len(teksty) else ""
                                
                                if "HOME" in gospodarz.upper() or "GOSPODARZ" in gospodarz.upper(): continue
                                    
                                if gospodarz and gosc and gosc != gospodarz:
                                    ht, o25, tg, bts = "-", "-", "-", "-"
                                    statystyki = [s for s in teksty[wynik_index + 2:] if s.strip()] 
                                    
                                    if len(statystyki) >= 4:
                                        ht, o25, tg, bts = statystyki[0], statystyki[1], statystyki[2], statystyki[3]
                                    elif len(statystyki) > 0:
                                        ht = statystyki[0]
                                        if len(statystyki) > 1: o25 = statystyki[1]
                                        if len(statystyki) > 2: tg = statystyki[2]
                                        
                                    wynik_czysty = wynik.replace("*", "").strip().replace(" ", "").replace("-", ":")
                                    ht_czysty = ht.replace("*", "").strip().replace(" ", "").replace("-", ":").replace("(", "").replace(")", "")
                                    
                                    g_gosp_m, g_gosc_m, suma_m = "-", "-", "-"
                                    g_gosp_1h, g_gosc_1h, suma_1h = "-", "-", "-"
                                    g_gosp_2h, g_gosc_2h, suma_2h = "-", "-", "-"
                                    
                                    if ":" in wynik_czysty:
                                        try:
                                            p_m = wynik_czysty.split(":")
                                            g_gosp_m, g_gosc_m = int(p_m[0]), int(p_m[1])
                                            suma_m = g_gosp_m + g_gosc_m
                                        except: pass
                                            
                                    if ":" in ht_czysty:
                                        try:
                                            p_1h = ht_czysty.split(":")
                                            g_gosp_1h, g_gosc_1h = int(p_1h[0]), int(p_1h[1])
                                            suma_1h = g_gosp_1h + g_gosc_1h
                                        except: pass
                                            
                                    if isinstance(g_gosp_m, int) and isinstance(g_gosp_1h, int):
                                        try:
                                            g_gosp_2h, g_gosc_2h = g_gosp_m - g_gosp_1h, g_gosc_m - g_gosc_1h
                                            suma_2h = g_gosp_2h + g_gosc_2h
                                        except: pass
                                    
                                    dane_soccerstats_baza.append([
                                        gospodarz, gosc, wynik_czysty,
                                        g_gosp_m, g_gosc_m, suma_m,
                                        ht_czysty, g_gosp_1h, g_gosc_1h, suma_1h,
                                        g_gosp_2h, g_gosc_2h, suma_2h,
                                        o25.replace("*", "").strip(), tg.replace("*", "").strip(), bts.replace("*", "").strip()
                                    ])
                                    ss_count += 1
                                    
                if ss_count == 0:
                    scrape_report.append(["SoccerStats", url_ss_clean, "BŁĄD: Znaleziono tabelę, ale 0 wierszy"])
                else:
                    scrape_report.append(["SoccerStats", url_ss_clean, f"OK (Pobrano: {ss_count} wierszy)"])
            except Exception as e:
                scrape_report.append(["SoccerStats", url_ss_clean, f"BŁĄD: {str(e)}"])
                        
        if dane_soccerstats_baza:
            ss_df = pd.DataFrame(dane_soccerstats_baza, columns=[
                "Home", "Away", "Score",
                "Gole_Gosp_Mecz", "Gole_Gosc_Mecz", "Suma_Goli_Mecz",
                "Wynik_HT", "Gole_Gosp_1H", "Gole_Gosc_1H", "Suma_Goli_1H",
                "Gole_Gosp_2H", "Gole_Gosc_2H", "Suma_Goli_2H", "2.5+", "TG", "BTS"
            ])
            ss_df = ss_df.drop_duplicates(subset=["Home", "Away", "Score"])
        else: ss_df = pd.DataFrame()
            
except Exception as e: 
    print("Błąd SoccerStats:", e)
    ss_df = pd.DataFrame()


# ==========================================================
# 4. MAPOWANIE I SCALANIE DANYCH 
# ==========================================================
mapowanie_ss, mapowanie_fd = {}, {}
if os.path.exists("slownik_druzyn.json"):
    try:
        with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
            slownik_data = json.load(f)
            mapowanie_ss = slownik_data.get("SoccerStats_To_BetExplorer", {})
            mapowanie_fd = slownik_data.get("FootballData_To_BetExplorer", {})
    except: pass

if not ss_df.empty and not results_df.empty:
    print("Ujednolicam nazwy i scalam historię z SoccerStats...")
    ss_do_scalenia = ss_df.copy()
    ss_do_scalenia["Home"] = ss_do_scalenia["Home"].apply(lambda x: mapowanie_ss.get(x, x))
    ss_do_scalenia["Away"] = ss_do_scalenia["Away"].apply(lambda x: mapowanie_ss.get(x, x))
    
    results_df = pd.merge(results_df, ss_do_scalenia, on=["Home", "Away", "Score"], how="left")

print("Rozpoczynam integrację danych z Football-Data.co.uk...")
fd_df = fetch_football_data(scrape_report)

if not fd_df.empty and not results_df.empty:
    fd_df['HomeTeam'] = fd_df['HomeTeam'].astype(str).str.strip().replace(mapowanie_fd)
    fd_df['AwayTeam'] = fd_df['AwayTeam'].astype(str).str.strip().replace(mapowanie_fd)
    
    # Przemiana daty na string, by zapobiec błędom złączania
    results_df['Date_str'] = pd.to_datetime(results_df['Date'], errors='coerce').astype(str)
    fd_df['Date_str'] = pd.to_datetime(fd_df['Date'], dayfirst=True, errors='coerce').astype(str)

    fd_df = fd_df.drop_duplicates(subset=['Date_str', 'HomeTeam', 'AwayTeam'], keep='last')
    fd_df = fd_df.rename(columns={'HomeTeam': 'Home', 'AwayTeam': 'Away'})

    # Bezpieczne Merge po Dacie i Drużynach (najlepsza precyzja)
    results_df = pd.merge(
        results_df, 
        fd_df.drop(columns=['Date']), # Unikamy duplikowania kolumny Data (Date_x, Date_y)
        how='left', 
        left_on=['Date_str', 'Home', 'Away'], 
        right_on=['Date_str', 'Home', 'Away']
    )
    results_df = results_df.drop(columns=['Date_str'])
else:
    # Wypełniamy puste miejsca, jeśli brak plików z Football-Data
    fd_cols = ['HTHG', 'HTAG', 'HS', 'AS', 'HST', 'AST', 'HC', 'AC', 'HY', 'AY', 'AvgH', 'AvgD', 'AvgA']
    for col in fd_cols: 
        if col not in results_df.columns:
            results_df[col] = "-"

# ==========================================
# 5. CZYSZCZENIE I FORMATOWANIE DO GOOGLE SHEETS
# ==========================================

# Przekształcamy wszystkie statystyki na czyste liczby (bez kropek i zer po przecinku)
kolumny_liczbowe = [
    "Gole_Gosp_Mecz", "Gole_Gosc_Mecz", "Suma_Goli_Mecz",
    "Gole_Gosp_1H", "Gole_Gosc_1H", "Suma_Goli_1H",
    "Gole_Gosp_2H", "Gole_Gosc_2H", "Suma_Goli_2H",
    'HTHG', 'HTAG', 'HS', 'AS', 'HST', 'AST', 'HC', 'AC', 'HY', 'AY'
]

if not results_df.empty:
    for col in kolumny_liczbowe:
        if col in results_df.columns:
            results_df[col] = pd.to_numeric(results_df[col], errors='coerce')
            results_df[col] = results_df[col].apply(lambda x: str(int(x)) if pd.notnull(x) else "-")

if not fixtures_df.empty: fixtures_df = fixtures_df.fillna("-")
if not results_df.empty: results_df = results_df.fillna("-")

# Zmiana kropek na przecinki dla uśrednionych kursów
for col in ["Odd1", "OddX", "Odd2", "AvgH", "AvgD", "AvgA"]:
    if col in fixtures_df.columns:
        fixtures_df[col] = fixtures_df[col].astype(str).apply(lambda x: x.replace(".", ",") if x not in ["-", "", "nan", "NaN"] else "-")
    if col in results_df.columns:
        results_df[col] = results_df[col].astype(str).apply(lambda x: x.replace(".", ",") if x not in ["-", "", "nan", "NaN"] else "-")

# ==========================================
# 6. GOOGLE SHEETS AUTORYZACJA I ZAPIS
# ==========================================
scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

if os.path.exists("credentials.json"): creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
else: creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)

client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

try: summary_sheet = spreadsheet.worksheet("Summary")
except: summary_sheet = spreadsheet.add_worksheet(title="Summary", rows=100, cols=10)

try: fixtures_sheet = spreadsheet.worksheet("Fixtures")
except: fixtures_sheet = spreadsheet.add_worksheet(title="Fixtures", rows=1000, cols=35)

try: results_sheet = spreadsheet.worksheet("Results")
except: results_sheet = spreadsheet.add_worksheet(title="Results", rows=5000, cols=35)

# Zabezpieczenie przed błędem zbyt małej ilości kolumn
try:
    fixtures_sheet.resize(rows=5000, cols=35)
    results_sheet.resize(rows=10000, cols=65) # <--- Zwiększono do 65 kolumn
except: pass

print("Wysyłam Czysty Terminarz do Google Sheets...")
fixtures_sheet.clear()
fixtures_sheet.update([fixtures_df.columns.tolist()] + fixtures_df.astype(str).values.tolist())

print("Wysyłam Historię ze statystykami do Google Sheets...")
results_sheet.clear()
results_sheet.update([results_df.columns.tolist()] + results_df.astype(str).values.tolist())

print("Wysyłam Logi Pobierania (Summary) do Google Sheets...")
summary_data = [
    ["==== PODSUMOWANIE OGÓLNE ====", "", ""],
    ["Ostatnia aktualizacja", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""],
    ["Fixtures Czyste", len(fixtures_df), ""],
    ["Results Zintegrowane", len(results_df), ""],
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
