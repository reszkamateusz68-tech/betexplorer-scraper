import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import json
import math

# --- KONFIGURACJA TELEGRAM ---
TELEGRAM_TOKEN = "8905463018:AAHcBKiPhOwlV7T2FEKSOWvvVmzfUBujpYM"
TELEGRAM_CHAT_ID = "-1003525389019"
LINK_DASHBOARDU = "https://datastudio.google.com/embed/reporting/99821c8b-06f8-4b96-b5d4-2384420e2b75/page/p_oeivgwp54d"

# --- KONFIGURACJA GOOGLE SHEETS ---
# Używasz tych samych poświadczeń co w betexplorer_all.py
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
arkusz = client.open("BetExplorer").worksheet("All_Predictions")

def generuj_i_wyslij_ako():
    print("Łączę z Google Sheets w poszukiwaniu zaznaczonych typów...")
    wszystkie_dane = arkusz.get_all_records()
    
    kupon_mecze = []
    wiersze_do_wyczyszczenia = []
    
    # Przeszukujemy arkusz (zakładamy, że dodałeś kolumnę 'Wyslij_AKO')
    for index, wiersz in enumerate(wszystkie_dane):
        if str(wiersz.get('Wyslij_AKO', '')).upper() == 'X':
            kupon_mecze.append(wiersz)
            wiersze_do_wyczyszczenia.append(index + 2) # +2 bo gspread liczy od 1, a 1 to nagłówek
            
    if not kupon_mecze:
        print("Nie zaznaczono żadnego meczu ('X' w kolumnie Wyslij_AKO).")
        return

    # --- OBLICZANIE MATEMATYKI KUPONU ---
    laczny_kurs = 1.0
    lista_tekstowa = ""
    
    for i, mecz in enumerate(kupon_mecze, 1):
        gospodarz = mecz.get('Gospodarz', 'Nieznany')
        gosc = mecz.get('Gość', 'Nieznany')
        typ = mecz.get('Typ', '-')
        
        # Pobieranie kursu (wymaga zamiany na float dla obliczeń)
        kurs_str = str(mecz.get('Kurs_Rynek', '1.0')).replace(',', '.')
        try:
            kurs_wartosc = float(kurs_str)
        except ValueError:
            kurs_wartosc = 1.0 # Bezpiecznik, jeśli kursu brakuje
            
        laczny_kurs *= kurs_wartosc
        
        lista_tekstowa += f"<b>{i}. {gospodarz} vs {gosc}</b>\n"
        lista_tekstowa += f"🎯 Typ: {typ} | 📈 Kurs: {kurs_wartosc:.2f}\n\n"

    # Symulacja Stawki: 100 PLN, Podatek w PL: 12%
    stawka = 100
    wspolczynnik = 0.88
    potencjalna_wygrana = stawka * wspolczynnik * laczny_kurs

    # --- FORMATOWANIE WIADOMOŚCI ---
    wiadomosc = f"""🔥 <b>GOTOWY KUPON AKO (Algorytm + Weryfikacja)</b> 🔥\n\n"""
    wiadomosc += lista_tekstowa
    wiadomosc += f"""───────────────
📊 <b>Podsumowanie Kuponu:</b>
📈 Łączny kurs: <b>{laczny_kurs:.2f}</b>
💰 Stawka: <b>{stawka} PLN</b>
💸 Do wygrania: <b>{potencjalna_wygrana:.2f} PLN</b> <i>(po odliczeniu podatku 12%)</i>"""

    klawiatura = {"inline_keyboard": [[{"text": "📊 Otwórz Pełny Dashboard", "url": LINK_DASHBOARDU}]]}
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": wiadomosc,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(klawiatura)
    }
    
    # --- WYSYŁKA ---
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data=payload)
    
    if resp.status_code == 200:
        print("✅ Kupon AKO wysłany na Telegram!")
        # Czyszczenie 'X' w arkuszu, żeby nie dublować przy kolejnym uruchomieniu
        for wiersz_idx in wiersze_do_wyczyszczenia:
            arkusz.update_cell(wiersz_idx, len(wszystkie_dane[0]), "") # Aktualizuje ostatnią kolumnę
        print("🧹 Wyszyszczono arkusz ze znaczników.")
    else:
        print(f"❌ Błąd wysyłki: {resp.text}")

generuj_i_wyslij_ako()
