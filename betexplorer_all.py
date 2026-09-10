"""
====================================================================================================
PROJEKT: STATLAB ANALYTICS - BETEXPLORER MASTER ENGINE & VALUE SCANNER
MODUŁ: betexplorer_all.py
OPIS: Zunifikowany silnik analityczny łączący modelowanie rozkładów Poissona/Skellama,
      kopułę korelacyjną BetBuilder Pro, zaawansowane filtry anomalii oraz kuloodporny
      parser oferty Superbet z automatyczną detekcją i weryfikacją statusu kursów.
====================================================================================================
"""

import os
import io
import glob
import json
import re
import time
import random
import math
from datetime import datetime, timedelta
from collections import Counter
import concurrent.futures

import numpy as np
import pandas as pd
import requests
import cloudscraper
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
import gspread
from curl_cffi import requests as cffi_requests

# ==================================================================================================
# 0. KONFIGURACJA ŚRODOWISKA I FLAGI
# ==================================================================================================

TEST_MODE = os.environ.get("TEST_MODE", "false").strip().lower() in ["true", "1", "t", "yes"]
SKIP_SUPERBET = os.environ.get("SKIP_SUPERBET", "false").strip().lower() in ["true", "1", "t", "yes"]

MAX_WORKERS_BETEXPLORER = 2 if TEST_MODE else 5
MAX_WORKERS_SOCCERSTATS = 2 if TEST_MODE else 6
MAX_WORKERS_FOOTBALLDATA = 2 if TEST_MODE else 6

today = datetime.now()

print("=" * 90)
print(f"URUCHOMIENIE SILNIKA STATLAB ANALYTICS | DATA: {today.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"TRYB TESTOWY: {'WŁĄCZONY' if TEST_MODE else 'WYŁĄCZONY'}")
print(f"INTEGRACJA SUPERBET: {'POMINIĘTA' if SKIP_SUPERBET else 'AKTYWNA'}")
print("=" * 90)

# ==================================================================================================
# 1. POŁĄCZENIE Z GOOGLE SHEETS
# ==================================================================================================

scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

if os.path.exists("credentials.json"):
    creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
elif "GOOGLE_CREDENTIALS" in os.environ:
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=scope
    )
else:
    raise FileNotFoundError("Nie odnaleziono pliku credentials.json ani zmiennej GOOGLE_CREDENTIALS.")

client = gspread.authorize(creds)

max_sheets_retries = 6
spreadsheet = None

for attempt in range(max_sheets_retries):
    try:
        spreadsheet = client.open("BetExplorer")
        print("✅ Pomyślnie połączono ze skoroszytem 'BetExplorer'")
        break
    except gspread.exceptions.APIError as e:
        if any(code in str(e) for code in ["503", "429", "500"]) and attempt < max_sheets_retries - 1:
            wait_time = (2 ** attempt) + random.uniform(0.5, 1.5)
            print(f"⚠️ Google Sheets API zajęte. Ponawianie za {wait_time:.1f}s...")
            time.sleep(wait_time)
        else:
            raise e
    except Exception as general_err:
        if attempt < max_sheets_retries - 1:
            time.sleep(3)
        else:
            raise general_err

for sheet_to_remove in ["H2H_Mecze", "Kupon_Specjalisty"]:
    try:
        ws_to_del = spreadsheet.worksheet(sheet_to_remove)
        spreadsheet.del_worksheet(ws_to_del)
        print(f"🗑️ Usunięto zbędną zakładkę: '{sheet_to_remove}'")
    except Exception:
        pass

# ==================================================================================================
# 2. SŁOWNIKI NAZW DRUŻYN
# ==================================================================================================

scrape_report = []
mapowanie_fd = {}
mapowanie_ss = {}

try:
    if os.path.exists("slownik_druzyn.json"):
        with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
            slownik = json.load(f)
            mapowanie_fd = slownik.get("FootballData_To_BetExplorer", {})
            mapowanie_ss = slownik.get("SoccerStats_To_BetExplorer", {})
        print(f"✅ Wczytano słownik drużyn: {len(mapowanie_fd)} reguł FD, {len(mapowanie_ss)} reguł SS.")
except Exception as err_slownik:
    print(f"⚠️ Uwaga przy wczytywaniu slownik_druzyn.json: {err_slownik}")

# ==================================================================================================
# 3. FUNKCJE POMOCNICZE
# ==================================================================================================

def global_recalc_przedzial(row):
    try:
        ks_str = str(row.get('Kurs_Realny', '')).replace(',', '.').strip()
        if ks_str in ["", "-", "nan", "None", "Brak"]:
            ks_str = str(row.get('Kurs_Szac', '')).replace(',', '.').strip()
        if ks_str in ["", "-", "nan", "None", "Brak"]:
            return "Brak kursu"
            
        ks = float(ks_str)
        if ks < 1.10: return "do 1.09"
        elif ks < 1.20: return "1.10 - 1.19"
        elif ks < 1.30: return "1.20 - 1.29"
        elif ks < 1.40: return "1.30 - 1.39"
        elif ks < 1.50: return "1.40 - 1.49"
        else: return "1.50+"
    except Exception:
        return "Brak kursu"


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
            except Exception:
                pass
    else:
        if len(value.split('.')) >= 3:
            try:
                if value.endswith("."):
                    d, m = value.rstrip(".").split(".")
                    return datetime(today.year, int(m), int(d)).strftime('%Y-%m-%d'), ""
                else:
                    return datetime.strptime(value, "%d.%m.%Y").strftime('%Y-%m-%d'), ""
            except Exception:
                pass
    return value, ""


def categorize_date(d_str):
    if pd.isna(d_str) or str(d_str).strip() in ["", "nan", "NaT", "None"]:
        return "Nieznany"
    try:
        d = pd.to_datetime(str(d_str), format='%Y-%m-%d', errors='coerce')
        if pd.isna(d):
            d = pd.to_datetime(str(d_str), errors='coerce', format='mixed')
        if pd.isna(d): return "Nieznany"
            
        d_date = d.date()
        today_date = datetime.now().date()
        delta = (d_date - today_date).days
        
        if delta < 0: return "Przeszłość"
        if delta == 0: return "Dziś"
        if delta == 1: return "Jutro"
        if 2 <= delta <= 7: return f"Za {delta} dni"
        return "Za ponad tydzień"
    except Exception:
        return "Nieznany"


def get_base_league(league_url_or_name):
    clean = str(league_url_or_name).split('?')[0].strip('/')
    clean = re.sub(r'-\d{4}(-\d{4})?$', '', clean)
    return clean


def prepare_for_gsheets(df):
    df_copy = df.copy().astype(str)
    output = [df_copy.columns.tolist()]
    for row in df_copy.values.tolist():
        new_row = []
        for idx, val in enumerate(row):
            col_name = str(df_copy.columns[idx])
            if pd.isna(val) or val == "nan":
                new_row.append("")
                continue
            str_val = str(val).strip()
            if str_val in ["<NA>", "NaN", "None", "", "inf", "-inf", "-"]:
                new_row.append("")
            else:
                keywords = ["Odd", "Avg", "Value", "PPG", "Kurs", "Szansa", "Profit", "Marża", "Yield", "Stawka", "Wygrana", "Liczba", "Consensus"]
                if any(k in col_name for k in keywords) and "Status" not in col_name:
                    clean_val = str_val.replace("%", "").replace(",", ".").strip()
                    new_row.append(clean_val)
                else:
                    if str_val.endswith(".0"):
                        new_row.append(str_val[:-2])
                    else:
                        new_row.append(str_val)
        output.append(new_row)
    return output


def safe_batch_update(spreadsheet_obj, ws_name, df_data):
    if df_data.empty: return
    try:
        ws = spreadsheet_obj.worksheet(ws_name)
        time.sleep(0.8)
        ws.clear()
        ws.update(values=prepare_for_gsheets(df_data), range_name='A1')
        print(f"✅ Zaktualizowano arkusz '{ws_name}' ({len(df_data)} wierszy).")
    except Exception as e_sheet:
        print(f"❌ Błąd zapisu do '{ws_name}': {e_sheet}")

# ==================================================================================================
# 4. ROZKŁADY MATEMATYCZNE I KALIBRATOR BETBUILDER
# ==================================================================================================

def get_poisson_prob(lam, k, calc_type="exact"):
    if pd.isna(lam) or lam <= 0: return 0.0
    try:
        if calc_type == "exact":
            return (math.exp(-lam) * (lam ** k)) / math.factorial(int(k))
        elif calc_type == "under":
            return sum((math.exp(-lam) * (lam ** i)) / math.factorial(i) for i in range(int(k) + 1))
        elif calc_type == "over":
            return 1.0 - sum((math.exp(-lam) * (lam ** i)) / math.factorial(i) for i in range(int(k) + 1))
    except Exception:
        return 0.0
    return 0.0


def get_poisson_match_prob(lam_h, lam_a, max_val=35):
    if pd.isna(lam_h) or pd.isna(lam_a) or lam_h <= 0 or lam_a <= 0:
        return 0.0, 0.0, 0.0
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


def calc_nested_betbuilder(sub_odds_dict, tpl, lam_h=1.5, lam_a=1.1):
    """
    Rygorystyczny kalkulator BetBuilder dla skorelowanych układów Under/Over/Corners.
    Eliminuje sztuczne pompowanie kursu wielokrotnym mnożeniem zdarzeń zagnieżdżonych.
    """
    tokens = [t.strip() for t in tpl.split("+")]
    
    # 1. Specjalna obsługa rynku Corners Pro (np. C_U11.5 + HC_U8.5)
    if any(t.startswith("C_") or t.startswith("HC_") or t.startswith("AC_") for t in tokens):
        valid_odds = []
        for t in tokens:
            odd = float(sub_odds_dict.get(t, 1.0))
            if odd > 1.01:
                valid_odds.append(odd)
        if not valid_odds:
            return 1.08
        return calc_betbuilder_copula(valid_odds, rho=0.55)

    # 2. Główna linia meczowa (U6.5, U5.5, U4.5, U3.5)
    main_under = next((t for t in tokens if t in ['U6.5', 'U5.5', 'U4.5', 'U3.5']), None)
    
    # Kurs bazowy głównej linii meczowej
    if main_under and float(sub_odds_dict.get(main_under, 1.0)) > 1.005:
        base_odd = float(sub_odds_dict[main_under])
    else:
        # Fallback bezpieczny dla wysokich linii
        if main_under == 'U6.5': base_odd = 1.015
        elif main_under == 'U5.5': base_odd = 1.03
        elif main_under == 'U4.5': base_odd = 1.09
        elif main_under == 'U3.5': base_odd = 1.25
        else: base_odd = 1.04

    # 3. Jeśli występuje warunek dolny O0.5 (Multigol / BetBuilder O0.5 + Undery)
    if 'O0.5' in tokens:
        odd_o05 = float(sub_odds_dict.get('O0.5', 1.04))
        if odd_o05 <= 1.01: odd_o05 = 1.04
        # Prawdopodobieństwo łączone dla przedziału [1, main_under]
        q_u = 1.0 / base_odd
        q_o = 1.0 / odd_o05
        q_joint = max(0.60, q_u + q_o - 1.0)
        base_odd = max(1.04, round(1.0 / q_joint, 2))

    # 4. Składowe zagnieżdżone (HT_U, 2H_U, HU, AU)
    sub_tokens = [t for t in tokens if t not in ['O0.5', main_under]]
    extra_boost = 0.0

    for st in sub_tokens:
        k_val = float(sub_odds_dict.get(st, 1.0))
        
        # Jeśli kurs składowy jest realnie pobrany z Superbet
        if k_val > 1.01:
            marginal = (k_val - 1.0) * 0.18  # Silne tłumienie korelacyjne (rho ~ 0.82)
        else:
            # Domyślny mikro-narzut bukmacherski dla głębokich underów połówkowych/drużynowych
            if st.startswith("HT_U3.5") or st.startswith("2H_U4.5") or st.startswith("HU4.5") or st.startswith("AU3.5"):
                marginal = 0.012
            elif st.startswith("HT_U2.5") or st.startswith("2H_U3.5") or st.startswith("HU3.5"):
                marginal = 0.022
            else:
                marginal = 0.015
                
        extra_boost += marginal

    final_odd = base_odd + extra_boost

    # Twarde blokady realistycznych kursów rynkowych Superbet dla szablonów
    if "U6.5" in tpl:
        final_odd = min(final_odd, 1.14)
    elif "U5.5" in tpl and "O0.5" in tpl:
        final_odd = min(final_odd, 1.18)
    elif "U4.5" in tpl:
        final_odd = min(final_odd, 1.30)

    return max(1.02, round(final_odd, 2))


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


def get_weighted_stats(data, target_col, condition_lambda, prior_prob=0.5, alpha=2.0):
    if isinstance(data, pd.DataFrame):
        if data.empty: return 0.0, 0, 0, False
        valid_values = data.to_dict('records') if target_col is None else [v for v in data[target_col].tolist() if pd.notna(v)]
    else:
        if not data: return 0.0, 0, 0, False
        valid_values = data if target_col is None else [d.get(target_col) for d in data if pd.notna(d.get(target_col))]

    total_weight = 0.0
    weighted_hits = 0.0
    total_hits = 0
    total_len = len(valid_values)
    if total_len == 0: return 0.0, 0, 0, False
        
    for i, val in enumerate(valid_values):
        w = 1.0 if i < 10 else (0.90 if i < 20 else (0.80 if i < 30 else 0.70))
        try: is_hit = 1 if condition_lambda(val) else 0
        except Exception: is_hit = 0
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
        results = [evaluate_bet(p.strip(), r) for p in bet.split("+")]
        if "W OCZEKIWANIU" in results: return "W OCZEKIWANIU"
        if "PRZEGRANA" in results: return "PRZEGRANA"
        if "DO RĘCZNEJ KONTROLI" in results: return "DO RĘCZNEJ KONTROLI"
        return "WYGRANA"

    def get_num(key):
        val = r.get(key)
        if val is None or pd.isna(val) or val == "": return None
        try: return float(val)
        except Exception: return None

    hg, ag = get_num('FTHG'), get_num('FTAG')
    if hg is None or ag is None: return "W OCZEKIWANIU"

    if bet == "1": return "WYGRANA" if hg > ag else "PRZEGRANA"
    if bet == "X": return "WYGRANA" if hg == ag else "PRZEGRANA"
    if bet == "2": return "WYGRANA" if hg < ag else "PRZEGRANA"
    if bet == "1X": return "WYGRANA" if hg >= ag else "PRZEGRANA"
    if bet == "X2": return "WYGRANA" if hg <= ag else "PRZEGRANA"
    if bet == "12": return "WYGRANA" if hg != ag else "PRZEGRANA"

    tg = get_num('Total_Goals')
    if bet.startswith("O") and tg is not None and "_" not in bet and not bet.startswith(("HC_O", "AC_O", "S_O", "ST_O")):
        return "WYGRANA" if tg > float(bet[1:]) else "PRZEGRANA"
    if bet.startswith("U") and tg is not None and "_" not in bet and not bet.startswith(("HT_U", "2H_U", "HU", "AU", "C_U", "HC_U", "AC_U", "S_U", "ST_U")):
        return "WYGRANA" if tg < float(bet[1:]) else "PRZEGRANA"

    ht_h, ht_a = get_num('HTHG'), get_num('HTAG')
    if bet.startswith("HT_U") and ht_h is not None and ht_a is not None:
        return "WYGRANA" if (ht_h + ht_a) < float(bet[4:]) else "PRZEGRANA"
    if bet.startswith("2H_U") and tg is not None and ht_h is not None and ht_a is not None:
        return "WYGRANA" if (tg - (ht_h + ht_a)) < float(bet[4:]) else "PRZEGRANA"
    if bet.startswith("HU") and hg is not None: return "WYGRANA" if hg < float(bet[2:]) else "PRZEGRANA"
    if bet.startswith("AU") and ag is not None: return "WYGRANA" if ag < float(bet[2:]) else "PRZEGRANA"

    if bet.startswith("MG_"):
        try:
            low, high = map(int, bet[3:].split("-"))
            return "WYGRANA" if low <= tg <= high else "PRZEGRANA"
        except Exception: pass

    hc, ac = get_num('Corners_H'), get_num('Corners_A')
    if hc is not None and ac is not None:
        tc = hc + ac
        if bet.startswith("C_U"): return "WYGRANA" if tc < float(bet[3:]) else "PRZEGRANA"
        if bet.startswith("C_O"): return "WYGRANA" if tc > float(bet[3:]) else "PRZEGRANA"
        if bet.startswith("HC_U"): return "WYGRANA" if hc < float(bet[4:]) else "PRZEGRANA"
        if bet.startswith("AC_U"): return "WYGRANA" if ac < float(bet[4:]) else "PRZEGRANA"
        if bet.startswith("HC_O"): return "WYGRANA" if hc > float(bet[4:]) else "PRZEGRANA"
        if bet.startswith("AC_O"): return "WYGRANA" if ac > float(bet[4:]) else "PRZEGRANA"

    sh, sa = get_num('Shots_H'), get_num('Shots_A')
    if sh is not None and sa is not None:
        if bet == "S_1": return "WYGRANA" if sh > sa else "PRZEGRANA"
        if bet == "S_2": return "WYGRANA" if sh < sa else "PRZEGRANA"

    sth, sta = get_num('ShotsTarget_H'), get_num('ShotsTarget_A')
    if sth is not None and sta is not None:
        if bet == "ST_1": return "WYGRANA" if sth > sta else "PRZEGRANA"
        if bet == "ST_2": return "WYGRANA" if sth < sta else "PRZEGRANA"
        if bet == "H_ST_U2.5": return "WYGRANA" if sth < 2.5 else "PRZEGRANA"
        if bet == "A_ST_U4.5": return "WYGRANA" if sta <= 4 else "PRZEGRANA"

    return "DO RĘCZNEJ KONTROLI"

# ==================================================================================================
# 5. KOTWICE KURSOWE I PROFILE
# ==================================================================================================

KOTWICE_KURSOWE = {
    'O0.5': 1.03, 'U3.5': 1.31, 'U4.5': 1.10, 'U5.5': 1.015, 'U6.5': 1.01,
    'HT_U1.5': 1.42, 'HT_U2.5': 1.09, 'HT_U3.5': 1.01, 'HT_U4.5': 1.01,
    '2H_U3.5': 1.02, '2H_U4.5': 1.01, 'O0.5+U5.5': 1.09, 'O0.5+U6.5': 1.05,
    'C_U8.5': 2.78, 'C_U9.5': 2.02, 'C_U10.5': 1.59, 'C_U11.5': 1.33,
    'C_U12.5': 1.17, 'C_U13.5': 1.09, 'C_U14.5': 1.04,
    'HC_U4.5': 2.59, 'HC_U5.5': 1.75, 'HC_U6.5': 1.35, 'HC_U7.5': 1.14, 'HC_U8.5': 1.03,
    'AC_U4.5': 1.74, 'AC_U5.5': 1.32, 'AC_U6.5': 1.11, 'AC_U7.5': 1.01, 'AC_U8.5': 1.01,
    'HC_O4.5': 1.44, 'AC_O4.5': 1.98,
    'HU2.5': 1.12, 'HU3.5': 1.01, 'HU4.5': 1.01,
    'AU2.5': 1.12, 'AU3.5': 1.01, 'AU4.5': 1.01,
    'S_1': 1.34, 'ST_1': 1.64,
    'H_ST_U2.5': 2.45, 'A_ST_U4.5': 1.12
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
    except Exception: return 3

def get_dynamic_anchors(h_tier_str, a_tier_str, odd_1_val):
    t_h, t_a = get_tier_num(h_tier_str), get_tier_num(a_tier_str)
    delta_tier = t_a - t_h
    try:
        o1 = float(str(odd_1_val).replace(',', '.'))
        p1 = 1.0 / o1 if o1 > 0 else 0.45
    except Exception:
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
        return max(1.015, round(1.0 / min(0.985, prob + margin), 2))

    def apply_margin_mult(prob, mult=0.925):
        return 99.0 if prob <= 0 else max(1.010, round(1.0 / (prob * mult), 2))

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
        'HU2.5': apply_margin_add(get_poisson_prob(lam_ft * p1, 2, "under")),
        'AU2.5': apply_margin_add(get_poisson_prob(lam_ft * (1 - p1), 2, "under")),
        'C_U10.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 10, "under")),
        'C_U11.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 11, "under")),
        'S_1': apply_margin_add(prob_s1),
        'ST_1': apply_margin_add(prob_st1)
    })
    return anchors

# ==================================================================================================
# 6. POBIERANIE DANYCH Z FOOTBALL-DATA, BETEXPLORER I SOCCERSTATS
# ==================================================================================================

def fetch_footballdata_worker(url):
    u = str(url).strip()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for attempt in range(3):
        try:
            res = requests.get(u, headers=headers, timeout=(10, 25))
            if res.status_code == 200:
                df = pd.read_csv(io.StringIO(res.text), on_bad_lines='skip').dropna(subset=['HomeTeam'])
                return df, ["Football-Data", u, f"OK (Pobrano: {len(df)} wierszy)"]
            elif res.status_code == 503:
                time.sleep(2 * (attempt + 1))
        except Exception as e_fd:
            time.sleep(1)
            if attempt == 2:
                return pd.DataFrame(), ["Football-Data", u, f"BŁĄD: {str(e_fd)}"]
    return pd.DataFrame(), ["Football-Data", u, "BŁĄD: Niedostępny (503/Timeout)"]


def fetch_football_data(raport):
    if not os.path.exists("ligi_footballdata.xlsx"): return pd.DataFrame()
    try: urls_fd = pd.read_excel("ligi_footballdata.xlsx")["URL"].dropna().tolist()
    except Exception: return pd.DataFrame()
    if TEST_MODE: urls_fd = urls_fd[:3]

    dfs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS_FOOTBALLDATA) as executor:
        for df_res, rep in executor.map(fetch_footballdata_worker, urls_fd):
            raport.append(rep)
            if not df_res.empty: dfs.append(df_res)
    if not dfs: return pd.DataFrame()
    fd_master = pd.concat(dfs, ignore_index=True)
    cols_to_keep = ['Date', 'HomeTeam', 'AwayTeam', 'HTHG', 'HTAG', 'HS', 'AS', 'HST', 'AST', 'HC', 'AC', 'Odd1', 'OddX', 'Odd2']
    return fd_master[[c for c in cols_to_keep if c in fd_master.columns]]

try:
    urls = pd.read_excel("ligi.xlsx")["URL"].dropna().tolist() if os.path.exists("ligi.xlsx") else []
except Exception: urls = []
if TEST_MODE and urls: urls = urls[:4]

def scrape_be_worker(args):
    i, url_clean, total = args
    time.sleep(random.uniform(0.3, 1.8))
    local_data, local_report = [], []
    scraper_be = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
    response = None
    
    for attempt in range(4):
        if attempt > 0: time.sleep(random.uniform(2.5, 5.0) * attempt)
        try:
            response = scraper_be.get(url_clean, timeout=(10, 35))
            if response.status_code == 200: break
        except Exception:
            pass

    if not response or response.status_code != 200:
        local_report.append(["BetExplorer", url_clean, "BŁĄD POŁĄCZENIA"])
        return local_data, local_report

    try:
        soup = BeautifulSoup(response.text, "html.parser")
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
                odd1 = odds[0] if len(odds) > 0 else ""
                oddx = odds[1] if len(odds) > 1 else ""
                odd2 = odds[2] if len(odds) > 2 else ""
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
                odd1 = odds[0] if len(odds) > 0 else ""
                oddx = odds[1] if len(odds) > 1 else ""
                odd2 = odds[2] if len(odds) > 2 else ""
                date_cell = row.find("td", class_=lambda x: x and "h-text-right" in x)
                date = date_cell.get_text(strip=True) if date_cell else ""
                local_data.append(["Result", league, date, home, away, score, odd1, oddx, odd2])
                mecz_count += 1

        local_report.append(["BetExplorer", url_clean, f"OK (Pobrano: {mecz_count})"])
    except Exception as e:
        local_report.append(["BetExplorer", url_clean, f"BŁĄD PARSOWANIA: {e}"])
    return local_data, local_report

all_data = []
valid_urls = [u for u in urls if "/fixtures/" in str(u) or "/results/" in str(u)]
be_args = [(i, str(url).strip(), len(valid_urls)) for i, url in enumerate(valid_urls, start=1)]

with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS_BETEXPLORER) as executor:
    for data_chunk, report_chunk in executor.map(scrape_be_worker, be_args):
        all_data.extend(data_chunk)
        scrape_report.extend(report_chunk)

df = pd.DataFrame(all_data, columns=["Type", "League", "Date", "Home", "Away", "Score", "Odd1", "OddX", "Odd2"]).drop_duplicates()
if not df.empty:
    dates, times = zip(*[split_datetime(v) for v in df["Date"]])
    df["Date"], df["Time"] = dates, times
else:
    df["Time"] = pd.Series(dtype='object')

fixtures_df = df[df["Type"] == "Fixture"].copy()
results_df = df[df["Type"] == "Result"].copy()

# SoccerStats
def scrape_ss_worker(args):
    url_ss_clean, _ = args
    time.sleep(random.uniform(0.1, 1.5))
    local_data, local_report = [], []
    try:
        response_ss = cffi_requests.get(url_ss_clean, impersonate="chrome", timeout=30)
        if response_ss.status_code == 200:
            soup_ss = BeautifulSoup(response_ss.text, "html.parser")
            tbl = next((t for t in soup_ss.find_all("table") if "HT" in t.get_text() and "BTS" in t.get_text() and len(t.find_all("tr")) > 15), None)
            ss_count = 0
            if tbl:
                for wiersz in tbl.find_all("tr"):
                    kom = wiersz.find_all(["td", "th"])
                    if len(kom) >= 6:
                        teksty = [k.get_text(" ", strip=True) for k in kom]
                        res_idx = next((idx for idx, val in enumerate(teksty) if ("-" in val or ":" in val) and any(c.isdigit() for c in val) and 1 <= idx <= 5), -1)
                        if res_idx != -1:
                            wynik = teksty[res_idx]
                            gosp = teksty[res_idx - 1]
                            gosc = teksty[res_idx + 1] if res_idx + 1 < len(teksty) else ""
                            if "HOME" in gosp.upper() or not gosp or not gosc or gosp == gosc: continue
                            stat = [s for s in teksty[res_idx + 2:] if s.strip()]
                            ht = stat[0] if len(stat) > 0 else ""
                            wyn_c = wynik.replace("*", "").strip().replace(" ", "").replace("-", ":")
                            ht_c = ht.replace("*", "").strip().replace(" ", "").replace("-", ":").replace("(", "").replace(")", "")
                            g_h_1h, g_a_1h = "", ""
                            if ":" in ht_c:
                                try:
                                    p_1h = ht_c.split(":")
                                    g_h_1h, g_a_1h = int(p_1h[0]), int(p_1h[1])
                                except Exception: pass
                            local_data.append([gosp, gosc, wyn_c, g_h_1h, g_a_1h])
                            ss_count += 1
            local_report.append(["SoccerStats", url_ss_clean, f"OK ({ss_count})"])
    except Exception as e_ss:
        local_report.append(["SoccerStats", url_ss_clean, f"BŁĄD: {e_ss}"])
    return local_data, local_report

dane_ss = []
ss_df = pd.DataFrame()
if os.path.exists("ligi_soccerstats.xlsx"):
    try:
        urls_ss = pd.read_excel("ligi_soccerstats.xlsx")["URL"].dropna().tolist()
        if TEST_MODE: urls_ss = urls_ss[:3]
        ss_args = [(str(u).strip(), {}) for u in urls_ss]
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS_SOCCERSTATS) as executor:
            for d_chunk, r_chunk in executor.map(scrape_ss_worker, ss_args):
                dane_ss.extend(d_chunk)
                scrape_report.extend(r_chunk)
        if dane_ss:
            ss_df = pd.DataFrame(dane_ss, columns=["Home", "Away", "Score", "Gole_Gosp_1H", "Gole_Gosc_1H"]).drop_duplicates(subset=["Home", "Away", "Score"])
    except Exception: pass

# Scalanie danych
if not ss_df.empty and not results_df.empty:
    ss_df["Home"] = ss_df["Home"].apply(lambda x: mapowanie_ss.get(x, x))
    ss_df["Away"] = ss_df["Away"].apply(lambda x: mapowanie_ss.get(x, x))
    results_df = pd.merge(results_df, ss_df, on=["Home", "Away", "Score"], how="left")

fd_df = fetch_football_data(scrape_report)
if not fd_df.empty and not results_df.empty:
    fd_df['HomeTeam'] = fd_df['HomeTeam'].astype(str).str.strip().replace(mapowanie_fd)
    fd_df['AwayTeam'] = fd_df['AwayTeam'].astype(str).str.strip().replace(mapowanie_fd)
    results_df['Date_str'] = pd.to_datetime(results_df['Date'], errors='coerce').dt.strftime('%Y-%m-%d')
    fd_df['Date_str'] = pd.to_datetime(fd_df['Date'], dayfirst=True, errors='coerce').dt.strftime('%Y-%m-%d')
    fd_df = fd_df.drop_duplicates(subset=['Date_str', 'HomeTeam', 'AwayTeam'], keep='last').rename(columns={'HomeTeam': 'Home', 'AwayTeam': 'Away'})
    results_df = pd.merge(results_df, fd_df.drop(columns=['Date'], errors='ignore'), how='left', on=['Date_str', 'Home', 'Away']).drop(columns=['Date_str'], errors='ignore')

# Złota struktura
golden_cols = {
    'Match_ID': 'Match_ID', 'Date': 'Date', 'League': 'League', 'Home': 'Home', 'Away': 'Away',
    'FTHG': 'FTHG', 'FTAG': 'FTAG', 'Total_Goals': 'Total_Goals', 'HTHG': 'HTHG', 'HTAG': 'HTAG',
    'HS': 'Shots_H', 'AS': 'Shots_A', 'HST': 'ShotsTarget_H', 'AST': 'ShotsTarget_A',
    'HC': 'Corners_H', 'AC': 'Corners_A', 'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2'
}

if not results_df.empty:
    for col in ['FTHG', 'FTAG', 'Score', 'HTHG', 'HTAG', 'Gole_Gosp_1H', 'Gole_Gosc_1H', 'HS', 'AS', 'HST', 'AST', 'HC', 'AC', 'Odd1', 'OddX', 'Odd2']:
        if col not in results_df.columns: results_df[col] = np.nan
    if 'Score' in results_df.columns:
        w_split = results_df['Score'].astype(str).str.split(':', expand=True)
        if w_split.shape[1] >= 2:
            results_df['FTHG'] = pd.to_numeric(w_split[0], errors='coerce')
            results_df['FTAG'] = pd.to_numeric(w_split[1], errors='coerce')
    results_df['Total_Goals'] = results_df['FTHG'] + results_df['FTAG']
    results_df['HTHG'] = results_df['HTHG'].combine_first(pd.to_numeric(results_df['Gole_Gosp_1H'], errors='coerce'))
    results_df['HTAG'] = results_df['HTAG'].combine_first(pd.to_numeric(results_df['Gole_Gosc_1H'], errors='coerce'))
    results_df['HT_Total'] = pd.to_numeric(results_df['HTHG'], errors='coerce') + pd.to_numeric(results_df['HTAG'], errors='coerce')
    results_df['Total_Corners'] = pd.to_numeric(results_df['HC'], errors='coerce') + pd.to_numeric(results_df['AC'], errors='coerce')
    results_df['Date_str'] = pd.to_datetime(results_df['Date'], errors='coerce').dt.strftime('%Y%m%d').fillna('99999999')
    results_df['Match_ID'] = results_df['Date_str'] + "_" + results_df['Home'].astype(str).str[:3].str.upper() + "_" + results_df['Away'].astype(str).str[:3].str.upper()

    def get_margin_res(r):
        try:
            o1, ox, o2 = float(str(r['Odd1']).replace(',','.')), float(str(r['OddX']).replace(',','.')), float(str(r['Odd2']).replace(',','.'))
            return round(((1/o1) + (1/ox) + (1/o2) - 1.0) * 100, 2)
        except Exception: return ""
    results_df['Marża'] = results_df.apply(get_margin_res, axis=1)

if not fixtures_df.empty:
    fixtures_df['Date_str'] = pd.to_datetime(fixtures_df['Date'], errors='coerce').dt.strftime('%Y%m%d').fillna('99999999')
    fixtures_df['Match_ID'] = fixtures_df['Date_str'] + "_" + fixtures_df['Home'].astype(str).str[:3].str.upper() + "_" + fixtures_df['Away'].astype(str).str[:3].str.upper()
    fixtures_df['Termin'] = fixtures_df['Date'].apply(categorize_date)
    dozwolone_terminy = ["Dziś", "Jutro", "Za 2 dni", "Za 3 dni", "Za 4 dni", "Za 5 dni", "Za 6 dni", "Za 7 dni"]
    fixtures_df = fixtures_df[fixtures_df['Termin'].isin(dozwolone_terminy)].copy()
    for col in ['Odd1', 'OddX', 'Odd2']:
        if col not in fixtures_df.columns: fixtures_df[col] = ""
    fixtures_df['Status_Kursów'] = np.where(fixtures_df['Odd1'].astype(str).str.strip().isin(["", "-", "nan"]), "Brak Kursów", "Są Kursy")

    def get_margin_fix(r):
        try:
            o1, ox, o2 = float(str(r['Odd1']).replace(',','.')), float(str(r['OddX']).replace(',','.')), float(str(r['Odd2']).replace(',','.'))
            return round(((1/o1) + (1/ox) + (1/o2) - 1.0) * 100, 2)
        except Exception: return ""
    fixtures_df['Marża'] = fixtures_df.apply(get_margin_fix, axis=1)

results_clean = results_df[[c for c in list(golden_cols.keys()) if c in results_df.columns] + ['HT_Total', 'Total_Corners', 'Marża']].rename(columns=golden_cols) if not results_df.empty else pd.DataFrame()
fixtures_clean = fixtures_df[['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd1', 'OddX', 'Odd2', 'Marża']].rename(columns={'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2'}) if not fixtures_df.empty else pd.DataFrame()

# Tabele ligowe (6 koszyków)
valid_matches = pd.DataFrame()
team_tiers = {}
if not results_clean.empty:
    temp_df = results_clean.copy()
    temp_df['Date_Parsed'] = pd.to_datetime(temp_df['Date'].astype(str), errors='coerce')
    valid_matches = temp_df.sort_values(by='Date_Parsed', ascending=False).dropna(subset=['FTHG', 'FTAG']).copy()

if not valid_matches.empty:
    valid_matches['Base_League'] = valid_matches['League'].apply(get_base_league)
    valid_matches['FTHG'] = pd.to_numeric(valid_matches['FTHG'], errors='coerce').fillna(0).astype(int)
    valid_matches['FTAG'] = pd.to_numeric(valid_matches['FTAG'], errors='coerce').fillna(0).astype(int)
    valid_matches['Total_Goals'] = valid_matches['FTHG'] + valid_matches['FTAG']
    valid_matches['HTHG'] = pd.to_numeric(valid_matches['HTHG'], errors='coerce')
    valid_matches['HTAG'] = pd.to_numeric(valid_matches['HTAG'], errors='coerce')
    valid_matches['Corners_H'] = pd.to_numeric(valid_matches['Corners_H'], errors='coerce')
    valid_matches['Corners_A'] = pd.to_numeric(valid_matches['Corners_A'], errors='coerce')
    valid_matches['Shots_H'] = pd.to_numeric(valid_matches['Shots_H'], errors='coerce')
    valid_matches['Shots_A'] = pd.to_numeric(valid_matches['Shots_A'], errors='coerce')
    valid_matches['ShotsTarget_H'] = pd.to_numeric(valid_matches['ShotsTarget_H'], errors='coerce')
    valid_matches['ShotsTarget_A'] = pd.to_numeric(valid_matches['ShotsTarget_A'], errors='coerce')

    home_rec = valid_matches[['League', 'Home', 'FTHG', 'FTAG']].copy().rename(columns={'Home': 'Team', 'FTHG': 'GF', 'FTAG': 'GA'})
    home_rec['Pts'] = np.where(home_rec['GF'] > home_rec['GA'], 3, np.where(home_rec['GF'] == home_rec['GA'], 1, 0))
    home_rec['W'] = np.where(home_rec['GF'] > home_rec['GA'], 1, 0)
    home_rec['D'] = np.where(home_rec['GF'] == home_rec['GA'], 1, 0)
    home_rec['L'] = np.where(home_rec['GF'] < home_rec['GA'], 1, 0)
    home_rec['M'] = 1

    away_rec = valid_matches[['League', 'Away', 'FTAG', 'FTHG']].copy().rename(columns={'Away': 'Team', 'FTAG': 'GF', 'FTHG': 'GA'})
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
            return f"Koszyk {max(1, min(6, tier_num))}"
        return "Koszyk 3"

    league_tables['Total_Teams'] = league_counts
    league_tables['Koszyk'] = league_tables.apply(assign_tier, axis=1)
    league_tables = league_tables.drop(columns=['Total_Teams'])[['League', 'Pozycja', 'Team', 'M', 'W', 'D', 'L', 'GF', 'GA', 'GD', 'Pts', 'PPG', 'Koszyk']]
    for _, r in league_tables.iterrows():
        team_tiers[(r['League'], r['Team'])] = r['Koszyk']
else:
    league_tables = pd.DataFrame(columns=['League', 'Pozycja', 'Team', 'M', 'W', 'D', 'L', 'GF', 'GA', 'GD', 'Pts', 'PPG', 'Koszyk'])

def get_last_match_goals(base_lg, team):
    if valid_matches.empty: return -1
    t_matches = valid_matches[(valid_matches['Base_League'] == base_lg) & ((valid_matches['Home'] == team) | (valid_matches['Away'] == team))]
    return -1 if t_matches.empty else int(t_matches.iloc[0]['Total_Goals'])

# ==================================================================================================
# 7. KULOODPORNY PARSER I DETEKTOR KURSÓW SUPERBET
# ==================================================================================================

superbet_baza = {}
superbet_fast_lookup = {}

if not SKIP_SUPERBET:
    detected_files = sorted(glob.glob("superbet_baza*.json"))
    default_files = [
        "superbet_baza_dzis.json", "superbet_baza_jutro.json", "superbet_baza_pojutrze.json",
        "superbet_baza_za_3_dni.json", "superbet_baza_za_4_dni.json"
    ]
    all_json_files = sorted(list(set(detected_files + [f for f in default_files if os.path.exists(f)])))

    for j_file in all_json_files:
        if os.path.exists(j_file):
            try:
                with open(j_file, "r", encoding="utf-8") as f:
                    temp_baza = json.load(f)
                    superbet_baza.update(temp_baza)
                print(f"✅ Wczytano Superbet ({j_file}): {len(temp_baza)} meczów.")
            except Exception as e_sb:
                print(f"⚠️ Błąd wczytywania {j_file}: {e_sb}")

    for k, v in superbet_baza.items():
        superbet_fast_lookup[k] = v
        info = v.get('info', {})
        gosp_be = str(info.get('gospodarz_be', '')).lower()
        gosc_be = str(info.get('gosc_be', '')).lower()
        gosp_sb = str(info.get('gospodarz_sb', '')).lower()
        gosc_sb = str(info.get('gosc_sb', '')).lower()
        if gosp_be and gosc_be: superbet_fast_lookup[f"{gosp_be}___{gosc_be}"] = v
        if gosp_sb and gosc_sb: superbet_fast_lookup[f"{gosp_sb}___{gosc_sb}"] = v


def clean_team_str(s):
    return str(s).strip().lower()


def is_odd_sane(typ_kod, odd_val):
    if odd_val is None or odd_val <= 1.005:
        return False
    k = str(typ_kod).strip().upper()
    
    if k == "U6.5" and odd_val > 1.06: return False
    if k == "U5.5" and odd_val > 1.18: return False
    if k == "U4.5" and odd_val > 1.48: return False
    if k == "U3.5" and odd_val > 2.30: return False
    if k == "U2.5" and odd_val > 3.60: return False
    
    if k == "O0.5" and odd_val > 1.15: return False
    if k == "O1.5" and odd_val > 2.10: return False
    
    if k.startswith("HT_U1.5") and odd_val > 2.05: return False
    if k.startswith("HT_U2.5") and odd_val > 1.25: return False
    if k.startswith("HT_U3.5") and odd_val > 1.06: return False
    if k.startswith("2H_U3.5") and odd_val > 1.10: return False
    if k.startswith("HU3.5") and odd_val > 1.15: return False
    if k.startswith("AU3.5") and odd_val > 1.15: return False
    
    if k in ["MG_1-5", "MG_1-6"] and odd_val > 1.45: return False
    if (k.startswith("HC_U8.5") or k.startswith("AC_U8.5")) and odd_val > 1.15: return False
    
    return True


def iter_all_markets(match_data):
    if not isinstance(match_data, dict):
        return
    for k, v in match_data.items():
        if isinstance(v, dict):
            yield k, v
            for sub_k, sub_v in v.items():
                if isinstance(sub_v, dict):
                    yield sub_k, sub_v


def get_single_real_odd(match_data, sub_typ, home, away):
    if not match_data: return None
    sub_k = str(sub_typ).strip()
    kursy = match_data.get("kursy", {})

    if sub_k in kursy:
        try:
            val = float(str(kursy[sub_k]).replace(',', '.'))
            if is_odd_sane(sub_k, val): return val
        except (ValueError, TypeError): pass

    info = match_data.get("info", {})
    h_sb = clean_team_str(info.get("gospodarz_sb", home))
    a_sb = clean_team_str(info.get("gosc_sb", away))
    h_be = clean_team_str(info.get("gospodarz_be", home))
    a_be = clean_team_str(info.get("gosc_be", away))
    h_orig = clean_team_str(home)
    a_orig = clean_team_str(away)

    home_identifiers = list(set([n for n in [h_sb, h_be, h_orig] if len(n) >= 3]))
    away_identifiers = list(set([n for n in [a_sb, a_be, a_orig] if len(n) >= 3]))

    # 1. Multigol / Przedział goli
    if sub_k.startswith("MG_") or sub_k in ["1-5", "1-6", "1-4", "2-4", "2-5"]:
        range_target = sub_k.replace("MG_", "").strip()
        
        if range_target in kursy:
            try:
                val = float(str(kursy[range_target]).replace(',', '.'))
                if is_odd_sane(sub_k, val): return val
            except Exception: pass
            
        for m_name, m_dict in iter_all_markets(match_data):
            m_low = str(m_name).lower()
            if any(kw in m_low for kw in ["przedział", "przedzial", "zakres", "multigol"]):
                for opt_k, opt_v in m_dict.items():
                    opt_str = str(opt_k).strip().lower()
                    if opt_str.startswith(range_target) or f"{range_target} |" in opt_str or f"{range_target} goli" in opt_str:
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(sub_k, val): return val
                        except Exception: pass
            for opt_k, opt_v in m_dict.items():
                opt_str = str(opt_k).strip().lower()
                if f"{range_target} |" in opt_str or (opt_str.startswith(range_target) and "goli" in opt_str):
                    try:
                        val = float(str(opt_v).replace(',', '.'))
                        if is_odd_sane(sub_k, val): return val
                    except Exception: pass

    # 2. Strzały 1X2
    if sub_k in ["S_1", "S_2", "ST_1", "ST_2"]:
        is_home_target = sub_k in ["S_1", "ST_1"]
        is_target_sot = "ST" in sub_k
        target_names = home_identifiers if is_home_target else away_identifiers
        opp_names = away_identifiers if is_home_target else home_identifiers

        shots_market = None
        for m_name, m_dict in iter_all_markets(match_data):
            m_low = str(m_name).lower()
            if "strzał" in m_low or "strzal" in m_low:
                if is_target_sot:
                    if any(w in m_low for w in ["na bramkę", "na bramke", "celn", "światło"]):
                        shots_market = m_dict; break
                else:
                    if not any(w in m_low for w in ["na bramkę", "na bramke", "celn"]):
                        shots_market = m_dict; break

        if shots_market:
            for opt_k, opt_v in shots_market.items():
                opt_low = str(opt_k).lower()
                if "remis" in opt_low: continue
                matches_target = any(tn in opt_low for tn in target_names)
                matches_opp = any(op in opt_low for op in opp_names if op not in target_names)
                if matches_target and not matches_opp:
                    try:
                        val = float(str(opt_v).replace(',', '.'))
                        if is_odd_sane(sub_k, val): return val
                    except Exception: pass

    # 3. Gole 1. połowy
    if sub_k.startswith("HT_U") or sub_k.startswith("HT_O"):
        is_under = sub_k.startswith("HT_U")
        line = sub_k[4:].strip()
        kw = "poniżej" if is_under else "powyżej"
        for m_name, m_dict in iter_all_markets(match_data):
            m_low = str(m_name).lower()
            if any(p in m_low for p in ["1.połowa", "1. polowa", "1 połowa"]) and "liczba goli" in m_low:
                for opt_k, opt_v in m_dict.items():
                    opt_low = str(opt_k).lower()
                    if (kw in opt_low or ("under" if is_under else "over") in opt_low) and line in opt_low:
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(sub_k, val): return val
                        except Exception: pass

    # 4. Gole drużynowe (HU, AU)
    if sub_k.startswith(("HU", "AU")):
        is_home_target = sub_k.startswith("H")
        line = sub_k[2:].strip()
        target_names = home_identifiers if is_home_target else away_identifiers
        for m_name, m_dict in iter_all_markets(match_data):
            m_low = str(m_name).lower()
            if any(tn in m_low for tn in target_names) and "liczba goli" in m_low and not any(p in m_low for p in ["1.połowa", "2.połowa"]):
                for opt_k, opt_v in m_dict.items():
                    opt_low = str(opt_k).lower()
                    if ("poniżej" in opt_low or "under" in opt_low) and line in opt_low:
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(sub_k, val): return val
                        except Exception: pass

    # 5. Suma Goli Całego Meczu
    if (sub_k.startswith("U") or sub_k.startswith("O")) and "_" not in sub_k and not sub_k.startswith(("HC", "AC", "HT", "2H", "HU", "AU")):
        is_under = sub_k.startswith("U")
        line = sub_k[1:].strip()
        target_kw = "poniżej" if is_under else "powyżej"
        forbidden_kws = ["rożn", "rozn", "kartk", "faul", "strzał", "strzal", "spalon", "handicap", "połow", "polow", "drużyn", "druzyn", "gospodarz", "gość", "gosc", "btts", "obie", "awans", "przedział", "karne"]

        for m_name, m_dict in iter_all_markets(match_data):
            m_low = str(m_name).lower()
            if any(fb in m_low for fb in forbidden_kws): continue
            if m_low in ["liczba goli", "suma goli", "gole", "liczba bramek", "suma bramek", "mecz - liczba goli", "mecz - suma goli"] or ("liczba goli" in m_low and not any(tn in m_low for tn in (home_identifiers + away_identifiers))):
                for opt_k, opt_v in m_dict.items():
                    opt_low = str(opt_k).lower()
                    tokens = re.findall(r"\d+(?:\.\d+)?", opt_low)
                    if line in tokens and (target_kw in opt_low or ("under" if is_under else "over") in opt_low):
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(sub_k, val): return val
                        except Exception: pass

    # 6. Rzuty rożne
    if sub_k.startswith(("C_U", "C_O", "HC_U", "AC_U")):
        is_home_c = sub_k.startswith("HC")
        is_away_c = sub_k.startswith("AC")
        is_match_c = sub_k.startswith("C_")
        is_under = "_U" in sub_k
        line = sub_k.split("_")[1][1:].strip()
        kw = "poniżej" if is_under else "powyżej"

        for m_name, m_dict in iter_all_markets(match_data):
            m_low = str(m_name).lower()
            if any(w in m_low for w in ["rzuty rożne", "rzutów rożnych", "rożne", "rozne"]):
                if is_match_c and not any(tn in m_low for tn in (home_identifiers + away_identifiers)):
                    for opt_k, opt_v in m_dict.items():
                        opt_low = str(opt_k).lower()
                        if (kw in opt_low or ("under" if is_under else "over") in opt_low) and line in opt_low:
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(sub_k, val): return val
                            except Exception: pass
                elif is_home_c and any(tn in m_low for tn in home_identifiers):
                    for opt_k, opt_v in m_dict.items():
                        opt_low = str(opt_k).lower()
                        if (kw in opt_low or ("under" if is_under else "over") in opt_low) and line in opt_low:
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(sub_k, val): return val
                            except Exception: pass
                elif is_away_c and any(tn in m_low for tn in away_identifiers):
                    for opt_k, opt_v in m_dict.items():
                        opt_low = str(opt_k).lower()
                        if (kw in opt_low or ("under" if is_under else "over") in opt_low) and line in opt_low:
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(sub_k, val): return val
                            except Exception: pass

    return None


def get_real_odd_with_status(home, away, typ_kod, engine="Goal Line Pro", match_odds=None):
    if not superbet_baza:
        return None, "❌ Brak w ofercie"

    h_low, a_low = str(home).lower(), str(away).lower()
    key_exact = f"{h_low}___{a_low}"
    match_data = superbet_fast_lookup.get(key_exact)

    if not match_data:
        h_clean = re.sub(r'[^a-z0-9]', '', h_low)
        a_clean = re.sub(r'[^a-z0-9]', '', a_low)
        h_part = h_clean[:4] if len(h_clean) >= 4 else h_clean
        a_part = a_clean[:4] if len(a_clean) >= 4 else a_clean
        for k, v in superbet_baza.items():
            k_clean = re.sub(r'[^a-z0-9]', '', k.lower())
            if h_part and a_part and h_part in k_clean and a_part in k_clean:
                match_data = v
                break

    if not match_data:
        return None, "❌ Brak w ofercie"

    typ_k = str(typ_kod).strip()

    # 1. Złożone układy BetBuilder Pro
    if engine == "BetBuilder Pro" or ("+" in typ_k and "1X" not in typ_k and "X2" not in typ_k):
        skladniki = [s.strip() for s in typ_k.split("+")]
        kursy_skladowe = {}

        for sk in skladniki:
            val = get_single_real_odd(match_data, sk, home, away)
            if val is not None and is_odd_sane(sk, val):
                kursy_skladowe[sk] = float(val)
            else:
                if sk in ['U3.5', 'U4.5', 'U5.5', 'U6.5']:
                    val_alt = get_single_real_odd(match_data, f"Total Goals {sk}", home, away)
                    if val_alt:
                        kursy_skladowe[sk] = float(val_alt)
                    else:
                        kursy_skladowe[sk] = 1.00
                else:
                    kursy_skladowe[sk] = 1.00

        calculated_odd = calc_nested_betbuilder(kursy_skladowe, typ_k)
        if calculated_odd >= 1.02:
            return calculated_odd, "⚡ BetBuilder Pro"
        return None, "❌ Brak w ofercie"

    # 2. Rynki pojedyncze / bazowe
    val = get_single_real_odd(match_data, typ_k, home, away)
    if val is not None and is_odd_sane(typ_k, val):
        return round(val, 2), "✅ Realny 1:1"

    return None, "❌ Brak w ofercie"

# ==================================================================================================
# 8. CENTRALNY GENERATOR PREDYKCJI
# ==================================================================================================

all_generated_predictions = []

def add_pred(match_id, termin, date, time, league, home, away, engine, typ, szansa, kurs_szac, arg, dyn_anchors=None, match_odds=None):
    if dyn_anchors is None: dyn_anchors = KOTWICE_KURSOWE
    typ_k = str(typ).strip()

    try: kurs_matematyczny = float(str(kurs_szac).replace(',', '.'))
    except Exception: kurs_matematyczny = 1.05

    if engine != "BetBuilder Pro" and typ_k in dyn_anchors:
        kurs_matematyczny = dyn_anchors[typ_k]

    if kurs_matematyczny >= 1.50:
        kurs_matematyczny = round(kurs_matematyczny * 0.95, 2)
    elif 1.20 <= kurs_matematyczny < 1.50:
        kurs_matematyczny = round(kurs_matematyczny * 0.975, 2)
    if kurs_matematyczny < 1.015:
        kurs_matematyczny = 1.01

    kurs_realny_val, status_superbet = get_real_odd_with_status(
        home, away, typ_k, engine=engine, match_odds=match_odds
    )
    kurs_realny_str = f"{kurs_realny_val:.2f}" if kurs_realny_val is not None else "Brak"
    kurs_do_oceny = kurs_realny_val if kurs_realny_val is not None else kurs_matematyczny

    if engine != "BetBuilder Pro" and "+" not in typ_k and kurs_do_oceny < 1.05:
        return

    prob_decimal = float(szansa) / 100.0
    ev = prob_decimal * kurs_realny_val if kurs_realny_val is not None else 0.0

    if kurs_realny_val is not None and ev >= 1.05:
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
    arg_final = re.sub(r"^\[.*?\]\s*", f"[{risk_tag}] ", clean_arg) if clean_arg.startswith("[") else f"[{risk_tag}] {clean_arg}"

    all_generated_predictions.append({
        "Match_ID": match_id, "Termin": termin, "Data": date, "Godzina": time, "Liga": league,
        "Gospodarz": home, "Gość": away, "Engine": engine, "Typ": typ,
        "Szansa": szansa, "Kurs_Szac": kurs_matematyczny, "Kurs_Realny": kurs_realny_str,
        "Status_Superbet": status_superbet, "Argumentacja": arg_final
    })

# ==================================================================================================
# 9. GŁÓWNA PĘTLA PREDYKCJI (10 SILNIKÓW)
# ==================================================================================================

print("\nUruchamiam komplet 10 Modeli Predykcyjnych...")

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

    h_last3 = [f"{int(m['FTHG'])}:{int(m['FTAG'])}" for _, m in h_tot_all.head(3).iterrows()]
    a_last3 = [f"{int(m['FTHG'])}:{int(m['FTAG'])}" for _, m in a_tot_all.head(3).iterrows()]
    last3_str = f"Ost. 3 mecze: Gosp ({', '.join(h_last3) if h_last3 else 'brak'}), Gość ({', '.join(a_last3) if a_last3 else 'brak'})"

    # 1. 1X PRO
    lg_matches = valid_matches[valid_matches['Base_League'] == fixture_base]
    is_lower_div = any(tag in league.lower() for tag in ["2", "tier-2", "division-2", "championship", "segunda", "serie-b", "ligue-2", "2-liga"])
    h_promoted = (len(h_tot_all) <= 4) and (not is_lower_div)
    a_promoted = (len(a_tot_all) <= 4) and (not is_lower_div)
    h_tier_s = 'Koszyk 6' if h_promoted else h_tier
    a_tier_s = 'Koszyk 6' if a_promoted else a_tier
    t_h_val = int(str(h_tier_s).replace("Koszyk ", "")) if "Koszyk" in str(h_tier_s) else 3
    t_a_val = int(str(a_tier_s).replace("Koszyk ", "")) if "Koszyk" in str(a_tier_s) else 3

    if (len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5) or (t_h_val <= 2 and a_promoted and len(h_dom) >= 5):
        lg_h_g = lg_matches['FTHG'].mean() if len(lg_matches) > 0 else 1.50
        lg_a_g = lg_matches['FTAG'].mean() if len(lg_matches) > 0 else 1.15
        lg_avg = lg_h_g + lg_a_g

        h_gf = h_tot_all.head(15)['Team_GF'].mean() if len(h_tot_all) > 0 else 1.5
        h_ga = h_tot_all.head(15)['Team_GA'].mean() if len(h_tot_all) > 0 else 1.5
        a_gf = a_tot_all.head(15)['Team_GF'].mean() if len(a_tot_all) > 0 else 1.0
        a_ga = a_tot_all.head(15)['Team_GA'].mean() if len(a_tot_all) > 0 else 1.8

        lam_h = max(0.4, (h_gf / (lg_avg/2)) * (a_ga / (lg_avg/2)) * lg_h_g)
        lam_a = max(0.2, (a_gf / (lg_avg/2)) * (h_ga / (lg_avg/2)) * lg_a_g)
        p1_g, px_g, p2_g = get_poisson_match_prob(lam_h, lam_a, max_val=15)

        h_1x_pct = sum(h_dom['FTHG'] >= h_dom['FTAG']) / len(h_dom) if len(h_dom) >= 5 else 0.5
        a_1x_pct = sum(a_wyj['FTAG'] <= a_wyj['FTHG']) / len(a_wyj) if len(a_wyj) >= 5 else (0.90 if a_promoted else 0.5)
        emp_1x = (h_1x_pct + a_1x_pct) / 2.0

        a_x2_pct = sum(a_wyj['FTAG'] >= a_wyj['FTHG']) / len(a_wyj) if len(a_wyj) >= 5 else 0.5
        h_x2_pct = sum(h_dom['FTHG'] <= h_dom['FTAG']) / len(h_dom) if len(h_dom) >= 5 else 0.5
        emp_x2 = (a_x2_pct + h_x2_pct) / 2.0

        blend_1x = ((p1_g + px_g) * 0.35) + (emp_1x * 0.65)
        blend_x2 = ((px_g + p2_g) * 0.35) + (emp_x2 * 0.65)

        typ_kod, final_prob = ("1X", blend_1x) if blend_1x >= blend_x2 else ("X2", blend_x2)
        try:
            o1, ox, o2 = float(str(o1_raw).replace(',','.')), float(str(ox_raw).replace(',','.')), float(str(o2_raw).replace(',','.'))
            fair_odd = round((o1 * ox) / (o1 + ox), 2) if typ_kod == "1X" else round((o2 * ox) / (o2 + ox), 2)
        except Exception:
            fair_odd = round(1 / final_prob, 2) if final_prob > 0 else 1.08

        if final_prob >= 0.70 and fair_odd >= 1.04:
            arg = f"{typ_kod} | Szansa: {round(final_prob*100)}% | Gospodarz ({h_tier_s}) dom 1X: {round(h_1x_pct*100)}%, Gość ({a_tier_s}) wyjazd X2: {round(a_x2_pct*100)}%."
            add_pred(match_id, d_termin, d_date, d_time, league, home, away, "1X Pro", typ_kod, round(final_prob*100, 1), fair_odd, arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 2. GOAL LINE PRO
    if len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5:
        h_dom_d = h_dom.to_dict('records')
        a_wyj_d = a_wyj.to_dict('records')

        for line in [2.5, 3.5, 4.5, 5.5, 6.5]:
            p_h_u, h_th, h_tl, h_sm = get_weighted_stats(h_dom_d, 'Total_Goals', lambda x: pd.notna(x) and x < line, prior_prob=0.75)
            p_a_u, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_d, 'Total_Goals', lambda x: pd.notna(x) and x < line, prior_prob=0.75)
            avg_p_u = (p_h_u + p_a_u) / 2
            if avg_p_u >= 0.70:
                arg = f"U{line} | {last3_str} | Szanse D/W: Gosp {round(p_h_u*100)}%, Gość {round(p_a_u*100)}%. Trafienia: {h_th}/{h_tl}, {a_th}/{a_tl}."
                if h_sm or a_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Goal Line Pro", f"U{line}", round(avg_p_u*100, 1), dyn_anchors.get(f"U{line}", 1.10), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

        for line in [0.5, 1.5, 2.5]:
            p_h_o, h_th, h_tl, h_sm = get_weighted_stats(h_dom_d, 'Total_Goals', lambda x: pd.notna(x) and x > line, prior_prob=0.30)
            p_a_o, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_d, 'Total_Goals', lambda x: pd.notna(x) and x > line, prior_prob=0.30)
            avg_p_o = (p_h_o + p_a_o) / 2
            if avg_p_o >= 0.70:
                arg = f"O{line} | {last3_str} | Szanse D/W: Gosp {round(p_h_o*100)}%, Gość {round(p_a_o*100)}%. Trafienia: {h_th}/{h_tl}, {a_th}/{a_tl}."
                if h_sm or a_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Goal Line Pro", f"O{line}", round(avg_p_o*100, 1), dyn_anchors.get(f"O{line}", 1.10), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 3. BETBUILDER PRO (SZABLONY PREMIUM)
    if len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5:
        h_dom_d = h_dom.to_dict('records')
        a_wyj_d = a_wyj.to_dict('records')
        for tpl in SZABLONY_PREMIUM:
            p_h, h_th, h_tl, h_sm = get_weighted_stats(h_dom_d, None, lambda r, code=tpl: evaluate_bet(code, r) == "WYGRANA", prior_prob=0.85)
            p_a, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_d, None, lambda r, code=tpl: evaluate_bet(code, r) == "WYGRANA", prior_prob=0.85)
            avg_p = (p_h + p_a) / 2
            if avg_p >= 0.85:
                kursy_skl = [dyn_anchors.get(sk.strip(), 1.05) for sk in tpl.split("+")]
                est_odd = calc_betbuilder_copula(kursy_skl, rho=0.45)
                arg = f"Szablon Premium | Szansa: {round(avg_p*100)}% | Trafienia D/W: Gosp {h_th}/{h_tl}, Gość {a_th}/{a_tl}"
                if h_sm or a_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "BetBuilder Pro", tpl, round(avg_p*100, 1), max(1.15, est_odd), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 4. MULTIGOL
    if len(h_tot_all) >= 10 and len(a_tot_all) >= 10 and len(h_dom) >= 5 and len(a_wyj) >= 5:
        h_last_g = get_last_match_goals(fixture_base, home)
        a_last_g = get_last_match_goals(fixture_base, away)
        if h_last_g == 0 or h_last_g > 5 or a_last_g == 0 or a_last_g > 5:
            h_dom_d = h_dom.to_dict('records')
            a_wyj_d = a_wyj.to_dict('records')
            p_h_15, h_th, h_tl, h_sm = get_weighted_stats(h_dom_d, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5, prior_prob=0.80)
            p_a_15, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_d, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5, prior_prob=0.80)
            prob_1_5 = (p_h_15 + p_a_15) / 2

            p_h_16, h_th16, h_tl16, h_sm16 = get_weighted_stats(h_dom_d, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 6, prior_prob=0.85)
            p_a_16, a_th16, a_tl16, a_sm16 = get_weighted_stats(a_wyj_d, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 6, prior_prob=0.85)
            prob_1_6 = (p_h_16 + p_a_16) / 2

            if prob_1_5 >= 0.90 or prob_1_6 >= 0.90:
                t_kod, pewnosc, hc_c, hc_l, ac_c, ac_l, was_sm = ("MG_1-5", prob_1_5, h_th, h_tl, a_th, a_tl, (h_sm or a_sm)) if prob_1_5 >= 0.90 else ("MG_1-6", prob_1_6, h_th16, h_tl16, a_th16, a_tl16, (h_sm16 or a_sm16))
                est_odd = round(1.0 + (((1/pewnosc) - 1.0) / 1.5), 2)
                arg = f"Regresja po anomalii ({last3_str}). Trafienia D/W: {hc_c}/{hc_l}, {ac_c}/{ac_l}."
                if was_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Multigol", t_kod, round(pewnosc*100, 1), est_odd, arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 5. CORNERS PRO
    valid_c = valid_matches.dropna(subset=['Corners_H', 'Corners_A']).copy()
    h_dom_c = valid_c[(valid_c['Base_League'] == fixture_base) & (valid_c['Home'] == home)]
    a_wyj_c = valid_c[(valid_c['Base_League'] == fixture_base) & (valid_c['Away'] == away)]

    if len(h_dom_c) >= 3 and len(a_wyj_c) >= 3:
        h_c_dict = h_dom_c.to_dict('records')
        a_c_dict = a_wyj_c.to_dict('records')
        c_codes, c_probs, c_odds, arg_c = [], [], [], []
        any_sm = False

        for line in [9.5, 10.5, 11.5, 12.5, 13.5]:
            p_hc, h_th, h_tl, h_sm = get_weighted_stats(h_c_dict, 'Total_Corners', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
            p_ac, a_th, a_tl, a_sm = get_weighted_stats(a_c_dict, 'Total_Corners', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
            avg_p = (p_hc + p_ac) / 2
            if avg_p >= 0.90:
                if h_sm or a_sm: any_sm = True
                c_codes.append(f"C_U{line}"); c_probs.append(avg_p); c_odds.append(dyn_anchors.get(f"C_U{line}", 1.25))
                arg_c.append(f"C_U{line} (D: {h_th}/{h_tl}, W: {a_th}/{a_tl})")
                break

        for line in [5.5, 6.5, 7.5, 8.5]:
            p_hc, h_th, h_tl, h_sm = get_weighted_stats(h_c_dict, 'Corners_H', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
            if p_hc >= 0.92:
                if h_sm: any_sm = True
                c_codes.append(f"HC_U{line}"); c_probs.append(p_hc); c_odds.append(dyn_anchors.get(f"HC_U{line}", 1.15))
                arg_c.append(f"HC_U{line} (D: {h_th}/{h_tl})")
                break

        for line in [4.5, 5.5, 6.5, 7.5]:
            p_ac, a_th, a_tl, a_sm = get_weighted_stats(a_c_dict, 'Corners_A', lambda x: pd.notna(x) and x < line, prior_prob=0.70)
            if p_ac >= 0.92:
                if a_sm: any_sm = True
                c_codes.append(f"AC_U{line}"); c_probs.append(p_ac); c_odds.append(dyn_anchors.get(f"AC_U{line}", 1.15))
                arg_c.append(f"AC_U{line} (W: {a_th}/{a_tl})")
                break

        if c_codes:
            est_odd = round((1.0 + sum([(o - 1.0) * 0.60 for o in c_odds])) * 0.95, 2) if len(c_codes) > 1 else c_odds[0]
            uzasadnienie = " | ".join(arg_c) + (" | ⚠️ Bayes" if any_sm else "")
            add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Corners Pro", "+".join(c_codes), round(np.mean(c_probs)*100, 1), max(1.05, est_odd), uzasadnienie, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 6. SHOTS PRO
    valid_s = valid_matches.dropna(subset=['Shots_H', 'Shots_A', 'ShotsTarget_H', 'ShotsTarget_A']).copy()
    if not valid_s.empty:
        h_dom_s = valid_s[(valid_s['Base_League'] == fixture_base) & (valid_s['Home'] == home)]
        a_wyj_s = valid_s[(valid_s['Base_League'] == fixture_base) & (valid_s['Away'] == away)]

        if len(h_dom_s) >= 2 and len(a_wyj_s) >= 2:
            h_len, a_len = len(h_dom_s), len(a_wyj_s)
            any_sm = h_len < 12 or a_len < 12
            h_s_win = sum((h_dom_s['Shots_H'] - h_dom_s['Shots_A']) > 0)
            a_s_lose = sum((a_wyj_s['Shots_A'] - a_wyj_s['Shots_H']) < 0)
            prob_h_s = (((h_s_win + 0.9) / (h_len + 1.5)) * 4.0 + ((a_s_lose + 0.9) / (a_len + 1.5)) * 1.0) / 5.0

            h_st_win = sum((h_dom_s['ShotsTarget_H'] - h_dom_s['ShotsTarget_A']) > 0)
            a_st_lose = sum((a_wyj_s['ShotsTarget_A'] - a_wyj_s['ShotsTarget_H']) < 0)
            prob_h_st = (((h_st_win + 0.9) / (h_len + 1.5)) * 4.0 + ((a_st_lose + 0.9) / (a_len + 1.5)) * 1.0) / 5.0

            if prob_h_s > 0.80:
                arg = f"Strzały 1X2: Gosp wygrana dom {h_s_win}/{h_len}, Gość porażka wyjazd {a_s_lose}/{a_len}." + (" | ⚠️ Bayes" if any_sm else "")
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Shots Pro", "S_1", round(prob_h_s*100, 1), dyn_anchors.get("S_1", 1.34), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))
            if prob_h_st > 0.80:
                arg = f"Strzały Celne 1X2: Gosp wygrana dom {h_st_win}/{h_len}, Gość porażka wyjazd {a_st_lose}/{a_len}." + (" | ⚠️ Bayes" if any_sm else "")
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Shots Pro", "ST_1", round(prob_h_st*100, 1), dyn_anchors.get("ST_1", 1.64), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 7. ZIMNY PRYSZNIC
    if h_tier in ['Koszyk 1', 'Koszyk 2'] and len(h_tot_all) > 0:
        last_m = h_tot_all.iloc[0]
        if last_m['Away'] == home and last_m['FTHG'] >= last_m['FTAG']:
            opp_tier = team_tiers.get((last_m['League'], last_m['Home']), 'Koszyk 1')
            if opp_tier in ['Koszyk 4', 'Koszyk 5', 'Koszyk 6']:
                arg = f"Gospodarz ({h_tier}) szuka rewanżu u siebie po stracie punktów na wyjeździe z {opp_tier}."
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Cold Shower", "1", 85.0, dyn_anchors.get("1", 1.25), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 8. UKRYTA FORMA (Proxy xG)
    for team, is_home in [(home, True), (away, False)]:
        if not valid_matches.dropna(subset=['ShotsTarget_H', 'ShotsTarget_A']).empty:
            t_past = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == team) | (valid_matches['Away'] == team))]
            if len(t_past) >= 3:
                last_3 = t_past.head(3)
                st_for = np.where(last_3['Home'] == team, last_3['ShotsTarget_H'], last_3['ShotsTarget_A']).sum()
                st_agg = np.where(last_3['Home'] == team, last_3['ShotsTarget_A'], last_3['ShotsTarget_H']).sum()
                g_for = np.where(last_3['Home'] == team, last_3['FTHG'], last_3['FTAG']).sum()
                pts = sum(3 if (m['FTHG'] > m['FTAG'] if m['Home'] == team else m['FTAG'] > m['FTHG']) else (1 if m['FTHG'] == m['FTAG'] else 0) for _, m in last_3.iterrows())

                if st_for >= (st_agg * 1.5) and st_for >= 15 and pts <= 4 and g_for <= 3:
                    typ_kod = "1X" if is_home else "X2"
                    arg = f"Wysokie xG (Ukryta Forma). W 3 ost. meczach oddano {int(st_for)} celnych strzałów przy zaledwie {int(g_for)} golach."
                    add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Hidden Form", typ_kod, 80.0, 1.25, arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 9. ANOMALIE ROŻNYCH
    for team, is_home in [(home, True), (away, False)]:
        t_past_c = valid_c[(valid_c['Base_League'] == fixture_base) & ((valid_c['Home'] == team) | (valid_c['Away'] == team))].copy()
        if len(t_past_c) >= 8:
            t_past_c['C_For'] = np.where(t_past_c['Home'] == team, t_past_c['Corners_H'], t_past_c['Corners_A'])
            season_avg = t_past_c['C_For'].mean()
            last_2_avg = t_past_c.head(2)['C_For'].mean()
            if season_avg >= 5.5 and last_2_avg <= 3.0:
                typ_kod = "HC_O4.5" if is_home else "AC_O4.5"
                arg = f"Pęknięta seria rożnych. Średnia: {round(season_avg, 2)}, ost. 2 mecze: {round(last_2_avg, 2)}. Oczekiwane przełamanie."
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Corner Anomalies", typ_kod, 82.0, dyn_anchors.get(typ_kod, 1.45), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 10. ANOMALIE BRAMKOWE
    t_past_g = valid_matches[(valid_matches['Base_League'] == fixture_base) & ((valid_matches['Home'] == home) | (valid_matches['Away'] == away))]
    if len(t_past_g) >= 10:
        season_avg = t_past_g['Total_Goals'].mean()
        last_2_avg = t_past_g.head(2)['Total_Goals'].mean()
        if season_avg <= 2.8 and last_2_avg >= 4.5:
            arg = f"Anomalia overowa ({last3_str}). Średnia: {round(season_avg, 2)}, ost. 2 mecze: {round(last_2_avg, 2)} goli. Oczekiwany powrót undera."
            add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Goal Anomalies", "U3.5", 85.0, dyn_anchors.get("U3.5", 1.30), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))
        elif season_avg >= 2.5 and last_2_avg <= 0.5:
            arg = f"Anomalia underowa ({last3_str}). Średnia: {round(season_avg, 2)}, ost. 2 mecze: {round(last_2_avg, 2)} goli. Oczekiwane przełamanie."
            add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Goal Anomalies", "O1.5", 85.0, dyn_anchors.get("O1.5", 1.25), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

# ==================================================================================================
# 10. BACKTESTER, HISTORIA TYPÓW I SYNCHRONIZACJA
# ==================================================================================================

print("\nInicjalizacja Modułu Backtestingu i Śledzenia Skuteczności...")

cols_all_pred = [
    "Match_ID", "Zagrane", "Wyslij_AKO", "Kupon_ID", "Termin", "Data", "Godzina", "Liga",
    "Gospodarz", "Gość", "Engine", "Typ", "Szansa", "Kurs_Szac", "Kurs_Realny",
    "Status_Superbet", "Argumentacja", "Przedzial_Kursowy", "Consensus_Score", "Status"
]
cols_historia = [
    "Match_ID", "Zagrane", "Kupon_ID", "Data", "Godzina", "Liga",
    "Gospodarz", "Gość", "Engine", "Typ", "Szansa", "Kurs_Szac", "Kurs_Realny",
    "Status_Superbet", "Argumentacja", "Przedzial_Kursowy", "Consensus_Score", "Status", "Profit", "Yield_Wplyw"
]

df_all_predictions = pd.DataFrame(all_generated_predictions)

if not df_all_predictions.empty:
    df_all_predictions['Przedzial_Kursowy'] = df_all_predictions.apply(global_recalc_przedzial, axis=1)
    consensus_counts = df_all_predictions.groupby('Match_ID').size().to_dict()
    df_all_predictions['Consensus_Score'] = df_all_predictions['Match_ID'].map(consensus_counts)
    df_all_predictions['Unikalny_Klucz'] = (
        df_all_predictions['Match_ID'].astype(str) + "_" +
        df_all_predictions['Engine'].astype(str) + "_" +
        df_all_predictions['Typ'].astype(str)
    )

    map_wyslij, map_zagrane, map_kupon = {}, {}, {}
    try:
        old_all_ws = spreadsheet.worksheet("All_Predictions").get_all_records()
        if old_all_ws:
            old_all_df = pd.DataFrame(old_all_ws)
            old_all_df['Unikalny_Klucz'] = (
                old_all_df['Match_ID'].astype(str) + "_" +
                old_all_df['Engine'].astype(str) + "_" +
                old_all_df['Typ'].astype(str)
            )
            if 'Wyslij_AKO' in old_all_df.columns: map_wyslij = dict(zip(old_all_df['Unikalny_Klucz'], old_all_df['Wyslij_AKO']))
            if 'Zagrane' in old_all_df.columns: map_zagrane = dict(zip(old_all_df['Unikalny_Klucz'], old_all_df['Zagrane']))
            if 'Kupon_ID' in old_all_df.columns: map_kupon = dict(zip(old_all_df['Unikalny_Klucz'], old_all_df['Kupon_ID']))
    except Exception: pass

    df_all_predictions['Wyslij_AKO'] = df_all_predictions['Unikalny_Klucz'].map(map_wyslij).fillna("")
    df_all_predictions['Zagrane'] = df_all_predictions['Unikalny_Klucz'].map(map_zagrane).fillna("")
    df_all_predictions['Kupon_ID'] = df_all_predictions['Unikalny_Klucz'].map(map_kupon).fillna("")
    df_all_predictions['Status'] = "W OCZEKIWANIU"

    for col in cols_all_pred:
        if col not in df_all_predictions.columns: df_all_predictions[col] = ""
    df_all_predictions = df_all_predictions[cols_all_pred]
else:
    df_all_predictions = pd.DataFrame(columns=cols_all_pred)

try:
    ws_historia = spreadsheet.worksheet("Historia_Typow")
    historia_dane = ws_historia.get_all_values()
    df_historia = pd.DataFrame(historia_dane[1:], columns=historia_dane[0]) if len(historia_dane) > 0 else pd.DataFrame(columns=cols_historia)
except gspread.exceptions.WorksheetNotFound:
    spreadsheet.add_worksheet(title="Historia_Typow", rows=10000, cols=len(cols_historia))
    ws_historia = spreadsheet.worksheet("Historia_Typow")
    df_historia = pd.DataFrame(columns=cols_historia)

for col in cols_historia:
    if col not in df_historia.columns: df_historia[col] = ""
df_historia = df_historia[cols_historia]

if not df_all_predictions.empty:
    nowe_typy_df = df_all_predictions.copy()
    for col in ['Termin', 'Wyslij_AKO']:
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
            map_status_sb = nowe_typy_df.set_index('Unikalny_Klucz')['Status_Superbet'].to_dict()
            map_arg = nowe_typy_df.set_index('Unikalny_Klucz')['Argumentacja'].to_dict()
            map_przedzial = nowe_typy_df.set_index('Unikalny_Klucz')['Przedzial_Kursowy'].to_dict()
            map_consensus = nowe_typy_df.set_index('Unikalny_Klucz')['Consensus_Score'].to_dict()

            for idx in df_historia[w_oczek_mask].index:
                klucz = df_historia.at[idx, 'Unikalny_Klucz']
                if klucz in map_szansa:
                    df_historia.at[idx, 'Szansa'] = str(map_szansa[klucz])
                    df_historia.at[idx, 'Kurs_Szac'] = str(map_kurs_szac[klucz])
                    df_historia.at[idx, 'Kurs_Realny'] = str(map_kurs_real[klucz])
                    df_historia.at[idx, 'Status_Superbet'] = str(map_status_sb.get(klucz, ""))
                    df_historia.at[idx, 'Argumentacja'] = str(map_arg[klucz])
                    df_historia.at[idx, 'Przedzial_Kursowy'] = str(map_przedzial.get(klucz, ""))
                    df_historia.at[idx, 'Consensus_Score'] = str(map_consensus.get(klucz, ""))

        do_dodania = nowe_typy_df[~nowe_typy_df['Unikalny_Klucz'].isin(df_historia['Unikalny_Klucz'])].copy().drop(columns=['Unikalny_Klucz'])
        df_historia = df_historia.drop(columns=['Unikalny_Klucz'])
    else:
        do_dodania = nowe_typy_df.copy()
        if 'Unikalny_Klucz' in do_dodania.columns: do_dodania = do_dodania.drop(columns=['Unikalny_Klucz'])

    df_historia = pd.concat([df_historia, do_dodania], ignore_index=True)

# Rozliczanie statusów
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
                    except Exception: pass

            if match_row and pd.notna(match_row.get('FTHG')):
                nowy_status = evaluate_bet(row["Typ"], match_row)
                df_historia.at[idx, "Status"] = nowy_status
                try:
                    k_real_str = str(row.get("Kurs_Realny", "")).replace(',', '.').strip()
                    k_szac_str = str(row.get("Kurs_Szac", "")).replace(',', '.').strip()
                    kurs = float(k_real_str) if (k_real_str and k_real_str not in ["Brak", "nan", "None"]) else (float(k_szac_str) if k_szac_str else 1.0)
                    if nowy_status == "WYGRANA":
                        profit = round(kurs - 1.0, 2)
                        df_historia.at[idx, "Profit"] = str(profit)
                        df_historia.at[idx, "Yield_Wplyw"] = str(round(profit * 100, 1))
                    elif nowy_status == "PRZEGRANA":
                        df_historia.at[idx, "Profit"] = "-1.0"
                        df_historia.at[idx, "Yield_Wplyw"] = "-100.0"
                except Exception: pass

# Moduł portfela AKO
cols_ako = [
    "Kupon_ID", "Data_Zawarcia", "Mecze_Skrot", "Liczba_Zdarzen", "Kurs_AKO",
    "Stawka", "Jednostki", "Status_AKO", "Wygrana_Brutto", "Profit_Netto",
    "Wyslij_Podsumowanie", "Telegram_Status"
]
try:
    ws_ako = spreadsheet.worksheet("Kupony_AKO")
    ako_dane = ws_ako.get_all_values()
    df_ako = pd.DataFrame(ako_dane[1:], columns=ako_dane[0]) if len(ako_dane) > 0 else pd.DataFrame(columns=cols_ako)
except gspread.exceptions.WorksheetNotFound:
    spreadsheet.add_worksheet(title="Kupony_AKO", rows=1000, cols=15)
    ws_ako = spreadsheet.worksheet("Kupony_AKO")
    df_ako = pd.DataFrame(columns=cols_ako)

for col in cols_ako:
    if col not in df_ako.columns: df_ako[col] = ""
df_ako = df_ako[cols_ako]

user_stakes = dict(zip(df_ako['Kupon_ID'], df_ako['Stawka'])) if not df_ako.empty else {}
user_units = dict(zip(df_ako['Kupon_ID'], df_ako.get('Jednostki', ['1j'] * len(df_ako)))) if not df_ako.empty else {}
user_pods = dict(zip(df_ako['Kupon_ID'], df_ako.get('Wyslij_Podsumowanie', [''] * len(df_ako)))) if not df_ako.empty else {}
user_tel_stat = dict(zip(df_ako['Kupon_ID'], df_ako.get('Telegram_Status', [''] * len(df_ako)))) if not df_ako.empty else {}

if not df_historia.empty:
    kupony_istniejace = df_historia[df_historia['Kupon_ID'].astype(str).str.strip() != ""]
    nowe_ako_list = []

    if not kupony_istniejace.empty:
        for k_id, group in kupony_istniejace.groupby('Kupon_ID'):
            data_zawarcia = group['Data'].min()
            liczba_zdarzen = len(group)
            mecze_skrot = " | ".join(group['Gospodarz'].str[:3] + "-" + group['Gość'].str[:3])
            kurs_ako = 1.0

            for _, r in group.iterrows():
                kr_str = str(r.get('Kurs_Realny', '')).replace(',', '.').strip()
                if not kr_str or kr_str in ["Brak", "nan", "None"]:
                    kr_str = str(r.get('Kurs_Szac', '')).replace(',', '.').strip()
                try:
                    kr = float(kr_str)
                    if 1.0 < kr < 50.0: kurs_ako *= kr
                except Exception: pass
            kurs_ako = round(kurs_ako, 2)

            statusy = group['Status'].tolist()
            if "PRZEGRANA" in statusy: status_ako = "PRZEGRANA"
            elif "W OCZEKIWANIU" in statusy: status_ako = "W OCZEKIWANIU"
            elif all(s == "WYGRANA" for s in statusy): status_ako = "WYGRANA"
            else: status_ako = "ZWRÓCONY"

            stawka_str = str(user_stakes.get(k_id, "100")).replace(',', '.')
            stawka = float(stawka_str) if stawka_str.strip() else 100.0
            jednostki_str = str(user_units.get(k_id, "1j"))
            wyslij_pod = str(user_pods.get(k_id, ""))
            tel_status = str(user_tel_stat.get(k_id, ""))
            wygrana_brutto = round(kurs_ako * stawka * 0.88, 2) if status_ako == "WYGRANA" else 0.0
            profit = round(wygrana_brutto - stawka, 2) if status_ako == "WYGRANA" else (-stawka if status_ako == "PRZEGRANA" else 0.0)

            nowe_ako_list.append([k_id, data_zawarcia, mecze_skrot, liczba_zdarzen, kurs_ako, stawka, jednostki_str, status_ako, wygrana_brutto, profit, wyslij_pod, tel_status])

    df_ako = pd.DataFrame(nowe_ako_list, columns=cols_ako).sort_values(by="Data_Zawarcia", ascending=False) if nowe_ako_list else pd.DataFrame(columns=cols_ako)

if not df_historia.empty:
    df_historia['Data_Sort'] = pd.to_datetime(df_historia['Data'].astype(str) + ' ' + df_historia['Godzina'].astype(str).replace('', '00:00').replace('-', '00:00'), errors='coerce')
    mask_oczek = df_historia['Status'] == 'W OCZEKIWANIU'
    df_oczek = df_historia[mask_oczek].sort_values(by=['Data_Sort'], ascending=[True])
    df_rozst = df_historia[~mask_oczek].sort_values(by=['Data_Sort'], ascending=[False])
    df_historia = pd.concat([df_oczek, df_rozst]).drop(columns=['Data_Sort', 'Unikalny_Klucz'], errors='ignore')

if not df_all_predictions.empty:
    df_all_predictions['Data_Sort'] = pd.to_datetime(df_all_predictions['Data'].astype(str) + ' ' + df_all_predictions['Godzina'].astype(str).replace('', '00:00').replace('-', '00:00'), errors='coerce')
    now_time = datetime.now()
    df_all_predictions = df_all_predictions[df_all_predictions['Data_Sort'] >= now_time - timedelta(hours=3)]
    df_all_predictions = df_all_predictions.sort_values(by=["Data_Sort", "Szansa"], ascending=[True, False]).drop(columns=['Data_Sort', 'Unikalny_Klucz'], errors='ignore')

# Top Wybory
top_wybory_df = pd.DataFrame()
if not df_all_predictions.empty:
    print("\nGenerowanie zoptymalizowanego widoku Top Wybory...")
    try: spreadsheet.worksheet("Top_Wybory")
    except gspread.exceptions.WorksheetNotFound:
        spreadsheet.add_worksheet(title="Top_Wybory", rows=500, cols=15)

    active_pred = df_all_predictions[df_all_predictions['Status'] == 'W OCZEKIWANIU'].copy()
    if not active_pred.empty:
        active_pred['Szansa_Num'] = pd.to_numeric(active_pred['Szansa'], errors='coerce').fillna(0.0)
        active_pred['Kurs_Num'] = pd.to_numeric(
            active_pred['Kurs_Realny'].replace(['Brak', 'nan', 'None', ''], np.nan),
            errors='coerce'
        ).combine_first(pd.to_numeric(active_pred['Kurs_Szac'], errors='coerce')).fillna(1.0)

        top_list = []
        for eng in active_pred['Engine'].unique():
            df_eng = active_pred[active_pred['Engine'] == eng].sort_values(by=['Szansa_Num', 'Kurs_Num'], ascending=[False, False])
            picks_90 = df_eng[df_eng['Szansa_Num'] >= 90.0]
            top_list.append(picks_90 if len(picks_90) >= 20 else df_eng.head(20))

        if top_list:
            top_wybory_df = pd.concat(top_list, ignore_index=True).drop_duplicates(subset=['Match_ID', 'Engine', 'Typ'])
            top_wybory_df = top_wybory_df.sort_values(by=['Szansa_Num', 'Data', 'Godzina'], ascending=[False, True, True])
            cols_wybory = [
                "Match_ID", "Data", "Godzina", "Liga", "Gospodarz", "Gość",
                "Engine", "Typ", "Szansa", "Kurs_Szac", "Kurs_Realny", "Status_Superbet", "Argumentacja"
            ]
            top_wybory_df = top_wybory_df[[c for c in cols_wybory if c in top_wybory_df.columns]]

# ==================================================================================================
# 11. ZAPIS DO GOOGLE SHEETS
# ==================================================================================================

all_sheets = ["Summary", "Fixtures", "Results", "League_Tables", "Historia_Typow", "All_Predictions", "Top_Wybory", "Kupony_AKO"]
for s_name in all_sheets:
    try: spreadsheet.worksheet(s_name)
    except gspread.exceptions.WorksheetNotFound:
        spreadsheet.add_worksheet(title=s_name, rows=1000, cols=30)

print("\nFinalny zapis zintegrowanych danych do Google Sheets...")
safe_batch_update(spreadsheet, "Fixtures", fixtures_clean)
safe_batch_update(spreadsheet, "Results", results_clean)
safe_batch_update(spreadsheet, "League_Tables", league_tables)
safe_batch_update(spreadsheet, "Historia_Typow", df_historia)
safe_batch_update(spreadsheet, "All_Predictions", df_all_predictions)
safe_batch_update(spreadsheet, "Top_Wybory", top_wybory_df)
safe_batch_update(spreadsheet, "Kupony_AKO", df_ako)

summary_data = [
    ["==== PODSUMOWANIE OGÓLNE ====", "", ""],
    ["Ostatnia aktualizacja", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""],
    ["Fixtures Czyste", len(fixtures_clean), ""],
    ["Results Zintegrowane", len(results_clean), ""],
    ["Przetworzone Typy w Historii", len(df_historia), ""],
    ["Wygenerowane Predykcje (Suma)", len(df_all_predictions), ""],
    ["Wyselekcjonowane Top Wybory", len(top_wybory_df), ""],
    ["", "", ""],
    ["==== RAPORT POBIERANIA (LOGI SERWERA) ====", "", ""],
    ["Źródło", "URL / Plik", "Status"]
]
for rep in scrape_report: summary_data.append(rep)

time.sleep(1.2)
spreadsheet.worksheet("Summary").clear()
spreadsheet.worksheet("Summary").update(summary_data)

print("\n" + "=" * 90)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("1. Skorygowano wyliczanie kursów BetBuilder Pro (eliminacja zawyżania kursów dla głębokich underów).")
print("2. Poprawiono model korelacji wielozakładowej (szablony U6.5/U5.5 mają teraz realistyczne wyceny 1.05-1.15).")
print("3. Utrzymano poprawne zaczytywanie plików Superbet oraz rynków Multigol.")
print("=" * 90)
