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
import concurrent.futures

today = datetime.now()

# ==========================================================
# GŁÓWNE FUNKCJE POMOCNICZE
# ==========================================================
def global_recalc_przedzial(row):
    try:
        ks_str = str(row['Kurs_Szac']).replace(',', '.').strip()
        if ks_str in ["", "-", "nan", "None"]: return "Brak kursu"
        ks = float(ks_str)
        if ks < 1.10: return "do 1.09"
        elif ks < 1.20: return "1.10 - 1.19"
        elif ks < 1.30: return "1.20 - 1.29"
        elif ks < 1.40: return "1.30 - 1.39"
        elif ks < 1.50: return "1.40 - 1.49"
        else: return "1.50+"
    except: return "Brak kursu"

def split_datetime(value):
    if pd.isna(value): return "", ""
    value = str(value).strip()
    
    if value.lower().startswith("today"): 
        parts = value.split()
        return today.strftime('%Y-%m-%d'), parts[1] if len(parts) > 1 else ""
    if value.lower().startswith("tomorrow"): 
        parts = value.split()
        return (today + timedelta(days=1)).strftime('%Y-%m-%d'), parts[1] if len(parts) > 1 else ""
    if value.lower().startswith("yesterday"): 
        parts = value.split()
        return (today - timedelta(days=1)).strftime('%Y-%m-%d'), parts[1] if len(parts) > 1 else ""
        
    parts = value.split()
    if len(parts) == 2:
        date_part, time_part = parts[0], parts[1]
        if len(date_part.split('.')) >= 3:
            try:
                if date_part.endswith("."):
                    d, m = date_part.rstrip(".").split(".")
                    return datetime(today.year, int(m), int(d)).strftime('%Y-%m-%d'), time_part
                else:
                    return datetime.strptime(date_part, "%d.%m.%Y").strftime('%Y-%m-%d'), time_part
            except: pass
            
    else:
        if len(value.split('.')) >= 3:
            try:
                if value.endswith("."):
                    d, m = value.rstrip(".").split(".")
                    return datetime(today.year, int(m), int(d)).strftime('%Y-%m-%d'), ""
                else:
                    return datetime.strptime(value, "%d.%m.%Y").strftime('%Y-%m-%d'), ""
            except: pass
            
    return value, ""

def categorize_date(d_str):
    if pd.isna(d_str) or str(d_str).strip() in ["", "nan", "NaT", "None"]: return "Nieznany"
    try:
        d = pd.to_datetime(str(d_str), format='%Y-%m-%d', errors='coerce')
        if pd.isna(d): d = pd.to_datetime(str(d_str), errors='coerce', format='mixed')
        if pd.isna(d): return "Nieznany"
        d_date = d.date()
        today_date = datetime.now().date()
        delta = (d_date - today_date).days
        
        if delta < 0: return "Przeszłość"
        if delta == 0: return "Dziś"
        if delta in [1, 2]: return "Następne 2 dni"
        if delta == 3: return "Następne 3 dni"
        if 4 <= delta <= 7: return "Następny tydzień"
        if delta > 7: return "Za ponad tydzień"
    except Exception: return "Nieznany"

def get_base_league(l):
    clean_l = str(l).split('?')[0].strip('/')
    clean_l = re.sub(r'-\d{4}(-\d{4})?$', '', clean_l)
    return clean_l

def fetch_footballdata_worker(url):
    u = str(url).strip()
    try:
        df = pd.read_csv(u, on_bad_lines='skip')
        df = df.dropna(subset=['HomeTeam'])
        return df, ["Football-Data", u, f"OK (Pobrano: {len(df)} wierszy)"]
    except Exception as e:
        return pd.DataFrame(), ["Football-Data", u, f"BŁĄD: {e}"]

def fetch_football_data(raport):
    print("Pobieram statystyki z ligi_footballdata.xlsx (Wielowątkowo)...")
    try: urls = pd.read_excel("ligi_footballdata.xlsx")["URL"].dropna().tolist()
    except Exception as e:
        raport.append(["Football-Data", "ligi_footballdata.xlsx", f"BŁĄD Excela: {e}"])
        return pd.DataFrame()
        
    dfs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(fetch_footballdata_worker, urls)
        for df_res, rep in results:
            raport.append(rep)
            if not df_res.empty: dfs.append(df_res)
            
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
                if any(k in col_name for k in ["Odd", "Avg", "Value", "PPG", "Kurs", "Szansa", "Profit", "Marża", "Yield", "Stawka", "Wygrana", "Liczba", "Consensus"]):
                    clean_val = str_val.replace("%", "").replace(",", ".").strip()
                    new_row.append(clean_val)
                else:
                    if str_val.endswith(".0"): new_row.append(str_val[:-2])
                    else: new_row.append(str_val)
        output.append(new_row)
    return output

# ==========================================================
# FUNKCJE MATEMATYCZNE BUKMACHERA (POISSON I COPULA)
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

def calc_betbuilder_copula(odds_list, rho=0.85):
    if not odds_list: return 1.0
    q_list = [1.0 / o for o in odds_list if o > 0]
    if not q_list: return 1.0
    q_list.sort() 
    
    q_joint = q_list[0]
    for q_next in q_list[1:]:
        gamma = 1.0 - rho * (1.0 - min(q_joint, q_next))
        q_joint = q_joint * (q_next ** gamma)
        
    final_odd = 1.0 / q_joint if q_joint > 0 else 99.0
    return max(1.01, round(final_odd, 2))

# ==========================================================
# FUNKCJE ANALITYCZNE I KONTROLA RYZYKA (TIME DECAY & BAYES)
# ==========================================================
def get_weighted_stats(df, target_col, condition_lambda, prior_prob=0.5, alpha=2.0):
    if df.empty: return 0.0, 0, 0, False
        
    total_weight = 0.0
    weighted_hits = 0.0
    total_hits = 0
    
    if target_col is None:
        valid_values = [row for _, row in df.iterrows()]
    else:
        if target_col not in df.columns: return 0.0, 0, 0, False
        values = df[target_col].tolist()
        valid_values = [v for v in values if pd.notna(v)]
        
    total_len = len(valid_values)
    
    for i, val in enumerate(valid_values):
        if i < 10: w = 1.0
        elif i < 20: w = 0.90
        elif i < 30: w = 0.80
        else: w = 0.70
        
        try: is_hit = 1 if condition_lambda(val) else 0
        except: is_hit = 0
            
        if is_hit: total_hits += 1
        
        weighted_hits += is_hit * w
        total_weight += w
        
    raw_prob = weighted_hits / total_weight if total_weight > 0 else 0.0
    
    is_smoothed = False
    if 0 < total_len < 12 and alpha > 0:
        prob = (weighted_hits + (alpha * prior_prob)) / (total_weight + alpha)
        is_smoothed = True
    else:
        prob = raw_prob
        
    return prob, total_hits, total_len, is_smoothed

# Funkcja wyciągająca detale o koszykach rozegranych meczów do argumentacji
def get_tier_stats(df, is_home, league, team_tiers, target_col, condition):
    if df.empty: return "0/0 [Brak]"
    try:
        if target_col is None:
            hits_df = df[df.apply(condition, axis=1)]
        else:
            hits_df = df[df[target_col].apply(condition)]
            
        opp_col = 'Away' if is_home else 'Home'
        tiers = dict(Counter([team_tiers.get((league, x), 'K3').replace('Koszyk ', 'K') for x in hits_df[opp_col]]))
        tiers_str = ", ".join([f"{k}:{v}x" for k, v in tiers.items()]) if tiers else "Brak"
        return f"{len(hits_df)}/{len(df)} [{tiers_str}]"
    except Exception:
        return "?/? [Błąd]"

def evaluate_bet(bet_type, row_data):
    bet = str(bet_type).upper().strip()
    hg = pd.to_numeric(row_data.get('FTHG', np.nan))
    ag = pd.to_numeric(row_data.get('FTAG', np.nan))
    tg = pd.to_numeric(row_data.get('Total_Goals', np.nan))
    ht_hg = pd.to_numeric(row_data.get('HTHG', np.nan))
    ht_ag = pd.to_numeric(row_data.get('HTAG', np.nan))
    hc = pd.to_numeric(row_data.get('Corners_H', np.nan))
    ac = pd.to_numeric(row_data.get('Corners_A', np.nan))

    if "+" in bet:
        parts = bet.split("+")
        results = [evaluate_bet(p.strip(), row_data) for p in parts]
        if "PRZEGRANA" in results: return "PRZEGRANA"
        if "W OCZEKIWANIU" in results: return "W OCZEKIWANIU"
        return "WYGRANA"

    if pd.isna(hg) or pd.isna(ag): return "W OCZEKIWANIU"

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

    return "DO RĘCZNEJ KONTROLI"

scrape_report = []
try:
    with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
        slownik = json.load(f)
        mapowanie_fd = slownik.get("FootballData_To_BetExplorer", {})
        mapowanie_ss = slownik.get("SoccerStats_To_BetExplorer", {})
except Exception: mapowanie_fd, mapowanie_ss = {}, {}

# ==========================================
# 1. WIELOWĄTKOWE POBIERANIE Z BETEXPLORER 
# ==========================================
try: urls = pd.read_excel("ligi.xlsx")["URL"].dropna().tolist()
except: urls = []

def scrape_be_worker(args):
    i, url_clean, total = args
    time.sleep(random.uniform(0.1, 3.0))
    local_data = []
    local_report = []
    print(f"[{i}/{total}] Pobieram BetExplorer (Wątek): {url_clean}")
    
    scraper_be = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
    max_retries = 3
    response = None
    bypass_used, success = False, False

    for attempt in range(max_retries):
        if attempt > 0: time.sleep(random.uniform(5, 10) * attempt)
        try:
            response = scraper_be.get(url_clean, timeout=30)
            if response.status_code == 200: success = True; break
            elif response.status_code in [429, 403]: bypass_used = True
            else: break
        except Exception:
            if attempt < max_retries - 1: time.sleep(3)

    if not success or response is None or response.status_code != 200:
        final_code = response.status_code if response else "Brak odpowiedzi"
        local_report.append(["BetExplorer", url_clean, f"BŁĄD: Kod {final_code}"])
        return local_data, local_report

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
                local_data.append(["Fixture", league, date_cell.get_text(strip=True), home, away, "", odd1, oddx, odd2])
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
                local_data.append(["Result", league, date, home, away, score, odd1, oddx, odd2])
                mecz_count += 1
                
        if mecz_count > 0:
            status_msg = f"OK (Pobrano: {mecz_count} meczów)" + (" [Zadziałał Bypass 429]" if bypass_used else "")
            local_report.append(["BetExplorer", url_clean, status_msg])
        else:
            local_report.append(["BetExplorer", url_clean, "OSTRZEŻENIE: Brak meczów na stronie (0)"])
    except Exception as e: local_report.append(["BetExplorer", url_clean, f"BŁĄD PARSOWANIA: {e}"])
    
    return local_data, local_report

all_data = []
print("Rozpoczynam pobieranie z BetExplorer (Wielowątkowo)...")
valid_urls = [u for u in urls if "/fixtures/" in str(u) or "/results/" in str(u)]
be_args = [(i, str(url).strip(), len(valid_urls)) for i, url in enumerate(valid_urls, start=1)]

with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    for data_chunk, report_chunk in executor.map(scrape_be_worker, be_args):
        all_data.extend(data_chunk)
        scrape_report.extend(report_chunk)

df = pd.DataFrame(all_data, columns=["Type", "League", "Date", "Home", "Away", "Score", "Odd1", "OddX", "Odd2"]).drop_duplicates()

if not df.empty:
    dates, times = zip(*[split_datetime(v) for v in df["Date"]])
    df["Date"], df["Time"] = dates, times
else: df["Time"] = pd.Series(dtype='object')

fixtures_df = df[df["Type"] == "Fixture"].copy()
results_df = df[df["Type"] == "Result"].copy()

# ==========================================
# 2. WIELOWĄTKOWE POBIERANIE Z SOCCERSTATS 
# ==========================================
def scrape_ss_worker(args):
    url_ss_clean, headers = args
    time.sleep(random.uniform(0.1, 2.0))
    local_data = []
    local_report = []
    skaner_ss = cloudscraper.create_scraper(browser={'browser': 'chrome','platform': 'windows','desktop': True})
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
                            ht = statystyki[0] if len(statystyki) > 0 else ""
                            wynik_czysty = wynik.replace("*", "").strip().replace(" ", "").replace("-", ":")
                            ht_czysty = ht.replace("*", "").strip().replace(" ", "").replace("-", ":").replace("(", "").replace(")", "")
                            g_gosp_1h, g_gosc_1h = "", ""
                            if ":" in ht_czysty:
                                try: p_1h = ht_czysty.split(":"); g_gosp_1h, g_gosc_1h = int(p_1h[0]), int(p_1h[1])
                                except: pass
                            local_data.append([gospodarz, gosc, wynik_czysty, g_gosp_1h, g_gosc_1h])
                            ss_count += 1
        if ss_count > 0: local_report.append(["SoccerStats", url_ss_clean, f"OK (Pobrano: {ss_count} wierszy)"])
        else: local_report.append(["SoccerStats", url_ss_clean, "OSTRZEŻENIE: Brak meczów na stronie (0)"])
    except Exception as e: local_report.append(["SoccerStats", url_ss_clean, f"BŁĄD HTTP: {str(e)}"])
    return local_data, local_report

dane_soccerstats_baza = []
print("Rozpoczynam pobieranie z SoccerStats (Wielowątkowo)...")
try:
    if os.path.exists("ligi_soccerstats.xlsx"):
        urls_ss = pd.read_excel("ligi_soccerstats.xlsx")["URL"].dropna().tolist()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"}
        ss_args = [(str(u).strip(), headers) for u in urls_ss]
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            for data_chunk, report_chunk in executor.map(scrape_ss_worker, ss_args):
                dane_soccerstats_baza.extend(data_chunk)
                scrape_report.extend(report_chunk)

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
    'HC': 'Corners_H', 'AC': 'Corners_A'
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

if not fixtures_df.empty:
    fixtures_df['Date_str'] = pd.to_datetime(fixtures_df['Date'], errors='coerce').dt.strftime('%Y%m%d').fillna('99999999')
    fixtures_df['Match_ID'] = fixtures_df['Date_str'] + "_" + fixtures_df['Home'].str[:3].str.upper() + "_" + fixtures_df['Away'].str[:3].str.upper()
    fixtures_df['Termin'] = fixtures_df['Date'].apply(categorize_date)
    fixtures_df['Status_Kursów'] = np.where(fixtures_df['Odd1'].astype(str).str.strip().isin(["", "-", "nan"]), "Brak Kursów", "Są Kursy")

results_clean = results_df[list(golden_cols.keys()) + ['HT_Total', 'Total_Corners']].rename(columns=golden_cols) if not results_df.empty else pd.DataFrame(columns=list(golden_cols.values()) + ['HT_Total', 'Total_Corners'])
fixtures_clean = fixtures_df[['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away']] if not fixtures_df.empty else pd.DataFrame(columns=['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away'])

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

h2h_list = []
h2h_cols = ["Match_ID", "Nadchodzący Mecz", "Data Meczu", "Liga", "Data H2H", "Gospodarz H2H", "Gość H2H", "Wynik H2H", "Gole HT", "Rożne H2H"]
df_h2h = pd.DataFrame(columns=h2h_cols)

if not fixtures_clean.empty and not valid_matches.empty:
    upcoming = fixtures_clean[fixtures_clean['Status_Kursów'] == 'Są Kursy']
    for _, f in upcoming.iterrows():
        f_match_id, f_home, f_away, f_date, f_league = f['Match_ID'], f['Home'], f['Away'], f['Date'], f['League']
        base_lg = get_base_league(f_league)
        h2h_m = valid_matches[(valid_matches['Base_League'] == base_lg) & (((valid_matches['Home'] == f_home) & (valid_matches['Away'] == f_away)) | ((valid_matches['Home'] == f_away) & (valid_matches['Away'] == f_home)))].head(5)
        for _, h in h2h_m.iterrows():
            h2h_list.append([
                f_match_id, f"{f_home} - {f_away}", f_date, f_league,
                h['Date'], h['Home'], h['Away'], f"{int(h['FTHG'])}:{int(h['FTAG'])}",
                str(h['HT_Total']).replace('.0', ''), str(h['Total_Corners']).replace('.0', '')
            ])
    if h2h_list:
        df_h2h = pd.DataFrame(h2h_list, columns=h2h_cols)

# ==========================================================
# 6. SILNIKI PREDYKCYJNE (Z centralnym Generatorem Ryzyka)
# ==========================================================
all_generated_predictions = []

ZAKAZANE_TYPY_SOLO = ["O0.5", "U5.5", "U6.5", "HT_U1.5", "HT_U2.5", "2H_U3.5", "HU3.5", "AU2.5", "HU4.5", "AU4.5"]

# KOMPLETNA BAZA KOTWIC Z MATEMATYCZNEGO MODELU KALIBRACJI
KOTWICE_KURSOWE = {
    'U2.5': 1.85, 'U3.5': 1.31, 'U4.5': 1.10, 'U5.5': 1.015, 'U6.5': 1.01,
    'O0.5': 1.03, 'O1.5': 1.25, 'O2.5': 1.85,
    'HT_U1.5': 1.42, 'HT_U2.5': 1.138, 'HT_U3.5': 1.053, 'HT_U4.5': 1.01,
    '2H_U1.5': 1.512, '2H_U2.5': 1.20, '2H_U3.5': 1.08, '2H_U4.5': 1.02,
    'HU2.5': 1.20, 'HU3.5': 1.06, 'HU4.5': 1.01,
    'AU2.5': 1.15, 'AU3.5': 1.04, 'AU4.5': 1.01,
    'MG_1-5': 1.09,
    'C_U8.5': 3.00, 'C_U9.5': 2.18, 'C_U10.5': 1.71, 'C_U11.5': 1.43,
    'C_U12.5': 1.26, 'C_U13.5': 1.15, 'C_U14.5': 1.09,
    'HC_U4.5': 2.79, 'HC_U5.5': 1.89, 'HC_U6.5': 1.45, 'HC_U7.5': 1.23, 'HC_U8.5': 1.11,
    'AC_U4.5': 1.87, 'AC_U5.5': 1.42, 'AC_U6.5': 1.20, 'AC_U7.5': 1.09, 'AC_U8.5': 1.04,
    'S_1': 1.34, 'ST_1': 1.64
}

BB_TEMPLATES = [
    {"name": "Optymalny", "code": "U6.5+HT_U4.5+2H_U4.5+HU4.5+AU4.5", "min_prob": 0.85},
    {"name": "Bezpieczny", "code": "U5.5+HT_U4.5+2H_U4.5+HU4.5+AU4.5", "min_prob": 0.80},
    {"name": "Standard", "code": "U4.5+HT_U3.5+2H_U4.5+HU3.5+AU3.5", "min_prob": 0.65}
]

print("Uruchamiam Modele Predykcyjne...")

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    match_id, d_termin, d_date, d_time = row['Match_ID'], row['Termin'], row['Date'], row['Time']

    h_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == home))].copy()
    a_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Away'] == away) | (valid_matches['Home'] == away))].copy()
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

    match_preds = []
    
    def add_pred_local(engine, typ, szansa, kurs_szac, arg):
        typ_k = str(typ).strip()
        try: kurs_docelowy = float(str(kurs_szac).replace(',', '.'))
        except: kurs_docelowy = 1.02

        if kurs_docelowy < 1.01: 
            kurs_docelowy = 1.01

        prob_decimal = float(szansa) / 100.0
        
        if prob_decimal >= 0.95 and kurs_docelowy >= 1.20: risk_tag = "👑 GOLDEN PICK"
        elif prob_decimal >= 0.95 and 1.10 <= kurs_docelowy < 1.20: risk_tag = "🥈 SILVER PICK"
        elif prob_decimal >= 0.95: risk_tag = "SAFE (95%+)"
        elif prob_decimal >= 0.85: risk_tag = "STANDARD (85-94%)"
        elif prob_decimal >= 0.75: risk_tag = "VALUE (75-84%)"
        else: risk_tag = "RISK (70-74%)"

        clean_arg = str(arg)
        if clean_arg.startswith("["): arg_final = re.sub(r"^\[.*?\]\s*", f"[{risk_tag}] ", clean_arg)
        else: arg_final = f"[{risk_tag}] {clean_arg}"

        match_preds.append({
            "Match_ID": match_id, "Termin": d_termin, "Data": d_date, "Godzina": d_time, "Liga": league, 
            "Gospodarz": home, "Gość": away, "Engine": engine, "Typ": typ, 
            "Szansa": szansa, "Kurs_Szac": kurs_docelowy, "Argumentacja": arg_final
        })

    # --- 6a. 1X PRO Z SZCZEGÓŁOWĄ ANALIZĄ H2H ---
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
        
        prob_1 = p1_g
        prob_x = px_g
        prob_2 = p2_g
        prob_1x = p1_g + px_g
        prob_x2 = px_g + p2_g
        
        najlepszy_typ = "1X"
        najlepsze_prob = prob_1x
        if prob_x2 > najlepsze_prob:
            najlepszy_typ = "X2"
            najlepsze_prob = prob_x2
        if prob_1 > 0.65 and prob_1 > najlepsze_prob * 0.8:
            najlepszy_typ = "1"
            najlepsze_prob = prob_1
        elif prob_2 > 0.65 and prob_2 > najlepsze_prob * 0.8:
            najlepszy_typ = "2"
            najlepsze_prob = prob_2

        if najlepsze_prob >= 0.70:
            fair_odd = round(1 / najlepsze_prob, 2)
            
            h_wins = h_dom[h_dom['FTHG'] > h_dom['FTAG']]
            h_losses = h_dom[h_dom['FTHG'] < h_dom['FTAG']]
            a_wins = a_wyj[a_wyj['FTAG'] > a_wyj['FTHG']]
            a_losses = a_wyj[a_wyj['FTAG'] < a_wyj['FTHG']]
            
            h_ws_txt = ", ".join([f"{k.replace('Koszyk ', 'K')}:{v}x" for k, v in dict(Counter([team_tiers.get((league, x), 'K3') for x in h_wins['Away']])).items()]) or "Brak"
            h_ls_txt = ", ".join([f"{k.replace('Koszyk ', 'K')}:{v}x" for k, v in dict(Counter([team_tiers.get((league, x), 'K3') for x in h_losses['Away']])).items()]) or "Brak"
            
            a_ws_txt = ", ".join([f"{k.replace('Koszyk ', 'K')}:{v}x" for k, v in dict(Counter([team_tiers.get((league, x), 'K3') for x in a_wins['Home']])).items()]) or "Brak"
            a_ls_txt = ", ".join([f"{k.replace('Koszyk ', 'K')}:{v}x" for k, v in dict(Counter([team_tiers.get((league, x), 'K3') for x in a_losses['Home']])).items()]) or "Brak"

            if najlepszy_typ in ["1X", "1"]:
                arg = f"Gosp ({h_tier}) u siebie: wygrał {len(h_wins)}/{len(h_dom)} [{h_ws_txt}], przegrał {len(h_losses)}/{len(h_dom)} [{h_ls_txt}]. Gość ({a_tier}) na wyjeździe: wygrał {len(a_wins)}/{len(a_wyj)} [{a_ws_txt}], przegrał {len(a_losses)}/{len(a_wyj)} [{a_ls_txt}]."
            else:
                arg = f"Gość ({a_tier}) na wyjeździe: wygrał {len(a_wins)}/{len(a_wyj)} [{a_ws_txt}], przegrał {len(a_losses)}/{len(a_wyj)} [{a_ls_txt}]. Gosp ({h_tier}) u siebie: wygrał {len(h_wins)}/{len(h_dom)} [{h_ws_txt}], przegrał {len(h_losses)}/{len(h_dom)} [{h_ls_txt}]."
                
            add_pred_local("1X Pro", najlepszy_typ, round(najlepsze_prob*100, 1), fair_odd, arg)

    # --- 6b. GOAL LINE PRO ---
    if len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5:
        for line in [2.5, 3.5, 4.5]: 
            prob_h_u, h_th, h_tl, h_sm = get_weighted_stats(h_dom, 'Total_Goals', lambda x: pd.notna(x) and x < line, prior_prob=0.75)
            prob_a_u, a_th, a_tl, a_sm = get_weighted_stats(a_wyj, 'Total_Goals', lambda x: pd.notna(x) and x < line, prior_prob=0.75)
            avg_prob_u = (prob_h_u + prob_a_u) / 2
            if avg_prob_u >= 0.70:
                h_stat = get_tier_stats(h_dom, True, league, team_tiers, 'Total_Goals', lambda x: pd.notna(x) and x < line)
                a_stat = get_tier_stats(a_wyj, False, league, team_tiers, 'Total_Goals', lambda x: pd.notna(x) and x < line)
                arg = f"Gosp ({h_tier}): {h_stat} | Gość ({a_tier}): {a_stat}"
                if h_sm or a_sm: arg += " | ⚠️ Bayes"
                add_pred_local("Goal Line Pro", f"U{line}", round(avg_prob_u*100, 1), KOTWICE_KURSOWE.get(f"U{line}", 1.10), arg)

        for line in [1.5, 2.5]: 
            prob_h_o, h_th, h_tl, h_sm = get_weighted_stats(h_dom, 'Total_Goals', lambda x: pd.notna(x) and x > line, prior_prob=0.30)
            prob_a_o, a_th, a_tl, a_sm = get_weighted_stats(a_wyj, 'Total_Goals', lambda x: pd.notna(x) and x > line, prior_prob=0.30)
            avg_prob_o = (prob_h_o + prob_a_o) / 2
            if avg_prob_o >= 0.70: 
                h_stat = get_tier_stats(h_dom, True, league, team_tiers, 'Total_Goals', lambda x: pd.notna(x) and x > line)
                a_stat = get_tier_stats(a_wyj, False, league, team_tiers, 'Total_Goals', lambda x: pd.notna(x) and x > line)
                arg = f"Gosp ({h_tier}): {h_stat} | Gość ({a_tier}): {a_stat}"
                if h_sm or a_sm: arg += " | ⚠️ Bayes"
                add_pred_local("Goal Line Pro", f"O{line}", round(avg_prob_o*100, 1), KOTWICE_KURSOWE.get(f"O{line}", 1.10), arg)

    # --- 6c. BETBUILDER PRO (Model z wysoką korelacją) ---
    if len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5:
        h_dom['HT_Total'] = pd.to_numeric(h_dom['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(h_dom['HTAG'], errors='coerce').fillna(0)
        a_wyj['HT_Total'] = pd.to_numeric(a_wyj['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(a_wyj['HTAG'], errors='coerce').fillna(0)
        h_dom['2H_Total'] = pd.to_numeric(h_dom['Total_Goals'], errors='coerce').fillna(0) - h_dom['HT_Total']
        a_wyj['2H_Total'] = pd.to_numeric(a_wyj['Total_Goals'], errors='coerce').fillna(0) - a_wyj['HT_Total']
        
        for tpl in BB_TEMPLATES:
            p_h, h_h, h_l, h_sm = get_weighted_stats(h_dom, None, lambda r, code=tpl['code']: evaluate_bet(code, r) == "WYGRANA", prior_prob=tpl['min_prob'])
            p_a, a_h, a_l, a_sm = get_weighted_stats(a_wyj, None, lambda r, code=tpl['code']: evaluate_bet(code, r) == "WYGRANA", prior_prob=tpl['min_prob'])
            
            p_combined = (p_h + p_a) / 2
            if p_combined >= tpl['min_prob']:
                skladniki = tpl['code'].split("+")
                k_skladowe = [KOTWICE_KURSOWE.get(sk.strip(), 1.05) for sk in skladniki]
                final_odd = calc_betbuilder_copula(k_skladowe, rho=0.85) 
                
                h_stat = get_tier_stats(h_dom, True, league, team_tiers, None, lambda r, code=tpl['code']: evaluate_bet(code, r) == "WYGRANA")
                a_stat = get_tier_stats(a_wyj, False, league, team_tiers, None, lambda r, code=tpl['code']: evaluate_bet(code, r) == "WYGRANA")
                arg = f"BB {tpl['name']} | Gosp ({h_tier}): {h_stat} | Gość ({a_tier}): {a_stat}"
                if h_sm or a_sm: arg += " | ⚠️ Bayes"
                add_pred_local(f"BetBuilder Pro", tpl['code'], round(p_combined*100, 1), final_odd, arg)

    # --- 6d. MULTIGOL ---
    if len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5:
        prob_h_15, h_th, h_tl, h_sm = get_weighted_stats(h_dom, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5, prior_prob=0.80)
        prob_a_15, a_th, a_tl, a_sm = get_weighted_stats(a_wyj, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5, prior_prob=0.80)
        prob_1_5 = (prob_h_15 + prob_a_15) / 2
        
        if prob_1_5 >= 0.90:
            est_odd = KOTWICE_KURSOWE.get("MG_1-5", 1.09)
            h_stat = get_tier_stats(h_dom, True, league, team_tiers, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5)
            a_stat = get_tier_stats(a_wyj, False, league, team_tiers, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5)
            arg = f"Gosp ({h_tier}): {h_stat} | Gość ({a_tier}): {a_stat}"
            if h_sm or a_sm: arg += " | ⚠️ Bayes"
            add_pred_local("Multigol", "MG_1-5", round(prob_1_5*100, 1), est_odd, arg)

    # --- 6e. CORNERS PRO ---
    valid_corners = valid_matches.dropna(subset=['Corners_H', 'Corners_A']).copy()
    h_tot_all_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & ((valid_corners['Home'] == home) | (valid_corners['Away'] == home))].copy()
    a_tot_all_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & ((valid_corners['Home'] == away) | (valid_corners['Away'] == away))].copy()
    h_dom_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & (valid_corners['Home'] == home)]
    a_wyj_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & (valid_corners['Away'] == away)]

    if len(h_tot_all_c) >= 8 and len(a_tot_all_c) >= 8 and len(h_dom_c) >= 3 and len(a_wyj_c) >= 3:
        max_match = max(h_dom_c['Total_Corners'].max(), a_wyj_c['Total_Corners'].max())
        max_h = h_dom_c['Corners_H'].max()
        max_a = a_wyj_c['Corners_A'].max()
        c_blocks_code, c_probs, c_odds, arg_c = [], [], [], []

        for line in [8.5, 9.5, 10.5, 11.5, 12.5, 13.5, 14.5]:
            if line > max_match - 2:
                prob_h_c, h_th, h_tl, c_h_sm = get_weighted_stats(h_dom_c, 'Total_Corners', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
                prob_a_c, a_th, a_tl, c_a_sm = get_weighted_stats(a_wyj_c, 'Total_Corners', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
                avg_p = (prob_h_c + prob_a_c) / 2
                if avg_p >= 0.90:
                    c_blocks_code.append(f"C_U{line}")
                    c_probs.append(avg_p)
                    c_odds.append(KOTWICE_KURSOWE.get(f"C_U{line}", round(1/(avg_p*0.90), 2)))
                    h_stat = get_tier_stats(h_dom_c, True, league, team_tiers, 'Total_Corners', lambda x: pd.notna(x) and x < line)
                    a_stat = get_tier_stats(a_wyj_c, False, league, team_tiers, 'Total_Corners', lambda x: pd.notna(x) and x < line)
                    arg_c.append(f"C_U{line} (Gosp: {h_stat}, Gość: {a_stat})")
                    break

        for line in [4.5, 5.5, 6.5, 7.5, 8.5]:
            if line > max_h - 1:
                prob_hc, h_th, h_tl, _ = get_weighted_stats(h_dom_c, 'Corners_H', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
                if prob_hc >= 0.92:
                    c_blocks_code.append(f"HC_U{line}")
                    c_probs.append(prob_hc)
                    c_odds.append(KOTWICE_KURSOWE.get(f"HC_U{line}", round(1/(prob_hc*0.90), 2)))
                    h_stat = get_tier_stats(h_dom_c, True, league, team_tiers, 'Corners_H', lambda x: pd.notna(x) and x < line)
                    arg_c.append(f"HC_U{line} (Gosp: {h_stat})")
                    break

        for line in [3.5, 4.5, 5.5, 6.5, 7.5]:
            if line > max_a - 1:
                prob_ac, a_th, a_tl, _ = get_weighted_stats(a_wyj_c, 'Corners_A', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
                if prob_ac >= 0.92:
                    c_blocks_code.append(f"AC_U{line}")
                    c_probs.append(prob_ac)
                    c_odds.append(KOTWICE_KURSOWE.get(f"AC_U{line}", round(1/(prob_ac*0.90), 2)))
                    a_stat = get_tier_stats(a_wyj_c, False, league, team_tiers, 'Corners_A', lambda x: pd.notna(x) and x < line)
                    arg_c.append(f"AC_U{line} (Gość: {a_stat})")
                    break

        if len(c_blocks_code) >= 1:
            est_odd = calc_betbuilder_copula(c_odds, rho=0.60) if len(c_blocks_code) > 1 else c_odds[0]
            if est_odd < 1.02: est_odd = 1.02
            add_pred_local("Corners Pro", "+".join(c_blocks_code), round(np.mean(c_probs)*100, 1), round(est_odd, 2), " | ".join(arg_c))

    # --- 6f. SHOTS PRO ---
    valid_shots = valid_matches.dropna(subset=['Shots_H', 'Shots_A', 'ShotsTarget_H', 'ShotsTarget_A']).copy()
    if not valid_shots.empty:
        valid_shots['Shots_H'] = pd.to_numeric(valid_shots['Shots_H'], errors='coerce')
        valid_shots['Shots_A'] = pd.to_numeric(valid_shots['Shots_A'], errors='coerce')
        valid_shots['ShotsTarget_H'] = pd.to_numeric(valid_shots['ShotsTarget_H'], errors='coerce')
        valid_shots['ShotsTarget_A'] = pd.to_numeric(valid_shots['ShotsTarget_A'], errors='coerce')
        valid_shots = valid_shots.dropna(subset=['Shots_H', 'Shots_A', 'ShotsTarget_H', 'ShotsTarget_A'])
        
        h_dom_s = valid_shots[(valid_shots['Base_League'] == fixture_base) & (valid_shots['Home'] == home)]
        a_wyj_s = valid_shots[(valid_shots['Base_League'] == fixture_base) & (valid_shots['Away'] == away)]

        if len(h_dom_s) >= 2 and len(a_wyj_s) >= 2:
            h_s_win = sum((h_dom_s['Shots_H'] - h_dom_s['Shots_A']) > 0)
            a_s_lose = sum((a_wyj_s['Shots_A'] - a_wyj_s['Shots_H']) < 0)
            
            h_len, a_len = len(h_dom_s), len(a_wyj_s)
            
            h_s_win_prob = (h_s_win + 1.5 * 0.6) / (h_len + 1.5) if h_len < 12 else h_s_win / h_len
            a_s_lose_prob = (a_s_lose + 1.5 * 0.6) / (a_len + 1.5) if a_len < 12 else a_s_lose / a_len
            prob_h_s = (h_s_win_prob * 4.0 + a_s_lose_prob * 1.0) / 5.0

            h_st_win = sum((h_dom_s['ShotsTarget_H'] - h_dom_s['ShotsTarget_A']) > 0)
            a_st_lose = sum((a_wyj_s['ShotsTarget_A'] - a_wyj_s['ShotsTarget_H']) < 0)
            
            h_st_win_prob = (h_st_win + 1.5 * 0.6) / (h_len + 1.5) if h_len < 12 else h_st_win / h_len
            a_st_lose_prob = (a_st_lose + 1.5 * 0.6) / (a_len + 1.5) if a_len < 12 else a_st_lose / a_len
            prob_h_st = (h_st_win_prob * 4.0 + a_st_lose_prob * 1.0) / 5.0
            
            any_sm = h_len < 12 or a_len < 12

            if prob_h_s > 0.80:
                est_odd_s = KOTWICE_KURSOWE.get("S_1", 1.34)
                h_stat = get_tier_stats(h_dom_s, True, league, team_tiers, None, lambda r: (pd.to_numeric(r['Shots_H'], errors='coerce') - pd.to_numeric(r['Shots_A'], errors='coerce')) > 0)
                a_stat = get_tier_stats(a_wyj_s, False, league, team_tiers, None, lambda r: (pd.to_numeric(r['Shots_A'], errors='coerce') - pd.to_numeric(r['Shots_H'], errors='coerce')) < 0)
                arg = f"Strzały Ogółem: Gosp ({h_tier}) win u siebie: {h_stat}. Gość ({a_tier}) brak winu wyjazd: {a_stat}."
                if any_sm: arg += " | ⚠️ Bayes"
                add_pred_local("Shots Pro", "S_1", round(prob_h_s*100, 1), round(est_odd_s, 2), arg)
            
            if prob_h_st > 0.80:
                est_odd_st = KOTWICE_KURSOWE.get("ST_1", 1.64)
                h_stat_st = get_tier_stats(h_dom_s, True, league, team_tiers, None, lambda r: (pd.to_numeric(r['ShotsTarget_H'], errors='coerce') - pd.to_numeric(r['ShotsTarget_A'], errors='coerce')) > 0)
                a_stat_st = get_tier_stats(a_wyj_s, False, league, team_tiers, None, lambda r: (pd.to_numeric(r['ShotsTarget_A'], errors='coerce') - pd.to_numeric(r['ShotsTarget_H'], errors='coerce')) < 0)
                arg_st = f"Strzały Celne: Gosp ({h_tier}) win u siebie: {h_stat_st}. Gość ({a_tier}) brak winu wyjazd: {a_stat_st}."
                if any_sm: arg_st += " | ⚠️ Bayes"
                add_pred_local("Shots Pro", "ST_1", round(prob_h_st*100, 1), round(est_odd_st, 2), arg_st)

    # --- 6g. ZIMNY PRYSZNIC ---
    if h_tier in ['Koszyk 1', 'Koszyk 2'] and len(h_tot_all) > 0:
        last_m = h_tot_all.iloc[0] 
        if last_m['Away'] == home and last_m['FTHG'] >= last_m['FTAG']:
            opp_tier = team_tiers.get((last_m['League'], last_m['Home']), 'Koszyk 1')
            if opp_tier in ['Koszyk 4', 'Koszyk 5', 'Koszyk 6']:
                add_pred_local("Cold Shower", "1", 85.0, 1.15, f"Gospodarz ({h_tier}) szuka rewanżu po stracie pkt z ({opp_tier}).")

    # --- POST-PROCESSING: CZYSZCZENIE ŚMIECI (GARBAGE COLLECTOR) ---
    czy_jest_dobry_bb = any("BetBuilder Pro" in p['Engine'] for p in match_preds)
    
    for p in match_preds:
        if p['Typ'] in ZAKAZANE_TYPY_SOLO and p['Engine'] != "BetBuilder Pro":
            continue
            
        if czy_jest_dobry_bb and p['Engine'] in ['Goal Line Pro', 'Multigol']:
            try:
                if float(p['Kurs_Szac']) < 1.35: continue 
            except: pass
            
        all_generated_predictions.append(p)


# ==========================================================
# 7. SYSTEM ŚLEDZENIA SKUTECZNOŚCI I YIELDU (BACKTESTER)
# ==========================================================
print("Inicjalizacja Modułu Backtestingu (Śledzenie Skuteczności)...")

scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file("credentials.json", scopes=scope) if os.path.exists("credentials.json") else Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

cols_all_pred = ["Match_ID", "Zagrane", "Wyslij_AKO", "Kupon_ID", "Termin", "Data", "Godzina", "Liga", "Gospodarz", "Gość", "Engine", "Typ", "Szansa", "Kurs_Szac", "Argumentacja", "Przedzial_Kursowy", "Consensus_Score", "Status", "Profit", "Yield_Wplyw"]
cols_historia = ["Match_ID", "Zagrane", "Kupon_ID", "Data", "Godzina", "Liga", "Gospodarz", "Gość", "Engine", "Typ", "Szansa", "Kurs_Szac", "Argumentacja", "Przedzial_Kursowy", "Consensus_Score", "Status", "Profit", "Yield_Wplyw"]

df_all_predictions = pd.DataFrame(all_generated_predictions)

if not df_all_predictions.empty:
    df_all_predictions['Przedzial_Kursowy'] = df_all_predictions.apply(global_recalc_przedzial, axis=1)
    consensus_counts = df_all_predictions.groupby('Match_ID').size().to_dict()
    df_all_predictions['Consensus_Score'] = df_all_predictions['Match_ID'].map(consensus_counts)
    
    df_all_predictions['Unikalny_Klucz'] = df_all_predictions['Match_ID'].astype(str) + "_" + df_all_predictions['Engine'].astype(str) + "_" + df_all_predictions['Typ'].astype(str)
    
    map_wyslij, map_zagrane, map_kupon = {}, {}, {}
    try:
        old_all_ws = spreadsheet.worksheet("All_Predictions").get_all_records()
        if old_all_ws:
            old_all_df = pd.DataFrame(old_all_ws)
            old_all_df['Unikalny_Klucz'] = old_all_df['Match_ID'].astype(str) + "_" + old_all_df['Engine'].astype(str) + "_" + old_all_df['Typ'].astype(str)
            if 'Wyslij_AKO' in old_all_df.columns: map_wyslij = dict(zip(old_all_df['Unikalny_Klucz'], old_all_df['Wyslij_AKO']))
            if 'Zagrane' in old_all_df.columns: map_zagrane = dict(zip(old_all_df['Unikalny_Klucz'], old_all_df['Zagrane']))
            if 'Kupon_ID' in old_all_df.columns: map_kupon = dict(zip(old_all_df['Unikalny_Klucz'], old_all_df['Kupon_ID']))
    except: pass
    
    df_all_predictions['Wyslij_AKO'] = df_all_predictions['Unikalny_Klucz'].map(map_wyslij).fillna("")
    df_all_predictions['Zagrane'] = df_all_predictions['Unikalny_Klucz'].map(map_zagrane).fillna("")
    df_all_predictions['Kupon_ID'] = df_all_predictions['Unikalny_Klucz'].map(map_kupon).fillna("")
    df_all_predictions['Status'] = "W OCZEKIWANIU"
    df_all_predictions['Profit'] = ""
    df_all_predictions['Yield_Wplyw'] = ""
    
    for col in cols_all_pred:
        if col not in df_all_predictions.columns:
            df_all_predictions[col] = ""
    df_all_predictions = df_all_predictions[cols_all_pred]
else:
    df_all_predictions = pd.DataFrame(columns=cols_all_pred)

try:
    ws_historia = spreadsheet.worksheet("Historia_Typow")
    historia_dane = ws_historia.get_all_values()
    if len(historia_dane) > 0: df_historia = pd.DataFrame(historia_dane[1:], columns=historia_dane[0])
    else: df_historia = pd.DataFrame(columns=cols_historia)
except gspread.exceptions.WorksheetNotFound:
    spreadsheet.add_worksheet(title="Historia_Typow", rows=10000, cols=len(cols_historia))
    ws_historia = spreadsheet.worksheet("Historia_Typow")
    df_historia = pd.DataFrame(columns=cols_historia)

for col in cols_historia:
    if col not in df_historia.columns: df_historia[col] = ""
df_historia = df_historia[cols_historia]

if not df_all_predictions.empty:
    nowe_typy_df = df_all_predictions.copy()
    if 'Termin' in nowe_typy_df.columns: nowe_typy_df = nowe_typy_df.drop(columns=['Termin'])
    if 'Wyslij_AKO' in nowe_typy_df.columns: nowe_typy_df = nowe_typy_df.drop(columns=['Wyslij_AKO'])
        
    for col in cols_historia:
        if col not in nowe_typy_df.columns: nowe_typy_df[col] = ""
    nowe_typy_df = nowe_typy_df[cols_historia]
    
    if not df_historia.empty:
        df_historia['Unikalny_Klucz'] = df_historia['Match_ID'].astype(str) + "_" + df_historia['Engine'].astype(str) + "_" + df_historia['Typ'].astype(str)
        df_historia = df_historia.drop_duplicates(subset=['Unikalny_Klucz'], keep='last')
        
        nowe_typy_df['Unikalny_Klucz'] = nowe_typy_df['Match_ID'].astype(str) + "_" + nowe_typy_df['Engine'].astype(str) + "_" + nowe_typy_df['Typ'].astype(str)
        
        w_oczek_mask = df_historia['Status'] == "W OCZEKIWANIU"
        if w_oczek_mask.any():
            map_szansa = nowe_typy_df.set_index('Unikalny_Klucz')['Szansa'].to_dict()
            map_kurs = nowe_typy_df.set_index('Unikalny_Klucz')['Kurs_Szac'].to_dict()
            map_arg = nowe_typy_df.set_index('Unikalny_Klucz')['Argumentacja'].to_dict()
            map_przedzial = nowe_typy_df.set_index('Unikalny_Klucz')['Przedzial_Kursowy'].to_dict()
            map_consensus = nowe_typy_df.set_index('Unikalny_Klucz')['Consensus_Score'].to_dict()
            map_kupon_upd = nowe_typy_df.set_index('Unikalny_Klucz')['Kupon_ID'].to_dict()
            map_zagrane_upd = nowe_typy_df.set_index('Unikalny_Klucz')['Zagrane'].to_dict()
            
            for idx in df_historia[w_oczek_mask].index:
                klucz = df_historia.at[idx, 'Unikalny_Klucz']
                if klucz in map_szansa:
                    df_historia.at[idx, 'Szansa'] = str(map_szansa[klucz])
                    df_historia.at[idx, 'Kurs_Szac'] = str(map_kurs[klucz])
                    df_historia.at[idx, 'Argumentacja'] = str(map_arg[klucz])
                    df_historia.at[idx, 'Przedzial_Kursowy'] = str(map_przedzial.get(klucz, ""))
                    df_historia.at[idx, 'Consensus_Score'] = str(map_consensus.get(klucz, ""))
                        
                    k_id_val = map_kupon_upd.get(klucz, "")
                    if k_id_val and str(df_historia.at[idx, 'Kupon_ID']).strip() == "":
                        df_historia.at[idx, 'Kupon_ID'] = str(k_id_val)
                        
                    zag_val = map_zagrane_upd.get(klucz, "")
                    if zag_val:
                        df_historia.at[idx, 'Zagrane'] = str(zag_val)

        do_dodania = nowe_typy_df[~nowe_typy_df['Unikalny_Klucz'].isin(df_historia['Unikalny_Klucz'])].copy()
        do_dodania = do_dodania.drop(columns=['Unikalny_Klucz'])
        df_historia = df_historia.drop(columns=['Unikalny_Klucz'])
    else:
        do_dodania = nowe_typy_df.copy()
        
    df_historia = pd.concat([df_historia, do_dodania], ignore_index=True)

# Ewaluacja statusu w locie
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
                        kurs_str = str(row["Kurs_Szac"]).replace(',', '.').strip()
                        try: kurs = float(kurs_str)
                        except: kurs = 1.0
                        
                        if nowy_status == "WYGRANA":
                            profit = round(kurs - 1.0, 2)
                            df_historia.at[idx, "Profit"] = str(profit)
                            df_historia.at[idx, "Yield_Wplyw"] = str(round(profit*100, 1))
                        elif nowy_status == "PRZEGRANA":
                            df_historia.at[idx, "Profit"] = "-1.0"
                            df_historia.at[idx, "Yield_Wplyw"] = "-100.0"
                    except: pass

# --- 7b. SYSTEM ŚLEDZENIA AKO (PORTFEL REALNY) ---
cols_ako = ["Kupon_ID", "Data_Zawarcia", "Mecze_Skrot", "Liczba_Zdarzen", "Kurs_AKO", "Stawka", "Jednostki", "Status_AKO", "Wygrana_Brutto", "Profit_Netto", "Wyslij_Podsumowanie", "Telegram_Status"]
try:
    ws_ako = spreadsheet.worksheet("Kupony_AKO")
    ako_dane = ws_ako.get_all_values()
    if len(ako_dane) > 0: df_ako = pd.DataFrame(ako_dane[1:], columns=ako_dane[0])
    else: df_ako = pd.DataFrame(columns=cols_ako)
except gspread.exceptions.WorksheetNotFound:
    spreadsheet.add_worksheet(title="Kupony_AKO", rows=1000, cols=15)
    ws_ako = spreadsheet.worksheet("Kupony_AKO")
    df_ako = pd.DataFrame(columns=cols_ako)

for col in cols_ako:
    if col not in df_ako.columns: df_ako[col] = ""
df_ako = df_ako[cols_ako]

user_stakes, user_units, user_pods, user_tel_stat = {}, {}, {}, {}
if not df_ako.empty:
    user_stakes = dict(zip(df_ako['Kupon_ID'], df_ako['Stawka']))
    user_units = dict(zip(df_ako['Kupon_ID'], df_ako.get('Jednostki', ['1j']*len(df_ako))))
    user_pods = dict(zip(df_ako['Kupon_ID'], df_ako.get('Wyslij_Podsumowanie', ['']*len(df_ako))))
    user_tel_stat = dict(zip(df_ako['Kupon_ID'], df_ako.get('Telegram_Status', ['']*len(df_ako))))

if not df_historia.empty:
    mask_zagrane = df_historia['Zagrane'].astype(str).str.upper().isin(['TRUE', 'PRAWDA', '1', 'TAK'])
    mask_bez_id = df_historia['Kupon_ID'].astype(str).str.strip() == ""
    
    try: mask_dzis = pd.to_datetime(df_historia['Data'], errors='coerce').dt.date >= datetime.now().date()
    except: mask_dzis = pd.Series([True]*len(df_historia))
        
    mask_do_zaktualizowania = mask_zagrane & mask_bez_id & mask_dzis
    
    if mask_do_zaktualizowania.any():
        nowy_id = f"AKO_{datetime.now().strftime('%y%m%d_%H%M')}"
        df_historia.loc[mask_do_zaktualizowania, 'Kupon_ID'] = nowy_id

    df_historia['Unikalny_Klucz'] = df_historia['Match_ID'].astype(str) + "_" + df_historia['Engine'].astype(str) + "_" + df_historia['Typ'].astype(str)
    hist_kupon_map = df_historia[df_historia['Kupon_ID'].astype(str).str.strip() != ""].set_index('Unikalny_Klucz')['Kupon_ID'].to_dict()
    
    if not df_all_predictions.empty:
        df_all_predictions['Unikalny_Klucz'] = df_all_predictions['Match_ID'].astype(str) + "_" + df_all_predictions['Engine'].astype(str) + "_" + df_all_predictions['Typ'].astype(str)
        df_all_predictions['Kupon_ID'] = df_all_predictions['Unikalny_Klucz'].map(hist_kupon_map).fillna(df_all_predictions['Kupon_ID'])

    nowe_ako_list = []
    grupy_ako = df_historia[df_historia['Kupon_ID'].astype(str).str.strip() != ""].groupby('Kupon_ID')

    for k_id, group in grupy_ako:
        data_zawarcia = group['Data'].min()
        liczba_zdarzen = len(group)
        mecze_skrot = " | ".join(group['Gospodarz'].str[:3] + "-" + group['Gość'].str[:3])
        
        kurs_ako = 1.0
        for _, r in group.iterrows():
            kr_str = str(r['Kurs_Szac']).replace(',', '.').strip()
            try: 
                kr = float(kr_str)
                if 1.0 < kr < 50.0: kurs_ako *= kr
            except: pass
        kurs_ako = round(kurs_ako, 2)
        
        statusy = group['Status'].tolist()
        if "PRZEGRANA" in statusy: status_ako = "PRZEGRANA"
        elif "W OCZEKIWANIU" in statusy: status_ako = "W OCZEKIWANIU"
        elif all(s == "WYGRANA" for s in statusy): status_ako = "WYGRANA"
        else: status_ako = "ZWRÓCONY"
        
        stawka_str = str(user_stakes.get(k_id, "100")).replace(',', '.')
        if stawka_str.strip() == "": stawka_str = "100"
        try: stawka = float(stawka_str)
        except: stawka = 100.0
        
        jednostki_str = str(user_units.get(k_id, "1j"))
        wyslij_pod = str(user_pods.get(k_id, ""))
        tel_status = str(user_tel_stat.get(k_id, ""))
        
        wygrana_brutto = round(kurs_ako * stawka * 0.88, 2) if status_ako == "WYGRANA" else 0.0
        
        if status_ako == "WYGRANA": profit = round(wygrana_brutto - stawka, 2)
        elif status_ako == "PRZEGRANA": profit = -stawka
        else: profit = 0.0
        
        nowe_ako_list.append([k_id, data_zawarcia, mecze_skrot, liczba_zdarzen, kurs_ako, stawka, jednostki_str, status_ako, wygrana_brutto, profit, wyslij_pod, tel_status])

    df_ako = pd.DataFrame(nowe_ako_list, columns=cols_ako)
    df_ako = df_ako.sort_values(by="Data_Zawarcia", ascending=False)

# --- NAPRAWA SORTOWANIA W HISTORIA TYPÓW ---
if not df_historia.empty:
    mask_puste_daty_h = df_historia['Data'].astype(str).str.strip().isin(['', 'nan', 'None', 'Nieznany'])
    df_historia['Data_Sort'] = pd.to_datetime(df_historia['Data'].astype(str) + ' ' + df_historia['Godzina'].astype(str).replace(['', '-', 'nan', 'None'], '00:00'), errors='coerce')
    df_historia.loc[mask_puste_daty_h, 'Data_Sort'] = pd.NaT 
    
    mask_oczek = df_historia['Status'] == 'W OCZEKIWANIU'
    df_oczek = df_historia[mask_oczek].sort_values(by=['Data_Sort'], ascending=[True], na_position='last')
    df_rozst = df_historia[~mask_oczek].sort_values(by=['Data_Sort'], ascending=[False], na_position='last')
    df_historia = pd.concat([df_oczek, df_rozst]).drop(columns=['Data_Sort', 'Unikalny_Klucz'], errors='ignore')

# --- NAPRAWA SORTOWANIA W ALL PREDICTIONS ---
if not df_all_predictions.empty: 
    mask_puste_daty_p = df_all_predictions['Data'].astype(str).str.strip().isin(['', 'nan', 'None', 'Nieznany'])
    df_all_predictions['Data_Sort'] = pd.to_datetime(df_all_predictions['Data'].astype(str) + ' ' + df_all_predictions['Godzina'].astype(str).replace(['', '-', 'nan', 'None'], '00:00'), errors='coerce')
    df_all_predictions.loc[mask_puste_daty_p, 'Data_Sort'] = pd.NaT
    
    df_all_predictions = df_all_predictions.sort_values(by=["Data_Sort", "Szansa"], ascending=[True, False], na_position='last').drop(columns=['Data_Sort', 'Unikalny_Klucz'], errors='ignore')

# ==========================================
# 8. WYSYŁKA GOOGLE SHEETS
# ==========================================
all_sheets = ["Summary", "Fixtures", "Results", "League_Tables", "H2H_Mecze", "Historia_Typow", "All_Predictions", "Kupony_AKO"]

for sheet_name in all_sheets:
    try: spreadsheet.worksheet(sheet_name)
    except: spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=30)

print("Wysyłam Tabele Ligowe i Czysty Terminarz...")
time.sleep(1.5)
spreadsheet.worksheet("Fixtures").clear()
if not fixtures_clean.empty: spreadsheet.worksheet("Fixtures").update(prepare_for_gsheets(fixtures_clean))

spreadsheet.worksheet("Results").clear()
if not results_clean.empty: spreadsheet.worksheet("Results").update(prepare_for_gsheets(results_clean))

spreadsheet.worksheet("League_Tables").clear()
if not league_tables.empty: spreadsheet.worksheet("League_Tables").update(prepare_for_gsheets(league_tables))

spreadsheet.worksheet("H2H_Mecze").clear()
if not df_h2h.empty: spreadsheet.worksheet("H2H_Mecze").update(prepare_for_gsheets(df_h2h))

print("Wysyłam Logi Systemu Backtestingu (Historia_Typow)...")
time.sleep(1.5)
ws_historia.clear()
if not df_historia.empty: ws_historia.update(prepare_for_gsheets(df_historia))

print("Wysyłam Moduł Portfela AKO (Kupony_AKO)...")
time.sleep(1.5)
ws_ako.clear()
if not df_ako.empty: ws_ako.update(prepare_for_gsheets(df_ako))

print("Wysyłam Ujednoliconą Listę Wszystkich Predykcji (All_Predictions)...")
time.sleep(1.5)
spreadsheet.worksheet("All_Predictions").clear()
if not df_all_predictions.empty: spreadsheet.worksheet("All_Predictions").update(prepare_for_gsheets(df_all_predictions))

summary_data = [
    ["==== PODSUMOWANIE OGÓLNE ====", "", ""],
    ["Ostatnia aktualizacja", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""],
    ["Fixtures Czyste", len(fixtures_clean), ""],
    ["Results Zintegrowane", len(results_clean), ""],
    ["Przetworzone Typy w Historii", len(df_historia), ""],
    ["Wygenerowane Predykcje (Suma)", len(df_all_predictions), ""]
]
time.sleep(1.5)
spreadsheet.worksheet("Summary").clear()
spreadsheet.worksheet("Summary").update(summary_data)

print("\n" + "=" * 60)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Wdrożono analityczną kalibrację kursów, szczegółową argumentację i zaktualizowano sortowanie.")
print("=" * 60)
