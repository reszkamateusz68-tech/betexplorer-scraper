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

today = datetime.now()

# ==========================================================
# FUNKCJE MATEMATYCZNE BUKMACHERA (POISSON I KORELACJE)
# ==========================================================
def get_poisson_prob(lam, k, calc_type="exact"):
    """ Oblicza Prawdopodobieństwo z Rozkładu Poissona. """
    if pd.isna(lam) or lam <= 0: return 0.0
    try:
        if calc_type == "exact":
            return (math.exp(-lam) * (lam**k)) / math.factorial(int(k))
        elif calc_type == "under":
            return sum((math.exp(-lam) * (lam**i)) / math.factorial(i) for i in range(int(k) + 1))
        elif calc_type == "over":
            return 1.0 - sum((math.exp(-lam) * (lam**i)) / math.factorial(i) for i in range(int(k) + 1))
    except:
        return 0.0

def get_poisson_match_prob(lam_h, lam_a, max_val=35):
    """
    Krzyżowa macierz Poissona. 
    Zwraca Prawdopodobieństwo (1, X, 2) dla Strzałów, Rożnych lub Goli.
    """
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
    """
    Kalkulator kursów BetBuilder (Same Game Parlay).
    correlation_factor: 0 = brak korelacji (czyste AKO), 1 = pełna korelacja.
    margin: marża bukmachera (0.92 to ok. 8% marży).
    """
    if not probs: return 1.0
    probs.sort(reverse=True) 
    combined_p = probs[0]
    
    for p in probs[1:]:
        combined_p *= (p ** (1 - correlation_factor))
        
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
    if pd.isna(d_str) or str(d_str).strip() in ["", "nan", "NaT", "None"]:
        return "Nieznany"
        
    try:
        d = pd.to_datetime(str(d_str), format='%d.%m.%Y', errors='coerce')
        if pd.isna(d):
            d = pd.to_datetime(str(d_str), errors='coerce', format='mixed')
            
        if pd.isna(d):
            return "Nieznany"
            
        d_date = d.date()
        today_date = datetime.now().date()
        
        delta = (d_date - today_date).days
        
        if delta < 0: return "Przeszłość"
        if delta == 0: return "Dziś"
        if delta == 1: return "Jutro"
        
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
                new_row.append("")
                continue
            str_val = str(val).strip()
            if str_val in ["<NA>", "NaN", "None", "", "inf", "-inf", "-"]:
                new_row.append("")
            else:
                if any(k in col_name for k in ["Odd", "Avg", "Value", "PPG", "Prawdopodobieństwo", "Pewność", "Kurs", "Szansa", "Profit"]):
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

scraper_be = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

all_data = []
for i, url in enumerate(urls, start=1):
    url_clean = str(url).strip()
    print(f"[{i}/{len(urls)}] Pobieram BetExplorer: {url_clean}")
    if "/fixtures/" not in url_clean and "/results/" not in url_clean: continue

    if i > 1: time.sleep(random.uniform(1.0, 2.5))

    max_retries = 3
    response = None
    bypass_used = False
    success = False

    for attempt in range(max_retries):
        if attempt > 0:
            wait_time = random.uniform(10, 15) * attempt
            print(f"  -> Kod antybotowy. Chłodzenie {wait_time:.1f}s (Próba {attempt+1}/{max_retries})...")
            time.sleep(wait_time)
        
        try:
            response = scraper_be.get(url_clean, timeout=30)
            if response.status_code == 200:
                success = True
                break
            elif response.status_code in [429, 403]:
                bypass_used = True
            else:
                break
        except Exception as e:
            print(f"  -> Błąd żądania: {e}")
            if attempt < max_retries - 1: time.sleep(5)

    if not success or response is None or response.status_code != 200:
        final_code = response.status_code if response else "Brak odpowiedzi"
        scrape_report.append(["BetExplorer", url_clean, f"BŁĄD: Kod {final_code} po {max_retries} próbach"])
        continue

    try:
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
    except Exception as e: scrape_report.append(["BetExplorer", url_clean, f"BŁĄD PARSOWANIA: {e}"])

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
        skaner_ss = cloudscraper.create_scraper(browser={'browser': 'chrome','platform': 'windows','desktop': True})
        for i_ss, url_ss in enumerate(urls_ss, start=1):
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
    fixtures_df['Status_Kursów'] = np.where(fixtures_df['Odd1'].astype(str).str.strip().isin(["", "-", "nan"]), "Brak Kursów", "Są Kursy")

    # --- DODANA MARŻA BUKMACHERA ---
    def get_margin(r):
        try:
            o1 = float(str(r['Odd1']).replace(',', '.'))
            ox = float(str(r['OddX']).replace(',', '.'))
            o2 = float(str(r['Odd2']).replace(',', '.'))
            margin = ((1 / o1) + (1 / ox) + (1 / o2) - 1.0) * 100
            # Formatuje do polskiego standardu, np. 5,25%
            return f"{round(margin, 2)}%".replace('.', ',')
        except:
            return "-"
    fixtures_df['Marża'] = fixtures_df.apply(get_margin, axis=1)
    # -------------------------------

results_clean = results_df[list(golden_cols.keys())].rename(columns=golden_cols) if not results_df.empty else pd.DataFrame(columns=golden_cols.values())

# Aktualizacja fixtures_clean o kolumnę 'Marża'
fixtures_clean = fixtures_df[['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd1', 'OddX', 'Odd2', 'Marża']].rename(columns={'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2'}) if not fixtures_df.empty else pd.DataFrame(columns=['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd_1', 'Odd_X', 'Odd_2', 'Marża'])

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

# ŻELAZNY STANDARD NAGŁÓWKÓW PREDYKCYJNYCH
STANDARD_HEADERS = ["Match_ID", "Termin", "Data", "Godzina", "Liga", "Mecz", "Status_Kursów", "Sugerowany Typ", "Szansa", "Kurs Szac.", "Argumentacja"]

# ==========================================================
# 6a. ENGINE 1X PRO (Model: Poisson + Kroczące Okno)
# ==========================================================
print("Uruchamiam Engine 1X Pro (Baza 30 gier + Rozkład Poissona)...")
predictions_1x = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    status_k = row['Status_Kursów']
    
    o1_raw = row['Odd_1']
    ox_raw = row['Odd_X']
    buk_odd_1x = "-"
    if str(o1_raw).strip() not in ["", "-", "nan"] and str(ox_raw).strip() not in ["", "-", "nan"]:
        try:
            o1 = float(str(o1_raw).replace(',', '.'))
            ox = float(str(ox_raw).replace(',', '.'))
            buk_odd_1x = round(1 / ((1 / o1) + (1 / ox)), 2)
        except: pass

    h_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Home'] == home)]
    a_all = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Away'] == away)]

    h_window = h_all.head(30)
    a_window = a_all.head(30)
    if len(h_window) < 10 or len(a_window) < 10: continue

    h_gf, h_ga = h_window['FTHG'].mean(), h_window['FTAG'].mean()
    a_gf, a_ga = a_window['FTAG'].mean(), a_window['FTHG'].mean()
    
    lam_h_goals = (h_gf + a_ga) / 2
    lam_a_goals = (a_gf + h_ga) / 2
    
    p1_g, px_g, p2_g = get_poisson_match_prob(lam_h_goals, lam_a_goals, max_val=15)
    poisson_1x = p1_g + px_g

    h_1x_window_cnt = sum(h_window['FTHG'] >= h_window['FTAG'])
    a_lose_window_cnt = sum(a_window['FTHG'] >= a_window['FTAG'])
    hist_1x = (h_1x_window_cnt / len(h_window) + a_lose_window_cnt / len(a_window)) / 2

    final_prob = min(max((poisson_1x * 0.5) + (hist_1x * 0.5), 0.05), 0.95)
    fair_odd = round((1 / final_prob) * 0.93, 2) 
    szansa_str = f"{round(final_prob*100, 1)}%"
    
    if buk_odd_1x != "-":
        value_perc = round(((buk_odd_1x / fair_odd) - 1) * 100, 2)
        val_str = f"{value_perc}%"
        buk_str = str(buk_odd_1x).replace('.', ',')
    else:
        val_str, buk_str = "-", "-"

    if final_prob >= 0.70:
        arg = f"Poisson (λ_H={round(lam_h_goals,2)}, λ_A={round(lam_a_goals,2)}) zblendowany z historią."
        
        predictions_1x.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", status_k,
            "1X", szansa_str, str(fair_odd).replace('.', ','), arg,
            val_str, buk_str, 
            f"Baza H: {len(h_window)}", f"{h_1x_window_cnt}/{len(h_window)}",
            f"Baza A: {len(a_window)}", f"{a_lose_window_cnt}/{len(a_window)}"
        ])
        all_generated_predictions.append([row['Match_ID'], row['Termin'], row['Date'], home, away, "1X Pro", "1X", buk_odd_1x, szansa_str, str(fair_odd).replace('.', ','), arg])

headers_1x = STANDARD_HEADERS + ["Value %", "Buk_Odd (Rynek)", "H_Probka", "H_1X_Okno", "A_Probka", "A_NieWygra_Okno"]
df_pred_1x = pd.DataFrame(predictions_1x, columns=headers_1x).sort_values(by="Szansa", ascending=False) if predictions_1x else pd.DataFrame(columns=headers_1x)

# ==========================================================
# 6b. ENGINE BETBUILDER PRO (Zestaw Skorelowany)
# ==========================================================
print("Uruchamiam Engine BetBuilder Pro...")
predictions_builder = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    status_k = row['Status_Kursów']
    
    h_dom = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Home'] == home)].copy()
    a_wyj = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Away'] == away)].copy()
    if len(h_dom) < 10 or len(a_wyj) < 10: continue

    h_dom['HT_Total'] = pd.to_numeric(h_dom['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(h_dom['HTAG'], errors='coerce').fillna(0)
    a_wyj['HT_Total'] = pd.to_numeric(a_wyj['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(a_wyj['HTAG'], errors='coerce').fillna(0)
    
    builder_blocks_code = []
    block_probabilities = []
    
    h_dom_o05 = sum(h_dom['Total_Goals'] >= 1) / len(h_dom)
    a_wyj_o05 = sum(a_wyj['Total_Goals'] >= 1) / len(a_wyj)
    prob_o05 = (h_dom_o05 + a_wyj_o05) / 2
    if prob_o05 >= 0.95:
        builder_blocks_code.append("O0.5")
        block_probabilities.append(prob_o05)

    for line in [4.5, 5.5, 6.5]:
        h_u = sum(h_dom['Total_Goals'] < line) / len(h_dom)
        a_u = sum(a_wyj['Total_Goals'] < line) / len(a_wyj)
        prob_u = (h_u + a_u) / 2
        if prob_u >= 0.94:
            builder_blocks_code.append(f"U{line}")
            block_probabilities.append(prob_u)
            break

    for line in [1.5, 2.5]:
        h_u_1h = sum(h_dom['HT_Total'] < line) / len(h_dom)
        a_u_1h = sum(a_wyj['HT_Total'] < line) / len(a_wyj)
        prob_u_1h = (h_u_1h + a_u_1h) / 2
        if prob_u_1h >= 0.94:
            builder_blocks_code.append(f"HT_U{line}")
            block_probabilities.append(prob_u_1h)
            break

    if len(builder_blocks_code) >= 3:
        final_builder_safety = round(np.mean(block_probabilities) * 100, 1)
        estimated_bb_odd = calc_betbuilder_odd(block_probabilities, correlation_factor=0.65, margin=0.92)
        sugerowany_kupon = "+".join(builder_blocks_code)
        uzasadnienie = f"BetBuilder skalkulowany z korelacją zdarzeń."

        predictions_builder.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", status_k,
            sugerowany_kupon, f"{final_builder_safety}%", str(estimated_bb_odd).replace('.', ','), uzasadnienie,
            f"Dom: {len(h_dom)}", f"Wyj: {len(a_wyj)}", 
            int(h_dom['FTHG'].max()), int(h_dom['FTAG'].max()), 
            int(a_wyj['FTAG'].max()), int(a_wyj['FTHG'].max())
        ])
        
        all_generated_predictions.append([row['Match_ID'], row['Termin'], row['Date'], home, away, "BetBuilder Pro", sugerowany_kupon, "-", f"{final_builder_safety}%", str(estimated_bb_odd).replace('.', ','), uzasadnienie])

headers_builder = STANDARD_HEADERS + ["H_Probka", "A_Probka", "H_Max_Strz_Dom", "H_Max_Stra_Dom", "A_Max_Strz_Wyj", "A_Max_Stra_Wyj"]
df_pred_builder = pd.DataFrame(predictions_builder, columns=headers_builder).sort_values(by="Szansa", ascending=False) if predictions_builder else pd.DataFrame(columns=headers_builder)

# ==========================================================
# 6c. ENGINE MULTIGOL (Przedziały 1-5 i 1-6) z Poissonem
# ==========================================================
print("Uruchamiam Engine Multigol (Detektor z Regresją i Poissonem)...")
predictions_multigol = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    status_k = row['Status_Kursów']
    
    h_dom = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Home'] == home)]
    a_wyj = valid_matches[(valid_matches['Base_League'] == fixture_base) & (valid_matches['Away'] == away)]
    if len(h_dom) < 5 or len(a_wyj) < 5: continue

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

    avg_h_goals = h_dom['Total_Goals'].mean()
    avg_a_goals = a_wyj['Total_Goals'].mean()
    lam_match = (avg_h_goals + avg_a_goals) / 2
    
    poisson_0_goals = get_poisson_prob(lam_match, 0, "exact")
    poisson_under_5 = get_poisson_prob(lam_match, 5, "under")
    poisson_under_6 = get_poisson_prob(lam_match, 6, "under")
    
    poisson_1_5 = poisson_under_5 - poisson_0_goals
    poisson_1_6 = poisson_under_6 - poisson_0_goals

    hist_1_5 = (sum((h_dom['Total_Goals'] >= 1) & (h_dom['Total_Goals'] <= 5)) / len(h_dom) + 
                sum((a_wyj['Total_Goals'] >= 1) & (a_wyj['Total_Goals'] <= 5)) / len(a_wyj)) / 2
    hist_1_6 = (sum((h_dom['Total_Goals'] >= 1) & (h_dom['Total_Goals'] <= 6)) / len(h_dom) + 
                sum((a_wyj['Total_Goals'] >= 1) & (a_wyj['Total_Goals'] <= 6)) / len(a_wyj)) / 2
    
    prob_1_5 = (hist_1_5 + poisson_1_5) / 2
    prob_1_6 = (hist_1_6 + poisson_1_6) / 2

    if prob_1_5 >= 0.88 or prob_1_6 >= 0.88:
        if prob_1_5 >= 0.88:
            typ_kod, pewnosc = "MG_1-5", prob_1_5
        else:
            typ_kod, pewnosc = "MG_1-6", prob_1_6
            
        est_odd = max(1.05, round((1 / pewnosc) * 0.93, 2))
        szansa_str = f"{round(pewnosc*100, 1)}%"
        uzasadnienie = anom_text + f"Regresja + Poisson (λ={round(lam_match, 2)})."

        predictions_multigol.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", status_k,
            typ_kod, szansa_str, str(est_odd).replace('.', ','), uzasadnienie,
            f"D: {len(h_dom)}", f"W: {len(a_wyj)}", h_last_goals, a_last_goals
        ])
        
        all_generated_predictions.append([row['Match_ID'], row['Termin'], row['Date'], home, away, "Multigol", typ_kod, "-", szansa_str, str(est_odd).replace('.', ','), uzasadnienie])

headers_multigol = STANDARD_HEADERS + ["H_Próbka", "A_Próbka", "Ostatnie_Gole_H", "Ostatnie_Gole_A"]
df_pred_multigol = pd.DataFrame(predictions_multigol, columns=headers_multigol).sort_values(by="Szansa", ascending=False) if predictions_multigol else pd.DataFrame(columns=headers_multigol)

# ==========================================================
# 6d. ENGINE CORNERS PRO (Undery Rzutów Rożnych z Poissonem)
# ==========================================================
print("Uruchamiam Engine Corners Pro (Model Poissona dla Rzutów Rożnych)...")
predictions_corners = []
valid_corners = valid_matches.dropna(subset=['Corners_H', 'Corners_A']).copy()

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    status_k = row['Status_Kursów']

    h_dom_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & (valid_corners['Home'] == home)].copy()
    a_wyj_c = valid_corners[(valid_corners['Base_League'] == fixture_base) & (valid_corners['Away'] == away)].copy()
    if len(h_dom_c) < 5 or len(a_wyj_c) < 5: continue

    h_c_for = h_dom_c['Corners_H'].mean()
    h_c_ag = h_dom_c['Corners_A'].mean()
    a_c_for = a_wyj_c['Corners_A'].mean()
    a_c_ag = a_wyj_c['Corners_H'].mean()

    lam_match = (h_c_for + h_c_ag + a_c_for + a_c_ag) / 2
    lam_h = (h_c_for + a_c_ag) / 2
    lam_a = (a_c_for + h_c_ag) / 2

    c_blocks_code = []
    c_probs = []

    for line in [8.5, 9.5, 10.5, 11.5]:
        poisson_u = get_poisson_prob(lam_match, int(line), "under")
        hist_u = (sum(h_dom_c['Total_Corners'] < line)/len(h_dom_c) + sum(a_wyj_c['Total_Corners'] < line)/len(a_wyj_c)) / 2
        prob_u = (poisson_u + hist_u) / 2
        
        if prob_u >= 0.88:
            c_blocks_code.append(f"C_U{line}")
            c_probs.append(prob_u)
            break 

    for line in [4.5, 5.5, 6.5, 7.5]:
        p_u = get_poisson_prob(lam_h, int(line), "under")
        h_u = sum(h_dom_c['Corners_H'] < line) / len(h_dom_c)
        prob = (p_u + h_u) / 2
        if prob >= 0.90:
            c_blocks_code.append(f"HC_U{line}")
            c_probs.append(prob)
            break

    for line in [3.5, 4.5, 5.5, 6.5]:
        p_u = get_poisson_prob(lam_a, int(line), "under")
        h_u = sum(a_wyj_c['Corners_A'] < line) / len(a_wyj_c)
        prob = (p_u + h_u) / 2
        if prob >= 0.90:
            c_blocks_code.append(f"AC_U{line}")
            c_probs.append(prob)
            break

    if len(c_blocks_code) >= 1:
        est_odd = calc_betbuilder_odd(c_probs, correlation_factor=0.60, margin=0.92)
        final_safety = round(np.mean(c_probs) * 100, 1)
        szansa_str = f"{final_safety}%"
        typ_kod = "+".join(c_blocks_code)
        uzasadnienie = f"Poisson Rzutów Rożnych (Mecz λ={round(lam_match,1)}, H={round(lam_h,1)}, A={round(lam_a,1)})"

        predictions_corners.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", status_k,
            typ_kod, szansa_str, str(est_odd).replace('.', ','), uzasadnienie,
            round(lam_match, 1), round(lam_h, 1), round(lam_a, 1)
        ])
        
        all_generated_predictions.append([row['Match_ID'], row['Termin'], row['Date'], home, away, "Corners Pro", typ_kod, "-", szansa_str, str(est_odd).replace('.', ','), uzasadnienie])

headers_corners = STANDARD_HEADERS + ["Oczekiwane Rożne (Mecz)", "Oczekiwane Rożne H", "Oczekiwane Rożne A"]
df_pred_corners = pd.DataFrame(predictions_corners, columns=headers_corners).sort_values(by="Szansa", ascending=False) if predictions_corners else pd.DataFrame(columns=headers_corners)

# ==========================================================
# 6e. ENGINE SHOTS PRO (1X2 dla Strzałów z Poissona)
# ==========================================================
print("Uruchamiam Engine Shots Pro (Poisson dla Strzałów/Celnych)...")
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
    status_k = row['Status_Kursów']

    h_dom_s = valid_shots[(valid_shots['Base_League'] == fixture_base) & (valid_shots['Home'] == home)].copy()
    a_wyj_s = valid_shots[(valid_shots['Base_League'] == fixture_base) & (valid_shots['Away'] == away)].copy()
    if len(h_dom_s) < 5 or len(a_wyj_s) < 5: continue

    lam_h_shots = (h_dom_s['Shots_H'].mean() + a_wyj_s['Shots_H'].mean()) / 2
    lam_a_shots = (a_wyj_s['Shots_A'].mean() + h_dom_s['Shots_A'].mean()) / 2
    
    p1_s, px_s, p2_s = get_poisson_match_prob(lam_h_shots, lam_a_shots, max_val=35)
    
    hist_h_s = (sum(h_dom_s['Shots_H'] > h_dom_s['Shots_A'])/len(h_dom_s) + sum(a_wyj_s['Shots_H'] > a_wyj_s['Shots_A'])/len(a_wyj_s)) / 2
    prob_h_s = (p1_s + hist_h_s) / 2

    lam_h_st = (h_dom_s['ShotsTarget_H'].mean() + a_wyj_s['ShotsTarget_H'].mean()) / 2
    lam_a_st = (a_wyj_s['ShotsTarget_A'].mean() + h_dom_s['ShotsTarget_A'].mean()) / 2
    
    p1_st, px_st, p2_st = get_poisson_match_prob(lam_h_st, lam_a_st, max_val=25)
    
    hist_h_st = (sum(h_dom_s['ShotsTarget_H'] > h_dom_s['ShotsTarget_A'])/len(h_dom_s) + sum(a_wyj_s['ShotsTarget_H'] > a_wyj_s['ShotsTarget_A'])/len(a_wyj_s)) / 2
    prob_h_st = (p1_st + hist_h_st) / 2

    if prob_h_s > 0.80:
        est_odd_s = max(1.05, round((1 / prob_h_s) * 0.93, 2))
        szansa_str = f"{round(prob_h_s * 100, 1)}%"
        uzasadnienie_s = f"Przewaga strzałów (Poisson H: {round(lam_h_shots,1)}, A: {round(lam_a_shots,1)})"
        
        predictions_shots.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", status_k,
            "S_1", szansa_str, str(est_odd_s).replace('.', ','), uzasadnienie_s,
            "Strzały Ogółem", round(lam_h_shots,1), round(lam_a_shots,1)
        ])
        all_generated_predictions.append([row['Match_ID'], row['Termin'], row['Date'], home, away, "Shots Pro", "S_1", "-", szansa_str, str(est_odd_s).replace('.', ','), uzasadnienie_s])
        
    if prob_h_st > 0.80:
        est_odd_st = max(1.05, round((1 / prob_h_st) * 0.93, 2))
        szansa_str = f"{round(prob_h_st * 100, 1)}%"
        uzasadnienie_st = f"Przewaga celnych (Poisson H: {round(lam_h_st,1)}, A: {round(lam_a_st,1)})"
        
        predictions_shots.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", status_k,
            "ST_1", szansa_str, str(est_odd_st).replace('.', ','), uzasadnienie_st,
            "Strzały Celne", round(lam_h_st,1), round(lam_a_st,1)
        ])
        all_generated_predictions.append([row['Match_ID'], row['Termin'], row['Date'], home, away, "Shots Pro", "ST_1", "-", szansa_str, str(est_odd_st).replace('.', ','), uzasadnienie_st])

headers_shots = STANDARD_HEADERS + ["Typ Statystyki", "Oczekiwane Strz H", "Oczekiwane Strz A"]
df_pred_shots = pd.DataFrame(predictions_shots, columns=headers_shots).sort_values(by="Szansa", ascending=False) if predictions_shots else pd.DataFrame(columns=headers_shots)

# ==========================================================
# 6f. ENGINE ZIMNY PRYSZNIC (Test Motywacji)
# ==========================================================
print("Uruchamiam Engine Zimny Prysznic (Reakcja TOP drużyn na wpadkę)...")
predictions_coldshower = []

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    status_k = row['Status_Kursów']
    
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
            szansa_str = f"{round(prob_bounce*100)}%"
            uzasadnienie = "Reakcja TOP drużyny na potknięcie z dołem tabeli."
            
            predictions_coldshower.append([
                row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", status_k,
                "1", szansa_str, str(est_odd).replace('.', ','), uzasadnienie,
                f"{last_m['Home']} {last_m['FTHG']}:{last_m['FTAG']} {home}"
            ])
            all_generated_predictions.append([row['Match_ID'], row['Termin'], row['Date'], home, away, "Cold Shower", "1", "-", szansa_str, str(est_odd).replace('.', ','), uzasadnienie])

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
    status_k = row['Status_Kursów']
    
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
                szansa_str = f"{round(prob*100)}%"
                uzasadnienie = f"Wysokie xG ze strzałów celnych ({int(st_for)}) bez poparcia w wynikach ({int(g_for)} goli)."
                
                predictions_hiddenform.append([
                    row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", status_k,
                    typ_kod, szansa_str, str(est_odd).replace('.', ','), uzasadnienie,
                    f"Celne Zespół: {int(st_for)} | Rywale: {int(st_agg)}", f"{int(g_for)} goli w 3 meczach"
                ])
                all_generated_predictions.append([row['Match_ID'], row['Termin'], row['Date'], home, away, "Hidden Form", typ_kod, "-", szansa_str, str(est_odd).replace('.', ','), uzasadnienie])

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
    status_k = row['Status_Kursów']
    
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
            szansa_str = f"{round(prob*100)}%"
            uzasadnienie = f"Pęknięta seria rożnych. Średnia z sezonu: {round(season_avg, 2)}, ostatnio: {round(last_2_avg, 2)}"
            
            predictions_corner_anomalies.append([
                row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", status_k,
                typ_kod, szansa_str, str(est_odd).replace('.', ','), uzasadnienie,
                str(round(season_avg, 2)).replace('.', ','), str(round(last_2_avg, 2)).replace('.', ',')
            ])
            all_generated_predictions.append([row['Match_ID'], row['Termin'], row['Date'], home, away, "Corner Anomalies", typ_kod, "-", szansa_str, str(est_odd).replace('.', ','), uzasadnienie])

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
    status_k = row['Status_Kursów']
    
    t_past = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == away))]
    if len(t_past) < 10: continue
    
    season_avg = t_past['Total_Goals'].mean()
    last_2 = t_past.head(2)
    last_2_avg = last_2['Total_Goals'].mean()
    
    if season_avg <= 2.8 and last_2_avg >= 4.5:
        typ_kod = "U3.5"
        prob = 0.85
        est_odd = round(1.0 + (((1/prob) - 1.0) / 1.5), 2)
        szansa_str = f"{round(prob*100)}%"
        uzasadnienie = f"Anomalia overowa. Sezon: {round(season_avg, 2)}, ost. 2 mecze: {round(last_2_avg, 2)}"
        predictions_goal_anomalies.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", status_k,
            typ_kod, szansa_str, str(est_odd).replace('.', ','), uzasadnienie,
            str(round(season_avg, 2)).replace('.', ','), str(round(last_2_avg, 2)).replace('.', ',')
        ])
        all_generated_predictions.append([row['Match_ID'], row['Termin'], row['Date'], home, away, "Goal Anomalies", typ_kod, "-", szansa_str, str(est_odd).replace('.', ','), uzasadnienie])
        
    elif season_avg >= 2.5 and last_2_avg <= 0.5:
        typ_kod = "O1.5"
        prob = 0.85
        est_odd = round(1.0 + (((1/prob) - 1.0) / 1.5), 2)
        szansa_str = f"{round(prob*100)}%"
        uzasadnienie = f"Anomalia underowa. Sezon: {round(season_avg, 2)}, ost. 2 mecze: {round(last_2_avg, 2)}"
        predictions_goal_anomalies.append([
            row['Match_ID'], row['Termin'], row['Date'], row['Time'], league, f"{home} - {away}", status_k,
            typ_kod, szansa_str, str(est_odd).replace('.', ','), uzasadnienie,
            str(round(season_avg, 2)).replace('.', ','), str(round(last_2_avg, 2)).replace('.', ',')
        ])
        all_generated_predictions.append([row['Match_ID'], row['Termin'], row['Date'], home, away, "Goal Anomalies", typ_kod, "-", szansa_str, str(est_odd).replace('.', ','), uzasadnienie])

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

cols_historia = ["Match_ID", "Akceptacja", "Date", "Home", "Away", "Engine", "Bet_Type", "Odds", "Szansa", "Kurs_Szac", "Argumentacja", "Status", "Profit"]

try:
    ws_historia = spreadsheet.worksheet("Historia_Typow")
    historia_dane = ws_historia.get_all_values()
    if len(historia_dane) > 0:
        df_historia = pd.DataFrame(historia_dane[1:], columns=historia_dane[0])
    else:
        df_historia = pd.DataFrame(columns=cols_historia)
except gspread.exceptions.WorksheetNotFound:
    spreadsheet.add_worksheet(title="Historia_Typow", rows=10000, cols=15)
    ws_historia = spreadsheet.worksheet("Historia_Typow")
    df_historia = pd.DataFrame(columns=cols_historia)

for col in cols_historia:
    if col not in df_historia.columns:
        if col == "Akceptacja": df_historia.insert(1, "Akceptacja", "")
        else: df_historia[col] = ""
        
df_historia = df_historia[cols_historia]

# NOWY FILTR BEZPIECZEŃSTWA: Automatycznie odrzuca puste wiersze powstałe podczas ręcznego czyszczenia w Google Sheets
if not df_historia.empty:
    df_historia = df_historia[df_historia['Match_ID'].astype(str).str.strip() != ""]

if all_generated_predictions:
    nowe_typy_df = pd.DataFrame(all_generated_predictions, columns=["Match_ID", "Termin", "Date", "Home", "Away", "Engine", "Bet_Type", "Odds", "Szansa", "Kurs_Szac", "Argumentacja"])
    nowe_typy_df = nowe_typy_df.drop(columns=["Termin"])
    
    nowe_typy_df.insert(1, "Akceptacja", "") 
    nowe_typy_df["Status"] = "W OCZEKIWANIU"
    nowe_typy_df["Profit"] = ""
    
    nowe_typy_df = nowe_typy_df[cols_historia]
    
    if not df_historia.empty:
        df_historia['Unikalny_Klucz'] = df_historia['Match_ID'] + df_historia['Engine'] + df_historia['Bet_Type']
        nowe_typy_df['Unikalny_Klucz'] = nowe_typy_df['Match_ID'] + nowe_typy_df['Engine'] + nowe_typy_df['Bet_Type']
        
        # --- INTELIGENTNA AKTUALIZACJA ---
        w_oczek_mask = df_historia['Status'] == "W OCZEKIWANIU"
        if w_oczek_mask.any():
            map_szansa = nowe_typy_df.set_index('Unikalny_Klucz')['Szansa'].to_dict()
            map_kurs = nowe_typy_df.set_index('Unikalny_Klucz')['Kurs_Szac'].to_dict()
            map_arg = nowe_typy_df.set_index('Unikalny_Klucz')['Argumentacja'].to_dict()
            map_odds = nowe_typy_df.set_index('Unikalny_Klucz')['Odds'].to_dict()
            
            for idx in df_historia[w_oczek_mask].index:
                klucz = df_historia.at[idx, 'Unikalny_Klucz']
                if klucz in map_szansa:
                    # Rzutujemy absolutnie WSZYSTKO na tekst (str)
                    df_historia.at[idx, 'Szansa'] = str(map_szansa[klucz])
                    df_historia.at[idx, 'Kurs_Szac'] = str(map_kurs[klucz])
                    df_historia.at[idx, 'Argumentacja'] = str(map_arg[klucz])
                    
                    odd_val = map_odds[klucz]
                    if pd.notna(odd_val) and str(odd_val).strip() not in ["-", ""]:
                        df_historia.at[idx, 'Odds'] = str(odd_val)
        # ---------------------------------

        # --- ZAUTOMATYZOWANE CZYSZCZENIE (GARBAGE COLLECTOR) ULEPSZONE ---
        # Definiujemy "pustą" komórkę odporną na dziwne zachowania pandas
        is_empty_szansa = df_historia['Szansa'].astype(str).str.strip().isin(["", "nan", "None"])
        is_empty_akceptacja = df_historia['Akceptacja'].astype(str).str.strip().isin(["", "nan", "None"])
        
        ghost_mask = (df_historia['Status'] == "W OCZEKIWANIU") & \
                     is_empty_szansa & \
                     is_empty_akceptacja & \
                     (~df_historia['Unikalny_Klucz'].isin(nowe_typy_df['Unikalny_Klucz']))
        
        if ghost_mask.any():
            df_historia = df_historia[~ghost_mask]
        # -----------------------------------------------------------------
        
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
                    nowy_status = evaluate_bet(row["Bet_Type"], match_row)
                    df_historia.at[idx, "Status"] = nowy_status
                    
                    try:
                        # --- AWARYJNY SYSTEM OBLICZANIA ZYSKU ---
                        kurs_str = str(row["Odds"]).replace(',', '.').strip()
                        
                        # Jeśli rynkowego kursu (Odds) nie ma, używamy szacowanego z modelu Poissona
                        if kurs_str in ["", "-", "nan", "None"]:
                            kurs_str = str(row["Kurs_Szac"]).replace(',', '.').strip()
                            
                        kurs = float(kurs_str)
                        if nowy_status == "WYGRANA":
                            df_historia.at[idx, "Profit"] = round(kurs - 1.0, 2)
                        elif nowy_status == "PRZEGRANA":
                            df_historia.at[idx, "Profit"] = -1.0
                    except: pass

# ==========================================
# 8. WYSYŁKA GOOGLE SHEETS I AGREGACJA PREDYKCJI
# ==========================================
print("Agregacja predykcji do jednej inteligentnej tabeli (All_Predictions)...")

all_pred_dfs = [
    ("1X Pro", df_pred_1x if 'df_pred_1x' in locals() else pd.DataFrame()),
    ("BetBuilder", df_pred_builder if 'df_pred_builder' in locals() else pd.DataFrame()),
    ("Multigol", df_pred_multigol if 'df_pred_multigol' in locals() else pd.DataFrame()),
    ("Corners Pro", df_pred_corners if 'df_pred_corners' in locals() else pd.DataFrame()),
    ("Shots Pro", df_pred_shots if 'df_pred_shots' in locals() else pd.DataFrame()),
    ("Zimny Prysznic", df_pred_coldshower if 'df_pred_coldshower' in locals() else pd.DataFrame()),
    ("Ukryta Forma", df_pred_hiddenform if 'df_pred_hiddenform' in locals() else pd.DataFrame()),
    ("Anomalie Rożnych", df_pred_corner_anomalies if 'df_pred_corner_anomalies' in locals() else pd.DataFrame()),
    ("Anomalie Bramkowe", df_pred_goal_anomalies if 'df_pred_goal_anomalies' in locals() else pd.DataFrame())
]

master_predictions_list = []
base_cols = ["Match_ID", "Date", "Godzina", "Liga", "Mecz", "Sugerowany Typ", "Szansa", "Kurs Szac.", "Argumentacja"]

# Słownik do szybkiego wyciągania rynkowych kursów na podstawie Match_ID, Engine i Typu
odds_dict = {f"{r[0]}_{r[5]}_{r[6]}": r[7] for r in all_generated_predictions}

for engine_name, df_e in all_pred_dfs:
    if df_e.empty: continue
    context_cols = [c for c in df_e.columns if c not in base_cols and c not in ["Termin", "Status_Kursów", "Data", "Buk_Odd (Rynek)", "Value %"]]
    
    for _, row in df_e.iterrows():
        context_data = " | ".join([f"{c}: {row[c]}" for c in context_cols if str(row[c]).strip() not in ["", "-", "nan"]])
        
        klucz = f"{row.get('Match_ID')}_{engine_name}_{row.get('Sugerowany Typ')}"
        kurs_rynkowy = odds_dict.get(klucz, "-")
        
        master_predictions_list.append([
            row.get("Match_ID", ""), row.get("Date", row.get("Data", "")), row.get("Godzina", ""), 
            row.get("Liga", ""), row.get("Mecz", ""), engine_name, 
            row.get("Sugerowany Typ", kurs_rynkowy), kurs_rynkowy, row.get("Szansa", ""), 
            row.get("Kurs Szac.", ""), row.get("Argumentacja", ""), context_data
        ])

df_all_predictions = pd.DataFrame(master_predictions_list, columns=["Match_ID", "Date", "Godzina", "Liga", "Mecz", "Engine", "Sugerowany Typ", "Kurs Rynek", "Szansa", "Kurs Szac.", "Argumentacja", "Metrics_Context"])
if not df_all_predictions.empty:
    df_all_predictions = df_all_predictions.sort_values(by=["Date", "Szansa"], ascending=[True, False])

all_sheets = [
    "Summary", "Fixtures", "Results", "League_Tables", "Historia_Typow", "All_Predictions"
]

for sheet_name in all_sheets:
    try: spreadsheet.worksheet(sheet_name)
    except: spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=20)

try:
    spreadsheet.worksheet("Fixtures").resize(rows=5000, cols=35)
    spreadsheet.worksheet("Results").resize(rows=10000, cols=45) 
    spreadsheet.worksheet("Historia_Typow").resize(rows=10000, cols=15)
    spreadsheet.worksheet("All_Predictions").resize(rows=5000, cols=15)
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

print("Wysyłam Skonsolidowane Predykcje (All_Predictions)...")
spreadsheet.worksheet("All_Predictions").clear()
if not df_all_predictions.empty: spreadsheet.worksheet("All_Predictions").update(prepare_for_gsheets(df_all_predictions))

print("Wysyłam Logi Pobierania (Summary) do Google Sheets...")
summary_data = [
    ["==== PODSUMOWANIE OGÓLNE ====", "", ""],
    ["Ostatnia aktualizacja", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""],
    ["Fixtures Czyste", len(fixtures_clean), ""],
    ["Results Zintegrowane", len(results_clean), ""],
    ["Tabela Drużyn", len(league_tables), ""],
    ["Przetworzone Typy w Historii", len(df_historia), ""],
    ["Wygenerowane Predykcje (Wszystkie)", len(df_all_predictions), ""],
    ["", "", ""],
    ["==== RAPORT POBIERANIA Z LINKÓW ====", "", ""],
    ["System", "URL", "Status / Wynik"]
]
summary_data.extend(scrape_report)
spreadsheet.worksheet("Summary").clear()
spreadsheet.worksheet("Summary").update(summary_data)

print("\n" + "=" * 60)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Zaktualizowano historię typów oraz skonsolidowane predykcje.")
print("=" * 60)
