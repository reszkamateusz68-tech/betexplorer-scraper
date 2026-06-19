import os
import json
import gspread
import requests
import cloudscraper
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

today = datetime.now()

def split_datetime(value):
    if pd.isna(value):
        return None, ""

    value = str(value).strip()

    if value.lower().startswith("today"):
        time_part = value.replace("Today", "").strip()
        return today.date(), time_part

    if value.lower().startswith("tomorrow"):
        time_part = value.replace("Tomorrow", "").strip()
        return (today.date() + timedelta(days=1), time_part)

    if value.lower().startswith("yesterday"):
        time_part = value.replace("Yesterday", "").strip()
        return (today.date() - timedelta(days=1), time_part)

    try:
        parts = value.split()
        if len(parts) == 2:
            date_part = parts[0]
            time_part = parts[1]
            if date_part.endswith("."):
                day, month = date_part.rstrip(".").split(".")
                return (datetime(today.year, int(month), int(day)).date(), time_part)
    except:
        pass

    try:
        return (datetime.strptime(value, "%d.%m.%Y").date(), "")
    except:
        pass

    try:
        if value.endswith("."):
            day, month = value.rstrip(".").split(".")
            return (datetime(today.year, int(month), int(day)).date(), "")
    except:
        pass

    return value, ""

# ==========================================================
# 1. POBIERANIE Z BETEXPLORER (Stara, niezawodna metoda)
# ==========================================================
urls = pd.read_excel("ligi.xlsx")["URL"].dropna().tolist()

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache"
}

all_data = []

for i, url in enumerate(urls, start=1):
    print(f"[{i}/{len(urls)}] Pobieram BetExplorer: {url}")
    try:
        html = requests.get(url, headers=headers, timeout=30).text
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

    except Exception as e:
        print()
        print("BŁĄD:", url, e)
        print()

# ==========================================================
# 2. DATAFRAME (Stara i bezpieczna logika)
# ==========================================================
df = pd.DataFrame(all_data, columns=["Type", "League", "Date", "Home", "Away", "Score", "Odd1", "OddX", "Odd2"])
df = df.drop_duplicates()

dates, times = [], []
for value in df["Date"]:
    d, t = split_datetime(value)
    dates.append(d)
    times.append(t)

df["Date"] = dates
df.insert(3, "Time", times)

fixtures_df = df[df["Type"] == "Fixture"].copy()
results_df = df[df["Type"] == "Result"].copy()

fixtures_df = fixtures_df.sort_values(by=["Date", "Time"], ascending=True)
results_df = results_df.sort_values(by=["Date"], ascending=False)

# ==========================================================
# 3. POBIERANIE Z SOCCERSTATS
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
            
            html_ss = skaner_ss.get(url_ss, headers={"User-Agent": headers["User-Agent"]}, timeout=30).text
            soup_ss = BeautifulSoup(html_ss, "html.parser")
            
            tabela_meczow = None
            for t in soup_ss.find_all("table"):
                if "HT" in t.get_text() and "BTS" in t.get_text() and len(t.find_all("tr")) > 15:
                    tabela_meczow = t
                    break
            
            if tabela_meczow:
                for wiersz in tabela_meczow.find_all("tr"):
                    komorki = wiersz.find_all(["td", "th"])
                    if len(komorki) >= 6:
                        teksty = [k.get_text(" ", strip=True) for k in komorki]
                        wynik_index = -1
                        for idx, val in enumerate(teksty):
                            if ("-" in val or ":" in val) and any(c.isdigit() for c in val) and 1 <= idx <= 5: 
                                wynik_index = idx
                                break
                                    
                        if wynik_index != -1:
                            wynik = teksty[wynik_index].replace("*", "").strip().replace("-", ":").replace(" ", "")
                            gospodarz = teksty[wynik_index - 1]
                            gosc = teksty[wynik_index + 1] if wynik_index + 1 < len(teksty) else ""
                            
                            if "HOME" in gospodarz.upper() or "GOSPODARZ" in gospodarz.upper() or not gospodarz or not gosc or gospodarz == gosc:
                                continue
                                
                            statystyki = [s for s in teksty[wynik_index + 2:] if s.strip()] 
                            ht = statystyki[0].replace("*", "").strip().replace("-", ":").replace(" ", "").replace("(", "").replace(")", "") if len(statystyki) > 0 else "-"
                            o25 = statystyki[1].replace("*", "").strip() if len(statystyki) > 1 else "-"
                            tg = statystyki[2].replace("*", "").strip() if len(statystyki) > 2 else "-"
                            bts = statystyki[3].replace("*", "").strip() if len(statystyki) > 3 else "-"
                            
                            g_gosp_m, g_gosc_m, suma_m = "-", "-", "-"
                            g_gosp_1h, g_gosc_1h, suma_1h = "-", "-", "-"
                            g_gosp_2h, g_gosc_2h, suma_2h = "-", "-", "-"
                            
                            if ":" in wynik:
                                try:
                                    p_m = wynik.split(":")
                                    g_gosp_m, g_gosc_m = int(p_m[0]), int(p_m[1])
                                    suma_m = g_gosp_m + g_gosc_m
                                except: pass
                            if ":" in ht:
                                try:
                                    p_1h = ht.split(":")
                                    g_gosp_1h, g_gosc_1h = int(p_1h[0]), int(p_1h[1])
                                    suma_1h = g_gosp_1h + g_gosc_1h
                                except: pass
                            if isinstance(g_gosp_m, int) and isinstance(g_gosp_1h, int):
                                try:
                                    g_gosp_2h, g_gosc_2h = g_gosp_m - g_gosp_1h, g_gosc_m - g_gosc_1h
                                    suma_2h = g_gosp_2h + g_gosc_2h
                                except: pass
                            
                            dane_soccerstats_baza.append([
                                gospodarz, gosc, wynik,
                                g_gosp_m, g_gosc_m, suma_m,
                                ht, g_gosp_1h, g_gosc_1h, suma_1h,
                                g_gosp_2h, g_gosc_2h, suma_2h,
                                o25, tg, bts
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
# 4. POBIERANIE Z FOOTBALL-DATA.CO.UK
# ==========================================================
print("Rozpoczynam integrację danych z Football-Data.co.uk...")
fd_all_data = pd.DataFrame()

try:
    if os.path.exists("ligi_footballdata.xlsx"):
        urls_fd = pd.read_excel("ligi_footballdata.xlsx")["URL"].dropna().tolist()
        fd_rename_dict = {"HomeTeam": "Home", "AwayTeam": "Away", "FTHG": "HG", "FTAG": "AG"}
        
        for url_fd in urls_fd:
            try:
                fd_raw = pd.read_csv(url_fd.strip(), on_bad_lines='skip')
                if not fd_raw.empty:
                    fd_raw = fd_raw.rename(columns=fd_rename_dict)
                    fd_all_data = pd.concat([fd_all_data, fd_raw], ignore_index=True)
            except: pass
except: pass

# ==========================================================
# 5. BEZPIECZNE SCALANIE DANYCH (Mecze z BetExplorera NIE MOGĄ zniknąć)
# ==========================================================
mapowanie_ss, mapowanie_fd = {}, {}
if os.path.exists("slownik_druzyn.json"):
    try:
        with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
            slownik_data = json.load(f)
            mapowanie_ss = slownik_data.get("SoccerStats_To_BetExplorer", {})
            mapowanie_fd = slownik_data.get("FootballData_To_BetExplorer", {})
    except: pass

# Łączenie z SoccerStats
if not ss_df.empty and not results_df.empty:
    print("Ujednolicam nazwy i scalam historię z SoccerStats...")
    ss_df["Home"] = ss_df["Home"].astype(str).str.strip().apply(lambda x: mapowanie_ss.get(x, x))
    ss_df["Away"] = ss_df["Away"].astype(str).str.strip().apply(lambda x: mapowanie_ss.get(x, x))
    
    results_df = pd.merge(results_df, ss_df, on=["Home", "Away", "Score"], how="left")

# Łączenie z Football-Data
if not fd_all_data.empty and not results_df.empty:
    try:
        kolumny_fd = ["Home", "Away", "HG", "AG", "HS", "AS", "HST", "AST", "HC", "AC"]
        fd_processed = fd_all_data[[c for c in kolumny_fd if c in fd_all_data.columns]].copy()
        
        fd_processed = fd_processed.dropna(subset=["HG", "AG"])
        fd_processed["Score"] = fd_processed["HG"].astype(int).astype(str) + ":" + fd_processed["AG"].astype(int).astype(str)
        
        fd_processed["Home"] = fd_processed["Home"].astype(str).str.strip().apply(lambda x: mapowanie_fd.get(x, x))
        fd_processed["Away"] = fd_processed["Away"].astype(str).str.strip().apply(lambda x: mapowanie_fd.get(x, x))
        
        if "HC" in fd_processed.columns and "AC" in fd_processed.columns:
            fd_processed["Suma_Roznych"] = fd_processed["HC"] + fd_processed["AC"]
        
        if "HS" in fd_processed.columns and "AS" in fd_processed.columns:
            fd_processed["Suma_Strzalow"] = fd_processed["HS"] + fd_processed["AS"]
            
        if "HST" in fd_processed.columns and "AST" in fd_processed.columns:
            fd_processed["Suma_Celnych"] = fd_processed["HST"] + fd_processed["AST"]
            
        fd_final = fd_processed.drop(columns=["HG", "AG", "HS", "AS", "HST", "AST", "HC", "AC"], errors="ignore")
        fd_final = fd_final.drop_duplicates(subset=["Home", "Away", "Score"])
        
        results_df = pd.merge(results_df, fd_final, on=["Home", "Away", "Score"], how="left")
        print("Integracja Football-Data zakończona pomyślnie.")
            
    except Exception as e: print("Football-Data błąd mapowania:", e)

# ==========================================================
# 6. SILNIK FORMY I STATYSTYK (TEAM FORM ENGINE)
# ==========================================================
print("Generuję automatyczne statystyki formy dla nadchodzących meczów...")

def oblicz_statystyki_druzyny(team_name, wyniki_df, n_matches=5):
    mecze_team = wyniki_df[(wyniki_df["Home"] == team_name) | (wyniki_df["Away"] == team_name)].copy()
    mecze_team = mecze_team.head(n_matches)
    
    if mecze_team.empty:
        return {"Forma_Punkty": 0, "Śr_Goli_Zdob": 0.0, "Śr_Goli_Strac": 0.0, "Śr_Roznych": "-", "Śr_Celnych": "-"}
    
    punkty, gole_zdobyte, gole_stracone, rozne, celne = 0, 0, 0, [], []
    
    for _, match in mecze_team.iterrows():
        is_home = match["Home"] == team_name
        try:
            if ":" in str(match["Score"]):
                parts = str(match["Score"]).split(":")
                g_home, g_away = int(parts[0]), int(parts[1])
                
                if is_home:
                    gole_zdobyte += g_home; gole_stracone += g_away
                    if g_home > g_away: punkty += 3
                    elif g_home == g_away: punkty += 1
                else:
                    gole_zdobyte += g_away; gole_stracone += g_home
                    if g_away > g_home: punkty += 3
                    elif g_away == g_home: punkty += 1
        except: pass
            
        try:
            if "Suma_Roznych" in match and pd.notna(match["Suma_Roznych"]) and str(match["Suma_Roznych"]) != "-":
                rozne.append(int(float(match["Suma_Roznych"])))
        except: pass
        
        try:
            if "Suma_Celnych" in match and pd.notna(match["Suma_Celnych"]) and str(match["Suma_Celnych"]) != "-":
                celne.append(int(float(match["Suma_Celnych"])))
        except: pass

    n_real = len(mecze_team)
    return {
        "Forma_Punkty": punkty,
        "Śr_Goli_Zdob": round(gole_zdobyte / n_real, 2),
        "Śr_Goli_Strac": round(gole_stracone / n_real, 2),
        "Śr_Roznych": round(sum(rozne) / len(rozne), 1) if rozne else "-",
        "Śr_Celnych": round(sum(celne) / len(celne), 1) if celne else "-"
    }

fixtures_stats_list = []
for _, fix in fixtures_df.iterrows():
    stats_home = oblicz_statystyki_druzyny(fix["Home"], results_df)
    stats_away = oblicz_statystyki_druzyny(fix["Away"], results_df)
    
    fixtures_stats_list.append([
        fix["League"], fix["Date"], fix["Time"], fix["Home"], fix["Away"],
        fix["Odd1"], fix["OddX"], fix["Odd2"],
        stats_home["Forma_Punkty"], stats_away["Forma_Punkty"],
        stats_home["Śr_Goli_Zdob"], stats_away["Śr_Goli_Zdob"],
        stats_home["Śr_Goli_Strac"], stats_away["Śr_Goli_Strac"],
        stats_home["Śr_Roznych"], stats_away["Śr_Roznych"],
        stats_home["Śr_Celnych"], stats_away["Śr_Celnych"]
    ])

analysis_df = pd.DataFrame(fixtures_stats_list, columns=[
    "League", "Date", "Time", "Home", "Away", "Odd1", "OddX", "Odd2",
    "Forma_H_5m", "Forma_A_5m", "Śr_Goli_Zdob_H", "Śr_Goli_Zdob_A",
    "Śr_Goli_Strac_H", "Śr_Goli_Strac_A", "Śr_Roznych_Mecz_H", "Śr_Roznych_Mecz_A",
    "Śr_Celnych_Mecz_H", "Śr_Celnych_Mecz_A"
])

# ==========================================================
# 7. FORMATOWANIE I WYSYŁKA DO GOOGLE SHEETS
# ==========================================================
kolumny_liczbowe = [
    "Gole_Gosp_Mecz", "Gole_Gosc_Mecz", "Suma_Goli_Mecz",
    "Gole_Gosp_1H", "Gole_Gosc_1H", "Suma_Goli_1H",
    "Gole_Gosp_2H", "Gole_Gosc_2H", "Suma_Goli_2H",
    "Suma_Roznych", "Suma_Strzalow", "Suma_Celnych"
]

if not results_df.empty:
    for col in kolumny_liczbowe:
        if col in results_df.columns:
            results_df[col] = pd.to_numeric(results_df[col], errors='coerce').astype('Int64').astype(str).replace('<NA>', '-')

# Zabezpieczenie przed NaN (zamiana na kreski)
fixtures_df = fixtures_df.fillna("-")
results_df = results_df.fillna("-")
analysis_df = analysis_df.fillna("-")

# Formatowanie przecinków do polskich ustawień arkusza
for df_to_format in [fixtures_df, results_df, analysis_df]:
    for col in ["Odd1", "OddX", "Odd2"]:
        if col in df_to_format.columns:
            df_to_format[col] = df_to_format[col].apply(lambda x: str(x).replace(".", ",") if str(x) != "-" else "-")

scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
if os.path.exists("credentials.json"): creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
else: creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)

client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

# Dynamiczne tworzenie i wypełnianie zakładek
zakladki = [
    ("Fixtures", fixtures_df, 1000, 35),
    ("Results", results_df, 5000, 45),
    ("Analysis", analysis_df, 1000, 30)
]

for nazwa_zakladki, df_data, rows_cnt, cols_cnt in zakladki:
    try: sheet = spreadsheet.worksheet(nazwa_zakladki)
    except: sheet = spreadsheet.add_worksheet(title=nazwa_zakladki, rows=rows_cnt, cols=cols_cnt)
    
    print(f"Wysyłam zakładkę: {nazwa_zakladki}...")
    sheet.clear()
    if not df_data.empty:
        sheet.update([df_data.columns.tolist()] + df_data.astype(str).values.tolist())

# Podsumowanie
try: summary_sheet = spreadsheet.worksheet("Summary")
except: summary_sheet = spreadsheet.add_worksheet(title="Summary", rows=100, cols=10)

summary_sheet.clear()
summary_sheet.update([
    ["Metric", "Value"],
    ["Last Update", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
    ["Fixtures Czyste", len(fixtures_df)],
    ["Results Zintegrowane", len(results_df)],
    ["Mecze w Arkuszu Analizy", len(analysis_df)]
])

print("\n" + "=" * 60)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Fixtures:", len(fixtures_df))
print("Results:", len(results_df))
print("Analysis:", len(analysis_df))
print("=" * 60)
