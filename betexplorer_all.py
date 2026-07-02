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
import math
from collections import Counter

today = datetime.now()

# ==========================================================
# FUNKCJE MATEMATYCZNE BUKMACHERA (POISSON I KORELACJE)
# ==========================================================
def get_poisson_prob(lam, k, calc_type="exact"):
    if pd.isna(lam) or lam <= 0: return 0.0
    try:
        if calc_type == "exact": return (math.exp(-lam) * (lam**k)) / math.factorial(int(k))
        elif calc_type == "under": return sum((math.exp(-lam) * (lam**i)) / math.factorial(i) for i in range(int(k) + 1))
        elif calc_type == "over": return 1.0 - sum((math.exp(-lam) * (lam**i)) / math.factorial(i) for i in range(int(k) + 1))
    except: return 0.0

def get_poisson_match_prob(lam_h, lam_a, max_val=35):
    if pd.isna(lam_h) or pd.isna(lam_a) or lam_h <= 0 or lam_a <= 0: return 0.0, 0.0, 0.0
    p_1, p_x, p_2 = 0.0, 0.0, 0.0
    for i in range(max_val):
        prob_i = get_poisson_prob(lam_h, i, "exact")
        for j in range(max_val):
            prob_j = get_poisson_prob(lam_a, j, "exact")
            prob_ij = prob_i * prob_j
            if i > j: p_1 += prob_ij
            elif i == j: p_x += prob_ij
            else: p_2 += prob_ij
    return p_1, p_x, p_2

def calc_betbuilder_odd(probs, correlation_factor=0.65, margin=0.92):
    if not probs: return 1.0
    probs.sort(reverse=True) 
    combined_p = probs[0]
    for p in probs[1:]: combined_p *= (p ** (1 - correlation_factor))
    fair_odd = 1 / combined_p if combined_p > 0 else 99.0
    return max(1.05, round(fair_odd * margin, 2))

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
    if pd.isna(d_str) or str(d_str).strip() in ["", "nan", "NaT", "None"]: return "Nieznany"
    try:
        d = pd.to_datetime(str(d_str), format='%d.%m.%Y', errors='coerce')
        if pd.isna(d): d = pd.to_datetime(str(d_str), errors='coerce', format='mixed')
        if pd.isna(d): return "Nieznany"
        d_date = d.date()
        today_date = datetime.now().date()
        delta = (d_date - today_date).days
        if delta < 0: return "Przeszłość"
        if delta == 0: return "Dziś"
        if delta == 1: return "Jutro"
        if 2 <= delta <= 7 and d_date.weekday() >= 4: return "Ten Weekend"
        elif 2 <= delta <= 7: return "Ten Tydzień"
        else: return "Przyszłość"
    except Exception: return "Nieznany"

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
                new_row.append("")
                continue
            str_val = str(val).strip()
            if str_val in ["<NA>", "NaN", "None", "", "inf", "-inf", "-"]:
                new_row.append("")
            else:
                if any(k in col_name for k in ["Odd", "Avg", "Value", "PPG", "Prawdopodobieństwo", "Pewność", "Kurs", "Szansa", "Profit", "Marża", "Wplyw"]):
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
except Exception: mapowanie_fd, mapowanie_ss = {}, {}

# ==========================================
# 1. POBIERANIE Z BETEXPLORER 
# ==========================================
try: urls = pd.read_excel("ligi.xlsx")["URL"].dropna().tolist()
except: urls = []

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"}
scraper_be = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})

all_data = []
for i, url in enumerate(urls, start=1):
    url_clean = str(url).strip()
    print(f"[{i}/{len(urls)}] Pobieram BetExplorer: {url_clean}")
    if "/fixtures/" not in url_clean and "/results/" not in url_clean: continue
    if i > 1: time.sleep(random.uniform(1.0, 2.5))
    
    max_retries = 3
    response = None
    bypass_used, success = False, False

    for attempt in range(max_retries):
        if attempt > 0: time.sleep(random.uniform(10, 15) * attempt)
        try:
            response = scraper_be.get(url_clean, timeout=30)
            if response.status_code == 200: success = True; break
            elif response.status_code in [429, 403]: bypass_used = True
            else: break
        except Exception:
            if attempt < max_retries - 1: time.sleep(5)

    if not success or response is None or response.status_code != 200:
        final_code = response.status_code if response else "Brak odpowiedzi"
        scrape_report.append(["BetExplorer", url_clean, f"BŁĄD: Kod {final_code}"])
        continue

    try:
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        league = url_clean.split("/football/")[1].replace("/fixtures/", "").replace("/results/", "").split('?')[0].strip('/')
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
                    odd = cell.get("data-odd") or (cell.find(attrs={"data-odd": True}).get("data-odd") if cell.find(attrs={"data-odd": True}) else None) or cell.get_text(" ", strip=True)
                    odds.append(odd if odd else "")
                odd1, oddx, odd2 = (odds[0] if len(odds)>0 else ""), (odds[1] if len(odds)>1 else ""), (odds[2] if len(odds)>2 else "")
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
                    odd = cell.get("data-odd") or (cell.find(attrs={"data-odd": True}).get("data-odd") if cell.find(attrs={"data-odd": True}) else None) or cell.get_text(" ", strip=True)
                    odds.append(odd if odd else "")
                odd1, oddx, odd2 = (odds[0] if len(odds)>0 else ""), (odds[1] if len(odds)>1 else ""), (odds[2] if len(odds)>2 else "")
                date_cell = row.find("td", class_=lambda x: x and "h-text-right" in x)
                date = date_cell.get_text(strip=True) if date_cell else ""
                all_data.append(["Result", league, date, home, away, score, odd1, oddx, odd2])
                mecz_count += 1
                
        if mecz_count > 0:
            status_msg = f"OK (Pobrano: {mecz_count} meczów)" + (" [Zadziałał Bypass 429]" if bypass_used else "")
            scrape_report.append(["BetExplorer", url_clean, status_msg])
        else:
            scrape_report.append(["BetExplorer", url_clean, "OSTRZEŻENIE: Brak meczów na stronie (0)"])
    except Exception as e: scrape_report.append(["BetExplorer", url_clean, f"BŁĄD PARSOWANIA: {e}"])

df = pd.DataFrame(all_data, columns=["Type", "League", "Date", "Home", "Away", "Score", "Odd1", "OddX", "Odd2"]).drop_duplicates()

if not df.empty:
    dates, times = zip(*[split_datetime(v) for v in df["Date"]])
    df["Date"], df["Time"] = dates, times
else: df["Time"] = pd.Series(dtype='object')

fixtures_df = df[df["Type"] == "Fixture"].copy()
results_df = df[df["Type"] == "Result"].copy()

# ==========================================
# 2. POBIERANIE Z SOCCERSTATS 
# ==========================================
dane_soccerstats_baza = []
print("Rozpoczynam pobieranie z SoccerStats...")
try:
    if os.path.exists("ligi_soccerstats.xlsx"):
        urls_ss = pd.read_excel("ligi_soccerstats.xlsx")["URL"].dropna().tolist()
        skaner_ss = cloudscraper.create_scraper(browser={'browser': 'chrome','platform': 'windows','desktop': True})
        for url_ss in urls_ss:
            url_ss_clean = str(url_ss).strip()
            time.sleep(random.uniform(1.0, 2.0))
            try:
                response_ss = skaner_ss.get(url_ss_clean, headers=headers, timeout=30)
                soup_ss = BeautifulSoup(response_ss.text, "html.parser")
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
                                    g_gosp_1h, g_gosc_1h = "", ""
                                    if ":" in ht_czysty:
                                        try: p_1h = ht_czysty.split(":"); g_gosp_1h, g_gosc_1h = int(p_1h[0]), int(p_1h[1])
                                        except: pass
                                    dane_soccerstats_baza.append([gospodarz, gosc, wynik_czysty, g_gosp_1h, g_gosc_1h])
                                    ss_count += 1
                if ss_count > 0: scrape_report.append(["SoccerStats", url_ss_clean, f"OK (Pobrano: {ss_count} wierszy)"])
                else: scrape_report.append(["SoccerStats", url_ss_clean, "OSTRZEŻENIE: Brak meczów na stronie (0)"])
            except Exception as e: scrape_report.append(["SoccerStats", url_ss_clean, f"BŁĄD HTTP: {str(e)}"])
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
    'Match_ID': 'Match_ID', 'Date': 'Date', 'League': 'League', 'Home': 'Home', 'Away': 'Away',
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

    results_df['HT_Total'] = pd.to_numeric(results_df['HTHG'], errors='coerce') + pd.to_numeric(results_df['HTAG'], errors='coerce')
    results_df['Total_Corners'] = pd.to_numeric(results_df['HC'], errors='coerce') + pd.to_numeric(results_df['AC'], errors='coerce')

    fd_expected_cols = ['HS', 'AS', 'HST', 'AST', 'HC', 'AC']
    for col in fd_expected_cols:
        if col not in results_df.columns: results_df[col] = np.nan

    results_df['Date_str'] = pd.to_datetime(results_df['Date'], errors='coerce').dt.strftime('%Y%m%d').fillna('99999999')
    results_df['Match_ID'] = results_df['Date_str'] + "_" + results_df['Home'].str[:3].str.upper() + "_" + results_df['Away'].str[:3].str.upper()

    def get_margin_results(r):
        try:
            o1, ox, o2 = float(str(r['Odd1']).replace(',','.')), float(str(r['OddX']).replace(',','.')), float(str(r['Odd2']).replace(',','.'))
            return f"{round(((1/o1)+(1/ox)+(1/o2)-1.0)*100, 2)}%".replace('.', ',')
        except: return ""
    results_df['Marża'] = results_df.apply(get_margin_results, axis=1)

if not fixtures_df.empty:
    fixtures_df['Date_str'] = pd.to_datetime(fixtures_df['Date'], errors='coerce').dt.strftime('%Y%m%d').fillna('99999999')
    fixtures_df['Match_ID'] = fixtures_df['Date_str'] + "_" + fixtures_df['Home'].str[:3].str.upper() + "_" + fixtures_df['Away'].str[:3].str.upper()
    fixtures_df['Termin'] = fixtures_df['Date'].apply(categorize_date)
    fixtures_df['Status_Kursów'] = np.where(fixtures_df['Odd1'].astype(str).str.strip().isin(["", "-", "nan"]), "Brak Kursów", "Są Kursy")

    def get_margin(r):
        try:
            o1, ox, o2 = float(str(r['Odd1']).replace(',','.')), float(str(r['OddX']).replace(',','.')), float(str(r['Odd2']).replace(',','.'))
            return f"{round(((1/o1)+(1/ox)+(1/o2)-1.0)*100, 2)}%".replace('.', ',')
        except: return ""
    fixtures_df['Marża'] = fixtures_df.apply(get_margin, axis=1)

results_clean = results_df[list(golden_cols.keys()) + ['HT_Total', 'Total_Corners', 'Marża']].rename(columns=golden_cols) if not results_df.empty else pd.DataFrame(columns=list(golden_cols.values()) + ['HT_Total', 'Total_Corners', 'Marża'])
fixtures_clean = fixtures_df[['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd1', 'OddX', 'Odd2', 'Marża']].rename(columns={'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2'}) if not fixtures_df.empty else pd.DataFrame(columns=['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd_1', 'Odd_X', 'Odd_2', 'Marża'])

# ==========================================
# 5. GENEROWANIE TABEL LIGOWYCH I H2H
# ==========================================
print("Generowanie inteligentnych tabel ligowych (6 Koszyków) oraz zestawień H2H...")
valid_matches = pd.DataFrame()

if not results_clean.empty:
    temp_df = results_clean.copy()
    temp_df['Date_Parsed'] = pd.to_datetime(temp_df['Date'].astype(str), errors='coerce')
    temp_df = temp_df.sort_values(by='Date_Parsed', ascending=False)
    valid_matches = temp_df.dropna(subset=['FTHG', 'FTAG']).copy()

if not valid_matches.empty:
    valid_matches['Base_League'] = valid_matches['League'].apply(get_base_league)
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
    league_tables['Pozycja'] = league_tables.groupby('League').cumcount() + 1
    league_counts = league_tables.groupby('League')['Team'].transform('count')
    
    def assign_tier(row):
        total = row['Total_Teams']
        pos = row['Pozycja']
        if total > 0:
            tier_num = math.ceil((pos / total) * 6)
            if tier_num < 1: tier_num = 1
            if tier_num > 6: tier_num = 6
            return f"Koszyk {tier_num}"
        return "Koszyk 3"
        
    league_tables['Total_Teams'] = league_counts
    league_tables['Koszyk'] = league_tables.apply(assign_tier, axis=1)
    league_tables = league_tables.drop(columns=['Total_Teams'])
    league_tables = league_tables[['League', 'Pozycja', 'Team', 'M', 'W', 'D', 'L', 'GF', 'GA', 'GD', 'Pts', 'PPG', 'Koszyk']]
else:
    league_tables = pd.DataFrame(columns=['League', 'Pozycja', 'Team', 'M', 'W', 'D', 'L', 'GF', 'GA', 'GD', 'Pts', 'PPG', 'Koszyk'])

team_tiers = {}
if not league_tables.empty:
    for _, r in league_tables.iterrows():
        team_tiers[(r['League'], r['Team'])] = r['Koszyk']

# --- GENERATOR H2H DLA ZAKŁADKI "H2H_Mecze" ---
h2h_list = []
if not fixtures_clean.empty and not valid_matches.empty:
    upcoming = fixtures_clean[fixtures_clean['Status_Kursów'] == 'Są Kursy']
    for _, f in upcoming.iterrows():
        f_home, f_away, f_date, f_league = f['Home'], f['Away'], f['Date'], f['League']
        base_lg = get_base_league(f_league)
        h2h_m = valid_matches[(valid_matches['Base_League'] == base_lg) & (((valid_matches['Home'] == f_home) & (valid_matches['Away'] == f_away)) | ((valid_matches['Home'] == f_away) & (valid_matches['Away'] == f_home)))].head(5)
        for _, h in h2h_m.iterrows():
            h2h_list.append([
                f"{f_home} - {f_away}", f_date, f_league,
                h['Date'], h['Home'], h['Away'], f"{int(h['FTHG'])}:{int(h['FTAG'])}",
                str(h['HT_Total']).replace('.0', ''), str(h['Total_Corners']).replace('.0', '')
            ])
df_h2h = pd.DataFrame(h2h_list, columns=["Nadchodzący Mecz", "Data Meczu", "Liga", "Data H2H", "Gospodarz H2H", "Gość H2H", "Wynik H2H", "Gole HT", "Rożne H2H"])


# ==========================================================
# 6. SILNIKI PREDYKCYJNE (Z centralizowanym Generatorem Tekstu)
# ==========================================================
all_generated_predictions = []

def add_pred(match_id, date, time, league, home, away, engine, typ, kurs_rynek, szansa, kurs_szac, arg):
    all_generated_predictions.append({
        "Match_ID": match_id, "Data": date, "Godzina": time, "Liga": league, 
        "Gospodarz": home, "Gość": away, "Engine": engine, "Typ": typ, 
        "Kurs_Rynek": kurs_rynek if kurs_rynek not in ["-", "nan"] else "",
        "Szansa": szansa, "Kurs_Szac": kurs_szac, "Argumentacja": arg
    })

print("Uruchamiam Modele Predykcyjne...")

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    match_id, d_date, d_time = row['Match_ID'], row['Date'], row['Time']
    
    o1_raw, ox_raw, o2_raw = row['Odd_1'], row['Odd_X'], row['Odd_2']
    buk_odd_1x = ""
    if str(o1_raw).strip() not in ["", "-", "nan"] and str(ox_raw).strip() not in ["", "-", "nan"]:
        try: buk_odd_1x = round(1 / ((1 / float(str(o1_raw).replace(',','.'))) + (1 / float(str(ox_raw).replace(',','.')))), 2)
        except: pass

    # --- WSPÓLNE BAZY DO ANALIZ ---
    h_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == home))].copy()
    a_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == away) | (valid_matches['Away'] == away))].copy()
    h_dom = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Home'] == home)].copy()
    a_wyj = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Away'] == away)].copy()
    
    h_tier = team_tiers.get((league, home), 'Koszyk 3')
    a_tier = team_tiers.get((league, away), 'Koszyk 3')

    if len(h_tot_all) > 0:
        h_tot_all['Team_GF'] = np.where(h_tot_all['Home'] == home, h_tot_all['FTHG'], h_tot_all['FTAG'])
        h_tot_all['Team_GA'] = np.where(h_tot_all['Home'] == home, h_tot_all['FTAG'], h_tot_all['FTHG'])
    if len(a_tot_all) > 0:
        a_tot_all['Team_GF'] = np.where(a_tot_all['Home'] == away, a_tot_all['FTHG'], a_tot_all['FTAG'])
        a_tot_all['Team_GA'] = np.where(a_tot_all['Home'] == away, a_tot_all['FTAG'], a_tot_all['FTHG'])

    # ----------------------------------------------------
    # 6a. 1X PRO (Dixon-Coles + H2H Koszyki Argumentacja)
    # ----------------------------------------------------
    lg_matches = valid_matches[valid_matches['Base_League'] == fixture_base]
    if len(lg_matches) >= 20 and len(h_tot_all) >= 5 and len(a_tot_all) >= 5 and len(h_dom) > 0 and len(a_wyj) > 0:
        lg_home_goals, lg_away_goals = lg_matches['FTHG'].mean(), lg_matches['FTAG'].mean()
        lg_avg_goals = lg_home_goals + lg_away_goals

        h_gf_avg = np.where(h_tot_all.head(15)['Home'] == home, h_tot_all.head(15)['FTHG'], h_tot_all.head(15)['FTAG']).mean()
        h_ga_avg = np.where(h_tot_all.head(15)['Home'] == home, h_tot_all.head(15)['FTAG'], h_tot_all.head(15)['FTHG']).mean()
        a_gf_avg = np.where(a_tot_all.head(15)['Home'] == away, a_tot_all.head(15)['FTHG'], a_tot_all.head(15)['FTAG']).mean()
        a_ga_avg = np.where(a_tot_all.head(15)['Home'] == away, a_tot_all.head(15)['FTAG'], a_tot_all.head(15)['FTHG']).mean()

        h_att = h_gf_avg / (lg_avg_goals / 2) if lg_avg_goals > 0 else 1.0
        h_def = h_ga_avg / (lg_avg_goals / 2) if lg_avg_goals > 0 else 1.0
        a_att = a_gf_avg / (lg_avg_goals / 2) if lg_avg_goals > 0 else 1.0
        a_def = a_ga_avg / (lg_avg_goals / 2) if lg_avg_goals > 0 else 1.0

        lam_h = h_att * a_def * lg_home_goals
        lam_a = a_att * h_def * lg_away_goals
        p1_g, px_g, p2_g = get_poisson_match_prob(lam_h, lam_a, max_val=15)
        
        prob_1x, prob_x2 = p1_g + px_g, px_g + p2_g
        if prob_1x >= prob_x2: typ_kod, final_prob = "1X", min(prob_1x, 0.95)
        else: typ_kod, final_prob = "X2", min(prob_x2, 0.95)

        if final_prob >= 0.70:
            fair_odd = round((1 / final_prob) * 0.93, 2)
            
            if typ_kod == "1X":
                h_1x_c = sum(h_dom['FTHG'] >= h_dom['FTAG'])
                a_win_c = sum(a_wyj['FTAG'] > a_wyj['FTHG'])
                h_1x_tot = sum(h_tot_all['Team_GF'] >= h_tot_all['Team_GA'])
                a_win_tot = sum(a_tot_all['Team_GF'] > a_tot_all['Team_GA'])
                
                h_losses = h_dom[h_dom['FTHG'] < h_dom['FTAG']]
                h_ls_tiers = [team_tiers.get((league, x), 'Koszyk 3') for x in h_losses['Away']]
                h_ls_txt = ", ".join([f"{k.replace('Koszyk ', 'K')}:{v}x" for k, v in dict(Counter(h_ls_tiers)).items()]) if h_ls_tiers else "Brak"

                a_wins = a_wyj[a_wyj['FTAG'] > a_wyj['FTHG']]
                a_ws_tiers = [team_tiers.get((league, x), 'Koszyk 3') for x in a_wins['Home']]
                a_ws_txt = ", ".join([f"{k.replace('Koszyk ', 'K')}:{v}x" for k, v in dict(Counter(a_ws_tiers)).items()]) if a_ws_tiers else "Brak"

                arg = f"Gosp ({h_tier}) dom bez porażki {h_1x_c}/{len(h_dom)} (Ogółem: {h_1x_tot}/{len(h_tot_all)}). Gosp przegrywał z: [{h_ls_txt}]. Gość ({a_tier}) wyjazd wygrał {a_win_c}/{len(a_wyj)} (Ogółem: {a_win_tot}/{len(a_tot_all)}). Gość wygrywał z: [{a_ws_txt}]."
            else:
                a_x2_c = sum(a_wyj['FTAG'] >= a_wyj['FTHG'])
                h_win_c = sum(h_dom['FTHG'] > h_dom['FTAG'])
                a_x2_tot = sum(a_tot_all['Team_GF'] >= a_tot_all['Team_GA'])
                h_win_tot = sum(h_tot_all['Team_GF'] > h_tot_all['Team_GA'])

                a_losses = a_wyj[a_wyj['FTAG'] < a_wyj['FTHG']]
                a_ls_tiers = [team_tiers.get((league, x), 'Koszyk 3') for x in a_losses['Home']]
                a_ls_txt = ", ".join([f"{k.replace('Koszyk ', 'K')}:{v}x" for k, v in dict(Counter(a_ls_tiers)).items()]) if a_ls_tiers else "Brak"

                h_wins = h_dom[h_dom['FTHG'] > h_dom['FTAG']]
                h_ws_tiers = [team_tiers.get((league, x), 'Koszyk 3') for x in h_wins['Away']]
                h_ws_txt = ", ".join([f"{k.replace('Koszyk ', 'K')}:{v}x" for k, v in dict(Counter(h_ws_tiers)).items()]) if h_ws_tiers else "Brak"

                arg = f"Gość ({a_tier}) wyjazd bez porażki {a_x2_c}/{len(a_wyj)} (Ogółem: {a_x2_tot}/{len(a_tot_all)}). Gość przegrywał z: [{a_ls_txt}]. Gosp ({h_tier}) dom wygrał {h_win_c}/{len(h_dom)} (Ogółem: {h_win_tot}/{len(h_tot_all)}). Gosp wygrywał z: [{h_ws_txt}]."
                
            add_pred(match_id, d_date, d_time, league, home, away, "1X Pro", typ_kod, str(buk_odd_1x), f"{round(final_prob*100)}%", str(fair_odd).replace('.', ','), arg)

    # ----------------------------------------------------
    # 6b. BETBUILDER PRO (Taśmy)
    # ----------------------------------------------------
    PROG_OVER = 0.88
    PROG_UNDER = 0.88
    MIN_BLOKOW = 2
    
    if len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5:
        h_dom['HT_Total'] = pd.to_numeric(h_dom['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(h_dom['HTAG'], errors='coerce').fillna(0)
        a_wyj['HT_Total'] = pd.to_numeric(a_wyj['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(a_wyj['HTAG'], errors='coerce').fillna(0)
        h_tot_all['HT_Total'] = pd.to_numeric(h_tot_all['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(h_tot_all['HTAG'], errors='coerce').fillna(0)
        a_tot_all['HT_Total'] = pd.to_numeric(a_tot_all['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(a_tot_all['HTAG'], errors='coerce').fillna(0)
        
        builder_blocks_code, block_probabilities, arg_blocks = [], [], []
        
        h_o05 = sum(h_dom['Total_Goals'] >= 1)
        a_o05 = sum(a_wyj['Total_Goals'] >= 1)
        h_tot_o05 = sum(h_tot_all['Total_Goals'] >= 1)
        a_tot_o05 = sum(a_tot_all['Total_Goals'] >= 1)
        prob_o05 = (h_o05/len(h_dom) + a_o05/len(a_wyj)) / 2
        if prob_o05 >= PROG_OVER:
            builder_blocks_code.append("O0.5")
            block_probabilities.append(prob_o05)
            arg_blocks.append(f"O0.5 (D: {h_o05}/{len(h_dom)}, W: {a_o05}/{len(a_wyj)} | Ogół Gosp: {h_tot_o05}/{len(h_tot_all)}, Gość: {a_tot_o05}/{len(a_tot_all)})")

        for line in [3.5, 4.5, 5.5, 6.5]:
            h_u = sum(h_dom['Total_Goals'] < line)
            a_u = sum(a_wyj['Total_Goals'] < line)
            h_tot_u = sum(h_tot_all['Total_Goals'] < line)
            a_tot_u = sum(a_tot_all['Total_Goals'] < line)
            prob_u = (h_u/len(h_dom) + a_u/len(a_wyj)) / 2
            if prob_u >= PROG_UNDER:
                builder_blocks_code.append(f"U{line}")
                block_probabilities.append(prob_u)
                arg_blocks.append(f"U{line} (D: {h_u}/{len(h_dom)}, W: {a_u}/{len(a_wyj)} | Ogół Gosp: {h_tot_u}/{len(h_tot_all)}, Gość: {a_tot_u}/{len(a_tot_all)})")
                break

        for line in [1.5, 2.5]:
            h_u_1h = sum(h_dom['HT_Total'] < line)
            a_u_1h = sum(a_wyj['HT_Total'] < line)
            h_tot_u_1h = sum(h_tot_all['HT_Total'] < line)
            a_tot_u_1h = sum(a_tot_all['HT_Total'] < line)
            prob_u_1h = (h_u_1h/len(h_dom) + a_u_1h/len(a_wyj)) / 2
            if prob_u_1h >= PROG_UNDER:
                builder_blocks_code.append(f"HT_U{line}")
                block_probabilities.append(prob_u_1h)
                arg_blocks.append(f"HT_U{line} (D: {h_u_1h}/{len(h_dom)}, W: {a_u_1h}/{len(a_wyj)} | Ogół Gosp: {h_tot_u_1h}/{len(h_tot_all)}, Gość: {a_tot_u_1h}/{len(a_tot_all)})")
                break

        if len(builder_blocks_code) >= MIN_BLOKOW:
            final_builder_safety = round(np.mean(block_probabilities) * 100, 1)
            estimated_bb_odd = calc_betbuilder_odd(block_probabilities, correlation_factor=0.65, margin=0.92)
            uzasadnienie = " | ".join(arg_blocks)
            add_pred(match_id, d_date, d_time, league, home, away, "BetBuilder Pro", "+".join(builder_blocks_code), "", f"{final_builder_safety}%", str(estimated_bb_odd).replace('.', ','), uzasadnienie)

    # ----------------------------------------------------
    # 6c. MULTIGOL (Regresja z Dokładnymi Wynikami)
    # ----------------------------------------------------
    if len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5:
        h_last_goals = get_last_match_goals(fixture_base, home)
        a_last_goals = get_last_match_goals(fixture_base, away)
        
        if h_last_goals == 0 or h_last_goals > 5 or a_last_goals == 0 or a_last_goals > 5:
            h_15 = sum((h_dom['Total_Goals'].between(1,5)))
            a_15 = sum((a_wyj['Total_Goals'].between(1,5)))
            h_tot_15 = sum((h_tot_all['Total_Goals'].between(1,5)))
            a_tot_15 = sum((a_tot_all['Total_Goals'].between(1,5)))
            prob_1_5 = (h_15/len(h_dom) + a_15/len(a_wyj)) / 2
            
            h_16 = sum((h_dom['Total_Goals'].between(1,6)))
            a_16 = sum((a_wyj['Total_Goals'].between(1,6)))
            h_tot_16 = sum((h_tot_all['Total_Goals'].between(1,6)))
            a_tot_16 = sum((a_tot_all['Total_Goals'].between(1,6)))
            prob_1_6 = (h_16/len(h_dom) + a_16/len(a_wyj)) / 2
            
            if prob_1_5 >= 0.90 or prob_1_6 >= 0.90:
                typ_kod, pewnosc, hc, ac, htc, atc = ("MG_1-5", prob_1_5, h_15, a_15, h_tot_15, a_tot_15) if prob_1_5 >= 0.90 else ("MG_1-6", prob_1_6, h_16, a_16, h_tot_16, a_tot_16)
                est_odd = round(1.0 + (((1/pewnosc) - 1.0) / 1.5), 2)
                
                h_scores = ", ".join([f"{int(m['FTHG'])}:{int(m['FTAG'])}" for _, m in h_tot_all.head(3).iterrows()])
                a_scores = ", ".join([f"{int(m['FTHG'])}:{int(m['FTAG'])}" for _, m in a_tot_all.head(3).iterrows()])
                
                arg = f"Regresja po anomalii (Wyniki Gosp ost. 3: {h_scores} | Gość ost. 3: {a_scores}). Historycznie {typ_kod} wchodzi u Gosp: {hc}/{len(h_dom)} (Ogół: {htc}/{len(h_tot_all)}), u Gości: {ac}/{len(a_wyj)} (Ogół: {atc}/{len(a_tot_all)})."
                add_pred(match_id, d_date, d_time, league, home, away, "Multigol", typ_kod, "", f"{round(pewnosc*100, 1)}%", str(est_odd).replace('.', ','), arg)

    # ----------------------------------------------------
    # 6d. CORNERS PRO
    # ----------------------------------------------------
    valid_corners = valid_matches.dropna(subset=['Corners_H', 'Corners_A']).copy()
    h_tot_all_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & ((valid_corners['Home'] == home) | (valid_corners['Away'] == home))].copy()
    a_tot_all_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & ((valid_corners['Home'] == away) | (valid_corners['Away'] == away))].copy()
    h_dom_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & (valid_corners['Home'] == home)]
    a_wyj_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & (valid_corners['Away'] == away)]

    if len(h_tot_all_c) >= 8 and len(a_tot_all_c) >= 8 and len(h_dom_c) >= 3 and len(a_wyj_c) >= 3:
        h_tot_all_c['Team_C_For'] = np.where(h_tot_all_c['Home'] == home, h_tot_all_c['Corners_H'], h_tot_all_c['Corners_A'])
        a_tot_all_c['Team_C_For'] = np.where(a_tot_all_c['Home'] == away, a_tot_all_c['Corners_H'], a_tot_all_c['Corners_A'])
        
        max_match = max(h_dom_c['Total_Corners'].max(), a_wyj_c['Total_Corners'].max())
        max_h = h_dom_c['Corners_H'].max()
        max_a = a_wyj_c['Corners_A'].max()

        c_blocks_code, c_probs, c_odds, arg_c = [], [], [], []

        for line in [8.5, 9.5, 10.5, 11.5, 12.5, 13.5, 14.5]:
            if line > max_match - 2:
                h_c = sum(h_dom_c['Total_Corners'] < line)
                a_c = sum(a_wyj_c['Total_Corners'] < line)
                h_tot_c = sum(h_tot_all_c['Total_Corners'] < line)
                a_tot_c = sum(a_tot_all_c['Total_Corners'] < line)
                avg_p = (h_c/len(h_dom_c) + a_c/len(a_wyj_c)) / 2
                if avg_p >= 0.90:
                    c_blocks_code.append(f"C_U{line}"); c_probs.append(avg_p); c_odds.append(round(1/(avg_p*0.90), 2))
                    arg_c.append(f"C_U{line} (D: {h_c}/{len(h_dom_c)}, W: {a_c}/{len(a_wyj_c)} | Ogół Gosp: {h_tot_c}/{len(h_tot_all_c)}, Gość: {a_tot_c}/{len(a_tot_all_c)})")
                    break

        for line in [4.5, 5.5, 6.5, 7.5, 8.5]:
            if line > max_h - 1:
                h_c = sum(h_dom_c['Corners_H'] < line)
                h_tot_c = sum(h_tot_all_c['Team_C_For'] < line)
                if h_c/len(h_dom_c) >= 0.92:
                    c_blocks_code.append(f"HC_U{line}"); c_probs.append(h_c/len(h_dom_c)); c_odds.append(round(1/((h_c/len(h_dom_c))*0.90), 2))
                    arg_c.append(f"HC_U{line} (D: {h_c}/{len(h_dom_c)} | Ogół: {h_tot_c}/{len(h_tot_all_c)})")
                    break

        for line in [3.5, 4.5, 5.5, 6.5, 7.5]:
            if line > max_a - 1:
                a_c = sum(a_wyj_c['Corners_A'] < line)
                a_tot_c = sum(a_tot_all_c['Team_C_For'] < line)
                if a_c/len(a_wyj_c) >= 0.92:
                    c_blocks_code.append(f"AC_U{line}"); c_probs.append(a_c/len(a_wyj_c)); c_odds.append(round(1/((a_c/len(a_wyj_c))*0.90), 2))
                    arg_c.append(f"AC_U{line} (W: {a_c}/{len(a_wyj_c)} | Ogół: {a_tot_c}/{len(a_tot_all_c)})")
                    break

        if len(c_blocks_code) >= 1:
            est_odd = round((1.0 + sum([(o - 1.0) * 0.60 for o in c_odds])) * 0.95, 2) if len(c_blocks_code) > 1 else c_odds[0]
            if est_odd < 1.05: est_odd = 1.05
            add_pred(match_id, d_date, d_time, league, home, away, "Corners Pro", "+".join(c_blocks_code), "", f"{round(np.mean(c_probs)*100, 1)}%", str(est_odd).replace('.', ','), " | ".join(arg_c))

    # ----------------------------------------------------
    # 6e. SHOTS PRO
    # ----------------------------------------------------
    valid_shots = valid_matches.dropna(subset=['Shots_H', 'Shots_A', 'ShotsTarget_H', 'ShotsTarget_A']).copy()
    if not valid_shots.empty:
        valid_shots['Shots_H'] = pd.to_numeric(valid_shots['Shots_H'], errors='coerce')
        valid_shots['Shots_A'] = pd.to_numeric(valid_shots['Shots_A'], errors='coerce')
        valid_shots['ShotsTarget_H'] = pd.to_numeric(valid_shots['ShotsTarget_H'], errors='coerce')
        valid_shots['ShotsTarget_A'] = pd.to_numeric(valid_shots['ShotsTarget_A'], errors='coerce')
        valid_shots = valid_shots.dropna(subset=['Shots_H', 'Shots_A', 'ShotsTarget_H', 'ShotsTarget_A'])
        
        h_tot_all_s = valid_shots[(valid_shots['Base_League'] == fixture_base) & ((valid_shots['Home'] == home) | (valid_shots['Away'] == home))].copy()
        a_tot_all_s = valid_shots[(valid_shots['Base_League'] == fixture_base) & ((valid_shots['Home'] == away) | (valid_shots['Away'] == away))].copy()
        h_dom_s = valid_shots[(valid_shots['Base_League'] == fixture_base) & (valid_shots['Home'] == home)]
        a_wyj_s = valid_shots[(valid_shots['Base_League'] == fixture_base) & (valid_shots['Away'] == away)]

        if len(h_dom_s) >= 2 and len(a_wyj_s) >= 2 and len(h_tot_all_s) >= 2 and len(a_tot_all_s) >= 2:
            h_s_win = sum((h_dom_s['Shots_H'] - h_dom_s['Shots_A']) > 0)
            a_s_lose = sum((a_wyj_s['Shots_A'] - a_wyj_s['Shots_H']) < 0)
            
            h_tot_all_s['Team_S'] = np.where(h_tot_all_s['Home'] == home, h_tot_all_s['Shots_H'], h_tot_all_s['Shots_A'])
            h_tot_all_s['Opp_S'] = np.where(h_tot_all_s['Home'] == home, h_tot_all_s['Shots_A'], h_tot_all_s['Shots_H'])
            a_tot_all_s['Team_S'] = np.where(a_tot_all_s['Home'] == away, a_tot_all_s['Shots_H'], a_tot_all_s['Shots_A'])
            a_tot_all_s['Opp_S'] = np.where(a_tot_all_s['Home'] == away, a_tot_all_s['Shots_A'], a_tot_all_s['Shots_H'])
            
            h_tot_s_win = sum((h_tot_all_s['Team_S'] - h_tot_all_s['Opp_S']) > 0)
            a_tot_s_lose = sum((a_tot_all_s['Team_S'] - a_tot_all_s['Opp_S']) < 0)
            
            prob_h_s = ((h_s_win/len(h_dom_s))*4.0 + (a_s_lose/len(a_wyj_s))*1.0) / 5.0

            h_st_win = sum((h_dom_s['ShotsTarget_H'] - h_dom_s['ShotsTarget_A']) > 0)
            a_st_lose = sum((a_wyj_s['ShotsTarget_A'] - a_wyj_s['ShotsTarget_H']) < 0)
            
            h_tot_all_s['Team_ST'] = np.where(h_tot_all_s['Home'] == home, h_tot_all_s['ShotsTarget_H'], h_tot_all_s['ShotsTarget_A'])
            h_tot_all_s['Opp_ST'] = np.where(h_tot_all_s['Home'] == home, h_tot_all_s['ShotsTarget_A'], h_tot_all_s['ShotsTarget_H'])
            a_tot_all_s['Team_ST'] = np.where(a_tot_all_s['Home'] == away, a_tot_all_s['ShotsTarget_H'], a_tot_all_s['ShotsTarget_A'])
            a_tot_all_s['Opp_ST'] = np.where(a_tot_all_s['Home'] == away, a_tot_all_s['ShotsTarget_A'], a_tot_all_s['ShotsTarget_H'])
            
            h_tot_st_win = sum((h_tot_all_s['Team_ST'] - h_tot_all_s['Opp_ST']) > 0)
            a_tot_st_lose = sum((a_tot_all_s['Team_ST'] - a_tot_all_s['Opp_ST']) < 0)
            
            prob_h_st = ((h_st_win/len(h_dom_s))*4.0 + (a_st_lose/len(a_wyj_s))*1.0) / 5.0

            if prob_h_s > 0.80:
                est_odd_s = round(1.0 + (((1/prob_h_s) - 1.0) / 1.5), 2) if prob_h_s < 1.0 else 1.01
                arg = f"Strzały Ogółem: Gosp win u siebie {h_s_win}/{len(h_dom_s)} (Ogółem: {h_tot_s_win}/{len(h_tot_all_s)}). Gość lose wyjazd {a_s_lose}/{len(a_wyj_s)} (Ogółem: {a_tot_s_lose}/{len(a_tot_all_s)})."
                add_pred(match_id, d_date, d_time, league, home, away, "Shots Pro", "S_1", "", f"{round(prob_h_s*100, 1)}%", str(est_odd_s).replace('.', ','), arg)
            
            if prob_h_st > 0.80:
                est_odd_st = round(1.0 + (((1/prob_h_st) - 1.0) / 1.5), 2) if prob_h_st < 1.0 else 1.01
                arg = f"Strzały Celne: Gosp win u siebie {h_st_win}/{len(h_dom_s)} (Ogółem: {h_tot_st_win}/{len(h_tot_all_s)}). Gość lose wyjazd {a_st_lose}/{len(a_wyj_s)} (Ogółem: {a_tot_st_lose}/{len(a_tot_all_s)})."
                add_pred(match_id, d_date, d_time, league, home, away, "Shots Pro", "ST_1", "", f"{round(prob_h_st*100, 1)}%", str(est_odd_st).replace('.', ','), arg)

    # ----------------------------------------------------
    # 6f. ZIMNY PRYSZNIC
    # ----------------------------------------------------
    if h_tier in ['Koszyk 1', 'Koszyk 2'] and len(h_tot_all) > 0:
        last_m = h_tot_all.iloc[0] 
        if last_m['Away'] == home and last_m['FTHG'] >= last_m['FTAG']:
            opp_tier = team_tiers.get((last_m['League'], last_m['Home']), 'Koszyk 1')
            if opp_tier in ['Koszyk 4', 'Koszyk 5', 'Koszyk 6']:
                est_odd = round(1.0 + (((1/0.85) - 1.0) / 1.5), 2)
                arg = f"Gospodarz ({h_tier}) szuka rewanżu u siebie po stracie punktów na wyjeździe z dużo słabszym rywalem ({opp_tier})."
                add_pred(match_id, d_date, d_time, league, home, away, "Cold Shower", "1", "", "85%", str(est_odd).replace('.', ','), arg)

    # ----------------------------------------------------
    # 6g. UKRYTA FORMA (Proxy xG)
    # ----------------------------------------------------
    for team, is_home in [(home, True), (away, False)]:
        t_past = valid_shots[(valid_shots['Base_League'] == fixture_base) & ((valid_shots['Home'] == team) | (valid_shots['Away'] == team))]
        if len(t_past) >= 3:
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
                    est_odd = round(1.0 + (((1/0.80) - 1.0) / 1.5), 2)
                    arg = f"Wysokie xG. W 3 ost. meczach zespół oddał {int(st_for)} celnych strzałów, ale zdobył tylko {int(g_for)} goli."
                    add_pred(match_id, d_date, d_time, league, home, away, "Hidden Form", typ_kod, "", "80%", str(est_odd).replace('.', ','), arg)

    # ----------------------------------------------------
    # 6h. ANOMALIE ROŻNYCH
    # ----------------------------------------------------
    valid_corners_all = valid_matches.dropna(subset=['Corners_H', 'Corners_A']).copy()
    for team, is_home in [(home, True), (away, False)]:
        t_past = valid_corners_all[(valid_corners_all['Base_League'] == fixture_base) & ((valid_corners_all['Home'] == team) | (valid_corners_all['Away'] == team))].copy()
        if len(t_past) >= 8:
            t_past['C_For'] = np.where(t_past['Home'] == team, t_past['Corners_H'], t_past['Corners_A'])
            season_avg = t_past['C_For'].mean()
            last_2_avg = t_past.head(2)['C_For'].mean()
            
            if season_avg >= 5.5 and last_2_avg <= 3.0:
                typ_kod = "HC_O4.5" if is_home else "AC_O4.5"
                est_odd = round(1.0 + (((1/0.82) - 1.0) / 1.5), 2)
                arg = f"Pęknięta seria. Średnia sezonu zespołu: {round(season_avg, 2)}. Średnia 2 ost. meczów: tylko {round(last_2_avg, 2)}."
                add_pred(match_id, d_date, d_time, league, home, away, "Corner Anomalies", typ_kod, "", "82%", str(est_odd).replace('.', ','), arg)

    # ----------------------------------------------------
    # 6i. ANOMALIE BRAMKOWE
    # ----------------------------------------------------
    t_past = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == away))]
    if len(t_past) >= 10:
        season_avg = t_past['Total_Goals'].mean()
        last_2_avg = t_past.head(2)['Total_Goals'].mean()
        if season_avg <= 2.8 and last_2_avg >= 4.5:
            est_odd = round(1.0 + (((1/0.85) - 1.0) / 1.5), 2)
            arg = f"Anomalia overowa. Średnia sezonu obu ekip: {round(season_avg, 2)}. Ost. 2 mecze: aż {round(last_2_avg, 2)} goli. Oczekiwany powrót undera."
            add_pred(match_id, d_date, d_time, league, home, away, "Goal Anomalies", "U3.5", "", "85%", str(est_odd).replace('.', ','), arg)
        elif season_avg >= 2.5 and last_2_avg <= 0.5:
            est_odd = round(1.0 + (((1/0.85) - 1.0) / 1.5), 2)
            arg = f"Anomalia underowa. Średnia sezonu obu ekip: {round(season_avg, 2)}. Ost. 2 mecze: tylko {round(last_2_avg, 2)} goli. Oczekiwane przełamanie."
            add_pred(match_id, d_date, d_time, league, home, away, "Goal Anomalies", "O1.5", "", "85%", str(est_odd).replace('.', ','), arg)


# ==========================================================
# 7. SYSTEM ŚLEDZENIA SKUTECZNOŚCI I YIELDU (BACKTESTER)
# ==========================================================
print("Inicjalizacja Modułu Backtestingu (Śledzenie Skuteczności)...")

scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file("credentials.json", scopes=scope) if os.path.exists("credentials.json") else Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

# ================= IDEALNA, 100% ZGODNA STRUKTURA =================
cols_all_pred = ["Match_ID", "Data", "Godzina", "Liga", "Gospodarz", "Gość", "Engine", "Typ", "Kurs_Rynek", "Szansa", "Kurs_Szac", "Argumentacja"]
cols_historia = ["Match_ID", "Zagrane", "Data", "Godzina", "Liga", "Gospodarz", "Gość", "Engine", "Typ", "Kurs_Rynek", "Szansa", "Kurs_Szac", "Argumentacja", "Status", "Profit", "Yield_Wplyw"]

df_all_predictions = pd.DataFrame(all_generated_predictions, columns=cols_all_pred)

try:
    ws_historia = spreadsheet.worksheet("Historia_Typow")
    historia_dane = ws_historia.get_all_values()
    if len(historia_dane) > 0: df_historia = pd.DataFrame(historia_dane[1:], columns=historia_dane[0])
    else: df_historia = pd.DataFrame(columns=cols_historia)
except gspread.exceptions.WorksheetNotFound:
    spreadsheet.add_worksheet(title="Historia_Typow", rows=10000, cols=20)
    ws_historia = spreadsheet.worksheet("Historia_Typow")
    df_historia = pd.DataFrame(columns=cols_historia)

# Wymuszenie czystej struktury nagłówków
for col in cols_historia:
    if col not in df_historia.columns: df_historia[col] = ""
df_historia = df_historia[cols_historia]

if not df_all_predictions.empty:
    nowe_typy_df = df_all_predictions.copy()
    nowe_typy_df.insert(1, "Zagrane", "") 
    nowe_typy_df["Status"] = "W OCZEKIWANIU"
    nowe_typy_df["Profit"] = ""
    nowe_typy_df["Yield_Wplyw"] = ""
    nowe_typy_df = nowe_typy_df[cols_historia]
    
    if not df_historia.empty:
        df_historia['Unikalny_Klucz'] = df_historia['Match_ID'] + df_historia['Engine'] + df_historia['Typ']
        nowe_typy_df['Unikalny_Klucz'] = nowe_typy_df['Match_ID'] + nowe_typy_df['Engine'] + nowe_typy_df['Typ']
        
        w_oczek_mask = df_historia['Status'] == "W OCZEKIWANIU"
        if w_oczek_mask.any():
            map_szansa = nowe_typy_df.set_index('Unikalny_Klucz')['Szansa'].to_dict()
            map_kurs = nowe_typy_df.set_index('Unikalny_Klucz')['Kurs_Szac'].to_dict()
            map_arg = nowe_typy_df.set_index('Unikalny_Klucz')['Argumentacja'].to_dict()
            map_kr = nowe_typy_df.set_index('Unikalny_Klucz')['Kurs_Rynek'].to_dict()
            
            for idx in df_historia[w_oczek_mask].index:
                klucz = df_historia.at[idx, 'Unikalny_Klucz']
                if klucz in map_szansa:
                    df_historia.at[idx, 'Szansa'] = str(map_szansa[klucz])
                    df_historia.at[idx, 'Kurs_Szac'] = str(map_kurs[klucz])
                    df_historia.at[idx, 'Argumentacja'] = str(map_arg[klucz])
                    kr_val = map_kr[klucz]
                    if pd.notna(kr_val) and str(kr_val).strip() not in ["", "-"]:
                        df_historia.at[idx, 'Kurs_Rynek'] = str(kr_val)

        do_dodania = nowe_typy_df[~nowe_typy_df['Unikalny_Klucz'].isin(df_historia['Unikalny_Klucz'])].copy()
        do_dodania = do_dodania.drop(columns=['Unikalny_Klucz'])
        df_historia = df_historia.drop(columns=['Unikalny_Klucz'])
    else:
        do_dodania = nowe_typy_df.copy()
        
    df_historia = pd.concat([df_historia, do_dodania], ignore_index=True)

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

if not df_historia.empty and not results_clean.empty:
    for idx, row in df_historia.iterrows():
        if row["Status"] == "W OCZEKIWANIU":
            match_data = results_clean[results_clean['Match_ID'] == row["Match_ID"]]
            if not match_data.empty:
                match_row = match_data.iloc[0]
                if pd.notna(match_row.get('FTHG')):
                    nowy_status = evaluate_bet(row["Typ"], match_row)
                    df_historia.at[idx, "Status"] = nowy_status
                    
                    try:
                        kurs_str = str(row["Kurs_Rynek"]).replace(',', '.').strip()
                        if kurs_str in ["", "-", "nan", "None"]: kurs_str = str(row["Kurs_Szac"]).replace(',', '.').strip()
                        kurs = float(kurs_str)
                        if nowy_status == "WYGRANA":
                            profit = round(kurs - 1.0, 2)
                            df_historia.at[idx, "Profit"] = f"+{profit}".replace('.', ',')
                            df_historia.at[idx, "Yield_Wplyw"] = f"+{round(profit*100, 1)}%".replace('.', ',')
                        elif nowy_status == "PRZEGRANA":
                            df_historia.at[idx, "Profit"] = "-1,0"
                            df_historia.at[idx, "Yield_Wplyw"] = "-100%"
                    except: pass

# --- INTELIGENTNE SORTOWANIE (W OCZEKIWANIU NA GÓRZE, Z DATAMI DO PRZODU) ---
if not df_historia.empty:
    df_historia['Data_Sort'] = pd.to_datetime(df_historia['Data'].astype(str) + ' ' + df_historia['Godzina'].astype(str).replace('', '00:00').replace('-', '00:00'), errors='coerce')
    mask_oczek = df_historia['Status'] == 'W OCZEKIWANIU'
    df_oczek = df_historia[mask_oczek].sort_values(by=['Data_Sort'], ascending=[True])
    df_rozst = df_historia[~mask_oczek].sort_values(by=['Data_Sort'], ascending=[False])
    df_historia = pd.concat([df_oczek, df_rozst]).drop(columns=['Data_Sort'])

if not df_all_predictions.empty: 
    df_all_predictions['Data_Sort'] = pd.to_datetime(df_all_predictions['Data'].astype(str) + ' ' + df_all_predictions['Godzina'].astype(str).replace('', '00:00').replace('-', '00:00'), errors='coerce')
    df_all_predictions = df_all_predictions.sort_values(by=["Data_Sort", "Szansa"], ascending=[True, False]).drop(columns=['Data_Sort'])

# ==========================================
# 8. WYSYŁKA GOOGLE SHEETS
# ==========================================
# TYLKO 7 ZŁOTYCH ZAKŁADEK (DODANO H2H)
all_sheets = ["Summary", "Fixtures", "Results", "League_Tables", "H2H_Mecze", "Historia_Typow", "All_Predictions"]

for sheet_name in all_sheets:
    try: spreadsheet.worksheet(sheet_name)
    except: spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=30)

try:
    spreadsheet.worksheet("Fixtures").resize(rows=5000, cols=25)
    spreadsheet.worksheet("Results").resize(rows=10000, cols=35) 
    spreadsheet.worksheet("H2H_Mecze").resize(rows=5000, cols=15)
    spreadsheet.worksheet("Historia_Typow").resize(rows=10000, cols=20)
    spreadsheet.worksheet("All_Predictions").resize(rows=5000, cols=20)
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

print("Wysyłam Analizę H2H do Google Sheets...")
spreadsheet.worksheet("H2H_Mecze").clear()
if not df_h2h.empty: spreadsheet.worksheet("H2H_Mecze").update(prepare_for_gsheets(df_h2h))

print("Wysyłam Logi Systemu Backtestingu (Historia_Typow)...")
ws_historia.clear()
if not df_historia.empty: ws_historia.update(prepare_for_gsheets(df_historia))

print("Wysyłam Ujednoliconą Listę Wszystkich Predykcji (All_Predictions)...")
spreadsheet.worksheet("All_Predictions").clear()
if not df_all_predictions.empty: spreadsheet.worksheet("All_Predictions").update(prepare_for_gsheets(df_all_predictions))

print("Wysyłam Zaawansowane Logi Pobierania (Summary) do Google Sheets...")
pred_breakdown = []
if not df_all_predictions.empty:
    counts = df_all_predictions['Engine'].value_counts()
    for engine, count in counts.items():
        pred_breakdown.append([f"  - {engine}", count, ""])
else:
    pred_breakdown.append(["  - Brak wygenerowanych predykcji", 0, ""])

league_breakdown = [["==== STATUS LIG (Pobranie Danych) ====", "", ""]]
league_breakdown.append(["Liga", "Liczba Fixtures (Nadchodzące)", "Liczba Results (Zintegrowane)"])

all_leagues = set()
if not fixtures_clean.empty: all_leagues.update(fixtures_clean['League'].unique())
if not results_clean.empty: all_leagues.update(results_clean['League'].unique())

for lg in sorted(all_leagues):
    f_cnt = len(fixtures_clean[fixtures_clean['League'] == lg]) if not fixtures_clean.empty else 0
    r_cnt = len(results_clean[results_clean['League'] == lg]) if not results_clean.empty else 0
    league_breakdown.append([lg, f_cnt, r_cnt])

summary_data = [
    ["==== PODSUMOWANIE OGÓLNE ====", "", ""],
    ["Ostatnia aktualizacja", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""],
    ["Fixtures Czyste", len(fixtures_clean), ""],
    ["Results Zintegrowane", len(results_clean), ""],
    ["Tabela Drużyn", len(league_tables), ""],
    ["Wygenerowane Zestawienia H2H", len(df_h2h), ""],
    ["Przetworzone Typy w Historii", len(df_historia), ""],
    ["Wygenerowane Predykcje (Suma)", len(df_all_predictions), ""],
    ["", "", ""],
    ["==== ROZKŁAD PREDYKCJI (SILNIKI) ====", "", ""]
]
summary_data.extend(pred_breakdown)
summary_data.append(["", "", ""])
summary_data.extend(league_breakdown)
summary_data.append(["", "", ""])
summary_data.append(["==== RAPORT POBIERANIA Z LINKÓW ====", "", ""])
summary_data.append(["System", "URL", "Status / Wynik"])
summary_data.extend(scrape_report)

spreadsheet.worksheet("Summary").clear()
spreadsheet.worksheet("Summary").update(summary_data)

print("\n" + "=" * 60)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Architektura z 7 zakładkami (w tym H2H) z wdrożoną pełną argumentacją statystyczną.")
print("=" * 60)
