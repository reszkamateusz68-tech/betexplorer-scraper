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
# 0. INICJALIZACJA GOOGLE SHEETS (Z ZABEZPIECZENIEM 503)
# ==========================================================
scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
if os.path.exists("credentials.json"):
    creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
else:
    creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)

client = gspread.authorize(creds)

max_retries = 5
for attempt in range(max_retries):
    try:
        spreadsheet = client.open("BetExplorer")
        break
    except gspread.exceptions.APIError as e:
        if "503" in str(e) and attempt < max_retries - 1:
            time.sleep(2 ** attempt)
        else:
            raise e

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
        if delta == 1: return "Jutro"
        if 2 <= delta <= 7: return f"Za {delta} dni"
        return "Za ponad tydzień"
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
            col_name = str(df.columns[idx]) 
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
def get_weighted_stats(data, target_col, condition_lambda, prior_prob=0.5, alpha=2.0):
    if isinstance(data, pd.DataFrame):
        if data.empty: return 0.0, 0, 0, False
        if target_col is None:
            valid_values = data.to_dict('records')
        else:
            if target_col not in data.columns: return 0.0, 0, 0, False
            valid_values = [v for v in data[target_col].tolist() if pd.notna(v)]
    else:
        if not data: return 0.0, 0, 0, False
        if target_col is None:
            valid_values = data
        else:
            valid_values = [d.get(target_col) for d in data if pd.notna(d.get(target_col))]

    total_weight = 0.0
    weighted_hits = 0.0
    total_hits = 0
    total_len = len(valid_values)
    
    if total_len == 0: return 0.0, 0, 0, False
    
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

def evaluate_bet(bet_type, r):
    bet = str(bet_type).upper().strip()
    
    if "+" in bet:
        results = []
        for p in bet.split("+"):
            res = evaluate_bet(p.strip(), r)
            if res == "W OCZEKIWANIU": return "W OCZEKIWANIU"
            if res == "PRZEGRANA": return "PRZEGRANA"
            if res == "DO RĘCZNEJ KONTROLI": return "DO RĘCZNEJ KONTROLI"
            results.append(res)
        return "WYGRANA"

    def get_num(key):
        val = r.get(key)
        if val is None or pd.isna(val) or val == "": return None
        try: return float(val)
        except: return None

    hg = get_num('FTHG')
    ag = get_num('FTAG')
    if hg is None or ag is None: return "W OCZEKIWANIU"

    if bet == "1": return "WYGRANA" if hg > ag else "PRZEGRANA"
    if bet == "X": return "WYGRANA" if hg == ag else "PRZEGRANA"
    if bet == "2": return "WYGRANA" if hg < ag else "PRZEGRANA"
    if bet == "1X": return "WYGRANA" if hg >= ag else "PRZEGRANA"
    if bet == "X2": return "WYGRANA" if hg <= ag else "PRZEGRANA"
    if bet == "12": return "WYGRANA" if hg != ag else "PRZEGRANA"
    
    if bet == "2 (+1.5)": return "WYGRANA" if (hg - ag) <= 1 else "PRZEGRANA"
    if bet == "1 (+1.5)": return "WYGRANA" if (ag - hg) <= 1 else "PRZEGRANA"
    
    tg = get_num('Total_Goals')
    if bet.startswith("O") and tg is not None and "_" not in bet: return "WYGRANA" if tg > float(bet[1:]) else "PRZEGRANA"
    if bet.startswith("U") and tg is not None and "_" not in bet: return "WYGRANA" if tg < float(bet[1:]) else "PRZEGRANA"
    
    ht_h = get_num('HTHG')
    ht_a = get_num('HTAG')
    if bet.startswith("HT_U") and ht_h is not None and ht_a is not None: return "WYGRANA" if (ht_h + ht_a) < float(bet[4:]) else "PRZEGRANA"
    if bet.startswith("2H_U") and tg is not None and ht_h is not None and ht_a is not None: return "WYGRANA" if (tg - (ht_h + ht_a)) < float(bet[4:]) else "PRZEGRANA"
    if bet.startswith("HU") and hg is not None: return "WYGRANA" if hg < float(bet[2:]) else "PRZEGRANA"
    if bet.startswith("AU") and ag is not None: return "WYGRANA" if ag < float(bet[2:]) else "PRZEGRANA"

    if bet.startswith("MG_"):
        try:
            low, high = map(int, bet[3:].split("-"))
            return "WYGRANA" if low <= tg <= high else "PRZEGRANA"
        except: pass

    hc = get_num('Corners_H')
    ac = get_num('Corners_A')
    if hc is not None and ac is not None:
        tc = hc + ac
        if bet.startswith("C_U"): return "WYGRANA" if tc < float(bet[3:]) else "PRZEGRANA"
        if bet.startswith("C_O"): return "WYGRANA" if tc > float(bet[3:]) else "PRZEGRANA"
        if bet.startswith("HC_U"): return "WYGRANA" if hc < float(bet[4:]) else "PRZEGRANA"
        if bet.startswith("AC_U"): return "WYGRANA" if ac < float(bet[4:]) else "PRZEGRANA"
        if bet.startswith("HC_O"): return "WYGRANA" if hc > float(bet[4:]) else "PRZEGRANA"
        if bet.startswith("AC_O"): return "WYGRANA" if ac > float(bet[4:]) else "PRZEGRANA"

    sh = get_num('Shots_H')
    sa = get_num('Shots_A')
    if sh is not None and sa is not None:
        if bet == "S_1": return "WYGRANA" if sh > sa else "PRZEGRANA"
        if bet == "S_2": return "WYGRANA" if sh < sa else "PRZEGRANA"
        if bet == "H_S_O11.5": return "WYGRANA" if sh >= 12 else "PRZEGRANA"
    
    sth = get_num('ShotsTarget_H')
    sta = get_num('ShotsTarget_A')
    if sth is not None and sta is not None:
        if bet == "ST_1": return "WYGRANA" if sth > sta else "PRZEGRANA"
        if bet == "ST_2": return "WYGRANA" if sth < sta else "PRZEGRANA"
        if bet == "H_ST_O2.5": return "WYGRANA" if sth >= 3 else "PRZEGRANA"
        if bet == "A_ST_U4.5": return "WYGRANA" if sta <= 4 else "PRZEGRANA"

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
    local_data, local_report = [], []
    print(f"[{i}/{total}] Pobieram BetExplorer (Wątek): {url_clean}")
    
    scraper_be = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
    max_retries = 3
    response, bypass_used, success = None, False, False

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
    url_ss_clean, base_headers = args
    time.sleep(random.uniform(0.5, 1.5))
    local_data, local_report = [], []
    headers = base_headers.copy()

    try:
        resp = requests.get(url_ss_clean, headers=headers, timeout=20)
        if resp.status_code != 200:
            local_report.append(["SoccerStats", url_ss_clean, f"BŁĄD HTTP: Kod {resp.status_code}"])
            return local_data, local_report
            
        soup_ss = BeautifulSoup(resp.text, "html.parser")
        rows = soup_ss.find_all("tr")
        ss_count = 0

        for row in rows:
            komorki = row.find_all("td")
            if len(komorki) < 4: continue
            row_text = " ".join(k.get_text(" ", strip=True) for k in komorki)
            ht_match = re.search(r'\(\s*(\d+)\s*[-:]\s*(\d+)\s*\)', row_text)
            home_team, away_team, score_found = None, None, None
            g_gosp_1h, g_gosc_1h = "", ""

            for idx, td in enumerate(komorki):
                txt = td.get_text(strip=True).replace("*", "")
                m_score = re.fullmatch(r'(\d+)[\:\-](\d+)', txt)
                if m_score and 0 < idx < len(komorki) - 1:
                    h_candidate = komorki[idx - 1].get_text(strip=True)
                    a_candidate = komorki[idx + 1].get_text(strip=True)
                    if h_candidate and a_candidate and "HOME" not in h_candidate.upper():
                        home_team, away_team = h_candidate, a_candidate
                        score_found = f"{m_score.group(1)}:{m_score.group(2)}"
                        break

            if score_found and home_team and away_team:
                if ht_match:
                    try:
                        g_gosp_1h = int(ht_match.group(1))
                        g_gosc_1h = int(ht_match.group(2))
                    except: pass
                local_data.append([home_team, away_team, score_found, g_gosp_1h, g_gosc_1h])
                ss_count += 1

        if ss_count > 0: local_report.append(["SoccerStats", url_ss_clean, f"OK (Pobrano: {ss_count} wierszy)"])
        else: local_report.append(["SoccerStats", url_ss_clean, "OSTRZEŻENIE: Brak meczów na stronie"])
    except Exception as e: local_report.append(["SoccerStats", url_ss_clean, f"BŁĄD: {str(e)}"])
    return local_data, local_report

dane_soccerstats_baza = []
print("Rozpoczynam pobieranie z SoccerStats...")
try:
    if os.path.exists("ligi_soccerstats.xlsx"):
        urls_ss = pd.read_excel("ligi_soccerstats.xlsx")["URL"].dropna().tolist()
        base_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "Referer": "https://www.soccerstats.com/"}
        ss_args = [(str(u).strip(), base_headers) for u in urls_ss]
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            for data_chunk, report_chunk in executor.map(scrape_ss_worker, ss_args):
                dane_soccerstats_baza.extend(data_chunk)
                scrape_report.extend(report_chunk)

        if dane_soccerstats_baza: 
            ss_df = pd.DataFrame(dane_soccerstats_baza, columns=["Home", "Away", "Score", "Gole_Gosp_1H", "Gole_Gosc_1H"]).drop_duplicates(subset=["Home", "Away", "Score"])
        else: ss_df = pd.DataFrame()
except Exception as e: 
    scrape_report.append(["SoccerStats", "Główny proces", f"BŁĄD: {e}"])
    ss_df = pd.DataFrame()

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

    fd_expected_cols = ['HS', 'AS', 'HST', 'AST', 'HC', 'AC']
    for col in fd_expected_cols:
        if col not in results_df.columns: results_df[col] = np.nan

    if 'Gole_Gosp_1H' in results_df.columns:
        results_df['HTHG'] = results_df['HTHG'].combine_first(pd.to_numeric(results_df['Gole_Gosp_1H'], errors='coerce'))
        results_df['HTAG'] = results_df['HTAG'].combine_first(pd.to_numeric(results_df['Gole_Gosc_1H'], errors='coerce'))

    results_df['HT_Total'] = pd.to_numeric(results_df['HTHG'], errors='coerce') + pd.to_numeric(results_df['HTAG'], errors='coerce')
    results_df['Total_Corners'] = pd.to_numeric(results_df['HC'], errors='coerce') + pd.to_numeric(results_df['AC'], errors='coerce')

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
    
    dozwolone_terminy = ["Dziś", "Jutro", "Za 2 dni", "Za 3 dni", "Za 4 dni", "Za 5 dni", "Za 6 dni", "Za 7 dni"]
    fixtures_df = fixtures_df[fixtures_df['Termin'].isin(dozwolone_terminy)].copy()

    fixtures_df['Status_Kursów'] = np.where(fixtures_df['Odd1'].astype(str).str.strip().isin(["", "-", "nan"]), "Brak Kursów", "Są Kursy")

    def get_margin(r):
        try:
            o1, ox, o2 = float(str(r['Odd1']).replace(',','.')), float(str(r['OddX']).replace(',','.')), float(str(r['Odd2']).replace(',','.'))
            return round(((1/o1)+(1/ox)+(1/o2)-1.0)*100, 2)
        except: return ""
    fixtures_df['Marża'] = fixtures_df.apply(get_margin, axis=1)

results_clean = results_df[list(golden_cols.keys()) + ['HT_Total', 'Total_Corners', 'Marża']].rename(columns=golden_cols) if not results_df.empty else pd.DataFrame(columns=list(golden_cols.values()) + ['HT_Total', 'Total_Corners', 'Marża'])
fixtures_clean = fixtures_df[['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd1', 'OddX', 'Odd2', 'Marża']].rename(columns={'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2'}) if not fixtures_df.empty else pd.DataFrame(columns=['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd_1', 'Odd_X', 'Odd_2', 'Marża'])

# ==========================================================
# 5. GENEROWANIE TABEL LIGOWYCH
# ==========================================================
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
    valid_matches['HTHG'] = pd.to_numeric(valid_matches['HTHG'], errors='coerce')
    valid_matches['HTAG'] = pd.to_numeric(valid_matches['HTAG'], errors='coerce')
    valid_matches['Corners_H'] = pd.to_numeric(valid_matches['Corners_H'], errors='coerce')
    valid_matches['Corners_A'] = pd.to_numeric(valid_matches['Corners_A'], errors='coerce')
    valid_matches['Shots_H'] = pd.to_numeric(valid_matches['Shots_H'], errors='coerce')
    valid_matches['Shots_A'] = pd.to_numeric(valid_matches['Shots_A'], errors='coerce')
    valid_matches['ShotsTarget_H'] = pd.to_numeric(valid_matches['ShotsTarget_H'], errors='coerce')
    valid_matches['ShotsTarget_A'] = pd.to_numeric(valid_matches['ShotsTarget_A'], errors='coerce')
    
    valid_matches['Total_Goals'] = valid_matches['FTHG'] + valid_matches['FTAG']
    valid_matches['HT_Total'] = valid_matches['HTHG'].fillna(0) + valid_matches['HTAG'].fillna(0)
    valid_matches['2H_Total'] = valid_matches['Total_Goals'] - valid_matches['HT_Total']
    valid_matches['Total_Corners'] = valid_matches['Corners_H'].fillna(0) + valid_matches['Corners_A'].fillna(0)
    
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
# 6. SILNIKI PREDYKCYJNE I WCZYTYWANIE REALNYCH KURSÓW
# ==========================================================
all_generated_predictions = []

superbet_baza = {}
if os.path.exists("superbet_baza_dzis.json"):
    try:
        with open("superbet_baza_dzis.json", "r", encoding="utf-8") as f:
            superbet_baza = json.load(f)
        print(f"✅ Wczytano bazę Superbet: {len(superbet_baza)} spotkań.")
    except Exception as e:
        print(f"Błąd wczytywania bazy Superbet: {e}")

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

def get_tier_num(tier_str):
    try: return int(str(tier_str).replace("Koszyk", "").replace("K", "").strip())
    except: return 3

def get_dynamic_anchors(h_tier_str, a_tier_str, odd_1_val):
    t_h = get_tier_num(h_tier_str)
    t_a = get_tier_num(a_tier_str)
    delta_tier = t_a - t_h

    try:
        o1 = float(str(odd_1_val).replace(',', '.'))
        p1 = 1.0 / o1 if o1 > 0 else 0.45
    except:
        p1 = 0.45

    lam_ft = 2.6741 + (0.08 * delta_tier)
    lam_ht = 1.2033 + (0.04 * delta_tier)

    lam_hc = max(2.5, 5.5 + (0.40 * delta_tier))
    lam_ac = max(1.5, 4.5 - (0.40 * delta_tier))
    lam_corners_tot = lam_hc + lam_ac

    mu_h_s = max(6.0, 14.0 * (1 + 0.07 * delta_tier) * ((p1 / 0.45) ** 0.35))
    mu_a_s = max(4.0, 11.0 * (1 - 0.07 * delta_tier) * (((1 - p1) / 0.55) ** 0.35))
    mu_h_st = max(2.5, 5.0 * (1 + 0.08 * delta_tier) * ((p1 / 0.45) ** 0.40))
    mu_a_st = max(1.5, 4.0 * (1 - 0.08 * delta_tier) * (((1 - p1) / 0.55) ** 0.40))

    def apply_margin_add(prob, margin=0.0405):
        q = min(0.985, prob + margin)
        return max(1.015, round(1.0 / q, 2))

    def apply_margin_mult(prob, mult=0.925):
        if prob <= 0: return 99.0
        return max(1.010, round(1.0 / (prob * mult), 2))

    prob_s1, _, _ = get_poisson_match_prob(mu_h_s, mu_a_s, max_val=40)
    prob_st1, _, _ = get_poisson_match_prob(mu_h_st, mu_a_st, max_val=25)
    
    anchors = KOTWICE_KURSOWE.copy()
    anchors.update({
        'O0.5': apply_margin_add(get_poisson_prob(lam_ft, 0, "over")),
        'U3.5': apply_margin_add(get_poisson_prob(lam_ft, 3, "under")),
        'U4.5': apply_margin_add(get_poisson_prob(lam_ft, 4, "under")),
        'U5.5': apply_margin_add(get_poisson_prob(lam_ft, 5, "under")),
        'U6.5': apply_margin_add(get_poisson_prob(lam_ft, 6, "under")),
        'HT_U1.5': apply_margin_add(get_poisson_prob(lam_ht, 1, "under")),
        'HT_U2.5': apply_margin_add(get_poisson_prob(lam_ht, 2, "under")),
        'HT_U3.5': apply_margin_add(get_poisson_prob(lam_ht, 3, "under")),
        'HU2.5': apply_margin_add(get_poisson_prob(lam_ft * p1, 2, "under")),
        'HU3.5': apply_margin_add(get_poisson_prob(lam_ft * p1, 3, "under")),
        'AU2.5': apply_margin_add(get_poisson_prob(lam_ft * (1 - p1), 2, "under")),
        'AU3.5': apply_margin_add(get_poisson_prob(lam_ft * (1 - p1), 3, "under")),
        'C_U8.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 8, "under")),
        'C_U9.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 9, "under")),
        'C_U10.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 10, "under")),
        'C_U11.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 11, "under")),
        'C_U12.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 12, "under")),
        'C_U13.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 13, "under")),
        'C_U14.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 14, "under")),
        'HC_U4.5': apply_margin_mult(get_poisson_prob(lam_hc, 4, "under")),
        'HC_U5.5': apply_margin_mult(get_poisson_prob(lam_hc, 5, "under")),
        'HC_U6.5': apply_margin_mult(get_poisson_prob(lam_hc, 6, "under")),
        'HC_U7.5': apply_margin_mult(get_poisson_prob(lam_hc, 7, "under")),
        'HC_U8.5': apply_margin_mult(get_poisson_prob(lam_hc, 8, "under")),
        'AC_U4.5': apply_margin_mult(get_poisson_prob(lam_ac, 4, "under")),
        'AC_U5.5': apply_margin_mult(get_poisson_prob(lam_ac, 5, "under")),
        'AC_U6.5': apply_margin_mult(get_poisson_prob(lam_ac, 6, "under")),
        'AC_U7.5': apply_margin_mult(get_poisson_prob(lam_ac, 7, "under")),
        'AC_U8.5': apply_margin_mult(get_poisson_prob(lam_ac, 8, "under")),
        'HC_O4.5': apply_margin_mult(get_poisson_prob(lam_hc, 4, "over")),
        'AC_O4.5': apply_margin_mult(get_poisson_prob(lam_ac, 4, "over")),
        'S_1': apply_margin_add(prob_s1),
        'ST_1': apply_margin_add(prob_st1),
        'H_ST_O2.5': apply_margin_add(get_poisson_prob(mu_h_st, 2, "over")),
        'H_S_O11.5': apply_margin_add(get_poisson_prob(mu_h_s, 11, "over")),
        'A_ST_U4.5': apply_margin_add(get_poisson_prob(mu_a_st, 4, "under")),
        'O0.5+U5.5': apply_margin_add(get_poisson_prob(lam_ft, 5, "under") - get_poisson_prob(lam_ft, 0, "exact")),
        'O0.5+U6.5': apply_margin_add(get_poisson_prob(lam_ft, 6, "under") - get_poisson_prob(lam_ft, 0, "exact"))
    })
    return anchors

def get_real_odd(home, away, typ_kod):
    h_low, a_low = str(home).lower(), str(away).lower()
    match_data = None
    
    key_exact = f"{h_low}___{a_low}"
    if key_exact in superbet_baza:
        match_data = superbet_baza[key_exact]
    else:
        for k, v in superbet_baza.items():
            if (v['info']['gospodarz_be'].lower() == h_low and v['info']['gosc_be'].lower() == a_low) or \
               (v['info']['gospodarz_sb'].lower() == h_low and v['info']['gosc_sb'].lower() == a_low) or \
               (h_low[:5] in k and a_low[:5] in k):
                match_data = v
                break
    
    if not match_data:
        return None
        
    kursy = match_data.get("kursy", {})
    
    if typ_kod in kursy:
        return float(kursy[typ_kod])
        
    if "+" in typ_kod:
        skladniki = [s.strip() for s in typ_kod.split("+")]
        kursy_skladowe = []
        for sk in skladniki:
            if sk in kursy:
                kursy_skladowe.append(float(kursy[sk]))
            else:
                return None 
        
        if len(kursy_skladowe) == len(skladniki):
            if "O0.5" in typ_kod and ("U5.5" in typ_kod or "U6.5" in typ_kod):
                q_o = 1.0 / kursy_skladowe[0]
                q_u = 1.0 / kursy_skladowe[1]
                q_joint = q_o + q_u - 1.0
                if q_joint > 0:
                    return round(max(1.01, 1.0 / q_joint), 2)
                return None
            else:
                rho_val = 0.22 if any(c in typ_kod for c in ["C_U", "HC_", "AC_"]) else 0.45
                return calc_betbuilder_copula(kursy_skladowe, rho=rho_val)
    return None

def add_pred(match_id, termin, date, time, league, home, away, engine, typ, szansa, kurs_szac, arg, dyn_anchors=None):
    if dyn_anchors is None: dyn_anchors = KOTWICE_KURSOWE
    typ_k = str(typ).strip()
    
    try: kurs_matematyczny = float(str(kurs_szac).replace(',', '.'))
    except: kurs_matematyczny = 1.05
    
    if engine == "BetBuilder Pro" or ("+" in typ_k and "1X+U" not in typ_k and "X2+U" not in typ_k): 
        skladniki = typ_k.split("+")
        kursy_skladowe_mat = [dyn_anchors.get(sk.strip(), 1.05) for sk in skladniki]
        rho_val = 0.22 if any(c in typ_k for c in ["C_U", "HC_", "AC_"]) else 0.65
        kurs_matematyczny = calc_betbuilder_copula(kursy_skladowe_mat, rho=rho_val)
    else:
        if typ_k in dyn_anchors:
            kurs_matematyczny = dyn_anchors[typ_k]
            
    if kurs_matematyczny >= 1.50: kurs_matematyczny = round(kurs_matematyczny * 0.95, 2)
    elif 1.20 <= kurs_matematyczny < 1.50: kurs_matematyczny = round(kurs_matematyczny * 0.975, 2)
    if kurs_matematyczny < 1.015: kurs_matematyczny = 1.01
    
    # KURS REALNY SUPERBET
    kurs_realny_val = get_real_odd(home, away, typ_k)
    kurs_realny_str = f"{kurs_realny_val:.2f}" if kurs_realny_val else "Brak"
    
    kurs_do_oceny = kurs_realny_val if kurs_realny_val else kurs_matematyczny

    if engine != "BetBuilder Pro" and "+" not in typ_k and kurs_do_oceny < 1.05:
        return

    prob_decimal = float(szansa) / 100.0
    ev = prob_decimal * kurs_do_oceny

    if kurs_realny_val and ev >= 1.05:
        risk_tag = "💰 REAL VALUE"
    elif prob_decimal >= 0.95 and kurs_do_oceny >= 1.20:
        risk_tag = "🥇 1. ZŁOTY TYP"
    elif prob_decimal >= 0.95 and kurs_do_oceny >= 1.15:
        risk_tag = "🥈 2. SREBRNY TYP"
    elif prob_decimal >= 0.95 and kurs_do_oceny >= 1.10:
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
        "Szansa": szansa, "Kurs_Szac": kurs_matematyczny, "Kurs_Realny": kurs_realny_str, "Argumentacja": arg_final
    })

print("Uruchamiam Modele Predykcyjne...")

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    match_id, d_termin, d_date, d_time = row['Match_ID'], row['Termin'], row['Date'], row['Time']
    
    o1_raw, ox_raw, o2_raw = row['Odd_1'], row['Odd_X'], row['Odd_2']
    
    h_tier = team_tiers.get((league, home), 'Koszyk 3')
    a_tier = team_tiers.get((league, away), 'Koszyk 3')

    dyn_anchors = get_dynamic_anchors(h_tier, a_tier, o1_raw)

    h_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == home))].copy()
    a_tot_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == away) | (valid_matches['Away'] == away))].copy()
    h_dom = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Home'] == home)].copy()
    a_wyj = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Away'] == away)].copy()

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
    
    h_is_promoted = len(h_tot_all) <= 4
    a_is_promoted = len(a_tot_all) <= 4

    h_tier_str = 'Koszyk 6' if h_is_promoted else h_tier
    a_tier_str = 'Koszyk 6' if a_is_promoted else a_tier
    
    t_h = int(str(h_tier_str).replace("Koszyk ", "")) if "Koszyk" in str(h_tier_str) else 3
    t_a = int(str(a_tier_str).replace("Koszyk ", "")) if "Koszyk" in str(a_tier_str) else 3

    normal_match = len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5
    fav_vs_prom_home = (t_h <= 2 and a_is_promoted and len(h_tot_all) >= 10 and len(h_dom) >= 5)
    fav_vs_prom_away = (t_a <= 2 and h_is_promoted and len(a_tot_all) >= 10 and len(a_wyj) >= 5)

    if normal_match or fav_vs_prom_home or fav_vs_prom_away:
        lg_home_goals = lg_matches['FTHG'].mean() if len(lg_matches) > 0 else 1.50
        lg_away_goals = lg_matches['FTAG'].mean() if len(lg_matches) > 0 else 1.15
        lg_avg_goals = lg_home_goals + lg_away_goals

        h_gf_avg = np.where(h_tot_all.head(15)['Home'] == home, h_tot_all.head(15)['FTHG'], h_tot_all.head(15)['FTAG']).mean() if len(h_tot_all) > 0 else 1.5
        h_ga_avg = np.where(h_tot_all.head(15)['Home'] == home, h_tot_all.head(15)['FTAG'], h_tot_all.head(15)['FTHG']).mean() if len(h_tot_all) > 0 else 1.5
        a_gf_avg = np.where(a_tot_all.head(15)['Home'] == away, a_tot_all.head(15)['FTHG'], a_tot_all.head(15)['FTAG']).mean() if len(a_tot_all) > 0 else 1.0
        a_ga_avg = np.where(a_tot_all.head(15)['Home'] == away, a_tot_all.head(15)['FTAG'], a_tot_all.head(15)['FTHG']).mean() if len(a_tot_all) > 0 else 1.8

        h_att = h_gf_avg / (lg_avg_goals / 2) if lg_avg_goals > 0 else 1.0
        h_def = h_ga_avg / (lg_avg_goals / 2) if lg_avg_goals > 0 else 1.0
        a_att = a_gf_avg / (lg_avg_goals / 2) if lg_avg_goals > 0 else 1.0
        a_def = a_ga_avg / (lg_avg_goals / 2) if lg_avg_goals > 0 else 1.0

        lam_h = max(0.4, h_att * a_def * lg_home_goals)
        lam_a = max(0.2, a_att * h_def * lg_away_goals)
        p1_g, px_g, p2_g = get_poisson_match_prob(lam_h, lam_a, max_val=15)
        
        prob_1x_poisson = p1_g + px_g
        prob_x2_poisson = px_g + p2_g

        h_1x_dom_pct = sum(h_dom['FTHG'] >= h_dom['FTAG']) / len(h_dom) if len(h_dom) >= 5 else 0.5
        a_1x_wyj_pct = sum(a_wyj['FTAG'] <= a_wyj['FTHG']) / len(a_wyj) if len(a_wyj) >= 5 else (0.90 if a_is_promoted else 0.5)
        emp_1x = (h_1x_dom_pct + a_1x_wyj_pct) / 2.0

        a_x2_wyj_pct = sum(a_wyj['FTAG'] >= a_wyj['FTHG']) / len(a_wyj) if len(a_wyj) >= 5 else 0.5
        h_x2_dom_pct = sum(h_dom['FTHG'] <= h_dom['FTAG']) / len(h_dom) if len(h_dom) >= 5 else (0.85 if h_is_promoted else 0.5)
        emp_x2 = (a_x2_wyj_pct + h_x2_dom_pct) / 2.0

        blend_1x = (prob_1x_poisson * 0.35) + (emp_1x * 0.65)
        blend_x2 = (prob_x2_poisson * 0.35) + (emp_x2 * 0.65)

        if fav_vs_prom_home: blend_1x = max(blend_1x, 0.89)
        if fav_vs_prom_away: blend_x2 = max(blend_x2, 0.86)

        if blend_1x >= blend_x2: typ_kod, final_prob = "1X", blend_1x
        else: typ_kod, final_prob = "X2", blend_x2

        try:
            o1, ox, o2 = float(str(o1_raw).replace(',','.')), float(str(ox_raw).replace(',','.')), float(str(o2_raw).replace(',','.'))
            if typ_kod == "1X": fair_odd = round((o1 * ox) / (o1 + ox), 2)
            else: fair_odd = round((o2 * ox) / (o2 + ox), 2)
        except: fair_odd = round(1 / final_prob, 2) if final_prob > 0 else 1.08

        is_logical = True
        if typ_kod == "1X" and t_h > t_a + 2 and not a_is_promoted: is_logical = False
        if typ_kod == "X2" and t_a > t_h + 2 and not h_is_promoted: is_logical = False

        is_fav = (typ_kod == "1X" and t_h <= 3) or (typ_kod == "X2" and t_a <= 3)

        if (is_logical or is_fav or fav_vs_prom_home or fav_vs_prom_away) and final_prob >= 0.70 and fair_odd >= 1.04:
            if typ_kod == "1X":
                if fav_vs_prom_home:
                    h_wins = sum(h_dom['FTHG'] >= h_dom['FTAG'])
                    arg = f"Gosp ({h_tier_str}) podejmuje beniaminka (Koszyk 6). Punktowanie dom: {round(h_1x_dom_pct*100)}% (1X w {h_wins}/{len(h_dom)}). Przewaga klasy rozgrywkowej."
                else:
                    h_wins = sum(h_dom['FTHG'] >= h_dom['FTAG'])
                    a_loss = sum(a_wyj['FTHG'] >= a_wyj['FTAG'])
                    arg = f"Gosp ({h_tier_str}) punktuje dom: {round(h_1x_dom_pct*100)}% (1X w {h_wins}/{len(h_dom)}). Gość ({a_tier_str}) gubi pkt wyjazd: {round(a_1x_wyj_pct*100)}% (Bez wygranej: {a_loss}/{len(a_wyj)})."
            else:
                if fav_vs_prom_away:
                    a_wins = sum(a_wyj['FTAG'] >= a_wyj['FTHG'])
                    arg = f"Faworyt ({a_tier_str}) wyjazd z beniaminkiem (Koszyk 6). Skuteczność faworyta: {round(a_x2_wyj_pct*100)}% (X2 w {a_wins}/{len(a_wyj)}). Przewaga doświadczenia."
                else:
                    a_wins = sum(a_wyj['FTAG'] >= a_wyj['FTHG'])
                    h_loss = sum(h_dom['FTAG'] >= h_dom['FTHG'])
                    arg = f"Gość ({a_tier_str}) punktuje wyjazd: {round(a_x2_wyj_pct*100)}% (X2 w {a_wins}/{len(a_wyj)}). Gosp ({h_tier_str}) gubi pkt dom: {round(h_x2_dom_pct*100)}% (Bez wygranej: {h_loss}/{len(h_dom)})."

            add_pred(match_id, d_termin, d_date, d_time, league, home, away, "1X Pro", typ_kod, round(final_prob*100, 1), round(fair_odd, 2), arg, dyn_anchors)
                
    # 6b. GOAL LINE PRO
    if len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5:
        h_dom_dict = h_dom.to_dict('records')
        a_wyj_dict = a_wyj.to_dict('records')
        h_tot_all_dict = h_tot_all.to_dict('records')
        a_tot_all_dict = a_tot_all.to_dict('records')

        for line in [2.5, 3.5, 4.5, 5.5, 6.5]:
            prob_h_u, h_th, h_tl, h_sm = get_weighted_stats(h_dom_dict, 'Total_Goals', lambda x: pd.notna(x) and x < line, prior_prob=0.75)
            prob_a_u, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_dict, 'Total_Goals', lambda x: pd.notna(x) and x < line, prior_prob=0.75)
            _, ht_th, ht_tl, _ = get_weighted_stats(h_tot_all_dict, 'Total_Goals', lambda x: pd.notna(x) and x < line)
            _, at_th, at_tl, _ = get_weighted_stats(a_tot_all_dict, 'Total_Goals', lambda x: pd.notna(x) and x < line)
            
            avg_prob_u = (prob_h_u + prob_a_u) / 2
            if avg_prob_u >= 0.70:
                arg = f"U{line} | Ważone szanse: Gosp {round(prob_h_u*100)}%, Gość {round(prob_a_u*100)}%. Trafienia (dom/wyj): Gosp {h_th}/{h_tl}, Gość {a_th}/{a_tl}."
                if h_sm or a_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Goal Line Pro", f"U{line}", round(avg_prob_u*100, 1), dyn_anchors.get(f"U{line}", 1.10), arg, dyn_anchors)

        for line in [0.5, 1.5, 2.5]:
            prob_h_o, h_th, h_tl, h_sm = get_weighted_stats(h_dom_dict, 'Total_Goals', lambda x: pd.notna(x) and x > line, prior_prob=0.30)
            prob_a_o, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_dict, 'Total_Goals', lambda x: pd.notna(x) and x > line, prior_prob=0.30)
            _, ht_th, ht_tl, _ = get_weighted_stats(h_tot_all_dict, 'Total_Goals', lambda x: pd.notna(x) and x > line)
            _, at_th, at_tl, _ = get_weighted_stats(a_tot_all_dict, 'Total_Goals', lambda x: pd.notna(x) and x > line)
            
            avg_prob_o = (prob_h_o + prob_a_o) / 2
            if avg_prob_o >= 0.70: 
                arg = f"O{line} | Ważone szanse: Gosp {round(prob_h_o*100)}%, Gość {round(prob_a_o*100)}%. Trafienia (dom/wyj): Gosp {h_th}/{h_tl}, Gość {a_th}/{a_tl}."
                if h_sm or a_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Goal Line Pro", f"O{line}", round(avg_prob_o*100, 1), dyn_anchors.get(f"O{line}", 1.10), arg, dyn_anchors)

    # 6c. BETBUILDER PRO
    if len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5:
        h_dom_dict = h_dom.to_dict('records')
        a_wyj_dict = a_wyj.to_dict('records')
        for tpl in SZABLONY_PREMIUM:
            p_h, h_th, h_tl, h_sm = get_weighted_stats(h_dom_dict, None, lambda r, code=tpl: evaluate_bet(code, r) == "WYGRANA", prior_prob=0.85)
            p_a, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_dict, None, lambda r, code=tpl: evaluate_bet(code, r) == "WYGRANA", prior_prob=0.85)
            avg_p = (p_h + p_a) / 2
            
            if avg_p >= 0.85:
                kursy_skl = [dyn_anchors.get(sk.strip(), 1.05) for sk in tpl.split("+")]
                est_odd = calc_betbuilder_copula(kursy_skl, rho=0.45)
                if est_odd < 1.15: est_odd = 1.15
                
                arg = f"Szablon Premium | Szansa bazowa: {round(avg_p*100)}% | Trafienia: Gosp {h_th}/{h_tl}, Gość {a_th}/{a_tl}"
                if h_sm or a_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "BetBuilder Pro", tpl, round(avg_p*100, 1), round(est_odd, 2), arg, dyn_anchors)

    # 6d. MULTIGOL
    if len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5:
        h_last_goals = get_last_match_goals(fixture_base, home)
        a_last_goals = get_last_match_goals(fixture_base, away)
        
        if h_last_goals == 0 or h_last_goals > 5 or a_last_goals == 0 or a_last_goals > 5:
            h_dom_dict = h_dom.to_dict('records')
            a_wyj_dict = a_wyj.to_dict('records')
            h_tot_all_dict = h_tot_all.to_dict('records')
            a_tot_all_dict = a_tot_all.to_dict('records')
            
            prob_h_15, h_th, h_tl, h_sm = get_weighted_stats(h_dom_dict, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5, prior_prob=0.80)
            prob_a_15, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_dict, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5, prior_prob=0.80)
            _, ht_th, ht_tl, _ = get_weighted_stats(h_tot_all_dict, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5)
            _, at_th, at_tl, _ = get_weighted_stats(a_tot_all_dict, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5)
            prob_1_5 = (prob_h_15 + prob_a_15) / 2
            
            prob_h_16, h_th16, h_tl16, h_sm16 = get_weighted_stats(h_dom_dict, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 6, prior_prob=0.85)
            prob_a_16, a_th16, a_tl16, a_sm16 = get_weighted_stats(a_wyj_dict, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 6, prior_prob=0.85)
            _, ht_th16, ht_tl16, _ = get_weighted_stats(h_tot_all_dict, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 6)
            _, at_th16, at_tl16, _ = get_weighted_stats(a_tot_all_dict, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 6)
            prob_1_6 = (prob_h_16 + prob_a_16) / 2
            
            if prob_1_5 >= 0.90 or prob_1_6 >= 0.90:
                typ_kod, pewnosc, hc, hc_tl, ac, ac_tl, htc, htc_tl, atc, atc_tl, was_sm = ("MG_1-5", prob_1_5, h_th, h_tl, a_th, a_tl, ht_th, ht_tl, at_th, at_tl, (h_sm or a_sm)) if prob_1_5 >= 0.90 else ("MG_1-6", prob_1_6, h_th16, h_tl16, a_th16, a_tl16, ht_th16, ht_tl16, at_th16, at_tl16, (h_sm16 or a_sm16))
                est_odd = round(1.0 + (((1/pewnosc) - 1.0) / 1.5), 2)
                
                h_scores = ", ".join([f"{int(m['FTHG'])}:{int(m['FTAG'])}" for _, m in h_tot_all.head(3).iterrows()])
                a_scores = ", ".join([f"{int(m['FTHG'])}:{int(m['FTAG'])}" for _, m in a_tot_all.head(3).iterrows()])
                
                arg = f"Regresja po anomalii (Wyniki Gosp ost: {h_scores} | Gość: {a_scores}). Trafienia D/W: {hc}/{hc_tl}, {ac}/{ac_tl}."
                if was_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Multigol", typ_kod, round(pewnosc*100, 1), round(est_odd, 2), arg, dyn_anchors)

    # 6e. CORNERS PRO
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

        h_dom_c_dict = h_dom_c.to_dict('records')
        a_wyj_c_dict = a_wyj_c.to_dict('records')
        h_tot_all_c_dict = h_tot_all_c.to_dict('records')
        a_tot_all_c_dict = a_tot_all_c.to_dict('records')

        c_blocks_code, c_probs, c_odds, arg_c = [], [], [], []
        any_smoothed = False

        for line in [8.5, 9.5, 10.5, 11.5, 12.5, 13.5, 14.5]:
            if line > max_match - 2:
                prob_h_c, h_th, h_tl, h_sm = get_weighted_stats(h_dom_c_dict, 'Total_Corners', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
                prob_a_c, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_c_dict, 'Total_Corners', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
                _, ht_th, ht_tl, _ = get_weighted_stats(h_tot_all_c_dict, 'Total_Corners', lambda x: pd.notna(x) and x < line)
                _, at_th, at_tl, _ = get_weighted_stats(a_tot_all_c_dict, 'Total_Corners', lambda x: pd.notna(x) and x < line)
                
                avg_p = (prob_h_c + prob_a_c) / 2
                
                if avg_p >= 0.90:
                    if h_sm or a_sm: any_smoothed = True
                    c_blocks_code.append(f"C_U{line}"); c_probs.append(avg_p); c_odds.append(dyn_anchors.get(f"C_U{line}", round(1/(avg_p*0.90), 2)))
                    arg_c.append(f"C_U{line} (D: {h_th}/{h_tl}, W: {a_th}/{a_tl})")
                    break

        for line in [4.5, 5.5, 6.5, 7.5, 8.5]:
            if line > max_h - 1:
                prob_hc, h_th, h_tl, h_sm = get_weighted_stats(h_dom_c_dict, 'Corners_H', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
                _, ht_th, ht_tl, _ = get_weighted_stats(h_tot_all_c_dict, 'Team_C_For', lambda x: pd.notna(x) and x < line)
                
                if prob_hc >= 0.92:
                    if h_sm: any_smoothed = True
                    c_blocks_code.append(f"HC_U{line}"); c_probs.append(prob_hc); c_odds.append(dyn_anchors.get(f"HC_U{line}", round(1/(prob_hc*0.90), 2)))
                    arg_c.append(f"HC_U{line} (D: {h_th}/{h_tl})")
                    break

        for line in [3.5, 4.5, 5.5, 6.5, 7.5]:
            if line > max_a - 1:
                prob_ac, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_c_dict, 'Corners_A', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
                _, at_th, at_tl, _ = get_weighted_stats(a_tot_all_c_dict, 'Team_C_For', lambda x: pd.notna(x) and x < line)
                
                if prob_ac >= 0.92:
                    if a_sm: any_smoothed = True
                    c_blocks_code.append(f"AC_U{line}"); c_probs.append(prob_ac); c_odds.append(dyn_anchors.get(f"AC_U{line}", round(1/(prob_ac*0.90), 2)))
                    arg_c.append(f"AC_U{line} (W: {a_th}/{a_tl})")
                    break

        if len(c_blocks_code) >= 1:
            est_odd = round((1.0 + sum([(o - 1.0) * 0.60 for o in c_odds])) * 0.95, 2) if len(c_blocks_code) > 1 else c_odds[0]
            if est_odd < 1.05: est_odd = 1.05
            uzasadnienie = " | ".join(arg_c)
            if any_smoothed: uzasadnienie += " | ⚠️ Bayes"
            add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Corners Pro", "+".join(c_blocks_code), round(np.mean(c_probs)*100, 1), round(est_odd, 2), uzasadnienie, dyn_anchors)

    # 6f. SHOTS PRO
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
                est_odd_s = round(1.0 + (((1/prob_h_s) - 1.0) / 1.5), 2) if prob_h_s < 1.0 else 1.01
                arg = f"Strzały Ogółem: Gosp win u siebie {h_s_win}/{h_len}. Gość lose wyjazd {a_s_lose}/{a_len}."
                if any_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Shots Pro", "S_1", round(prob_h_s*100, 1), dyn_anchors.get("S_1", round(est_odd_s, 2)), arg, dyn_anchors)
            
            if prob_h_st > 0.80:
                est_odd_st = round(1.0 + (((1/prob_h_st) - 1.0) / 1.5), 2) if prob_h_st < 1.0 else 1.01
                arg = f"Strzały Celne: Gosp win u siebie {h_st_win}/{h_len}. Gość lose wyjazd {a_st_lose}/{a_len}."
                if any_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Shots Pro", "ST_1", round(prob_h_st*100, 1), dyn_anchors.get("ST_1", round(est_odd_st, 2)), arg, dyn_anchors)

    # 6g. ZIMNY PRYSZNIC
    if h_tier in ['Koszyk 1', 'Koszyk 2'] and len(h_tot_all) > 0:
        last_m = h_tot_all.iloc[0] 
        if last_m['Away'] == home and last_m['FTHG'] >= last_m['FTAG']:
            opp_tier = team_tiers.get((last_m['League'], last_m['Home']), 'Koszyk 1')
            if opp_tier in ['Koszyk 4', 'Koszyk 5', 'Koszyk 6']:
                est_odd = round(1.0 + (((1/0.85) - 1.0) / 1.5), 2)
                arg = f"Gospodarz ({h_tier}) szuka rewanżu u siebie po stracie pkt na wyjeździe z {opp_tier}."
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Cold Shower", "1", 85.0, dyn_anchors.get("1", round(est_odd, 2)), arg, dyn_anchors)

# ==========================================================
# 7. SYSTEM ŚLEDZENIA SKUTECZNOŚCI I YIELDU (BACKTESTER)
# ==========================================================
print("Inicjalizacja Modułu Backtestingu (Śledzenie Skuteczności)...")

cols_all_pred = ["Match_ID", "Zagrane", "Wyslij_AKO", "Kupon_ID", "Termin", "Data", "Godzina", "Liga", "Gospodarz", "Gość", "Engine", "Typ", "Szansa", "Kurs_Szac", "Kurs_Realny", "Argumentacja", "Przedzial_Kursowy", "Consensus_Score", "Status"]
cols_historia = ["Match_ID", "Zagrane", "Kupon_ID", "Data", "Godzina", "Liga", "Gospodarz", "Gość", "Engine", "Typ", "Szansa", "Kurs_Szac", "Kurs_Realny", "Argumentacja", "Przedzial_Kursowy", "Consensus_Score", "Status", "Profit", "Yield_Wplyw"]

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

# ==========================================
# 7c. AUTO-KREATOR AKO Z PODZIAŁEM NA DNI TYGODNIA (KURS MIN. 2.30)
# ==========================================
print("Generowanie optymalnych kuponów AKO (Podział na dni tygodnia, kurs min. 2.30)...")

if not df_all_predictions.empty:
    dzis_dt = datetime.now().date()
    pula_predykcji = df_all_predictions.copy()
    pula_predykcji['Data_DT'] = pd.to_datetime(pula_predykcji['Data'], errors='coerce').dt.date
    
    def get_ako_group(date_obj):
        if pd.isna(date_obj): return None, 0
        wd = date_obj.weekday()
        if wd in [0, 1, 2]: 
            target = date_obj - timedelta(days=wd) 
            return target.strftime('%Y%m%d'), 1
        elif wd in [3, 4]: 
            target = date_obj + timedelta(days=(4-wd)) 
            return target.strftime('%Y%m%d'), 1
        elif wd == 5: 
            return date_obj.strftime('%Y%m%d'), 4
        elif wd == 6: 
            return date_obj.strftime('%Y%m%d'), 4
        return None, 0

    grupy_info = pula_predykcji['Data_DT'].apply(get_ako_group)
    pula_predykcji['Group_Date'] = [x[0] for x in grupy_info]
    pula_predykcji['Max_Coupons'] = [x[1] for x in grupy_info]
    
    pula_aktywna = pula_predykcji[(pula_predykcji['Data_DT'] >= dzis_dt) & (pula_predykcji['Kupon_ID'] == "")].sort_values(by=['Szansa'], ascending=False)
    istniejace_kupony = df_all_predictions['Kupon_ID'].dropna().unique()
    unikalne_grupy = pula_aktywna[['Group_Date', 'Max_Coupons']].drop_duplicates().dropna()
    
    for _, grp in unikalne_grupy.iterrows():
        g_date = grp['Group_Date']
        max_c = grp['Max_Coupons']
        if not g_date: continue
        
        prefiks = f"AKO_{g_date}"
        wygenerowane_w_grupie = [k for k in istniejace_kupony if str(k).startswith(prefiks)]
        aktualna_liczba = len(wygenerowane_w_grupie)
        
        if aktualna_liczba >= max_c: continue
            
        mecze_grupy = pula_aktywna[pula_aktywna['Group_Date'] == g_date].drop_duplicates(subset=['Match_ID'])
        zebrane_typy = []
        biezacy_kurs = 1.0
        
        for _, typ_row in mecze_grupy.iterrows():
            k_real_str = str(typ_row.get('Kurs_Realny', '')).replace(',', '.')
            if not k_real_str or k_real_str == "Brak" or k_real_str == "nan":
                k_real_str = str(typ_row.get('Kurs_Szac', '')).replace(',', '.')
                
            try: k = float(k_real_str)
            except: k = 1.0
                
            if k > 1.01:
                zebrane_typy.append(typ_row)
                biezacy_kurs *= k
                
                if biezacy_kurs >= 2.30:
                    aktualna_liczba += 1
                    nowe_id_ako = f"{prefiks}_{aktualna_liczba:02d}"
                    istniejace_kupony = np.append(istniejace_kupony, nowe_id_ako)
                    
                    for t in zebrane_typy:
                        k_match, k_eng, k_typ = t['Match_ID'], t['Engine'], t['Typ']
                        mask_pred = (df_all_predictions['Match_ID'] == k_match) & (df_all_predictions['Engine'] == k_eng) & (df_all_predictions['Typ'] == k_typ)
                        df_all_predictions.loc[mask_pred, 'Kupon_ID'] = nowe_id_ako
                        df_all_predictions.loc[mask_pred, 'Zagrane'] = "TRUE"
                        df_all_predictions.loc[mask_pred, 'Wyslij_AKO'] = "TRUE"
                    
                    zebrane_typy = []
                    biezacy_kurs = 1.0
                    
                    if aktualna_liczba >= max_c: break

# ==========================================
# 7d. OBSŁUGA KUPONU SPECJALISTY (10j)
# ==========================================
try:
    ws_ekspert = spreadsheet.worksheet("Kupon_Specjalisty")
    ekspert_dane = ws_ekspert.get_all_records()
    df_ekspert = pd.DataFrame(ekspert_dane)
except gspread.exceptions.WorksheetNotFound:
    spreadsheet.add_worksheet(title="Kupon_Specjalisty", rows=100, cols=3)
    ws_ekspert = spreadsheet.worksheet("Kupon_Specjalisty")
    ws_ekspert.update(values=[["Match_ID", "Engine", "Typ"]], range_name='A1')
    df_ekspert = pd.DataFrame()

nowe_id_ekspert = None
if not df_ekspert.empty and "Match_ID" in df_ekspert.columns:
    df_ekspert = df_ekspert[df_ekspert["Match_ID"].astype(str).str.strip() != ""]
    if not df_ekspert.empty:
        print("Wykryto ręczne zgłoszenie w Kupon_Specjalisty. Przetwarzam pakiet Eksperta...")
        nowe_id_ekspert = f"AKO_EXPERT_{datetime.now().strftime('%y%m%d_%H%M')}"
        
        for _, row_ekspert in df_ekspert.iterrows():
            k_match = str(row_ekspert.get("Match_ID", "")).strip()
            k_eng = str(row_ekspert.get("Engine", "")).strip()
            k_typ = str(row_ekspert.get("Typ", "")).strip()

            mask_pred = (df_all_predictions['Match_ID'] == k_match) & (df_all_predictions['Engine'] == k_eng) & (df_all_predictions['Typ'] == k_typ)
            df_all_predictions.loc[mask_pred, 'Kupon_ID'] = nowe_id_ekspert
            df_all_predictions.loc[mask_pred, 'Zagrane'] = "TRUE"
            df_all_predictions.loc[mask_pred, 'Wyslij_AKO'] = "TRUE"

        ws_ekspert.clear()
        ws_ekspert.update(values=[["Match_ID", "Engine", "Typ"]], range_name='A1')

if not df_all_predictions.empty:
    nowe_typy_df = df_all_predictions.copy()
    for col in ['Termin', 'Wyslij_AKO', 'Group_Date', 'Max_Coupons']:
        if col in nowe_typy_df.columns: nowe_typy_df = nowe_typy_df.drop(columns=[col])
    
    nowe_typy_df['Profit'] = ""
    nowe_typy_df['Yield_Wplyw'] = ""
        
    for col in cols_historia:
        if col not in nowe_typy_df.columns: nowe_typy_df[col] = ""
    nowe_typy_df = nowe_typy_df[cols_historia]
    
    if not df_historia.empty:
        df_historia['Unikalny_Klucz'] = df_historia['Match_ID'].astype(str) + "_" + df_historia['Engine'].astype(str) + "_" + df_historia['Typ'].astype(str)
        df_historia = df_historia.drop_duplicates(subset=['Unikalny_Klucz'], keep='last')
        
        nowe_typy_df['Unikalny_Klucz'] = nowe_typy_df['Match_ID'].astype(str) + "_" + nowe_typy_df['Engine'].astype(str) + "_" + nowe_typy_df['Typ'].astype(str)
        
        for idx in df_historia.index:
            klucz = df_historia.at[idx, 'Unikalny_Klucz']
            if klucz in map_kupon and str(map_kupon[klucz]).strip() != "":
                if str(df_historia.at[idx, 'Kupon_ID']).strip() == "":
                    df_historia.at[idx, 'Kupon_ID'] = str(map_kupon[klucz])
            if klucz in map_zagrane and str(map_zagrane[klucz]).strip() != "":
                df_historia.at[idx, 'Zagrane'] = str(map_zagrane[klucz])
        
        w_oczek_mask = df_historia['Status'] == "W OCZEKIWANIU"
        if w_oczek_mask.any():
            map_szansa = nowe_typy_df.set_index('Unikalny_Klucz')['Szansa'].to_dict()
            map_kurs_szac = nowe_typy_df.set_index('Unikalny_Klucz')['Kurs_Szac'].to_dict()
            map_kurs_real = nowe_typy_df.set_index('Unikalny_Klucz')['Kurs_Realny'].to_dict()
            map_arg = nowe_typy_df.set_index('Unikalny_Klucz')['Argumentacja'].to_dict()
            map_przedzial = nowe_typy_df.set_index('Unikalny_Klucz')['Przedzial_Kursowy'].to_dict()
            map_consensus = nowe_typy_df.set_index('Unikalny_Klucz')['Consensus_Score'].to_dict()
            
            for idx in df_historia[w_oczek_mask].index:
                klucz = df_historia.at[idx, 'Unikalny_Klucz']
                if klucz in map_szansa:
                    df_historia.at[idx, 'Szansa'] = str(map_szansa[klucz])
                    df_historia.at[idx, 'Kurs_Szac'] = str(map_kurs_szac[klucz])
                    df_historia.at[idx, 'Kurs_Realny'] = str(map_kurs_real[klucz])
                    df_historia.at[idx, 'Argumentacja'] = str(map_arg[klucz])
                    df_historia.at[idx, 'Przedzial_Kursowy'] = str(map_przedzial.get(klucz, ""))
                    df_historia.at[idx, 'Consensus_Score'] = str(map_consensus.get(klucz, ""))

        do_dodania = nowe_typy_df[~nowe_typy_df['Unikalny_Klucz'].isin(df_historia['Unikalny_Klucz'])].copy()
        do_dodania = do_dodania.drop(columns=['Unikalny_Klucz'])
        df_historia = df_historia.drop(columns=['Unikalny_Klucz'])
    else:
        do_dodania = nowe_typy_df.copy()
        if 'Unikalny_Klucz' in do_dodania.columns: do_dodania = do_dodania.drop(columns=['Unikalny_Klucz'])
        
    df_historia = pd.concat([df_historia, do_dodania], ignore_index=True)

if not df_historia.empty and not results_clean.empty:
    results_dict = {str(r['Match_ID']): r for r in results_clean.to_dict('records')}
    
    for idx, row in df_historia.iterrows():
        if row["Status"] == "W OCZEKIWANIU":
            match_id = str(row["Match_ID"])
            match_row = results_dict.get(match_id)
            
            if not match_row:
                fuzzy = results_clean[(results_clean['Home'] == row['Gospodarz']) & (results_clean['Away'] == row['Gość'])].copy()
                if not fuzzy.empty:
                    try:
                        dt_hist = pd.to_datetime(row['Data'], errors='coerce')
                        if pd.notna(dt_hist):
                            fuzzy['Diff'] = (pd.to_datetime(fuzzy['Date'], errors='coerce') - dt_hist).dt.days.abs()
                            fuzzy = fuzzy[fuzzy['Diff'] <= 2]
                            if not fuzzy.empty:
                                match_row = fuzzy.sort_values('Diff').iloc[0].to_dict()
                    except: pass
            
            if match_row and pd.notna(match_row.get('FTHG')):
                nowy_status = evaluate_bet(row["Typ"], match_row)
                df_historia.at[idx, "Status"] = nowy_status
                
                try:
                    k_real_str = str(row.get("Kurs_Realny", "")).replace(',', '.').strip()
                    k_szac_str = str(row.get("Kurs_Szac", "")).replace(',', '.').strip()
                    
                    if k_real_str and k_real_str != "Brak" and k_real_str != "nan":
                        kurs = float(k_real_str)
                    else:
                        kurs = float(k_szac_str) if k_szac_str else 1.0
                    
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
            kr_str = str(r.get('Kurs_Realny', '')).replace(',', '.').strip()
            if not kr_str or kr_str == "Brak" or kr_str == "nan":
                kr_str = str(r.get('Kurs_Szac', '')).replace(',', '.').strip()
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
        
        if str(k_id).startswith("AKO_EXPERT"):
            stawka_str = "1000"
            jednostki_str = "10j"
        else:
            if stawka_str.strip() == "": stawka_str = "100"
            jednostki_str = str(user_units.get(k_id, "1j"))
            
        try: stawka = float(stawka_str)
        except: stawka = 100.0
        
        wyslij_pod = str(user_pods.get(k_id, ""))
        tel_status = str(user_tel_stat.get(k_id, ""))
        
        wygrana_brutto = round(kurs_ako * stawka * 0.88, 2) if status_ako == "WYGRANA" else 0.0
        
        if status_ako == "WYGRANA": profit = round(wygrana_brutto - stawka, 2)
        elif status_ako == "PRZEGRANA": profit = -stawka
        else: profit = 0.0
        
        nowe_ako_list.append([k_id, data_zawarcia, mecze_skrot, liczba_zdarzen, kurs_ako, stawka, jednostki_str, status_ako, wygrana_brutto, profit, wyslij_pod, tel_status])

    df_ako = pd.DataFrame(nowe_ako_list, columns=cols_ako)
    df_ako = df_ako.sort_values(by="Data_Zawarcia", ascending=False)

if not df_historia.empty:
    df_historia['Data_Sort'] = pd.to_datetime(df_historia['Data'].astype(str) + ' ' + df_historia['Godzina'].astype(str).replace('', '00:00').replace('-', '00:00'), errors='coerce')
    mask_oczek = df_historia['Status'] == 'W OCZEKIWANIU'
    df_oczek = df_historia[mask_oczek].sort_values(by=['Data_Sort'], ascending=[True])
    df_rozst = df_historia[~mask_oczek].sort_values(by=['Data_Sort'], ascending=[False])
    df_historia = pd.concat([df_oczek, df_rozst]).drop(columns=['Data_Sort', 'Unikalny_Klucz'], errors='ignore')

if not df_all_predictions.empty: 
    df_all_predictions['Data_Sort'] = pd.to_datetime(df_all_predictions['Data'].astype(str) + ' ' + df_all_predictions['Godzina'].astype(str).replace('', '00:00').replace('-', '00:00'), errors='coerce')
    now = datetime.now()
    df_all_predictions = df_all_predictions[df_all_predictions['Data_Sort'] >= now - timedelta(hours=3)]
    df_all_predictions = df_all_predictions.sort_values(by=["Data_Sort", "Szansa"], ascending=[True, False]).drop(columns=['Data_Sort', 'Unikalny_Klucz', 'Group_Date', 'Max_Coupons'], errors='ignore')

# ==========================================
# 8. WYSYŁKA GOOGLE SHEETS
# ==========================================
all_sheets = ["Summary", "Fixtures", "Results", "League_Tables", "Historia_Typow", "All_Predictions", "Kupony_AKO"]

for sheet_name in all_sheets:
    try: spreadsheet.worksheet(sheet_name)
    except: spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=30)

def safe_batch_update(ws_name, df_data):
    if df_data.empty: return
    ws = spreadsheet.worksheet(ws_name)
    time.sleep(1.0)
    ws.clear()
    ws.update(values=prepare_for_gsheets(df_data), range_name='A1')

print("Zapisywanie danych do Google Sheets...")
safe_batch_update("Fixtures", fixtures_clean)
safe_batch_update("Results", results_clean)
safe_batch_update("League_Tables", league_tables)
safe_batch_update("Historia_Typow", df_historia)
safe_batch_update("Kupony_AKO", df_ako)
safe_batch_update("All_Predictions", df_all_predictions)

if not df_all_predictions.empty:
    print("Generowanie lekkiego widoku Top Wybory dla Eksperta...")
    try: spreadsheet.worksheet("Top_Wybory")
    except: spreadsheet.add_worksheet(title="Top_Wybory", rows=100, cols=15)
    
    top_wybory_df = df_all_predictions[df_all_predictions['Status'] == 'W OCZEKIWANIU'].sort_values(by=['Szansa'], ascending=False).head(40)
    cols_wybory = ["Match_ID", "Data", "Godzina", "Liga", "Gospodarz", "Gość", "Engine", "Typ", "Szansa", "Kurs_Szac", "Kurs_Realny", "Argumentacja"]
    top_wybory_df = top_wybory_df[[c for c in cols_wybory if c in top_wybory_df.columns]]
    safe_batch_update("Top_Wybory", top_wybory_df)

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
print("Zintegrowano API Superbet i Kalibrację Wariancji. Dopisano rynkowy Kurs Realny.")
print("=" * 60)
