import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import os
import json

def fetch_all_opta_results():
    """Wczytuje bazę meczów bezpośrednio z lokalnego pliku tekstowego w repozytorium."""
    file_path = "opta_raw.txt"
    if not os.path.exists(file_path):
        print(f"BŁĄD: Nie znaleziono pliku {file_path} w repozytorium!")
        return None
        
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw_text = f.read().strip()
            
        start_idx = raw_text.find("(")
        end_idx = raw_text.rfind(")")
        
        if start_idx != -1 and end_idx != -1:
            json_string = raw_text[start_idx + 1:end_idx]
            return json.loads(json_string)
        else:
            return json.loads(raw_text)
    except Exception as e:
        print(f"Błąd podczas czytania pliku opta_raw.txt: {e}")
        return None

def parse_all_matches(json_data):
    """Przetwarza całą strukturę i wyciąga statystyki meczowe oraz zaawansowane parametry drużyn."""
    if not json_data or 'match' not in json_data:
        print("Brak danych meczowych w strukturze JSON.")
        return []
        
    all_parsed_matches = []
    
    for match_node in json_data['match']:
        match_info = match_node.get('matchInfo', {})
        live_data = match_node.get('liveData', {})
        match_details = live_data.get('matchDetails', {})
        
        if match_details.get('matchStatus') != 'Played':
            continue
            
        match_id = match_info.get('id')
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
                
        # Podstawowe statystyki kartek i zmian
        cards = live_data.get('card', [])
        home_yellows = sum(1 for c in cards if c.get('type') == 'YC' and c.get('contestantId') == home_id)
        away_yellows = sum(1 for c in cards if c.get('type') == 'YC' and c.get('contestantId') == away_id)
        home_reds = sum(1 for c in cards if c.get('type') in ['RC', 'Y2C'] and c.get('contestantId') == home_id)
        away_reds = sum(1 for c in cards if c.get('type') in ['RC', 'Y2C'] and c.get('contestantId') == away_id)
        
        subs = live_data.get('substitute', [])
        home_subs = sum(1 for s in subs if s.get('contestantId') == home_id)
        away_subs = sum(1 for s in subs if s.get('contestantId') == away_id)
        
        var_events = len(live_data.get('VAR', []))
        
        # ==============================================================================
        # EKSTRAKCJA ZAAWANSOWANYCH STATYSTYK DRUŻYNOWYCH (Strzały, rożne, posiadanie, faule)
        # ==============================================================================
        # Inicjalizacja domyślnych wartości na wypadek braku danych w pliku surowym
        possession_h, possession_a = "-", "-"
        shots_h, shots_a = "-", "-"
        sot_h, sot_a = "-", "-"
        corners_h, corners_a = "-", "-"
        fouls_h, fouls_a = "-", "-"
        xg_h, xg_a = "-", "-"
        
        # Szukamy sekcji 'lineUp' lub 'teamStats', w której Opta zapisuje podsumowania statystyczne
        lineups = live_data.get('lineUp', [])
        for lineup in lineups:
            c_id = lineup.get('contestantId')
            stats = lineup.get('teamStats', {})
            
            # Pobieramy parametry słownikowe statystyk
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
            "Match_ID": match_id,
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
            # Nowe zaawansowane kolumny:
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
        all_parsed_matches.append(parsed_row)
        
    return all_parsed_matches

def save_to_google_sheets(parsed_data):
    """Zapisuje przefiltrowane dane do Google Sheets."""
    if not parsed_data:
        print("Brak nowych danych do zapisania.")
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
    print(f"SUKCES: Pomyślnie zsynchronizowano {len(df)} rozegranych meczów wraz Z ZAAWANSOWANYMI STATYSTYKAMI do Google Sheets!")

if __name__ == "__main__":
    print("Wczytywanie lokalnej bazy danych Opta...")
    raw_json = fetch_all_opta_results()
    if raw_json:
        print("Przetwarzanie danych...")
        clean_data = parse_all_matches(raw_json)
        print("Zapis do Google Sheets...")
        save_to_google_sheets(clean_data)
