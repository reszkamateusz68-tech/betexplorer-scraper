import os
import json
import gspread
import requests
import cloudscraper
import pandas as pd
import re
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

today = datetime.now()

def split_datetime(value):
    if pd.isna(value): return None, ""
    value = str(value).strip()

    if value.lower().startswith("today"): return today.date(), value.replace("Today", "").strip()
    if value.lower().startswith("tomorrow"): return (today.date() + timedelta(days=1), value.replace("Tomorrow", "").strip())
    if value.lower().startswith("yesterday"): return (today.date() - timedelta(days=1), value.replace("Yesterday", "").strip())

    try:
        parts = value.split()
        if len(parts) == 2 and parts[0].endswith("."):
            day, month = parts[0].rstrip(".").split(".")
            return (datetime(today.year, int(month), int(day)).date(), parts[1])
    except: pass

    try: return (datetime.strptime(value, "%d.%m.%Y").date(), "")
    except: pass

    try:
        if value.endswith("."):
            day, month = value.rstrip(".").split(".")
            return (datetime(today.year, int(month), int(day)).date(), "")
    except: pass

    return value, ""

# ==========================================
# 1. POBIERANIE Z BETEXPLORER (Z CLOUDSCRAPER)
# ==========================================
urls = pd.read_excel("ligi.xlsx")["URL"].dropna().tolist()
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

all_data = []

# Używamy cloudscapera, aby ominąć ukrywanie kursów przez systemy anty-botowe BetExplorera
skaner_be = cloudscraper.create_scraper()

for i, url in enumerate(urls, start=1):
    print(f"[{i}/{len(urls)}] Pobieram: {url}")
    try:
        html = skaner_be.get(url, headers=headers, timeout=30).text
        soup = BeautifulSoup(html, "html.parser")
        league = url.split("/football/")[1].replace("/fixtures/", "").replace("/results/", "")
        rows = soup.find_all("tr")

        if "/fixtures/" in url:
            for row in rows:
                date_cell = row.find("td", class_="table-main__datetime")
                if not date_cell: continue

                spans = row.find_all("span")
                if len(spans) < 2: continue

                home = spans[0].get_text(strip=True)
                away = spans[1].get_text(strip=True)

                odds = []
                odds_cells = row.select("td.table-main__odds")
                
                # Agresywny wyciągacz kursów - zagląda w każdą możliwą strukturę komórki
                for cell in odds_cells:
                    odd = cell.get("data-odd")
                    if not odd:
                        span = cell.find(attrs={"data-odd": True})
                        if span: odd = span.get("data-odd")
                    if not odd:
                        button = cell.find("button")
                        if button: odd = button.get_text(strip=True)
                    if not odd:
                        text = cell.get_text(" ", strip=True)
                        if text: odd = text
                    odds.append(odd if odd else "-")
                        
                odd1 = odds[0] if len(odds) >= 1 else "-"
                oddx = odds[1] if len(odds) >= 2 else "-"
                odd2 = odds[2] if len(odds) >= 3 else "-"

                all_data.append(["Fixture", league, date_cell.get_text(strip=True), home, away, "", odd1, oddx, odd2])

        elif "/results/" in url:
            for row in rows:
                match = row.find("a", class_="in-match")
                if not match: continue

                spans = match.find_all("span")
                if len(spans) < 2: continue

                home = spans[0].get_text(" ", strip=True)
                away = spans[1].get_text(" ", strip=True)

                score = ""
                score_cell = row.find("td", class_="h-text-center")
                if score_cell: score = score_cell.get_text(strip=True)

                odds_cells = row.select("td.table-main__odds")
                odds = []
                for cell in odds_cells:
                    odd = cell.get("data-odd")
                    if not odd:
                        span = cell.find(attrs={"data-odd": True})
                        if span: odd = span.get("data-odd")
                    if not odd:
                        text = cell.get_text(" ", strip=True)
                        if text: odd = text
                    odds.append(odd if odd else "-")

                odd1 = odds[0] if len(odds) >= 1 else "-"
                oddx = odds[1] if len(odds) >= 2 else "-"
                odd2 = odds[2] if len(odds) >= 3 else "-"

                date = ""
                date_cell = row.find("td", class_=lambda x: x and "h-text-right" in x)
                if date_cell: date = date_cell.get_text(strip=True)

                all_data.append(["Result", league, date, home, away, score, odd1, oddx, odd2])

    except Exception as e: print("BŁĄD:", url, e)

df = pd.DataFrame(all_data, columns=["Type", "League", "Date", "Home", "Away", "Score", "Odd1", "OddX", "Odd2"])

dates, times = [], []
for value in df["Date"]:
    d, t = split_datetime(value)
    dates.append(d)
    times.append(t)
    
df["Date"] = dates
df.insert(3, "Time", times)

fixtures_df = df[df["Type"] == "Fixture"].copy()
results_df = df[df["Type"] == "Result"].copy()

# PANCERNE ZABEZPIECZENIE KURSÓW - gwarantuje, że wiersz z kursami wypycha na wierzch myślniki
fixtures_df['HasOdds'] = fixtures_df['Odd1'].astype(str).apply(lambda x: 1 if x.strip() not in ["", "-", "nan"] else 0)
fixtures_df = fixtures_df.sort_values(by=["Date", "Time", "Home", "Away", "HasOdds"], ascending=[True, True, True, True, False])
fixtures_df = fixtures_df.drop_duplicates(subset=["Date", "Home", "Away"]).drop(columns=["HasOdds"])

results_df['HasOdds'] = results_df['Odd1'].astype(str).apply(lambda x: 1 if x.strip() not in ["", "-", "nan"] else 0)
results_df = results_df.sort_values(by=["Date", "Home", "Away", "HasOdds"], ascending=[False, True, True, False])
results_df = results_df.drop_duplicates(subset=["Date", "Home", "Away"]).drop(columns=["HasOdds"])


# ==========================================================
# 2. POBIERANIE Z SOCCERSTATS
# ==========================================================
dane_soccerstats_baza = []
print("Rozpoczynam pobieranie danych z SoccerStats...")

try:
    if os.path.exists("ligi_soccerstats.xlsx"):
        urls_ss = pd.read_excel("ligi_soccerstats.xlsx")["URL"].dropna().tolist()
        skaner_ss = cloudscraper.create_scraper()
        
        for i_ss, url_ss in enumerate(urls_ss, start=1):
            url_ss = str(url_ss).strip()
            nazwa_ligi = url_ss.split("league=")[1].split("&")[0] if "league=" in url_ss else f"Liga_{i_ss}"
            print(f"[{i_ss}/{len(urls_ss)}] Pobieram SoccerStats dla: {nazwa_ligi}")
            
            html_ss = skaner_ss.get(url_ss, headers=headers, timeout=30).text
            soup_ss = BeautifulSoup(html_ss, "html.parser")
            
            tabela_meczow = None
            for t in soup_ss.find_all("table"):
                if "HT" in t.get_text() and "BTS" in t.get_text() and len(t.find_all("tr")) > 15:
                    tabela_meczow = t
                    break
            
            if tabela_meczow:
                wiersze_ss = tabela_meczow.find_all("tr")
                for wiersz in wiersze_ss:
                    komorki = wiersz.find_all(["td", "th"])
                    if len(komorki) >= 6:
                        teksty = [k.get_text(" ", strip=True) for k in komorki]
                        wynik_index = -1
                        for idx, val in enumerate(teksty):
                            if ("-" in val or ":" in val) and any(c.isdigit() for c in val):
                                if 1 <= idx <= 5: 
                                    wynik_index = idx
                                    break
                                        
                        if wynik_index != -1:
                            wynik = teksty[wynik_index]
                            gospodarz = teksty[wynik_index - 1]
                            gosc = teksty[wynik_index + 1] if wynik_index + 1 < len(teksty) else ""
                            
                            if "HOME" in gospodarz.upper() or "GOSPODARZ" in gospodarz.upper(): continue
                                
                            if gospodarz and gosc and gosc != gospodarz:
                                ht, o25, tg, bts = "-", "-", "-", "-"
                                statystyki = [s for s in teksty[wynik_index + 2:] if s.strip()] 
                                
                                if len(statystyki) >= 4:
                                    ht, o25, tg, bts = statystyki[0], statystyki[1], statystyki[2], statystyki[3]
                                elif len(statystyki) > 0:
                                    ht = statystyki[0]
                                    if len(statystyki) > 1: o25 = statystyki[1]
                                    if len(statystyki) > 2: tg = statystyki[2]
                                    
                                wynik_czysty = wynik.replace("*", "").strip().replace(" ", "").replace("-", ":")
                                ht_czysty = ht.replace("*", "").strip().replace(" ", "").replace("-", ":").replace("(", "").replace(")", "")
                                
                                g_gosp_m, g_gosc_m, suma_m = "-", "-", "-"
                                g_gosp_1h, g_gosc_1h, suma_1h = "-", "-", "-"
                                g_gosp_2h, g_gosc_2h, suma_2h = "-", "-", "-"
                                
                                if ":" in wynik_czysty:
                                    try:
                                        p_m = wynik_czysty.split(":")
                                        g_gosp_m, g_gosc_m = int(p_m[0]), int(p_m[1])
                                        suma_m = g_gosp_m + g_gosc_m
                                    except: pass
                                        
                                if ":" in ht_czysty:
                                    try:
                                        p_1h = ht_czysty.split(":")
                                        g_gosp_1h, g_gosc_1h = int(p_1h[0]), int(p_1h[1])
                                        suma_1h = g_gosp_1h + g_gosc_1h
                                    except: pass
                                        
                                if isinstance(g_gosp_m, int) and isinstance(g_gosp_1h, int):
                                    try:
                                        g_gosp_2h, g_gosc_2h = g_gosp_m - g_gosp_1h, g_gosc_m - g_gosc_1h
                                        suma_2h = g_gosp_2h + g_gosc_2h
                                    except: pass
                                
                                dane_soccerstats_baza.append([
                                    gospodarz, gosc, wynik_czysty,
                                    g_gosp_m, g_gosc_m, suma_m,
                                    ht_czysty, g_gosp_1h, g_gosc_1h, suma_1h,
                                    g_gosp_2h, g_gosc_2h, suma_2h,
                                    o25.replace("*", "").strip(), tg.replace("*", "").strip(), bts.replace("*", "").strip()
                                ])
                        
        if dane_soccerstats_baza:
            ss_df = pd.DataFrame(dane_soccerstats_baza, columns=[
                "Home", "Away", "Score",
                "Gole_Gosp_Mecz", "Gole_Gosc_Mecz", "Suma_Goli_Mecz",
                "Wynik_HT", "Gole_Gosp_1H", "Gole_Gosc_1H", "Suma_Goli_1H",
                "Gole_Gosp_2H", "Gole_Gosc_2H", "Suma_Goli_2H", "2.5+", "TG", "BTS"
            ])
            ss_df = ss_df.drop_duplicates(subset=["Home", "Away", "Score"])
        else: ss_df = pd.DataFrame()
            
except Exception as e: print("Błąd SoccerStats:", e); ss_df = pd.DataFrame()


# ==========================================================
# 3. MAPOWANIE I SCALANIE DANYCH 
# ==========================================================
mapowanie_ss, mapowanie_fd = {}, {}
if os.path.exists("slownik_druzyn.json"):
    try:
        with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
            slownik_data = json.load(f)
            mapowanie_ss = slownik_data.get("SoccerStats_To_BetExplorer", {})
            mapowanie_fd = slownik_data.get("FootballData_To_BetExplorer", {})
    except: pass

if not ss_df.empty and not results_df.empty:
    print("Ujednolicam nazwy i scalam historię z SoccerStats...")
    ss_do_scalenia = ss_df.copy()
    ss_do_scalenia["Home"] = ss_do_scalenia["Home"].apply(lambda x: mapowanie_ss.get(x, x))
    ss_do_scalenia["Away"] = ss_do_scalenia["Away"].apply(lambda x: mapowanie_ss.get(x, x))
    
    results_df = pd.merge(results_df, ss_do_scalenia, on=["Home", "Away", "Score"], how="left")

print("Rozpoczynam integrację danych z Football-Data.co.uk...")
fd_all_data = pd.DataFrame()

urls_fd = ["https://www.football-data.co.uk/new/FIN.csv"]
if os.path.exists("ligi_footballdata.xlsx"):
    try: urls_fd = pd.read_excel("ligi_footballdata.xlsx")["URL"].dropna().tolist()
    except: pass

for url_fd in urls_fd:
    try:
        fd_raw = pd.read_csv(url_fd, on_bad_lines='skip')
        if not fd_raw.empty and "Home" in fd_raw.columns:
            fd_all_data = pd.concat([fd_all_data, fd_raw], ignore_index=True)
    except: pass

if not fd_all_data.empty and not results_df.empty:
    try:
        kolumny_fd = ["Home", "Away", "HG", "AG", "HS", "AS", "HST", "AST", "HC", "AC"]
        fd_processed = fd_all_data[[c for c in kolumny_fd if c in fd_all_data.columns]].copy()
        
        fd_processed = fd_processed.dropna(subset=["HG", "AG"])
        fd_processed["Score"] = fd_processed["HG"].astype(int).astype(str) + ":" + fd_processed["AG"].astype(int).astype(str)
        
        fd_processed["Home"] = fd_processed["Home"].apply(lambda x: mapowanie_fd.get(str(x).strip(), str(x).strip()))
        fd_processed["Away"] = fd_processed["Away"].apply(lambda x: mapowanie_fd.get(str(x).strip(), str(x).strip()))
        
        if "HC" in fd_processed.columns and "AC" in fd_processed.columns:
            fd_processed["Suma_Roznych"] = fd_processed["HC"] + fd_processed["AC"]
        
        if "HS" in fd_processed.columns and "AS" in fd_processed.columns:
            fd_processed["Suma_Strzalow"] = fd_processed["HS"] + fd_processed["AS"]
            fd_processed["Wiecej_Strzalow"] = "Remis"
            fd_processed.loc[fd_processed["HS"] > fd_processed["AS"], "Wiecej_Strzalow"] = "Gospodarz"
            fd_processed.loc[fd_processed["AS"] > fd_processed["HS"], "Wiecej_Strzalow"] = "Gosc"
            
        if "HST" in fd_processed.columns and "AST" in fd_processed.columns:
            fd_processed["Suma_Celnych"] = fd_processed["HST"] + fd_processed["AST"]
            fd_processed["Wiecej_Celnych"] = "Remis"
            fd_processed.loc[fd_processed["HST"] > fd_processed["AST"], "Wiecej_Celnych"] = "Gospodarz"
            fd_processed.loc[fd_processed["AST"] > fd_processed["HST"], "Wiecej_Celnych"] = "Gosc"
            
        fd_final = fd_processed.drop(columns=["HG", "AG", "HS", "AS", "HST", "AST", "HC", "AC"], errors="ignore")
        fd_final = fd_final.drop_duplicates(subset=["Home", "Away", "Score"])
        
        results_df = pd.merge(results_df, fd_final, on=["Home", "Away", "Score"], how="left")
            
    except Exception as e: print("Football-Data błąd mapowania:", e)


# ==========================================
# 4. CZYSZCZENIE I FORMATOWANIE DO GOOGLE SHEETS
# ==========================================
kolumny_liczbowe = [
    "Gole_Gosp_Mecz", "Gole_Gosc_Mecz", "Suma_Goli_Mecz",
    "Gole_Gosp_1H", "Gole_Gosc_1H", "Suma_Goli_1H",
    "Gole_Gosp_2H", "Gole_Gosc_2H", "Suma_Goli_2H",
    "Suma_Roznych", "Suma_Strzalow", "Suma_Celnych"
]

for col in kolumny_liczbowe:
    if col in results_df.columns:
        results_df[col] = pd.to_numeric(results_df[col], errors='coerce').astype('Int64').astype(str).replace('<NA>', '-')

if not fixtures_df.empty: fixtures_df = fixtures_df.fillna("-")
if not results_df.empty: results_df = results_df.fillna("-")

for col in ["Odd1", "OddX", "Odd2"]:
    if col in fixtures_df.columns: fixtures_df[col] = fixtures_df[col].apply(lambda x: str(x).replace(".", ",") if str(x) != "-" else "-")
    if col in results_df.columns: results_df[col] = results_df[col].apply(lambda x: str(x).replace(".", ",") if str(x) != "-" else "-")

# ==========================================
# 5. GOOGLE SHEETS AUTORYZACJA I ZAPIS
# ==========================================
scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

if os.path.exists("credentials.json"): creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
else: creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)

client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

try: summary_sheet = spreadsheet.worksheet("Summary")
except: summary_sheet = spreadsheet.add_worksheet(title="Summary", rows=100, cols=10)

try: fixtures_sheet = spreadsheet.worksheet("Fixtures")
except: fixtures_sheet = spreadsheet.add_worksheet(title="Fixtures", rows=1000, cols=35)

try: results_sheet = spreadsheet.worksheet("Results")
except: results_sheet = spreadsheet.add_worksheet(title="Results", rows=5000, cols=35)

try:
    fixtures_sheet.resize(rows=1000, cols=35)
    results_sheet.resize(rows=5000, cols=35)
except: pass

print("Wysyłam Czysty Terminarz do Google Sheets...")
fixtures_sheet.clear()
fixtures_sheet.update([fixtures_df.columns.tolist()] + fixtures_df.astype(str).values.tolist())

print("Wysyłam Historię ze statystykami do Google Sheets...")
results_sheet.clear()
results_sheet.update([results_df.columns.tolist()] + results_df.astype(str).values.tolist())

summary_sheet.clear()
summary_sheet.update([
    ["Metric", "Value"],
    ["Last Update", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
    ["Fixtures Czyste", len(fixtures_df)],
    ["Results Zintegrowane", len(results_df)]
])

print("\n" + "=" * 60)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Fixtures:", len(fixtures_df))
print("Results:", len(results_df))
print("=" * 60)
