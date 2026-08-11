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
        if len(date_part.split('.')) >= 2:
            try:
                date_clean = date_part.rstrip(".")
                sub_p = date_clean.split(".")
                if len(sub_p) == 2:
                    d, m = int(sub_p[0]), int(sub_p[1])
                    target_year = today.year
                    if m < today.month and (today.month - m) > 6:
                        target_year += 1
                    return datetime(target_year, m, d).strftime('%Y-%m-%d'), time_part
                elif len(sub_p) == 3:
                    d, m, y = int(sub_p[0]), int(sub_p[1]), int(sub_p[2])
                    if y < 100: y += 2000
                    return datetime(y, m, d).strftime('%Y-%m-%d'), time_part
            except: pass
            
    else:
        if len(value.split('.')) >= 2:
            try:
                date_clean = value.rstrip(".")
                sub_p = date_clean.split(".")
                if len(sub_p) == 2:
                    d, m = int(sub_p[0]), int(sub_p[1])
                    target_year = today.year
                    if m < today.month and (today.month - m) > 6:
                        target_year += 1
                    return datetime(target_year, m, d).strftime('%Y-%m-%d'), ""
                elif len(sub_p) == 3:
                    d, m, y = int(sub_p[0]), int(sub_p[1]), int(sub_p[2])
                    if y < 100: y += 2000
                    return datetime(y, m, d).strftime('%Y-%m-%d'), ""
            except: pass
            
    return value, ""

def categorize_date(d_str):
    if pd.isna(d_str) or str(d_str).strip() in ["", "nan", "NaT", "None", "Nieznany"]: return "Nieznany"
    try:
        d = pd.to_datetime(str(d_str), format='%Y-%m-%d', errors='coerce')
        if pd.isna(d): d = pd.to_datetime(str(d_str), errors='coerce', format='mixed')
        if pd.isna(d): return "Nieznany"
        d_date = d.date()
        today_date = datetime.now().date()
        delta = (d_date - today_date).days
        
        if delta < 0: return "przeszłość"
        if delta == 0: return "dziś"
        if delta == 1: return "jutro"
        if delta == 2: return "za 2 dni"
        if delta == 3: return "za 3 dni"
        if delta == 4: return "za 4 dni"
        if delta == 5: return "za 5 dni"
        if delta == 6: return "za 6 dni"
        if delta == 7: return "za 7 dni"
        if delta > 7: return "za ponad tydzień"
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

def calc_betbuilder_copula(odds_list, rho=0.65):
    if not odds_list: return 1.0
    q_list = [1.0 / o for o in odds_list if o > 0]
    if not q_list: return 1.0
    q_list.sort() 
    
    q_joint = q_list[0]
    for q_next in q_list[1:]:
        gamma = 1.0 - rho * (1.0 - min(q_joint, q_next))
        q_joint = q_joint * (q_next ** gamma)
        
    final_odd = 1.0 / q_joint if q_joint > 0 else 99.0
    return max(1.015, round(final_odd, 2))

# ==========================================================
# FUNKCJE ANALITYCZNE, EVALUACJA I KONTROLA RYZYKA
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
                raw_datetime = date_cell.get_text(strip=True)
                local_data.append(["Fixture", league, raw_datetime, home, away, "", odd1, oddx, odd2])
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
            return round(((1/o1)+(1/ox)+(1/o2)-1.0)*100, 2)
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
            return round(((1/o1)+(1/ox)+(1/o2)-1.0)*100, 2)
        except: return ""
    fixtures_df['Marża'] = fixtures_df.apply(get_margin, axis=1)

results_clean = results_df[list(golden_cols.keys()) + ['HT_Total', 'Total_Corners', 'Marża']].rename(columns=golden_cols) if not results_df.empty else pd.DataFrame(columns=list(golden_cols.values()) + ['HT_Total', 'Total_Corners', 'Marża'])
fixtures_clean = fixtures_df[['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd1', 'OddX', 'Odd2', 'Marża']].rename(columns={'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2'}) if not fixtures_df.empty else pd.DataFrame(columns=['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd_1', 'Odd_X', 'Odd_2', 'Marża'])

# --- NAPRAWA I FILTROWANIE FIXTURES: USUNIĘCIE "Nieznany" ORAZ > 7 DNI ---
if not fixtures_clean.empty:
    fixtures_clean['Data_Parsed'] = pd.to_datetime(fixtures_clean['Date'], errors='coerce')
    today_dt = pd.to_datetime(datetime.now().date())
    delta_days = (fixtures_clean['Data_Parsed'] - today_dt).dt.days
    
    mask_valid_fixture = (fixtures_clean['Termin'] != "Nieznany") & \
                         (fixtures_clean['Termin'] != "przeszłość") & \
                         (fixtures_clean['Termin'] != "za ponad tydzień") & \
                         (delta_days >= 0) & (delta_days <= 7)
                         
    fixtures_clean = fixtures_clean[mask_valid_fixture].drop(columns=['Data_Parsed'])

# ==========================================
# 5. GENEROWANIE TABEL LIGOWYCH
# ==========================================
print("Generowanie inteligentnych tabel ligowych (6 Koszyków)...")
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

# ==========================================================
# 6. SILNIKI PREDYKCYJNE
# ==========================================================
all_generated_predictions = []

KOTWICE_KURSOWE = {
    'O0.5': 1.03, 'U3.5': 1.31, 'U4.5': 1.10, 'U5.5': 1.015, 'U6.5': 1.01,
    'HT_U1.5': 1.42, 'HT_U2.5': 1.09, 'HT_U3.5': 1.01, 'HT_U4.5': 1.01,
    '2H_U3.5': 1.02, '2H_U4.5': 1.01,
    'O0.5+U5.5': 1.09, 'O0.5+U6.5': 1.05,
    'C_U8.5': 2.78, 'C_U9.5': 2.02, 'C_U10.5': 1.59, 'C_U11.5': 1.33,
    'C_U12.5': 1.17, 'C_U13.5': 1.09, 'C_U14.5': 1.04, 
    'HC_U4.5': 2.59, 'HC_U5.5': 1.75, 'HC_U6.5': 1.35, 'HC_U7.5': 1.14, 'HC_U8.5': 1.03,
    'AC_U4.5': 1.74, 'AC_U5.5': 1.32, 'AC_U6.5': 1.11, 'AC_U7.5': 1.01, 'AC_U8.5': 1.01, 
    'HC_O4.5': 1.44, 'AC_O4.5': 1.98, 
    'HU2.5': 1.12, 'HU3.5': 1.01, 'HU4.5': 1.01,
    'AU2.5': 1.12, 'AU3.5': 1.01, 'AU4.5': 1.01,
    'S_1': 1.34, 'ST_1': 1.64
}

SZABLONY_PREMIUM = [
    "O0.5+U5.5+HT_U3.5+2H_U3.5+HU3.5+AU3.5",
    "O0.5+U4.5+HT_U3.5+2H_U3.5+HU3.5+AU3.5",
    "U6.5+HT_U3.5+2H_U4.5+HU4.5+AU3.5",
    "C_U11.5+HC_U8.5",
    "U4.5+HT_U2.5+2H_U3.5+HU3.5+AU3.5"
]

def add_pred(match_id, termin, date, time, league, home, away, engine, typ, szansa, kurs_szac, arg):
    typ_k = str(typ).strip()
    try: kurs_bazowy = float(str(kurs_szac).replace(',', '.'))
    except: kurs_bazowy = 1.05
    is_anchor = False

    if engine == "BetBuilder Pro":
        skladniki = typ_k.split("+")
        kursy_skladowe = [KOTWICE_KURSOWE.get(sk.strip(), 1.05) for sk in skladniki]
        rho_val = 0.22 if any(c in typ_k for c in ["C_U", "HC_", "AC_"]) else 0.65
        kurs_docelowy = calc_betbuilder_copula(kursy_skladowe, rho=rho_val)
        
    elif "+" in typ_k and "O0.5+U" not in typ_k: 
        skladniki = typ_k.split("+")
        kursy_skladowe = [KOTWICE_KURSOWE.get(sk.strip(), 1.05) for sk in skladniki]
        rho_val = 0.22 if any(c in typ_k for c in ["C_U", "HC_", "AC_"]) else 0.65
        kurs_docelowy = calc_betbuilder_copula(kursy_skladowe, rho=rho_val)
    else:
        if typ_k in KOTWICE_KURSOWE:
            kurs_docelowy = KOTWICE_KURSOWE[typ_k]
            is_anchor = True
        else: 
            kurs_docelowy = kurs_bazowy

    if not is_anchor:
        if kurs_docelowy >= 1.50: kurs_docelowy = round(kurs_docelowy * 0.95, 2)
        elif 1.20 <= kurs_docelowy < 1.50: kurs_docelowy = round(kurs_docelowy * 0.975, 2)
        
    if kurs_docelowy < 1.01: 
        kurs_docelowy = 1.01

    prob_decimal = float(szansa) / 100.0
    if prob_decimal >= 0.95 and kurs_docelowy >= 1.20:
        risk_tag = "🥇 1. ZŁOTY TYP"
    elif prob_decimal >= 0.95 and kurs_docelowy >= 1.15:
        risk_tag = "🥈 2. SREBRNY TYP"
    elif prob_decimal >= 0.95 and kurs_docelowy >= 1.10:
        risk_tag = "🥉 3. BRĄZOWY TYP"
    elif prob_decimal >= 0.95:
        risk_tag = "SAFE (95%+)"
    elif prob_decimal >= 0.85:
        risk_tag = "STANDARD (85-94%)"
    elif prob_decimal >= 0.75:
        risk_tag = "VALUE (75-84%)"
    else:
        risk_tag = "RISK (70-74%)"

    clean_arg = str(arg)
    if clean_arg.startswith("["):
        arg_final = re.sub(r"^\[.*?\]\s*", f"[{risk_tag}] ", clean_arg)
    else:
        arg_final = f"[{risk_tag}] {clean_arg}"

    all_generated_predictions.append({
        "Match_ID": match_id, "Termin": termin, "Data": date, "Godzina": time, "Liga": league, 
        "Gospodarz": home, "Gość": away, "Engine": engine, "Typ": typ, 
        "Szansa": szansa, "Kurs_Szac": kurs_docelowy, "Argumentacja": arg_final
    })

print("Uruchamiam Modele Predykcyjne...")

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    match_id, d_termin, d_date, d_time = row['Match_ID'], row['Termin'], row['Date'], row['Time']
    
    o1_raw, ox_raw, o2_raw = row['Odd_1'], row['Odd_X'], row['Odd_2']

    h_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == home))].copy()
    a_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == away) | (valid_matches['Away'] == away))].copy()
    h_dom = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Home'] == home)].copy()
    a_wyj = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Away'] == away)].copy()
    
    h_tier = team_tiers.get((league, home), 'Koszyk 3')
    a_tier = team_tiers.get((league, away), 'Koszyk 3')

    if len(h_dom) > 0:
        h_dom['HT_Total'] = pd.to_numeric(h_dom['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(h_dom['HTAG'], errors='coerce').fillna(0)
        h_dom['2H_Total'] = pd.to_numeric(h_dom['Total_Goals'], errors='coerce').fillna(0) - h_dom['HT_Total']
    if len(a_wyj) > 0:
        a_wyj['HT_Total'] = pd.to_numeric(a_wyj['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(a_wyj['HTAG'], errors='coerce').fillna(0)
        a_wyj['2H_Total'] = pd.to_numeric(a_wyj['Total_Goals'], errors='coerce').fillna(0) - a_wyj['HT_Total']

    if len(h_tot_all) > 0:
        h_tot_all['Team_GF'] = np.where(h_tot_all['Home'] == home, h_tot_all['FTHG'], h_tot_all['FTAG'])
        h_tot_all['Team_GA'] = np.where(h_tot_all['Home'] == home, h_tot_all['FTAG'], h_tot_all['FTHG'])
    if len(a_tot_all) > 0:
        a_tot_all['Team_GF'] = np.where(a_tot_all['Home'] == away, a_tot_all['FTHG'], a_tot_all['FTAG'])
        a_tot_all['Team_GA'] = np.where(a_tot_all['Home'] == away, a_tot_all['FTAG'], a_tot_all['FTHG'])

    # 6a. 1X PRO
    lg_matches = valid_matches[valid_matches['Base_League'] == fixture_base]
    if len(lg_matches) >= 15 and len(h_tot_all) >= 3 and len(a_tot_all) >= 3:
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
            try:
                o1 = float(str(o1_raw).replace(',','.'))
                ox = float(str(ox_raw).replace(',','.'))
                o2 = float(str(o2_raw).replace(',','.'))
                if typ_kod == "1X": fair_odd = round((o1 * ox) / (o1 + ox), 2)
                else: fair_odd = round((o2 * ox) / (o2 + ox), 2)
            except: fair_odd = round(1 / final_prob, 2)
            
            if typ_kod == "1X":
                h_1x_c = sum(h_dom['FTHG'] >= h_dom['FTAG']) if len(h_dom) > 0 else 0
                a_win_c = sum(a_wyj['FTAG'] > a_wyj['FTHG']) if len(a_wyj) > 0 else 0
                h_1x_tot = sum(h_tot_all['Team_GF'] >= h_tot_all['Team_GA'])
                a_win_tot = sum(a_tot_all['Team_GF'] > a_tot_all['Team_GA'])
                arg = f"Gosp ({h_tier}) dom bez porażki {h_1x_c}/{len(h_dom)} (Ogółem: {h_1x_tot}/{len(h_tot_all)}). Gość ({a_tier}) wyjazd wygrał {a_win_c}/{len(a_wyj)}."
            else:
                a_x2_c = sum(a_wyj['FTAG'] >= a_wyj['FTHG']) if len(a_wyj) > 0 else 0
                h_win_c = sum(h_dom['FTHG'] > h_dom['FTAG']) if len(h_dom) > 0 else 0
                a_x2_tot = sum(a_tot_all['Team_GF'] >= a_tot_all['Team_GA'])
                h_win_tot = sum(h_tot_all['Team_GF'] > h_tot_all['Team_GA'])
                arg = f"Gość ({a_tier}) wyjazd bez porażki {a_x2_c}/{len(a_wyj)} (Ogółem: {a_x2_tot}/{len(a_tot_all)}). Gosp ({h_tier}) dom wygrał {h_win_c}/{len(h_dom)}."
                
            add_pred(match_id, d_termin, d_date, d_time, league, home, away, "1X Pro", typ_kod, round(final_prob*100, 1), round(fair_odd, 2), arg)

    # 6b. GOAL LINE PRO
    if len(h_tot_all) >= 5 and len(a_tot_all) >= 5:
        for line in [2.5, 3.5, 4.5, 5.5, 6.5]:
            prob_h_u, h_th, h_tl, h_sm = get_weighted_stats(h_dom if len(h_dom)>=3 else h_tot_all, 'Total_Goals', lambda x: pd.notna(x) and x < line, prior_prob=0.75)
            prob_a_u, a_th, a_tl, a_sm = get_weighted_stats(a_wyj if len(a_wyj)>=3 else a_tot_all, 'Total_Goals', lambda x: pd.notna(x) and x < line, prior_prob=0.75)
            avg_prob_u = (prob_h_u + prob_a_u) / 2
            if avg_prob_u >= 0.70:
                arg = f"U{line} | Ważone szanse: Gosp {round(prob_h_u*100)}%, Gość {round(prob_a_u*100)}%."
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Goal Line Pro", f"U{line}", round(avg_prob_u*100, 1), KOTWICE_KURSOWE.get(f"U{line}", 1.10), arg)

        for line in [0.5, 1.5, 2.5]:
            prob_h_o, h_th, h_tl, h_sm = get_weighted_stats(h_dom if len(h_dom)>=3 else h_tot_all, 'Total_Goals', lambda x: pd.notna(x) and x > line, prior_prob=0.30)
            prob_a_o, a_th, a_tl, a_sm = get_weighted_stats(a_wyj if len(a_wyj)>=3 else a_tot_all, 'Total_Goals', lambda x: pd.notna(x) and x > line, prior_prob=0.30)
            avg_prob_o = (prob_h_o + prob_a_o) / 2
            if avg_prob_o >= 0.70: 
                arg = f"O{line} | Ważone szanse: Gosp {round(prob_h_o*100)}%, Gość {round(prob_a_o*100)}%."
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Goal Line Pro", f"O{line}", round(avg_prob_o*100, 1), KOTWICE_KURSOWE.get(f"O{line}", 1.10), arg)

    # 6c. BETBUILDER PRO
    if len(h_tot_all) >= 5 and len(a_tot_all) >= 5:
        for tpl in SZABLONY_PREMIUM:
            p_h, h_th, h_tl, h_sm = get_weighted_stats(h_dom if len(h_dom)>=3 else h_tot_all, None, lambda r, code=tpl: evaluate_bet(code, r) == "WYGRANA", prior_prob=0.85)
            p_a, a_th, a_tl, a_sm = get_weighted_stats(a_wyj if len(a_wyj)>=3 else a_tot_all, None, lambda r, code=tpl: evaluate_bet(code, r) == "WYGRANA", prior_prob=0.85)
            avg_p = (p_h + p_a) / 2
            
            if avg_p >= 0.80:
                kursy_skl = [KOTWICE_KURSOWE.get(sk.strip(), 1.05) for sk in tpl.split("+")]
                est_odd = calc_betbuilder_copula(kursy_skl, rho=0.45) 
                if est_odd < 1.15: est_odd = 1.15
                arg = f"Szablon Premium | Szansa bazowa układu: {round(avg_p*100)}%"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "BetBuilder Pro", tpl, round(avg_p*100, 1), round(est_odd, 2), arg)

    # 6d. MULTIGOL
    if len(h_tot_all) >= 5 and len(a_tot_all) >= 5:
        h_last_goals = get_last_match_goals(fixture_base, home)
        a_last_goals = get_last_match_goals(fixture_base, away)
        
        if h_last_goals == 0 or h_last_goals > 5 or a_last_goals == 0 or a_last_goals > 5:
            prob_h_15, _, _, _ = get_weighted_stats(h_dom if len(h_dom)>=3 else h_tot_all, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5, prior_prob=0.80)
            prob_a_15, _, _, _ = get_weighted_stats(a_wyj if len(a_wyj)>=3 else a_tot_all, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5, prior_prob=0.80)
            prob_1_5 = (prob_h_15 + prob_a_15) / 2
            
            prob_h_16, _, _, _ = get_weighted_stats(h_dom if len(h_dom)>=3 else h_tot_all, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 6, prior_prob=0.85)
            prob_a_16, _, _, _ = get_weighted_stats(a_wyj if len(a_wyj)>=3 else a_tot_all, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 6, prior_prob=0.85)
            prob_1_6 = (prob_h_16 + prob_a_16) / 2
            
            if prob_1_5 >= 0.80 or prob_1_6 >= 0.80:
                typ_kod, pewnosc = ("MG_1-5", prob_1_5) if prob_1_5 >= prob_1_6 else ("MG_1-6", prob_1_6)
                est_odd = round(1.0 + (((1/pewnosc) - 1.0) / 1.5), 2)
                arg = f"Regresja po anomalii bramkowej. Szansa: {round(pewnosc*100)}%"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Multigol", typ_kod, round(pewnosc*100, 1), round(est_odd, 2), arg)

    # 6e. CORNERS PRO
    valid_corners = valid_matches.dropna(subset=['Corners_H', 'Corners_A']).copy()
    if not valid_corners.empty:
        h_tot_all_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & ((valid_corners['Home'] == home) | (valid_corners['Away'] == home))]
        a_tot_all_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & ((valid_corners['Home'] == away) | (valid_corners['Away'] == away))]

        if len(h_tot_all_c) >= 3 and len(a_tot_all_c) >= 3:
            c_blocks_code, c_probs, c_odds, arg_c = [], [], [], []
            for line in [9.5, 10.5, 11.5, 12.5, 13.5, 14.5]:
                prob_h_c, _, _, _ = get_weighted_stats(h_tot_all_c, 'Total_Corners', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
                prob_a_c, _, _, _ = get_weighted_stats(a_tot_all_c, 'Total_Corners', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
                avg_p = (prob_h_c + prob_a_c) / 2
                if avg_p >= 0.82:
                    c_blocks_code.append(f"C_U{line}"); c_probs.append(avg_p); c_odds.append(round(1/(avg_p*0.90), 2))
                    arg_c.append(f"C_U{line} ({round(avg_p*100)}%)")
                    break

            if len(c_blocks_code) >= 1:
                est_odd = c_odds[0]
                if est_odd < 1.05: est_odd = 1.05
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Corners Pro", "+".join(c_blocks_code), round(np.mean(c_probs)*100, 1), round(est_odd, 2), " | ".join(arg_c))

# ==========================================================
# 7. SYSTEM ŚLEDZENIA SKUTECZNOŚCI I YIELDU (BACKTESTER)
# ==========================================================
print("Inicjalizacja Modułu Backtestingu (Śledzenie Skuteczności)...")

scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file("credentials.json", scopes=scope) if os.path.exists("credentials.json") else Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

cols_all_pred = ["Match_ID", "Zagrane", "Wyslij_AKO", "Kupon_ID", "Termin", "Data", "Godzina", "Liga", "Gospodarz", "Gość", "Engine", "Typ", "Szansa", "Kurs_Szac", "Argumentacja", "Przedzial_Kursowy", "Consensus_Score", "Status"]
cols_historia = ["Match_ID", "Zagrane", "Kupon_ID", "Data", "Godzina", "Liga", "Gospodarz", "Gość", "Engine", "Typ", "Szansa", "Kurs_Szac", "Argumentacja", "Przedzial_Kursowy", "Consensus_Score", "Status", "Profit", "Yield_Wplyw"]

df_all_predictions = pd.DataFrame(all_generated_predictions)

if not df_all_predictions.empty:
    # FILTR: Szansa >= 80%
    df_all_predictions['Szansa_num'] = pd.to_numeric(df_all_predictions['Szansa'], errors='coerce').fillna(0)
    df_all_predictions = df_all_predictions[df_all_predictions['Szansa_num'] >= 80.0]
    
    # FILTR: Brak meczów odległych o więcej niż 7 dni
    df_all_predictions['Data_Parsed'] = pd.to_datetime(df_all_predictions['Data'], errors='coerce')
    today_dt = pd.to_datetime(datetime.now().date())
    delta_pred = (df_all_predictions['Data_Parsed'] - today_dt).dt.days
    df_all_predictions = df_all_predictions[(delta_pred >= 0) & (delta_pred <= 7)]
    
    # LIMIT: Do 8 typów na spotkanie
    df_all_predictions['Kurs_num'] = pd.to_numeric(df_all_predictions['Kurs_Szac'].astype(str).str.replace(',', '.'), errors='coerce').fillna(0)
    df_all_predictions = df_all_predictions.sort_values(by=['Match_ID', 'Szansa_num', 'Kurs_num'], ascending=[True, False, False])
    df_all_predictions = df_all_predictions.groupby('Match_ID').head(8).reset_index(drop=True)
    
    df_all_predictions = df_all_predictions.drop(columns=['Szansa_num', 'Kurs_num', 'Data_Parsed'])
    
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
    
    for col in cols_all_pred:
        if col not in df_all_predictions.columns:
            df_all_predictions[col] = ""
    df_all_predictions = df_all_predictions[cols_all_pred]
else:
    df_all_predictions = pd.DataFrame(columns=cols_all_pred)

# WCZYTANIE HISTORII
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

# EWALUACJA WYROZLICZONYCH TYPÓW W HISTORII
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
                        kurs = float(kurs_str) if kurs_str else 1.0
                        if nowy_status == "WYGRANA":
                            profit = round(kurs - 1.0, 2)
                            df_historia.at[idx, "Profit"] = str(profit)
                            df_historia.at[idx, "Yield_Wplyw"] = str(round(profit*100, 1))
                        elif nowy_status == "PRZEGRANA":
                            df_historia.at[idx, "Profit"] = "-1.0"
                            df_historia.at[idx, "Yield_Wplyw"] = "-100.0"
                    except: pass

# PRZENOSZENIE TYLKO ZAKOŃCZONYCH / PRZESZŁYCH TYPÓW Z ALL_PREDICTIONS DO HISTORIA_TYPOW
if not df_all_predictions.empty and not results_clean.empty:
    meczowe_wyniki_ids = set(results_clean['Match_ID'].dropna().unique())
    
    typy_zakonczone = df_all_predictions[df_all_predictions['Match_ID'].isin(meczowe_wyniki_ids)].copy()
    
    if not typy_zakonczone.empty:
        typy_zakonczone['Profit'] = ""
        typy_zakonczone['Yield_Wplyw'] = ""
        
        for idx, row in typy_zakonczone.iterrows():
            m_data = results_clean[results_clean['Match_ID'] == row["Match_ID"]]
            if not m_data.empty:
                m_row = m_data.iloc[0]
                n_stat = evaluate_bet(row["Typ"], m_row)
                typy_zakonczone.at[idx, "Status"] = n_stat
                try:
                    k_val = float(str(row["Kurs_Szac"]).replace(',', '.'))
                    if n_stat == "WYGRANA":
                        typy_zakonczone.at[idx, "Profit"] = str(round(k_val - 1.0, 2))
                        typy_zakonczone.at[idx, "Yield_Wplyw"] = str(round((k_val - 1.0)*100, 1))
                    elif n_stat == "PRZEGRANA":
                        typy_zakonczone.at[idx, "Profit"] = "-1.0"
                        typy_zakonczone.at[idx, "Yield_Wplyw"] = "-100.0"
                except: pass

        for col in cols_historia:
            if col not in typy_zakonczone.columns: typy_zakonczone[col] = ""
        typy_zakonczone = typy_zakonczone[cols_historia]
        
        if not df_historia.empty:
            df_historia['Unikalny_Klucz'] = df_historia['Match_ID'].astype(str) + "_" + df_historia['Engine'].astype(str) + "_" + df_historia['Typ'].astype(str)
            typy_zakonczone['Unikalny_Klucz'] = typy_zakonczone['Match_ID'].astype(str) + "_" + typy_zakonczone['Engine'].astype(str) + "_" + typy_zakonczone['Typ'].astype(str)
            
            do_dodania = typy_zakonczone[~typy_zakonczone['Unikalny_Klucz'].isin(df_historia['Unikalny_Klucz'])].copy()
            
            df_historia = df_historia.drop(columns=['Unikalny_Klucz'])
            do_dodania = do_dodania.drop(columns=['Unikalny_Klucz'])
            df_historia = pd.concat([df_historia, do_dodania], ignore_index=True)
        else:
            df_historia = typy_zakonczone.copy()

# CZYSZCZENIE HISTORII Z TYPÓW "W OCZEKIWANIU" (Zgodnie z punktem 9)
if not df_historia.empty:
    df_historia = df_historia[df_historia['Status'] != "W OCZEKIWANIU"].copy()

# --- SYSTEM ŚLEDZENIA AKO (PORTFEL REALNY) ---
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

baza_zagranych = pd.concat([df_all_predictions, df_historia], ignore_index=True) if not df_historia.empty else df_all_predictions.copy()

if not baza_zagranych.empty:
    nowe_ako_list = []
    grupy_ako = baza_zagranych[baza_zagranych['Kupon_ID'].astype(str).str.strip() != ""].groupby('Kupon_ID')

    for k_id, group in grupy_ako:
        data_zawarcia = group['Data'].min()
        liczba_zdarzen = len(group)
        mecze_skrot = " | ".join(group['Match_ID'].astype(str).unique())
        
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

if not df_all_predictions.empty: 
    df_all_predictions['Data_Sort'] = pd.to_datetime(df_all_predictions['Data'].astype(str) + ' ' + df_all_predictions['Godzina'].astype(str).replace('', '00:00').replace('-', '00:00'), errors='coerce')
    now = datetime.now()
    df_all_predictions = df_all_predictions[df_all_predictions['Data_Sort'] >= now - timedelta(hours=3)]
    df_all_predictions = df_all_predictions.sort_values(by=["Data_Sort", "Szansa"], ascending=[True, False]).drop(columns=['Data_Sort', 'Unikalny_Klucz'], errors='ignore')

# ==========================================
# 8. WYSYŁKA GOOGLE SHEETS
# ==========================================
all_sheets = ["Summary", "Fixtures", "Results", "League_Tables", "Historia_Typow", "All_Predictions", "Kupony_AKO"]

for sheet_name in all_sheets:
    try: spreadsheet.worksheet(sheet_name)
    except: spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=30)

print("Wysyłam Czysty Terminarz do Google Sheets...")
time.sleep(1.5)
spreadsheet.worksheet("Fixtures").clear()
if not fixtures_clean.empty: spreadsheet.worksheet("Fixtures").update(prepare_for_gsheets(fixtures_clean))

print("Wysyłam Historię ze statystykami do Google Sheets...")
time.sleep(1.5)
spreadsheet.worksheet("Results").clear()
if not results_clean.empty: spreadsheet.worksheet("Results").update(prepare_for_gsheets(results_clean))

print("Wysyłam Tabele Ligowe...")
time.sleep(1.5)
spreadsheet.worksheet("League_Tables").clear()
if not league_tables.empty: spreadsheet.worksheet("League_Tables").update(prepare_for_gsheets(league_tables))

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
summary_data.append(["", "", ""])
summary_data.append(["==== RAPORT POBIERANIA (LOGI) ====", "", ""])
summary_data.append(["Źródło", "URL / Plik", "Status"])
for rep in scrape_report:
    summary_data.append(rep)

time.sleep(1.5)
spreadsheet.worksheet("Summary").clear()
spreadsheet.worksheet("Summary").update(summary_data)

print("\n" + "=" * 60)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Wyeliminowano błędy dat w Fixtures, przefiltrowano mecze > 7 dni, odchudzono Historię z 'W oczekiwaniu' i rozszerzono selekcję typów w All_Predictions.")
print("=" * 60)
