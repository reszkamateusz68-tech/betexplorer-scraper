import os
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

now = datetime.now(timezone.utc)
target_day = now.date() + timedelta(days=1)
next_day = target_day + timedelta(days=1)

start_iso = f"{target_day}T00:00:00.000Z"
end_iso = f"{next_day}T00:00:00.000Z"

base_url = "https://production-superbet-offer-pl.freetls.fastly.net/v3/pl-PL/events"
params = {
    "startDate": start_iso,
    "endDate": end_iso,
    "index": "active-prematch",
    "sports": "5"
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.superbet.pl/",
    "Origin": "https://www.superbet.pl"
}

print(f"1. Pobieram kalendarz meczów na JUTRO ({start_iso[:10]})...")
start_time = time.time()
session = requests.Session()

try:
    res = session.get(base_url, headers=headers, params=params, timeout=12)
    if res.status_code != 200:
        print(f"Błąd kalendarza: {res.status_code}")
        exit()
    raw_data = res.json()
except Exception as e:
    print(f"Błąd połączenia: {e}")
    exit()

event_ids = []
d = raw_data.get("data", raw_data.get("events", [])) if isinstance(raw_data, dict) else raw_data
if isinstance(d, list):
    for item in d:
        if isinstance(item, (int, str)):
            event_ids.append(str(item))
        elif isinstance(item, dict):
            eid = item.get("eventId") or item.get("event_id") or item.get("id")
            if eid:
                event_ids.append(str(eid))

total_events = len(event_ids)
print(f"2. Znaleziono {total_events} meczów na jutro. Pobieram pełną ofertę rynków (16 wątków)...")

if not total_events:
    print("Brak meczów na jutro w ofercie.")
    exit()

mapowanie_sb = {}
if os.path.exists("slownik_druzyn.json"):
    try:
        with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
            slownik = json.load(f)
            map_be_to_sb = slownik.get("BetExplorer_To_Superbet", {})
            mapowanie_sb = {v.strip().lower(): k for k, v in map_be_to_sb.items() if v.strip() != ""}
    except Exception:
        pass

def fetch_single_event(event_id):
    detail_url = f"https://production-superbet-offer-pl.freetls.fastly.net/v3/subscription/pl-PL/events?events={event_id}"
    match_rows = []
    match_name = f"Mecz ID {event_id}"
    
    try:
        with requests.get(detail_url, headers=headers, stream=True, timeout=8) as r:
            raw_match = None
            for line in r.iter_lines(decode_unicode=True):
                if line and line.startswith("data:"):
                    raw_match = line[5:].strip()
                    break

            if raw_match:
                match_data = json.loads(raw_match)
                if isinstance(match_data, dict):
                    match_data = match_data.get("data", [])

                for m_ev in match_data:
                    fixture = m_ev.get("fixture", {}) if isinstance(m_ev, dict) else {}
                    match_name = m_ev.get("event_name") or fixture.get("event_name", match_name)
                    event_date = m_ev.get("event_date") or fixture.get("event_date", "")

                    if "·" in match_name:
                        parts = match_name.split("·")
                        home_team, away_team = parts[0].strip(), parts[1].strip()
                    elif " - " in match_name:
                        parts = match_name.split(" - ")
                        home_team, away_team = parts[0].strip(), parts[1].strip()
                    elif "-" in match_name:
                        parts = match_name.split("-")
                        home_team, away_team = parts[0].strip(), parts[1].strip()
                    else:
                        home_team, away_team = match_name.strip(), ""

                    raw_markets = m_ev.get("markets", [])
                    market_list = list(raw_markets.values()) if isinstance(raw_markets, dict) else raw_markets

                    for m in market_list:
                        if not isinstance(m, dict):
                            continue
                        market_name = m.get("name") or m.get("market_name", "")

                        raw_odds = m.get("odds", [])
                        odds_list = list(raw_odds.values()) if isinstance(raw_odds, dict) else raw_odds

                        for o in odds_list:
                            if not isinstance(o, dict):
                                continue
                            meta = o.get("metadata", {}) if isinstance(o.get("metadata"), dict) else {}

                            odd_type = str(meta.get("name") or o.get("name") or o.get("code") or o.get("outcome_name") or "").strip()
                            full_info = str(meta.get("info") or o.get("info") or o.get("special_bet_value") or "").strip()
                            price_raw = o.get("price", 1.0)

                            try:
                                price_flt = float(price_raw)
                            except:
                                price_flt = 1.0

                            if price_flt <= 1.0:
                                continue

                            match_rows.append({
                                "Event_ID": event_id,
                                "Data": event_date,
                                "Gospodarz": home_team,
                                "Gosc": away_team,
                                "Rynek": market_name,
                                "Typ": odd_type,
                                "Opis_Zdarzenia": full_info,
                                "Kurs_Float": price_flt,
                                "Kurs": str(price_flt).replace(".", ",")
                            })
        return match_rows, match_name, None
    except Exception as e:
        return [], match_name, str(e)

all_rows = []
completed_count = 0

with ThreadPoolExecutor(max_workers=16) as executor:
    futures = {executor.submit(fetch_single_event, eid): eid for eid in event_ids}
    for future in as_completed(futures):
        completed_count += 1
        rows, m_name, _ = future.result()
        if rows:
            all_rows.extend(rows)
            print(f"[{completed_count:03d}/{total_events}] Pobrano: {m_name}")
        else:
            print(f"[{completed_count:03d}/{total_events}] Brak oferty dla {m_name}")

elapsed = round(time.time() - start_time, 2)

if all_rows:
    df = pd.DataFrame(all_rows)
    df_csv = df.drop(columns=["Kurs_Float"])
    df_csv.to_csv("superbet_baza_kursow_jutro.csv", sep=";", index=False, encoding="utf-8-sig")

    print("\n3. Budowanie precyzyjnej bazy JSON...")
    superbet_db = {}

    for row in all_rows:
        sb_h = row["Gospodarz"].strip()
        sb_a = row["Gosc"].strip()
        be_h = mapowanie_sb.get(sb_h.lower(), sb_h)
        be_a = mapowanie_sb.get(sb_a.lower(), sb_a)
        key = f"{be_h.lower()}___{be_a.lower()}"

        if key not in superbet_db:
            superbet_db[key] = {
                "info": {
                    "event_id": row["Event_ID"],
                    "data": row["Data"],
                    "gospodarz_sb": sb_h,
                    "gosc_sb": sb_a,
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

        if rynek not in superbet_db[key]["rynki"]:
            superbet_db[key]["rynki"][rynek] = {}
        superbet_db[key]["rynki"][rynek][f"{typ} | {opis}".strip(" |")] = kurs

        rynek_l = rynek.lower().strip()
        typ_l = typ.lower().strip()

        if rynek_l == "mecz" and typ in ["1", "X", "2"]:
            superbet_db[key]["kursy"][typ] = kurs
        elif rynek_l == "podwójna szansa" and typ in ["1X", "X2", "12"]:
            superbet_db[key]["kursy"][typ] = kurs
        elif rynek_l == "obie drużyny strzelą":
            if typ_l == "tak": superbet_db[key]["kursy"]["BTTS_TAK"] = kurs
            elif typ_l == "nie": superbet_db[key]["kursy"]["BTTS_NIE"] = kurs
        elif rynek_l == "liczba goli":
            m_line = re.search(r'(\d+(?:\.\d+)?)', typ_l)
            if m_line:
                line_val = m_line.group(1)
                if "poniżej" in typ_l or typ_l.startswith("-"):
                    superbet_db[key]["kursy"][f"U{line_val}"] = kurs
                elif "powyżej" in typ_l or typ_l.startswith("+"):
                    superbet_db[key]["kursy"][f"O{line_val}"] = kurs
        elif rynek_l == "1. połowa - liczba goli":
            m_line = re.search(r'(\d+(?:\.\d+)?)', typ_l)
            if m_line:
                line_val = m_line.group(1)
                if "poniżej" in typ_l or typ_l.startswith("-"):
                    superbet_db[key]["kursy"][f"HT_U{line_val}"] = kurs
                elif "powyżej" in typ_l or typ_l.startswith("+"):
                    superbet_db[key]["kursy"][f"HT_O{line_val}"] = kurs
        elif rynek_l == f"{sb_h.lower()} - liczba goli":
            m_line = re.search(r'(\d+(?:\.\d+)?)', typ_l)
            if m_line and ("poniżej" in typ_l or typ_l.startswith("-")):
                superbet_db[key]["kursy"][f"HU{m_line.group(1)}"] = kurs
        elif rynek_l == f"{sb_a.lower()} - liczba goli":
            m_line = re.search(r'(\d+(?:\.\d+)?)', typ_l)
            if m_line and ("poniżej" in typ_l or typ_l.startswith("-")):
                superbet_db[key]["kursy"][f"AU{m_line.group(1)}"] = kurs
        elif rynek_l == "liczba rzutów rożnych":
            m_line = re.search(r'(\d+(?:\.\d+)?)', typ_l)
            if m_line:
                line_val = m_line.group(1)
                if "poniżej" in typ_l or typ_l.startswith("-"):
                    superbet_db[key]["kursy"][f"C_U{line_val}"] = kurs
                elif "powyżej" in typ_l or typ_l.startswith("+"):
                    superbet_db[key]["kursy"][f"C_O{line_val}"] = kurs
        elif "strzały w meczu - h2h" in rynek_l:
            if typ in ["1", "2"]: superbet_db[key]["kursy"][f"S_{typ}"] = kurs
        elif "celne strzały w meczu - h2h" in rynek_l:
            if typ in ["1", "2"]: superbet_db[key]["kursy"][f"ST_{typ}"] = kurs

    with open("superbet_baza_jutro.json", "w", encoding="utf-8") as f:
        json.dump(superbet_db, f, ensure_ascii=False, indent=2)

    print(f"SUKCES! Baza Superbet (JUTRO) została zaktualizowana w {elapsed} s.")
