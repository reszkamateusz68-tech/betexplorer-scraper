import os
import json
import requests
from datetime import datetime, timedelta, timezone
import concurrent.futures

def scrape_superbet():
    print("1. Pobieranie listy zdarzeń z Superbet API...")
    now = datetime.now(timezone.utc)
    start_iso = now.strftime("%Y-%m-%dT00:00:00.000Z")
    end_iso = (now + timedelta(days=3)).strftime("%Y-%m-%dT23:59:59.000Z")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    list_url = f"https://production-superbet-offer-pl.freetls.fastly.net/v3/pl-PL/events?startDate={start_iso}&endDate={end_iso}&index=active-prematch&sports=5"

    try:
        res = requests.get(list_url, headers=headers, timeout=12)
        if res.status_code != 200:
            print(f"Błąd pobierania listy: {res.status_code}")
            return
        data = res.json()
    except Exception as e:
        print(f"Błąd połączenia z Superbet: {e}")
        return

    raw_events = data.get("data", data.get("events", []))
    event_ids = []
    if isinstance(raw_events, list):
        for item in raw_events:
            if isinstance(item, (int, str)): event_ids.append(str(item))
            elif isinstance(item, dict):
                eid = item.get("eventId") or item.get("event_id") or item.get("id")
                if eid: event_ids.append(str(eid))

    if not event_ids:
        print("Brak aktywnych zdarzeń.")
        return

    mapowanie_sb = {}
    if os.path.exists("slownik_druzyn.json"):
        try:
            with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
                slownik = json.load(f)
                map_be_to_sb = slownik.get("BetExplorer_To_Superbet", {})
                mapowanie_sb = {v.strip().lower(): k for k, v in map_be_to_sb.items() if v.strip() != ""}
        except Exception: pass

    batch_size = 50
    batches = [event_ids[i:i + batch_size] for i in range(0, len(event_ids), batch_size)]
    superbet_db = {}

    def fetch_batch(batch_ids):
        url = f"https://production-superbet-offer-pl.freetls.fastly.net/v3/pl-PL/events?events={','.join(batch_ids)}"
        batch_res = {}
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                for m_ev in r.json().get("data", []):
                    fixture = m_ev.get("fixture", {}) if isinstance(m_ev, dict) else {}
                    ev_name = m_ev.get("event_name") or fixture.get("event_name", "")
                    
                    if "·" in ev_name: parts = ev_name.split("·", 1)
                    elif " - " in ev_name: parts = ev_name.split(" - ", 1)
                    else: continue

                    sb_h, sb_a = parts[0].strip().lower(), parts[1].strip().lower()
                    be_h = mapowanie_sb.get(sb_h, sb_h).lower()
                    be_a = mapowanie_sb.get(sb_a, sb_a).lower()

                    market_map = {}
                    raw_markets = m_ev.get("markets", [])
                    market_list = list(raw_markets.values()) if isinstance(raw_markets, dict) else raw_markets

                    for m in market_list:
                        if not isinstance(m, dict): continue
                        m_name = m.get("name", "").lower()
                        raw_odds = m.get("odds", [])
                        odds_list = list(raw_odds.values()) if isinstance(raw_odds, dict) else raw_odds

                        if m_name == "mecz":
                            for o in odds_list:
                                if o.get("name") in ["1", "X", "2"]: market_map[o["name"]] = float(o.get("price", 1.0))
                        elif m_name == "podwójna szansa":
                            for o in odds_list:
                                if o.get("name") in ["1X", "X2", "12"]: market_map[o["name"]] = float(o.get("price", 1.0))
                        elif "liczba goli" in m_name and "połowa" not in m_name and "drużyna" not in m_name and "handicap" not in m_name:
                            for o in odds_list:
                                meta = o.get("metadata", {}).get("info", "") or o.get("special_bet_value", "") or (o.get("specifiers", {}).get("total", "") if isinstance(o.get("specifiers"), dict) else "")
                                if "Poniżej" in o.get("name", ""): market_map[f"U{meta}"] = float(o.get("price", 1.0))
                                elif "Powyżej" in o.get("name", ""): market_map[f"O{meta}"] = float(o.get("price", 1.0))
                        elif "1. połowa - liczba goli" in m_name:
                            for o in odds_list:
                                meta = o.get("special_bet_value", "") or (o.get("specifiers", {}).get("total", "") if isinstance(o.get("specifiers"), dict) else "")
                                if "Poniżej" in o.get("name", ""): market_map[f"HT_U{meta}"] = float(o.get("price", 1.0))
                        elif "2. połowa - liczba goli" in m_name:
                            for o in odds_list:
                                meta = o.get("special_bet_value", "") or (o.get("specifiers", {}).get("total", "") if isinstance(o.get("specifiers"), dict) else "")
                                if "Poniżej" in o.get("name", ""): market_map[f"2H_U{meta}"] = float(o.get("price", 1.0))
                        elif "liczba rzutów rożnych" in m_name and "drużyna" not in m_name:
                            for o in odds_list:
                                meta = o.get("special_bet_value", "") or (o.get("specifiers", {}).get("total", "") if isinstance(o.get("specifiers"), dict) else "")
                                if "Poniżej" in o.get("name", ""): market_map[f"C_U{meta}"] = float(o.get("price", 1.0))
                                elif "Powyżej" in o.get("name", ""): market_map[f"C_O{meta}"] = float(o.get("price", 1.0))
                        elif "1. drużyna - liczba rzutów rożnych" in m_name:
                            for o in odds_list:
                                meta = o.get("special_bet_value", "") or (o.get("specifiers", {}).get("total", "") if isinstance(o.get("specifiers"), dict) else "")
                                if "Poniżej" in o.get("name", ""): market_map[f"HC_U{meta}"] = float(o.get("price", 1.0))
                        elif "2. drużyna - liczba rzutów rożnych" in m_name:
                            for o in odds_list:
                                meta = o.get("special_bet_value", "") or (o.get("specifiers", {}).get("total", "") if isinstance(o.get("specifiers"), dict) else "")
                                if "Poniżej" in o.get("name", ""): market_map[f"AC_U{meta}"] = float(o.get("price", 1.0))
                        elif "kto więcej celnych strzałów" in m_name or "celne strzały w meczu - h2h" in m_name:
                            for o in odds_list:
                                if o.get("name") == "1": market_map["ST_1"] = float(o.get("price", 1.0))
                                elif o.get("name") == "2": market_map["ST_2"] = float(o.get("price", 1.0))
                        elif "1. drużyna - liczba strzałów" in m_name:
                            for o in odds_list:
                                meta = o.get("special_bet_value", "") or (o.get("specifiers", {}).get("total", "") if isinstance(o.get("specifiers"), dict) else "")
                                if "Powyżej" in o.get("name", ""): market_map[f"H_S_O{meta}"] = float(o.get("price", 1.0))
                        elif "1. drużyna - liczba celnych strzałów" in m_name:
                            for o in odds_list:
                                meta = o.get("special_bet_value", "") or (o.get("specifiers", {}).get("total", "") if isinstance(o.get("specifiers"), dict) else "")
                                if "Powyżej" in o.get("name", ""): market_map[f"H_ST_O{meta}"] = float(o.get("price", 1.0))
                        elif "2. drużyna - liczba celnych strzałów" in m_name:
                            for o in odds_list:
                                meta = o.get("special_bet_value", "") or (o.get("specifiers", {}).get("total", "") if isinstance(o.get("specifiers"), dict) else "")
                                if "Poniżej" in o.get("name", ""): market_map[f"A_ST_U{meta}"] = float(o.get("price", 1.0))

                    if market_map:
                        key = f"{be_h}___{be_a}"
                        batch_res[key] = market_map
        except Exception: pass
        return batch_res

    print(f"2. Pobieranie szczegółów dla {len(event_ids)} spotkań ({len(batches)} paczek)...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as exe:
        for res_batch in exe.map(fetch_batch, batches):
            superbet_db.update(res_batch)

    with open("superbet_baza.json", "w", encoding="utf-8") as f:
        json.dump(superbet_db, f, ensure_ascii=False, indent=2)

    print(f"Sukces: Zapisano rynki dla {len(superbet_db)} meczów do superbet_baza.json.")

if __name__ == "__main__":
    scrape_superbet()
