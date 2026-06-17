import requests
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import os
import json

# Konfiguracja API Opta na podstawie oficjalnego widgetu ligowego
OPTA_UUID = "ft1tiv1inq7v1sk3y9tv12yh5"
SEASON_ID = "51r6ph2woavlbbpk8f29nynf8"

def fetch_all_opta_results():
    """
    Pobiera pełną bazę meczów. Sprawdza dwa alternatywne adresy URL (JSON oraz JSONP),
    aby wyeliminować błędy 400 i błędy pustej struktury danych.
    """
    # Wariant 1: Oficjalne żądanie JSONP z kompletnym identyfikatorem widżetu strony głównej
    url_jsonp = f"https://api.performfeeds.com/soccerdata/match/{OPTA_UUID}?_rt=c&live=yes&_lcl=en&_fmt=jsonp&sps=widgets&tournamentCalendarId={SEASON_ID}&_clbk=W3754ce3eb8ab7e2434613a6cb279a2fa7c2a72eb7"
    
    # Wariant 2: Żądanie czystego formatu JSON używane jako fallback
    url_json = f"https://api.performfeeds.com/soccerdata/match/{OPTA_UUID}?_rt=c&live=yes&_lcl=en&_fmt=json&sps=widgets&tournamentCalendarId={SEASON_ID}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://optaplayerstats.statsperform.com",
        "Referer": "https://optaplayerstats.statsperform.com/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    
    # Najpierw próbujemy pobrać oficjalny format ze strony (JSONP)
    try:
        print("Próba pobrania danych przez główny kanał JSONP...")
        response = requests.get(url_jsonp, headers=headers, timeout=30)
        if response.status_code == 200 and "(" in response.text:
            raw_text = response.text.strip()
            start_idx = raw_text.find("(")
            end_idx = raw_text.rfind(")")
            if start_idx != -1 and end_idx != -1:
                json_string = raw_text[start_idx + 1:end_idx]
                data = json.loads(json_string)
                if 'match' in data:
                    return data
    except Exception as e:
        print(f"Główny kanał niedostępny: {e}")

    # Jeśli JSONP zawiedzie lub struktura będzie pusta, automatycznie odpala się Fallback (Czysty JSON)
    try:
        print("Kanał główny pusty. Uruchamianie alternatywnego pobierania JSON...")
        response = requests.get(url_json, headers=headers, timeout=30)
        if response.status_code == 200:
            data = response.json()
            if 'match' in data:
                return data
    except Exception as e:
        print(f"Błąd alternatywnego pobierania: {e}")
        
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
        
        # Interesują nas wyłącznie mecze zakończone
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
        
        # Zliczanie zmian (Substitutions)
        subs = live_data.get('substitute', [])
        home_subs = sum(1 for s in subs if s.get('contestantId') == home_id)
        away_subs = sum(1 for s in subs if s.get('contestantId') == away_id)
        
        # Interwencje VAR
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
    """Zapisuje przefiltrowane dane do Google Sheets przy użyciu bezpośredniego adresu URL."""
    if not parsed_data:
        print("Brak nowych danych do wykonania zapisu.")
        return
        
    df = pd.DataFrame(parsed_data)
    
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    if os.path.exists("credentials.json"):
        creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
    else:
        creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
        
    client = gspread.authorize(creds)
    
    # Otwieranie pliku bezpośrednio przez URL - omija restrykcje Dysków Wspólnych (Błąd 400 bad request)
    spreadsheet = client.open_by_url("https://docs.google.com/spreadsheets/d/11yc_BrZA649aZgeJhLedETqg6NI1k1_QFje7WNEjIHk/edit")
    
    try:
        sheet = spreadsheet.worksheet("Opta_Results")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Opta_Results", rows=2000, cols=20)
        
    sheet.clear()
    # Nowa, zaktualizowana składnia dla biblioteki gspread v6+ z jawnym podaniem komórki startowej A1
    sheet.update(([df.columns.tolist()] + df.values.tolist()), "A1")
    print(f"Pomyślnie zsynchronizowano {len(df)} rozegranych meczów z Opta Stats do Google Sheets!")

if __name__ == "__main__":
    print("Pobieranie bazy danych Opta...")
    raw_json = fetch_all_opta_results()
    if raw_json:
        print("Przetwarzanie danych...")
        clean_data = parse_all_matches(raw_json)
        print("Zapis do Google Sheets...")
        save_to_google_sheets(clean_data)
