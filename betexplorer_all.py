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

    # Today 20:00
    if value.lower().startswith("today"):
        time_part = value.replace("Today", "").strip()
        return today.date(), time_part

    # Tomorrow 01:00
    if value.lower().startswith("tomorrow"):
        time_part = value.replace("Tomorrow", "").strip()
        return (
            today.date() + timedelta(days=1),
            time_part
        )

    # Yesterday
    if value.lower().startswith("yesterday"):
        time_part = value.replace("Yesterday", "").strip()
        return (
            today.date() - timedelta(days=1),
            time_part
        )

    # 17.06. 01:00
    try:
        parts = value.split()
        if len(parts) == 2:
            date_part = parts[0]
            time_part = parts[1]
            if date_part.endswith("."):
                day, month = date_part.rstrip(".").split(".")
                return (
                    datetime(
                        today.year,
                        int(month),
                        int(day)
                    ).date(),
                    time_part
                )
    except:
        pass

    # 18.08.2025
    try:
        return (
            datetime.strptime(
                value,
                "%d.%m.%Y"
            ).date(),
            ""
        )
    except:
        pass

    # 24.05.
    try:
        if value.endswith("."):
            day, month = value.rstrip(".").split(".")
            return (
                datetime(
                    today.year,
                    int(month),
                    int(day)
                ).date(),
                ""
            )
    except:
        pass

    return value, ""

# Wczytanie lig z Excel
urls = pd.read_excel("ligi.xlsx")["URL"].dropna().tolist()

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache"
}

all_data = []

for i, url in enumerate(urls, start=1):
    print(f"[{i}/{len(urls)}] Pobieram: {url}")
    try:
        html = requests.get(
            url,
            headers=headers,
            timeout=30
        ).text

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        league = url.split("/football/")[1]
        league = league.replace("/fixtures/", "")
        league = league.replace("/results/", "")

        rows = soup.find_all("tr")

        # ==========================================
        # FUTURE FIXTURES
        # ==========================================
        if "/fixtures/" in url:
            for row in rows:
                date_cell = row.find(
                    "td",
                    class_="table-main__datetime"
                )

                if not date_cell:
                    continue

                spans = row.find_all("span")
                if len(spans) < 2:
                    continue

                home = spans[0].get_text(strip=True)
                away = spans[1].get_text(strip=True)

                odds = []
                odds_cells = row.select("td.table-main__odds")

                for cell in odds_cells:
                    odd = cell.get("data-odd")
                    if not odd:
                        span = cell.find(attrs={"data-odd": True})
                        if span:
                            odd = span.get("data-odd")
                    if not odd:
                        button = cell.find("button")
                        if button:
                            odd = button.get_text(strip=True)
                    if not odd:
                        text = cell.get_text(" ", strip=True)
                        if text:
                            odd = text
                    odds.append(odd if odd else "-")
                        
                if len(odds) == 0:
                    print("BRAK KURSÓW:")
                    print(row)
                    print("-" * 80)
                    
                odd1 = "-"
                oddx = "-"
                odd2 = "-"

                if len(odds) >= 1:
                    odd1 = odds[0]
                if len(odds) >= 2:
                    oddx = odds[1]
                if len(odds) >= 3:
                    odd2 = odds[2]

                all_data.append([
                    "Fixture",
                    league,
                    date_cell.get_text(strip=True),
                    home,
                    away,
                    "",
                    odd1,
                    oddx,
                    odd2
                ])

        # ==========================================
        # HISTORICAL RESULTS
        # ==========================================
        elif "/results/" in url:
            for row in rows:
                match = row.find(
                    "a",
                    class_="in-match"
                )
                if not match:
                    continue

                spans = match.find_all("span")
                if len(spans) < 2:
                    continue

                home = spans[0].get_text(" ", strip=True)
                away = spans[1].get_text(" ", strip=True)

                score = ""
                score_cell = row.find(
                    "td",
                    class_="h-text-center"
                )
                if score_cell:
                    score = score_cell.get_text(strip=True)

                odds_cells = row.select(
                    "td.table-main__odds"
                )
                odds = []
                for cell in odds_cells:
                    odd = cell.get("data-odd")
                    if not odd:
                        span = cell.find(
                            attrs={"data-odd": True}
                        )
                        if span:
                            odd = span.get("data-odd")
                    odds.append(
                        odd if odd else "-"
                    )

                odd1 = "-"
                oddx = "-"
                odd2 = "-"

                if len(odds) >= 1:
                    odd1 = odds[0]
                if len(odds) >= 2:
                    oddx = odds[1]
                if len(odds) >= 3:
                    odd2 = odds[2]

                date = ""
                date_cell = row.find(
                    "td",
                    class_=lambda x: x and "h-text-right" in x
                )
                if date_cell:
                    date = date_cell.get_text(strip=True)

                all_data.append([
                    "Result",
                    league,
                    date,
                    home,
                    away,
                    score,
                    odd1,
                    oddx,
                    odd2
                ])

    except Exception as e:
        print()
        print("BŁĄD:", url)
        print(e)
        print()

# ==========================================
# DATAFRAME
# ==========================================
df = pd.DataFrame(
    all_data,
    columns=[
        "Type",
        "League",
        "Date",
        "Home",
        "Away",
        "Score",
        "Odd1",
        "OddX",
        "Odd2"
    ]
)
df = df.drop_duplicates()

dates = []
times = []
for value in df["Date"]:
    d, t = split_datetime(value)
    dates.append(d)
    times.append(t)

df["Date"] = dates
df.insert(3, "Time", times)

# ==========================================================
# SEKCJA: SOCCERSTATS (ZAAWANSOWANA TRANSFORMAZJA MATEMATYCZNA)
# ==========================================================
dane_soccerstats_baza = []
print("Rozpoczynam pobieranie danych z SoccerStats...")

try:
    if os.path.exists("ligi_soccerstats.xlsx"):
        urls_ss = pd.read_excel("ligi_soccerstats.xlsx")["URL"].dropna().tolist()
        print(f"Znaleziono {len(urls_ss)} linków w pliku ligi_soccerstats.xlsx")
        
        skaner_ss = cloudscraper.create_scraper()
        
        for i_ss, url_ss in enumerate(urls_ss, start=1):
            url_ss = str(url_ss).strip()
            
            try:
                nazwa_ligi = url_ss.split("league=")[1].split("&")[0]
            except:
                nazwa_ligi = f"Liga_{i_ss}"
                
            print(f"[{i_ss}/{len(urls_ss)}] Pobieram SoccerStats dla: {nazwa_ligi}")
            
            html_ss = skaner_ss.get(url_ss, headers=headers, timeout=30).text
            soup_ss = BeautifulSoup(html_ss, "html.parser")
            
            tabela_meczow = None
            wszystkie_tabele = soup_ss.find_all("table")
            for t in wszystkie_tabele:
                tekst_tabeli = t.get_text()
                if "HT" in tekst_tabeli and "BTS" in tekst_tabeli:
                    wiersze_test = t.find_all("tr")
                    if len(wiersze_test) > 15:  
                        tabela_meczow = t
                        break
            
            if tabela_meczow:
                wiersze_ss = tabela_meczow.find_all("tr")
                ostatnia_data = ""
                
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
                            data = teksty[wynik_index - 2] if wynik_index >= 2 else teksty[0]
                            gosc = teksty[wynik_index + 1] if wynik_index + 1 < len(teksty) else ""
                            
                            if "HOME" in gospodarz.upper() or "GOSPODARZ" in gospodarz.upper():
                                continue
                                
                            if data and len(data) > 2:
                                ostatnia_data = data
                            else:
                                data = ostatnia_data
                                
                            if gospodarz and gosc and gosc != gospodarz:
                                ht, o25, tg, bts = "-", "-", "-", "-"
                                pozostale_komorki = teksty[wynik_index + 2:]
                                statystyki = [s for s in pozostale_komorki if s.strip()] 
                                
                                if len(statystyki) >= 4:
                                    ht = statystyki[0]
                                    o25 = statystyki[1]
                                    tg = statystyki[2]
                                    bts = statystyki[3]
                                elif len(statystyki) > 0:
                                    ht = statystyki[0]
                                    if len(statystyki) > 1: o25 = statystyki[1]
                                    if len(statystyki) > 2: tg = statystyki[2]
                                    
                                wynik_czysty = wynik.replace("*", "").strip().replace(" ", "").replace("-", ":")
                                ht_czysty = ht.replace("*", "").strip().replace(" ", "").replace("-", ":").replace("(", "").replace(")", "")
                                o25_czysty = o25.replace("*", "").strip()
                                tg_czysty = tg.replace("*", "").strip()
                                bts_czysty = bts.replace("*", "").strip()
                                
                                g_gosp_m, g_gosc_m, suma_m = "-", "-", "-"
                                g_gosp_1h, g_gosc_1h, suma_1h = "-", "-", "-"
                                g_gosp_2h, g_gosc_2h, suma_2h = "-", "-", "-"
                                
                                if ":" in wynik_czysty:
                                    try:
                                        p_m = wynik_czysty.split(":")
                                        g_gosp_m = int(p_m[0])
                                        g_gosc_m = int(p_m[1])
                                        suma_m = g_gosp_m + g_gosc_m
                                    except:
                                        pass
                                        
                                if ":" in ht_czysty:
                                    try:
                                        p_1h = ht_czysty.split(":")
                                        g_gosp_1h = int(p_1h[0])
                                        g_gosc_1h = int(p_1h[1])
                                        suma_1h = g_gosp_1h + g_gosc_1h
                                    except:
                                        pass
                                        
                                if isinstance(g_gosp_m, int) and isinstance(g_gosp_1h, int):
                                    try:
                                        g_gosp_2h = g_gosp_m - g_gosp_1h
                                        g_gosc_2h = g_gosc_m - g_gosc_1h
                                        suma_2h = g_gosp_2h + g_gosc_2h
                                    except:
                                        pass
                                
                                dane_soccerstats_baza.append([
                                    nazwa_ligi, data, gospodarz, gosc,
                                    wynik_czysty, g_gosp_m, g_gosc_m, suma_m,
                                    ht_czysty, g_gosp_1h, g_gosc_1h, suma_1h,
                                    g_gosp_2h, g_gosc_2h, suma_2h,
                                    o25_czysty, tg_czysty, bts_czysty
                                ])
                        
        if dane_soccerstats_baza:
            ss_df = pd.DataFrame(
                dane_soccerstats_baza, 
                columns=[
                    "Liga", "Data", "Gospodarz", "Gosc",
                    "Wynik_Koncowy", "Gole_Gosp_Mecz", "Gole_Gosc_Mecz", "Suma_Goli_Mecz",
                    "Wynik_HT", "Gole_Gosp_1H", "Gole_Gosc_1H", "Suma_Goli_1H",
                    "Gole_Gosp_2H", "Gole_Gosc_2H", "Suma_Goli_2H",
                    "2.5+", "TG", "BTS"
                ]
            )
            ss_df = ss_df.drop_duplicates()
            print(f"Sukces! Pobrano i przeliczono {len(ss_df)} wierszy z SoccerStats.")
        else:
            ss_df = pd.DataFrame()
            
except Exception as e:
    print("Wystąpił błąd podczas pracy z SoccerStats:", e)
    ss_df = pd.DataFrame()

# ==========================================
# GOOGLE SHEETS AUTORYZACJA
# ==========================================
scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

if os.path.exists("credentials.json"):
    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=scope
    )
else:
    credentials_dict = json.loads(
        os.environ["GOOGLE_CREDENTIALS"]
    )
    creds = Credentials.from_service_account_info(
        credentials_dict,
        scopes=scope
    )

client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

try:
    summary_sheet = spreadsheet.worksheet("Summary")
except:
    summary_sheet = spreadsheet.add_worksheet(title="Summary", rows=100, cols=10)

# ==========================================
# PODZIAŁ DANYCH
# ==========================================
fixtures_df = df[df["Type"] == "Fixture"].copy()
results_df = df[df["Type"] == "Result"].copy()

fixtures_df = fixtures_df.sort_values(by=["Date", "Time"], ascending=True)
results_df = results_df.sort_values(by=["Date"], ascending=False)

# ==========================================================
# SEKCJA: SŁOWNIK MAPOWANIA I SCALANIE DANYCH (OPCJA B)
# ==========================================================
mapowanie_ss = {}
mapowanie_fd = {}

if os.path.exists("slownik_druzyn.json"):
    try:
        with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
            slownik_data = json.load(f)
            mapowanie_ss = slownik_data.get("SoccerStats_To_BetExplorer", {})
            mapowanie_fd = slownik_data.get("FootballData_To_BetExplorer", {})
            print(f"Załadowano słownik. Reguły SS: {len(mapowanie_ss)}, Reguły FD: {len(mapowanie_fd)}")
    except Exception as e:
        print("Błąd podczas ładowania slownik_druzyn.json:", e)

# --- MAPOWANIE I SCALANIE SOCCERSTATS ---
if not ss_df.empty:
    print("Ujednolicam nazwy drużyn dla SoccerStats...")
    ss_do_scalenia = ss_df.copy()
    
    # 1. Zmiana nazw kolumn na takie same jak w BetExplorer, aby funkcja merge je rozpoznała
    ss_do_scalenia = ss_do_scalenia.rename(columns={"Gospodarz": "Home", "Gosc": "Away"})
    
    # 2. Używamy słownika do podmienienia nazw drużyn
    ss_do_scalenia["Home"] = ss_do_scalenia["Home"].apply(lambda x: mapowanie_ss.get(x, x))
    ss_do_scalenia["Away"] = ss_do_scalenia["Away"].apply(lambda x: mapowanie_ss.get(x, x))
    
    # 3. Usuwamy kolumny z ligą, datą i surowym wynikiem (BetExplorer ma własne, lepsze)
    ss_do_scalenia = ss_do_scalenia.drop(columns=["Liga", "Wynik_Koncowy", "Wynik_HT", "Data"], errors="ignore")
    
    if not fixtures_df.empty:
        print("Scalam terminarz BetExplorer + SoccerStats...")
        fixtures_df = pd.merge(fixtures_df, ss_do_scalenia, on=["Home", "Away"], how="left")
        
    if not results_df.empty:
        print("Scalam historię BetExplorer + SoccerStats...")
        results_df = pd.merge(results_df, ss_do_scalenia, on=["Home", "Away"], how="left")

# --- INTEGRACJA FOOTBALL-DATA.CO.UK (ZAAWANSOWANE STATYSTYKI) ---
print("Rozpoczynam integrację danych z Football-Data.co.uk...")
try:
    url_fd = "https://www.football-data.co.uk/new/FIN.csv"
    fd_raw = pd.read_csv(url_fd, on_bad_lines='skip')
    
    if not fd_raw.empty and "Home" in fd_raw.columns:
        print("Pomyślnie pobrano plik zaawansowany z Football-Data!")
        
        kolumny_fd = ["Home", "Away", "HG", "AG", "HTHG", "HTAG", "HS", "AS", "HST", "AST", "HC", "AC"]
        istniejace_kolumny = [c for c in kolumny_fd if c in fd_raw.columns]
        fd_processed = fd_raw[istniejace_kolumny].copy()
        
        # Mapowanie drużyn dla Football-Data
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
            
        # Usuwamy podstawowe kolumny bramkowe z FD, bo SoccerStats/BetExplorer mają to dokładniej
        fd_final = fd_processed.drop(columns=["HG", "AG", "HTHG", "HTAG"], errors="ignore")
        
        if not results_df.empty:
            print("Doklejam statystyki rożnych i strzałów do tabeli Results...")
            results_df = pd.merge(results_df, fd_final, on=["Home", "Away"], how="left")
            
except Exception as e:
    print("Football-Data pominięte lub brak danych dla tej ligi:", e)

if not fixtures_df.empty: fixtures_df = fixtures_df.fillna("-")
if not results_df.empty: results_df = results_df.fillna("-")

# ==========================================
# GOOGLE SHEETS - ZAPIS SCALONYCH TABEL
# ==========================================
try:
    fixtures_sheet = spreadsheet.worksheet("Fixtures")
except:
    fixtures_sheet = spreadsheet.add_worksheet(title="Fixtures", rows=1000, cols=35)

try:
    results_sheet = spreadsheet.worksheet("Results")
except:
    results_sheet = spreadsheet.add_worksheet(title="Results", rows=5000, cols=35)

try:
    fixtures_sheet.resize(rows=1000, cols=35)
    results_sheet.resize(rows=5000, cols=35)
except:
    pass

for col in ["Odd1", "OddX", "Odd2"]:
    if col in fixtures_df.columns:
        fixtures_df[col] = fixtures_df[col].apply(lambda x: str(x).replace(".", ",") if str(x) != "-" else "-")
    if col in results_df.columns:
        results_df[col] = results_df[col].apply(lambda x: str(x).replace(".", ",") if str(x) != "-" else "-")

print("Wysyłam zintegrowany Terminarz do Google Sheets...")
fixtures_sheet.clear()
fixtures_sheet.update(
    [fixtures_df.columns.tolist()] +
    fixtures_df.astype(str).values.tolist()
)

print("Wysyłam zintegrowaną Historię ze statystykami rożnych i strzałów...")
results_sheet.clear()
results_sheet.update(
    [results_df.columns.tolist()] +
    results_df.astype(str).values.tolist()
)

try:
    stary_arkusz = spreadsheet.worksheet("SoccerStats_Model")
    spreadsheet.del_worksheet(stary_arkusz)
except:
    pass

summary_sheet.clear()
summary_sheet.update(
    [
        ["Metric", "Value"],
        ["Last Update", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["Fixtures Zintegrowane", len(fixtures_df)],
        ["Results Zintegrowane", len(results_df)],
        ["Baza Zaawansowana (Opcja B)", "TAK - Połączono 3 systemy"]
    ]
)

print()
print("=" * 60)
print("PROCES INTEGRACJI 3 SYSTEMÓW ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Fixtures (wzbogacone):", len(fixtures_df))
print("Results (wzbogacone + rożne + strzały):", len(results_df))
print("=" * 60)
