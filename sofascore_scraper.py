import requests
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import os
import json
import time

def get_sofascore_headers():
    """Generuje nagłówki imitujące prawdziwą przeglądarkę, aby ominąć blokady API."""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://www.sofascore.com",
        "Referer": "https://www.sofascore.com/",
        "Cache-Control": "no-cache"
    }

def load_sofascore_ids():
    """Czyta plik tekstowy i wyciąga unikalne ID meczów z linków Sofascore."""
    file_path = "sofascore_matches.txt"
    if not os.path.exists(file_path):
        print(f"BŁĄD: Nie znaleziono pliku {file_path}")
        return []
        
    match_ids = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "id:" in line:
                # Wyciągamy sam numer ID po "id:"
                m_id = line.split("id:")[-1]
                match_ids.append(m_id)
                
    print(f"Znaleziono {len(match_ids)} meczów do przetworzenia.")
    return match_ids

def get_match_data(match_id):
    """Pobiera podstawowe informacje o meczu (drużyny, wyniki, data)."""
    url = f"https://api.sofascore.com/api/v1/event/{match_id}"
    try:
        response = requests.get(url, headers=get_sofascore_headers(), timeout=10)
        if response.status_code == 200:
            return response.json().get('event', {})
    except Exception as e:
        print(f"Błąd pobierania danych meczu {match_id}: {e}")
    return {}

def get_match_statistics(match_id):
    """Pobiera głębokie statystyki techniczne z ukrytego API."""
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/statistics"
    try:
        response = requests.get(url, headers=get_sofascore_headers(), timeout=10)
        if response.status_code == 200:
            return response.json().get('statistics', [])
    except Exception as e:
        print(f"Błąd pobierania statystyk meczu {match_id}: {e}")
    return []

def extract_stat_value(stat_groups, stat_name, team_side):
    """Pomocnicza funkcja do wyciągania konkretnej statystyki z zagnieżdżonej struktury Sofascore."""
    for group in stat_groups:
        for item in group.get('statisticsItems', []):
            if item.get('name') == stat_name:
                return item.get(team_side, "-")
    return "-"

def process_sofascore_scraper():
    """Główny silnik analizujący mecze i mapujący dane pod arkusz."""
    match_ids = load_sofascore_ids()
    all_rows = []
    
    for match_id in match_ids:
        print(f"Analiza meczu ID: {match_id}...")
        
        # 1. Pobieranie danych
        event_data = get_match_data(match_id)
        stats_data = get_match_statistics(match_id)
        
        if not event_data:
            continue
            
        # Zabezpieczenie przed meczami, które się jeszcze nie odbyły
        if event_data.get('status', {}).get('type') != 'finished':
            print(f"Mecz {match_id} jeszcze się nie zakończył. Pomijam.")
            continue

        # 2. Wyciąganie podstawowych informacji
        tournament = event_data.get('tournament', {}).get('name', 'Nieznana liga')
        home_team = event_data.get('homeTeam', {}).get('name', 'Gospodarz')
        away_team = event_data.get('awayTeam', {}).get('name', 'Gość')
        
        # Wyniki z podziałem na końcowy i do przerwy (HT)
        score_ft_home = event_data.get('homeScore', {}).get('current', 0)
        score_ft_away = event_data.get('awayScore', {}).get('current', 0)
        score_ht_home = event_data.get('homeScore', {}).get('period1', 0)
        score_ht_away = event_data.get('awayScore', {}).get('period1', 0)
        
        # 3. Wyciąganie zaawansowanych statystyk (Domyślnie z okresu "ALL")
        possession_h, possession_a = "-", "-"
        corners_h, corners_a = "-", "-"
        shots_on_target_h, shots_on_target_a = "-", "-"
        fouls_h, fouls_a = "-", "-"
        yellow_cards_h, yellow_cards_a = "-", "-"
        xg_h, xg_a = "-", "-"
        
        # Filtrujemy blok statystyk dla całego meczu ('ALL')
        all_stats_group = next((s.get('groups', []) for s in stats_data if s.get('period') == 'ALL'), [])
        
        if all_stats_group:
            possession_h = extract_stat_value(all_stats_group, 'Ball possession', 'home')
            possession_a = extract_stat_value(all_stats_group, 'Ball possession', 'away')
            
            corners_h = extract_stat_value(all_stats_group, 'Corner kicks', 'home')
            corners_a = extract_stat_value(all_stats_group, 'Corner kicks', 'away')
            
            shots_on_target_h = extract_stat_value(all_stats_group, 'Shots on target', 'home')
            shots_on_target_a = extract_stat_value(all_stats_group, 'Shots on target', 'away')
            
            fouls_h = extract_stat_value(all_stats_group, 'Fouls', 'home')
            fouls_a = extract_stat_value(all_stats_group, 'Fouls', 'away')
            
            yellow_cards_h = extract_stat_value(all_stats_group, 'Yellow cards', 'home')
            yellow_cards_a = extract_stat_value(all_stats_group, 'Yellow cards', 'away')
            
            xg_h = extract_stat_value(all_stats_group, 'Expected goals', 'home')
            xg_a = extract_stat_value(all_stats_group, 'Expected goals', 'away')

        parsed_row = {
            "ID_Meczu": match_id,
            "Liga": tournament,
            "Gospodarz": home_team,
            "Gosc": away_team,
            "Gole_Gosp": score_ft_home,
            "Gole_Gosc": score_ft_away,
            "HT_Gosp": score_ht_home,
            "HT_Gosc": score_ht_away,
            "Posiadanie_Gosp": possession_h,
            "Posiadanie_Gosc": possession_a,
            "Rozne_Gosp": corners_h,
            "Rozne_Gosc": corners_a,
            "Celne_Gosp": shots_on_target_h,
            "Celne_Gosc": shots_on_target_a,
            "Faule_Gosp": fouls_h,
            "Faule_Gosc": fouls_a,
            "Zolte_Gosp": yellow_cards_h,
            "Zolte_Gosc": yellow_cards_a,
            "xG_Gosp": xg_h,
            "xG_Gosc": xg_a
        }
        all_rows.append(parsed_row)
        
        # Zabezpieczenie przed blokadą anty-botową (1 sekunda przerwy)
        time.sleep(1)
        
    return all_rows

def save_to_google_sheets(parsed_data):
    """Eksportuje wygenerowaną tabelę do Google Sheets."""
    if not parsed_data:
        print("Brak nowych statystyk do zapisu.")
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
        sheet = spreadsheet.worksheet("Sofascore_Stats")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Sofascore_Stats", rows=2000, cols=25)
        
    sheet.clear()
    sheet.update(([df.columns.tolist()] + df.values.tolist()), "A1")
    print(f"SUKCES: Zaawansowane statystyki Sofascore dla {len(df)} meczów zostały zapisane w arkuszu!")

if __name__ == "__main__":
    print("Uruchamiam zautomatyzowany system pobierania statystyk Sofascore...")
    data = process_sofascore_scraper()
    save_to_google_sheets(data)
