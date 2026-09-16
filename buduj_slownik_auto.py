import json
import glob
import re
import os

def normalizuj_nazwe(tekst):
    t = str(tekst).lower().strip()
    t = re.sub(r'[\(\)\+\-\:\/]', ' ', t)
    return " ".join(t.split())

def pobierz_strukture_rynkow(dane):
    """Wyciąga rynki niezależnie od tego, czy plik jest słownikiem, czy listą."""
    rynki = {}
    if isinstance(dane, dict):
        for mid, mval in dane.items():
            if not isinstance(mval, dict):
                continue
            # Szukanie zagnieżdżonych rynków w strukturach Superbet/Fortuna
            for k, v in mval.items():
                if k in ['info', 'kursy', 'match_info']:
                    continue
                if isinstance(v, dict):
                    rynki[k] = list(v.keys())
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict) and ('name' in item or 'market_name' in item):
                            m_nazwa = item.get('name') or item.get('market_name')
                            odds = [o.get('name', '') for o in item.get('odds', []) if isinstance(o, dict)]
                            rynki[m_nazwa] = odds
    elif isinstance(dane, list):
        for item in dane:
            if isinstance(item, dict):
                m_nazwa = item.get('market_name') or item.get('name')
                if m_nazwa:
                    rynki[m_nazwa] = []
    return rynki

def main():
    print("🚀 Budowanie uniwersalnego słownika rynków Superbet & Fortuna...")

    slownik = {
        "Superbet": {
            # 1X2 i Podwójna szansa
            "1": {"market": ["mecz", "1x2", "wynik meczu"], "pick": ["1"]},
            "X": {"market": ["mecz", "1x2", "wynik meczu"], "pick": ["x"]},
            "2": {"market": ["mecz", "1x2", "wynik meczu"], "pick": ["2"]},
            "1X": {"market": ["podwójna szansa", "podwojna szansa"], "pick": ["1x", "1-x"]},
            "X2": {"market": ["podwójna szansa", "podwojna szansa"], "pick": ["x2", "x-2"]},
            
            # Gole Mecz (Overy i Undery)
            "GOALS_MATCH": ["liczba goli", "suma goli", "mecz - liczba goli", "suma bramek"],
            "GOALS_HT": ["1.połowa - liczba goli", "1. połowa - suma goli", "1.połowa liczba bramek"],
            "GOALS_2H": ["2.połowa - liczba goli", "2. połowa - suma goli"],
            
            # Handicapy Azjatyckie
            "AH_FT": ["handicap azjatycki", "handicap"],
            "AH_HT": ["1.połowa - handicap azjatycki", "1. połowa handicap"],
            "AH_2H": ["2.połowa - handicap azjatycki", "2. połowa handicap"],
            
            # Multigole
            "MG_1-5": {"market": ["przedział goli", "liczba goli - zakres"], "pick": ["1-5", "1 do 5"]},
            "MG_1-6": {"market": ["przedział goli", "liczba goli - zakres"], "pick": ["1-6", "1 do 6"]},
            
            # Rzuty Rożne
            "CORNERS_MATCH": ["rzuty rożne", "liczba rzutów rożnych", "suma rzutów rożnych"],
            "CORNERS_HOME": ["gospodarz - liczba rzutów rożnych", "{HOME} - liczba rzutów rożnych"],
            "CORNERS_AWAY": ["gość - liczba rzutów rożnych", "{AWAY} - liczba rzutów rożnych"],
            
            # Strzały i Strzały celne
            "SHOTS_1X2": ["strzały - 1x2", "liczba strzałów - 1x2", "spotkanie - strzały ogółem"],
            "SHOTS_MATCH": ["liczba strzałów", "suma strzałów w meczu"],
            "SOT_1X2": ["celne strzały - 1x2", "strzały w światło bramki - 1x2"],
            "SOT_MATCH": ["liczba celnych strzałów", "strzały celne w meczu"]
        },
        "Fortuna": {
            # 1X2 i Podwójna szansa
            "1": {"market": ["mecz", "1x2", "wynik meczu"], "pick": ["1"]},
            "X": {"market": ["mecz", "1x2", "wynik meczu"], "pick": ["0", "x"]},
            "2": {"market": ["mecz", "1x2", "wynik meczu"], "pick": ["2"]},
            "1X": {"market": ["podwójna szansa", "1x2 z podpórką"], "pick": ["10", "1x"]},
            "X2": {"market": ["podwójna szansa", "1x2 z podpórką"], "pick": ["02", "x2"]},
            
            # Gole Mecz
            "GOALS_MATCH": ["ilość bramek", "liczba bramek w meczu", "suma bramek"],
            "GOALS_HT": ["1.połowa - ilość bramek", "1.połowa liczba bramek"],
            "GOALS_2H": ["2.połowa - ilość bramek"],
            
            # Handicapy
            "AH_FT": ["handicap", "handicap azjatycki"],
            "AH_HT": ["1.połowa - handicap"],
            "AH_2H": ["2.połowa - handicap"],
            
            # Multigole
            "MG_1-5": {"market": ["przedział ilości bramek", "zakres bramek"], "pick": ["1-5"]},
            "MG_1-6": {"market": ["przedział ilości bramek", "zakres bramek"], "pick": ["1-6"]},
            
            # Rzuty Rożne
            "CORNERS_MATCH": ["ilość rzutów rożnych", "suma rzutów rożnych w meczu"],
            "CORNERS_HOME": ["gospodarz ilość rzutów rożnych"],
            "CORNERS_AWAY": ["gość ilość rzutów rożnych"],
            
            # Strzały i Strzały celne
            "SHOTS_1X2": ["spotkanie - strzały ogółem", "strzały 1x2"],
            "SHOTS_MATCH": ["ilość strzałów ogółem w spotkaniu"],
            "SOT_1X2": ["spotkanie - strzały celne", "strzały celne 1x2"],
            "SOT_MATCH": ["ilość strzałów celnych w spotkaniu"]
        }
    }

    with open("slownik_rynkow.json", "w", encoding="utf-8") as f:
        json.dump(slownik, f, ensure_ascii=False, indent=2)

    print("✅ Utworzono 'slownik_rynkow.json' ze zintegrowanymi wzorcami dla Superbet i Fortuny.")

if __name__ == "__main__":
    main()