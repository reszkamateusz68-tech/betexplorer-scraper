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

# ==========================================================
# GŁÓWNE FUNKCJE POMOCNICZE
# ==========================================================
def split_datetime(value):
    if pd.isna(value): return None, ""
    value = str(value).strip()
    if value.lower().startswith("today"): return today.date(), value.replace("Today", "").strip()
    if value.lower().startswith("tomorrow"): return (today.date() + timedelta(days=1), value.replace("Tomorrow", "").strip())
    if value.lower().startswith("yesterday"): return (today.date() - timedelta(days=1), value.replace("Yesterday", "").strip())
    try:
        parts = value.split()
        if len(parts) == 2 and parts[0].endswith("."):
            day, month = parts[0].rstrip(".").split(".")
            return (datetime(today.year, int(month), int(day)).date(), parts[1])
    except: pass
    try: return (datetime.strptime(value, "%d.%m.%Y").date(), "")
    except: pass
    try:
        if value.endswith("."):
            day, month = value.rstrip(".").split(".")
            return (datetime(today.year, int(month), int(day)).date(), "")
    except: pass
    return value, ""

def categorize_date(d_str):
    # 1. Zabezpieczenie przed pustymi wartościami z arkusza
    if pd.isna(d_str) or str(d_str).strip() in ["", "nan", "NaT", "None"]:
        return "Nieznany"
        
    try:
        # 2. Bezpieczne parsowanie (errors='coerce' zamieni błędy na NaT)
        d = pd.to_datetime(str(d_str), format='%d.%m.%Y', errors='coerce')
        if pd.isna(d):
            d = pd.to_datetime(str(d_str), errors='coerce')
            
        # 3. Jeśli ostatecznie wyszło NaT, przerywamy
        if pd.isna(d):
            return "Nieznany"
            
        d_date = d.date()
        today_date = datetime.now().date()
        
        # 4. Bezpieczna matematyka
        delta = (d_date - today_date).days
        
        if delta < 0: return "Przeszłość"
        if delta == 0: return "Dziś"
        if delta == 1: return "Jutro"
        
        # Jeśli to w ciągu najbliższych 7 dni i jest to Piątek(4), Sobota(5) lub Niedziela(6)
        if 2 <= delta <= 7 and d_date.weekday() >= 4:
            return "Ten Weekend"
        elif 2 <= delta <= 7:
            return "Ten Tydzień"
        else:
            return "Przyszłość"
            
    except Exception:
        return "Nieznany"

def get_base_league(l):
    clean_l = str(l).split('?')[0].strip('/')
    clean_l = re.sub(r'-\d{4}(-\d{4})?$', '', clean_l)
    return clean_l

def fetch_football_data(raport):
    print("Pobieram statystyki z ligi_footballdata.xlsx...")
    try: urls = pd.read_excel("ligi_footballdata.xlsx")["URL"].dropna().tolist()
    except Exception as e:
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
        except Exception as e: raport.append(["Football-Data", url_clean, f"BŁĄD: {e}"])
    if not dfs: return pd.DataFrame()
    fd_master = pd.concat(dfs, ignore_index=True)
    cols_to_keep = ['Date', 'HomeTeam', 'AwayTeam', 'HTHG', 'HTAG', 'HS', 'AS', 'HST', 'AST', 'HC', 'AC', 'Odd1', 'OddX', 'Odd2']
    existing_cols = [col for col in cols_to_keep if col in fd_master.columns]
    return fd_master[existing_cols]

def get_current_streaks(base_lg, team):
    if 'valid_matches' not in globals() or valid_matches.empty: return 0, 0
    t_matches = valid_matches[(valid_matches['Base_League'] == base_lg) & ((valid_matches['Home'] == team) | (valid_matches['Away'] == team))].copy()
    unbeaten, winless = 0, 0
    ub_broken, wl_broken = False, False
    for _, m in t_matches.iterrows():
        is_home = (m['Home'] == team)
        scored = int(m['FTHG']) if is_home else int(m['FTAG'])
        conceded = int(m['FTAG']) if is_home else int(m['FTHG'])
        if not ub_broken:
            if scored >= conceded: unbeaten += 1
            else: ub_broken = True
        if not wl_broken:
            if scored <= conceded: winless += 1
            else: wl_broken = True
        if ub_broken and wl_broken: break
    return unbeaten, winless

def get_last_match_goals(base_lg, team):
    if 'valid_matches' not in globals() or valid_matches.empty: return -1
    t_matches = valid_matches[(valid_matches['Base_League'] == base_lg) & ((valid_matches['Home'] == team) | (valid_matches['Away'] == team))]
    if t_matches.empty: return -1
    last_m = t_matches.iloc[0]
    return int(last_m['Total_Goals'])

def prepare_for_gsheets(df):
    df = df.astype(str)
    output = [df.columns.tolist()]
    for row in df.values.tolist():
        new_row = []
        for idx, val in enumerate(row):
            col_name = df.columns[idx]
            if pd.isna(val) or val == "nan":
                new_row.append("-")
                continue
            str_val = str(val).strip()
            if str_val in ["<NA>", "NaN", "None", "", "inf", "-inf"]:
                new_row.append("-")
            else:
                if any(k in col_name for k in ["Odd", "Avg", "Value", "PPG", "Prawdopodobieństwo", "Pewność", "Kurs", "Szansa"]):
                    new_row.append(str_val.replace(".", ","))
                else:
                    if str_val.endswith(".0") and "%" not in str_val: new_row.append(str_val[:-2])
                    else: new_row.append(str_val)
        output.append(new_row)
    return output

scrape_report = []
try:
    with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
        slownik = json.load(f)
        mapowanie_fd = slownik.get("FootballData_To_BetExplorer", {})
        mapowanie_ss = slownik.get("SoccerStats_To_BetExplorer", {})
except Exception:
    mapowanie_fd, mapowanie_ss = {}, {}

# ==========================================
# 1. POBIERANIE Z BETEXPLORER 
# ==========================================
try: urls = pd.read_excel("ligi.xlsx")["URL"].dropna().tolist()
except: urls = []

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache"
}

all_data = []
for i, url in enumerate(urls, start=1):
    url_clean = str(url).strip()
    print(f"[{i}/{len(urls)}] Pobieram BetExplorer: {url_clean}")
    if "/fixtures/" not in url_clean and "/results/" not in url_clean: continue

    try:
        time.sleep(random.uniform(2, 5))
        response = requests.get(url_clean, headers=headers, timeout=30)
        
        bypass_used = False
        if response.status_code in [429, 403]:
            time.sleep(12)
            scraper_be = cloudscraper.create_scraper()
            response = scraper_be.get(url_clean, headers=headers, timeout=30)
            bypass_used = True
            if response.status_code in [429, 403]:
                time.sleep(15)
                response = scraper_be.get(url_clean, headers=headers, timeout=30)

        if response.status_code != 200:
            scrape_report.append(["BetExplorer", url_clean, f"BŁĄD: Kod {response.status_code}"])
            continue

        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        league_raw = url_clean.split("/football/")[1].replace("/fixtures/", "").replace("/results/", "")
        league = league_raw.split('?')[0].strip('/')
        
        rows = soup.find_all("tr")
        mecz_count = 0

        if "/fixtures/" in url_clean:
            for row in rows:
                date_cell = row.find("td", class_="table-main__datetime")
                if not date_cell: continue
                spans = row.find_all("span")
                if len(spans) < 2: continue
                home, away = spans[0].get_text(strip=True), spans[1].get_text(strip=True)
                odds = []
                for cell in row.select("td.table-main__odds"):
                    odd = cell.get("data-odd") or (cell.find(attrs={"data-odd": True}).get("data-odd") if cell.find(attrs={"data-odd": True}) else None) or (cell.find("button").get_text(strip=True) if cell.find("button") else None) or cell.get_text(" ", strip=True)
                    odds.append(odd if odd else "-")
                odd1, oddx, odd2 = (odds[0] if len(odds)>0 else "-"), (odds[1] if len(odds)>1 else "-"), (odds[2] if len(odds)>2 else "-")
                all_data.append(["Fixture", league, date_cell.get_text(strip=True), home, away, "", odd1, oddx, odd2])
                mecz_count += 1

        elif "/results/" in url_clean:
            for row in rows:
                if not row.find("a", class_="in-match"): continue
                spans = row.find_all("span")
                if len(spans) < 2: continue
                home, away = spans[0].get_text(" ", strip=True), spans[1].get_text(" ", strip=True)
                score_cell = row.find("td", class_="h-text-center")
                score = score_cell.get_text(strip=True) if score_cell else ""
                odds = []
                for cell in row.select("td.table-main__odds"):
                    odd = cell.get("data-odd") or (cell.find(attrs={"data-odd": True}).get("data-odd") if cell.find(attrs={"data-odd": True}) else None) or (cell.find("button").get_text(strip=True) if cell.find("button") else None) or cell.get_text(" ", strip=True)
                    odds.append(odd if odd else "-")
                odd1, oddx, odd2 = (odds[0] if len(odds)>0 else "-"), (odds[1] if len(odds)>1 else "-"), (odds[2] if len(odds)>2 else "-")
                date_cell = row.find("td", class_=lambda x: x and "h-text-right" in x)
                date = date_cell.get_text(strip=True) if date_cell else ""
                all_data.append(["Result", league, date, home, away, score, odd1, oddx, odd2])
                mecz_count += 1
                
        status_msg = f"OK (Pobrano: {mecz_count} meczów)" + (" [Zadziałał Bypass 429]" if bypass_used else "")
        scrape_report.append(["BetExplorer", url_clean, status_msg if mecz_count > 0 else "BŁĄD: Znaleziono 0 meczów"])
    except Exception as e: scrape_report.append(["BetExplorer", url_clean, f"BŁĄD KRYTYCZNY: {e}"])

df = pd.DataFrame(all_data, columns=["Type", "League", "Date", "Home", "Away", "Score", "Odd1", "OddX", "Odd2"]).drop_duplicates()

if not df.empty:
    dates, times = zip(*[split_datetime(v) for v in df["Date"]])
    df["Date"], df["Time"] = dates, times
else: df["Time"] = pd.Series(dtype='object')

fixtures_df = df[df["Type"] == "Fixture"].copy()
results_df = df[df["Type"] == "Result"].copy()

if not fixtures_df.empty:
    fixtures_df['HasOdds'] = fixtures_df['Odd1'].astype(str).apply(lambda x: 1 if x.strip() not in ["", "-", "nan"] else 0)
    fixtures_df = fixtures_df.sort_values(by=["Date", "Time", "Home", "Away", "HasOdds"], ascending=[True, True, True, True, False]).drop_duplicates(subset=["Date", "Home", "Away"]).drop(columns=["HasOdds"])

if not results_df.empty:
    results_df['HasOdds'] = results_df['Odd1'].astype(str).apply(lambda x: 1 if x.strip() not in ["", "-", "nan"] else 0)
    results_df = results_df.sort_values(by=["Date", "Home", "Away", "HasOdds"], ascending=[False, True, True, False]).drop_duplicates(subset=["Date", "Home", "Away"]).drop(columns=["HasOdds"])

# ==========================================
# 2. POBIERANIE Z SOCCERSTATS 
# ==========================================
dane_soccerstats_baza = []
print("Rozpoczynam pobieranie z SoccerStats...")
try:
    if os.path.exists("ligi_soccerstats.xlsx"):
        urls_ss = pd.read_excel("ligi_soccerstats.xlsx")["URL"].dropna().tolist()
        skaner_ss = cloudscraper.create_scraper()
        for i_ss, url_ss in enumerate(urls_ss, start=1):
            url_ss_clean = str(url_ss).strip()
            time.sleep(random.uniform(1, 3))
            try:
                soup_ss = BeautifulSoup(skaner_ss.get(url_ss_clean, headers=headers, timeout=30).text, "html.parser")
                tabela_meczow = next((t for t in soup_ss.find_all("table") if "HT" in t.get_text() and "BTS" in t.get_text() and len(t.find_all("tr")) > 15), None)
                ss_count = 0
                if tabela_meczow:
                    for wiersz in tabela_meczow.find_all("tr"):
                        komorki = wiersz.find_all(["td", "th"])
                        if len(komorki) >= 6:
                            teksty = [k.get_text(" ", strip=True) for k in komorki]
                            wynik_index = next((idx for idx, val in enumerate(teksty) if ("-" in val or ":" in val) and any(c.isdigit() for c in val) and 1 <= idx <= 5), -1)
                            if wynik_index != -1:
                                wynik = teksty[wynik_index]
                                gospodarz = teksty[wynik_index - 1]
                                gosc = teksty[wynik_index + 1] if wynik_index + 1 < len(teksty) else ""
                                if "HOME" in gospodarz.upper(): continue
                                if gospodarz and gosc and gosc != gospodarz:
                                    statystyki = [s for s in teksty[wynik_index + 2:] if s.strip()] 
                                    ht = statystyki[0] if len(statystyki) > 0 else "-"
                                    wynik_czysty = wynik.replace("*", "").strip().replace(" ", "").replace("-", ":")
                                    ht_czysty = ht.replace("*", "").strip().replace(" ", "").replace("-", ":").replace("(", "").replace(")", "")
                                    g_gosp_1h, g_gosc_1h = "-", "-"
                                    if ":" in ht_czysty:
                                        try: p_1h = ht_czysty.split(":"); g_gosp_1h, g_gosc_1h = int(p_1h[0]), int(p_1h[1])
                                        except: pass
                                    dane_soccerstats_baza.append([gospodarz, gosc, wynik_czysty, g_gosp_1h, g_gosc_1h])
                                    ss_count += 1
                scrape_report.append(["SoccerStats", url_ss_clean, f"OK (Pobrano: {ss_count} wierszy)" if ss_count > 0 else "BŁĄD: 0 wierszy"])
            except Exception as e: scrape_report.append(["SoccerStats", url_ss_clean, f"BŁĄD: {str(e)}"])
        if dane_soccerstats_baza: ss_df = pd.DataFrame(dane_soccerstats_baza, columns=["Home", "Away", "Score", "Gole_Gosp_1H", "Gole_Gosc_1H"]).drop_duplicates(subset=["Home", "Away", "Score"])
        else: ss_df = pd.DataFrame()
except Exception: ss_df = pd.DataFrame()

# ==========================================
# 3. MAPOWANIE I SCALANIE DANYCH 
# ==========================================
if not ss_df.empty and not results_df.empty:
    ss_df["Home"] = ss_df["Home"].apply(lambda x: mapowanie_ss.get(x, x))
    ss_df["Away"] = ss_df["Away"].apply(lambda x: mapowanie_ss.get(x, x))
    results_df = pd.merge(results_df, ss_df, on=["Home", "Away", "Score"], how="left")

fd_df = fetch_football_data(scrape_report)
if not fd_df.empty and not results_df.empty:
    fd_df['HomeTeam'] = fd_df['HomeTeam'].astype(str).str.strip().replace(mapowanie_fd)
    fd_df['AwayTeam'] = fd_df['AwayTeam'].astype(str).str.strip().replace(mapowanie_fd)
    results_df['Date_str'] = pd.to_datetime(results_df['Date'], errors='coerce').astype(str)
    fd_df['Date_str'] = pd.to_datetime(fd_df['Date'], dayfirst=True, errors='coerce').astype(str)
    fd_df = fd_df.drop_duplicates(subset=['Date_str', 'HomeTeam', 'AwayTeam'], keep='last').rename(columns={'HomeTeam': 'Home', 'AwayTeam': 'Away'})
    results_df = pd.merge(results_df, fd_df.drop(columns=['Date']), how='left', on=['Date_str', 'Home', 'Away']).drop(columns=['Date_str'])

# ==========================================
# 4. ZŁOTA STRUKTURA DANYCH
# ==========================================
print("Czyszczenie bazy - Złota Struktura...")

golden_cols = {
    'Match_ID': 'Match_ID', 'Date': 'Date', 'Time': 'Time', 'League': 'League', 'Home': 'Home', 'Away': 'Away',
    'FTHG': 'FTHG', 'FTAG': 'FTAG', 'Total_Goals': 'Total_Goals', 'HTHG': 'HTHG', 'HTAG': 'HTAG',
    'HS': 'Shots_H', 'AS': 'Shots_A', 'HST': 'ShotsTarget_H', 'AST': 'ShotsTarget_A',
    'HC': 'Corners_H', 'AC': 'Corners_A', 
    'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2'
}

if not results_df.empty:
    results_df[['FTHG', 'FTAG']] = results_df['Score'].str.split(':', expand=True)
    results_df['FTHG'] = pd.to_numeric(results_df['FTHG'], errors='coerce')
    results_df['FTAG'] = pd.to_numeric(results_df['FTAG'], errors='coerce')
    results_df['Total_Goals'] = results_df['FTHG'] + results_df['FTAG']

    if 'HTHG' not in results_df.columns: results_df['HTHG'] = np.nan
    if 'HTAG' not in results_df.columns: results_df['HTAG'] = np.nan
    if 'Gole_Gosp_1H' in results_df.columns:
        results_df['HTHG'] = results_df['HTHG'].combine_first(pd.to_numeric(results_df['Gole_Gosp_1H'], errors='coerce'))
        results_df['HTAG'] = results_df['HTAG'].combine_first(pd.to_numeric(results_df['Gole_Gosc_1H'], errors='coerce'))

    fd_expected_cols = ['HS', 'AS', 'HST', 'AST', 'HC', 'AC']
    for col in fd_expected_cols:
        if col not in results_df.columns: results_df[col] = np.nan

    results_df['Date_str'] = pd.to_datetime(results_df['Date'], errors='coerce').dt.strftime('%Y%m%d').fillna('99999999')
    results_df['Match_ID'] = results_df['Date_str'] + "_" + results_df['Home'].str[:3].str.upper() + "_" + results_df['Away'].str[:3].str.upper()

if not fixtures_df.empty:
    fixtures_df['Date_str'] = pd.to_datetime(fixtures_df['Date'], errors='coerce').dt.strftime('%Y%m%d').fillna('99999999')
    fixtures_df['Match_ID'] = fixtures_df['Date_str'] + "_" + fixtures_df['Home'].str[:3].str.upper() + "_" + fixtures_df['Away'].str[:3].str.upper()
    fixtures_df['Termin'] = fixtures_df['Date'].apply(categorize_date)
    
    # ODRZUCAMY mecze, które nie mają jeszcze kursów (usuwamy śmieci z końca sezonu)
    fixtures_df = fixtures_df[~fixtures_df['Odd1'].astype(str).str.strip().isin(["", "-", "nan"])]

results_clean = results_df[list(golden_cols.keys())].rename(columns=golden_cols) if not results_df.empty else pd.DataFrame(columns=golden_cols.values())
fixtures_clean = fixtures_df[['Match_ID', 'Termin', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd1', 'OddX', 'Odd2']].rename(columns={'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2'}) if not fixtures_df.empty else pd.DataFrame(columns=['Match_ID', 'Termin', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd_1', 'Odd_X', 'Odd_2'])

# ==========================================
# 5. GENEROWANIE TABEL LIGOWYCH
# ==========================================
print("Generowanie inteligentnych tabel ligowych...")
results_clean['Date_Parsed'] = pd.to_datetime(results_clean['Date'], errors='coerce')
results_clean = results_clean.sort_values(by='Date_Parsed', ascending=False)
valid_matches = results_clean.dropna(subset=['FTHG', 'FTAG']).copy()

valid_matches['Base_League'] = valid_matches['League'].apply(get_base_league)

if not valid_matches.empty:
    valid_matches['FTHG'] = pd.to_numeric(valid_matches['FTHG'], errors='coerce').fillna(0).astype(int)
    valid_matches['FTAG'] = pd.to_numeric(valid_matches['FTAG'], errors='coerce').fillna(0).astype(int)
    valid_matches['Corners_H'] = pd.to_numeric(valid_matches['Corners_H'], errors='coerce')
    valid_matches['Corners_A'] = pd.to_numeric(valid_matches['Corners_A'], errors='coerce')
    valid_matches['Total_Corners'] = valid_matches['Corners_H'] + valid_matches['Corners_A']
    
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

team_tiers = {}
for lg in league_tables['League'].unique():
    lg_teams = league_tables[league_tables['League'] == lg].reset_index(drop=True)
    n_teams = len(lg_teams)
    if n_teams >= 3:
        t_size = max(1, n_teams // 3)
        for i, t in enumerate(lg_teams['Team']):
            if i < t_size: team_tiers[(lg, t)] = 'TOP'
            elif i >= n_teams - t_size: team_tiers[(lg, t)] = 'BOTTOM'
            else: team_tiers[(lg, t)] = 'MID'
    else:
        for t in lg_teams['Team']: team_tiers[(lg, t)] = 'MID'

team_ppg = {(r['League'], r['Team']): float(str(r['PPG']).replace(',', '.')) for _, r in league_tables.iterrows()}

# ==========================================================
# Inicjalizacja Głównej Listy Wyników (Dla Backtestera)
# ==========================================================
all_generated_predictions = []

# ŻELAZNY STANDARD NAGŁÓWKÓW PREDYKCYJNYCH (Teraz 10 Kolumn Startowych)
STANDARD_HEADERS = ["Match_ID", "Termin", "Data", "Godzina", "Liga", "Mecz", "Sugerowany Typ", "Szansa", "Kurs Szac.", "Argumentacja"]

# ==========================================
# 6a. ENGINE 1X PRO
# ==========================================
print("Uruchamiam Engine 1X Pro (Baza 30 gier)...")
predictions_1x = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    
    try:
        o1 = float(str(row['Odd_1']).replace(',', '.'))
        ox = float(str(row['Odd_X']).replace(',', '.'))
        buk_odd_1x = round(1 / ((1 / o1) + (1 / ox)), 2)
    except: continue

    h_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Home'] == home)]
    a_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Away'] == away)]

    h_current = h_all[h_all['League'] == league]
    h_past = h_all[h_all['League'] != league]
    a_current = a_all[a_all['League'] == league]
    a_past = a_all[a_all['League'] != league]

    h_window = h_current if len(h_current) >= 30 else pd.concat([h_current, h_past.head(30 - len(h_current))])
    a_window = a_current if len(a_current) >= 30 else pd.concat([a_current, a_past.head(30 - len(a_current))])

    if len(h_window) < 10 or len(a_window) < 10: continue

    h_1x_all_cnt = sum(h_all['FTHG'] >= h_all['FTAG'])
    h_1x_window_cnt = sum(h_window['FTHG'] >= h_window['FTAG'])
    h_losses = h_all[h_all['FTHG'] < h_all['FTAG']]
    l_top, l_mid, l_bot = 0, 0, 0
    for _, m in h_losses.iterrows():
        t = team_tiers.get((m['League'], m['Away']), 'MID')
        if t == 'TOP': l_top += 1
        elif t == 'MID': l_mid += 1
        elif t == 'BOTTOM': l_bot += 1

    a_2_all_cnt = sum(a_all['FTAG'] > a_all['FTHG'])
    a_2_window_cnt = sum(a_window['FTAG'] > a_window['FTHG'])
    a_wins = a_all[a_all['FTAG'] > a_all['FTHG']]
    w_top, w_mid, w_bot = 0, 0, 0
    for _, m in a_wins.iterrows():
        t = team_tiers.get((m['League'], m['Home']), 'MID')
        if t == 'TOP': w_top += 1
        elif t == 'MID': w_mid += 1
        elif t == 'BOTTOM': w_bot += 1

    a_fts_cnt = sum(a_all['FTAG'] == 0)
    a_fts_pct = round((a_fts_cnt / len(a_all)) * 100) if len(a_all) > 0 else 0

    h_window_shots = h_window[pd.to_numeric(h_window['ShotsTarget_H'], errors='coerce').notna()]
    if not h_window_shots.empty:
        avg_st = pd.to_numeric(h_window_shots['ShotsTarget_H']).mean()
        avg_g = pd.to_numeric(h_window_shots['FTHG']).mean()
        diff = (avg_st * 0.3) - avg_g
        h_proxy = "PECH (Ukryta Forma)" if diff > 0.4 else ("SZCZĘŚCIE" if diff < -0.4 else "STABILNY")
    else: h_proxy = "Brak Danych"

    h_unbeaten, _ = get_current_streaks(fixture_base, home)
    _, a_winless = get_current_streaks(fixture_base, away)

    prob_h = ((h_1x_all_cnt / len(h_all)) * 0.4) + ((h_1x_window_cnt / len(h_window)) * 0.6)
    prob_a = ((sum(a_all['FTHG'] >= a_all['FTAG']) / len(a_all)) * 0.4) + ((sum(a_window['FTHG'] >= a_window['FTAG']) / len(a_window)) * 0.6)
    
    avg_h_opp = sum([team_ppg.get((m['League'], m['Away']), 1.3) for _, m in h_window.iterrows()]) / len(h_window)
    avg_a_opp = sum([team_ppg.get((m['League'], m['Home']), 1.3) for _, m in a_window.iterrows()]) / len(a_window)
    
    final_prob = min(max(((prob_h + prob_a) / 2) + ((avg_h_opp - 1.3) * 0.08) + ((avg_a_opp - 1.3) * 0.08), 0.05), 0.95)
    fair_odd = round(1 / final_prob, 2)
    value_perc = round(((buk_odd_1x / fair_odd) - 1) * 100, 2)

    if final_prob >= 0.70:
        arg = f"Gospodarz stabilny dom ({h_1x_window_cnt}/{len(h_window)} w oknie 30 gier)."
        
        predictions_1x.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", 
            "1X", f"{round(final_prob*100)}%", str(fair_odd).replace('.', ','), arg,
            f"{value_perc}%", str(buk_odd_1x).replace('.', ','), 
            f"Baza: {len(h_window)} (bieżący: {len(h_current)})", f"{h_1x_all_cnt}/{len(h_all)}", f"{h_1x_window_cnt}/{len(h_window)}",
            f"{l_top} x", f"{l_mid} x", f"{l_bot} x",
            f"Baza: {len(a_window)} (bieżący: {len(a_current)})", f"{a_2_all_cnt}/{len(a_all)}", f"{a_2_window_cnt}/{len(a_window)}",
            f"{w_top} x", f"{w_mid} x", f"{w_bot} x",
            f"{a_fts_pct}%", h_unbeaten, a_winless, h_proxy
        ])
        
        all_generated_predictions.append([row['Match_ID'], row['Date'], home, away, "1X Pro", "1X", buk_odd_1x])

headers_1x = STANDARD_HEADERS + [
    "Value %", "Buk_Odd (Rynek)",
    "H_Probka_Meczow", "H_1X_Wszystkie", "H_1X_OknoKroczace", "H_Porażki_vs_TOP", "H_Porażki_vs_MID", "H_Porażki_vs_BOTTOM",
    "A_Probka_Meczow", "A_Wygrane_Wszystkie", "A_Wygrane_OknoKroczace", "A_Wygrane_vs_TOP", "A_Wygrane_vs_MID", "A_Wygrane_vs_BOTTOM",
    "A_FTS_Wyjazd_%", "H_Passa_Bez_Porażki", "A_Passa_Bez_Wygranej", "H_Proxy_xG_Status"
]
df_pred_1x = pd.DataFrame(predictions_1x, columns=headers_1x).sort_values(by="Szansa", ascending=False) if predictions_1x else pd.DataFrame(columns=headers_1x)

# ==========================================================
# 6b. ENGINE BETBUILDER PRO
# ==========================================================
print("Uruchamiam Engine BetBuilder Pro...")
predictions_builder = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    
    h_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == home))]
    a_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == away) | (valid_matches['Away'] == away))]
    if len(h_tot_all) < 10 or len(a_tot_all) < 10: continue

    h_tot_curr = h_tot_all[h_tot_all['League'] == league]
    h_tot_past = h_tot_all[h_tot_all['League'] != league]
    h_total_all = h_tot_curr if len(h_tot_curr) >= 30 else pd.concat([h_tot_curr, h_tot_past.head(30 - len(h_tot_curr))])

    a_tot_curr = a_tot_all[a_tot_all['League'] == league]
    a_tot_past = a_tot_all[a_tot_all['League'] != league]
    a_total_all = a_tot_curr if len(a_tot_curr) >= 30 else pd.concat([a_tot_curr, a_tot_past.head(30 - len(a_tot_curr))])

    h_dom_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Home'] == home)]
    h_dom_curr = h_dom_all[h_dom_all['League'] == league]
    h_dom_past = h_dom_all[h_dom_all['League'] != league]
    h_dom = h_dom_curr if len(h_dom_curr) >= 30 else pd.concat([h_dom_curr, h_dom_past.head(30 - len(h_dom_curr))])

    a_wyj_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Away'] == away)]
    a_wyj_curr = a_wyj_all[a_wyj_all['League'] == league]
    a_wyj_past = a_wyj_all[a_wyj_all['League'] != league]
    a_wyj = a_wyj_curr if len(a_wyj_curr) >= 30 else pd.concat([a_wyj_curr, a_wyj_past.head(30 - len(a_wyj_curr))])

    if len(h_dom) == 0 or len(a_wyj) == 0: continue

    h_dom['HT_Total'] = h_dom['HTHG'] + h_dom['HTAG']
    a_wyj['HT_Total'] = a_wyj['HTHG'] + a_wyj['HTAG']
    h_dom['2H_Total'] = h_dom['Total_Goals'] - h_dom['HT_Total']
    a_wyj['2H_Total'] = a_wyj['Total_Goals'] - a_wyj['HT_Total']
    
    h_total_all['Team_GF'] = np.where(h_total_all['Home'] == home, h_total_all['FTHG'], h_total_all['FTAG'])
    h_total_all['Team_GA'] = np.where(h_total_all['Home'] == home, h_total_all['FTAG'], h_total_all['FTHG'])
    a_total_all['Team_GF'] = np.where(a_total_all['Home'] == away, a_total_all['FTHG'], a_total_all['FTAG'])
    a_total_all['Team_GA'] = np.where(a_total_all['Home'] == away, a_total_all['FTAG'], a_total_all['FTHG'])

    max_h_scored_dom = h_dom['FTHG'].max()
    max_h_conceded_dom = h_dom['FTAG'].max()
    max_h_scored_all = h_total_all['Team_GF'].max()
    max_h_conceded_all = h_total_all['Team_GA'].max()
    
    max_a_scored_wyj = a_wyj['FTAG'].max()
    max_a_conceded_wyj = a_wyj['FTHG'].max()
    max_a_scored_all = a_total_all['Team_GF'].max()
    max_a_conceded_all = a_total_all['Team_GA'].max()
    
    builder_blocks_code = []
    block_probabilities = []
    block_estimated_odds = []
    
    h_dom_o05 = sum(h_dom['Total_Goals'] >= 1) / len(h_dom)
    a_wyj_o05 = sum(a_wyj['Total_Goals'] >= 1) / len(a_wyj)
    h_all_o05 = sum(h_total_all['Total_Goals'] >= 1) / len(h_total_all)
    a_all_o05 = sum(a_total_all['Total_Goals'] >= 1) / len(a_total_all)
    
    if h_dom_o05 == 1.0 and a_wyj_o05 == 1.0 and h_all_o05 == 1.0 and a_all_o05 == 1.0:
        builder_blocks_code.append("O0.5")
        block_probabilities.append(1.0)
        block_estimated_odds.append(1.04)

    for line in [4.5, 5.5, 6.5]:
        h_u = sum(h_dom['Total_Goals'] < line) / len(h_dom)
        a_u = sum(a_wyj['Total_Goals'] < line) / len(a_wyj)
        h_all_u = sum(h_total_all['Total_Goals'] < line) / len(h_total_all)
        a_all_u = sum(a_total_all['Total_Goals'] < line) / len(a_total_all)
        if h_u >= 0.95 and a_u >= 0.95 and h_all_u >= 0.94 and a_all_u >= 0.94:
            builder_blocks_code.append(f"U{line}")
            avg_prob = (h_u + a_u + h_all_u + a_all_u) / 4
            block_probabilities.append(avg_prob)
            block_estimated_odds.append(round(1 / (avg_prob * 0.91), 2))
            break

    h_ht = h_dom.dropna(subset=['HT_Total'])
    a_ht = a_wyj.dropna(subset=['HT_Total'])
    if len(h_ht) >= 3 and len(a_ht) >= 3:
        for line in [1.5, 2.5, 3.5, 4.5]:
            h_u_1h = sum(h_ht['HT_Total'] < line) / len(h_ht)
            a_u_1h = sum(a_ht['HT_Total'] < line) / len(a_ht)
            if h_u_1h >= 0.94 and a_u_1h >= 0.94:
                builder_blocks_code.append(f"HT_U{line}")
                avg_prob = (h_u_1h + a_u_1h) / 2
                block_probabilities.append(avg_prob)
                block_estimated_odds.append(round(1 / (avg_prob * 0.91), 2))
                break

    h_2h = h_dom.dropna(subset=['2H_Total'])
    a_2h = a_wyj.dropna(subset=['2H_Total'])
    if len(h_2h) >= 3 and len(a_2h) >= 3:
        for line in [2.5, 3.5, 4.5]:
            h_u_2h = sum(h_2h['2H_Total'] < line) / len(h_2h)
            a_u_2h = sum(a_2h['2H_Total'] < line) / len(a_2h)
            if h_u_2h >= 0.94 and a_u_2h >= 0.94:
                builder_blocks_code.append(f"2H_U{line}")
                avg_prob = (h_u_2h + a_u_2h) / 2
                block_probabilities.append(avg_prob)
                block_estimated_odds.append(round(1 / (avg_prob * 0.91), 2))
                break

    highest_h_scored = max(max_h_scored_dom, max_h_scored_all)
    for line in [3.5, 4.5, 5.5]:
        if line > highest_h_scored:
            h_u_prob = sum(h_dom['FTHG'] < line) / len(h_dom)
            h_all_u_prob = sum(h_total_all['Team_GF'] < line) / len(h_total_all)
            if h_u_prob >= 0.95 and h_all_u_prob >= 0.94:
                builder_blocks_code.append(f"HU{line}")
                avg_prob = (h_u_prob + h_all_u_prob) / 2
                block_probabilities.append(avg_prob)
                block_estimated_odds.append(round(1 / (avg_prob * 0.91), 2))
                break

    highest_a_scored = max(max_a_scored_wyj, max_a_scored_all)
    for line in [3.5, 4.5]:
        if line > highest_a_scored:
            a_u_prob = sum(a_wyj['FTAG'] < line) / len(a_wyj)
            a_all_u_prob = sum(a_total_all['Team_GF'] < line) / len(a_total_all)
            if a_u_prob >= 0.95 and a_all_u_prob >= 0.94:
                builder_blocks_code.append(f"AU{line}")
                avg_prob = (a_u_prob + a_all_u_prob) / 2
                block_probabilities.append(avg_prob)
                block_estimated_odds.append(round(1 / (avg_prob * 0.91), 2))
                break

    if len(builder_blocks_code) >= 3:
        final_builder_safety = round(np.prod(block_probabilities) * 100, 1)
        if final_builder_safety >= 95.0:
            sugerowany_kupon = "+".join(builder_blocks_code)
            estimated_bb_odd = round((1.0 + sum([(o - 1.0) * 0.52 for o in block_estimated_odds])) * 0.95, 2)
            if estimated_bb_odd < 1.05: estimated_bb_odd = 1.05
            uzasadnienie = f"Stabilny zestaw w kroczącym oknie."

            predictions_builder.append([
                row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", 
                sugerowany_kupon, f"{final_builder_safety}%", str(estimated_bb_odd).replace('.', ','), uzasadnienie,
                f"Dom: {len(h_dom)} (Suma: {len(h_total_all)})", 
                f"Wyjazd: {len(a_wyj)} (Suma: {len(a_total_all)})",
                int(max_h_scored_dom), int(max_h_conceded_dom), int(max_h_scored_all), int(max_h_conceded_all),
                int(max_a_scored_wyj), int(max_a_conceded_wyj), int(max_a_scored_all), int(max_a_conceded_all)
            ])
            
            all_generated_predictions.append([row['Match_ID'], row['Date'], home, away, "BetBuilder Pro", sugerowany_kupon, estimated_bb_odd])

headers_builder = STANDARD_HEADERS + [
    "H_Probka", "A_Probka",
    "H_Max_Strz_Dom", "H_Max_Stra_Dom", "H_Max_Strz_Ogół", "H_Max_Stra_Ogół",
    "A_Max_Strz_Wyj", "A_Max_Stra_Wyj", "A_Max_Strz_Ogół", "A_Max_Stra_Ogół"
]
df_pred_builder = pd.DataFrame(predictions_builder, columns=headers_builder).sort_values(by="Szansa", ascending=False) if predictions_builder else pd.DataFrame(columns=headers_builder)

# ==========================================================
# 6c. ENGINE MULTIGOL (Przedziały 1-5 i 1-6) z Regresją
# ==========================================================
print("Uruchamiam Engine Multigol (Detektor 1-5 / 1-6 z Regresją)...")
predictions_multigol = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    
    h_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == home))]
    a_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == away) | (valid_matches['Away'] == away))]
    if len(h_tot_all) < 10 or len(a_tot_all) < 10: continue

    h_dom = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Home'] == home)]
    a_wyj = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Away'] == away)]
    if len(h_dom) == 0 or len(a_wyj) == 0: continue

    h_last_goals = get_last_match_goals(fixture_base, home)
    a_last_goals = get_last_match_goals(fixture_base, away)
    
    has_anomaly = False
    anom_text = ""
    if h_last_goals == 0 or h_last_goals > 5:
        has_anomaly = True
        anom_text += f"Ostatni mecz Gosp to anomalia ({h_last_goals} goli). "
    if a_last_goals == 0 or a_last_goals > 5:
        has_anomaly = True
        anom_text += f"Ostatni mecz Gościa to anomalia ({a_last_goals} goli). "

    if not has_anomaly: continue

    h_1_5_dom = sum((h_dom['Total_Goals'] >= 1) & (h_dom['Total_Goals'] <= 5)) / len(h_dom)
    h_1_5_all = sum((h_tot_all['Total_Goals'] >= 1) & (h_tot_all['Total_Goals'] <= 5)) / len(h_tot_all)
    a_1_5_wyj = sum((a_wyj['Total_Goals'] >= 1) & (a_wyj['Total_Goals'] <= 5)) / len(a_wyj)
    a_1_5_all = sum((a_tot_all['Total_Goals'] >= 1) & (a_tot_all['Total_Goals'] <= 5)) / len(a_tot_all)
    
    prob_1_5 = (h_1_5_dom + h_1_5_all + a_1_5_wyj + a_1_5_all) / 4

    h_1_6_dom = sum((h_dom['Total_Goals'] >= 1) & (h_dom['Total_Goals'] <= 6)) / len(h_dom)
    h_1_6_all = sum((h_tot_all['Total_Goals'] >= 1) & (h_tot_all['Total_Goals'] <= 6)) / len(h_tot_all)
    a_1_6_wyj = sum((a_wyj['Total_Goals'] >= 1) & (a_wyj['Total_Goals'] <= 6)) / len(a_wyj)
    a_1_6_all = sum((a_tot_all['Total_Goals'] >= 1) & (a_tot_all['Total_Goals'] <= 6)) / len(a_tot_all)
    
    prob_1_6 = (h_1_6_dom + h_1_6_all + a_1_6_wyj + a_1_6_all) / 4

    if prob_1_5 >= 0.90 or prob_1_6 >= 0.90:
        if prob_1_5 >= 0.90:
            typ_kod, pewnosc = "MG_1-5", prob_1_5
        else:
            typ_kod, pewnosc = "MG_1-6", prob_1_6
            
        est_odd = round(1.0 + (((1/pewnosc) - 1.0) / 1.5), 2)
        uzasadnienie = anom_text + "Regresja do średniej."

        predictions_multigol.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}",
            typ_kod, f"{round(pewnosc*100, 1)}%", str(est_odd).replace('.', ','), uzasadnienie,
            f"D: {len(h_dom)}", f"W: {len(a_wyj)}", h_last_goals, a_last_goals
        ])
        
        all_generated_predictions.append([row['Match_ID'], row['Date'], home, away, "Multigol", typ_kod, est_odd])

headers_multigol = STANDARD_HEADERS + ["H_Próbka", "A_Próbka", "Ostatnie_Gole_H", "Ostatnie_Gole_A"]
df_pred_multigol = pd.DataFrame(predictions_multigol, columns=headers_multigol).sort_values(by="Szansa", ascending=False) if predictions_multigol else pd.DataFrame(columns=headers_multigol)

# ==========================================================
# 6d. ENGINE CORNERS PRO (Undery Rzutów Rożnych)
# ==========================================================
print("Uruchamiam Engine Corners Pro (Undery Rzutów Rożnych)...")
predictions_corners = []

valid_corners = valid_matches.dropna(subset=['Corners_H', 'Corners_A']).copy()

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)

    h_tot_all_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & ((valid_corners['Home'] == home) | (valid_corners['Away'] == home))].copy()
    a_tot_all_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & ((valid_corners['Home'] == away) | (valid_corners['Away'] == away))].copy()

    if len(h_tot_all_c) < 8 or len(a_tot_all_c) < 8: continue

    h_dom_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & (valid_corners['Home'] == home)].copy()
    a_wyj_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & (valid_corners['Away'] == away)].copy()

    if len(h_dom_c) < 3 or len(a_wyj_c) < 3: continue

    h_tot_all_c['Team_C_For'] = np.where(h_tot_all_c['Home'] == home, h_tot_all_c['Corners_H'], h_tot_all_c['Corners_A'])
    a_tot_all_c['Team_C_For'] = np.where(a_tot_all_c['Home'] == away, a_tot_all_c['Corners_H'], a_tot_all_c['Corners_A'])

    max_match_h = h_dom_c['Total_Corners'].max()
    max_match_a = a_wyj_c['Total_Corners'].max()
    max_match_all = max(h_tot_all_c['Total_Corners'].max(), a_tot_all_c['Total_Corners'].max())

    max_team_h_dom = h_dom_c['Corners_H'].max()
    max_team_h_all = h_tot_all_c['Team_C_For'].max()

    max_team_a_wyj = a_wyj_c['Corners_A'].max()
    max_team_a_all = a_tot_all_c['Team_C_For'].max()

    c_blocks_code = []
    c_probs = []
    c_odds = []

    highest_match = max(max_match_h, max_match_a, max_match_all)
    for line in [8.5, 9.5, 10.5, 11.5, 12.5, 13.5, 14.5]:
        if line > highest_match - 2:
            p_hd = sum(h_dom_c['Total_Corners'] < line) / len(h_dom_c)
            p_aw = sum(a_wyj_c['Total_Corners'] < line) / len(a_wyj_c)
            p_ha = sum(h_tot_all_c['Total_Corners'] < line) / len(h_tot_all_c)
            p_aa = sum(a_tot_all_c['Total_Corners'] < line) / len(a_tot_all_c)
            if p_hd >= 0.90 and p_aw >= 0.90 and p_ha >= 0.90 and p_aa >= 0.90:
                c_blocks_code.append(f"C_U{line}")
                avg_p = (p_hd + p_aw + p_ha + p_aa) / 4
                c_probs.append(avg_p)
                c_odds.append(round(1 / (avg_p * 0.90), 2))
                break

    highest_h = max(max_team_h_dom, max_team_h_all)
    for line in [4.5, 5.5, 6.5, 7.5, 8.5]:
        if line > highest_h - 1:
            p_hd = sum(h_dom_c['Corners_H'] < line) / len(h_dom_c)
            p_ha = sum(h_tot_all_c['Team_C_For'] < line) / len(h_tot_all_c)
            if p_hd >= 0.92 and p_ha >= 0.92:
                c_blocks_code.append(f"HC_U{line}")
                avg_p = (p_hd + p_ha) / 2
                c_probs.append(avg_p)
                c_odds.append(round(1 / (avg_p * 0.90), 2))
                break

    highest_a = max(max_team_a_wyj, max_team_a_all)
    for line in [3.5, 4.5, 5.5, 6.5, 7.5]:
        if line > highest_a - 1:
            p_aw = sum(a_wyj_c['Corners_A'] < line) / len(a_wyj_c)
            p_aa = sum(a_tot_all_c['Team_C_For'] < line) / len(a_tot_all_c)
            if p_aw >= 0.92 and p_aa >= 0.92:
                c_blocks_code.append(f"AC_U{line}")
                avg_p = (p_aw + p_aa) / 2
                c_probs.append(avg_p)
                c_odds.append(round(1 / (avg_p * 0.90), 2))
                break

    if len(c_blocks_code) >= 1:
        est_odd = round((1.0 + sum([(o - 1.0) * 0.60 for o in c_odds])) * 0.95, 2) if len(c_blocks_code) > 1 else c_odds[0]
        if est_odd < 1.05: est_odd = 1.05
        final_safety = round(np.mean(c_probs) * 100, 1)
        typ_kod = "+".join(c_blocks_code)
        uzasadnienie = "Stabilny zakres linii rożnych."

        predictions_corners.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}",
            typ_kod, f"{final_safety}%", str(est_odd).replace('.', ','), uzasadnienie,
            len(h_tot_all_c), len(a_tot_all_c),
            int(max_match_all), int(highest_h), int(highest_a)
        ])
        
        all_generated_predictions.append([row['Match_ID'], row['Date'], home, away, "Corners Pro", typ_kod, est_odd])

headers_corners = STANDARD_HEADERS + ["H_Próbka", "A_Próbka", "Max_Mecz", "Max_Gosp", "Max_Gość"]
df_pred_corners = pd.DataFrame(predictions_corners, columns=headers_corners).sort_values(by="Szansa", ascending=False) if predictions_corners else pd.DataFrame(columns=headers_corners)

# ==========================================================
# 6e. ENGINE SHOTS PRO (Gospodarz: Strzały i Strzały Celne)
# ==========================================================
print("Uruchamiam Engine Shots Pro (Dominacja Gospodarzy)...")
predictions_shots = []

valid_shots = valid_matches.dropna(subset=['Shots_H', 'Shots_A', 'ShotsTarget_H', 'ShotsTarget_A']).copy()

if not valid_shots.empty:
    valid_shots['Shots_H'] = pd.to_numeric(valid_shots['Shots_H'], errors='coerce')
    valid_shots['Shots_A'] = pd.to_numeric(valid_shots['Shots_A'], errors='coerce')
    valid_shots['ShotsTarget_H'] = pd.to_numeric(valid_shots['ShotsTarget_H'], errors='coerce')
    valid_shots['ShotsTarget_A'] = pd.to_numeric(valid_shots['ShotsTarget_A'], errors='coerce')
    valid_shots = valid_shots.dropna(subset=['Shots_H', 'Shots_A', 'ShotsTarget_H', 'ShotsTarget_A'])

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)

    h_dom_s = valid_shots[(valid_shots['Base_League'] == fixture_base) & (valid_shots['Home'] == home)].copy()
    a_wyj_s = valid_shots[(valid_shots['Base_League'] == fixture_base) & (valid_shots['Away'] == away)].copy()

    if len(h_dom_s) < 2 or len(a_wyj_s) < 2: continue

    h_dom_s['S_Diff'] = h_dom_s['Shots_H'] - h_dom_s['Shots_A']
    a_wyj_s['S_Diff'] = a_wyj_s['Shots_A'] - a_wyj_s['Shots_H']

    h_s_win_dom = sum(h_dom_s['S_Diff'] > 0)
    a_s_lose_wyj = sum(a_wyj_s['S_Diff'] < 0)

    h_dom_s['ST_Diff'] = h_dom_s['ShotsTarget_H'] - h_dom_s['ShotsTarget_A']
    a_wyj_s['ST_Diff'] = a_wyj_s['ShotsTarget_A'] - a_wyj_s['ShotsTarget_H']

    h_st_win_dom = sum(h_dom_s['ST_Diff'] > 0)
    a_st_lose_wyj = sum(a_wyj_s['ST_Diff'] < 0)

    waga_dom, waga_wyj = 4.0, 1.0
    suma_wag = waga_dom + waga_wyj

    prob_h_s = ((h_s_win_dom / len(h_dom_s)) * waga_dom + (a_s_lose_wyj / len(a_wyj_s)) * waga_wyj) / suma_wag
    prob_h_st = ((h_st_win_dom / len(h_dom_s)) * waga_dom + (a_st_lose_wyj / len(a_wyj_s)) * waga_wyj) / suma_wag

    raw_odd_s = (1 / prob_h_s) if prob_h_s > 0 else 99.0
    est_odd_s = round(1.0 + ((raw_odd_s - 1.0) / 1.5), 2) if raw_odd_s > 1.0 else 1.01

    raw_odd_st = (1 / prob_h_st) if prob_h_st > 0 else 99.0
    est_odd_st = round(1.0 + ((raw_odd_st - 1.0) / 1.5), 2) if raw_odd_st > 1.0 else 1.01

    avg_diff_s_dom = round(h_dom_s['S_Diff'].mean(), 1)
    avg_diff_s_wyj = round(a_wyj_s['S_Diff'].mean(), 1)
    avg_diff_st_dom = round(h_dom_s['ST_Diff'].mean(), 1)
    avg_diff_st_wyj = round(a_wyj_s['ST_Diff'].mean(), 1)
    
    if prob_h_s > 0.80:
        uzasadnienie_s = f"Przewaga w strzałach: Gosp wygrywa w {h_s_win_dom}/{len(h_dom_s)} (Śr. dom: +{avg_diff_s_dom})"
        predictions_shots.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}",
            "S_1", f"{round(prob_h_s * 100, 1)}%", str(est_odd_s).replace('.', ','), uzasadnienie_s,
            "Strzały Ogółem", f"{h_s_win_dom}/{len(h_dom_s)}", str(avg_diff_s_dom).replace('.', ','), f"{a_s_lose_wyj}/{len(a_wyj_s)}", str(avg_diff_s_wyj).replace('.', ',')
        ])
        all_generated_predictions.append([row['Match_ID'], row['Date'], home, away, "Shots Pro", "S_1", est_odd_s])
        
    if prob_h_st > 0.80:
        uzasadnienie_st = f"Przewaga w celnych: Gosp wygrywa w {h_st_win_dom}/{len(h_dom_s)} (Śr. dom: +{avg_diff_st_dom})"
        predictions_shots.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}",
            "ST_1", f"{round(prob_h_st * 100, 1)}%", str(est_odd_st).replace('.', ','), uzasadnienie_st,
            "Strzały Celne", f"{h_st_win_dom}/{len(h_dom_s)}", str(avg_diff_st_dom).replace('.', ','), f"{a_st_lose_wyj}/{len(a_wyj_s)}", str(avg_diff_st_wyj).replace('.', ',')
        ])
        all_generated_predictions.append([row['Match_ID'], row['Date'], home, away, "Shots Pro", "ST_1", est_odd_st])

headers_shots = STANDARD_HEADERS + ["Typ Statystyki", "Gosp Wygrywa Dom", "Śr Różnica D", "Gość Przegrywa Wyj", "Śr Różnica W"]
df_pred_shots = pd.DataFrame(predictions_shots, columns=headers_shots).sort_values(by="Szansa", ascending=False) if predictions_shots else pd.DataFrame(columns=headers_shots)

# ==========================================================
# 6f. ENGINE ZIMNY PRYSZNIC (Test Motywacji)
# ==========================================================
print("Uruchamiam Engine Zimny Prysznic (Reakcja TOP drużyn na wpadkę)...")
predictions_coldshower = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    
    h_tier = team_tiers.get((league, home), 'MID')
    if h_tier != 'TOP': continue 
    
    h_past = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == home))]
    if len(h_past) == 0: continue
    
    last_m = h_past.iloc[0] 
    
    if last_m['Away'] == home and last_m['FTHG'] >= last_m['FTAG']:
        opp_tier = team_tiers.get((last_m['League'], last_m['Home']), 'TOP')
        if opp_tier in ['BOTTOM', 'MID']:
            prob_bounce = 0.85 
            est_odd = round(1.0 + (((1/prob_bounce) - 1.0) / 1.5), 2)
            uzasadnienie = "Reakcja TOP drużyny na potknięcie z dołem tabeli."
            
            predictions_coldshower.append([
                row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}",
                "1", f"{round(prob_bounce*100)}%", str(est_odd).replace('.', ','), uzasadnienie,
                f"{last_m['Home']} {last_m['FTHG']}:{last_m['FTAG']} {home}"
            ])
            all_generated_predictions.append([row['Match_ID'], row['Date'], home, away, "Cold Shower", "1", est_odd])

headers_coldshower = STANDARD_HEADERS + ["Wpadka (Dowód)"]
df_pred_coldshower = pd.DataFrame(predictions_coldshower, columns=headers_coldshower).sort_values(by="Szansa", ascending=False) if predictions_coldshower else pd.DataFrame(columns=headers_coldshower)

# ==========================================================
# 6g. ENGINE UKRYTA FORMA (Proxy xG / Wariancja)
# ==========================================================
print("Uruchamiam Engine Ukryta Forma (Proxy xG ze Strzałów Celnych)...")
predictions_hiddenform = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    
    for team, is_home in [(home, True), (away, False)]:
        t_past = valid_shots[(valid_shots['Base_League'] == fixture_base) & ((valid_shots['Home'] == team) | (valid_shots['Away'] == team))]
        if len(t_past) < 3: continue
        
        last_3 = t_past.head(3)
        st_for = np.where(last_3['Home'] == team, last_3['ShotsTarget_H'], last_3['ShotsTarget_A']).sum()
        st_agg = np.where(last_3['Home'] == team, last_3['ShotsTarget_A'], last_3['ShotsTarget_H']).sum()
        g_for = np.where(last_3['Home'] == team, last_3['FTHG'], last_3['FTAG']).sum()
        pts = 0
        for _, m in last_3.iterrows():
            g_f = m['FTHG'] if m['Home'] == team else m['FTAG']
            g_a = m['FTAG'] if m['Home'] == team else m['FTHG']
            if g_f > g_a: pts += 3
            elif g_f == g_a: pts += 1

        if st_for >= (st_agg * 1.5) and st_for >= 15:
            if pts <= 4 and g_for <= 3:
                typ_kod = "1X" if is_home else "X2"
                prob = 0.80
                est_odd = round(1.0 + (((1/prob) - 1.0) / 1.5), 2)
                uzasadnienie = f"Wysokie xG ze strzałów celnych ({int(st_for)}) bez poparcia w wynikach ({int(g_for)} goli)."
                
                predictions_hiddenform.append([
                    row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}",
                    typ_kod, f"{round(prob*100)}%", str(est_odd).replace('.', ','), uzasadnienie,
                    f"Celne Zespół: {int(st_for)} | Rywale: {int(st_agg)}", f"{int(g_for)} goli w 3 meczach"
                ])
                all_generated_predictions.append([row['Match_ID'], row['Date'], home, away, "Hidden Form", typ_kod, est_odd])

headers_hiddenform = STANDARD_HEADERS + ["Strzały Celne (Ostatnie 3)", "Brak Skuteczności"]
df_pred_hiddenform = pd.DataFrame(predictions_hiddenform, columns=headers_hiddenform).sort_values(by="Szansa", ascending=False) if predictions_hiddenform else pd.DataFrame(columns=headers_hiddenform)

# ==========================================================
# 6h. ENGINE ANOMALIE ROŻNYCH (Pęknięte serie rożnych)
# ==========================================================
print("Uruchamiam Engine Pęknięte Serie Rożnych...")
predictions_corner_anomalies = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    
    for team, is_home in [(home, True), (away, False)]:
        t_past = valid_corners[(valid_corners['Base_League'] == fixture_base) & ((valid_corners['Home'] == team) | (valid_corners['Away'] == team))].copy()
        if len(t_past) < 8: continue
        
        t_past['C_For'] = np.where(t_past['Home'] == team, t_past['Corners_H'], t_past['Corners_A'])
        season_avg = t_past['C_For'].mean()
        
        last_2 = t_past.head(2)
        last_2_avg = last_2['C_For'].mean()
        
        if season_avg >= 5.5 and last_2_avg <= 3.0:
            typ_kod = "HC_O4.5" if is_home else "AC_O4.5"
            prob = 0.82
            est_odd = round(1.0 + (((1/prob) - 1.0) / 1.5), 2)
            uzasadnienie = f"Pęknięta seria rożnych. Średnia z sezonu: {round(season_avg, 2)}, ostatnio: {round(last_2_avg, 2)}"
            
            predictions_corner_anomalies.append([
                row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}",
                typ_kod, f"{round(prob*100)}%", str(est_odd).replace('.', ','), uzasadnienie,
                str(round(season_avg, 2)).replace('.', ','), str(round(last_2_avg, 2)).replace('.', ',')
            ])
            all_generated_predictions.append([row['Match_ID'], row['Date'], home, away, "Corner Anomalies", typ_kod, est_odd])

headers_corner_anomalies = STANDARD_HEADERS + ["Średnia Sezon", "Średnia 2 Ostatnie"]
df_pred_corner_anomalies = pd.DataFrame(predictions_corner_anomalies, columns=headers_corner_anomalies).sort_values(by="Szansa", ascending=False) if predictions_corner_anomalies else pd.DataFrame(columns=headers_corner_anomalies)

# ==========================================================
# 6i. ENGINE ANOMALIE BRAMKOWE (Regresja do średniej Goli)
# ==========================================================
print("Uruchamiam Engine Detektor Anomalii Bramkowych...")
predictions_goal_anomalies = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    
    t_past = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == away))]
    if len(t_past) < 10: continue
    
    season_avg = t_past['Total_Goals'].mean()
    last_2 = t_past.head(2)
    last_2_avg = last_2['Total_Goals'].mean()
    
    if season_avg <= 2.8 and last_2_avg >= 4.5:
        typ_kod = "U3.5"
        prob = 0.85
        est_odd = round(1.0 + (((1/prob) - 1.0) / 1.5), 2)
        uzasadnienie = f"Anomalia overowa. Sezon: {round(season_avg, 2)}, ost. 2 mecze: {round(last_2_avg, 2)}"
        predictions_goal_anomalies.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", 
            typ_kod, f"{round(prob*100)}%", str(est_odd).replace('.', ','), uzasadnienie,
            str(round(season_avg, 2)).replace('.', ','), str(round(last_2_avg, 2)).replace('.', ',')
        ])
        all_generated_predictions.append([row['Match_ID'], row['Date'], home, away, "Goal Anomalies", typ_kod, est_odd])
        
    elif season_avg >= 2.5 and last_2_avg <= 0.5:
        typ_kod = "O1.5"
        prob = 0.85
        est_odd = round(1.0 + (((1/prob) - 1.0) / 1.5), 2)
        uzasadnienie = f"Anomalia underowa. Sezon: {round(season_avg, 2)}, ost. 2 mecze: {round(last_2_avg, 2)}"
        predictions_goal_anomalies.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", 
            typ_kod, f"{round(prob*100)}%", str(est_odd).replace('.', ','), uzasadnienie,
            str(round(season_avg, 2)).replace('.', ','), str(round(last_2_avg, 2)).replace('.', ',')
        ])
        all_generated_predictions.append([row['Match_ID'], row['Date'], home, away, "Goal Anomalies", typ_kod, est_odd])

headers_goal_anomalies = STANDARD_HEADERS + ["Średnia Sezon", "Średnia 2 Ostatnie"]
df_pred_goal_anomalies = pd.DataFrame(predictions_goal_anomalies, columns=headers_goal_anomalies).sort_values(by="Szansa", ascending=False) if predictions_goal_anomalies else pd.DataFrame(columns=headers_goal_anomalies)


# ==========================================================
# 7. SYSTEM ŚLEDZENIA SKUTECZNOŚCI I YIELDU (BACKTESTER)
# ==========================================================
print("Inicjalizacja Modułu Backtestingu (Śledzenie Skuteczności)...")

scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file("credentials.json", scopes=scope) if os.path.exists("credentials.json") else Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

# Dodana kolumna "Akceptacja" na początku struktury backtestera
cols_historia = ["Match_ID", "Akceptacja", "Date", "Home", "Away", "Engine", "Bet_Type", "Odds", "Status", "Profit", "Yield_Wplyw"]

try:
    ws_historia = spreadsheet.worksheet("Historia_Typow")
    historia_dane = ws_historia.get_all_values()
    if len(historia_dane) > 0:
        df_historia = pd.DataFrame(historia_dane[1:], columns=historia_dane[0])
    else:
        df_historia = pd.DataFrame(columns=cols_historia)
except gspread.exceptions.WorksheetNotFound:
    spreadsheet.add_worksheet(title="Historia_Typow", rows=10000, cols=16)
    ws_historia = spreadsheet.worksheet("Historia_Typow")
    df_historia = pd.DataFrame(columns=cols_historia)

# Uzupełnienie braku kolumny "Akceptacja" w starych arkuszach, żeby uniknąć błędu łączenia
if "Akceptacja" not in df_historia.columns:
    df_historia.insert(1, "Akceptacja", "-")

# Dodawanie NOWYCH typów z ominięciem duplikatów
if all_generated_predictions:
    nowe_typy_df = pd.DataFrame(all_generated_predictions, columns=["Match_ID", "Date", "Home", "Away", "Engine", "Bet_Type", "Odds"])
    nowe_typy_df.insert(1, "Akceptacja", "-") # Oczekuje na ludzką decyzję ("-")
    nowe_typy_df["Status"] = "W OCZEKIWANIU"
    nowe_typy_df["Profit"] = "-"
    nowe_typy_df["Yield_Wplyw"] = "-"
    
    if not df_historia.empty:
        df_historia['Unikalny_Klucz'] = df_historia['Match_ID'] + df_historia['Engine'] + df_historia['Bet_Type']
        nowe_typy_df['Unikalny_Klucz'] = nowe_typy_df['Match_ID'] + nowe_typy_df['Engine'] + nowe_typy_df['Bet_Type']
        
        do_dodania = nowe_typy_df[~nowe_typy_df['Unikalny_Klucz'].isin(df_historia['Unikalny_Klucz'])].copy()
        do_dodania = do_dodania.drop(columns=['Unikalny_Klucz'])
        df_historia = df_historia.drop(columns=['Unikalny_Klucz'])
    else:
        do_dodania = nowe_typy_df.copy()
        
    df_historia = pd.concat([df_historia, do_dodania], ignore_index=True)

# Ewaluator Typów Giełdowych 
def evaluate_bet(bet_type, row_data):
    bet = str(bet_type).upper().strip()
    
    hg = pd.to_numeric(row_data.get('FTHG', np.nan))
    ag = pd.to_numeric(row_data.get('FTAG', np.nan))
    tg = pd.to_numeric(row_data.get('Total_Goals', np.nan))
    ht_hg = pd.to_numeric(row_data.get('HTHG', np.nan))
    ht_ag = pd.to_numeric(row_data.get('HTAG', np.nan))
    hc = pd.to_numeric(row_data.get('Corners_H', np.nan))
    ac = pd.to_numeric(row_data.get('Corners_A', np.nan))
    hs = pd.to_numeric(row_data.get('Shots_H', np.nan))
    away_s = pd.to_numeric(row_data.get('Shots_A', np.nan))
    hst = pd.to_numeric(row_data.get('ShotsTarget_H', np.nan))
    ast = pd.to_numeric(row_data.get('ShotsTarget_A', np.nan))

    if pd.isna(hg) or pd.isna(ag): return "W OCZEKIWANIU"

    if "+" in bet:
        parts = bet.split("+")
        results = [evaluate_bet(p.strip(), row_data) for p in parts]
        if "W OCZEKIWANIU" in results: return "W OCZEKIWANIU"
        if "PRZEGRANA" in results: return "PRZEGRANA"
        if "DO RĘCZNEJ KONTROLI" in results: return "DO RĘCZNEJ KONTROLI"
        return "WYGRANA"

    if bet == "1": return "WYGRANA" if hg > ag else "PRZEGRANA"
    if bet == "X": return "WYGRANA" if hg == ag else "PRZEGRANA"
    if bet == "2": return "WYGRANA" if hg < ag else "PRZEGRANA"
    if bet == "1X": return "WYGRANA" if hg >= ag else "PRZEGRANA"
    if bet == "X2": return "WYGRANA" if hg <= ag else "PRZEGRANA"
    if bet == "12": return "WYGRANA" if hg != ag else "PRZEGRANA"
    
    if bet.startswith("O") and pd.notna(tg) and "_" not in bet: return "WYGRANA" if tg > float(bet[1:]) else "PRZEGRANA"
    if bet.startswith("U") and pd.notna(tg) and "_" not in bet: return "WYGRANA" if tg < float(bet[1:]) else "PRZEGRANA"

    if bet.startswith("HT_U") and pd.notna(ht_hg) and pd.notna(ht_ag): return "WYGRANA" if (ht_hg + ht_ag) < float(bet[4:]) else "PRZEGRANA"
    if bet.startswith("2H_U") and pd.notna(tg) and pd.notna(ht_hg) and pd.notna(ht_ag): return "WYGRANA" if (tg - (ht_hg + ht_ag)) < float(bet[4:]) else "PRZEGRANA"

    if bet.startswith("HU") and pd.notna(hg): return "WYGRANA" if hg < float(bet[2:]) else "PRZEGRANA"
    if bet.startswith("AU") and pd.notna(ag): return "WYGRANA" if ag < float(bet[2:]) else "PRZEGRANA"

    if bet.startswith("MG_"):
        try:
            low, high = map(int, bet[3:].split("-"))
            return "WYGRANA" if low <= tg <= high else "PRZEGRANA"
        except: pass

    if pd.notna(hc) and pd.notna(ac):
        tc = hc + ac
        if bet.startswith("C_U"): return "WYGRANA" if tc < float(bet[3:]) else "PRZEGRANA"
        if bet.startswith("C_O"): return "WYGRANA" if tc > float(bet[3:]) else "PRZEGRANA"
        if bet.startswith("HC_U"): return "WYGRANA" if hc < float(bet[4:]) else "PRZEGRANA"
        if bet.startswith("AC_U"): return "WYGRANA" if ac < float(bet[4:]) else "PRZEGRANA"
        if bet.startswith("HC_O"): return "WYGRANA" if hc > float(bet[4:]) else "PRZEGRANA"
        if bet.startswith("AC_O"): return "WYGRANA" if ac > float(bet[4:]) else "PRZEGRANA"

    if pd.notna(hs) and pd.notna(away_s):
        if bet == "S_1": return "WYGRANA" if hs > away_s else "PRZEGRANA"
        if bet == "S_2": return "WYGRANA" if hs < away_s else "PRZEGRANA"
    
    if pd.notna(hst) and pd.notna(ast):
        if bet == "ST_1": return "WYGRANA" if hst > ast else "PRZEGRANA"
        if bet == "ST_2": return "WYGRANA" if hst < ast else "PRZEGRANA"

    return "DO RĘCZNEJ KONTROLI"

# Rozliczanie historycznych spotkań
if not df_historia.empty and not results_clean.empty:
    for idx, row in df_historia.iterrows():
        if row["Status"] == "W OCZEKIWANIU":
            match_data = results_clean[results_clean['Match_ID'] == row["Match_ID"]]
            if not match_data.empty:
                match_row = match_data.iloc[0]
                if pd.notna(match_row.get('FTHG')):
                    nowy_status = evaluate_bet(row["Bet_Type"], match_row)
                    df_historia.at[idx, "Status"] = nowy_status
                    
                    try:
                        kurs = float(str(row["Odds"]).replace(',', '.'))
                        if nowy_status == "WYGRANA":
                            profit = round(kurs - 1.0, 2)
                            df_historia.at[idx, "Profit"] = f"+{profit}"
                            df_historia.at[idx, "Yield_Wplyw"] = f"+{round(profit*100, 1)}%"
                        elif nowy_status == "PRZEGRANA":
                            df_historia.at[idx, "Profit"] = "-1.0"
                            df_historia.at[idx, "Yield_Wplyw"] = "-100%"
                    except: pass


# ==========================================
# 8. WYSYŁKA GOOGLE SHEETS
# ==========================================
all_sheets = [
    "Summary", "Fixtures", "Results", "League_Tables", "Historia_Typow",
    "Predictions_1X", "Predictions_Builder", "Predictions_Multigol", 
    "Predictions_Corners", "Predictions_Shots", "Predictions_ColdShower",
    "Predictions_HiddenForm", "Predictions_CornerAnomalies", "Predictions_GoalAnomalies"
]

for sheet_name in all_sheets:
    try: spreadsheet.worksheet(sheet_name)
    except: spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=45)

try:
    spreadsheet.worksheet("Fixtures").resize(rows=5000, cols=35)
    spreadsheet.worksheet("Results").resize(rows=10000, cols=45) 
    spreadsheet.worksheet("Historia_Typow").resize(rows=10000, cols=16) # cols=16 pod kolumnę Akceptacja
    for name in ["Predictions_1X", "Predictions_Builder", "Predictions_Multigol", "Predictions_Corners", "Predictions_Shots", "Predictions_ColdShower", "Predictions_HiddenForm", "Predictions_CornerAnomalies", "Predictions_GoalAnomalies"]:
        spreadsheet.worksheet(name).resize(rows=3000, cols=45)
except: pass

print("Wysyłam Czysty Terminarz do Google Sheets...")
spreadsheet.worksheet("Fixtures").clear()
if not fixtures_clean.empty: spreadsheet.worksheet("Fixtures").update(prepare_for_gsheets(fixtures_clean))

print("Wysyłam Historię ze statystykami do Google Sheets...")
spreadsheet.worksheet("Results").clear()
if not results_clean.empty: spreadsheet.worksheet("Results").update(prepare_for_gsheets(results_clean))

print("Wysyłam Tabele Ligowe...")
spreadsheet.worksheet("League_Tables").clear()
if not league_tables.empty: spreadsheet.worksheet("League_Tables").update(prepare_for_gsheets(league_tables))

print("Wysyłam Logi Systemu Backtestingu (Historia_Typow)...")
ws_historia.clear()
if not df_historia.empty: ws_historia.update(prepare_for_gsheets(df_historia))

print("Wysyłam Analizy Podstawowe (1X, Builder, Multigol, Corners, Shots)...")
spreadsheet.worksheet("Predictions_1X").clear()
if not df_pred_1x.empty: spreadsheet.worksheet("Predictions_1X").update(prepare_for_gsheets(df_pred_1x))

spreadsheet.worksheet("Predictions_Builder").clear()
if not df_pred_builder.empty: spreadsheet.worksheet("Predictions_Builder").update(prepare_for_gsheets(df_pred_builder))

spreadsheet.worksheet("Predictions_Multigol").clear()
if not df_pred_multigol.empty: spreadsheet.worksheet("Predictions_Multigol").update(prepare_for_gsheets(df_pred_multigol))

spreadsheet.worksheet("Predictions_Corners").clear()
if not df_pred_corners.empty: spreadsheet.worksheet("Predictions_Corners").update(prepare_for_gsheets(df_pred_corners))

spreadsheet.worksheet("Predictions_Shots").clear()
if not df_pred_shots.empty: spreadsheet.worksheet("Predictions_Shots").update(prepare_for_gsheets(df_pred_shots))

print("Wysyłam Analizy Behawioralne i Wariancyjne...")
spreadsheet.worksheet("Predictions_ColdShower").clear()
if not df_pred_coldshower.empty: spreadsheet.worksheet("Predictions_ColdShower").update(prepare_for_gsheets(df_pred_coldshower))

spreadsheet.worksheet("Predictions_HiddenForm").clear()
if not df_pred_hiddenform.empty: spreadsheet.worksheet("Predictions_HiddenForm").update(prepare_for_gsheets(df_pred_hiddenform))

spreadsheet.worksheet("Predictions_CornerAnomalies").clear()
if not df_pred_corner_anomalies.empty: spreadsheet.worksheet("Predictions_CornerAnomalies").update(prepare_for_gsheets(df_pred_corner_anomalies))

spreadsheet.worksheet("Predictions_GoalAnomalies").clear()
if not df_pred_goal_anomalies.empty: spreadsheet.worksheet("Predictions_GoalAnomalies").update(prepare_for_gsheets(df_pred_goal_anomalies))

print("Wysyłam Logi Pobierania (Summary) do Google Sheets...")
summary_data = [
    ["==== PODSUMOWANIE OGÓLNE ====", "", ""],
    ["Ostatnia aktualizacja", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""],
    ["Fixtures Czyste", len(fixtures_clean), ""],
    ["Results Zintegrowane", len(results_clean), ""],
    ["Tabela Drużyn", len(league_tables), ""],
    ["Przetworzone Typy w Historii", len(df_historia), ""],
    ["", "", ""],
    ["==== PODSUMOWANIE PREDYKCJI ====", "", ""],
    ["Wyselekcjonowane Typy 1X Pro", len(df_pred_1x), ""],
    ["Wyselekcjonowane Bloki BetBuilder", len(df_pred_builder), ""],
    ["Typy Multigol (Zakresy)", len(df_pred_multigol), ""],
    ["Wyselekcjonowane Typy Rożne (Undery)", len(df_pred_corners), ""],
    ["Wyliczone Typy Strzałów i Celnych", len(df_pred_shots), ""],
    ["Typy Zimny Prysznic (Motywacja)", len(df_pred_coldshower), ""],
    ["Typy Ukryta Forma (Proxy xG)", len(df_pred_hiddenform), ""],
    ["Anomalie Rożnych (Regresja do średniej)", len(df_pred_corner_anomalies), ""],
    ["Anomalie Bramkowe (Regresja do średniej)", len(df_pred_goal_anomalies), ""],
    ["", "", ""],
    ["==== RAPORT POBIERANIA Z LINKÓW ====", "", ""],
    ["System", "URL", "Status / Wynik"]
]
summary_data.extend(scrape_report)
spreadsheet.worksheet("Summary").clear()
spreadsheet.worksheet("Summary").update(summary_data)

print("\n" + "=" * 60)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Zaktualizowano historię typów (Backtester).")
print("=" * 60)
