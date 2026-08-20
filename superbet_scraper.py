import datetime
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pandas as pd

# 1. Pobranie kalendarza (dziś i jutro)
now = datetime.datetime.now(datetime.timezone.utc)
start_iso = now.strftime("%Y-%m-%dT00:00:00.000Z")
end_iso = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%dT23:59:59.000Z")

base_url = "https://production-superbet-offer-pl.freetls.fastly.net/v3/pl-PL/events"
params = {
    "startDate": start_iso,
    "endDate": end_iso,
    "index": "active-prematch",
    "sports": "5"  # 5 = Piłka nożna w Superbet
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.superbet.pl/",
    "Origin": "https://www.superbet.pl"
}

print("1. Pobieram kalendarz meczów piłkarskich z Superbet...")
start_time = time.time()
session = requests.Session()

res = session.get(base_url, headers=headers, params=params, timeout=12)
if res.status_code != 200:
    print(f"Błąd pobierania kalendarza: {res.status_code}")
    exit()

raw_data = res.json()
event_ids = []
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
print(f"2. Znaleziono {total_events} meczów piłkarskich. Pobieram kursy (12 wątków)...")

def fetch_single_event(event_id):
    detail_url = f"https://production-superbet-offer-pl.freetls.fastly.net/v3/subscription/pl-PL/events?events={event_id}"
    match_rows = []
    match_name = f"Mecz ID {event_id}"
    
    try:
        with requests.get(detail_url, headers=headers, stream=True, timeout=8) as r:
            for line in r.iter_lines(decode_unicode=True):
                if line and line.startswith("data:"):
                    match_data = json.loads(line[5:].strip())
                    if isinstance(match_data, dict):
                        match_data = match_data.get("data", [])

                    for m_ev in match_data:
                        fixture = m_ev.get("fixture", {}) if isinstance(m_ev, dict) else {}
                        match_name = m_ev.get("event_name") or fixture.get("event_name", match_name)
                        event_date = m_ev.get("event_date") or fixture.get("event_date", "")

                        # Rozbicie meczu na gospodarza i gościa
                        if "·" in match_name:
                            parts = match_name.split("·")
                            home_team, away_team = parts[0].strip(), parts[1].strip()
                        elif " - " in match_name:
                            parts = match_name.split(" - ")
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

                                odd_type = meta.get("name") or o.get("name") or o.get("code") or ""
                                full_info = meta.get("info") or o.get("info") or o.get("special_bet_value") or ""
                                price = str(o.get("price", "")).replace(".", ",")

                                match_rows.append({
                                    "Event_ID": event_id,
                                    "Data": event_date,
                                    "Gospodarz": home_team,
                                    "Gosc": away_team,
                                    "Mecz": match_name,
                                    "Rynek": market_name,
                                    "Typ": odd_type,
                                    "Opis_Zdarzenia": full_info,
                                    "Kurs": price
                                })
                    break
        return match_rows, match_name, None
    except Exception as e:
        return [], match_name, str(e)

all_rows = []
completed_count = 0

with ThreadPoolExecutor(max_workers=12) as executor:
    futures = {executor.submit(fetch_single_event, eid): eid for eid in event_ids}
    for future in as_completed(futures):
        completed_count += 1
        rows, m_name, err = future.result()
        if rows:
            all_rows.extend(rows)
            print(f"[{completed_count:03d}/{total_events}] Pobrano: {m_name}")

elapsed = round(time.time() - start_time, 2)

if all_rows:
    df = pd.DataFrame(all_rows)
    file_name = "superbet_baza_kursow.csv"
    df.to_csv(file_name, sep=";", index=False, encoding="utf-8-sig")
    print(f"\nSUKCES! Zapisano {len(all_rows)} kursów dla {total_events} spotkań w {elapsed} s.")
