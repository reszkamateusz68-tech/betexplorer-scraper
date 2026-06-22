import os
import json
import re
import time
import random
import numpy as np
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
                                    statystyki = [s for s in teksty[wynik_index + 2:] if s.strip()] 
                                    ht = statystyki[0] if len(statystyki) > 0 else "-"
                                        
                                    wynik_czysty = wynik.replace("*", "").strip().replace(" ", "").replace("-", ":")
                                    ht_czysty = ht.replace("*", "").strip().replace(" ", "").replace("-", ":").replace("(", "").replace(")", "")
                                    
                                    g_gosp_1h, g_gosc_1h = "-", "-"
                                    
                                    if ":" in ht_czysty:
                                        try:
                                            p_1h = ht_czysty.split(":")
                                            g_gosp_1h, g_gosc_1h = int(p_1h[0]), int(p_1h[1])
                                        except: pass
                                            
                                    dane_soccerstats_baza.append([gospodarz, gosc, wynik_czysty, g_gosp_1h, g_gosc_1h])
                                    ss_count += 1
                                    
                if ss_count == 0:
                    scrape_report.append(["SoccerStats", url_ss_clean, "BŁĄD: Znaleziono tabelę, ale 0 wierszy"])
                else:
                    scrape_report.append(["SoccerStats", url_ss_clean, f"OK (Pobrano: {ss_count} wierszy)"])
            except Exception as e:
                scrape_report.append(["SoccerStats", url_ss_clean, f"BŁĄD: {str(e)}"])
                        
        if dane_soccerstats_baza:
            ss_df = pd.DataFrame(dane_soccerstats_baza, columns=["Home", "Away", "Score", "Gole_Gosp_1H", "Gole_Gosc_1H"]).drop_duplicates(subset=["Home", "Away", "Score"])
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
    ss_df["Home"] = ss_df["Home"].apply(lambda x: mapowanie_ss.get(x, x))
    ss_df["Away"] = ss_df["Away"].apply(lambda x: mapowanie_ss.get(x, x))
    results_df = pd.merge(results_df, ss_df, on=["Home", "Away", "Score"], how="left")

print("Rozpoczynam integrację danych z Football-Data.co.uk...")
fd_df = fetch_football_data(scrape_report)

if not fd_df.empty and not results_df.empty:
    fd_df['HomeTeam'] = fd_df['HomeTeam'].astype(str).str.strip().replace(mapowanie_fd)
    fd_df['AwayTeam'] = fd_df['AwayTeam'].astype(str).str.strip().replace(mapowanie_fd)
    
    results_df['Date_str'] = pd.to_datetime(results_df['Date'], errors='coerce').astype(str)
    fd_df['Date_str'] = pd.to_datetime(fd_df['Date'], dayfirst=True, errors='coerce').astype(str)

    fd_df = fd_df.drop_duplicates(subset=['Date_str', 'HomeTeam', 'AwayTeam'], keep='last')
    fd_df = fd_df.rename(columns={'HomeTeam': 'Home', 'AwayTeam': 'Away'})

    results_df = pd.merge(
        results_df, 
        fd_df.drop(columns=['Date']),
        how='left', 
        left_on=['Date_str', 'Home', 'Away'], 
        right_on=['Date_str', 'Home', 'Away']
    )
    results_df = results_df.drop(columns=['Date_str'])
else:
    fd_cols = ['HTHG', 'HTAG', 'HS', 'AS', 'HST', 'AST', 'HC', 'AC', 'HY', 'AY', 'AvgH', 'AvgD', 'AvgA']
    for col in fd_cols: 
        if col not in results_df.columns:
            results_df[col] = "-"

# ==========================================
# 5. TWORZENIE ZŁOTEJ STRUKTURY
# ==========================================
print("Czyszczenie bazy - Złota Struktura...")

results_df[['FTHG', 'FTAG']] = results_df['Score'].str.split(':', expand=True)
results_df['FTHG'] = pd.to_numeric(results_df['FTHG'], errors='coerce')
results_df['FTAG'] = pd.to_numeric(results_df['FTAG'], errors='coerce')
results_df['Total_Goals'] = results_df['FTHG'] + results_df['FTAG']

if 'HTHG' not in results_df.columns: results_df['HTHG'] = np.nan
if 'HTAG' not in results_df.columns: results_df['HTAG'] = np.nan
if 'Gole_Gosp_1H' in results_df.columns:
    results_df['HTHG'] = results_df['HTHG'].combine_first(pd.to_numeric(results_df['Gole_Gosp_1H'], errors='coerce'))
    results_df['HTAG'] = results_df['HTAG'].combine_first(pd.to_numeric(results_df['Gole_Gosc_1H'], errors='coerce'))

fd_expected_cols = ['HS', 'AS', 'HST', 'AST', 'HC', 'AC', 'HY', 'AY', 'AvgH', 'AvgD', 'AvgA']
for col in fd_expected_cols:
    if col not in results_df.columns: results_df[col] = np.nan

results_df['Date_str'] = pd.to_datetime(results_df['Date'], errors='coerce').dt.strftime('%Y%m%d').fillna('99999999')
results_df['Match_ID'] = results_df['Date_str'] + "_" + results_df['Home'].str[:3].str.upper() + "_" + results_df['Away'].str[:3].str.upper()
fixtures_df['Date_str'] = pd.to_datetime(fixtures_df['Date'], errors='coerce').dt.strftime('%Y%m%d').fillna('99999999')
fixtures_df['Match_ID'] = fixtures_df['Date_str'] + "_" + fixtures_df['Home'].str[:3].str.upper() + "_" + fixtures_df['Away'].str[:3].str.upper()

def calc_value(odd, avg):
    try:
        o, a = float(str(odd).replace(',', '.')), float(str(avg).replace(',', '.'))
        if a > 0: return round(((o / a) - 1) * 100, 2)
    except: pass
    return np.nan

for outcome in [('Odd1', 'AvgH', 'Val_1'), ('OddX', 'AvgD', 'Val_X'), ('Odd2', 'AvgA', 'Val_2')]:
    results_df[outcome[2]] = results_df.apply(lambda row: calc_value(row[outcome[0]], row[outcome[1]]), axis=1)

golden_cols = {
    'Match_ID': 'Match_ID', 'Date': 'Date', 'Time': 'Time', 'League': 'League', 'Home': 'Home', 'Away': 'Away',
    'FTHG': 'FTHG', 'FTAG': 'FTAG', 'Total_Goals': 'Total_Goals', 'HTHG': 'HTHG', 'HTAG': 'HTAG',
    'HS': 'Shots_H', 'AS': 'Shots_A', 'HST': 'ShotsTarget_H', 'AST': 'ShotsTarget_A',
    'HC': 'Corners_H', 'AC': 'Corners_A', 'HY': 'Cards_H', 'AY': 'Cards_A',
    'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2',
    'AvgH': 'Avg_1', 'AvgD': 'Avg_X', 'AvgA': 'Avg_2',
    'Val_1': 'Val_1', 'Val_X': 'Val_X', 'Val_2': 'Val_2'
}

results_clean = results_df[list(golden_cols.keys())].rename(columns=golden_cols)
fixtures_clean = fixtures_df[['Match_ID', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd1', 'OddX', 'Odd2']].rename(columns={'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2'})

# ==========================================
# 6. GENEROWANIE TABEL LIGOWYCH
# ==========================================
print("Generowanie inteligentnych tabel ligowych...")
valid_matches = results_clean.dropna(subset=['FTHG', 'FTAG']).copy()

if not valid_matches.empty:
    valid_matches['FTHG'] = pd.to_numeric(valid_matches['FTHG'], errors='coerce')
    valid_matches['FTAG'] = pd.to_numeric(valid_matches['FTAG'], errors='coerce')
    
    home_rec = valid_matches[['League', 'Home', 'FTHG', 'FTAG']].copy()
    home_rec.columns = ['League', 'Team', 'GF', 'GA']
    home_rec['Pts'] = np.where(home_rec['GF'] > home_rec['GA'], 3, np.where(home_rec['GF'] == home_rec['GA'], 1, 0))
    home_rec['W'] = np.where(home_rec['GF'] > home_rec['GA'], 1, 0)
    home_rec['D'] = np.where(home_rec['GF'] == home_rec['GA'], 1, 0)
    home_rec['L'] = np.where(home_rec['GF'] < home_rec['GA'], 1, 0)
    home_rec['M'] = 1

    away_rec = valid_matches[['League', 'Away', 'FTAG', 'FTHG']].copy()
    away_rec.columns = ['League', 'Team', 'GF', 'GA']
    away_rec['Pts'] = np.where(away_rec['GF'] > away_rec['GA'], 3, np.where(away_rec['GF'] == away_rec['GA'], 1, 0))
    away_rec['W'] = np.where(away_rec['GF'] > away_rec['GA'], 1, 0)
    away_rec['D'] = np.where(away_rec['GF'] == away_rec['GA'], 1, 0)
    away_rec['L'] = np.where(away_rec['GF'] < away_rec['GA'], 1, 0)
    away_rec['M'] = 1

    all_rec = pd.concat([home_rec, away_rec])
    league_tables = all_rec.groupby(['League', 'Team']).sum().reset_index()
    league_tables['GD'] = league_tables['GF'] - league_tables['GA']
    league_tables['PPG'] = round(league_tables['Pts'] / league_tables['M'].replace(0, 1), 2)

    league_tables = league_tables.sort_values(by=['League', 'Pts', 'GD', 'GF'], ascending=[True, False, False, False])
    league_tables = league_tables[['League', 'Team', 'M', 'W', 'D', 'L', 'GF', 'GA', 'GD', 'Pts', 'PPG']]
else:
    league_tables = pd.DataFrame(columns=['League', 'Team', 'M', 'W', 'D', 'L', 'GF', 'GA', 'GD', 'Pts', 'PPG'])

# ==========================================
# 7. KULOODPORNY FORMATER DO GOOGLE SHEETS
# ==========================================
def prepare_for_gsheets(df):
    """
    Całkowicie omija wady formatowania Pandas. 
    Bezpiecznie filtruje puste pola, ucina ułamki z wyników i formuje gotową strukturę pod gspread.
    """
    output = [df.columns.tolist()]
    for row in df.values.tolist():
        new_row = []
        for idx, val in enumerate(row):
            col_name = df.columns[idx]
            
            if pd.isna(val):
                new_row.append("-")
                continue
                
            str_val = str(val).strip()
            
            # Łapanie wszystkich ukrytych błędów "pustki" z Pandas
            if str_val in ["<NA>", "nan", "NaN", "None", "", "inf", "-inf"]:
                new_row.append("-")
            else:
                # Zamiana kropki na przecinek TYLKO w finansach i średnich punktach
                if any(k in col_name for k in ["Odd", "Avg", "Val", "PPG"]):
                    new_row.append(str_val.replace(".", ","))
                else:
                    # Zamienia "2.0" rożnego na czyste "2"
                    if str_val.endswith(".0"):
                        new_row.append(str_val[:-2])
                    else:
                        new_row.append(str_val)
        output.append(new_row)
    return output

# ==========================================
# 8. WYSYŁKA GOOGLE SHEETS
# ==========================================
scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file("credentials.json", scopes=scope) if os.path.exists("credentials.json") else Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

for sheet_name in ["Summary", "Fixtures", "Results", "League_Tables"]:
    try: spreadsheet.worksheet(sheet_name)
    except: spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=30)

try:
    spreadsheet.worksheet("Fixtures").resize(rows=5000, cols=35)
    spreadsheet.worksheet("Results").resize(rows=10000, cols=65) 
except: pass

print("Wysyłam Czysty Terminarz do Google Sheets...")
spreadsheet.worksheet("Fixtures").clear()
if not fixtures_clean.empty:
    spreadsheet.worksheet("Fixtures").update(prepare_for_gsheets(fixtures_clean))

print("Wysyłam Historię ze statystykami do Google Sheets...")
spreadsheet.worksheet("Results").clear()
if not results_clean.empty:
    spreadsheet.worksheet("Results").update(prepare_for_gsheets(results_clean))

print("Wysyłam Tabele Ligowe...")
spreadsheet.worksheet("League_Tables").clear()
if not league_tables.empty:
    spreadsheet.worksheet("League_Tables").update(prepare_for_gsheets(league_tables))

print("Wysyłam Logi Pobierania (Summary) do Google Sheets...")
summary_data = [
    ["==== PODSUMOWANIE OGÓLNE ====", "", ""],
    ["Ostatnia aktualizacja", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""],
    ["Fixtures Czyste", len(fixtures_clean), ""],
    ["Results Zintegrowane", len(results_clean), ""],
    ["Tabela Drużyn", len(league_tables), ""],
    ["", "", ""],
    ["==== RAPORT POBIERANIA Z LINKÓW ====", "", ""],
    ["System", "URL", "Status / Wynik"]
]
summary_data.extend(scrape_report)
spreadsheet.worksheet("Summary").clear()
spreadsheet.worksheet("Summary").update(summary_data)

print("\n" + "=" * 60)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Fixtures:", len(fixtures_clean))
print("Results:", len(results_clean))
print("=" * 60)
