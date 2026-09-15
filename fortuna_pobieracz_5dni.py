import os
import json
import re
import time
import socket
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pandas as pd

# Obejście problemów z DNS na Windows
try:
    socket.gethostbyname("api.efortuna.pl")
except socket.gaierror:
    import urllib3.util.connection as urllib_conn
    def force_ipv4():
        return socket.AF_INET
    urllib_conn.allowed_gai_family = force_ipv4

HEADERS = {
    "accept": "*/*",
    "accept-language": "pl-PL",
    "origin": "https://www.efortuna.pl",
    "referer": "https://www.efortuna.pl/",
    "sec-ch-ua": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
}

session = requests.Session()
session.headers.update(HEADERS)

DAYS_CONFIG = [
    ("today", "Dziś"),
    ("tomorrow", "Jutro"),
    ("tomorrow_1", "Za 2 dni"),
    ("tomorrow_2", "Za 3 dni"),
    ("tomorrow_3", "Za 4 dni")
]

start_time = time.time()
print("1. Zbieram listę meczów dla najbliższych 5 dni...")

# Słownik unikalnych meczów (żeby uniknąć duplikatów przy nakładających się filtrach)
all_matches_meta = {}

for filter_key, label in DAYS_CONFIG:
    t_url = f"https://api.efortuna.pl/offer/structure/api/v1_0/sport/ufo:sprt:00/tournaments?categories=true&timeFilter={filter_key}"
    try:
        res = session.get(t_url, timeout=12)
        if res.status_code != 200:
            print(f"[{label}] Błąd pobierania lig: {res.status_code}")
            continue
        t_data = res.json()
        tour_items = t_data.get("tournaments", [])
        tour_ids = [t["id"] for t in tour_items if isinstance(t, dict) and "id" in t]
        
        day_matches = 0
        for tid in tour_ids:
            m_url = f"https://api.efortuna.pl/offer/structure/api/v1_0/tournament/{tid}/matches?timeFilter={filter_key}"
            try:
                r = session.get(m_url, timeout=8)
                if r.status_code != 200:
                    continue
                m_data = r.json()
                for fix in m_data.get("fixtures", []):
                    fid = fix.get("id")
                    if not fid or fix.get("status") != "ACTIVE" or fid in all_matches_meta:
                        continue
                    
                    home_team, away_team = "", ""
                    for p in fix.get("participants", []):
                        if p.get("type") == "HOME":
                            home_team = p.get("name", "").strip()
                        elif p.get("type") == "AWAY":
                            away_team = p.get("name", "").strip()
                    
                    raw_name = fix.get("name", "")
                    if not home_team or not away_team:
                        parts = raw_name.split(" - ")
                        if len(parts) == 2:
                            home_team, away_team = parts[0].strip(), parts[1].strip()
                        else:
                            home_team, away_team = raw_name.strip(), ""

                    dt_ms = fix.get("startDatetime")
                    date_iso = datetime.fromtimestamp(dt_ms / 1000, tz=timezone.utc).isoformat() if dt_ms else ""

                    all_matches_meta[fid] = {
                        "home": home_team,
                        "away": away_team,
                        "date": date_iso,
                        "name": f"{home_team} - {away_team}" if away_team else raw_name
                    }
                    day_matches += 1
            except Exception:
                continue
        print(f" -> {label}: znaleziono {day_matches} nowych meczów")
    except Exception as e:
        print(f"[{label}] Błąd połączenia: {e}")

total_matches = len(all_matches_meta)
print(f"\n2. Łącznie unikalnych spotkań na 5 dni: {total_matches}. Pobieram kursy (12 wątków)...")

if not total_matches:
    print("Brak meczów w ofercie.")
    exit()

mapowanie_fortuna = {}
if os.path.exists("slownik_druzyn.json"):
    try:
        with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
            slownik = json.load(f)
            map_be = slownik.get("BetExplorer_To_Fortuna", {})
            mapowanie_fortuna = {v.strip().lower(): k for k, v in map_be.items() if v.strip() != ""}
    except Exception:
        pass

def fetch_match_markets(fid):
    meta = all_matches_meta.get(fid, {})
    h_team = meta.get("home", "")
    a_team = meta.get("away", "")
    e_date = meta.get("date", "")
    m_name = meta.get("name", fid)
    
    url = f"https://api.efortuna.pl/offer/markets/api/v1_0/fixture/{fid}/markets"
    rows = []

    try:
        res = session.get(url, timeout=12)
        if res.status_code != 200:
            return [], m_name, f"Status {res.status_code}"
        
        markets_list = res.json()
        if not isinstance(markets_list, list):
            return [], m_name, "Błędny format"

        for mkt in markets_list:
            market_name = mkt.get("name") or mkt.get("marketTypeName", "")
            
            for outcome in mkt.get("outcomes", []):
                odd_name = outcome.get("name", "")
                long_name = outcome.get("longName", "")
                
                try:
                    price_flt = float(outcome.get("odds", 1.0))
                except (ValueError, TypeError):
                    price_flt = 1.0

                if price_flt <= 1.0:
                    continue

                rows.append({
                    "Event_ID": fid,
                    "Data": e_date,
                    "Gospodarz": h_team,
                    "Gosc": a_team,
                    "Rynek": market_name,
                    "Typ": odd_name,
                    "Opis_Zdarzenia": long_name,
                    "Kurs_Float": price_flt,
                    "Kurs": str(price_flt).replace(".", ",")
                })

        return rows, m_name, None
    except Exception as e:
        return [], m_name, str(e)

all_rows = []
completed = 0

with ThreadPoolExecutor(max_workers=12) as executor:
    futures = {executor.submit(fetch_match_markets, fid): fid for fid in all_matches_meta.keys()}
    for future in as_completed(futures):
        completed += 1
        rows, match_title, err = future.result()
        if rows:
            all_rows.extend(rows)
            print(f"[{completed:03d}/{total_matches}] Pobrano ({len(rows)} kursów): {match_title}")
        else:
            print(f"[{completed:03d}/{total_matches}] Pominięto: {match_title}")

elapsed = round(time.time() - start_time, 2)

if all_rows:
    df = pd.DataFrame(all_rows)
    df.drop(columns=["Kurs_Float"]).to_csv("fortuna_baza_kursow_5dni.csv", sep=";", index=False, encoding="utf-8-sig")

    print("\n3. Budowanie zbiorczego pliku JSON dla 5 dni...")
    fortuna_db = {}

    for row in all_rows:
        f_h = row["Gospodarz"].strip()
        f_a = row["Gosc"].strip()
        be_h = mapowanie_fortuna.get(f_h.lower(), f_h)
        be_a = mapowanie_fortuna.get(f_a.lower(), f_a)
        key = f"{be_h.lower()}___{be_a.lower()}"

        if key not in fortuna_db:
            fortuna_db[key] = {
                "info": {
                    "event_id": row["Event_ID"],
                    "data": row["Data"],
                    "gospodarz_fortuna": f_h,
                    "gosc_fortuna": f_a,
                    "gospodarz_be": be_h,
                    "gosc_be": be_a
                },
                "kursy": {},
                "rynki": {}
            }

        rynek = str(row["Rynek"]).strip()
        typ = str(row["Typ"]).strip()
        opis = str(row["Opis_Zdarzenia"]).strip()
        kurs = row["Kurs_Float"]

        if rynek not in fortuna_db[key]["rynki"]:
            fortuna_db[key]["rynki"][rynek] = {}
        fortuna_db[key]["rynki"][rynek][f"{typ} | {opis}".strip(" |")] = kurs

        rynek_l = rynek.lower()
        typ_l = typ.lower()

        # 1. Mecz 1X2
        if rynek_l == "wynik meczu":
            if typ == "1": fortuna_db[key]["kursy"]["1"] = kurs
            elif typ == "0": fortuna_db[key]["kursy"]["X"] = kurs
            elif typ == "2": fortuna_db[key]["kursy"]["2"] = kurs

        # 2. Podwójna szansa
        elif "dwójtyp" in rynek_l or "podwójna szansa" in rynek_l:
            norm = typ.replace("0", "X")
            if norm in ["1X", "X2", "12"]:
                fortuna_db[key]["kursy"][norm] = kurs

        # 3. DNB
        elif "bez remisu" in rynek_l:
            if typ == "1": fortuna_db[key]["kursy"]["DNB_1"] = kurs
            elif typ == "2": fortuna_db[key]["kursy"]["DNB_2"] = kurs

        # 4. Obie strzelą (BTTS)
        elif "obie drużyny strzelą" in rynek_l:
            if typ_l == "tak": fortuna_db[key]["kursy"]["BTTS_TAK"] = kurs
            elif typ_l == "nie": fortuna_db[key]["kursy"]["BTTS_NIE"] = kurs

        # 5. Gole Under/Over
        elif "liczba goli" in rynek_l or "liczba bramek" in rynek_l:
            m_line = re.search(r'(\d+(?:\.\d+)?)', typ + " " + opis)
            if m_line:
                lv = m_line.group(1)
                text = (typ + " " + opis).lower()
                if any(x in text for x in ["mniej", "poniżej", "-"]):
                    fortuna_db[key]["kursy"][f"U{lv}"] = kurs
                elif any(x in text for x in ["więcej", "powyżej", "+"]):
                    fortuna_db[key]["kursy"][f"O{lv}"] = kurs

        # 6. Rzuty rożne mecz
        elif "rzuty rożne" in rynek_l and "liczba" in rynek_l:
            m_line = re.search(r'(\d+(?:\.\d+)?)', typ + " " + opis)
            if m_line:
                lv = m_line.group(1)
                text = (typ + " " + opis).lower()
                if any(x in text for x in ["mniej", "poniżej", "-"]):
                    fortuna_db[key]["kursy"][f"C_U{lv}"] = kurs
                elif any(x in text for x in ["więcej", "powyżej", "+"]):
                    fortuna_db[key]["kursy"][f"C_O{lv}"] = kurs

        # 7. Strzały w meczu H2H
        elif "strzały w meczu - h2h" in rynek_l:
            if typ in ["1", "2"]: fortuna_db[key]["kursy"][f"S_{typ}"] = kurs
        elif "strzały w światło" in rynek_l or "celne strzały" in rynek_l:
            if "h2h" in rynek_l and typ in ["1", "2"]:
                fortuna_db[key]["kursy"][f"ST_{typ}"] = kurs

    with open("fortuna_baza_5dni.json", "w", encoding="utf-8") as f:
        json.dump(fortuna_db, f, ensure_ascii=False, indent=2)

    print(f"\nSUKCES! Baza 5-dniowa zaktualizowana w {elapsed} s.")