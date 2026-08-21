import os
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

# ==========================================================
# 1. POBIERANIE KALENDARZA (DYNAMICZNE DATY)
# ==========================================================
base_url = "https://production-superbet-offer-pl.freetls.fastly.net/v3/pl-PL/events"

now = datetime.now(timezone.utc)
start_iso = now.strftime("%Y-%m-%dT00:00:00.000Z")
end_iso = (now + timedelta(days=2)).strftime("%Y-%m-%dT23:59:59.000Z")

params = {
    "startDate": start_iso,
    "endDate": end_iso,
    "index": "active-prematch",
    "sports": "5"  # 5 = Piłka nożna
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.superbet.pl/",
    "Origin": "https://www.superbet.pl"
}

print("1. Pobieram kalendarz wszystkich meczów piłkarskich z Superbet...")
start_time = time.time()
session = requests.Session()

try:
    res = session.get(base_url, headers=headers, params=params, timeout=12)
    if res.status_code != 200:
        print(f"Błąd pobierania listy: {res.status_code}")
        exit()
    raw_data = res.json()
except Exception as e:
    print(f"Błąd połączenia: {e}")
    exit()

# Wyciągnięcie wszystkich ID spotkań
event_ids = []
if isinstance(raw_data, list):
    event_ids = [str(x) for x in raw_data if isinstance(x, (int, str))]
elif isinstance(raw_data, dict):
    d = raw_data.get("data", raw_data.get("events", []))
    if isinstance(d, list):
        for item in d:
            if isinstance(item, (int, str)):
                event_ids.append(str(item))
            elif isinstance(item, dict):
                eid = item.get("eventId") or item.get("event_id") or item.get("id")
                if eid:
                    event_ids.append(str(eid))

total_events = len(event_ids)
print(f"2. Znaleziono łącznie {total_events} meczów w pełnej ofercie.")

if not total_events:
    print("Brak ID do pobrania.")
    exit()

# Wczytanie słownika do zunifikowania nazw drużyn
mapowanie_sb = {}
if os.path.exists("slownik_druzyn.json"):
    try:
        with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
            slownik = json.load(f)
            map_be_to_sb = slownik.get("BetExplorer_To_Superbet", {})
            mapowanie_sb = {v.strip().lower(): k for k, v in map_be_to_sb.items() if v.strip() != ""}
    except Exception: pass

print("3. Uruchamiam szybkie pobieranie wielowątkowe (12 wątków)...")

def fetch_single_event(event_id):
    """Pobiera i parsuje pojedynczy mecz z podziałem na gospodarza i gościa."""
    detail_url = f"https://production-superbet-offer-pl.freetls.fastly.net/v3/subscription/pl-PL/events?events={event_id}"
    match_rows = []
    match_name = f"Mecz ID {event_id}"
    
    try:
        with requests.get(detail_url, headers=headers, stream=True, timeout=10) as r:
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
                            price_str = str(price_raw).replace(".", ",") if isinstance(price_raw, (int, float)) else str(price_raw)

                            match_rows.append({
                                "Event_ID": event_id,
                                "Data": event_date,
                                "Gospodarz": home_team,
                                "Gosc": away_team,
                                "Rynek": market_name,
                                "Typ": odd_type,
                                "Opis_Zdarzenia": full_info,
                                "Kurs_Float": float(price_raw), # Wartość liczbowa dla JSON
                                "Kurs": price_str               # Wartość tekstowa dla CSV
                            })
        return match_rows, match_name, None
    except Exception as e:
        return [], match_name, str(e)

all_rows = []
completed_count = 0

with ThreadPoolExecutor(max_workers=12) as executor:
    futures = {executor.submit(fetch_single_event, eid): eid for eid in event_ids}
    for future in as_completed(futures):
        completed_count += 1
        eid = futures[future]
        rows, m_name, error = future.result()
        
        if rows:
            all_rows.extend(rows)
            print(f"[{completed_count:03d}/{total_events}] Pobrano: {m_name}")
        else:
            print(f"[{completed_count:03d}/{total_events}] Pominięto/Błąd ID {eid}: {error}")

elapsed = round(time.time() - start_time, 2)

if all_rows:
    df = pd.DataFrame(all_rows)
    
    # 1. Zapis klasycznego CSV w folderze głównym akcji
    df_csv = df.drop(columns=["Kurs_Float"])
    csv_file = "superbet_baza_kursow.csv"
    df_csv.to_csv(csv_file, sep=";", index=False, encoding="utf-8-sig")
    
    # ==========================================================
    # 4. GENEROWANIE PLIKU JSON DLA SILNIKA GŁÓWNEGO
    # ==========================================================
    print("4. Budowanie zoptymalizowanej bazy JSON dla silnika analitycznego...")
    superbet_db = {}
    
    for row in all_rows:
        sb_h = row["Gospodarz"].lower()
        sb_a = row["Gosc"].lower()
        be_h = mapowanie_sb.get(sb_h, sb_h)
        be_a = mapowanie_sb.get(sb_a, sb_a)
        key = f"{be_h}___{be_a}"
        
        if key not in superbet_db:
            superbet_db[key] = {}
            
        rynek = str(row["Rynek"]).lower()
        typ = str(row["Typ"])
        opis = str(row["Opis_Zdarzenia"])
        kurs = row["Kurs_Float"]
        
        if kurs <= 1.0: continue

        # Mapowanie na kody silnika
        if rynek == "mecz" and typ in ["1", "X", "2"]:
            superbet_db[key][typ] = kurs
        elif rynek == "podwójna szansa" and typ in ["1X", "X2", "12"]:
            superbet_db[key][typ] = kurs
        elif "liczba goli" in rynek and "połowa" not in rynek and "drużyna" not in rynek:
            if "poniżej" in typ.lower(): superbet_db[key][f"U{opis.strip()}"] = kurs
            elif "powyżej" in typ.lower(): superbet_db[key][f"O{opis.strip()}"] = kurs
        elif "1. połowa - liczba goli" in rynek:
            if "poniżej" in typ.lower(): superbet_db[key][f"HT_U{opis.strip()}"] = kurs
        elif "2. połowa - liczba goli" in rynek:
            if "poniżej" in typ.lower(): superbet_db[key][f"2H_U{opis.strip()}"] = kurs
        elif "liczba rzutów rożnych" in rynek and "drużyna" not in rynek:
            if "poniżej" in typ.lower(): superbet_db[key][f"C_U{opis.strip()}"] = kurs
            elif "powyżej" in typ.lower(): superbet_db[key][f"C_O{opis.strip()}"] = kurs
        elif "1. drużyna - liczba rzutów rożnych" in rynek:
            if "poniżej" in typ.lower(): superbet_db[key][f"HC_U{opis.strip()}"] = kurs
            elif "powyżej" in typ.lower(): superbet_db[key][f"HC_O{opis.strip()}"] = kurs
        elif "2. drużyna - liczba rzutów rożnych" in rynek:
            if "poniżej" in typ.lower(): superbet_db[key][f"AC_U{opis.strip()}"] = kurs
            elif "powyżej" in typ.lower(): superbet_db[key][f"AC_O{opis.strip()}"] = kurs
        elif "kto więcej strzałów w meczu" in rynek or rynek == "strzały w meczu - h2h":
            if typ == "1": superbet_db[key]["S_1"] = kurs
            elif typ == "2": superbet_db[key]["S_2"] = kurs
        elif "kto więcej celnych strzałów" in rynek or "celne strzały w meczu - h2h" in rynek:
            if typ == "1": superbet_db[key]["ST_1"] = kurs
            elif typ == "2": superbet_db[key]["ST_2"] = kurs
        elif "1. drużyna - liczba strzałów" in rynek and "powyżej" in typ.lower():
            superbet_db[key][f"H_S_O{opis.strip()}"] = kurs
        elif "1. drużyna - liczba celnych strzałów" in rynek and "powyżej" in typ.lower():
            superbet_db[key][f"H_ST_O{opis.strip()}"] = kurs
        elif "2. drużyna - liczba celnych strzałów" in rynek and "poniżej" in typ.lower():
            superbet_db[key][f"A_ST_U{opis.strip()}"] = kurs

    json_file = "superbet_baza.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(superbet_db, f, ensure_ascii=False, indent=2)

    print(f"\n=======================================================")
    print(f"SUKCES! Pomyślnie pobrano {len(all_rows)} kursów dla {total_events} meczów.")
    print(f"Całkowity czas wykonania: {elapsed} s (~{round(elapsed/60, 1)} min).")
    print(f"Plik CSV: {csv_file}")
    print(f"Plik JSON (dla silnika): {json_file} - spakowano {len(superbet_db)} unikalnych zdarzeń.")
    print(f"=======================================================")
