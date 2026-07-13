import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import json
import pandas as pd

# --- KONFIGURACJA TELEGRAM ---
TELEGRAM_TOKEN = "8905463018:AAHcBKiPhOwlV7T2FEKSOWvvVmzfUBujpYM"
TELEGRAM_CHAT_ID = "-1003525389019"
LINK_DASHBOARDU = "https://datastudio.google.com/embed/reporting/99821c8b-06f8-4b96-b5d4-2384420e2b75/page/p_oeivgwp54d"

# --- KONFIGURACJA GOOGLE SHEETS ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

arkusz_historia = client.open("BetExplorer").worksheet("Historia_Typow")
arkusz_ako = client.open("BetExplorer").worksheet("Kupony_AKO")

def podsumuj_i_wyslij():
    print("Łączę z Google Sheets w poszukiwaniu rozliczonych kuponów do wysłania...")
    
    # Pobieramy dane jako DataFrame dla łatwiejszego filtrowania
    dane_ako_raw = arkusz_ako.get_all_records()
    df_historia = pd.DataFrame(arkusz_historia.get_all_records())
    
    wiersze_do_wyczyszczenia = []
    kupony_do_wyslania = []

    for index, wiersz in enumerate(dane_ako_raw):
        wartosc_pola = str(wiersz.get('Wyslij_Podsumowanie', '')).upper()
        if wartosc_pola == 'TRUE' or wartosc_pola == 'X':
            kupony_do_wyslania.append(wiersz)
            wiersze_do_wyczyszczenia.append(index + 2) # +2 ze względu na nagłówek

    if not kupony_do_wyslania:
        print("Nie zaznaczono żadnego kuponu do podsumowania.")
        return

    for kupon in kupony_do_wyslania:
        kupon_id = str(kupon.get('Kupon_ID', ''))
        status_ako = str(kupon.get('Status_AKO', ''))
        kurs_ako = str(kupon.get('Kurs_AKO', ''))
        profit_netto = str(kupon.get('Profit_Netto', ''))
        
        # Pobieramy mecze składowe dla tego konkretnego kuponu
        mecze_składowe = df_historia[df_historia['Kupon_ID'] == kupon_id]
        
        if status_ako == "WYGRANA":
            naglowek = "✅ <b>KUPON ZAKOŃCZONY ZYSKIEM!</b> ✅"
            profit_tekst = f"💰 Czysty zysk: <b>+{profit_netto} PLN</b>"
        elif status_ako == "PRZEGRANA":
            naglowek = "❌ <b>KUPON ZAKOŃCZONY PORAŻKĄ</b> ❌"
            profit_tekst = f"📉 Strata: <b>{profit_netto} PLN</b>"
        else:
            naglowek = "⏳ <b>KUPON W GRZE / ZWROT</b> ⏳"
            profit_tekst = f"Saldo: <b>{profit_netto} PLN</b>"

        lista_meczow_tekst = ""
        for _, mecz in mecze_składowe.iterrows():
            gosp = mecz.get('Gospodarz', '')
            gosc = mecz.get('Gość', '')
            typ = mecz.get('Typ', '')
            status_meczu = mecz.get('Status', '')
            
            if status_meczu == "WYGRANA":
                ikona = "🟢"
            elif status_meczu == "PRZEGRANA":
                ikona = "🔴"
            else:
                ikona = "⚪"
                
            lista_meczow_tekst += f"{ikona} {gosp} - {gosc} | Typ: {typ}\n"

        wiadomosc = f"""{naglowek}\n\n<b>ID Kuponu:</b> #{kupon_id.split('_')[-1]}\n───────────────\n{lista_meczow_tekst}───────────────\n📈 Łączny kurs: <b>{kurs_ako}</b>\n{profit_tekst}"""

        klawiatura = {"inline_keyboard": [[{"text": "📊 Zobacz pełną historię systemu", "url": LINK_DASHBOARDU}]]}
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": wiadomosc,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(klawiatura)
        }

        # Wysyłka
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, data=payload)
        
        if resp.status_code == 200:
            print(f"✅ Podsumowanie dla {kupon_id} wysłane!")
        else:
            print(f"❌ Błąd wysyłki dla {kupon_id}: {resp.text}")

    # Czyszczenie checkboxów
    if wiersze_do_wyczyszczenia:
        kolumna_wyslij_index = len(dane_ako_raw[0])
        for wiersz_idx in wiersze_do_wyczyszczenia:
            arkusz_ako.update_cell(wiersz_idx, kolumna_wyslij_index, "FALSE")
        print("🧹 Odznaczono checkboxy wysyłki w arkuszu Kupony_AKO.")

if __name__ == "__main__":
    podsumuj_i_wyslij()
