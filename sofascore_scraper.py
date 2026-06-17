import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import os
import json

def load_local_sofascore_data():
    """Wczytuje surowe dane statystyk bezpośrednio z pliku w repozytorium, omijając błąd 403."""
    file_path = "sofascore_raw.txt"
    if not os.path.exists(file_path):
        print(f"BŁĄD: Nie znaleziono pliku {file_path} w repozytorium!")
        return None
        
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw_text = f.read().strip()
        
        # Ładujemy tekst jako czysty słownik JSON
        data = json.loads(raw_text)
        return data.get('statistics', data) # Obsługuje czysty wyciąg lub pełny obiekt
    except Exception as e:
        print(f"Błąd przetwarzania pliku sofascore_raw.txt: {e}")
        return None

def extract_stat_value(stat_groups, stat_name, team_side):
    """Wyciąga konkretną statystykę z zagnieżdżonej struktury Sofascore."""
    for group in stat_groups:
        for item in group.get('statisticsItems', []):
            if item.get('name') == stat_name:
                return item.get(team_side, "-")
    return "-"

def process_local_scraper():
    """Przetwarza lokalny plik z danymi i przygotowuje wiersz do arkusza."""
    stats_data = load_local_sofascore_data()
    if not stats_data:
        return []
        
    # Filtrujemy blok statystyk dla całego meczu ('ALL')
    all_stats_group = next((s.get('groups', []) for s in stats_data if s.get('period') == 'ALL'), [])
    
    if not all_stats_group:
        print("Nie znaleziono sekcji statystyk 'ALL' w pliku.")
        return []

    # Wyciąganie parametrów z poprawioną nazwą grupy (all_stats_group)
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

    # Tworzymy wiersz danych
    parsed_row = {
        "ID_Meczu": "Zrzut Lokalny",
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
    
    return [parsed_row]

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
    print(f"SUKCES: Statystyki Sofascore zostały pomyślnie przeniesione z pliku do arkusza Google Sheets!")

if __name__ == "__main__":
    print("Uruchamiam lokalny dekoder danych Sofascore...")
    data = process_local_scraper()
    save_to_google_sheets(data)
