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
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
arkusz = client.open("BetExplorer").worksheet("Historia_Typow")

def generuj_i_wyslij_ako():
    print("Łączę z Google Sheets w poszukiwaniu zaznaczonych typów...")
    wszystkie_dane = arkusz.get_all_records()
    
    kupon_mecze = []
    wiersze_do_wyczyszczenia = []
    
    # Przeszukujemy arkusz pod kątem zaznaczonych checkboxów (TRUE) lub wpisanego X
    for index, wiersz in enumerate(wszystkie_dane):
        wartosc_pola = str(wiersz.get('Wyslij_AKO', '')).upper()
        if wartosc_pola == 'TRUE' or wartosc_pola == 'X':
            kupon_mecze.append(wiersz)
            wiersze_do_wyczyszczenia.append(index + 2) # +2 bo gspread liczy od 1, a 1 to nagłówek
            
    if not kupon_mecze:
        print("Nie zaznaczono żadnego meczu (brak zaznaczonych checkboxów w kolumnie Wyslij_AKO).")
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
            try:
                # Jeśli Kurs_Rynek zawiedzie, próbujemy pobrać Kurs_Szac
                kurs_str_szac = str(mecz.get('Kurs_Szac', '1.0')).replace(',', '.')
                kurs_wartosc = float(kurs_str_szac)
            except ValueError:
                kurs_wartosc = 1.0 # Ostateczny bezpiecznik
            
        laczny_kurs *= kurs_wartosc
        
        lista_tekstowa += f"<b>{i}. {gospodarz} vs {gosc}</b>\n"
        lista_tekstowa += f"🎯 Typ: {typ} | 📈 Kurs: {kurs_wartosc:.2f}\n\n"

    # Symulacja Stawki: 100 PLN, Podatek w PL: 12%
    stawka = 100
    wspolczynnik = 0.88
    potencjalna_wygrana = stawka * wspolczynnik * laczny_kurs

    # --- FORMATOWANIE WIADOMOŚCI ---
    wiadomosc = f"""🔥 <b>GOTOWY KUPON AKO (Weryfikacja Ekspercka)</b> 🔥\n\n"""
    wiadomosc += lista_tekstowa
    wiadomosc += f"""───────────────
📊 <b>Podsumowanie Kuponu:</b>
📈 Łączny kurs: <b>{laczny_kurs:.2f}</b>
💰 Stawka sugerowana: <b>{stawka} PLN</b>
💸 Do wygrania: <b>{potencjalna_wygrana:.2f} PLN</b> <i>(po odliczeniu podatku)</i>"""

    klawiatura = {"inline_keyboard": [[{"text": "📊 Otwórz Pełny Raport Algorytmu", "url": LINK_DASHBOARDU}]]}
    
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
        # Zmiana statusu na FALSE odznaczy checkboxy w Google Sheets
        kolumna_wyslij_ako_index = len(wszystkie_dane[0]) 
        for wiersz_idx in wiersze_do_wyczyszczenia:
            arkusz.update_cell(wiersz_idx, kolumna_wyslij_ako_index, "FALSE")
        print("🧹 Odznaczono checkboxy w arkuszu.")
    else:
        print(f"❌ Błąd wysyłki: {resp.text}")

if __name__ == "__main__":
    generuj_i_wyslij_ako()
