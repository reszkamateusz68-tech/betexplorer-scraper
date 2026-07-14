import os
import json
import requests
import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

# ==========================================
# KONFIGURACJA WIADOMOŚCI - MOŻESZ DOWOLNIE ZMIENIAĆ!
# Zmienne w klamrach {} zostaną podmienione przez skrypt.
# ==========================================
SZABLON_NOWEGO_KUPONU = """
🚨 <b>NOWY KUPON SYSTEMOWY</b> 🚨
🆔 <i>{id_kuponu}</i>

{mecze}

📊 <b>Łączny Kurs:</b> {kurs}
💰 <b>Stawka:</b> {stawka} PLN ({jednostki})
"""
# ==========================================

scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

# Pobranie Kuponów
ws_ako = spreadsheet.worksheet("Kupony_AKO")
dane_ako = ws_ako.get_all_values()
df_ako = pd.DataFrame(dane_ako[1:], columns=dane_ako[0])

# Pobranie Predykcji, żeby zobaczyć co wysłać
ws_pred = spreadsheet.worksheet("All_Predictions")
dane_pred = ws_pred.get_all_values()
df_pred = pd.DataFrame(dane_pred[1:], columns=dane_pred[0])

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    requests.post(url, json=payload)

do_wysylki = df_pred[df_pred['Wyslij_AKO'].astype(str).str.upper().isin(['TRUE', 'TAK', '1'])]

if not do_wysylki.empty:
    for kupon_id in do_wysylki['Kupon_ID'].unique():
        kupon_data = df_ako[df_ako['Kupon_ID'] == kupon_id]
        if kupon_data.empty: continue
        
        rekord = kupon_data.iloc[0]
        mecze_df = do_wysylki[do_wysylki['Kupon_ID'] == kupon_id]
        
        lista_meczow_txt = ""
        for _, m in mecze_df.iterrows():
            lista_meczow_txt += f"⚽️ {m['Gospodarz']} vs {m['Gość']}\n👉 Typ: <b>{m['Typ']}</b> (Kurs: {m['Kurs_Szac']})\n"
            
        wiadomosc = SZABLON_NOWEGO_KUPONU.format(
            id_kuponu=kupon_id,
            mecze=lista_meczow_txt,
            kurs=rekord['Kurs_AKO'],
            stawka=rekord['Stawka'],
            jednostki=rekord['Jednostki'] if 'Jednostki' in rekord else "1j"
        )
        
        send_telegram(wiadomosc)
        
    # Odznaczanie po wysłaniu
    komorki_do_odznaczenia = []
    ws_pred_data = ws_pred.get_all_values()
    idx_wyslij = ws_pred_data[0].index("Wyslij_AKO")
    
    for r_idx, row in enumerate(ws_pred_data):
        if row[idx_wyslij].upper() in ['TRUE', 'TAK', '1']:
            komorki_do_odznaczenia.append(gspread.Cell(row=r_idx+1, col=idx_wyslij+1, value="FALSE"))
            
    if komorki_do_odznaczenia:
        ws_pred.update_cells(komorki_do_odznaczenia)
    print("Wysłano powiadomienia i odznaczono checkboxy.")
