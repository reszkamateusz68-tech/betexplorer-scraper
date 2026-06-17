import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import os
import json
import re
import time

OPTA_UUID = "ft1tiv1inq7v1sk3y9tv12yh5"
CALLBACK_KEY = "W3754ce3eb8ab7e2434613a6cb279a2fa7c2a72eb7"

def load_match_ids_from_file():
    """Wyciąga unikalne ID meczów z pliku matches.txt."""
    file_path = "matches.txt"
    if not os.path.exists(file_path):
        print(f"BŁĄD: Brak pliku {file_path} w repozytorium!")
        return []
        
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    match_ids = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Wyciągamy ostatni element po ukośniku (ID meczu)
        match_id = line.split("/")[-1]
        if match_id:
            match_ids.append(match_id)
            
    print(f"Zgromadzono {len(match_ids)} unikalnych identyfikatorów meczowych do przetworzenia.")
    return match_ids

def fetch_detailed_stats_from_api(match_id):
    """Pobiera zaawansowane statystyki bezpośrednio z API Opty dla podanego ID."""
    url = f"https://api.performfeeds.com/soccerdata/match/{OPTA_UUID}?_rt=c&live=yes&_lcl=en&_fmt=jsonp&sps=widgets&matchId={match_id}&_clbk={CALLBACK_KEY}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://optaplayerstats.statsperform.com",
        "Referer": "https://optaplayerstats.statsperform.com/"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            raw_text = response.text.strip()
            start_idx = raw_text.find("(")
            end_idx = raw_text.rfind(")")
            if start_idx != -1 and end_idx != -1:
                json_string = raw_text[start_idx + 1:end_idx]
                return json.loads(json_string)
    except Exception as e:
        print(f"Pomijam ID {match_id} z powodu błędu sieci: {e}")
    return None

def process_automated_scraper():
    """Główny proces pobierający, parsujący i łączący dane dla wszystkich meczów."""
    match_ids = load_match_ids_from_file()
    if not match_ids:
        return
        
    all_parsed_rows = []
    
    for idx, m_id in enumerate(match_ids, start=1):
        print(f"[{idx}/{len(match_ids)}] Pobieram szczegóły meczu: {m_id}")
        
        # Pobranie paczki danych per mecz
        data = fetch_detailed_stats_from_api(m_id)
        if not data or 'match' not in data:
            continue
            
        for match_node in data['match']:
            match_info = match_node.get('matchInfo', {})
            live_data = match_node.get('liveData', {})
            match_details = live_data.get('matchDetails', {})
            
            if match_details.get('matchStatus') != 'Played':
                continue
                
            date = match_info.get('localDate', '')
            week = match_info.get('week', '')
            
            home_team, home_id = "", ""
            away_team, away_id = "", ""
            for contestant in match_info.get('contestant', []):
                if contestant.get('position') == 'home':
                    home_team = contestant.get('name')
                    home_id = contestant.get('id')
                elif contestant.get('position') == 'away':
                    away_team = contestant.get('name')
                    away_id = contestant.get('id')
                    
            scores = match_details.get('scores', {})
            home_score_ft = scores.get('ft', {}).get('home', 0)
            away_score_ft = scores.get('ft', {}).get('away', 0)
            home_score_ht = scores.get('ht', {}).get('home', 0)
            away_score_ht = scores.get('ht', {}).get('away', 0)
            
            extra = live_data.get('matchDetailsExtra', {})
            attendance = extra.get('attendance', '0')
            
            referee = "Nieznany"
            for official in extra.get('matchOfficial', []):
                if official.get('type') == 'Main':
                    referee = f"{official.get('firstName', '')} {official.get('lastName', '')}".strip()
                    
            cards = live_data.get('card', [])
            home_yellows = sum(1 for c in cards if c.get('type') == 'YC' and c.get('contestantId') == home_id)
            away_yellows = sum(1 for c in cards if c.get('type') == 'YC' and c.get('contestantId') == away_id)
            home_reds = sum(1 for c in cards if c.get('type') in ['RC', 'Y2C'] and c.get('contestantId') == home_id)
            away_reds = sum(1 for c in cards if c.get('type') in ['RC', 'Y2C'] and c.get('contestantId') == away_id)
            
            subs = live_data.get('substitute', [])
            home_subs = sum(1 for s in subs if s.get('contestantId') == home_id)
            away_subs = sum(1 for s in subs if s.get('contestantId') == away_id)
            
            var_events = len(live_data.get('VAR', []))
            
            # Ekstrakcja zaawansowanych statystyk meczowych
            possession_h, possession_a = "-", "-"
            shots_h, shots_a = "-", "-"
            sot_h, sot_a = "-", "-"
            corners_h, corners_a = "-", "-"
            fouls_h, fouls_a = "-", "-"
            xg_h, xg_a = "-", "-"
            
            lineups = live_data.get('lineUp', [])
            for lineup in lineups:
                c_id = lineup.get('contestantId')
                stats = lineup.get('teamStats', {})
                
                possession = stats.get('possessionPercentage', "-")
                total_shots = stats.get('totalShots', "-")
                shots_on_target = stats.get('shotsOnTarget', "-")
                corners = stats.get('cornerKicks', "-")
                fouls = stats.get('foulsCommited', stats.get('fouls', "-"))
                expected_goals = stats.get('expectedGoals', stats.get('xg', "-"))
                
                if c_id == home_id:
                    possession_h = possession
                    shots_h = total_shots
                    sot_h = shots_on_target
                    corners_h = corners
                    fouls_h = fouls
                    xg_h = expected_goals
                elif c_id == away_id:
                    possession_a = possession
                    shots_a = total_shots
                    sot_a = shots_on_target
                    corners_a = corners
                    fouls_a = fouls
                    xg_a = expected_goals

            parsed_row = {
                "Match_ID": m_id,
                "Kolejka": week,
                "Data": date,
                "Gospodarz": home_team,
                "Gosc": away_team,
                "Gole_Gospodarz": home_score_ft,
                "Gole_Gosc": away_score_ft,
                "Gole_Gosp_HT": home_score_ht,
                "Gole_Gosc_HT": away_score_ht,
                "Zolte_Gospodarz": home_yellows,
                "Zolte_Gosc": away_yellows,
                "Czerwone_Gospodarz": home_reds,
                "Czerwone_Gosc": away_reds,
                "Zmiany_Gospodarz": home_subs,
                "Zmiany_Gosc": away_subs,
                "Interwencje_VAR": var_events,
                "Widzow": attendance,
                "Sedzia": referee,
                "Posiadanie_Gosp_%": possession_h,
                "Posiadanie_Gosc_%": possession_a,
                "Strzaly_Gospodarz": shots_h,
                "Strzaly_Gosc": shots_a,
                "Celne_Gospodarz": sot_h,
                "Celne_Gosc": sot_a,
                "Rozne_Gospodarz": corners_h,
                "Rozne_Gosc": corners_a,
                "Faule_Gospodarz": fouls_h,
                "Faule_Gosc": fouls_a,
                "xG_Gospodarz": xg_h,
                "xG_Gosc": xg_a
            }
            all_parsed_rows.append(parsed_row)
            
        # Zabezpieczenie przed przeciążeniem serwera Opta (0.5 sekundy przerwy między zapytaniami)
        time.sleep(0.5)
        
    return all_parsed_rows

def save_to_google_sheets(parsed_data):
    """Wstrzykuje komplet danych do arkusza Google."""
    if not parsed_data:
        print("Brak danych do wyeksportowania.")
        return
        
    df = pd.DataFrame(parsed_data)
    
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    if os.path.exists("credentials.json"):
        creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
    else:
        creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
        
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_url("https://docs.google.com/spreadsheets/d/11yc_BrZA649aZgeJhLedETqg6NI1k1_QFje7WNEjIHk/edit")
    
    try:
        sheet = spreadsheet.worksheet("Opta_Results")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Opta_Results", rows=2000, cols=35)
        
    sheet.clear()
    sheet.update(([df.columns.tolist()] + df.values.tolist()), "A1")
    print(f"SUKCES: Wszystkie kolumny zostały automatycznie uzupełnione dla {len(df)} meczów!")

if __name__ == "__main__":
    import requests
    print("Inicjalizacja w pełni automatycznego pobierania meczów po ID...")
    clean_data = process_automated_scraper()
    print("Zapisywanie kompletnych danych do Google Sheets...")
    save_to_google_sheets(clean_data)
