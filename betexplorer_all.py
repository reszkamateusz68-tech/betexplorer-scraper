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

# Wczytanie słownika z mapowaniem nazw drużyn
try:
    with open("slownik_druzyn.json", "r", encoding="utf-8") as f:
        slownik = json.load(f)
        fd_dict = slownik.get("FootballData_To_BetExplorer", {})
        ss_dict = slownik.get("SoccerStats_To_BetExplorer", {})
except FileNotFoundError:
    print("Brak pliku slownik_druzyn.json. Pobieram dane bez mapowania nazw.")
    fd_dict = {}
    ss_dict = {}

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
    except: pass

    try: return (datetime.strptime(value, "%d.%m.%Y").date(), "")
    except: pass

    try:
        if value.endswith("."):
            day, month = value.rstrip(".").split(".")
            return (datetime(today.year, int(month), int(day)).date(), "")
    except: pass

    return value, ""

def fetch_football_data():
    print("Pobieram linki i statystyki z ligi_footballdata.xlsx...")
    
    try:
        urls = pd.read_excel("ligi_footballdata.xlsx")["URL"].dropna().tolist()
    except Exception as e:
        print(f"Błąd podczas wczytywania pliku Excel: {e}")
        return pd.DataFrame()
    
    dfs = []
    for url in urls:
        try:
            df_fd = pd.read_csv(url.strip())
            # Zabezpieczenie przed pustymi wierszami w pliku z football-data
            df_fd = df_fd.dropna(subset=['HomeTeam']) 
            dfs.append(df_fd)
        except Exception as e:
            print(f"Błąd pobierania CSV z {url}: {e}")
            
    if not dfs:
        return pd.DataFrame()
        
    fd_master = pd.concat(dfs, ignore_index=True)
    
    cols_to_keep = ['Date', 'HomeTeam', 'AwayTeam', 
                    'HTHG', 'HTAG', 'HS', 'AS', 'HST', 'AST', 
                    'HC', 'AC', 'HY', 'AY', 'AvgH', 'AvgD', 'AvgA']
    
    existing_cols = [col for col in cols_to_keep if col in fd_master.columns]
    fd_master = fd_master[existing_cols]
    return fd_master

scrape_report = []

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache"
}

# ==========================================================
# 1. POBIERANIE Z BETEXPLORER
# ==========================================================
all_data = []

try: urls_be = pd.read_excel("ligi.xlsx")["URL"].dropna().tolist()
except: urls_be = []

# Inicjujemy pancernego scrapera dla BetExplorera przed pętlą
scraper_be = cloudscraper.create_scraper()

for i, url in enumerate(urls_be, start=1):
    url_clean = str(url).strip()
    print(f"[{i}/{len(urls_be)}] Pobieram BetExplorer: {url_clean}")
    
    if "/fixtures/" not in url_clean and "/results/" not in url_clean:
        scrape_report.append(["BetExplorer", url_clean, "BŁĄD: Zły link"])
        continue
        
    try:
        # Zmieniamy requests.get na scraper_be.get
        response = scraper_be.get(url_clean, headers=headers, timeout=30)
        if response.status_code != 200:
            scrape_report.append(["BetExplorer", url_clean, f"BŁĄD: Kod {response.status_code}"])
            continue
            
        soup = BeautifulSoup(response.text, "html.parser")

        try:
            if "/football/" in url_clean: league = url_clean.split("/football/")[1].replace("/fixtures/", "").replace("/results/", "").strip("/")
            else: league = url_clean.split(".com/")[1].replace("/fixtures/", "").replace("/results/", "").strip("/")
        except: league = "Unknown_League"

        rows = soup.find_all("tr")
        mecz_count = 0

        # FIXTURES
        if "/fixtures/" in url_clean:
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
                mecz_count += 1
            scrape_report.append(["BetExplorer", url_clean, f"OK (Pobrano: {mecz_count} meczów)"])

        # RESULTS
        elif "/results/" in url_clean:
            current_date = ""
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
                    # -- NAPRAWA: Zabezpieczenie przed ukrytymi kursami z historii --
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

                date_cell = row.find("td", class_=lambda x: x and "h-text-right" in x)
                if date_cell:
                    parsed_date = date_cell.get_text(strip=True)
                    if parsed_date: current_date = parsed_date

                all_data.append(["Result", league, current_date, home, away, score, odd1, oddx, odd2])
                mecz_count += 1
            
            if mecz_count == 0:
                scrape_report.append(["BetExplorer", url_clean, "BŁĄD: Znaleziono 0 meczów"])
            else:
                scrape_report.append(["BetExplorer", url_clean, f"OK (Pobrano: {mecz_count} meczów)"])

    except Exception as e:
        scrape_report.append(["BetExplorer", url_clean, f"BŁĄD: {str(e)}"])

# ==========================================================
# DATAFRAME Z BETEXPLORER
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

fixtures_df = df[df["Type"] == "Fixture"].copy().sort_values(by=["Date", "Time"], ascending=True)
results_df = df[df["Type"] == "Result"].copy().sort_values(by=["Date"], ascending=False)

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
            
            try:
                html_ss = skaner_ss.get(url_ss, headers={"User-Agent": headers["User-Agent"]}, timeout=30).text
                soup_ss = BeautifulSoup(html_ss, "html.parser")
                
                tabela_meczow = None
                for t in soup_ss.find_all("table"):
                    if "HT" in t.get_text() and "BTS" in t.get_text() and len(t.find_all("tr")) > 15:  
                        tabela_meczow = t
                        break
                
                if tabela_meczow:
                    wiersze_ss = tabela_meczow.find_all("tr")
                    ostatnia_data = ""
                    ss_count = 0
                    
                    for wiersz in wiersze_ss:
                        komorki = wiersz.find_all(["td", "th"])
                        if len(komorki) >= 6:
                            teksty = [k.get_text(" ", strip=True) for k in komorki]
                            wynik_index = -1
                            
                            for idx, val in enumerate(teksty):
                                if ("-" in val or ":" in val) and any(c.isdigit() for c in val) and 1 <= idx <= 5: 
                                    wynik_index = idx
                                    break
                                        
                            if wynik_index != -1:
                                wynik = teksty[wynik_index]
                                gospodarz = teksty[wynik_index - 1]
                                data = teksty[wynik_index - 2] if wynik_index >= 2 else teksty[0]
                                gosc = teksty[wynik_index + 1] if wynik_index + 1 < len(teksty) else ""
                                
                                if "HOME" in gospodarz.upper() or "GOSPODARZ" in gospodarz.upper(): continue
                                    
                                if data and len(data) > 2: ostatnia_data = data
                                else: data = ostatnia_data
                                    
                                if gospodarz and gosc and gosc != gospodarz:
                                    ht, o25, tg, bts = "-", "-", "-", "-"
                                    statystyki = [s for s in teksty[wynik_index + 2:] if s.strip()] 
                                    
                                    if len(statystyki) >= 4:
                                        ht, o25, tg, bts = statystyki[0], statystyki[1], statystyki[2], statystyki[3]
                                    elif len(statystyki) > 0:
                                        ht = statystyki[0]
                                        if len(statystyki) > 1: o25 = statystyki[1]
                                        if len(statystyki) > 2: tg = statystyki[2]
                                        
                                    wynik_czysty = wynik.replace("*", "").strip()
                                    if "-" in wynik_czysty: wynik_czysty = wynik_czysty.replace("-", ":").replace(" ", "")
                                    ht_czysty = ht.replace("*", "").strip()
                                    
                                    dane_soccerstats_baza.append([gospodarz, wynik_czysty, gosc, ht_czysty, o25.replace("*", "").strip(), tg.replace("*", "").strip(), bts.replace("*", "").strip()])
                                    ss_count += 1
                                    
                    if ss_count == 0: scrape_report.append(["SoccerStats", url_ss, "BŁĄD: Znaleziono tabelę, brak wierszy"])
                    else: scrape_report.append(["SoccerStats", url_ss, f"OK (Pobrano: {ss_count} wierszy)"])
                else:
                    scrape_report.append(["SoccerStats", url_ss, "BŁĄD: Nie znaleziono tabeli wyników"])
            except Exception as e:
                scrape_report.append(["SoccerStats", url_ss, f"BŁĄD: {str(e)}"])
                    
    if dane_soccerstats_baza:
        ss_df = pd.DataFrame(dane_soccerstats_baza, columns=["Home", "Score", "Away", "HT", "2.5+", "TG", "BTS"]).drop_duplicates()
    else:
        ss_df = pd.DataFrame()
except Exception as e:
    print("Wystąpił błąd SoccerStats:", e)
    ss_df = pd.DataFrame()

# ==========================================================
# 3. BEZPIECZNE SCALANIE DANYCH (Teraz pancerne!)
# ==========================================================
print("Przetwarzam i scalam statystyki (SoccerStats + Football-Data)...")

# 1. Konwersja daty i usunięcie białych znaków (BARDZO WAŻNE DLA ZŁĄCZENIA)
results_df['Date'] = pd.to_datetime(results_df['Date'], errors='coerce').astype(str)
results_df['Home'] = results_df['Home'].astype(str).str.strip()
results_df['Away'] = results_df['Away'].astype(str).str.strip()

# --- SCALANIE SOCCERSTATS ---
if not ss_df.empty:
    ss_df['Home'] = ss_df['Home'].astype(str).str.strip().replace(ss_dict)
    ss_df['Away'] = ss_df['Away'].astype(str).str.strip().replace(ss_dict)
    
    # Usuwamy ewentualne duplikaty meczów, zostawiając najnowsze
    ss_df = ss_df.drop_duplicates(subset=['Home', 'Away'], keep='last')
    
    # Łączenie po samych nazwach drużyn (bo w SoccerStats daty często nie mają roku)
    results_df = pd.merge(
        results_df,
        ss_df[['Home', 'Away', 'HT', '2.5+', 'TG', 'BTS']],
        how='left',
        on=['Home', 'Away']
    )
else:
    for col in ['HT', '2.5+', 'TG', 'BTS']: results_df[col] = "-"

# --- SCALANIE FOOTBALL DATA ---
fd_df = fetch_football_data()

if not fd_df.empty:
    fd_df['HomeTeam'] = fd_df['HomeTeam'].astype(str).str.strip().replace(fd_dict)
    fd_df['AwayTeam'] = fd_df['AwayTeam'].astype(str).str.strip().replace(fd_dict)
    fd_df['Date'] = pd.to_datetime(fd_df['Date'], dayfirst=True, errors='coerce').astype(str)
    
    fd_df = fd_df.drop_duplicates(subset=['Date', 'HomeTeam', 'AwayTeam'], keep='last')
    
    results_df = pd.merge(
        results_df, 
        fd_df, 
        how='left', 
        left_on=['Date', 'Home', 'Away'], 
        right_on=['Date', 'HomeTeam', 'AwayTeam']
    )
    results_df = results_df.drop(columns=['HomeTeam', 'AwayTeam'], errors='ignore')
else:
    fd_cols = ['HTHG', 'HTAG', 'HS', 'AS', 'HST', 'AST', 'HC', 'AC', 'HY', 'AY', 'AvgH', 'AvgD', 'AvgA']
    for col in fd_cols: results_df[col] = "-"

# Czyszczenie wyników: zamieniamy wszelkie "nan" i puste komórki na "-"
results_df = results_df.fillna("-").replace(["nan", "NaN", "NaT", ""], "-")
fixtures_df = fixtures_df.fillna("-").replace(["nan", "NaN", "NaT", ""], "-")

# ==========================================
# 4. GOOGLE SHEETS AUTORYZACJA I WYSYŁKA
# ==========================================
scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

if os.path.exists("credentials.json"): creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
else: creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scope)

client = gspread.authorize(creds)
spreadsheet = client.open("BetExplorer")

try: fixtures_sheet = spreadsheet.worksheet("Fixtures")
except: fixtures_sheet = spreadsheet.add_worksheet(title="Fixtures", rows=5000, cols=35)

try: results_sheet = spreadsheet.worksheet("Results")
except: results_sheet = spreadsheet.add_worksheet(title="Results", rows=5000, cols=55)

try: summary_sheet = spreadsheet.worksheet("Summary")
except: summary_sheet = spreadsheet.add_worksheet(title="Summary", rows=100, cols=10)

try:
    fixtures_sheet.resize(rows=5000, cols=35)
    results_sheet.resize(rows=5000, cols=55)
except: pass

# Zmiana kropek na przecinki dla polskich arkuszy (z pominięciem myślników i pustych miejsc)
for col in ["Odd1", "OddX", "Odd2", "AvgH", "AvgD", "AvgA"]:
    if col in fixtures_df.columns:
        fixtures_df[col] = fixtures_df[col].astype(str).apply(lambda x: x.replace(".", ",") if x not in ["-", ""] else "-")
    if col in results_df.columns:
        results_df[col] = results_df[col].astype(str).apply(lambda x: x.replace(".", ",") if x not in ["-", ""] else "-")

print("Wysyłam Fixtures...")
fixtures_sheet.clear()
if not fixtures_df.empty: fixtures_sheet.update([fixtures_df.columns.tolist()] + fixtures_df.astype(str).values.tolist())

print("Wysyłam Results...")
results_sheet.clear()
if not results_df.empty: results_sheet.update([results_df.columns.tolist()] + results_df.astype(str).values.tolist())

print("Wysyłam Summary...")
summary_data = [
    ["==== PODSUMOWANIE OGÓLNE ====", "", ""],
    ["Ostatnia aktualizacja", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""],
    ["Fixtures", len(fixtures_df), ""],
    ["Results", len(results_df), ""],
    ["Leagues", df["League"].nunique() if not df.empty else 0, ""],
    ["", "", ""],
    ["==== RAPORT POBIERANIA Z LINKÓW ====", "", ""],
    ["System", "URL", "Status / Wynik"]
]
summary_data.extend(scrape_report)

summary_sheet.clear()
summary_sheet.update(summary_data)

print("\n" + "=" * 60)
print("PROCES ZAKOŃCZONY PEŁNYM SUKCESEM!")
print("Fixtures:", len(fixtures_df))
print("Results:", len(results_df))
print("=" * 60)
