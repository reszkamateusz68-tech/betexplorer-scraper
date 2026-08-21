import os
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

base_url = "https://production-superbet-offer-pl.freetls.fastly.net/v3/pl-PL/events"

now = datetime.now(timezone.utc)
start_iso = now.strftime("%Y-%m-%dT00:00:00.000Z")
end_iso = (now + timedelta(days=7)).strftime("%Y-%m-%dT23:59:59.000Z")

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

print("1. Pobieram kalendarz Superbet (7 dni)...")
start_time = time.time()
session = requests.Session()

try:
    res = session.get(base_url, headers=headers, params=params, timeout=12)
    if res.status_code != 200:
        exit()
    raw_data = res.json()
except Exception:
    exit()

event_ids = []
d = raw_data.get("data", raw_data.get("events", [])) if isinstance(raw_data, dict) else raw_data
if isinstance(d, list):
    for item in d:
        if isinstance(item, (int, str)): event_ids.append(str(item))
        elif isinstance(item, dict):
            eid = item.get("eventId") or item.get("event_id") or item.get("id")
            if eid: event_ids.append(str(eid))

total_events = len(event_ids)
print(f"2. Znaleziono {total_events} spotkań.")
if not total_events: exit()

mapowanie_sb = {}
if os.path.exists("slownik_druzyn.json"):
    try:
        with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
            slownik = json.load(f)
            map_be_to_sb = slownik.get("BetExplorer_To_Superbet", {})
            mapowanie_sb = {v.strip().lower(): k for k, v in map_be_to_sb.items() if v.strip() != ""}
    except Exception: pass

BATCH_SIZE = 10
batches = [event_ids[i:i + BATCH_SIZE] for i in range(0, len(event_ids), BATCH_SIZE)]

def fetch_batch_events(id_list):
    ids_param = ",".join(id_list)
    detail_url = f"https://production-superbet-offer-pl.freetls.fastly.net/v3/subscription/pl-PL/events?events={ids_param}"
    batch_rows = []
    try:
        with requests.get(detail_url, headers=headers, stream=True, timeout=10) as r:
            for line in r.iter_lines(decode_unicode=True):
                if line and line.startswith("data:"):
                    match_data = json.loads(line[5:].strip())
                    if isinstance(match_data, dict): match_data = match_data.get("data", [])
                    for m_ev in match_data:
                        fixture = m_ev.get("fixture", {}) if isinstance(m_ev, dict) else {}
                        event_id = str(m_ev.get("event_id") or fixture.get("event_id", ""))
                        match_name = m_ev.get("event_name") or fixture.get("event_name", "")
                        event_date = m_ev.get("event_date") or fixture.get("event_date", "")

                        if "·" in match_name: parts = match_name.split("·")
                        elif " - " in match_name: parts = match_name.split(" - ")
                        elif "-" in match_name: parts = match_name.split("-")
                        else: continue
                        
                        home_team, away_team = parts[0].strip(), parts[1].strip()
                        raw_markets = m_ev.get("markets", [])
                        market_list = list(raw_markets.values()) if isinstance(raw_markets, dict) else raw_markets

                        for m in market_list:
                            if not isinstance(m, dict): continue
                            market_name = m.get("name") or m.get("market_name", "")
                            raw_odds = m.get("odds", [])
                            odds_list = list(raw_odds.values()) if isinstance(raw_odds, dict) else raw_odds

                            for o in odds_list:
                                if not isinstance(o, dict): continue
                                meta = o.get("metadata", {}) if isinstance(o.get("metadata"), dict) else {}
                                odd_type = (meta.get("name") or o.get("name") or o.get("code") or o.get("outcome_name") or "")
                                full_info = (meta.get("info") or o.get("info") or o.get("special_bet_value") or "")
                                price_raw = o.get("price", 1.0)
                                price_str = str(price_raw).replace(".", ",")

                                batch_rows.append({
                                    "Event_ID": event_id, "Data": event_date, "Gospodarz": home_team, "Gosc": away_team,
                                    "Rynek": market_name, "Typ": odd_type, "Opis_Zdarzenia": full_info,
                                    "Kurs_Float": float(price_raw) if price_raw else 1.0, "Kurs": price_str
                                })
                    break
        return batch_rows
    except Exception:
        return []

all_rows = []
with ThreadPoolExecutor(max_workers=12) as executor:
    futures = [executor.submit(fetch_batch_events, b) for b in batches]
    for future in as_completed(futures):
        res = future.result()
        if res: all_rows.extend(res)

def extract_line_number(text):
    match = re.search(r'\d+(\.\d+)?', str(text))
    return match.group(0) if match else ""

if all_rows:
    df = pd.DataFrame(all_rows)
    df.drop(columns=["Kurs_Float"], errors='ignore').to_csv("superbet_baza_kursow.csv", sep=";", index=False, encoding="utf-8-sig")
    
    superbet_db = {}
    for row in all_rows:
        sb_h, sb_a = row["Gospodarz"].strip(), row["Gosc"].strip()
        be_h = mapowanie_sb.get(sb_h.lower(), sb_h).lower()
        be_a = mapowanie_sb.get(sb_a.lower(), sb_a).lower()
        key = f"{be_h}___{be_a}"
        
        if key not in superbet_db: superbet_db[key] = {}
        rynek = str(row["Rynek"]).lower()
        typ = str(row["Typ"]).strip()
        opis = str(row["Opis_Zdarzenia"]).strip()
        kurs = row["Kurs_Float"]
        if kurs <= 1.0: continue

        linia = extract_line_number(opis) or extract_line_number(typ)
        typ_lower = typ.lower()

        if rynek == "mecz" and typ in ["1", "X", "2"]:
            superbet_db[key][typ] = kurs
        elif rynek == "podwójna szansa" and typ in ["1X", "X2", "12"]:
            superbet_db[key][typ] = kurs
        elif "liczba goli" in rynek and "połowa" not in rynek and "drużyna" not in rynek and "handicap" not in rynek:
            if "poniżej" in typ_lower and linia: superbet_db[key][f"U{linia}"] = kurs
            elif "powyżej" in typ_lower and linia: superbet_db[key][f"O{linia}"] = kurs
        elif "1. połowa - liczba goli" in rynek:
            if "poniżej" in typ_lower and linia: superbet_db[key][f"HT_U{linia}"] = kurs
        elif "2. połowa - liczba goli" in rynek:
            if "poniżej" in typ_lower and linia: superbet_db[key][f"2H_U{linia}"] = kurs
        elif "liczba rzutów rożnych" in rynek and "drużyna" not in rynek:
            if "poniżej" in typ_lower and linia: superbet_db[key][f"C_U{linia}"] = kurs
            elif "powyżej" in typ_lower and linia: superbet_db[key][f"C_O{linia}"] = kurs
        elif "1. drużyna - liczba rzutów rożnych" in rynek:
            if "poniżej" in typ_lower and linia: superbet_db[key][f"HC_U{linia}"] = kurs
            elif "powyżej" in typ_lower and linia: superbet_db[key][f"HC_O{linia}"] = kurs
        elif "2. drużyna - liczba rzutów rożnych" in rynek:
            if "poniżej" in typ_lower and linia: superbet_db[key][f"AC_U{linia}"] = kurs
            elif "powyżej" in typ_lower and linia: superbet_db[key][f"AC_O{linia}"] = kurs
        elif "kto więcej strzałów" in rynek or rynek == "strzały w meczu - h2h":
            if typ == "1": superbet_db[key]["S_1"] = kurs
            elif typ == "2": superbet_db[key]["S_2"] = kurs
        elif "kto więcej celnych strzałów" in rynek or "celne strzały w meczu - h2h" in rynek:
            if typ == "1": superbet_db[key]["ST_1"] = kurs
            elif typ == "2": superbet_db[key]["ST_2"] = kurs
        elif "1. drużyna - liczba strzałów" in rynek and "powyżej" in typ_lower and linia:
            superbet_db[key][f"H_S_O{linia}"] = kurs
        elif "1. drużyna - liczba celnych strzałów" in rynek and "powyżej" in typ_lower and linia:
            superbet_db[key][f"H_ST_O{linia}"] = kurs
        elif "2. drużyna - liczba celnych strzałów" in rynek and "poniżej" in typ_lower and linia:
            superbet_db[key][f"A_ST_U{linia}"] = kurs

    with open("superbet_baza.json", "w", encoding="utf-8") as f:
        json.dump(superbet_db, f, ensure_ascii=False, indent=2)
    print("Zapisano superbet_baza.json pomyślnie.")
