import requests
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import os
import json
import time

# Konfiguracja zbiorczego API Sofascore dla Premier League (Sezon 25/26)
TOURNAMENT_ID = "17"  # Premier League
SEASON_ID = "61643"   # Identyfikator konkretnego sezonu na Sofascore

def fetch_full_season_data():
    """Pobiera zbiorczą paczkę wszystkich meczów i statystyk z całego sezonu ligowego."""
    url = f"https://api.sofascore.com/api/v1/tournament/{TOURNAMENT_ID}/season/{SEASON_ID}/events"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://www.sofascore.com",
        "Referer": "https://www.sofascore.com/"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            return response.json().get('events', [])
        else:
            print(f"Błąd pobierania bazy ligowej: Status {response.status_code}")
            return []
    except Exception as e:
        print(f"Błąd sieciowy podczas pobierania bazy sezonu: {e}")
        return []

def parse_season_events(events_list):
    """Przetwarza setki meczów z bazy ligowej na czystą tabelę pod system typowania."""
    if not events_list:
        print("Brak wydarzeń ligowych do przetworzenia.")
        return []
        
    all_parsed_matches = []
    
    for event in events_list:
        # Interesują nas wyłącznie mecze zakończone
        if event.get('status', {}).get('type') != 'finished':
            continue
            
        match_id = event.get('id')
        custom_id = event.get('customId', '')
        date_timestamp = event.get('startTimestamp', 0)
        
        # Konwersja czasu Unix na czytelną datę
        date_str = time.strftime('%Y-%m-%d', time.localtime(date_timestamp)) if date_timestamp else "-"
        
        # Nazwy drużyn
        home_team = event.get('homeTeam', {}).get('name', 'Gospodarz')
        away_team = event.get('awayTeam', {}).get('name', 'Gość')
        
        # Wyniki końcowe (FT) i do przerwy (HT)
        home_score_ft = event.get('homeScore', {}).get('current', 0)
        away_score_ft = event.get('awayScore', {}).get('current', 0)
        home_score_ht = event.get('homeScore', {}).get('period1', 0)
        away_score_ht = event.get('awayScore', {}).get('period1', 0)
        
        # Sędzia i dodatkowe wskaźniki (jeśli są dostępne w pliku zbiorczym)
        referee = event.get('referee', {}).get('name', 'Nieznany')
        
        # Kartki wyciąganie ze struktury kar
        cards_h = event.get('homeScore', {}).get('yellowCards', "-")
        cards_a = event.get('awayScore', {}).get('yellowCards', "-")
        
        # Generujemy wiersz danych dla całego sezonu
        parsed_row = {
            "ID_Meczu": match_id,
            "Custom_ID": custom_id,
            "Data": date_str,
            "Gospodarz": home_team,
            "Gosc": away_team,
            "Gole_Gosp_FT": home_score_ft,
            "Gole_Gosc_FT": away_score_ft,
            "Gole_Gosp_HT": home_score_ht,
            "Gole_Gosc_HT": away_score_ht,
            "Zolte_Gosp": cards_h,
            "Zolte_Gosc": cards_a,
            "Sedzia": referee
        }
        all_parsed_matches.append(parsed_row)
        
    return all_parsed_matches

def save_to_google_sheets(parsed_data):
    """Zapisuje kompletną bazę danych całej ligi do Google Sheets."""
    if not parsed_data:
        print("Brak wygenerowanych danych do eksportu.")
        return
        
    df = pd.DataFrame(parsed_data)
    
    # Sortowanie od najnowszych meczów
    if 'Data' in df.columns:
        df = df.sort_values(by='Data', ascending=False)
        
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    if os.path.exists("credentials.json"):
        creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
    else:
        creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
        
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_url("https://docs.google.com/spreadsheets/d/11yc_BrZA649aZgeJhLedETqg6NI1k1_QFje7WNEjIHk/edit")
    
    try:
        sheet = spreadsheet.worksheet("Sofascore_Stats")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Sofascore_Stats", rows=3000, cols=20)
        
    sheet.clear()
    sheet.update(([df.columns.tolist()] + df.values.tolist()), "A1")
    print(f"SUKCES: Pomyślnie zsynchronizowano całą ligę ({len(df)} rozegranych meczów) do Google Sheets!")

if __name__ == "__main__":
    print("Inicjalizacja zautomatyzowanego pobierania całego sezonu Sofascore...")
    raw_events = fetch_full_season_data()
    if raw_events:
        print(f"Pobrano {len(raw_events)} surowych rekordów. Rozpoczynam parsowanie ligi...")
        clean_data = parse_season_events(raw_events)
        print("Wstrzykiwanie bazy do Google Sheets...")
        save_to_google_sheets(clean_data)
