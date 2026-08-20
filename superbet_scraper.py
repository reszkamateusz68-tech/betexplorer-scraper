# superbet_scraper.py
import datetime
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pandas as pd

def fetch_superbet_odds(days_ahead=1, max_workers=12):
    now = datetime.datetime.now(datetime.timezone.utc)
    start_iso = now.strftime("%Y-%m-%dT00:00:00.000Z")
    end_iso = (now + datetime.timedelta(days=days_ahead)).strftime("%Y-%m-%dT23:59:59.000Z")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.superbet.pl/",
        "Origin": "https://www.superbet.pl"
    }

    base_url = "https://production-superbet-offer-pl.freetls.fastly.net/v3/pl-PL/events"
    params = {
        "startDate": start_iso,
        "endDate": end_iso,
        "index": "active-prematch",
        "sports": "5"
    }

    session = requests.Session()
    res = session.get(base_url, headers=headers, params=params, timeout=12)
    if res.status_code != 200:
        print(f"[Superbet] Błąd kalendarza: {res.status_code}")
        return pd.DataFrame()

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

    print(f"[Superbet] Pobieram kursy dla {len(event_ids)} spotkań...")

    def fetch_single(event_id):
        url = f"https://production-superbet-offer-pl.freetls.fastly.net/v3/subscription/pl-PL/events?events={event_id}"
        rows = []
        try:
            with requests.get(url, headers=headers, stream=True, timeout=8) as r:
                for line in r.iter_lines(decode_unicode=True):
                    if line and line.startswith("data:"):
                        m_data = json.loads(line[5:].strip())
                        if isinstance(m_data, dict):
                            m_data = m_data.get("data", [])
                        for m_ev in m_data:
                            match_name = m_ev.get("event_name") or m_ev.get("fixture", {}).get("event_name", "")
                            raw_markets = m_ev.get("markets", [])
                            markets = list(raw_markets.values()) if isinstance(raw_markets, dict) else raw_markets
                            for m in markets:
                                if not isinstance(m, dict): continue
                                m_name = m.get("name") or m.get("market_name", "")
                                raw_odds = m.get("odds", [])
                                odds = list(raw_odds.values()) if isinstance(raw_odds, dict) else raw_odds
                                for o in odds:
                                    if not isinstance(o, dict): continue
                                    meta = o.get("metadata", {}) if isinstance(o.get("metadata"), dict) else {}
                                    odd_type = meta.get("name") or o.get("name") or ""
                                    price = o.get("price")
                                    if price:
                                        rows.append({
                                            "Event_ID": event_id,
                                            "Data": m_ev.get("event_date", ""),
                                            "Mecz": match_name,
                                            "Rynek": m_name,
                                            "Typ": odd_type,
                                            "Kurs": float(price)
                                        })
                        break
            return rows
        except Exception:
            return []

    all_rows = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_single, eid) for eid in event_ids]
        for f in as_completed(futures):
            all_rows.extend(f.result())

    df = pd.DataFrame(all_rows)
    print(f"[Superbet] Pobrano {len(df)} kursów.")
    return df
