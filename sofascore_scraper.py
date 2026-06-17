from curl_cffi import requests
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import os
import json
import time

# Konfiguracja Premier League
TOURNAMENT_ID = "17"  
SEASON_ID = "61643"   

def fetch_finished_match_ids():
    """Pobiera listę meczów z ligi z użyciem spoofingu przeglądarki Chrome."""
    url = f"https://api.sofascore.com/api/v1/tournament/{TOURNAMENT_ID}/season/{SEASON_ID}/events"
    try:
        # Parametr impersonate="chrome110" omija 99% zabezpieczeń Cloudflare
        response = requests.get(url, impersonate="chrome110", timeout=30)
        if response.status_code == 200:
            events = response.json().get('events', [])
            finished_matches = [e for e in events if e.get('status', {}).get('type') == 'finished']
            return finished_matches
        else:
            print(f"Błąd API Kalendarz: Otrzymano status {response.status_code}")
            return []
    except Exception as e:
        print(f"Błąd sieciowy przy kalendarzu: {e}")
        return []

def get_match_statistics(match_id):
    """Pobiera zaawansowane statystyki (xG, rożne) dla meczu."""
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/statistics"
    try:
        response = requests.get(url, impersonate="chrome110", timeout=15)
        if response.status_code == 200:
            return response.json().get('statistics', [])
    except Exception as e:
        print(f"Błąd pobierania statystyk dla ID {match_id}: {e}")
    return []

def extract_stat(stat_groups, stat_name, team_side):
    """Wyciąga pojedynczą statystykę ze struktury."""
    for group in stat_groups:
        for item in group.get('statisticsItems', []):
            if item.get('name') == stat_name:
                return item.get(team_side, "-")
    return "-"

def process_full_season():
    """Główny procesor łączący ligę i głębokie statystyki meczowe."""
    print("Krok 1: Próba ominięcia Cloudflare i pobrania kalendarza całej ligi...")
    finished_events = fetch_finished_match_ids()
    
    if not finished_events:
        print("Brak zakończonych meczów do przetworzenia.")
        return []
        
    print(f"SUKCES! Zabezpieczenia ominięte. Znaleziono {len(finished_events)} rozegranych spotkań.")
    all_rows = []
    
    for idx, event in enumerate(finished_events, start=1):
        match_id = event.get('id')
        home_team = event.get('homeTeam', {}).get('name', 'Gospodarz')
        away_team = event.get('awayTeam', {}).get('name', 'Gość')
        
        date_timestamp = event.get('startTimestamp', 0)
        date_str = time.strftime('%Y-%m-%d', time.localtime(date_timestamp)) if date_timestamp else "-"
        score_ft_home = event.get('homeScore', {}).get('current', 0)
        score_ft_away = event.get('awayScore', {}).get('current', 0)
        score_ht_home = event.get('homeScore', {}).get('period1', 0)
        score_ht_away = event.get('awayScore', {}).get('period1', 0)
        
        print(f"[{idx}/{len(finished_events)}] Skanowanie: {home_team} vs {away_team} (ID: {match_id})")
        
        stats_data = get_match_statistics(match_id)
        all_stats_group = next((s.get('groups', []) for s in stats_data if s.get('period') == 'ALL'), [])
        
        possession_h = extract_stat(all_stats_group, 'Ball possession', 'home')
        possession_a = extract_stat(all_stats_group, 'Ball possession', 'away')
        corners_h = extract_stat(all_stats_group, 'Corner kicks', 'home')
        corners_a = extract_stat(all_stats_group, 'Corner kicks', 'away')
        sot_h = extract_stat(all_stats_group, 'Shots on target', 'home')
        sot_a = extract_stat(all_stats_group, 'Shots on target', 'away')
        fouls_h = extract_stat(all_stats_group, 'Fouls', 'home')
        fouls_a = extract_stat(all_stats_group, 'Fouls', 'away')
        xg_h = extract_stat(all_stats_group, 'Expected goals', 'home')
        xg_a = extract_stat(all_stats_group, 'Expected goals', 'away')
        yellow_cards_h = extract_stat(all_stats_group, 'Yellow cards', 'home')
        yellow_cards_a = extract_stat(all_stats_group, 'Yellow cards', 'away')
        
        parsed_row = {
            "ID_Meczu": match_id,
            "Data": date_str,
            "Gospodarz": home_team,
            "Gosc": away_team,
            "Gole_Gosp_FT": score_ft_home,
            "Gole_Gosc_FT": score_ft_away,
            "Gole_Gosp_HT": score_ht_home,
            "Gole_Gosc_HT": score_ht_away,
            "Posiadanie_Gosp": possession_h,
            "Posiadanie_Gosc": possession_a,
            "Rozne_Gosp": corners_h,
            "Rozne_Gosc": corners_a,
            "Celne_Gosp": sot_h,
            "Celne_Gosc": sot_a,
            "Faule_Gosp": fouls_h,
            "Faule_Gosc": fouls_a,
            "Zolte_Gosp": yellow_cards_h,
            "Zolte_Gosc": yellow_cards_a,
            "xG_Gosp": xg_h,
            "xG_Gosc": xg_a
        }
        all_rows.append(parsed_row)
        
        # Obowiązkowa przerwa 2 sekundy, by nie rozgniewać serwera
        time.sleep(2)
        
    return all_rows

def save_to_google_sheets(parsed_data):
    if not parsed_data:
        print("Brak wygenerowanych danych do eksportu.")
        return
        
    df = pd.DataFrame(parsed_data)
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
        sheet = spreadsheet.add_worksheet(title="Sofascore_Stats", rows=3000, cols=25)
        
    sheet.clear()
    sheet.update(([df.columns.tolist()] + df.values.tolist()), "A1")
    print(f"SUKCES: Zapisano bazę {len(df)} meczów do Google Sheets!")

if __name__ == "__main__":
    print("Uruchamianie gigantycznego skanera ligowego Sofascore...")
    data = process_full_season()
    save_to_google_sheets(data)
