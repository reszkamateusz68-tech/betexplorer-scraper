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
# GŁÓWNE FUNKCJE POMOCNICZE (Zadeklarowane na samej górze)
# ==========================================================
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

def get_base_league(l):
    """Odpowiada za odcinanie lat oraz parametrów stage, łącząc sezony w ciąg osi czasu."""
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
        except Exception as e:
            raport.append(["Football-Data", url_clean, f"BŁĄD: {e}"])

    if not dfs: return pd.DataFrame()
    fd_master = pd.concat(dfs, ignore_index=True)
    cols_to_keep = ['Date', 'HomeTeam', 'AwayTeam', 'HTHG', 'HTAG', 'HS', 'AS', 'HST', 'AST', 'HC', 'AC', 'HY', 'AY', 'AvgH', 'AvgD', 'AvgA']
    existing_cols = [col for col in cols_to_keep if col in fd_master.columns]
    return fd_master[existing_cols]

def get_current_streaks(base_lg, team):
    """Liczy chronologiczne passy drużyn ponad sezonami."""
    if 'valid_matches' not in globals() or valid_matches.empty:
        return 0, 0
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

def prepare_for_gsheets(df):
    """Zabezpiecza przed błędami JSON i formatuje liczby pod polskie Sheets (przecinki)."""
    output = [df.columns.tolist()]
    for row in df.values.tolist():
        new_row = []
        for idx, val in enumerate(row):
            col_name = df.columns[idx]
            if pd.isna(val):
                new_row.append("-")
                continue
            str_val = str(val).strip()
            if str_val in ["<NA>", "nan", "NaN", "None", "", "inf", "-inf"]:
                new_row.append("-")
            else:
                if any(k in col_name for k in ["Odd", "Avg", "Value", "PPG", "Prawdopodobieństwo", "Pewność", "Kurs"]):
                    new_row.append(str_val.replace(".", ","))
                else:
                    if str_val.endswith(".0") and "%" not in str_val: new_row.append(str_val[:-2])
                    else: new_row.append(str_val)
        output.append(new_row)
    return output

# LISTA NA LOGI DO ZAKŁADKI SUMMARY
scrape_report = []

# Wczytanie słownika z mapowaniem nazw drużyn
try:
    with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
        slownik = json.load(f)
        mapowanie_fd = slownik.get("FootballData_To_BetExplorer", {})
        mapowanie_ss = slownik.get("SoccerStats_To_BetExplorer", {})
except Exception as e:
    print("Brak pliku slownik_druzyn.json lub błąd wczytywania. Pobieram dane bez mapowania nazw.")
    mapowanie_fd = {}
    mapowanie_ss = {}

# ==========================================
# 1. POBIERANIE Z BETEXPLORER 
# ==========================================
try: urls = pd.read_excel("ligi.xlsx")["URL"].dropna().tolist()
except: urls = []

# NAPRAWA 1: Przywrócenie definicji nagłówków dla requests
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
            time.sleep(5)
            scraper_be = cloudscraper.create_scraper()
            response = scraper_be.get(url_clean, headers=headers, timeout=30)
            bypass_used = True

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

# NAPRAWA 2: Pełne zabezpieczenie przed pustą bazą danych (Empty DataFrame Protection)
if not df.empty:
    dates, times = zip(*[split_datetime(v) for v in df["Date"]])
    df["Date"], df["Time"] = dates, times
else:
    df["Time"] = pd.Series(dtype='object')

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
except Exception as e: ss_df = pd.DataFrame()

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
# 4. TWORZENIE ZŁOTEJ STRUKTURY
# ==========================================
print("Czyszczenie bazy - Złota Struktura...")

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

    fd_expected_cols = ['HS', 'AS', 'HST', 'AST', 'HC', 'AC', 'HY', 'AY', 'AvgH', 'AvgD', 'AvgA']
    for col in fd_expected_cols:
        if col not in results_df.columns: results_df[col] = np.nan

    results_df['Date_str'] = pd.to_datetime(results_df['Date'], errors='coerce').dt.strftime('%Y%m%d').fillna('99999999')
    results_df['Match_ID'] = results_df['Date_str'] + "_" + results_df['Home'].str[:3].str.upper() + "_" + results_df['Away'].str[:3].str.upper()
    
    for outcome in [('Odd1', 'AvgH', 'Val_1'), ('OddX', 'AvgD', 'Val_X'), ('Odd2', 'AvgA', 'Val_2')]:
        results_df[outcome[2]] = results_df.apply(lambda row: calc_value(row[outcome[0]], row[outcome[1]]), axis=1)

if not fixtures_df.empty:
    fixtures_df['Date_str'] = pd.to_datetime(fixtures_df['Date'], errors='coerce').dt.strftime('%Y%m%d').fillna('99999999')
    fixtures_df['Match_ID'] = fixtures_df['Date_str'] + "_" + fixtures_df['Home'].str[:3].str.upper() + "_" + fixtures_df['Away'].str[:3].str.upper()

results_clean = results_df[list(golden_cols.keys())].rename(columns=golden_cols) if not results_df.empty else pd.DataFrame(columns=golden_cols.values())
fixtures_clean = fixtures_df[['Match_ID', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd1', 'OddX', 'Odd2']].rename(columns={'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2'}) if not fixtures_df.empty else pd.DataFrame(columns=['Match_ID', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd_1', 'Odd_X', 'Odd_2'])

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

# ==========================================
# 6a. ENGINE 1X PRO
# ==========================================
print("Uruchamiam Engine 1X Pro...")
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

    if len(h_current) >= 30: h_window = h_current
    else: h_window = pd.concat([h_current, h_past.head(30 - len(h_current))])

    if len(a_current) >= 30: a_window = a_current
    else: a_window = pd.concat([a_current, a_past.head(30 - len(a_current))])

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

    if final_prob >= 0.66 and value_perc > 0:
        arg = f"Gospodarz stabilny dom ({h_1x_window_cnt}/{len(h_window)} w oknie 30 gier). "
        predictions_1x.append([
            row['Date'], row['Time'], league, f"{home} - {away}", f"{round(final_prob*100)}%", f"{value_perc}%",
            fair_odd, buk_odd_1x, 
            f"Baza: {len(h_window)} (bieżący: {len(h_current)})", f"{h_1x_all_cnt}/{len(h_all)}", f"{h_1x_window_cnt}/{len(h_window)}",
            f"{l_top} x", f"{l_mid} x", f"{l_bot} x",
            f"Baza: {len(a_window)} (bieżący: {len(a_current)})", f"{a_2_all_cnt}/{len(a_all)}", f"{a_2_window_cnt}/{len(a_window)}",
            f"{w_top} x", f"{w_mid} x", f"{w_bot} x",
            f"{a_fts_pct}%", h_unbeaten, a_winless, h_proxy, arg
        ])

df_pred_1x = pd.DataFrame(predictions_1x, columns=headers_1x).sort_values(by="Prawdopodobieństwo_1X", ascending=False) if predictions_1x else pd.DataFrame(columns=headers_1x)


# ==========================================================
# 6b. ENGINE BETBUILDER PRO V4
# ==========================================================
print("Uruchamiam Engine BetBuilder Pro V4...")
predictions_builder = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    
    h_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == home))]
    a_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == away) | (valid_matches['Away'] == away))]
    
    if len(h_tot_all) < 10 or len(a_tot_all) < 10: continue

    h_tot_curr = h_tot_all[h_tot_all['League'] == league]
    h_tot_past = h_tot_all[h_tot_all['League'] != league]
    if len(h_tot_curr) >= 30: h_total_all = h_tot_curr
    else: h_total_all = pd.concat([h_tot_curr, h_tot_past.head(30 - len(h_tot_curr))])

    a_tot_curr = a_tot_all[a_tot_all['League'] == league]
    a_tot_past = a_tot_all[a_tot_all['League'] != league]
    if len(a_tot_curr) >= 30: a_total_all = a_tot_curr
    else: a_total_all = pd.concat([a_tot_curr, a_tot_past.head(30 - len(a_tot_curr))])

    h_dom_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Home'] == home)]
    h_dom_curr = h_dom_all[h_dom_all['League'] == league]
    h_dom_past = h_dom_all[h_dom_all['League'] != league]
    if len(h_dom_curr) >= 30: h_dom = h_dom_curr
    else: h_dom = pd.concat([h_dom_curr, h_dom_past.head(30 - len(h_dom_curr))])

    a_wyj_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Away'] == away)]
    a_wyj_curr = a_wyj_all[a_wyj_all['League'] == league]
    a_wyj_past = a_wyj_all[a_wyj_all['League'] != league]
    if len(a_wyj_curr) >= 30: a_wyj = a_wyj_curr
    else: a_wyj = pd.concat([a_wyj_curr, a_wyj_past.head(30 - len(a_wyj_curr))])

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
    
    builder_blocks = []
    block_probabilities = []
    block_estimated_odds = []
    
    # --- TEST 0: OVER 0.5 GOLA ---
    h_dom_o05 = sum(h_dom['Total_Goals'] >= 1) / len(h_dom)
    a_wyj_o05 = sum(a_wyj['Total_Goals'] >= 1) / len(a_wyj)
    h_all_o05 = sum(h_total_all['Total_Goals'] >= 1) / len(h_total_all)
    a_all_o05 = sum(a_total_all['Total_Goals'] >= 1) / len(a_total_all)
    
    if h_dom_o05 == 1.0 and a_wyj_o05 == 1.0 and h_all_o05 == 1.0 and a_all_o05 == 1.0:
        builder_blocks.append("Mecz: Over 0.5 gola")
        block_probabilities.append(1.0)
        block_estimated_odds.append(1.04)

    # --- TEST 1: MECZ UNDER 4.5 / 5.5 / 6.5 ---
    for line in [4.5, 5.5, 6.5]:
        h_u = sum(h_dom['Total_Goals'] < line) / len(h_dom)
        a_u = sum(a_wyj['Total_Goals'] < line) / len(a_wyj)
        h_all_u = sum(h_total_all['Total_Goals'] < line) / len(h_total_all)
        a_all_u = sum(a_total_all['Total_Goals'] < line) / len(a_total_all)
        
        if h_u >= 0.95 and a_u >= 0.95 and h_all_u >= 0.94 and a_all_u >= 0.94:
            builder_blocks.append(f"Mecz: Under {line} gola")
            avg_prob = (h_u + a_u + h_all_u + a_all_u) / 4
            block_probabilities.append(avg_prob)
            block_estimated_odds.append(round(1 / (avg_prob * 0.91), 2))
            break

    # --- TEST 2: 1. POŁOWA UNDER 1.5 / 2.5 / 3.5 / 4.5 ---
    h_ht = h_dom.dropna(subset=['HT_Total'])
    a_ht = a_wyj.dropna(subset=['HT_Total'])
    if len(h_ht) >= 3 and len(a_ht) >= 3:
        for line in [1.5, 2.5, 3.5, 4.5]:
            h_u_1h = sum(h_ht['HT_Total'] < line) / len(h_ht)
            a_u_1h = sum(a_ht['HT_Total'] < line) / len(a_ht)
            if h_u_1h >= 0.94 and a_u_1h >= 0.94:
                builder_blocks.append(f"1. Połowa: Under {line} gola")
                avg_prob = (h_u_1h + a_u_1h) / 2
                block_probabilities.append(avg_prob)
                block_estimated_odds.append(round(1 / (avg_prob * 0.91), 2))
                break

    # --- TEST 3: 2. POŁOWA UNDER 2.5 / 3.5 / 4.5 ---
    h_2h = h_dom.dropna(subset=['2H_Total'])
    a_2h = a_wyj.dropna(subset=['2H_Total'])
    if len(h_2h) >= 3 and len(a_2h) >= 3:
        for line in [2.5, 3.5, 4.5]:
            h_u_2h = sum(h_2h['2H_Total'] < line) / len(h_2h)
            a_u_2h = sum(a_2h['2H_Total'] < line) / len(a_2h)
            if h_u_2h >= 0.94 and a_u_2h >= 0.94:
                builder_blocks.append(f"2. Połowa: Under {line} gola")
                avg_prob = (h_u_2h + a_u_2h) / 2
                block_probabilities.append(avg_prob)
                block_estimated_odds.append(round(1 / (avg_prob * 0.91), 2))
                break

    # --- TEST 4: GOSPODARZ UNDER 3.5 / 4.5 / 5.5 ---
    highest_h_scored = max(max_h_scored_dom, max_h_scored_all)
    for line in [3.5, 4.5, 5.5]:
        if line > highest_h_scored:
            h_u_prob = sum(h_dom['FTHG'] < line) / len(h_dom)
            h_all_u_prob = sum(h_total_all['Team_GF'] < line) / len(h_total_all)
            if h_u_prob >= 0.95 and h_all_u_prob >= 0.94:
                builder_blocks.append(f"{home}: Under {line} gola")
                avg_prob = (h_u_prob + h_all_u_prob) / 2
                block_probabilities.append(avg_prob)
                block_estimated_odds.append(round(1 / (avg_prob * 0.91), 2))
                break

    # --- TEST 5: GOŚĆ UNDER 3.5 / 4.5 ---
    highest_a_scored = max(max_a_scored_wyj, max_a_scored_all)
    for line in [3.5, 4.5]:
        if line > highest_a_scored:
            a_u_prob = sum(a_wyj['FTAG'] < line) / len(a_wyj)
            a_all_u_prob = sum(a_total_all['Team_GF'] < line) / len(a_total_all)
            if a_u_prob >= 0.95 and a_all_u_prob >= 0.94:
                builder_blocks.append(f"{away}: Under {line} gola")
                avg_prob = (a_u_prob + a_all_u_prob) / 2
                block_probabilities.append(avg_prob)
                block_estimated_odds.append(round(1 / (avg_prob * 0.91), 2))
                break

    if len(builder_blocks) >= 3:
        final_builder_safety = round(np.prod(block_probabilities) * 100, 1)
        if final_builder_safety >= 95.0:
            sugerowany_kupon = " + ".join(builder_blocks)
            
            estimated_bb_odd = round((1.0 + sum([(o - 1.0) * 0.52 for o in block_estimated_odds])) * 0.95, 2)
            if estimated_bb_odd < 1.05: estimated_bb_odd = 1.05
            
            uzasadnienie = f"Stabilny zestaw kroczący (bufor 30 gier). "
            if "Over 0.5 gola" in sugerowany_kupon: uzasadnienie += "Wykryto 100% serii bramkowych (brak 0:0 w historii) -> dodano bezpieczny Over 0.5. "

            predictions_builder.append([
                row['Date'], row['Time'], league, f"{home} - {away}", sugerowany_kupon, 
                str(estimated_bb_odd).replace('.', ','),
                f"{final_builder_safety}%",
                f"Dom: {len(h_dom)} (Suma: {len(h_total_all)})", 
                f"Wyjazd: {len(a_wyj)} (Suma: {len(a_total_all)})",
                int(max_h_scored_dom), int(max_h_conceded_dom), int(max_h_scored_all), int(max_h_conceded_all),
                int(max_a_scored_wyj), int(max_a_conceded_wyj), int(max_a_scored_all), int(max_a_conceded_all),
                uzasadnienie
            ])

df_pred_builder = pd.DataFrame(predictions_builder, columns=headers_builder).sort_values(by="Pewność Matematyczna", ascending=False) if predictions_builder else pd.DataFrame(columns=headers_builder)


# ==========================================
# 8. WYSYŁKA GOOGLE SHEETS
# ==========================================
scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file("credentials.json", scopes=scope) if os.path.exists("credentials.json") else Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

for sheet_name in ["Summary", "Fixtures", "Results", "League_Tables", "Predictions_1X", "Predictions_Builder"]:
    try: spreadsheet.worksheet(sheet_name)
    except: spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=30)

try:
    spreadsheet.worksheet("Fixtures").resize(rows=5000, cols=35)
    spreadsheet.worksheet("Results").resize(rows=10000, cols=65) 
    spreadsheet.worksheet("Predictions_1X").resize(rows=3000, cols=40)
    spreadsheet.worksheet("Predictions_Builder").resize(rows=3000, cols=35)
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

print("Wysyłam Analizy 1X (Predictions 1X)...")
spreadsheet.worksheet("Predictions_1X").clear()
if not df_pred_1x.empty: spreadsheet.worksheet("Predictions_1X").update(prepare_for_gsheets(df_pred_1x))

print("Wysyłam Analizy BetBuilder (Predictions Builder)...")
spreadsheet.worksheet("Predictions_Builder").clear()
if not df_pred_builder.empty: spreadsheet.worksheet("Predictions_Builder").update(prepare_for_gsheets(df_pred_builder))

print("Wysyłam Logi Pobierania (Summary) do Google Sheets...")
summary_data = [
    ["==== PODSUMOWANIE OGÓLNE ====", "", ""],
    ["Ostatnia aktualizacja", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""],
    ["Fixtures Czyste", len(fixtures_clean), ""],
    ["Results Zintegrowane", len(results_clean), ""],
    ["Tabela Drużyn", len(league_tables), ""],
    ["Wyselekcjonowane Typy 1X Pro", len(df_pred_1x), ""],
    ["Wyselekcjonowane Bloki BetBuilder", len(df_pred_builder), ""],
    ["", "", ""],
    ["==== RAPORT POBIERANIA Z LINKÓW ====", "", ""],
    ["System", "URL", "Status / Wynik"]
]
summary_data.extend(scrape_report)
spreadsheet.worksheet("Summary").clear()
spreadsheet.worksheet("Summary").update(summary_data)

print("\n" + "=" * 60)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Wyselekcjonowano typów BetBuilder:", len(df_pred_builder))
print("=" * 60)
