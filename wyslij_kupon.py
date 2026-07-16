import os
import json
import requests
import gspread
import pandas as pd
from datetime import datetime
from google.oauth2.service_account import Credentials

# ==========================================
# KONFIGURACJA FINANSOWA I API
# ==========================================
WARTOSC_JEDNOSTKI_PLN = 100.0
PODATEK_BUKMACHERSKI = 0.88

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
💰 Wygrana na czysto: +{zysk_j}j (+{zysk_pln} PLN po odliczeniu podatku)
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
# INICJALIZACJA
# ==========================================
scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file("credentials.json", scopes=scope) if os.path.exists("credentials.json") else Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

ws_pred = spreadsheet.worksheet("All_Predictions")
ws_ako = spreadsheet.worksheet("Kupony_AKO")
ws_hist = spreadsheet.worksheet("Historia_Typow")

# Wczytanie danych z kluczami
df_pred = pd.DataFrame(ws_pred.get_all_records())
df_ako = pd.DataFrame(ws_ako.get_all_records())
df_hist = pd.DataFrame(ws_hist.get_all_records())

# Definicja unikalnego klucza (Match_ID + Typ)
df_pred['Unikalny_Klucz'] = df_pred['Match_ID'].astype(str) + "_" + df_pred['Typ'].astype(str)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    return requests.post(url, json=payload).status_code == 200

# ==========================================
# 1. WYSYŁKA NOWYCH KUPONÓW
# ==========================================
if 'Wyslij_AKO' in df_pred.columns:
    # Pobierz tylko te zaznaczone do wysyłki
    do_wysylki = df_pred[df_pred['Wyslij_AKO'].astype(str).str.upper().isin(['TRUE', 'TAK', '1'])].copy()
    
    if not do_wysylki.empty:
        # Automatyczne nadanie Kupon_ID dla pustych wierszy
        empty_mask = do_wysylki['Kupon_ID'].astype(str).str.strip() == ""
        if empty_mask.any():
            new_id = f"AKO_{datetime.now().strftime('%y%m%d_%H%M')}"
            do_wysylki.loc[empty_mask, 'Kupon_ID'] = new_id
            
            # Zapisz nowe ID do arkusza
            cells = []
            ws_data = ws_pred.get_all_values()
            headers = ws_data[0]
            for _, row in do_wysylki[empty_mask].iterrows():
                # Szukanie wiersza w arkuszu po Match_ID i Typ
                for r_idx, sheet_row in enumerate(ws_data[1:], start=2):
                    if sheet_row[headers.index("Match_ID")] == row['Match_ID'] and sheet_row[headers.index("Typ")] == row['Typ']:
                        cells.append(gspread.Cell(row=r_idx, col=headers.index("Kupon_ID")+1, value=new_id))
            if cells: ws_pred.update_cells(cells)

        wyslane_id = []
        # Przetwarzanie grup
        for kupon_id in do_wysylki['Kupon_ID'].unique():
            mecze_df = do_wysylki[do_wysylki['Kupon_ID'] == kupon_id]
            
            lista_meczow_txt = ""
            dynamic_kurs = 1.0
            
            for _, m in mecze_df.iterrows():
                lista_meczow_txt += f"⚽ {m['Gospodarz']} vs {m['Gość']}\n📅 {m['Data']} ⏰ {m['Godzina']} | 🎯 Typ: <b>{m['Typ']}</b> | 📈 {m['Kurs_Szac']}\n\n"
                try: 
                    k_str = str(m.get('Kurs_Rynek', '')).replace(',', '.')
                    if k_str in ["", "-", "nan", "None"]: k_str = str(m.get('Kurs_Szac', '1.0')).replace(',', '.')
                    k_val = float(k_str)
                    dynamic_kurs *= k_val 
                except: pass
            
            dynamic_kurs = round(dynamic_kurs, 2)
            
            # Pobranie stawki
            stawka_pln = 100.0
            if not df_ako.empty and kupon_id in df_ako['Kupon_ID'].values:
                row = df_ako[df_ako['Kupon_ID'] == kupon_id].iloc[0]
                try: stawka_pln = float(str(row.get('Stawka', '100')).replace(',', '.'))
                except: pass

            stawka_j = round(stawka_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            zysk_pln = round((stawka_pln * PODATEK_BUKMACHERSKI * dynamic_kurs) - stawka_pln, 2)
            zysk_j = round(zysk_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            
            wiadomosc = SZABLON_NOWY.format(
                id_kuponu=kupon_id, mecze=lista_meczow_txt, kurs=f"{dynamic_kurs:.2f}",
                stawka_j=stawka_j, stawka_pln=stawka_pln, wartosc_j=int(WARTOSC_JEDNOSTKI_PLN),
                zysk_j=zysk_j, zysk_pln=zysk_pln
            )
            
            if send_telegram(wiadomosc):
                wyslane_id.append(kupon_id)

        # Odznaczenie wysłanych w arkuszu
        if wyslane_id:
            ws_data = ws_pred.get_all_values()
            headers = ws_data[0]
            cells = []
            for r_idx, row in enumerate(ws_data[1:], start=2):
                if row[headers.index("Kupon_ID")] in wyslane_id:
                    cells.append(gspread.Cell(row=r_idx, col=headers.index("Wyslij_AKO")+1, value="FALSE"))
            if cells: ws_pred.update_cells(cells)
