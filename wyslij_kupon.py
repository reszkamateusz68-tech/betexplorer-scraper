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
💸 Ewentualna wygrana: {wygrana_j}j ({wygrana_pln} PLN po odliczeniu podatku)
"""

SZABLON_WYGRANA = """
✅ <b>KUPON ZAKOŃCZONY ZYSKIEM!</b> ✅

🆔 <i>{id_kuponu}</i>
───────────────
{mecze}───────────────
📈 Łączny kurs: {kurs}
💰 Wypłata: {wygrana_j}j ({wygrana_pln} PLN po odliczeniu podatku)
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

try:
    ws_res = spreadsheet.worksheet("Results")
    df_res = pd.DataFrame(ws_res.get_all_records())
except Exception:
    df_res = pd.DataFrame()

df_pred = pd.DataFrame(ws_pred.get_all_records())
df_ako = pd.DataFrame(ws_ako.get_all_records())
df_hist = pd.DataFrame(ws_hist.get_all_records())

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
# FUNKCJA GENERUJĄCA STATYSTYKI I POWODY PORAŻKI (KULOODPORNA)
# ==========================================
def format_match_details(m_row, df_results):
    match_id = str(m_row.get('Match_ID', '')).strip()
    status = str(m_row.get('Status', 'W OCZEKIWANIU')).upper()
    typ = str(m_row.get('Typ', '')).strip()
    
    if df_results.empty or 'Match_ID' not in df_results.columns:
        return ""
        
    res_match = df_results[df_results['Match_ID'] == match_id]
    if res_match.empty:
        return ""
        
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
    
    stats_str = ""
    sub_bets = [b.strip() for b in typ.split("+") if b.strip()]
    
    if status == "PRZEGRANA":
        reasons = []
        for sub_bet in sub_bets:
            # 1X2 / 1X / X2
            if sub_bet in ["1", "1X"] and pd.notna(hg) and pd.notna(ag) and hg < ag:
                reasons.append(f"Porażka gospodarzy ({int(hg)}:{int(ag)})")
            elif sub_bet == "1" and pd.notna(hg) and pd.notna(ag) and hg == ag:
                reasons.append(f"Remis w meczu ({int(hg)}:{int(ag)})")
            elif sub_bet in ["2", "X2"] and pd.notna(hg) and pd.notna(ag) and hg > ag:
                reasons.append(f"Porażka gości ({int(hg)}:{int(ag)})")
                
            # Gole ogółem
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
                
            # Gole 1H i 2H
            elif sub_bet.startswith("HT_U") and pd.notna(ht_h) and pd.notna(ht_a):
                try:
                    line = float(sub_bet.replace("HT_U", ""))
                    ht_tg = ht_h + ht_a
                    if ht_tg > line: reasons.append(f"Gole w 1. połowie: {int(ht_tg)} (linia: {line})")
                except: pass
            elif sub_bet.startswith("2H_U") and pd.notna(tg) and pd.notna(ht_h) and pd.notna(ht_a):
                try:
                    line = float(sub_bet.replace("2H_U", ""))
                    h2_tg = tg - (ht_h + ht_a)
                    if h2_tg > line: reasons.append(f"Gole w 2. połowie: {int(h2_tg)} (linia: {line})")
                except: pass
                
            # Gole drużyn
            elif sub_bet.startswith("HU") and pd.notna(hg):
                try:
                    line = float(sub_bet.replace("HU", ""))
                    if hg > line: reasons.append(f"Gole gospodarzy: {int(hg)} (linia: {line})")
                except: pass
            elif sub_bet.startswith("AU") and pd.notna(ag):
                try:
                    line = float(sub_bet.replace("AU", ""))
                    if ag > line: reasons.append(f"Gole gości: {int(ag)} (linia: {line})")
                except: pass
                
            # Rożne
            elif sub_bet.startswith("C_U") and pd.notna(tc):
                try:
                    line = float(sub_bet.replace("C_U", ""))
                    if tc > line: reasons.append(f"Suma rzutów rożnych: {int(tc)} (linia: {line})")
                except: pass
            elif sub_bet.startswith("HC_U") and pd.notna(hc):
                try:
                    line = float(sub_bet.replace("HC_U", ""))
                    if hc > line: reasons.append(f"Rożne gospodarzy: {int(hc)} (linia: {line})")
                except: pass
            elif sub_bet.startswith("AC_U") and pd.notna(ac):
                try:
                    line = float(sub_bet.replace("AC_U", ""))
                    if ac > line: reasons.append(f"Rożne gości: {int(ac)} (linia: {line})")
                except: pass
                
            # Strzały
            elif sub_bet == "S_1" and pd.notna(sh) and pd.notna(sa) and sh <= sa:
                reasons.append(f"Strzały ogółem: {int(sh)}:{int(sa)} (brak wygranej gospodarzy)")
            elif sub_bet == "ST_1" and pd.notna(sth) and pd.notna(sta) and sth <= sta:
                reasons.append(f"Strzały celne: {int(sth)}:{int(sta)} (brak wygranej gospodarzy)")

        reasons = list(dict.fromkeys(reasons))

        if reasons:
            stats_str = f"   └ 💡 <i>Powód porażki: {', '.join(reasons)}</i>\n\n"
        elif pd.notna(hg) and pd.notna(ag):
            stats_str = f"   └ 💡 <i>Wynik końcowy: {int(hg)}:{int(ag)}</i>\n\n"

    elif status == "WYGRANA":
        parts = []
        if pd.notna(hg) and pd.notna(ag):
            score_txt = f"Wynik {int(hg)}:{int(ag)}"
            if pd.notna(ht_h) and pd.notna(ht_a):
                score_txt += f" (1H {int(ht_h)}:{int(ht_a)})"
            parts.append(score_txt)
            
        if pd.notna(hc) and pd.notna(ac) and any(k in typ for k in ["C_", "HC_", "AC_"]):
            parts.append(f"Rożne {int(hc)}:{int(ac)}")
            
        if pd.notna(sth) and pd.notna(sta) and "ST_" in typ:
            parts.append(f"Strzały celne {int(sth)}:{int(sta)}")
        elif pd.notna(sh) and pd.notna(sa) and "S_" in typ:
            parts.append(f"Strzały {int(sh)}:{int(sa)}")

        if parts:
            stats_str = f"   └ 📊 <i>Statystyki: {' | '.join(parts)}</i>\n\n"

    return stats_str

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
            
            new_id_map = {}
            for _, r in do_wysylki[empty_mask].iterrows():
                new_id_map[(str(r['Match_ID']), str(r['Engine']), str(r['Typ']))] = new_id
                
            cells_to_update_pred = []
            ws_pred_data = ws_pred.get_all_values()
            if ws_pred_data:
                headers = ws_pred_data[0]
                try:
                    idx_kupon = headers.index("Kupon_ID")
                    idx_match = headers.index("Match_ID")
                    idx_engine = headers.index("Engine")
                    idx_typ = headers.index("Typ")
                    for r_idx, row in enumerate(ws_pred_data[1:], start=2):
                        key = (str(row[idx_match]), str(row[idx_engine]), str(row[idx_typ]))
                        if key in new_id_map:
                            cells_to_update_pred.append(gspread.Cell(row=r_idx, col=idx_kupon+1, value=new_id))
                    if cells_to_update_pred: ws_pred.update_cells(cells_to_update_pred)
                except: pass

            cells_to_update_hist = []
            ws_hist_data = ws_hist.get_all_values()
            if ws_hist_data:
                headers_hist = ws_hist_data[0]
                try:
                    idx_kupon_h = headers_hist.index("Kupon_ID")
                    idx_match_h = headers_hist.index("Match_ID")
                    idx_engine_h = headers_hist.index("Engine")
                    idx_typ_h = headers_hist.index("Typ")
                    for r_idx, row in enumerate(ws_hist_data[1:], start=2):
                        key = (str(row[idx_match_h]), str(row[idx_engine_h]), str(row[idx_typ_h]))
                        if key in new_id_map:
                            cells_to_update_hist.append(gspread.Cell(row=r_idx, col=idx_kupon_h+1, value=new_id))
                    if cells_to_update_hist: ws_hist.update_cells(cells_to_update_hist)
                except: pass

        wyslane_id = []

        for kupon_id in do_wysylki['Kupon_ID'].unique():
            if str(kupon_id).strip() == "": continue
            
            kupon_data = df_ako[df_ako['Kupon_ID'] == kupon_id] if not df_ako.empty else pd.DataFrame()
            mecze_df = do_wysylki[do_wysylki['Kupon_ID'] == kupon_id]
            
            lista_meczow_txt = ""
            dynamic_kurs = 1.0

            for _, m in mecze_df.iterrows():
                k_str = str(m.get('Kurs_Szac', '1.0')).replace(',', '.')
                try: k_val = float(k_str)
                except: k_val = 1.0
                if k_val > 1.0: dynamic_kurs *= k_val 
                    
                data_m = str(m.get('Data', ''))
                godz_m = str(m.get('Godzina', ''))
                lista_meczow_txt += f"⚽ {m['Gospodarz']} vs {m['Gość']}\n📅 {data_m} ⏰ {godz_m} | 🎯 Typ: <b>{m['Typ']}</b> | 📈 {k_val:.2f}\n\n"
            
            dynamic_kurs = round(dynamic_kurs, 2)
            kurs_ako = dynamic_kurs

            if not kupon_data.empty:
                rekord = kupon_data.iloc[0]
                try: stawka_pln = float(str(rekord.get('Stawka', '100')).replace(',', '.'))
                except: stawka_pln = 100.0
            else: stawka_pln = 100.0
            
            stawka_j = round(stawka_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            # CAŁKOWITA WYGRANA BRUTTO PO ODLICZENIU 12% PODATKU
            wygrana_pln = round(stawka_pln * kurs_ako * PODATEK_BUKMACHERSKI, 2)
            wygrana_j = round(wygrana_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            
            wiadomosc = SZABLON_NOWY.format(
                id_kuponu=kupon_id, mecze=lista_meczow_txt, kurs=f"{kurs_ako:.2f}",
                stawka_j=stawka_j, stawka_pln=stawka_pln, wartosc_j=int(WARTOSC_JEDNOSTKI_PLN),
                wygrana_j=wygrana_j, wygrana_pln=wygrana_pln
            )
            
            if send_telegram(wiadomosc): wyslane_id.append(kupon_id)
            
        if wyslane_id:
            komorki_do_odznaczenia = []
            ws_pred_data = ws_pred.get_all_values()
            headers = ws_pred_data[0]
            idx_wyslij = headers.index("Wyslij_AKO")
            idx_kupon = headers.index("Kupon_ID")
            
            for r_idx, row in enumerate(ws_pred_data[1:], start=2):
                if row[idx_wyslij].upper() in ['TRUE', 'TAK', '1'] and row[idx_kupon] in wyslane_id:
                    komorki_do_odznaczenia.append(gspread.Cell(row=r_idx, col=idx_wyslij+1, value="FALSE"))
            if komorki_do_odznaczenia: ws_pred.update_cells(komorki_do_odznaczenia)

# ==========================================
# 2. WYSYŁKA PODSUMOWAŃ
# ==========================================
if 'Telegram_Status' not in df_ako.columns:
    df_ako['Telegram_Status'] = ""
    ws_ako.update([df_ako.columns.values.tolist()] + df_ako.fillna("").values.tolist())

if 'Wyslij_Podsumowanie' in df_ako.columns and 'Status_AKO' in df_ako.columns:
    mask_auto = (df_ako['Status_AKO'].isin(['WYGRANA', 'PRZEGRANA'])) & (df_ako['Telegram_Status'] != 'WYSŁANO')
    mask_manual = (df_ako['Wyslij_Podsumowanie'].astype(str).str.upper().isin(['TRUE', 'TAK', '1']))
    
    do_podsumowania = df_ako[mask_auto | mask_manual]

    if not do_podsumowania.empty:
        komorki_ako_do_aktualizacji = []
        ws_ako_data = ws_ako.get_all_values()
        headers_ako = ws_ako_data[0]
        idx_kupon = headers_ako.index("Kupon_ID")
        idx_tel_status = headers_ako.index("Telegram_Status")
        idx_wyslij_pod = headers_ako.index("Wyslij_Podsumowanie")
        
        for _, rekord in do_podsumowania.iterrows():
            kupon_id = str(rekord['Kupon_ID']).strip()
            if not kupon_id: continue
            
            is_manual = str(rekord.get('Wyslij_Podsumowanie', '')).upper() in ['TRUE', 'TAK', '1']
            
            mecze_hist = df_hist[df_hist['Kupon_ID'].astype(str).str.strip() == kupon_id] if not df_hist.empty and 'Kupon_ID' in df_hist.columns else pd.DataFrame()
            mecze_pred = df_pred[df_pred['Kupon_ID'].astype(str).str.strip() == kupon_id] if not df_pred.empty and 'Kupon_ID' in df_pred.columns else pd.DataFrame()
            
            mecze_df = pd.concat([mecze_hist, mecze_pred])
            if not mecze_df.empty:
                mecze_df['Temp_Key'] = mecze_df['Match_ID'].astype(str) + "_" + mecze_df['Engine'].astype(str) + "_" + mecze_df['Typ'].astype(str)
                mecze_df = mecze_df.drop_duplicates(subset=['Temp_Key'], keep='first')
            
            lista_meczow_txt = ""
            dynamic_kurs = 1.0
            statusy_zdarzen = []
            
            for _, m in mecze_df.iterrows():
                status_meczu = str(m.get('Status', 'W OCZEKIWANIU')).upper()
                statusy_zdarzen.append(status_meczu)
                
                if status_meczu == "WYGRANA": emoji = "🟢"
                elif status_meczu == "PRZEGRANA": emoji = "🔴"
                else: emoji = "⏳" 
                
                k_str = str(m.get('Kurs_Szac', '1.0')).replace(',', '.')
                try: k_val = float(k_str)
                except: k_val = 1.0
                if k_val > 1.0: dynamic_kurs *= k_val
                
                data_m = str(m.get('Data', ''))
                godz_m = str(m.get('Godzina', ''))
                    
                lista_meczow_txt += f"{emoji} {m['Gospodarz']} vs {m['Gość']}\n📅 {data_m} ⏰ {godz_m} | 🎯 Typ: <b>{m['Typ']}</b> | 📈 {k_val:.2f}\n"
                
                detale_txt = format_match_details(m, df_res)
                if detale_txt:
                    lista_meczow_txt += detale_txt
                else:
                    lista_meczow_txt += "\n"
            
            kurs_ako = round(dynamic_kurs, 2)
            if kurs_ako == 1.0:
                try: kurs_ako = float(str(rekord.get('Kurs_AKO', '1.0')).replace(',', '.'))
                except: kurs_ako = 1.0
            
            if "PRZEGRANA" in statusy_zdarzen:
                real_status_ako = "PRZEGRANA"
            elif "W OCZEKIWANIU" in statusy_zdarzen or "DO RĘCZNEJ KONTROLI" in statusy_zdarzen:
                real_status_ako = "W OCZEKIWANIU"
            elif len(statusy_zdarzen) > 0 and all(s == "WYGRANA" for s in statusy_zdarzen):
                real_status_ako = "WYGRANA"
            else:
                real_status_ako = "ZWRÓCONY"

            if not lista_meczow_txt:
                lista_meczow_txt = f"⚽ Zdarzenia dla tego kuponu: <b>{rekord.get('Mecze_Skrot', 'Brak szczegółów w arkuszu')}</b>\n\n"
            
            try: stawka_pln = float(str(rekord.get('Stawka', '100')).replace(',', '.'))
            except: stawka_pln = 100.0
            
            stawka_j = round(stawka_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            
            # CAŁKOWITA WYGRANA BRUTTO PO ODLICZENIU 12% PODATKU
            wygrana_pln = round(kurs_ako * stawka_pln * PODATEK_BUKMACHERSKI, 2)
            wygrana_j = round(wygrana_pln / WARTOSC_JEDNOSTKI_PLN, 2)
            
            if real_status_ako == 'WYGRANA':
                wiadomosc = SZABLON_WYGRANA.format(
                    id_kuponu=kupon_id, mecze=lista_meczow_txt, 
                    kurs=f"{kurs_ako:.2f}", wygrana_j=wygrana_j, wygrana_pln=wygrana_pln
                )
            elif real_status_ako == 'PRZEGRANA':
                wiadomosc = SZABLON_PRZEGRANA.format(
                    id_kuponu=kupon_id, mecze=lista_meczow_txt, 
                    kurs=f"{kurs_ako:.2f}", stawka_j=f"-{stawka_j}", stawka_pln=f"-{stawka_pln}"
                )
            else:
                wiadomosc = SZABLON_OCZEKUJE.format(
                    id_kuponu=kupon_id, mecze=lista_meczow_txt, 
                    kurs=f"{kurs_ako:.2f}", stawka_j=stawka_j, stawka_pln=stawka_pln, 
                    wygrana_j=wygrana_j, wygrana_pln=wygrana_pln
                )
                
            if send_telegram(wiadomosc):
                for r_idx, row in enumerate(ws_ako_data):
                    if row[idx_kupon] == kupon_id:
                        if is_manual: 
                            komorki_ako_do_aktualizacji.append(gspread.Cell(row=r_idx+1, col=idx_wyslij_pod+1, value="FALSE"))
                        
                        if str(row[headers_ako.index("Status_AKO")]) != real_status_ako:
                            komorki_ako_do_aktualizacji.append(gspread.Cell(row=r_idx+1, col=headers_ako.index("Status_AKO")+1, value=real_status_ako))
                        if str(row[headers_ako.index("Kurs_AKO")]) != str(kurs_ako):
                             komorki_ako_do_aktualizacji.append(gspread.Cell(row=r_idx+1, col=headers_ako.index("Kurs_AKO")+1, value=kurs_ako))
                        
                        if real_status_ako in ['WYGRANA', 'PRZEGRANA']:
                            komorki_ako_do_aktualizacji.append(gspread.Cell(row=r_idx+1, col=idx_tel_status+1, value="WYSŁANO"))
                        break

        if komorki_ako_do_aktualizacji:
            ws_ako.update_cells(komorki_ako_do_aktualizacji)
            print("Pomyślnie wysłano podsumowania i zaktualizowano arkusz.")
