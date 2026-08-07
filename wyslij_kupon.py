import os
import json
import time
import requests
import gspread
import pandas as pd
from datetime import datetime
from google.oauth2.service_account import Credentials

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
💸 Ewentualna wygrana: {wygrana_j}j ({wygrana_pln} PLN po odliczeniu podatku)
"""

SZABLON_WYGRANA = """
✅ <b>KUPON ZAKOŃCZONY ZYSKIEM!</b> ✅

🆔 <i>{id_kuponu}</i>
───────────────
{mecze}───────────────
📈 Łączny kurs: {kurs}
💰 Wygrana: {wygrana_j}j ({wygrana_pln} PLN po odliczeniu podatku)
"""

SZABLON_PRZEGRANA = """
❌ <b>KUPON ZAKOŃCZONY PORAŻKĄ</b> ❌

🆔 <i>{id_kuponu}</i>
───────────────
{mecze}───────────────
📈 Łączny kurs: {kurs}
📉 Strata: {stawka_j}j ({stawka_pln} PLN)
"""

SZABLON_OCZEKUJE = """
⏳ <b>KUPON W GRZE (OCZEKUJE)</b> ⏳

🆔 <i>{id_kuponu}</i>
───────────────
{mecze}───────────────
📊 <b>Status Kuponu:</b>
📈 Łączny kurs: {kurs}
💰 Stawka: {stawka_j}j ({stawka_pln} PLN)
💸 Potencjalna wygrana: {wygrana_j}j ({wygrana_pln} PLN po odliczeniu podatku)
"""

# INICJALIZACJA GOOGLE SHEETS
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

try:
    ws_res = spreadsheet.worksheet("Results")
    df_res = pd.DataFrame(ws_res.get_all_records())
except Exception: df_res = pd.DataFrame()

df_pred = pd.DataFrame(ws_pred.get_all_records())
df_ako = pd.DataFrame(ws_ako.get_all_records())
df_hist = pd.DataFrame(ws_hist.get_all_records())

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Brak danych uwierzytelniających Telegram.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Błąd wysyłki Telegram: {e}")
        return False

def format_match_details(m_row, df_results):
    match_id = str(m_row.get('Match_ID', '')).strip()
    status = str(m_row.get('Status', 'W OCZEKIWANIU')).upper()
    typ = str(m_row.get('Typ', '')).strip()
    if df_results.empty or 'Match_ID' not in df_results.columns: return ""
    res_match = df_results[df_results['Match_ID'] == match_id]
    if res_match.empty: return ""
    
    r = res_match.iloc[0]
    hg = pd.to_numeric(r.get('FTHG', None), errors='coerce')
    ag = pd.to_numeric(r.get('FTAG', None), errors='coerce')
    tg = pd.to_numeric(r.get('Total_Goals', None), errors='coerce')
    ht_h = pd.to_numeric(r.get('HTHG', None), errors='coerce')
    ht_a = pd.to_numeric(r.get('HTAG', None), errors='coerce')
    hc = pd.to_numeric(r.get('Corners_H', None), errors='coerce')
    ac = pd.to_numeric(r.get('Corners_A', None), errors='coerce')
    tc = pd.to_numeric(r.get('Total_Corners', None), errors='coerce')
    sh = pd.to_numeric(r.get('Shots_H', None), errors='coerce')
    sa = pd.to_numeric(r.get('Shots_A', None), errors='coerce')
    sth = pd.to_numeric(r.get('ShotsTarget_H', None), errors='coerce')
    sta = pd.to_numeric(r.get('ShotsTarget_A', None), errors='coerce')
    
    sub_bets = [b.strip() for b in typ.split("+") if b.strip()]
    if status == "PRZEGRANA":
        reasons = []
        for sub_bet in sub_bets:
            if sub_bet in ["1", "1X"] and pd.notna(hg) and pd.notna(ag) and hg < ag: reasons.append(f"Porażka gospodarzy ({int(hg)}:{int(ag)})")
            elif sub_bet == "1" and pd.notna(hg) and pd.notna(ag) and hg == ag: reasons.append(f"Remis w meczu ({int(hg)}:{int(ag)})")
            elif sub_bet in ["2", "X2"] and pd.notna(hg) and pd.notna(ag) and hg > ag: reasons.append(f"Porażka gości ({int(hg)}:{int(ag)})")
            elif sub_bet.startswith("U") and not sub_bet.startswith(("HT_U", "2H_U", "HU", "AU", "C_U", "HC_U", "AC_U")) and pd.notna(tg):
                try:
                    line = float(sub_bet[1:])
                    if tg > line: reasons.append(f"Łącznie goli: {int(tg)} (linia: {line})")
                except: pass
            elif sub_bet.startswith("O") and not sub_bet.startswith(("HC_O", "AC_O")) and pd.notna(tg):
                try:
                    line = float(sub_bet[1:])
                    if tg < line: reasons.append(f"Łącznie goli: {int(tg)} (wymagano: ponad {line})")
                except: pass
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            wynik_txt = f"Wynik końcowy: {int(hg)}:{int(ag)}" if pd.notna(hg) and pd.notna(ag) else "Wynik nieznany"
            return f"   └ 💡 <i>{wynik_txt} | Powód porażki: {', '.join(reasons)}</i>\n\n"
        elif pd.notna(hg) and pd.notna(ag):
            return f"   └ 💡 <i>Wynik końcowy: {int(hg)}:{int(ag)}</i>\n\n"

    elif status == "WYGRANA":
        parts = []
        if pd.notna(hg) and pd.notna(ag):
            score_txt = f"Wynik {int(hg)}:{int(ag)}"
            if pd.notna(ht_h) and pd.notna(ht_a): score_txt += f" (1H {int(ht_h)}:{int(ht_a)})"
            parts.append(score_txt)
        if parts: return f"   └ 📊 <i>Statystyki: {' | '.join(parts)}</i>\n\n"
    return ""

def prepare_for_gsheets(df):
    df = df.astype(str)
    output = [df.columns.tolist()]
    for row in df.values.tolist():
        new_row = ["" if pd.isna(val) or str(val).strip() in ["nan", "<NA>", "NaN", "None"] else str(val).strip() for val in row]
        output.append(new_row)
    return output

# ==========================================
# 1. WYSYŁKA NOWYCH KUPONÓW (FAST BATCH)
# ==========================================
if 'Wyslij_AKO' in df_pred.columns:
    do_wysylki = df_pred[df_pred['Wyslij_AKO'].astype(str).str.upper().isin(['TRUE', 'TAK', '1'])].copy()
    
    if not do_wysylki.empty:
        empty_mask = do_wysylki['Kupon_ID'].astype(str).str.strip() == ""
        if empty_mask.any():
            new_id = f"AKO_{datetime.now().strftime('%y%m%d_%H%M')}"
            do_wysylki.loc[empty_mask, 'Kupon_ID'] = new_id

        wyslane_ids = []
        for kupon_id in do_wysylki['Kupon_ID'].unique():
            if str(kupon_id).strip() == "": continue
            kupon_data = df_ako[df_ako['Kupon_ID'] == kupon_id] if not df_ako.empty else pd.DataFrame()
            mecze_df = do_wysylki[do_wysylki['Kupon_ID'] == kupon_id]
            
            lista_meczow_txt = ""
            dynamic_kurs = 1.0
            for _, m in mecze_df.iterrows():
                try: k_val = float(str(m.get('Kurs_Szac', '1.0')).replace(',', '.'))
                except: k_val = 1.0
                if k_val > 1.0: dynamic_kurs *= k_val
                lista_meczow_txt += f"⚽ {m['Gospodarz']} vs {m['Gość']}\n📅 {m.get('Data','')} ⏰ {m.get('Godzina','')} | 🎯 Typ: <b>{m['Typ']}</b> | 📈 {k_val:.2f}\n\n"
            
            kurs_ako = round(dynamic_kurs, 2)
            stawka_pln = float(str(kupon_data.iloc[0].get('Stawka', '100')).replace(',', '.')) if not kupon_data.empty else 100.0
            stawka_j = round(stawka_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            wygrana_pln = round(stawka_pln * kurs_ako * PODATEK_BUKMACHERSKI, 2)
            wygrana_j = round(wygrana_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            
            wiadomosc = SZABLON_NOWY.format(
                id_kuponu=kupon_id, mecze=lista_meczow_txt, kurs=f"{kurs_ako:.2f}",
                stawka_j=stawka_j, stawka_pln=stawka_pln, wartosc_j=int(WARTOSC_JEDNOSTKI_PLN),
                wygrana_j=wygrana_j, wygrana_pln=wygrana_pln
            )
            
            if send_telegram(wiadomosc):
                wyslane_ids.append(kupon_id)
                print(f"Wysłano powiadomienie Telegram dla {kupon_id}")

        # Odznaczamy Wyslij_AKO na FALSE hurtowo
        if wyslane_ids:
            df_pred.loc[df_pred['Kupon_ID'].isin(wyslane_ids), 'Wyslij_AKO'] = "FALSE"
            ws_pred.clear()
            ws_pred.update(prepare_for_gsheets(df_pred))

# ==========================================
# 2. WYSYŁKA PODSUMOWAŃ (FAST BATCH)
# ==========================================
if 'Telegram_Status' not in df_ako.columns: df_ako['Telegram_Status'] = ""

if 'Wyslij_Podsumowanie' in df_ako.columns and 'Status_AKO' in df_ako.columns:
    mask_auto = (df_ako['Status_AKO'].isin(['WYGRANA', 'PRZEGRANA'])) & (df_ako['Telegram_Status'] != 'WYSŁANO')
    mask_manual = (df_ako['Wyslij_Podsumowanie'].astype(str).str.upper().isin(['TRUE', 'TAK', '1']))
    do_podsumowania = df_ako[mask_auto | mask_manual]

    if not do_podsumowania.empty:
        zaktualizowane_ako_ids = []
        for _, rekord in do_podsumowania.iterrows():
            kupon_id = str(rekord['Kupon_ID']).strip()
            if not kupon_id: continue
            
            mecze_hist = df_hist[df_hist['Kupon_ID'].astype(str).str.strip() == kupon_id] if not df_hist.empty and 'Kupon_ID' in df_hist.columns else pd.DataFrame()
            mecze_pred = df_pred[df_pred['Kupon_ID'].astype(str).str.strip() == kupon_id] if not df_pred.empty and 'Kupon_ID' in df_pred.columns else pd.DataFrame()
            mecze_df = pd.concat([mecze_hist, mecze_pred]).drop_duplicates(subset=['Match_ID', 'Engine', 'Typ'])
            
            lista_meczow_txt = ""
            dynamic_kurs = 1.0
            statusy_zdarzen = []
            
            for _, m in mecze_df.iterrows():
                status_meczu = str(m.get('Status', 'W OCZEKIWANIU')).upper()
                statusy_zdarzen.append(status_meczu)
                emoji = "🟢" if status_meczu == "WYGRANA" else ("🔴" if status_meczu == "PRZEGRANA" else "⏳")
                
                try: k_val = float(str(m.get('Kurs_Szac', '1.0')).replace(',', '.'))
                except: k_val = 1.0
                if k_val > 1.0: dynamic_kurs *= k_val
                
                lista_meczow_txt += f"{emoji} {m['Gospodarz']} vs {m['Gość']}\n📅 {m.get('Data','')} ⏰ {m.get('Godzina','')} | 🎯 Typ: <b>{m['Typ']}</b> | 📈 {k_val:.2f}\n"
                detale = format_match_details(m, df_res)
                lista_meczow_txt += detale if detale else "\n"
            
            kurs_ako = round(dynamic_kurs, 2)
            real_status_ako = "PRZEGRANA" if "PRZEGRANA" in statusy_zdarzen else ("W OCZEKIWANIU" if "W OCZEKIWANIU" in statusy_zdarzen else "WYGRANA")
            
            stawka_pln = float(str(rekord.get('Stawka', '100')).replace(',', '.'))
            stawka_j = round(stawka_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            wygrana_pln = round(kurs_ako * stawka_pln * PODATEK_BUKMACHERSKI, 2)
            wygrana_j = round(wygrana_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            
            if real_status_ako == 'WYGRANA':
                wiadomosc = SZABLON_WYGRANA.format(id_kuponu=kupon_id, mecze=lista_meczow_txt, kurs=f"{kurs_ako:.2f}", wygrana_j=wygrana_j, wygrana_pln=wygrana_pln)
            elif real_status_ako == 'PRZEGRANA':
                wiadomosc = SZABLON_PRZEGRANA.format(id_kuponu=kupon_id, mecze=lista_meczow_txt, kurs=f"{kurs_ako:.2f}", stawka_j=f"-{stawka_j}", stawka_pln=f"-{stawka_pln}")
            else:
                wiadomosc = SZABLON_OCZEKUJE.format(id_kuponu=kupon_id, mecze=lista_meczow_txt, kurs=f"{kurs_ako:.2f}", stawka_j=stawka_j, stawka_pln=stawka_pln, wygrana_j=wygrana_j, wygrana_pln=wygrana_pln)
                
            if send_telegram(wiadomosc):
                idx_ako = df_ako[df_ako['Kupon_ID'] == kupon_id].index
                df_ako.loc[idx_ako, 'Wyslij_Podsumowanie'] = "FALSE"
                df_ako.loc[idx_ako, 'Status_AKO'] = real_status_ako
                df_ako.loc[idx_ako, 'Kurs_AKO'] = str(kurs_ako)
                if real_status_ako in ['WYGRANA', 'PRZEGRANA']:
                    df_ako.loc[idx_ako, 'Telegram_Status'] = "WYSŁANO"
                zaktualizowane_ako_ids.append(kupon_id)

        if zaktualizowane_ako_ids:
            ws_ako.clear()
            ws_ako.update(prepare_for_gsheets(df_ako))
            print("Pomyślnie wysłano podsumowania i zaktualizowano arkusz Kupony_AKO.")
