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

# Nowy Klucz Zabezpieczający (Match_ID + Engine + Typ)
if not df_pred.empty:
    df_pred['Unikalny_Klucz'] = df_pred['Match_ID'].astype(str) + "_" + df_pred['Engine'].astype(str) + "_" + df_pred['Typ'].astype(str)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Brak danych uwierzytelniających Telegram (Token / Chat_ID).")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        print(f"Błąd wysyłki Telegram: {response.text}")
        return False
    return True

# ==========================================
# 1. WYSYŁKA NOWYCH KUPONÓW
# ==========================================
if 'Wyslij_AKO' in df_pred.columns:
    do_wysylki = df_pred[df_pred['Wyslij_AKO'].astype(str).str.upper().isin(['TRUE', 'TAK', '1'])].copy()
    
    if not do_wysylki.empty:
        print(f"Znaleziono {len(do_wysylki)} wytypowanych wierszy do wysyłki jako Nowe.")
        
        # 1a. AUTOMATYCZNE GENEROWANIE KUPON_ID JEŚLI JEST PUSTE PRZED WYSŁANIEM
        empty_mask = do_wysylki['Kupon_ID'].astype(str).str.strip() == ""
        if empty_mask.any():
            new_id = f"AKO_{datetime.now().strftime('%y%m%d_%H%M')}"
            print(f"Automatyczne nadanie nowego Kupon_ID: {new_id} dla {empty_mask.sum()} meczów bez ID.")
            do_wysylki.loc[empty_mask, 'Kupon_ID'] = new_id
            
            cells_to_update = []
            ws_pred_data = ws_pred.get_all_values()
            headers = ws_pred_data[0]
            idx_kupon = headers.index("Kupon_ID")
            idx_match = headers.index("Match_ID")
            idx_engine = headers.index("Engine")
            idx_typ = headers.index("Typ")
            
            new_id_map = {}
            for _, r in do_wysylki[empty_mask].iterrows():
                new_id_map[(str(r['Match_ID']), str(r['Engine']), str(r['Typ']))] = new_id
                
            for r_idx, row in enumerate(ws_pred_data[1:], start=2):
                key = (str(row[idx_match]), str(row[idx_engine]), str(row[idx_typ]))
                if key in new_id_map:
                    cells_to_update.append(gspread.Cell(row=r_idx, col=idx_kupon+1, value=new_id))
            
            if cells_to_update:
                ws_pred.update_cells(cells_to_update)

        wyslane_id = []

        for kupon_id in do_wysylki['Kupon_ID'].unique():
            if str(kupon_id).strip() == "": continue
            
            kupon_data = df_ako[df_ako['Kupon_ID'] == kupon_id] if not df_ako.empty else pd.DataFrame()
            mecze_df = do_wysylki[do_wysylki['Kupon_ID'] == kupon_id]
            
            lista_meczow_txt = ""
            dynamic_kurs = 1.0

            for _, m in mecze_df.iterrows():
                # UŻYWAMY TYLKO I WYŁĄCZNIE KURS_SZAC
                k_str = str(m.get('Kurs_Szac', '1.0')).replace(',', '.')
                try: k_val = float(k_str)
                except: k_val = 1.0
                
                if 1.0 < k_val < 50.0:
                    dynamic_kurs *= k_val 
                    
                lista_meczow_txt += f"⚽ {m['Gospodarz']} vs {m['Gość']}\n📅 {m['Data']} ⏰ {m['Godzina']} | 🎯 Typ: <b>{m['Typ']}</b> | 📈 {k_val:.2f}\n\n"
            
            dynamic_kurs = round(dynamic_kurs, 2)

            if not kupon_data.empty:
                rekord = kupon_data.iloc[0]
                try: stawka_pln = float(str(rekord.get('Stawka', '100')).replace(',', '.'))
                except: stawka_pln = 100.0
            else:
                stawka_pln = 100.0
            
            stawka_j = round(stawka_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            zysk_pln = round((stawka_pln * PODATEK_BUKMACHERSKI * dynamic_kurs) - stawka_pln, 2)
            zysk_j = round(zysk_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            
            wiadomosc = SZABLON_NOWY.format(
                id_kuponu=kupon_id,
                mecze=lista_meczow_txt,
                kurs=f"{dynamic_kurs:.2f}",
                stawka_j=stawka_j,
                stawka_pln=stawka_pln,
                wartosc_j=int(WARTOSC_JEDNOSTKI_PLN),
                zysk_j=zysk_j,
                zysk_pln=zysk_pln
            )
            
            if send_telegram(wiadomosc):
                wyslane_id.append(kupon_id)
            
        if wyslane_id:
            komorki_do_odznaczenia = []
            ws_pred_data = ws_pred.get_all_values()
            headers = ws_pred_data[0]
            idx_wyslij = headers.index("Wyslij_AKO")
            idx_kupon = headers.index("Kupon_ID")
            
            for r_idx, row in enumerate(ws_pred_data[1:], start=2):
                if row[idx_wyslij].upper() in ['TRUE', 'TAK', '1'] and row[idx_kupon] in wyslane_id:
                    komorki_do_odznaczenia.append(gspread.Cell(row=r_idx, col=idx_wyslij+1, value="FALSE"))
                    
            if komorki_do_odznaczenia:
                ws_pred.update_cells(komorki_do_odznaczenia)
                print(f"Pomyślnie odznaczono {len(wyslane_id)} wysłane kupony w arkuszu.")

# ==========================================
# 2. WYSYŁKA PODSUMOWAŃ (ROZLICZONE LUB W OCZEKIWANIU)
# ==========================================
if 'Telegram_Status' not in df_ako.columns:
    df_ako['Telegram_Status'] = ""
    ws_ako.update([df_ako.columns.values.tolist()] + df_ako.fillna("").values.tolist())

if 'Wyslij_Podsumowanie' in df_ako.columns and 'Status_AKO' in df_ako.columns:
    mask_auto = (df_ako['Status_AKO'].isin(['WYGRANA', 'PRZEGRANA'])) & (df_ako['Telegram_Status'] != 'WYSŁANO')
    mask_manual = (df_ako['Wyslij_Podsumowanie'].astype(str).str.upper().isin(['TRUE', 'TAK', '1']))
    
    do_podsumowania = df_ako[mask_auto | mask_manual]

    if not do_podsumowania.empty:
        print(f"Znaleziono {len(do_podsumowania)} kuponów do podsumowania/podglądu.")
        
        komorki_ako_do_aktualizacji = []
        ws_ako_data = ws_ako.get_all_values()
        headers_ako = ws_ako_data[0]
        idx_kupon = headers_ako.index("Kupon_ID")
        idx_tel_status = headers_ako.index("Telegram_Status")
        idx_wyslij_pod = headers_ako.index("Wyslij_Podsumowanie")
        
        for _, rekord in do_podsumowania.iterrows():
            kupon_id = str(rekord['Kupon_ID']).strip()
            if not kupon_id: continue
            
            # INTELIGENTNY FALLBACK DANYCH
            mecze_df = pd.DataFrame()
            if not df_hist.empty and 'Kupon_ID' in df_hist.columns:
                mecze_df = df_hist[df_hist['Kupon_ID'].astype(str).str.strip() == kupon_id]
            
            if mecze_df.empty and not df_pred.empty and 'Kupon_ID' in df_pred.columns:
                mecze_df = df_pred[df_pred['Kupon_ID'].astype(str).str.strip() == kupon_id]
            
            lista_meczow_txt = ""
            for _, m in mecze_df.iterrows():
                status_meczu = str(m.get('Status', 'W OCZEKIWANIU')).upper()
                if status_meczu == "WYGRANA": emoji = "🟢"
                elif status_meczu == "PRZEGRANA": emoji = "🔴"
                else: emoji = "⏳" 
                
                # Tylko Szacunkowy Kurs z bazy wyliczeń
                k_str = str(m.get('Kurs_Szac', '1.0')).replace(',', '.')
                try: k_val = float(k_str)
                except: k_val = 1.0
                
                lista_meczow_txt += f"{emoji} {m['Gospodarz']} - {m['Gość']} | Typ: <b>{m['Typ']}</b> | 📈 {k_val:.2f}\n"
            
            if not lista_meczow_txt:
                lista_meczow_txt = f"⚽ Zdarzenia dla tego kuponu: <b>{rekord.get('Mecze_Skrot', 'Brak szczegółów w arkuszu')}</b>\n\n"
            
            try: stawka_pln = float(str(rekord.get('Stawka', '100')).replace(',', '.'))
            except: stawka_pln = 100.0
            
            try: kurs_ako = float(str(rekord.get('Kurs_AKO', '1.0')).replace(',', '.'))
            except: kurs_ako = 1.0
            
            stawka_j = round(stawka_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            zysk_pln = round((stawka_pln * PODATEK_BUKMACHERSKI * kurs_ako) - stawka_pln, 2)
            zysk_j = round(zysk_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            
            is_manual = str(rekord.get('Wyslij_Podsumowanie', '')).upper() in ['TRUE', 'TAK', '1']
            
            if rekord['Status_AKO'] == 'WYGRANA':
                wiadomosc = SZABLON_WYGRANA.format(
                    id_kuponu=kupon_id, mecze=lista_meczow_txt, 
                    kurs=f"{kurs_ako:.2f}", zysk_j=zysk_j, zysk_pln=zysk_pln
                )
            elif rekord['Status_AKO'] == 'PRZEGRANA':
                wiadomosc = SZABLON_PRZEGRANA.format(
                    id_kuponu=kupon_id, mecze=lista_meczow_txt, 
                    kurs=f"{kurs_ako:.2f}", stawka_j=stawka_j, stawka_pln=stawka_pln
                )
            else:
                wiadomosc = SZABLON_OCZEKUJE.format(
                    id_kuponu=kupon_id, mecze=lista_meczow_txt, 
                    kurs=f"{kurs_ako:.2f}", stawka_j=stawka_j, stawka_pln=stawka_pln, 
                    zysk_j=zysk_j, zysk_pln=zysk_pln
                )
                
            if send_telegram(wiadomosc):
                for r_idx, row in enumerate(ws_ako_data):
                    if row[idx_kupon] == kupon_id:
                        if is_manual: 
                            komorki_ako_do_aktualizacji.append(gspread.Cell(row=r_idx+1, col=idx_wyslij_pod+1, value="FALSE"))
                        if rekord['Status_AKO'] in ['WYGRANA', 'PRZEGRANA']:
                            komorki_ako_do_aktualizacji.append(gspread.Cell(row=r_idx+1, col=idx_tel_status+1, value="WYSŁANO"))
                        break

        if komorki_ako_do_aktualizacji:
            ws_ako.update_cells(komorki_ako_do_aktualizacji)
            print("Pomyślnie wysłano podsumowania (Oczekujące/Zakończone) i zaktualizowano arkusz.")
