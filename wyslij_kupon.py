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

SZABLON_OCZEKUJE = """
⏳ <b>KUPON W GRZE (OCZEKUJE)</b> ⏳

🆔 <i>{id_kuponu}</i>
───────────────
{mecze}───────────────
📊 <b>Status Kuponu:</b>
📈 Łączny kurs: {kurs}
💰 Stawka: {stawka_j}j ({stawka_pln} PLN)
💸 Potencjalna wygrana: +{zysk_j}j (+{zysk_pln} PLN po odliczeniu podatku)
"""

scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file("credentials.json", scopes=scope) if os.path.exists("credentials.json") else Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)
client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

ws_pred = spreadsheet.worksheet("All_Predictions")
ws_ako = spreadsheet.worksheet("Kupony_AKO")
ws_hist = spreadsheet.worksheet("Historia_Typow")

df_pred = pd.DataFrame(ws_pred.get_all_records())
df_ako = pd.DataFrame(ws_ako.get_all_records())
df_hist = pd.DataFrame(ws_hist.get_all_records())

# Nowy Klucz (Match_ID + Engine + Typ) 
if not df_pred.empty:
    df_pred['Unikalny_Klucz'] = df_pred['Match_ID'].astype(str) + "_" + df_pred['Engine'].astype(str) + "_" + df_pred['Typ'].astype(str)

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
    do_wysylki = df_pred[df_pred['Wyslij_AKO'].astype(str).str.upper().isin(['TRUE', 'TAK', '1'])].copy()
    
    if not do_wysylki.empty:
        empty_mask = do_wysylki['Kupon_ID'].astype(str).str.strip() == ""
        if empty_mask.any():
            new_id = f"AKO_{datetime.now().strftime('%y%m%d_%H%M')}"
            do_wysylki.loc[empty_mask, 'Kupon_ID'] = new_id
            
            cells_to_update = []
            ws_pred_data = ws_pred.get_all_values()
            headers = ws_pred_data[0]
            for _, r in do_wysylki[empty_mask].iterrows():
                for r_idx, row in enumerate(ws_pred_data[1:], start=2):
                    if row[headers.index("Match_ID")] == str(r['Match_ID']) and row[headers.index("Engine")] == str(r['Engine']) and row[headers.index("Typ")] == str(r['Typ']):
                        cells_to_update.append(gspread.Cell(row=r_idx, col=headers.index("Kupon_ID")+1, value=new_id))
            if cells_to_update: ws_pred.update_cells(cells_to_update)

        wyslane_id = []
        for kupon_id in do_wysylki['Kupon_ID'].unique():
            if str(kupon_id).strip() == "": continue
            mecze_df = do_wysylki[do_wysylki['Kupon_ID'] == kupon_id]
            
            lista_meczow_txt = ""
            dynamic_kurs = 1.0
            for _, m in mecze_df.iterrows():
                k_val = float(str(m.get('Kurs_Szac', '1.0')).replace(',', '.'))
                if 1.0 < k_val < 50.0: dynamic_kurs *= k_val
                lista_meczow_txt += f"⚽ {m['Gospodarz']} vs {m['Gość']}\n📅 {m['Data']} ⏰ {m['Godzina']} | 🎯 Typ: <b>{m['Typ']}</b> | 📈 {k_val:.2f}\n\n"
            
            dynamic_kurs = round(dynamic_kurs, 2)
            
            kupon_data = df_ako[df_ako['Kupon_ID'] == kupon_id] if not df_ako.empty else pd.DataFrame()
            stawka_pln = float(str(kupon_data.iloc[0].get('Stawka', '100')).replace(',', '.')) if not kupon_data.empty else 100.0
            
            stawka_j = round(stawka_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            zysk_pln = round((stawka_pln * PODATEK_BUKMACHERSKI * dynamic_kurs) - stawka_pln, 2)
            zysk_j = round(zysk_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            
            wiadomosc = SZABLON_NOWY.format(
                id_kuponu=kupon_id, mecze=lista_meczow_txt, kurs=f"{dynamic_kurs:.2f}",
                stawka_j=stawka_j, stawka_pln=stawka_pln, wartosc_j=int(WARTOSC_JEDNOSTKI_PLN),
                zysk_j=zysk_j, zysk_pln=zysk_pln
            )
            if send_telegram(wiadomosc): wyslane_id.append(kupon_id)
            
        if wyslane_id:
            ws_pred_data = ws_pred.get_all_values()
            headers = ws_pred_data[0]
            komorki = []
            for r_idx, row in enumerate(ws_pred_data[1:], start=2):
                if row[headers.index("Kupon_ID")] in wyslane_id:
                    komorki.append(gspread.Cell(row=r_idx, col=headers.index("Wyslij_AKO")+1, value="FALSE"))
            if komorki: ws_pred.update_cells(komorki)

# ==========================================
# 2. WYSYŁKA PODSUMOWAŃ (ROZLICZONE LUB OCZEKUJĄCE)
# ==========================================
if 'Wyslij_Podsumowanie' in df_ako.columns and 'Status_AKO' in df_ako.columns:
    mask_auto = (df_ako['Status_AKO'].isin(['WYGRANA', 'PRZEGRANA'])) & (df_ako['Telegram_Status'] != 'WYSŁANO')
    mask_manual = (df_ako['Wyslij_Podsumowanie'].astype(str).str.upper().isin(['TRUE', 'TAK', '1']))
    do_podsumowania = df_ako[mask_auto | mask_manual]

    if not do_podsumowania.empty:
        komorki_ako = []
        ws_ako_data = ws_ako.get_all_values()
        headers_ako = ws_ako_data[0]
        
        for _, rekord in do_podsumowania.iterrows():
            kupon_id = str(rekord['Kupon_ID']).strip()
            if not kupon_id: continue
            
            mecze_df = df_hist[df_hist['Kupon_ID'].astype(str).str.strip() == kupon_id] if not df_hist.empty else pd.DataFrame()
            if mecze_df.empty and not df_pred.empty:
                mecze_df = df_pred[df_pred['Kupon_ID'].astype(str).str.strip() == kupon_id]
            
            lista_meczow_txt = ""
            for _, m in mecze_df.iterrows():
                status_meczu = str(m.get('Status', 'W OCZEKIWANIU')).upper()
                emoji = "🟢" if status_meczu == "WYGRANA" else ("🔴" if status_meczu == "PRZEGRANA" else "⏳")
                k_val = float(str(m.get('Kurs_Szac', '1.0')).replace(',', '.'))
                lista_meczow_txt += f"{emoji} {m['Gospodarz']} - {m['Gość']} | Typ: <b>{m['Typ']}</b> | 📈 {k_val:.2f}\n"
            
            if not lista_meczow_txt:
                lista_meczow_txt = f"⚽ Zdarzenia dla tego kuponu: <b>{rekord.get('Mecze_Skrot', 'Zarchiwizowane')}</b>\n\n"
            
            stawka_pln = float(str(rekord.get('Stawka', '100')).replace(',', '.'))
            kurs_ako = float(str(rekord.get('Kurs_AKO', '1.0')).replace(',', '.'))
            stawka_j = round(stawka_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            zysk_pln = round((stawka_pln * PODATEK_BUKMACHERSKI * kurs_ako) - stawka_pln, 2)
            zysk_j = round(zysk_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            
            if rekord['Status_AKO'] == 'WYGRANA':
                wiadomosc = SZABLON_WYGRANA.format(id_kuponu=kupon_id, mecze=lista_meczow_txt, kurs=f"{kurs_ako:.2f}", zysk_j=zysk_j, zysk_pln=zysk_pln)
            elif rekord['Status_AKO'] == 'PRZEGRANA':
                wiadomosc = SZABLON_PRZEGRANA.format(id_kuponu=kupon_id, mecze=lista_meczow_txt, kurs=f"{kurs_ako:.2f}", stawka_j=stawka_j, stawka_pln=stawka_pln)
            else:
                wiadomosc = SZABLON_OCZEKUJE.format(id_kuponu=kupon_id, mecze=lista_meczow_txt, kurs=f"{kurs_ako:.2f}", stawka_j=stawka_j, stawka_pln=stawka_pln, zysk_j=zysk_j, zysk_pln=zysk_pln)
                
            if send_telegram(wiadomosc):
                for r_idx, row in enumerate(ws_ako_data):
                    if row[headers_ako.index("Kupon_ID")] == kupon_id:
                        if str(rekord.get('Wyslij_Podsumowanie', '')).upper() in ['TRUE', 'TAK', '1']:
                            komorki_ako.append(gspread.Cell(row=r_idx+1, col=headers_ako.index("Wyslij_Podsumowanie")+1, value="FALSE"))
                        if rekord['Status_AKO'] in ['WYGRANA', 'PRZEGRANA']:
                            komorki_ako.append(gspread.Cell(row=r_idx+1, col=headers_ako.index("Telegram_Status")+1, value="WYSŁANO"))
                        break
        if komorki_ako: ws_ako.update_cells(komorki_ako)
