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
# SEKCJA: SOCCERSTATS (POBIERANIE LINKÓW Z EXCELA)
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
            
            # Pobieramy nazwę ligi z linku (np. finland, england)
            try:
                nazwa_ligi = url_ss.split("league=")[1].split("&")[0]
            except:
                nazwa_ligi = f"Liga_{i_ss}"
                
            print(f"[{i_ss}/{len(urls_ss)}] Pobieram SoccerStats dla: {nazwa_ligi}")
            
            html_ss = skaner_ss.get(url_ss, headers={"User-Agent": headers["User-Agent"]}, timeout=30).text
            soup_ss = BeautifulSoup(html_ss, "html.parser")
            
            # Szukamy konkretnej tabeli z meczami (ma klasę 'sortable')
            tabela_meczow = soup_ss.find("table", class_="sortable")
            
            if tabela_meczow:
                wiersze_ss = tabela_meczow.find_all("tr", class_="odd")
                
                for wiersz in wiersze_ss:
                    komorki = wiersz.find_all("td")
                    # Interesują nas tylko wiersze, które mają odpowiednią liczbę kolumn meczowych
                    if len(komorki) >= 9:
                        data = komorki[0].get_text(strip=True)
                        gospodarz = komorki[1].get_text(strip=True)
                        wynik = komorki[2].get_text(strip=True)
                        gosc = komorki[3].get_text(strip=True)
                        ht = komorki[4].get_text(strip=True)
                        o25 = komorki[5].get_text(strip=True)
                        tg = komorki[6].get_text(strip=True)
                        bts = komorki[7].get_text(strip=True)
                        
                        # Zapisujemy tylko jeśli wiersz zawiera dane meczu (np. ma podany wynik lub gwiazdki)
                        if gospodarz and gosc:
                            dane_soccerstats_baza.append([
                                nazwa_ligi, data, gospodarz, wynik, gosc, ht, o25, tg, bts
                            ])
                        
        if dane_soccerstats_baza:
            # Tworzymy piękny DataFrame z jasnymi, ustrukturyzowanymi nagłówkami
            ss_df = pd.DataFrame(dane_soccerstats_baza, columns=[
                "Liga", "Data", "Gospodarz", "Wynik", "Gosc", "HT", "2.5+", "TG", "BTS"
            ])
            print(f"Sukces! Pobrano łącznie {len(ss_df)} ustrukturyzowanych wierszy z SoccerStats.")
        else:
            print("Nie udało się wyciągnąć wierszy z tabeli meczowej sortable.")
            ss_df = pd.DataFrame()
    else:
        print("Błąd: Nie znaleziono pliku ligi_soccerstats.xlsx na GitHubie!")
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

# ==========================================
# TWORZENIE / POBIERANIE ARKUSZY
# ==========================================
try:
    fixtures_sheet = spreadsheet.worksheet("Fixtures")
except:
    fixtures_sheet = spreadsheet.add_worksheet(title="Fixtures", rows=1000, cols=20)

try:
    results_sheet = spreadsheet.worksheet("Results")
except:
    results_sheet = spreadsheet.add_worksheet(title="Results", rows=5000, cols=20)

# ==========================================
# FIXTURES UPDATE
# ==========================================
print("Aktualizacja Fixtures...")
for col in ["Odd1", "OddX", "Odd2"]:
    fixtures_df[col] = fixtures_df[col].apply(
        lambda x: str(x).replace(".", ",") if str(x) != "-" else "-"
    )
fixtures_sheet.clear()
fixtures_sheet.update(
    [fixtures_df.columns.tolist()] +
    fixtures_df.astype(str).values.tolist()
)

# ==========================================
# RESULTS UPDATE
# ==========================================
print("Aktualizacja Results...")
for col in ["Odd1", "OddX", "Odd2"]:
    results_df[col] = results_df[col].apply(
        lambda x: str(x).replace(".", ",") if str(x) != "-" else "-"
    )
results_sheet.clear()
results_sheet.update(
    [results_df.columns.tolist()] +
    results_df.astype(str).values.tolist()
)

# ==========================================
# SOCCERSTATS UPDATE
# ==========================================
if not ss_df.empty:
    print("Wysyłam statystyki SoccerStats do Google Sheets...")
    try:
        try:
            arkusz_ss = spreadsheet.worksheet("SoccerStats_Model")
        except:
            arkusz_ss = spreadsheet.add_worksheet(title="SoccerStats_Model", rows=2000, cols=25)
        
        # CZYSZCZENIE WARTOŚCI NAN / INF PRZED WYSYŁKĄ (NAPRAWA BŁĘDU JSON)
        ss_df = ss_df.fillna("")  # Zamienia wszystkie wartości NaN na pusty tekst
        
        arkusz_ss.clear()
        arkusz_ss.update(
            [ss_df.columns.tolist()] + 
            ss_df.astype(str).values.tolist()
        )
        print("Zakładka SoccerStats_Model została zaktualizowana!")
    except Exception as e:
        print("Błąd zapisu zakładki SoccerStats_Model:", e)

# ==========================================
# SUMMARY UPDATE
# ==========================================
summary_sheet.clear()
summary_sheet.update(
    [
        ["Metric", "Value"],
        ["Last Update", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["Fixtures", len(fixtures_df)],
        ["Results", len(results_df)],
        ["Leagues", df["League"].nunique()],
        ["Total Rows", len(df)]
    ]
)

print()
print("=" * 60)
print("GOTOWE")
print("Fixtures:", len(fixtures_df))
print("Results:", len(results_df))
if not ss_df.empty:
    print("SoccerStats wierszy:", len(ss_df))
print("=" * 60)
