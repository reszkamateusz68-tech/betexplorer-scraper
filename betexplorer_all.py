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
# DATAFRAME (BETEXPLORER)
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

fixtures_df = df[df["Type"] == "Fixture"].copy()
results_df = df[df["Type"] == "Result"].copy()

fixtures_df = fixtures_df.sort_values(
    by=["Date", "Time"],
    ascending=True
)
results_df = results_df.sort_values(
    by=["Date"],
    ascending=False
)

# ==========================================
# POBIERANIE ZAWÓD TYPER
# ==========================================
print("Pobieram dodatkowe statystyki z Zawód Typer...")
zt_df = pd.DataFrame()
try:
    scraper = cloudscraper.create_scraper()
    url_zt = "https://zawodtyper.pl/statystyki-pilkarskie-over-under/"
    html_zt = scraper.get(url_zt, headers={"User-Agent": headers["User-Agent"]}, timeout=30).text
    soup_zt = BeautifulSoup(html_zt, "html.parser")
    tabela = soup_zt.find("table")
    
    dane_tabeli = []
    if tabela:
        wiersze = tabela.find_all("tr")
        for wiersz in wiersze:
            komorki = wiersz.find_all(["th", "td"])
            tekst_komorek = [komorka.get_text(strip=True) for komorka in komorki]
            if tekst_komorek:
                dane_tabeli.append(tekst_komorek)
                
    if dane_tabeli:
        naglowki = dane_tabeli[0]
        zawartosc = dane_tabeli[1:]
        zt_df = pd.DataFrame(zawartosc, columns=naglowki)
        print(f"Sukces! Pobrano {len(zt_df)} wierszy z Zawód Typer.")
    else:
        print("Nie znaleziono tabeli na stronie Zawód Typer.")
except Exception as e:
    print("Błąd podczas pobierania z Zawód Typer:", e)

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

# ==========================================
# AKTUALIZACJA ARKUSZY GOOGLE
# ==========================================

# 1. Summary
try:
    summary_sheet = spreadsheet.worksheet("Summary")
except:
    summary_sheet = spreadsheet.add_worksheet(title="Summary", rows=100, cols=10)

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

# 2. Fixtures
print("Aktualizacja Fixtures...")
try:
    fixtures_sheet = spreadsheet.worksheet("Fixtures")
except:
    fixtures_sheet = spreadsheet.add_worksheet(title="Fixtures", rows=1000, cols=20)

for col in ["Odd1", "OddX", "Odd2"]:
    fixtures_df[col] = fixtures_df[col].apply(
        lambda x: str(x).replace(".", ",") if str(x) != "-" else "-"
    )
fixtures_sheet.clear()
fixtures_sheet.update(
    [fixtures_df.columns.tolist()] +
    fixtures_df.astype(str).values.tolist()
)

# 3. Results
print("Aktualizacja Results...")
try:
    results_sheet = spreadsheet.worksheet("Results")
except:
    results_sheet = spreadsheet.add_worksheet(title="Results", rows=5000, cols=20)

for col in ["Odd1", "OddX", "Odd2"]:
    results_df[col] = results_df[col].apply(
        lambda x: str(x).replace(".", ",") if str(x) != "-" else "-"
    )
results_sheet.clear()
results_sheet.update(
    [results_df.columns.tolist()] +
    results_df.astype(str).values.tolist()
)

# 4. Zawód Typer (NOWOŚĆ)
if not zt_df.empty:
    print("Aktualizacja zakładki ZawodTyper...")
    try:
        arkusz_zt = spreadsheet.worksheet("ZawodTyper")
    except:
        arkusz_zt = spreadsheet.add_worksheet(title="ZawodTyper", rows=1000, cols=15)
    
    arkusz_zt.clear()
    arkusz_zt.update(
        [zt_df.columns.tolist()] + 
        zt_df.astype(str).values.tolist()
    )
    print("Zakładka ZawodTyper zaktualizowana pomyślnie!")

print()
print("=" * 60)
print("GOTOWE")
print("Fixtures:", len(fixtures_df))
print("Results:", len(results_df))
if not zt_df.empty:
    print("Zawód Typer:", len(zt_df))
print("=" * 60)
