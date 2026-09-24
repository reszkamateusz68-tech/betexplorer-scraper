"""
====================================================================================================
PROJEKT: STATLAB ANALYTICS - BETEXPLORER MASTER ENGINE & VALUE SCANNER
MODUŁ: betexplorer_all.py
OPIS: Zunifikowany silnik analityczny łączący modelowanie rozkładów Poissona/Skellama,
      kopułę korelacyjną BetBuilder Pro, zaawansowane filtry anomalii (Cold Shower, Hidden Form,
      Goal Anomalies, Corner Anomalies), dwustronne statystyki ofensywy i defensywy rywala
      oraz zintegrowany, wielopoziomowy matcher kursów Superbet i Fortuna 1:1.
      W pełni darmowy, zoptymalizowany pod kątem pracy lokalnej (offline sandbox) i online (GitHub Actions).
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
SKIP_BOOKMAKERS = os.environ.get("SKIP_BOOKMAKERS", "false").strip().lower() in ["true", "1", "t", "yes"]
OFFLINE_LOCAL = os.environ.get("OFFLINE_LOCAL", "true").strip().lower() in ["true", "1", "t", "yes"]

# Dynamiczna detekcja katalogu danych
if os.path.exists("dane_testowe_weekend"):
    DATA_DIR = "dane_testowe_weekend"
elif os.path.exists("superbet_baza_5dni.json") or os.path.exists("Results.csv"):
    DATA_DIR = "."
else:
    DATA_DIR = "."

MAX_WORKERS_BETEXPLORER = 2 if TEST_MODE else 5
MAX_WORKERS_SOCCERSTATS = 2 if TEST_MODE else 6
MAX_WORKERS_FOOTBALLDATA = 2 if TEST_MODE else 6

today = datetime.now()

print("=" * 90)
print(f"URUCHOMIENIE SILNIKA STATLAB ANALYTICS | DATA: {today.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"TRYB TESTOWY: {'WŁĄCZONY' if TEST_MODE else 'WYŁĄCZONY'}")
print(f"TRYB LOKALNEJ SYMULACJI (OFFLINE): {'WŁĄCZONY (Zapis lokalny do CSV)' if OFFLINE_LOCAL else 'WYŁĄCZONY (Tryb online Google Sheets)'}")
print(f"KATALOG DANYCH: {DATA_DIR}")
print("=" * 90)

# ==================================================================================================
# 1. POŁĄCZENIE Z GOOGLE SHEETS LUB TRYB OFFLINE
# ==================================================================================================

scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

spreadsheet = None

if not OFFLINE_LOCAL:
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
else:
    print("🟡 TRYB TESTOWY OFFLINE: Pomijam autoryzację Google Sheets API. Wyniki trafią do plików CSV w 'wyniki_testowe/'.")

# ==================================================================================================
# 2. SŁOWNIKI NAZW DRUŻYN
# ==================================================================================================

scrape_report = []
mapowanie_fd = {}
mapowanie_ss = {}
mapowanie_sb_raw = {}
mapowanie_fortuna_raw = {}

dict_path = os.path.join(DATA_DIR, "slownik_druzyn.json") if os.path.exists(os.path.join(DATA_DIR, "slownik_druzyn.json")) else "slownik_druzyn.json"
try:
    if os.path.exists(dict_path):
        with open(dict_path, "r", encoding="utf-8") as f:
            slownik = json.load(f)
            mapowanie_fd = slownik.get("FootballData_To_BetExplorer", {})
            mapowanie_ss = slownik.get("SoccerStats_To_BetExplorer", {})
            mapowanie_sb_raw = slownik.get("BetExplorer_To_Superbet", {})
            mapowanie_fortuna_raw = slownik.get("BetExplorer_To_Fortuna", {})
        print(f"✅ Wczytano słownik drużyn: {len(mapowanie_fd)} FD, {len(mapowanie_ss)} SS, {len(mapowanie_sb_raw)} SB, {len(mapowanie_fortuna_raw)} Fortuna.")
except Exception as err_slownik:
    print(f"⚠️ Uwaga przy wczytywaniu slownik_druzyn.json: {err_slownik}")

# ==================================================================================================
# 3. FUNKCJE POMOCNICZE
# ==================================================================================================

def global_recalc_przedzial(row):
    try:
        ks_sb = str(row.get('Kurs_Realny_Superbet', '')).replace(',', '.').strip()
        ks_ft = str(row.get('Kurs_Realny_Fortuna', '')).replace(',', '.').strip()
        ks_szac = str(row.get('Kurs_Szac', '')).replace(',', '.').strip()

        realne = []
        for v in [ks_sb, ks_ft]:
            if v not in ["", "-", "nan", "None", "Brak"]:
                try: realne.append(float(v))
                except: pass

        if realne: ks = max(realne)
        elif ks_szac not in ["", "-", "nan", "None", "Brak"]: ks = float(ks_szac)
        else: return "Brak kursu"

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
            except Exception: pass
    else:
        if len(value.split('.')) >= 3:
            try:
                if value.endswith("."):
                    d, m = value.rstrip(".").split(".")
                    return datetime(today.year, int(m), int(d)).strftime('%Y-%m-%d'), ""
                else:
                    return datetime.strptime(value, "%d.%m.%Y").strftime('%Y-%m-%d'), ""
            except Exception: pass
    return value, ""


def categorize_date(d_str):
    if pd.isna(d_str) or str(d_str).strip() in ["", "nan", "NaT", "None"]:
        return "Nieznany"
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
                    if str_val.endswith(".0"): new_row.append(str_val[:-2])
                    else: new_row.append(str_val)
        output.append(new_row)
    return output


def safe_batch_update(spreadsheet_obj, ws_name, df_data):
    if df_data.empty: return
    if OFFLINE_LOCAL or spreadsheet_obj is None:
        os.makedirs("wyniki_testowe", exist_ok=True)
        out_path = os.path.join("wyniki_testowe", f"{ws_name}.csv")
        df_data.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"💾 [OFFLINE] Zapisano tabelę '{ws_name}' -> {out_path} ({len(df_data)} wierszy).")
        return

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
        if calc_type == "exact": return (math.exp(-lam) * (lam ** k)) / math.factorial(int(k))
        elif calc_type == "under": return sum((math.exp(-lam) * (lam ** i)) / math.factorial(i) for i in range(int(k) + 1))
        elif calc_type == "over": return 1.0 - sum((math.exp(-lam) * (lam ** i)) / math.factorial(i) for i in range(int(k) + 1))
    except Exception: return 0.0
    return 0.0


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


def calc_betbuilder_copula(odds_list, rho=0.55):
    valid_odds = [float(o) for o in odds_list if float(o) > 1.005]
    if not valid_odds: return 1.00
    if len(valid_odds) == 1: return valid_odds[0]

    q_list = [1.0 / o for o in valid_odds]
    q_list.sort(reverse=True)
    
    q_joint = q_list[0]
    for q_next in q_list[1:]:
        gamma = 1.0 - rho * (1.0 - min(q_joint, q_next))
        q_joint = q_joint * (q_next ** gamma)
        
    calc_odd = max(1.02, round(1.0 / q_joint, 2))
    return max(calc_odd, max(valid_odds))


def calc_nested_betbuilder(sub_odds_dict, tpl):
    tokens = [t.strip() for t in tpl.split("+")]
    real_sub_odds = [float(sub_odds_dict.get(t, 1.0)) for t in tokens if float(sub_odds_dict.get(t, 1.0)) > 1.005]
    max_single_odd = max(real_sub_odds) if real_sub_odds else 1.00

    # 1. BetBuilder rożnych lub rynków mieszanych
    if any(t.startswith(("C_", "HC_", "AC_")) for t in tokens) or any(t.startswith(("S_", "ST_")) for t in tokens):
        valid = [float(sub_odds_dict.get(t, 1.0)) for t in tokens if float(sub_odds_dict.get(t, 1.0)) > 1.005]
        calc = calc_betbuilder_copula(valid, rho=0.55) if valid else 1.08
        return max(calc, max_single_odd)

    # 2. BetBuilder bramkowy (szablony zagnieżdżone)
    main_under = next((t for t in tokens if t in ['U6.5', 'U5.5', 'U4.5', 'U3.5']), None)
    base_odd = 1.04
    if main_under:
        val_u = float(sub_odds_dict.get(main_under, 1.0))
        if val_u > 1.005:
            base_odd = val_u
        else:
            defaults = {'U6.5': 1.015, 'U5.5': 1.03, 'U4.5': 1.09, 'U3.5': 1.25}
            base_odd = defaults.get(main_under, 1.04)

    if 'O0.5' in tokens:
        odd_o05 = float(sub_odds_dict.get('O0.5', 1.04))
        if odd_o05 <= 1.005: odd_o05 = 1.04
        q_u = 1.0 / base_odd
        q_o = 1.0 / odd_o05
        q_joint = max(0.60, q_u + q_o - 1.0)
        base_odd = max(1.04, round(1.0 / q_joint, 2))

    extra_boost = 0.0
    for st in [t for t in tokens if t not in ['O0.5', main_under]]:
        k_val = float(sub_odds_dict.get(st, 1.0))
        if k_val > 1.005:
            extra_boost += (k_val - 1.0) * 0.20

    final_odd = base_odd + extra_boost
    final_odd = max(final_odd, max_single_odd)

    if "U6.5" in tpl: final_odd = min(final_odd, 1.15)
    elif "U5.5" in tpl and "O0.5" in tpl: final_odd = min(final_odd, 1.25)
    elif "U4.5" in tpl: final_odd = min(final_odd, 1.35)

    return max(1.02, round(final_odd, 2))


def get_weighted_stats(data, target_col, condition_lambda, prior_prob=0.5, alpha=2.0):
    if isinstance(data, pd.DataFrame):
        if data.empty: return 0.0, 0, 0, False
        valid_values = data.to_dict('records') if target_col is None else [v for v in data[target_col].tolist() if pd.notna(v)]
    else:
        if not data: return 0.0, 0, 0, False
        valid_values = data if target_col is None else [d.get(target_col) for d in data if pd.notna(d.get(target_col))]

    total_weight, weighted_hits, total_hits = 0.0, 0.0, 0
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
    if 0 < total_len < 12 and alpha > 0:
        return (weighted_hits + (alpha * prior_prob)) / (total_weight + alpha), total_hits, total_len, True
    return raw_prob, total_hits, total_len, False


def evaluate_bet(bet_type, r):
    bet = str(bet_type).upper().strip()
    if "+" in bet and not any(k in bet for k in ["_AH+", "H_AH", "A_AH"]):
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

    if bet.startswith("H_AH+"):
        try:
            line = float(bet.replace("H_AH+", ""))
            return "WYGRANA" if (hg + line) > ag else "PRZEGRANA"
        except Exception: pass
    if bet.startswith("A_AH+"):
        try:
            line = float(bet.replace("A_AH+", ""))
            return "WYGRANA" if (ag + line) > hg else "PRZEGRANA"
        except Exception: pass

    ht_h, ht_a = get_num('HTHG'), get_num('HTAG')
    tg = get_num('Total_Goals')
    if tg is None and hg is not None and ag is not None:
        tg = hg + ag

    if bet.startswith("HT_H_AH+") and ht_h is not None and ht_a is not None:
        try:
            line = float(bet.replace("HT_H_AH+", ""))
            return "WYGRANA" if (ht_h + line) > ht_a else "PRZEGRANA"
        except Exception: pass
    if bet.startswith("HT_A_AH+") and ht_h is not None and ht_a is not None:
        try:
            line = float(bet.replace("HT_A_AH+", ""))
            return "WYGRANA" if (ht_a + line) > ht_h else "PRZEGRANA"
        except Exception: pass

    if ht_h is not None and ht_a is not None and tg is not None:
        h2_h = hg - ht_h
        h2_a = ag - ht_a
        if bet.startswith("2H_H_AH+"):
            try:
                line = float(bet.replace("2H_H_AH+", ""))
                return "WYGRANA" if (h2_h + line) > h2_a else "PRZEGRANA"
            except Exception: pass
        if bet.startswith("2H_A_AH+"):
            try:
                line = float(bet.replace("2H_A_AH+", ""))
                return "WYGRANA" if (h2_a + line) > h2_h else "PRZEGRANA"
            except Exception: pass

    if bet.startswith("O") and tg is not None and "_" not in bet and not bet.startswith(("HC_O", "AC_O", "S_O", "ST_O")):
        return "WYGRANA" if tg > float(bet[1:]) else "PRZEGRANA"
    if bet.startswith("U") and tg is not None and "_" not in bet and not bet.startswith(("HT_U", "2H_U", "HU", "AU", "C_U", "HC_U", "AC_U", "S_U", "ST_U")):
        return "WYGRANA" if tg < float(bet[1:]) else "PRZEGRANA"

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
        ts = sh + sa
        if bet == "S_1": return "WYGRANA" if sh > sa else "PRZEGRANA"
        if bet == "S_2": return "WYGRANA" if sh < sa else "PRZEGRANA"
        if bet.startswith("S_U"): return "WYGRANA" if ts < float(bet[3:]) else "PRZEGRANA"
        if bet.startswith("S_O"): return "WYGRANA" if ts > float(bet[3:]) else "PRZEGRANA"
        if bet.startswith("H_S_U"): return "WYGRANA" if sh < float(bet[5:]) else "PRZEGRANA"
        if bet.startswith("H_S_O"): return "WYGRANA" if sh > float(bet[5:]) else "PRZEGRANA"
        if bet.startswith("A_S_U"): return "WYGRANA" if sa < float(bet[5:]) else "PRZEGRANA"
        if bet.startswith("A_S_O"): return "WYGRANA" if sa > float(bet[5:]) else "PRZEGRANA"

    sth, sta = get_num('ShotsTarget_H'), get_num('ShotsTarget_A')
    if sth is not None and sta is not None:
        tst = sth + sta
        if bet == "ST_1": return "WYGRANA" if sth > sta else "PRZEGRANA"
        if bet == "ST_2": return "WYGRANA" if sth < sta else "PRZEGRANA"
        if bet.startswith("ST_U"): return "WYGRANA" if tst < float(bet[4:]) else "PRZEGRANA"
        if bet.startswith("ST_O"): return "WYGRANA" if tst > float(bet[4:]) else "PRZEGRANA"
        if bet.startswith("H_ST_U"): return "WYGRANA" if sth < float(bet[6:]) else "PRZEGRANA"
        if bet.startswith("H_ST_O"): return "WYGRANA" if sth > float(bet[6:]) else "PRZEGRANA"
        if bet.startswith("A_ST_U"): return "WYGRANA" if sta < float(bet[6:]) else "PRZEGRANA"
        if bet.startswith("A_ST_O"): return "WYGRANA" if sta > float(bet[6:]) else "PRZEGRANA"

    return "DO RĘCZNEJ KONTROLI"

# ==================================================================================================
# 5. KOTWICE KURSOWE I DYNAMICZNA KALIBRACJA
# ==================================================================================================

KOTWICE_KURSOWE = {
    'O0.5': 1.03, 'U3.5': 1.31, 'U4.5': 1.10, 'U5.5': 1.015, 'U6.5': 1.01,
    'HT_U1.5': 1.42, 'HT_U2.5': 1.09, 'HT_U3.5': 1.01, 'HT_U4.5': 1.01,
    '2H_U3.5': 1.02, '2H_U4.5': 1.01, 'O0.5+U5.5': 1.09, 'O0.5+U6.5': 1.05,
    'C_U8.5': 2.78, 'C_U9.5': 2.02, 'C_U10.5': 1.59, 'C_U11.5': 1.33,
    'C_U12.5': 1.17, 'C_U13.5': 1.09, 'C_U14.5': 1.04, 'C_U15.5': 1.02,
    'HC_U5.5': 1.75, 'HC_U6.5': 1.35, 'HC_U7.5': 1.14, 'HC_U8.5': 1.05, 'HC_U9.5': 1.02,
    'AC_U4.5': 1.74, 'AC_U5.5': 1.32, 'AC_U6.5': 1.11, 'AC_U7.5': 1.04, 'AC_U8.5': 1.02,
    'HC_O4.5': 1.44, 'AC_O4.5': 1.98,
    'HU2.5': 1.12, 'HU3.5': 1.01, 'HU4.5': 1.01,
    'AU2.5': 1.12, 'AU3.5': 1.01, 'AU4.5': 1.01,
    'S_1': 1.34, 'ST_1': 1.64,
    'S_U24.5': 1.85, 'S_U25.5': 1.65, 'S_U26.5': 1.48, 'S_U27.5': 1.35,
    'S_O21.5': 1.30, 'S_O22.5': 1.45, 'S_O23.5': 1.65,
    'H_S_O10.5': 1.35, 'H_S_O11.5': 1.55, 'H_S_U14.5': 1.45, 'H_S_U15.5': 1.30,
    'A_S_O8.5': 1.38, 'A_S_O9.5': 1.60, 'A_S_U13.5': 1.45, 'A_S_U14.5': 1.30,
    'ST_U7.5': 1.75, 'ST_U8.5': 1.50, 'ST_U9.5': 1.32,
    'ST_O6.5': 1.45, 'ST_O7.5': 1.70,
    'H_ST_O3.5': 1.35, 'H_ST_O4.5': 1.65, 'H_ST_U5.5': 1.40, 'H_ST_U6.5': 1.22,
    'A_ST_O2.5': 1.40, 'A_ST_O3.5': 1.80, 'A_ST_U4.5': 1.35, 'A_ST_U5.5': 1.20,
    'H_AH+1.5': 1.28, 'H_AH+2.5': 1.12, 'A_AH+1.5': 1.32, 'A_AH+2.5': 1.14,
    'HT_H_AH+0.5': 1.24, 'HT_H_AH+1.5': 1.06, 'HT_A_AH+0.5': 1.30, 'HT_A_AH+1.5': 1.08,
    '2H_H_AH+0.5': 1.25, '2H_H_AH+1.5': 1.06, '2H_A_AH+0.5': 1.30, '2H_A_AH+1.5': 1.08
}

SZABLONY_PREMIUM = [
    "O0.5+U5.5+HT_U3.5+2H_U3.5+HU3.5+AU3.5",
    "O0.5+U4.5+HT_U3.5+2H_U3.5+HU3.5+AU3.5",
    "U6.5+HT_U3.5+2H_U4.5+HU4.5+AU3.5",
    "C_U11.5+HC_U8.5",
    "C_U12.5+HC_U9.5+AC_U8.5",
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
        'C_U12.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 12, "under")),
        'C_U13.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 13, "under")),
        'C_U14.5': apply_margin_mult(get_poisson_prob(lam_corners_tot, 14, "under")),
        'S_1': apply_margin_add(prob_s1),
        'ST_1': apply_margin_add(prob_st1)
    })
    return anchors

# ==================================================================================================
# 6. POBIERANIE / WCZYTYWANIE DANYCH MECZOWYCH
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
            if attempt == 2: return pd.DataFrame(), ["Football-Data", u, f"BŁĄD: {str(e_fd)}"]
    return pd.DataFrame(), ["Football-Data", u, "BŁĄD: Niedostępny (503/Timeout)"]

def fetch_football_data(raport):
    path_fd = os.path.join(DATA_DIR, "ligi_footballdata.xlsx") if os.path.exists(os.path.join(DATA_DIR, "ligi_footballdata.xlsx")) else "ligi_footballdata.xlsx"
    if not os.path.exists(path_fd): return pd.DataFrame()
    try: urls_fd = pd.read_excel(path_fd)["URL"].dropna().tolist()
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

path_ligi = os.path.join(DATA_DIR, "ligi.xlsx") if os.path.exists(os.path.join(DATA_DIR, "ligi.xlsx")) else "ligi.xlsx"
try: urls = pd.read_excel(path_ligi)["URL"].dropna().tolist() if os.path.exists(path_ligi) else []
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
        except Exception: pass

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
results_csv_local = os.path.join(DATA_DIR, "Results.csv") if os.path.exists(os.path.join(DATA_DIR, "Results.csv")) else "Results.csv"

if OFFLINE_LOCAL and os.path.exists(results_csv_local):
    print(f"📁 [OFFLINE] Wczytano wyniki meczów z pliku lokalnego: {results_csv_local}")
    results_df = pd.read_csv(results_csv_local)
    fixtures_df = pd.DataFrame()
else:
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

# SoccerStats scraping
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
path_ss = os.path.join(DATA_DIR, "ligi_soccerstats.xlsx") if os.path.exists(os.path.join(DATA_DIR, "ligi_soccerstats.xlsx")) else "ligi_soccerstats.xlsx"
if os.path.exists(path_ss) and not OFFLINE_LOCAL:
    try:
        urls_ss = pd.read_excel(path_ss)["URL"].dropna().tolist()
        if TEST_MODE: urls_ss = urls_ss[:3]
        ss_args = [(str(u).strip(), {}) for u in urls_ss]
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS_SOCCERSTATS) as executor:
            for d_chunk, r_chunk in executor.map(scrape_ss_worker, ss_args):
                dane_ss.extend(d_chunk)
                scrape_report.extend(r_chunk)
        if dane_ss:
            ss_df = pd.DataFrame(dane_ss, columns=["Home", "Away", "Score", "Gole_Gosp_1H", "Gole_Gosc_1H"]).drop_duplicates(subset=["Home", "Away", "Score"])
    except Exception: pass

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

results_clean = results_df[[c for c in list(golden_cols.keys()) if c in results_df.columns] + ['HT_Total', 'Total_Corners', 'Marża']].rename(columns=golden_cols) if not results_df.empty else pd.DataFrame()

# ==================================================================================================
# 7. TABELE LIGOWE I WYBÓR ZESTAWU SYMULACYJNEGO
# ==================================================================================================

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
        total, pos = row['Total_Teams'], row['Pozycja']
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


if OFFLINE_LOCAL and not valid_matches.empty:
    print("🎯 [OFFLINE] Generuję zbiór testowy z zakończonych meczów weekendowych (18-20.09.2026)...")
    valid_matches['Date_Clean'] = pd.to_datetime(valid_matches['Date'], errors='coerce')
    weekend_matches = valid_matches[
        (valid_matches['Date_Clean'] >= '2026-09-18') & (valid_matches['Date_Clean'] <= '2026-09-20')
    ].copy()

    if weekend_matches.empty:
        weekend_matches = valid_matches.head(40).copy()

    fixtures_clean = weekend_matches[['Match_ID', 'League', 'Date', 'Home', 'Away', 'Odd_1', 'Odd_X', 'Odd_2']].copy()
    fixtures_clean['Termin'] = "Weekend Test"
    fixtures_clean['Time'] = "18:00"
    fixtures_clean['Status_Kursów'] = "Są Kursy"
    fixtures_clean['Marża'] = "5.0"
else:
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

    fixtures_clean = fixtures_df[['Match_ID', 'Termin', 'Status_Kursów', 'League', 'Date', 'Time', 'Home', 'Away', 'Odd1', 'OddX', 'Odd2', 'Marża']].rename(columns={'Odd1': 'Odd_1', 'OddX': 'Odd_X', 'Odd2': 'Odd_2'}) if not fixtures_df.empty else pd.DataFrame()

# ==================================================================================================
# 8. ZUNIFIKOWANY WIELOPOZIOMOWY PARSER KURSÓW 1:1 (SUPERBET & FORTUNA)
# ==================================================================================================

def clean_team_str(s):
    t = str(s).lower().strip()
    for pl, en in [('ą','a'),('ć','c'),('ę','e'),('ł','l'),('ń','n'),('ó','o'),('ś','s'),('ź','z'),('ż','z')]:
        t = t.replace(pl, en)
    t = re.sub(r'\b(fc|ks|gks|mks|ac|as|cf|ss|sc|sa|sp|vfb|tsv|sv|fk|sk|stade|de|hsc)\b', '', t)
    return re.sub(r'[^a-z0-9]', '', t)

def get_team_tokens(team_name):
    tokens = set()
    cleaned = clean_team_str(team_name)
    if len(cleaned) >= 3:
        tokens.add(cleaned)
        tokens.add(cleaned[:4])
    raw_words = re.findall(r'[a-zA-Z0-9]+', str(team_name).lower())
    for w in raw_words:
        cw = clean_team_str(w)
        if len(cw) >= 3 and cw not in ["team", "club", "city", "town", "utd", "stade"]:
            tokens.add(cw)
            tokens.add(cw[:4])
    return list(tokens)

superbet_baza = {}
superbet_fast_lookup = {}
fortuna_baza = {}
fortuna_fast_lookup = {}

if not SKIP_BOOKMAKERS:
    # 1. Superbet
    detected_sb = sorted(glob.glob(os.path.join(DATA_DIR, "superbet_baza*.json")))
    for j_file in detected_sb:
        try:
            with open(j_file, "r", encoding="utf-8") as f:
                temp_baza = json.load(f)
                superbet_baza.update(temp_baza)
            print(f"✅ Wczytano Superbet ({j_file}): {len(temp_baza)} spotkań.")
        except Exception as e_sb:
            print(f"⚠️ Błąd wczytywania {j_file}: {e_sb}")

    for k, v in superbet_baza.items():
        k_clean = k.lower().replace(" ii", "").strip()
        superbet_fast_lookup[k.lower()] = v
        superbet_fast_lookup[k_clean] = v
        superbet_fast_lookup[clean_team_str(k)] = v
        info = v.get('info', {})
        g_be = clean_team_str(info.get('gospodarz_be', ''))
        a_be = clean_team_str(info.get('gosc_be', ''))
        if g_be and a_be:
            superbet_fast_lookup[f"{g_be}___{a_be}"] = v

    # 2. Fortuna
    detected_ft = sorted(glob.glob(os.path.join(DATA_DIR, "fortuna_baza*.json")))
    for f_file in detected_ft:
        try:
            with open(f_file, "r", encoding="utf-8") as f:
                temp_f = json.load(f)
                fortuna_baza.update(temp_f)
            print(f"✅ Wczytano Fortunę ({f_file}): {len(temp_f)} spotkań.")
        except Exception as e_f:
            print(f"⚠️ Błąd wczytywania {f_file}: {e_f}")

    for k, v in fortuna_baza.items():
        k_clean = k.lower().replace(" ii", "").strip()
        fortuna_fast_lookup[k.lower()] = v
        fortuna_fast_lookup[k_clean] = v
        fortuna_fast_lookup[clean_team_str(k)] = v
        info = v.get('info', {})
        g_be = clean_team_str(info.get('gospodarz_be', ''))
        a_be = clean_team_str(info.get('gosc_be', ''))
        if g_be and a_be:
            fortuna_fast_lookup[f"{g_be}___{a_be}"] = v


def is_odd_sane(typ_kod, odd_val):
    if odd_val is None or odd_val <= 1.005: return False
    k = str(typ_kod).strip().upper()
    if k == "U6.5" and odd_val > 1.06: return False
    if k == "U5.5" and odd_val > 1.18: return False
    if k == "U4.5" and odd_val > 1.48: return False
    if k == "U3.5" and odd_val > 2.30: return False
    if k == "O0.5" and odd_val > 1.15: return False
    if k.startswith("MG_") and odd_val > 1.60: return False
    if (k.startswith("HC_") or k.startswith("AC_")) and odd_val > 8.0: return False
    return True


def parsuj_kurs_ze_slownika(match_data, typ_kod, home, away):
    if not match_data or not isinstance(match_data, dict): return None
    typ_k = str(typ_kod).strip()

    rynki = match_data.get("rynki", {})
    kursy = match_data.get("kursy", {})
    info = match_data.get("info", {})

    h_buk = info.get("gospodarz_fortuna", info.get("gospodarz_sb", home))
    a_buk = info.get("gosc_fortuna", info.get("gosc_sb", away))

    home_tokens = list(set(get_team_tokens(home) + get_team_tokens(h_buk)))
    away_tokens = list(set(get_team_tokens(away) + get_team_tokens(a_buk)))

    is_team_goal = typ_k.startswith(("HU", "AU", "HO", "AO"))
    is_half_goal = typ_k.startswith(("HT_U", "HT_O", "2H_U", "2H_O"))
    is_shots = typ_k.startswith(("S_", "ST_", "H_S_", "A_S_", "H_ST_", "A_ST_"))
    is_corners = typ_k.startswith(("C_", "HC_", "AC_"))
    is_handicap = "_AH+" in typ_k or typ_k.startswith(("H_AH", "A_AH"))
    is_multigol = typ_k.startswith("MG_") or typ_k in ["1-5", "1-6", "1-4", "2-4", "2-5"]
    is_under_over = (typ_k.startswith("U") or typ_k.startswith("O")) and not any([is_shots, is_corners, is_handicap, is_multigol, is_team_goal, is_half_goal])

    # 1. RZUTY ROŻNE (C_U/O, HC_U/O, AC_U/O)
    if is_corners:
        is_home_c = typ_k.startswith("HC_")
        is_away_c = typ_k.startswith("AC_")
        is_match_c = typ_k.startswith("C_")

        parts = typ_k.split("_")
        line_part = parts[1]
        is_u = line_part.startswith("U")
        line = line_part[1:]
        
        kw_target = ["mniej", "ponizej", "-"] if is_u else ["wiecej", "powyzej", "+"]
        forbidden_corners = ["wygra", "obie", "btts", "gol", "bramk", "kartk", "spalon", "faul", "kombin", "combo", "podwojna", "dwojtyp"]

        for r_name, r_opts in rynki.items():
            r_clean = r_name.lower().replace("\n", " ")
            r_norm = clean_team_str(r_clean)

            if "rozn" not in r_norm: continue
            if any(fb in r_clean or fb in r_norm for fb in forbidden_corners): continue

            has_h = any(tok in r_norm for tok in home_tokens)
            has_a = any(tok in r_norm for tok in away_tokens)

            if is_match_c and (has_h or has_a): continue
            if is_home_c and not has_h: continue
            if is_away_c and not has_a: continue

            for opt_k, opt_v in r_opts.items():
                opt_norm = clean_team_str(opt_k)
                tokens = re.findall(r"\d+(?:\.\d+)?", str(r_name) + " " + str(opt_k))
                if line in tokens:
                    has_dir = any(kw in opt_norm for kw in kw_target) or (opt_k.strip().startswith("-") if is_u else opt_k.strip().startswith("+"))
                    if has_dir:
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val
                        except Exception: pass

        if is_match_c and typ_k in kursy:
            try:
                val = float(str(kursy[typ_k]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val
            except Exception: pass

    # 2. GOLE DRUŻYNOWE (HU, AU, HO, AO)
    if is_team_goal:
        is_home = typ_k.startswith("H")
        is_under = "U" in typ_k[:2]
        tokens_num = re.findall(r"\d*\.?\d+", typ_k)
        line = tokens_num[0] if tokens_num else ""
        if line.startswith("."): line = "0" + line

        target_tokens = home_tokens if is_home else away_tokens
        opp_tokens = away_tokens if is_home else home_tokens
        kw_target = ["mniej", "ponizej", "-"] if is_under else ["wiecej", "powyzej", "+"]
        forbidden = ["polow", "kartk", "rozn", "strzal", "faul", "spalon", "&", "lub", "wygra", "dokladny", "/", "remis"]

        for r_name, r_opts in rynki.items():
            r_clean = r_name.lower().replace("\n", " ")
            r_norm = clean_team_str(r_clean)

            if any(fb in r_clean for fb in forbidden): continue
            if "liczbagoli" not in r_norm and "liczbabramek" not in r_norm and "gole" not in r_norm: continue

            has_target = any(tok in r_norm for tok in target_tokens) or ("gospodarz" in r_norm if is_home else ("gosc" in r_norm or "gość" in r_clean))
            has_opp = any(tok in r_norm for tok in opp_tokens if tok not in target_tokens)
            if not has_target or has_opp: continue

            for opt_k, opt_v in r_opts.items():
                opt_clean = opt_k.lower().replace("\n", " ")
                opt_norm = clean_team_str(opt_clean)
                tokens = re.findall(r"\d+(?:\.\d+)?", r_clean + " " + opt_clean)
                if line in tokens:
                    has_dir = any(kw in opt_norm for kw in kw_target) or (opt_k.strip().startswith("-") if is_under else opt_k.strip().startswith("+"))
                    if has_dir:
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val
                        except Exception: pass

        if typ_k in kursy:
            try:
                val = float(str(kursy[typ_k]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val
            except Exception: pass

    # 3. GOLE POŁÓWKOWE (HT_U, 2H_U)
    if is_half_goal:
        is_ht = "HT_" in typ_k
        is_u = "_U" in typ_k
        line = typ_k[4:].strip()
        kw_list = ["ponizej", "mniej", "-"] if is_u else ["powyzej", "wiecej", "+"]
        time_kw = ["1. połowa", "1.połowa", "1 połowa"] if is_ht else ["2. połowa", "2.połowa", "2 połowa"]
        forbidden_half = ["&", "lub", "oraz", "/", "wygra", "btts", "obie", "kartk", "rozn", "dokladny", "handicap", "mecz"]

        for r_name, r_opts in rynki.items():
            r_clean = r_name.lower().replace("\n", " ")
            r_norm = clean_team_str(r_clean)

            if any(fb in r_clean for fb in forbidden_half): continue
            if not any(p in r_clean for p in time_kw): continue
            if is_ht and any(p in r_clean for p in ["2. połowa", "2.połowa", "2 połowa"]): continue
            if not is_ht and any(p in r_clean for p in ["1. połowa", "1.połowa", "1 połowa"]): continue

            if "liczbagoli" in r_norm or "liczbabramek" in r_norm or "sumagoli" in r_norm:
                for opt_k, opt_v in r_opts.items():
                    opt_norm = clean_team_str(opt_k)
                    tokens = re.findall(r"\d+(?:\.\d+)?", str(r_name) + " " + str(opt_k))
                    if line in tokens and any(kw in opt_norm for kw in kw_list):
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val
                        except Exception: pass

        if typ_k in kursy:
            try:
                val = float(str(kursy[typ_k]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val
            except Exception: pass

    # 4. GOLE MECZOWE UNDER / OVER
    if is_under_over:
        lv = typ_k[1:].strip()
        is_u = typ_k.startswith("U")
        kw_list = ["ponizej", "mniej", "-"] if is_u else ["powyzej", "wiecej", "+"]
        forbidden = ["rozn", "polow", "kartk", "faul", "strzal", "spalon", "dwojtyp", "zolt", "&", "wygra"]

        for r_name, r_opts in rynki.items():
            r_norm = clean_team_str(r_name)
            if any(fb in r_norm for fb in forbidden): continue
            if any(tok in r_norm for tok in (home_tokens + away_tokens)) or "gospodarz" in r_norm or "gość" in r_norm or "gosc" in r_norm:
                continue

            if "liczbagoli" in r_norm or "sumagoli" in r_norm or "liczbabramek" in r_norm or "meczliczbagoli" in r_norm:
                for opt_k, opt_v in r_opts.items():
                    opt_norm = clean_team_str(opt_k)
                    tokens = re.findall(r"\d+(?:\.\d+)?", str(r_name) + " " + str(opt_k))
                    if lv in tokens and any(kw in opt_norm for kw in kw_list):
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val
                        except Exception: pass

        if typ_k in kursy:
            try:
                val = float(str(kursy[typ_k]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val
            except Exception: pass

    # 5. STRZAŁY
    if is_shots:
        is_sot = ("ST" in typ_k)
        is_home_t = typ_k.startswith("H_")
        is_away_t = typ_k.startswith("A_")
        is_match_t = not is_home_t and not is_away_t

        if typ_k in ["S_1", "S_2", "ST_1", "ST_2"]:
            is_target_home = typ_k.endswith("_1")
            target_tokens = home_tokens if is_target_home else away_tokens
            opp_tokens = away_tokens if is_target_home else home_tokens

            for r_name, r_opts in rynki.items():
                r_norm = clean_team_str(r_name)
                if "strzal" not in r_norm: continue
                has_sot_kw = any(w in r_norm for w in ["celn", "swiatlo"])
                if is_sot and not has_sot_kw: continue
                if not is_sot and has_sot_kw: continue
                
                if any(w in r_norm for w in ["wiecej", "najwiecej", "h2h"]):
                    for opt_k, opt_v in r_opts.items():
                        opt_norm = clean_team_str(opt_k)
                        if "remis" in opt_norm or "rowno" in opt_norm: continue
                        matched = False
                        if is_target_home and (opt_k.strip().startswith("1") or any(tok in opt_norm for tok in target_tokens)):
                            matched = True
                        elif not is_target_home and (opt_k.strip().startswith("2") or any(tok in opt_norm for tok in target_tokens)):
                            matched = True

                        if matched and not any(op in opt_norm for op in opp_tokens if op not in target_tokens):
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(typ_k, val): return val
                            except Exception: pass

        tokens_line = re.findall(r"\d*\.?\d+", typ_k)
        tokens_line = [t for t in tokens_line if t and t != '.']
        if tokens_line:
            line = tokens_line[0]
            if line.startswith("."): line = "0" + line
            is_u = "_U" in typ_k
            kw_target = ["mniej", "ponizej", "-"] if is_u else ["wiecej", "powyzej", "+"]

            for r_name, r_opts in rynki.items():
                r_norm = clean_team_str(r_name)
                if "strzal" not in r_norm: continue
                has_sot_kw = any(w in r_norm for w in ["celn", "swiatlo"])
                if is_sot and not has_sot_kw: continue
                if not is_sot and has_sot_kw: continue

                has_h = any(tok in r_norm for tok in home_tokens)
                has_a = any(tok in r_norm for tok in away_tokens)

                if is_match_t and (has_h or has_a): continue
                if is_home_t and not has_h: continue
                if is_away_t and not has_a: continue

                for opt_k, opt_v in r_opts.items():
                    opt_norm = clean_team_str(opt_k)
                    tokens = re.findall(r"\d+(?:\.\d+)?", str(r_name) + " " + str(opt_k))
                    if line in tokens:
                        has_dir = any(kw in opt_norm for kw in kw_target) or (opt_k.strip().startswith("-") if is_u else opt_k.strip().startswith("+"))
                        if has_dir:
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(typ_k, val): return val
                            except Exception: pass

    # 6. MULTIGOLE
    if is_multigol:
        range_target = typ_k.replace("MG_", "").strip()
        if typ_k in kursy:
            try:
                val = float(str(kursy[typ_k]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val
            except Exception: pass
        if range_target in kursy:
            try:
                val = float(str(kursy[range_target]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val
            except Exception: pass

        for r_name, r_opts in rynki.items():
            r_low = r_name.lower().replace("\n", " ")
            if any(kw in r_low for kw in ["multigol", "przedział", "przedzial", "zakres", "liczba goli"]):
                for opt_k, opt_v in r_opts.items():
                    opt_str = str(opt_k).strip().lower()
                    if opt_str.startswith(range_target) or f"{range_target} |" in opt_str or f"{range_target} goli" in opt_str or opt_str == range_target:
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val
                        except Exception: pass

    # 7. HANDICAPY AZJATYCKIE I EUROPEJSKIE
    if is_handicap:
        is_ht = "HT_" in typ_k
        is_2h = "2H_" in typ_k
        is_ft = not is_ht and not is_2h
        is_home_h = "_H_AH+" in typ_k or typ_k.startswith("H_AH+")
        line = typ_k.split("+")[1].strip()
        target_tokens = home_tokens if is_home_h else away_tokens

        for r_name, r_opts in rynki.items():
            r_norm = clean_team_str(r_name)
            if "handicap" not in r_norm or any(fb in r_norm for fb in ["rozn", "kartk"]):
                continue

            has_1h = any(p in r_norm for p in ["1polowa", "1pol"])
            has_2h = any(p in r_norm for p in ["2polowa", "2pol"])
            if is_ht and not has_1h: continue
            if is_2h and not has_2h: continue
            if is_ft and (has_1h or has_2h): continue

            for opt_k, opt_v in r_opts.items():
                opt_norm = clean_team_str(opt_k)
                combo_norm = f"{r_norm} {opt_norm}"
                if f"-{line}" in combo_norm and f"+{line}" not in combo_norm:
                    continue

                has_team = any(tok in opt_norm for tok in target_tokens) or (opt_k.strip().startswith("1") if is_home_h else opt_k.strip().startswith("2"))
                if has_team:
                    if (f"+{line}" in combo_norm) or (f"({line})" in combo_norm) or (f"{line}" in opt_k and "-" not in opt_k):
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val
                        except Exception: pass
                    if not is_home_h:
                        target_lead = int(float(line) + 0.5)
                        if f"0{target_lead}" in r_norm and opt_k.strip().startswith("2"):
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(typ_k, val): return val
                            except Exception: pass
                    if is_home_h:
                        target_lead = int(float(line) + 0.5)
                        if f"{target_lead}0" in r_norm and opt_k.strip().startswith("1"):
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(typ_k, val): return val
                            except Exception: pass

    # 8. 1X2 i Podwójna Szansa
    if typ_k in ["1", "X", "2", "1X", "X2"]:
        for r_name, r_opts in rynki.items():
            r_norm = clean_team_str(r_name)
            if any(kw in r_norm for kw in ["mecz", "1x2", "wynik", "podwojnaszansa", "szansa"]):
                for opt_k, opt_v in r_opts.items():
                    opt_low = str(opt_k).lower().strip()
                    matched = False
                    if typ_k == "1X" and opt_low in ["1x", "1-x", "10", "1/x"]: matched = True
                    elif typ_k == "X2" and opt_low in ["x2", "x-2", "02", "x/2"]: matched = True
                    elif typ_k == "1" and opt_low in ["1", "gospodarz", "home"]: matched = True
                    elif typ_k == "2" and opt_low in ["2", "gość", "gosc", "away"]: matched = True
                    elif typ_k == "X" and opt_low in ["x", "0", "remis", "draw"]: matched = True
                    if matched:
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val
                        except Exception: pass

    # 9. Zapasowo ze słownika 'kursy'
    if typ_k in kursy and not any([is_under_over, is_handicap, is_multigol, is_corners, is_shots, is_team_goal, is_half_goal]):
        try:
            val = float(str(kursy[typ_k]).replace(',', '.'))
            if is_odd_sane(typ_k, val): return val
        except Exception: pass

    return None


def get_real_odds_with_status(home, away, typ_kod, engine="Goal Line Pro"):
    typ_k = str(typ_kod).strip()
    h_c = clean_team_str(home).replace("ii", "").strip()
    a_c = clean_team_str(away).replace("ii", "").strip()
    key_exact = f"{h_c}___{a_c}"

    def find_match(baza, fast_lookup):
        if key_exact in fast_lookup: return fast_lookup[key_exact]
        if f"{home.lower().strip()}_{away.lower().strip()}" in fast_lookup:
            return fast_lookup[f"{home.lower().strip()}_{away.lower().strip()}"]
        h_tokens = get_team_tokens(home)
        a_tokens = get_team_tokens(away)
        for k, v in baza.items():
            if "___" in k:
                b_h, b_a = k.split("___")
                cb_h, cb_a = clean_team_str(b_h), clean_team_str(b_a)
                if any(tok in cb_h for tok in h_tokens) and any(tok in cb_a for tok in a_tokens):
                    return v
        return None

    match_sb = find_match(superbet_baza, superbet_fast_lookup)
    match_ft = find_match(fortuna_baza, fortuna_fast_lookup)

    if not match_sb and not match_ft:
        return None, None, "❌ Brak w ofercie"

    # BetBuilder Pro
    is_handicap = any(tag in typ_k for tag in ["_AH+", "H_AH", "A_AH"])
    if (engine == "BetBuilder Pro" or ("+" in typ_k and "1X" not in typ_k and "X2" not in typ_k)) and not is_handicap:
        skladniki = [s.strip() for s in typ_k.split("+")]
        
        odd_bb_sb = None
        if match_sb:
            k_skl = {}
            for sk in skladniki:
                v_sk = parsuj_kurs_ze_slownika(match_sb, sk, home, away)
                k_skl[sk] = float(v_sk) if v_sk is not None else 1.00
            c_sb = calc_nested_betbuilder(k_skl, typ_k)
            if c_sb >= 1.05: odd_bb_sb = c_sb

        odd_bb_ft = None
        if match_ft:
            k_skl_ft = {}
            for sk in skladniki:
                v_sk = parsuj_kurs_ze_slownika(match_ft, sk, home, away)
                k_skl_ft[sk] = float(v_sk) if v_sk is not None else 1.00
            c_ft = calc_nested_betbuilder(k_skl_ft, typ_k)
            if c_ft >= 1.05: odd_bb_ft = c_ft

        if odd_bb_sb or odd_bb_ft: return odd_bb_sb, odd_bb_ft, "⚡ BetBuilder Pro"
        return None, None, "❌ Brak w ofercie"

    odd_sb = parsuj_kurs_ze_slownika(match_sb, typ_k, home, away) if match_sb else None
    odd_ft = parsuj_kurs_ze_slownika(match_ft, typ_k, home, away) if match_ft else None

    if odd_sb and odd_ft: status = "✅ SB + Fortuna 1:1"
    elif odd_sb: status = "✅ Superbet 1:1"
    elif odd_ft: status = "✅ Fortuna 1:1"
    else: status = "❌ Brak w ofercie"

    return odd_sb, odd_ft, status

# ==================================================================================================
# 9. CENTRALNY GENERATOR PREDYKCJI
# ==================================================================================================

all_generated_predictions = []

def add_pred(match_id, termin, date, time, league, home, away, engine, typ, szansa, kurs_szac, arg, dyn_anchors=None, match_odds=None):
    if dyn_anchors is None: dyn_anchors = KOTWICE_KURSOWE
    typ_k = str(typ).strip()

    try: kurs_matematyczny = float(str(kurs_szac).replace(',', '.'))
    except Exception: kurs_matematyczny = 1.05

    if engine != "BetBuilder Pro" and typ_k in dyn_anchors:
        kurs_matematyczny = dyn_anchors[typ_k]

    odd_sb_val, odd_ft_val, status_globalny = get_real_odds_with_status(home, away, typ_k, engine=engine)
    kurs_sb_str = f"{odd_sb_val:.2f}" if odd_sb_val is not None else "Brak"
    kurs_ft_str = f"{odd_ft_val:.2f}" if odd_ft_val is not None else "Brak"

    realne_kursy = [k for k in [odd_sb_val, odd_ft_val] if k is not None]
    max_realny = max(realne_kursy) if realne_kursy else None
    kurs_do_oceny = max_realny if max_realny is not None else kurs_matematyczny

    # 1. Bezwzględny filtr progu kursu realnego 1.05
    if kurs_do_oceny < 1.05:
        return

    # 2. Reguła dnia meczu (odrzucamy typy bez kursu bukmachera w dniu spotkania)
    if termin == "Dziś" and (max_realny is None or status_globalny == "❌ Brak w ofercie"):
        return

    prob_decimal = float(szansa) / 100.0
    ev = prob_decimal * max_realny if max_realny is not None else 0.0

    if max_realny is not None and ev >= 1.05: risk_tag = "💰 REAL VALUE"
    elif prob_decimal >= 0.95 and kurs_do_oceny >= 1.20: risk_tag = "🥇 1. ZŁOTY TYP"
    elif prob_decimal >= 0.95 and kurs_do_oceny >= 1.15: risk_tag = "🥈 2. SREBRNY TYP"
    elif prob_decimal >= 0.95 and kurs_do_oceny >= 1.10: risk_tag = "🥉 3. BRĄZOWY TYP"
    elif prob_decimal >= 0.95: risk_tag = "SAFE (95%+)"
    elif prob_decimal >= 0.85: risk_tag = "STANDARD (85-94%)"
    elif prob_decimal >= 0.75: risk_tag = "VALUE (75-84%)"
    else: risk_tag = "RISK (70-74%)"

    clean_arg = str(arg)
    arg_final = re.sub(r"^\[.*?\]\s*", f"[{risk_tag}] ", clean_arg) if clean_arg.startswith("[") else f"[{risk_tag}] {clean_arg}"

    all_generated_predictions.append({
        "Match_ID": match_id, "Termin": termin, "Data": date, "Godzina": time, "Liga": league,
        "Gospodarz": home, "Gość": away, "Engine": engine, "Typ": typ,
        "Szansa": szansa, "Kurs_Szac": kurs_matematyczny,
        "Kurs_Realny_Superbet": kurs_sb_str, "Kurs_Realny_Fortuna": kurs_ft_str,
        "Status_kursu_realnego": status_globalny, "Argumentacja": arg_final
    })

# ==================================================================================================
# 10. GŁÓWNA PĘTLA MODELI (KOMPLETNY ZESTAW SILNIKÓW Z DEFENSYWĄ RYWALÓW)
# ==================================================================================================

print("\nUruchamiam komplet Modeli Predykcyjnych...")

for idx, row in fixtures_clean.iterrows():
    league, home, away = row['League'], row['Home'], row['Away']
    fixture_base = get_base_league(league)
    match_id, d_termin, d_date, d_time = row['Match_ID'], row['Termin'], row['Date'], row['Time']
    o1_raw = row.get('Odd_1', '1.80')
    ox_raw = row.get('Odd_X', '3.40')
    o2_raw = row.get('Odd_2', '4.20')

    h_tier = team_tiers.get((league, home), 'Koszyk 3')
    a_tier = team_tiers.get((league, away), 'Koszyk 3')
    dyn_anchors = get_dynamic_anchors(h_tier, a_tier, o1_raw)

    m_hist = valid_matches[valid_matches['Match_ID'] != match_id] if OFFLINE_LOCAL else valid_matches

    h_tot_all = m_hist[(m_hist['Base_League'] == fixture_base) & ((m_hist['Home'] == home) | (m_hist['Away'] == home))].copy()
    a_tot_all = m_hist[(m_hist['Base_League'] == fixture_base) & ((m_hist['Home'] == away) | (m_hist['Away'] == away))].copy()
    h_dom = m_hist[(m_hist['Base_League'] == fixture_base) & (m_hist['Home'] == home)].copy()
    a_wyj = m_hist[(m_hist['Base_League'] == fixture_base) & (m_hist['Away'] == away)].copy()

    if len(h_dom) > 0:
        h_dom['HT_Total'] = pd.to_numeric(h_dom['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(h_dom['HTAG'], errors='coerce').fillna(0)
        h_dom['2H_Total'] = pd.to_numeric(h_dom['Total_Goals'], errors='coerce').fillna(0) - h_dom['HT_Total']
    if len(a_wyj) > 0:
        a_wyj['HT_Total'] = pd.to_numeric(a_wyj['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(a_wyj['HTAG'], errors='coerce').fillna(0)
        a_wyj['2H_Total'] = pd.to_numeric(a_wyj['Total_Goals'], errors='coerce').fillna(0) - a_wyj['HT_Total']

    if len(h_tot_all) > 0:
        h_tot_all['Team_GF'] = np.where(h_tot_all['Home'] == home, h_tot_all['FTHG'], h_tot_all['FTAG'])
        h_tot_all['Team_GA'] = np.where(h_tot_all['Home'] == home, h_tot_all['FTAG'], h_tot_all['FTHG'])
        h_tot_all['HT_Total'] = pd.to_numeric(h_tot_all['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(h_tot_all['HTAG'], errors='coerce').fillna(0)
        h_tot_all['2H_Total'] = pd.to_numeric(h_tot_all['Total_Goals'], errors='coerce').fillna(0) - h_tot_all['HT_Total']

    if len(a_tot_all) > 0:
        a_tot_all['Team_GF'] = np.where(a_tot_all['Home'] == away, a_tot_all['FTHG'], a_tot_all['FTAG'])
        a_tot_all['Team_GA'] = np.where(a_tot_all['Home'] == away, a_tot_all['FTAG'], a_tot_all['FTHG'])
        a_tot_all['HT_Total'] = pd.to_numeric(a_tot_all['HTHG'], errors='coerce').fillna(0) + pd.to_numeric(a_tot_all['HTAG'], errors='coerce').fillna(0)
        a_tot_all['2H_Total'] = pd.to_numeric(a_tot_all['Total_Goals'], errors='coerce').fillna(0) - a_tot_all['HT_Total']

    h_last3 = [f"{int(m['FTHG'])}:{int(m['FTAG'])}" for _, m in h_tot_all.head(3).iterrows()]
    a_last3 = [f"{int(m['FTHG'])}:{int(m['FTAG'])}" for _, m in a_tot_all.head(3).iterrows()]
    last3_str = f"Ost. 3 mecze: Gosp ({', '.join(h_last3) if h_last3 else 'brak'}), Gość ({', '.join(a_last3) if a_last3 else 'brak'})"

    # 1. 1X PRO
    lg_matches = m_hist[m_hist['Base_League'] == fixture_base]
    is_lower_div = any(tag in league.lower() for tag in ["2", "tier-2", "division-2", "championship", "segunda", "serie-b", "ligue-2", "2-liga"])
    h_promoted = (len(h_tot_all) <= 4) and (not is_lower_div)
    a_promoted = (len(a_tot_all) <= 4) and (not is_lower_div)
    h_tier_s = 'Koszyk 6' if h_promoted else h_tier
    a_tier_s = 'Koszyk 6' if a_promoted else a_tier
    t_h_val = int(str(h_tier_s).replace("Koszyk ", "")) if "Koszyk" in str(h_tier_s) else 3
    t_a_val = int(str(a_tier_s).replace("Koszyk ", "")) if "Koszyk" in str(a_tier_s) else 3

    if (len(h_tot_all) >= 8 and len(a_tot_all) >= 8 and len(h_dom) >= 4 and len(a_wyj) >= 4) or (t_h_val <= 2 and a_promoted and len(h_dom) >= 4):
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

        h_dom_1x_hits = sum(h_dom['FTHG'] >= h_dom['FTAG'])
        h_dom_total = len(h_dom)
        h_1x_pct = (h_dom_1x_hits / h_dom_total) if h_dom_total >= 4 else 0.5

        a_wyj_x2_hits = sum(a_wyj['FTAG'] >= a_wyj['FTHG'])
        a_wyj_total = len(a_wyj)
        a_x2_pct = (a_wyj_x2_hits / a_wyj_total) if a_wyj_total >= 4 else 0.5

        a_wyj_win_hits = sum(a_wyj['FTAG'] > a_wyj['FTHG'])
        h_dom_win_hits = sum(h_dom['FTHG'] > h_dom['FTAG'])

        emp_1x = (h_1x_pct + (1.0 - (a_wyj_win_hits / a_wyj_total if a_wyj_total > 0 else 0.5))) / 2.0
        emp_x2 = (a_x2_pct + (1.0 - (h_dom_win_hits / h_dom_total if h_dom_total > 0 else 0.5))) / 2.0

        blend_1x = ((p1_g + px_g) * 0.35) + (emp_1x * 0.65)
        blend_x2 = ((px_g + p2_g) * 0.35) + (emp_x2 * 0.65)

        typ_kod, final_prob = ("1X", blend_1x) if blend_1x >= blend_x2 else ("X2", blend_x2)
        try:
            o1, ox, o2 = float(str(o1_raw).replace(',','.')), float(str(ox_raw).replace(',','.')), float(str(o2_raw).replace(',','.'))
            fair_odd = round((o1 * ox) / (o1 + ox), 2) if typ_kod == "1X" else round((o2 * ox) / (o2 + ox), 2)
        except Exception:
            fair_odd = round(1 / final_prob, 2) if final_prob > 0 else 1.08

        if final_prob >= 0.70 and fair_odd >= 1.04:
            if typ_kod == "1X":
                arg = f"1X | Szansa: {round(final_prob*100)}% | Gosp ({h_tier_s}) dom 1X: {h_dom_1x_hits}/{h_dom_total} ({round(h_1x_pct*100)}%) | Gość ({a_tier_s}) wyjazd W: {a_wyj_win_hits}/{a_wyj_total}."
            else:
                arg = f"X2 | Szansa: {round(final_prob*100)}% | Gość ({a_tier_s}) wyjazd X2: {a_wyj_x2_hits}/{a_wyj_total} ({round(a_x2_pct*100)}%) | Gosp ({h_tier_s}) dom W: {h_dom_win_hits}/{h_dom_total}."
            add_pred(match_id, d_termin, d_date, d_time, league, home, away, "1X Pro", typ_kod, round(final_prob*100, 1), fair_odd, arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 2. GOAL LINE PRO
    if len(h_tot_all) >= 6 and len(a_tot_all) >= 6:
        h_dom_d, a_wyj_d = h_dom.to_dict('records'), a_wyj.to_dict('records')
        for line in [2.5, 3.5, 4.5, 5.5, 6.5]:
            p_h_u, h_th, h_tl, h_sm = get_weighted_stats(h_dom_d, 'Total_Goals', lambda x, l=line: pd.notna(x) and x < l, prior_prob=0.75)
            p_a_u, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_d, 'Total_Goals', lambda x, l=line: pd.notna(x) and x < l, prior_prob=0.75)
            avg_p_u = (p_h_u + p_a_u) / 2
            if avg_p_u >= 0.70:
                arg = f"U{line} | {last3_str} | Szanse D/W: Gosp {round(p_h_u*100)}%, Gość {round(p_a_u*100)}%. Trafienia: {h_th}/{h_tl}, {a_th}/{a_tl}."
                if h_sm or a_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Goal Line Pro", f"U{line}", round(avg_p_u*100, 1), dyn_anchors.get(f"U{line}", 1.10), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

        for line in [0.5, 1.5, 2.5]:
            p_h_o, h_th, h_tl, h_sm = get_weighted_stats(h_dom_d, 'Total_Goals', lambda x, l=line: pd.notna(x) and x > l, prior_prob=0.30)
            p_a_o, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_d, 'Total_Goals', lambda x, l=line: pd.notna(x) and x > l, prior_prob=0.30)
            avg_p_o = (p_h_o + p_a_o) / 2
            if avg_p_o >= 0.70:
                arg = f"O{line} | {last3_str} | Szanse D/W: Gosp {round(p_h_o*100)}%, Gość {round(p_a_o*100)}%. Trafienia: {h_th}/{h_tl}, {a_th}/{a_tl}."
                if h_sm or a_sm: arg += " | ⚠️ Bayes"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Goal Line Pro", f"O{line}", round(avg_p_o*100, 1), dyn_anchors.get(f"O{line}", 1.10), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 3. HANDICAP PRO (+1.5, +2.5 FT, HT, 2H)
    if len(h_tot_all) >= 6 and len(a_tot_all) >= 6:
        for team, is_h, opp_tier, my_tier in [(home, True, a_tier, h_tier), (away, False, h_tier, a_tier)]:
            t_df = h_dom if is_h else a_wyj
            t_all = h_tot_all if is_h else a_tot_all
            code_prefix = "H_AH+" if is_h else "A_AH+"
            for h_line in [1.5, 2.5]:
                cond = (lambda r, hl=h_line: (r['FTHG'] + hl > r['FTAG'])) if is_h else (lambda r, hl=h_line: (r['FTAG'] + hl > r['FTHG']))
                p_dw, th, tl, sm = get_weighted_stats(t_df, None, cond, prior_prob=0.85)
                p_all, tah, tal, _ = get_weighted_stats(t_all, None, cond, prior_prob=0.85)
                avg_p = (p_dw * 0.6) + (p_all * 0.4)
                if avg_p >= 0.88:
                    typ_kod = f"{code_prefix}{h_line}"
                    arg = f"Handicap Azjatycki +{h_line} ({'Gospodarz' if is_h else 'Gość'} {my_tier} vs {opp_tier}). Trafienia: D/W {th}/{tl}, Ogółem: {tah}/{tal}."
                    if sm: arg += " | ⚠️ Bayes"
                    add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Handicap Pro", typ_kod, round(avg_p*100, 1), dyn_anchors.get(typ_kod, 1.25), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

        for team, is_h, opp_tier, my_tier in [(home, True, a_tier, h_tier), (away, False, h_tier, a_tier)]:
            t_all = h_tot_all if is_h else a_tot_all
            for ht_line in [0.5, 1.5]:
                cond_ht = (lambda r, l=ht_line: pd.notna(r.get('HTHG')) and pd.notna(r.get('HTAG')) and ((r['HTHG'] + l) > r['HTAG'])) if is_h else (lambda r, l=ht_line: pd.notna(r.get('HTHG')) and pd.notna(r.get('HTAG')) and ((r['HTAG'] + l) > r['HTHG']))
                p_ht, th, tl, sm = get_weighted_stats(t_all, None, cond_ht, prior_prob=0.88)
                if p_ht >= 0.90:
                    typ_kod = f"HT_{'H' if is_h else 'A'}_AH+{ht_line}"
                    arg = f"Handicap 1. Połowa +{ht_line} ({'Gospodarz' if is_h else 'Gość'}). Trafienia w HT: {th}/{tl}."
                    add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Handicap Pro", typ_kod, round(p_ht*100, 1), dyn_anchors.get(typ_kod, 1.22), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

        for team, is_h, opp_tier, my_tier in [(home, True, a_tier, h_tier), (away, False, h_tier, a_tier)]:
            t_all = h_tot_all if is_h else a_tot_all
            for h2_line in [0.5, 1.5]:
                def cond_2h(r, l=h2_line, is_home_t=is_h):
                    if pd.isna(r.get('FTHG')) or pd.isna(r.get('FTAG')) or pd.isna(r.get('HTHG')) or pd.isna(r.get('HTAG')): return False
                    h2_h = float(r['FTHG']) - float(r['HTHG'])
                    h2_a = float(r['FTAG']) - float(r['HTAG'])
                    return (h2_h + l > h2_a) if is_home_t else (h2_a + l > h2_h)
                p_2h, th, tl, sm = get_weighted_stats(t_all, None, cond_2h, prior_prob=0.88)
                if p_2h >= 0.90:
                    typ_kod = f"2H_{'H' if is_h else 'A'}_AH+{h2_line}"
                    arg = f"Handicap 2. Połowa +{h2_line} ({'Gospodarz' if is_h else 'Gość'}). Trafienia w 2H: {th}/{tl}."
                    add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Handicap Pro", typ_kod, round(p_2h*100, 1), dyn_anchors.get(typ_kod, 1.22), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 4. BETBUILDER PRO (SZABLONY PREMIUM)
    if len(h_tot_all) >= 6 and len(a_tot_all) >= 6 and len(h_dom) >= 4 and len(a_wyj) >= 4:
        h_dom_d, a_wyj_d = h_dom.to_dict('records'), a_wyj.to_dict('records')
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

    # 5. MULTIGOL
    if len(h_tot_all) >= 6 and len(a_tot_all) >= 6 and len(h_dom) >= 4 and len(a_wyj) >= 4:
        h_last_g = get_last_match_goals(fixture_base, home)
        a_last_g = get_last_match_goals(fixture_base, away)
        if h_last_g == 0 or h_last_g > 5 or a_last_g == 0 or a_last_g > 5:
            h_dom_d, a_wyj_d = h_dom.to_dict('records'), a_wyj.to_dict('records')
            p_h_15, h_th, h_tl, h_sm = get_weighted_stats(h_dom_d, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5, prior_prob=0.80)
            p_a_15, a_th, a_tl, a_sm = get_weighted_stats(a_wyj_d, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 5, prior_prob=0.80)
            prob_1_5 = (p_h_15 + p_a_15) / 2

            p_h_16, h_th16, h_tl16, h_sm16 = get_weighted_stats(h_dom_d, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 6, prior_prob=0.85)
            p_a_16, a_th16, a_tl16, a_sm16 = get_weighted_stats(a_wyj_d, 'Total_Goals', lambda x: pd.notna(x) and 1 <= x <= 6, prior_prob=0.85)
            prob_1_6 = (p_h_16 + p_a_16) / 2

            if prob_1_5 >= 0.90 or prob_1_6 >= 0.90:
                t_kod, pewnosc, hc_c, hc_l, ac_c, ac_l = ("MG_1-5", prob_1_5, h_th, h_tl, a_th, a_tl) if prob_1_5 >= 0.90 else ("MG_1-6", prob_1_6, h_th16, h_tl16, a_th16, a_tl16)
                arg = f"Regresja Multigol po anomalii ({last3_str}). Trafienia D/W: {hc_c}/{hc_l}, {ac_c}/{ac_l}."
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Multigol", t_kod, round(pewnosc*100, 1), round(1.0 + (((1/pewnosc)-1.0)/1.5), 2), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 6. CORNERS PRO
    valid_c = m_hist.dropna(subset=['Corners_H', 'Corners_A']).copy()
    h_dom_c = valid_c[(valid_c['Base_League'] == fixture_base) & (valid_c['Home'] == home)]
    a_wyj_c = valid_c[(valid_c['Base_League'] == fixture_base) & (valid_c['Away'] == away)]

    if len(h_dom_c) >= 3 and len(a_wyj_c) >= 3:
        h_c_dict, a_c_dict = h_dom_c.to_dict('records'), a_wyj_c.to_dict('records')
        for line in [10.5, 11.5, 12.5]:
            p_hc, h_th, h_tl, _ = get_weighted_stats(h_c_dict, 'Total_Corners', lambda x, l=line: pd.notna(x) and x < l)
            p_ac, a_th, a_tl, _ = get_weighted_stats(a_c_dict, 'Total_Corners', lambda x, l=line: pd.notna(x) and x < l)
            avg_p = (p_hc + p_ac) / 2
            if avg_p >= 0.88:
                arg = f"C_U{line} (D: {h_th}/{h_tl}, W: {a_th}/{a_tl})"
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Corners Pro", f"C_U{line}", round(avg_p*100, 1), dyn_anchors.get(f"C_U{line}", 1.15), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))
                break

        for line in [4.5, 5.5]:
            p_ac, a_th, a_tl, _ = get_weighted_stats(a_c_dict, 'Corners_A', lambda x, l=line: pd.notna(x) and x < l)
            h_conceded_avg = h_dom_c['Corners_A'].mean() if len(h_dom_c) > 0 else 4.0
            if p_ac >= 0.90 and h_conceded_avg < (line + 0.3):
                arg = f"AC_U{line} | Gość under w {a_th}/{a_tl} meczach, Gospodarz dopuszcza rywalom śr. {h_conceded_avg:.1f} rożnych."
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Corners Pro", f"AC_U{line}", round(p_ac*100, 1), dyn_anchors.get(f"AC_U{line}", 1.10), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))
                break

    # 7. SHOTS PRO
    valid_s = m_hist.dropna(subset=['Shots_H', 'Shots_A']).copy()
    if not valid_s.empty:
        h_dom_s = valid_s[(valid_s['Base_League'] == fixture_base) & (valid_s['Home'] == home)]
        a_wyj_s = valid_s[(valid_s['Base_League'] == fixture_base) & (valid_s['Away'] == away)]

        if len(h_dom_s) >= 2 and len(a_wyj_s) >= 2:
            h_len, a_len = len(h_dom_s), len(a_wyj_s)
            h_s_win = sum((h_dom_s['Shots_H'] - h_dom_s['Shots_A']) > 0)
            a_s_lose = sum((a_wyj_s['Shots_A'] - a_wyj_s['Shots_H']) < 0)
            prob_h_s = (((h_s_win + 0.9) / (h_len + 1.5)) * 3.5 + ((a_s_lose + 0.9) / (a_len + 1.5)) * 1.5) / 5.0

            if prob_h_s >= 0.72:
                arg = f"Strzały 1X2 (S_1): Gosp wygrana dom {h_s_win}/{h_len}, Gość porażka wyjazd {a_s_lose}/{a_len}."
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Shots Pro", "S_1", round(prob_h_s*100, 1), dyn_anchors.get("S_1", 1.34), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

            for s_line in [13.5, 14.5]:
                p_a_u, ta_u, tal_u, _ = get_weighted_stats(a_wyj_s, 'Shots_A', lambda x, l=s_line: pd.notna(x) and x < l)
                h_conceded_s = h_dom_s['Shots_A'].mean() if len(h_dom_s) > 0 else 12.0
                a_scored_s = a_wyj_s['Shots_A'].mean() if len(a_wyj_s) > 0 else 11.0
                if p_a_u >= 0.85 and h_conceded_s < (s_line + 0.5):
                    arg = f"A_S_U{s_line} | Gość oddaje śr. {a_scored_s:.1f} strzałów, Gospodarz dopuszcza śr. {h_conceded_s:.1f} strzałów."
                    add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Shots Pro", f"A_S_U{s_line}", round(p_a_u*100, 1), dyn_anchors.get(f"A_S_U{s_line}", 1.30), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))
                    break

    # 8. SHOTS ON TARGET PRO
    valid_st = m_hist.dropna(subset=['ShotsTarget_H', 'ShotsTarget_A']).copy()
    if not valid_st.empty:
        h_dom_st = valid_st[(valid_st['Base_League'] == fixture_base) & (valid_st['Home'] == home)]
        a_wyj_st = valid_st[(valid_st['Base_League'] == fixture_base) & (valid_st['Away'] == away)]

        if len(h_dom_st) >= 2 and len(a_wyj_st) >= 2:
            h_len, a_len = len(h_dom_st), len(a_wyj_st)
            h_st_win = sum((h_dom_st['ShotsTarget_H'] - h_dom_st['ShotsTarget_A']) > 0)
            a_st_lose = sum((a_wyj_st['ShotsTarget_A'] - a_wyj_st['ShotsTarget_H']) < 0)
            prob_h_st = (((h_st_win + 0.9) / (h_len + 1.5)) * 3.5 + ((a_st_lose + 0.9) / (a_len + 1.5)) * 1.5) / 5.0

            if prob_h_st >= 0.72:
                arg = f"Strzały Celne 1X2 (ST_1): Gosp wygrana dom {h_st_win}/{h_len}, Gość porażka wyjazd {a_st_lose}/{a_len}."
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Shots On Target Pro", "ST_1", round(prob_h_st*100, 1), dyn_anchors.get("ST_1", 1.64), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

            for st_line in [4.5, 5.5]:
                p_a_st_u, ta_u, tal_u, _ = get_weighted_stats(a_wyj_st, 'ShotsTarget_A', lambda x, l=st_line: pd.notna(x) and x < l)
                h_conceded_st = h_dom_st['ShotsTarget_A'].mean() if len(h_dom_st) > 0 else 4.0
                a_scored_st = a_wyj_st['ShotsTarget_A'].mean() if len(a_wyj_s) > 0 else 3.5
                if p_a_st_u >= 0.85 and h_conceded_st < (st_line + 0.3):
                    arg = f"A_ST_U{st_line} | Gość śr. {a_scored_st:.1f} SOT, Gospodarz pozwala rywalom na śr. {h_conceded_st:.1f} SOT."
                    add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Shots On Target Pro", f"A_ST_U{st_line}", round(p_a_st_u*100, 1), dyn_anchors.get(f"A_ST_U{st_line}", 1.30), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))
                    break

    # 9. ZIMNY PRYSZNIC
    if h_tier in ['Koszyk 1', 'Koszyk 2'] and len(h_tot_all) > 0:
        last_m = h_tot_all.iloc[0]
        if last_m['Away'] == home and last_m['FTHG'] >= last_m['FTAG']:
            opp_tier = team_tiers.get((last_m['League'], last_m['Home']), 'Koszyk 1')
            if opp_tier in ['Koszyk 4', 'Koszyk 5', 'Koszyk 6']:
                arg = f"Gospodarz ({h_tier}) szuka rewanżu u siebie po stracie punktów na wyjeździe z {opp_tier}."
                add_pred(match_id, d_termin, d_date, d_time, league, home, away, "Cold Shower", "1", 85.0, dyn_anchors.get("1", 1.25), arg, dyn_anchors, match_odds=(o1_raw, ox_raw, o2_raw))

    # 10. UKRYTA FORMA
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

    # 11. ANOMALIE ROŻNYCH
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

    # 12. ANOMALIE BRAMKOWE
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
# 11. BACKTESTER, HISTORIA TYPÓW I SYNCHRONIZACJA
# ==================================================================================================

print("\nInicjalizacja Modułu Backtestingu i Śledzenia Skuteczności...")

cols_all_pred = [
    "Match_ID", "Zagrane", "Wyslij_AKO", "Kupon_ID", "Termin", "Data", "Godzina", "Liga",
    "Gospodarz", "Gość", "Engine", "Typ", "Szansa", "Kurs_Szac",
    "Kurs_Realny_Superbet", "Kurs_Realny_Fortuna", "Status_kursu_realnego",
    "Argumentacja", "Przedzial_Kursowy", "Consensus_Score", "Status"
]
cols_historia = [
    "Match_ID", "Zagrane", "Kupon_ID", "Data", "Godzina", "Liga",
    "Gospodarz", "Gość", "Engine", "Typ", "Szansa", "Kurs_Szac",
    "Kurs_Realny_Superbet", "Kurs_Realny_Fortuna", "Status_kursu_realnego",
    "Argumentacja", "Przedzial_Kursowy", "Consensus_Score", "Status", "Profit", "Yield_Wplyw"
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
    if not OFFLINE_LOCAL and spreadsheet is not None:
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

    # W trybie offline: natychmiastowe rozliczenie statusów względem bazy zakończonych spotkań
    if not results_clean.empty:
        results_dict = {str(r['Match_ID']): r for r in results_clean.to_dict('records')}
        for i_pred, r_pred in df_all_predictions.iterrows():
            m_id = str(r_pred['Match_ID'])
            if m_id in results_dict:
                df_all_predictions.at[i_pred, 'Status'] = evaluate_bet(r_pred['Typ'], results_dict[m_id])

    for col in cols_all_pred:
        if col not in df_all_predictions.columns: df_all_predictions[col] = ""
    df_all_predictions = df_all_predictions[cols_all_pred]
else:
    df_all_predictions = pd.DataFrame(columns=cols_all_pred)

# Historia typów
df_historia = pd.DataFrame(columns=cols_historia)
if not OFFLINE_LOCAL and spreadsheet is not None:
    try:
        ws_historia = spreadsheet.worksheet("Historia_Typow")
        historia_dane = ws_historia.get_all_values()
        df_historia = pd.DataFrame(historia_dane[1:], columns=historia_dane[0]) if len(historia_dane) > 0 else pd.DataFrame(columns=cols_historia)
    except gspread.exceptions.WorksheetNotFound:
        spreadsheet.add_worksheet(title="Historia_Typow", rows=10000, cols=len(cols_historia))
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
            map_kurs_sb = nowe_typy_df.set_index('Unikalny_Klucz')['Kurs_Realny_Superbet'].to_dict()
            map_kurs_ft = nowe_typy_df.set_index('Unikalny_Klucz')['Kurs_Realny_Fortuna'].to_dict()
            map_status_real = nowe_typy_df.set_index('Unikalny_Klucz')['Status_kursu_realnego'].to_dict()
            map_arg = nowe_typy_df.set_index('Unikalny_Klucz')['Argumentacja'].to_dict()
            map_przedzial = nowe_typy_df.set_index('Unikalny_Klucz')['Przedzial_Kursowy'].to_dict()
            map_consensus = nowe_typy_df.set_index('Unikalny_Klucz')['Consensus_Score'].to_dict()

            for idx in df_historia[w_oczek_mask].index:
                klucz = df_historia.at[idx, 'Unikalny_Klucz']
                if klucz in map_szansa:
                    df_historia.at[idx, 'Szansa'] = str(map_szansa[klucz])
                    df_historia.at[idx, 'Kurs_Szac'] = str(map_kurs_szac[klucz])
                    df_historia.at[idx, 'Kurs_Realny_Superbet'] = str(map_kurs_sb.get(klucz, "Brak"))
                    df_historia.at[idx, 'Kurs_Realny_Fortuna'] = str(map_kurs_ft.get(klucz, "Brak"))
                    df_historia.at[idx, 'Status_kursu_realnego'] = str(map_status_real.get(klucz, ""))
                    df_historia.at[idx, 'Argumentacja'] = str(map_arg[klucz])
                    df_historia.at[idx, 'Przedzial_Kursowy'] = str(map_przedzial.get(klucz, ""))
                    df_historia.at[idx, 'Consensus_Score'] = str(map_consensus.get(klucz, ""))

        do_dodania = nowe_typy_df[~nowe_typy_df['Unikalny_Klucz'].isin(df_historia['Unikalny_Klucz'])].copy().drop(columns=['Unikalny_Klucz'])
        df_historia = df_historia.drop(columns=['Unikalny_Klucz'])
    else:
        do_dodania = nowe_typy_df.copy()
        if 'Unikalny_Klucz' in do_dodania.columns: do_dodania = do_dodania.drop(columns=['Unikalny_Klucz'])

    df_historia = pd.concat([df_historia, do_dodania], ignore_index=True)

# Rozliczanie statusów w historii
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
                    k_sb_str = str(row.get("Kurs_Realny_Superbet", "")).replace(',', '.').strip()
                    k_ft_str = str(row.get("Kurs_Realny_Fortuna", "")).replace(',', '.').strip()
                    k_szac_str = str(row.get("Kurs_Szac", "")).replace(',', '.').strip()

                    cands = []
                    for v in [k_sb_str, k_ft_str]:
                        if v and v not in ["Brak", "nan", "None"]:
                            try: cands.append(float(v))
                            except: pass
                    kurs = max(cands) if cands else (float(k_szac_str) if k_szac_str else 1.0)

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

df_ako = pd.DataFrame(columns=cols_ako)
if not OFFLINE_LOCAL and spreadsheet is not None:
    try:
        ws_ako = spreadsheet.worksheet("Kupony_AKO")
        ako_dane = ws_ako.get_all_values()
        df_ako = pd.DataFrame(ako_dane[1:], columns=ako_dane[0]) if len(ako_dane) > 0 else pd.DataFrame(columns=cols_ako)
    except gspread.exceptions.WorksheetNotFound:
        spreadsheet.add_worksheet(title="Kupony_AKO", rows=1000, cols=15)
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
                k_sb_str = str(r.get('Kurs_Realny_Superbet', '')).replace(',', '.').strip()
                k_ft_str = str(r.get('Kurs_Realny_Fortuna', '')).replace(',', '.').strip()
                k_szac_str = str(r.get('Kurs_Szac', '')).replace(',', '.').strip()

                cands = []
                for v in [k_sb_str, k_ft_str]:
                    if v and v not in ["Brak", "nan", "None"]:
                        try: cands.append(float(v))
                        except: pass
                kr = max(cands) if cands else (float(k_szac_str) if k_szac_str else 1.0)
                try:
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
    if not OFFLINE_LOCAL:
        now_time = datetime.now()
        df_all_predictions = df_all_predictions[df_all_predictions['Data_Sort'] >= now_time - timedelta(hours=3)]
    df_all_predictions = df_all_predictions.sort_values(by=["Data_Sort", "Szansa"], ascending=[True, False]).drop(columns=['Data_Sort', 'Unikalny_Klucz'], errors='ignore')

# Top Wybory
top_wybory_df = pd.DataFrame()
if not df_all_predictions.empty:
    print("\nGenerowanie zoptymalizowanego widoku Top Wybory...")
    active_pred = df_all_predictions.copy()
    active_pred['Szansa_Num'] = pd.to_numeric(active_pred['Szansa'], errors='coerce').fillna(0.0)

    def resolve_top_odd(r):
        candidates = []
        for col in ['Kurs_Realny_Superbet', 'Kurs_Realny_Fortuna']:
            v = str(r.get(col, '')).replace(',', '.').strip()
            if v and v not in ["Brak", "nan", "None"]:
                try: candidates.append(float(v))
                except: pass
        if candidates: return max(candidates)
        try: return float(str(r.get('Kurs_Szac', 1.0)).replace(',', '.'))
        except: return 1.0

    active_pred['Kurs_Num'] = active_pred.apply(resolve_top_odd, axis=1)
    top_list = []
    for eng in active_pred['Engine'].unique():
        df_eng = active_pred[active_pred['Engine'] == eng].sort_values(by=['Szansa_Num', 'Kurs_Num'], ascending=[False, False])
        picks_90 = df_eng[df_eng['Szansa_Num'] >= 90.0]
        top_list.append(picks_90 if len(picks_90) >= 20 else df_eng.head(20))

    if top_list:
        top_wybory_df = pd.concat(top_list, ignore_index=True).drop_duplicates(subset=['Match_ID', 'Engine', 'Typ'])
        top_wybory_df = top_wybory_df.sort_values(by=['Szansa_Num', 'Data', 'Godzina'], ascending=[False, True, True])

# ==================================================================================================
# 12. ZAPIS WYNIKÓW (GOOGLE SHEETS LUB LOKALNE CSV W TRYBIE OFFLINE)
# ==================================================================================================

if not OFFLINE_LOCAL and spreadsheet is not None:
    all_sheets = ["Summary", "Fixtures", "Results", "League_Tables", "Historia_Typow", "All_Predictions", "Top_Wybory", "Kupony_AKO"]
    for s_name in all_sheets:
        try: spreadsheet.worksheet(s_name)
        except gspread.exceptions.WorksheetNotFound:
            spreadsheet.add_worksheet(title=s_name, rows=1000, cols=30)

print("\nFinalny zapis zintegrowanych danych...")
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

if not OFFLINE_LOCAL and spreadsheet is not None:
    time.sleep(1.2)
    spreadsheet.worksheet("Summary").clear()
    spreadsheet.worksheet("Summary").update(summary_data)
else:
    df_summary = pd.DataFrame(summary_data, columns=["Sekcja", "Wartosc", "Status"])
    safe_batch_update(None, "Summary", df_summary)

print("\n" + "=" * 90)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print(f"Wygenerowano predykcji: {len(df_all_predictions)}.")
print(f"Wyselekcjonowano Top Wyborów: {len(top_wybory_df)}.")
print("1. Wdrożono wielopoziomowy matcher dla Fortuny i Superbet 1:1.")
print("2. Wyeliminowano kursy poniżej progu 1.05.")
print("3. Wdrożono dwustronne statystyki rywala (obrona) w strzałach i rożnych.")
print("4. Zachowano kompletne silniki analityczne (Zimny Prysznic, Ukryta Forma, Anomalie) oraz portfel AKO.")
print("=" * 90)