import os
import json
import requests
import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

# ==========================================
# KONFIGURACJA FINANSOWA I API
# ==========================================
WARTOSC_JEDNOSTKI_PLN = 100.0  # Ustaw ile PLN to 1 jednostka (np. 100.0)
PODATEK_BUKMACHERSKI = 0.88    # Legalni bukmacherzy w PL (12% podatku od stawki)

# ==========================================
# SZABLONY WIADOMOŚCI
# ==========================================
SZABLON_NOWY = """
🔥 <b>PROPOZYCJA AKO</b> 🔥

🆔 <i>{id_kuponu}</i>
───────────────
{mecze}───────────────
📊 <b>Podsumowanie Kuponu:</b>
📈 Łączny kurs: {kurs}
💰 Stawka: {stawka_j}j ({stawka_pln} PLN przy 1j={wartosc_j}zł)
💸 Do wygrania: +{zysk_j}j (+{zysk_pln} PLN po odliczeniu podatku)
"""

SZABLON_WYGRANA = """
✅ <b>KUPON ZAKOŃCZONY ZYSKIEM!</b> ✅

🆔 <i>{id_kuponu}</i>
───────────────
{mecze}───────────────
📈 Łączny kurs: {kurs}
💰 Wygrana: +{zysk_j}j (+{zysk_pln} PLN po odliczeniu podatku)
"""

SZABLON_PRZEGRANA = """
❌ <b>KUPON ZAKOŃCZONY PORAŻKĄ</b> ❌

🆔 <i>{id_kuponu}</i>
───────────────
{mecze}───────────────
📈 Łączny kurs: {kurs}
📉 Strata: -{stawka_j}j (-{stawka_pln} PLN)
"""

# ==========================================
# INICJALIZACJA GOOGLE SHEETS
# ==========================================
scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
if os.path.exists("credentials.json"):
    creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
else:
    creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)

client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

ws_pred = spreadsheet.worksheet("All_Predictions")
ws_ako = spreadsheet.worksheet("Kupony_AKO")
ws_hist = spreadsheet.worksheet("Historia_Typow")

df_pred = pd.DataFrame(ws_pred.get_all_records())
df_ako = pd.DataFrame(ws_ako.get_all_records())
df_hist = pd.DataFrame(ws_hist.get_all_records())

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        print(f"Błąd wysyłki Telegram: {response.text}")

# ==========================================
# 1. WYSYŁKA NOWYCH KUPONÓW
# ==========================================
if 'Wyslij_AKO' in df_pred.columns:
    do_wysylki = df_pred[df_pred['Wyslij_AKO'].astype(str).str.upper().isin(['TRUE', 'TAK', '1'])]
    
    if not do_wysylki.empty:
        print(f"Znaleziono {len(do_wysylki['Kupon_ID'].unique())} nowych kuponów do wysyłki.")
        for kupon_id in do_wysylki['Kupon_ID'].unique():
            if str(kupon_id).strip() == "": continue
            
            kupon_data = df_ako[df_ako['Kupon_ID'] == kupon_id]
            if kupon_data.empty: continue
            
            rekord = kupon_data.iloc[0]
            mecze_df = do_wysylki[do_wysylki['Kupon_ID'] == kupon_id]
            
            lista_meczow_txt = ""
            for _, m in mecze_df.iterrows():
                lista_meczow_txt += f"⚽ {m['Gospodarz']} vs {m['Gość']}\n📅 {m['Data']} ⏰ {m['Godzina']} | 🎯 Typ: <b>{m['Typ']}</b> | 📈 {m['Kurs_Szac']}\n\n"
            
            try: stawka_pln = float(str(rekord.get('Stawka', '100')).replace(',', '.'))
            except: stawka_pln = 100.0
            
            try: kurs_ako = float(str(rekord.get('Kurs_AKO', '1.0')).replace(',', '.'))
            except: kurs_ako = 1.0
            
            stawka_j = round(stawka_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            zysk_pln = round((stawka_pln * PODATEK_BUKMACHERSKI * kurs_ako) - stawka_pln, 2)
            zysk_j = round(zysk_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            
            wiadomosc = SZABLON_NOWY.format(
                id_kuponu=kupon_id,
                mecze=lista_meczow_txt,
                kurs=f"{kurs_ako:.2f}",
                stawka_j=stawka_j,
                stawka_pln=stawka_pln,
                wartosc_j=int(WARTOSC_JEDNOSTKI_PLN),
                zysk_j=zysk_j,
                zysk_pln=zysk_pln
            )
            
            send_telegram(wiadomosc)
            
        # Oznaczanie kuponów jako wysłane
        komorki_do_odznaczenia = []
        ws_pred_data = ws_pred.get_all_values()
        idx_wyslij = ws_pred_data[0].index("Wyslij_AKO")
        
        for r_idx, row in enumerate(ws_pred_data):
            if row[idx_wyslij].upper() in ['TRUE', 'TAK', '1']:
                komorki_do_odznaczenia.append(gspread.Cell(row=r_idx+1, col=idx_wyslij+1, value="FALSE"))
                
        if komorki_do_odznaczenia:
            ws_pred.update_cells(komorki_do_odznaczenia)
            print("Pomyślnie oznaczono nowe kupony jako wysłane.")

# ==========================================
# 2. WYSYŁKA PODSUMOWAŃ ROZLICZONYCH KUPONÓW
# ==========================================
# Gwarancja istnienia kolumny powiadomień
if 'Telegram_Status' not in df_ako.columns:
    df_ako['Telegram_Status'] = ""
    ws_ako.update([df_ako.columns.values.tolist()] + df_ako.fillna("").values.tolist())

do_podsumowania = df_ako[
    (df_ako['Status_AKO'].isin(['WYGRANA', 'PRZEGRANA'])) & 
    (df_ako['Telegram_Status'] != 'WYSŁANO')
]

if not do_podsumowania.empty:
    print(f"Znaleziono {len(do_podsumowania)} rozliczonych kuponów do podsumowania.")
    
    komorki_ako_do_aktualizacji = []
    ws_ako_data = ws_ako.get_all_values()
    idx_kupon = ws_ako_data[0].index("Kupon_ID")
    idx_tel_status = ws_ako_data[0].index("Telegram_Status")
    
    for _, rekord in do_podsumowania.iterrows():
        kupon_id = rekord['Kupon_ID']
        if str(kupon_id).strip() == "": continue
        
        mecze_df = df_hist[df_hist['Kupon_ID'] == kupon_id]
        if mecze_df.empty: continue
        
        lista_meczow_txt = ""
        for _, m in mecze_df.iterrows():
            status_meczu = str(m.get('Status', '')).upper()
            if status_meczu == "WYGRANA": emoji = "🟢"
            elif status_meczu == "PRZEGRANA": emoji = "🔴"
            else: emoji = "⚪" # Zwroty, odwołane mecze, inne zdarzenia techniczne
            
            lista_meczow_txt += f"{emoji} {m['Gospodarz']} - {m['Gość']} | Typ: <b>{m['Typ']}</b>\n"
        
        try: stawka_pln = float(str(rekord.get('Stawka', '100')).replace(',', '.'))
        except: stawka_pln = 100.0
        
        try: kurs_ako = float(str(rekord.get('Kurs_AKO', '1.0')).replace(',', '.'))
        except: kurs_ako = 1.0
        
        stawka_j = round(stawka_pln / WARTOSC_JEDNOSTKI_PLN, 2)
        
        if rekord['Status_AKO'] == 'WYGRANA':
            zysk_pln = round((stawka_pln * PODATEK_BUKMACHERSKI * kurs_ako) - stawka_pln, 2)
            zysk_j = round(zysk_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            wiadomosc = SZABLON_WYGRANA.format(
                id_kuponu=kupon_id, mecze=lista_meczow_txt, 
                kurs=f"{kurs_ako:.2f}", zysk_j=zysk_j, zysk_pln=zysk_pln
            )
        else:
            wiadomosc = SZABLON_PRZEGRANA.format(
                id_kuponu=kupon_id, mecze=lista_meczow_txt, 
                kurs=f"{kurs_ako:.2f}", stawka_j=stawka_j, stawka_pln=stawka_pln
            )
            
        send_telegram(wiadomosc)
        
        for r_idx, row in enumerate(ws_ako_data):
            if row[idx_kupon] == kupon_id:
                komorki_ako_do_aktualizacji.append(gspread.Cell(row=r_idx+1, col=idx_tel_status+1, value="WYSŁANO"))
                break

    if komorki_ako_do_aktualizacji:
        ws_ako.update_cells(komorki_ako_do_aktualizacji)
        print("Pomyślnie wysłano podsumowania i oznaczono je w arkuszu.")
