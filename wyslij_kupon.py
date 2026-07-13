import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import json
import math
from datetime import datetime

# --- KONFIGURACJA TELEGRAM ---
TELEGRAM_TOKEN = "8905463018:AAHcBKiPhOwlV7T2FEKSOWvvVmzfUBujpYM"
TELEGRAM_CHAT_ID = "-1003525389019"
LINK_DASHBOARDU = "https://datastudio.google.com/embed/reporting/99821c8b-06f8-4b96-b5d4-2384420e2b75/page/p_oeivgwp54d"

# --- KONFIGURACJA GOOGLE SHEETS ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
arkusz_all = client.open("BetExplorer").worksheet("All_Predictions")
arkusz_historia = client.open("BetExplorer").worksheet("Historia_Typow")

def znajdz_indeksy_historii(match_id, typ, wszystkie_dane_historia):
    """Zwraca indeksy wierszy w Historii_Typow dla danego meczu i typu."""
    indeksy = []
    for idx, wiersz in enumerate(wszystkie_dane_historia):
        if str(wiersz.get('Match_ID', '')) == str(match_id) and str(wiersz.get('Typ', '')) == str(typ):
            indeksy.append(idx + 2) # +2 ze względu na nagłówek i index od 0
    return indeksy

def generuj_i_wyslij_ako():
    print("Łączę z Google Sheets w poszukiwaniu zaznaczonych typów...")
    wszystkie_dane_all = arkusz_all.get_all_records()
    wszystkie_dane_historia = arkusz_historia.get_all_records()
    
    kupon_mecze = []
    wiersze_do_wyczyszczenia = []
    
    for index, wiersz in enumerate(wszystkie_dane_all):
        wartosc_pola = str(wiersz.get('Wyslij_AKO', '')).upper()
        if wartosc_pola == 'TRUE' or wartosc_pola == 'X':
            kupon_mecze.append(wiersz)
            wiersze_do_wyczyszczenia.append(index + 2)
            
    if not kupon_mecze:
        print("Nie zaznaczono żadnego meczu.")
        return

    # Unikalny ID dla całego kuponu
    unikalny_id_kuponu = f"AKO_{datetime.now().strftime('%y%m%d_%H%M%S')}"

    laczny_kurs = 1.0
    lista_tekstowa = ""
    wiersze_historii_do_update = []
    
    for i, mecz in enumerate(kupon_mecze, 1):
        gospodarz = mecz.get('Gospodarz', 'Nieznany')
        gosc = mecz.get('Gość', 'Nieznany')
        typ = mecz.get('Typ', '-')
        data_meczu = mecz.get('Data', '')
        godzina_meczu = mecz.get('Godzina', '')
        match_id = mecz.get('Match_ID', '')
        
        # Logika kursów: jeśli Rynek jest pusty/brak, bierzemy Szacowany
        kurs_rynek_str = str(mecz.get('Kurs_Rynek', '')).replace(',', '.').strip()
        kurs_szac_str = str(mecz.get('Kurs_Szac', '1.0')).replace(',', '.').strip()
        
        if not kurs_rynek_str or kurs_rynek_str == "-" or kurs_rynek_str == "nan":
            kurs_str = kurs_szac_str
        else:
            kurs_str = kurs_rynek_str
            
        try:
            kurs_wartosc = float(kurs_str)
        except ValueError:
            kurs_wartosc = 1.0
            
        laczny_kurs *= kurs_wartosc
        
        # Zbieranie indeksów w Historii do automatycznego updatu
        wiersze_historii_do_update.extend(znajdz_indeksy_historii(match_id, typ, wszystkie_dane_historia))
        
        # Budowanie bloku wizualnego na Telegram
        lista_tekstowa += f"⚽ <b>{gospodarz} vs {gosc}</b>\n"
        lista_tekstowa += f"📅 {data_meczu} ⏰ {godzina_meczu} | 🎯 <b>Typ: {typ}</b> | 📈 <b>{kurs_wartosc:.2f}</b>\n\n"

    # --- OBLICZENIA JEDNOSTKOWE ---
    # Konfiguracja bazowa
    wartosc_1j_pln = 100 
    
    # Wyliczanie odpowiedniej stawki w zależności od ryzyka (na podstawie propozycji użytkownika)
    if laczny_kurs >= 6.0:
        stawka_j = 2.0
    elif laczny_kurs >= 3.0:
        stawka_j = 5.0
    else:
        stawka_j = 5.0 # Domyslna stawka dla bezpiecznych kuponow

    stawka_pln = stawka_j * wartosc_1j_pln
    wspolczynnik_podatkowy = 0.88
    
    # Wygrana = (Stawka_PLN * Laczny_Kurs * 0.88)
    potencjalna_wygrana_pln = stawka_pln * laczny_kurs * wspolczynnik_podatkowy
    zysk_netto_pln = potencjalna_wygrana_pln - stawka_pln
    zysk_netto_j = zysk_netto_pln / wartosc_1j_pln

    # --- FORMATOWANIE WIADOMOŚCI ---
    wiadomosc = f"""🔥 <b>QUANT PITCH | OFICJALNE AKO</b> 🔥\n\n"""
    wiadomosc += lista_tekstowa
    wiadomosc += f"""───────────────
📊 <b>PARAMETRY KUPONU:</b>
📈 Łączny kurs: <b>{laczny_kurs:.2f}</b>
💰 Stawka: <b>{stawka_j}j</b> <i>({stawka_pln:.0f} PLN przy 1j=100zł)</i>
💸 Zysk Netto: <b>+{zysk_netto_j:.2f}j</b> <i>(+{zysk_netto_pln:.0f} PLN po odliczeniu podatku)</i>"""

    klawiatura = {"inline_keyboard": [[{"text": "📊 Otwórz Algorytmiczny Dashboard", "url": LINK_DASHBOARDU}]]}
    
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
        
        # 1. Czyszczenie arkusza All_Predictions
        kolumna_wyslij_ako_index = len(wszystkie_dane_all[0])
        for wiersz_idx in wiersze_do_wyczyszczenia:
            arkusz_all.update_cell(wiersz_idx, kolumna_wyslij_ako_index, "FALSE")
            
        # 2. Automatyczne uzupełnianie Historii Typów
        try:
            kol_zagrane_idx = list(wszystkie_dane_historia[0].keys()).index('Zagrane') + 1
            kol_kupon_id_idx = list(wszystkie_dane_historia[0].keys()).index('Kupon_ID') + 1
            
            for h_idx in set(wiersze_historii_do_update):
                arkusz_historia.update_cell(h_idx, kol_zagrane_idx, "TRUE")
                arkusz_historia.update_cell(h_idx, kol_kupon_id_idx, unikalny_id_kuponu)
            print(f"🧹 Arkusze zaktualizowane. ID Kuponu to: {unikalny_id_kuponu}")
        except Exception as e:
            print(f"Błąd podczas aktualizacji historii: {e}")
    else:
        print(f"❌ Błąd wysyłki: {resp.text}")

if __name__ == "__main__":
    generuj_i_wyslij_ako()
