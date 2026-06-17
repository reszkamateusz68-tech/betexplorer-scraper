import requests
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import os
import json

# Konfiguracja API Opta na podstawie przesłanego tokenu i sezonu
OPTA_UUID = "ft1tiv1inq7v1sk3y9tv12yh5"
SEASON_ID = "51r6ph2woavlbbpk8f29nynf8"

def fetch_all_opta_results():
    """Pobiera pełny plik ze wszystkimi meczami sezonu w czystym formacie JSON."""
    # Zmieniamy parametry na czysty JSON (_fmt=json) i usuwamy callback JavaScript
    url = f"https://api.performfeeds.com/soccerdata/match/{OPTA_UUID}?_rt=c&live=yes&_lcl=en&_fmt=json&sps=widgets&tournamentCalendarId={SEASON_ID}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Origin": "https://optaplayerstats.statsperform.com",
        "Referer": "https://optaplayerstats.statsperform.com/"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=45)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Błąd pobierania danych z Opta API: Status {response.status_code}")
            return None
    except Exception as e:
        print(f"Błąd sieciowy podczas połączenia z Opta API: {e}")
        return None

def parse_all_matches(json_data):
    """Przetwarza całą strukturę JSON i wyciąga statystyki mecz po meczu."""
    if not json_data or 'match' not in json_data:
        print("Brak danych meczowych w strukturze JSON.")
        return []
        
    all_parsed_matches = []
    
    for match_node in json_data['match']:
        match_info = match_node.get('matchInfo', {})
        live_data = match_node.get('liveData', {})
        match_details = live_data.get('matchDetails', {})
        
        # Ignorujemy mecze, które się jeszcze nie odbyły (brak statusu 'Played')
        if match_details.get('matchStatus') != 'Played':
            continue
            
        match_id = match_info.get('id')
        date = match_info.get('localDate', '')
        week = match_info.get('week', '')
        
        # Identyfikacja drużyn i ich ID
        home_team, home_id = "", ""
        away_team, away_id = "", ""
        for contestant in match_info.get('contestant', []):
            if contestant.get('position') == 'home':
                home_team = contestant.get('name')
                home_id = contestant.get('id')
            elif contestant.get('position') == 'away':
                away_team = contestant.get('name')
                away_id = contestant.get('id')
                
        # Gole i wyniki (Do przerwy i Koncowe)
        scores = match_details.get('scores', {})
        home_score_ft = scores.get('ft', {}).get('home', 0)
        away_score_ft = scores.get('ft', {}).get('away', 0)
        home_score_ht = scores.get('ht', {}).get('home', 0)
        away_score_ht = scores.get('ht', {}).get('away', 0)
        
        # Frekwencja i Sędzia Główny
        extra = live_data.get('matchDetailsExtra', {})
        attendance = extra.get('attendance', '0')
        
        referee = "Nieznany"
        for official in extra.get('matchOfficial', []):
            if official.get('type') == 'Main':
                referee = f"{official.get('firstName', '')} {official.get('lastName', '')}".strip()
                
        # Agregacja kartek z podziałem na drużyny
        cards = live_data.get('card', [])
        home_yellows = sum(1 for c in cards if c.get('type') == 'YC' and c.get('contestantId') == home_id)
        away_yellows = sum(1 for c in cards if c.get('type') == 'YC' and c.get('contestantId') == away_id)
        home_reds = sum(1 for c in cards if c.get('type') in ['RC', 'Y2C'] and c.get('contestantId') == home_id)
        away_reds = sum(1 for c in cards if c.get('type') in ['RC', 'Y2C'] and c.get('contestantId') == away_id)
        
        # Zliczanie zmian (Substitutions) - przydatne do analizy intensywności meczu
        subs = live_data.get('substitute', [])
        home_subs = sum(1 for s in subs if s.get('contestantId') == home_id)
        away_subs = sum(1 for s in subs if s.get('contestantId') == away_id)
        
        # Interwencje VAR (ile razy anulowano lub zmieniono decyzję na korzyść/niekorzyść)
        var_events = len(live_data.get('VAR', []))
        
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
            "Sedzia": referee
        }
        all_parsed_matches.append(parsed_row)
        
    return all_parsed_matches

def save_to_google_sheets(parsed_data):
    """Zapisuje przefiltrowane dane do Google Sheets do arkusza Opta_Results."""
    if not parsed_data:
        print("Brak nowych danych do zapisania.")
        return
        
    df = pd.DataFrame(parsed_data)
    
    # Zamiana kropek na przecinki w liczbach (pod polskie ustawienia arkusza, jeśli wymagane)
    # Dla tych danych liczbowych zachowujemy czysty format int/str.
    
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    if os.path.exists("credentials.json"):
        creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
    else:
        creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
        
    client = gspread.authorize(creds)
    spreadsheet = client.open("BetExplorer") # Otwieramy Twój główny plik bota
    
    try:
        sheet = spreadsheet.worksheet("Opta_Results")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Opta_Results", rows=2000, cols=20)
        
    # Nadpisujemy lub aktualizujemy arkusz nowym zestawem danych
    sheet.clear()
    sheet.update([df.columns.tolist()] + df.values.tolist())
    print(f"Pomyślnie zsynchronizowano {len(df)} rozegranych meczów z Opta Stats do Google Sheets!")

if __name__ == "__main__":
    print("Pobieranie bazy danych Opta...")
    raw_json = fetch_all_opta_results()
    if raw_json:
        print("Przetwarzanie danych...")
        clean_data = parse_all_matches(raw_json)
        print("Zapis do Google Sheets...")
        save_to_google_sheets(clean_data)