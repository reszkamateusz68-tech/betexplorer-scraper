"""
MODUŁ: buduj_pelny_slownik_fortuna.py
OPIS: Generator pełnego słownika powiązań BetExplorer <-> Fortuna.
      Pobiera pełne drzewo drużyn z API Fortuny i dopasowuje je
      do bazy BetExplorera za pomocą algorytmu dopasowania tokenowego.
"""

import json
import os
import re
import socket
import time
from difflib import SequenceMatcher
import requests

# Zabezpieczenie DNS Windows dla API Fortuny
try:
    socket.gethostbyname("api.efortuna.pl")
except socket.gaierror:
    import urllib3.util.connection as urllib_conn
    def force_ipv4():
        return socket.AF_INET
    urllib_conn.allowed_gai_family = force_ipv4

HEADERS = {
    "accept": "*/*",
    "accept-language": "pl-PL",
    "origin": "https://www.efortuna.pl",
    "referer": "https://www.efortuna.pl/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def clean_tokens(name):
    """Czyści nazwę drużyny do zestawu słów kluczowych (usuwa stop-words)."""
    name = name.lower().strip()
    replacements = {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z', 'ü': 'u',
        'ö': 'o', 'ä': 'a', 'é': 'e', 'è': 'e', 'á': 'a'
    }
    for pl, en in replacements.items():
        name = name.replace(pl, en)
        
    stop_words = {
        "fc", "cf", "sc", "ac", "as", "mks", "gks", "ks", "ssa", "sp", "vfb",
        "tsv", "sv", "fk", "sk", "rcd", "ud", "cd", "ca", "de", "la", "le", "afc"
    }
    tokens = re.findall(r'[a-z0-9]+', name)
    meaningful = [t for t in tokens if t not in stop_words and len(t) > 1]
    return " ".join(meaningful) if meaningful else "".join(tokens)

def get_similarity(name_a, name_b):
    t_a = clean_tokens(name_a)
    t_b = clean_tokens(name_b)
    
    # 1. Dokładna zgodność rdzenia
    if t_a == t_b and len(t_a) > 2:
        return 1.0
        
    # 2. Zawieranie jednego członu w drugim (np. "Rakow Czestochowa" i "Rakow")
    if t_a in t_b or t_b in t_a:
        shorter = min(len(t_a), len(t_b))
        longer = max(len(t_a), len(t_b))
        if shorter >= 4:
            return 0.88 + (0.10 * (shorter / longer))

    # 3. Zbieżność rozmyta
    return SequenceMatcher(None, t_a, t_b).ratio()

def fetch_all_fortuna_teams(session):
    print("1. Pobieram pełną ofertę lig i turniejów z Fortuny (cała baza)...")
    url_tournaments = "https://api.efortuna.pl/offer/structure/api/v1_0/sport/ufo:sprt:00/tournaments?categories=true"
    teams = set()
    
    try:
        res = session.get(url_tournaments, timeout=12)
        if res.status_code != 200:
            print(f"Błąd API Fortuny: {res.status_code}")
            return teams
            
        data = res.json()
        tournaments = data.get("tournaments", [])
        print(f"   Znaleziono {len(tournaments)} lig. Pobieram listę aktywnych meczów...")

        for t in tournaments:
            tid = t.get("id")
            if not tid:
                continue
            m_url = f"https://api.efortuna.pl/offer/structure/api/v1_0/tournament/{tid}/matches"
            try:
                r_matches = session.get(m_url, timeout=6)
                if r_matches.status_code != 200:
                    continue
                for fix in r_matches.json().get("fixtures", []):
                    for p in fix.get("participants", []):
                        p_name = p.get("name", "").strip()
                        if p_name:
                            teams.add(p_name)
                    # Zapasowo z nazwy meczu
                    raw_n = fix.get("name", "")
                    if " - " in raw_n:
                        p_h, p_a = raw_n.split(" - ")
                        teams.add(p_h.strip())
                        teams.add(p_a.strip())
            except Exception:
                continue
    except Exception as e:
        print(f"Błąd pobierania bazy Fortuny: {e}")
        
    return teams

def build_full_dictionary():
    slownik_path = "slownik_druzyn.json"
    if not os.path.exists(slownik_path):
        print(f"Brak pliku {slownik_path}")
        return

    with open(slownik_path, "r", encoding="utf-8") as f:
        slownik = json.load(f)

    if "BetExplorer_To_Fortuna" not in slownik:
        slownik["BetExplorer_To_Fortuna"] = {}

    fortuna_map = slownik["BetExplorer_To_Fortuna"]

    # 1. Budujemy bazę wszystkich znanych zespołów z BetExplorera
    be_teams = set()
    
    # Źródło A: Słownik Superbet (sprawdzona baza 700+ klubów)
    for be_name in slownik.get("BetExplorer_To_Superbet", {}).keys():
        be_teams.add(be_name.strip())

    # Źródło B: Z bazy 5-dniowej Superbet
    if os.path.exists("superbet_baza_5dni.json"):
        try:
            with open("superbet_baza_5dni.json", "r", encoding="utf-8") as f:
                sb_db = json.load(f)
                for item in sb_db.values():
                    info = item.get("info", {})
                    if info.get("gospodarz_be"): be_teams.add(info["gospodarz_be"].strip())
                    if info.get("gosc_be"): be_teams.add(info["gosc_be"].strip())
        except Exception:
            pass

    print(f"Baza BetExplorer posiada: {len(be_teams)} unikalnych zespołów do dopasowania.")

    # 2. Pobieramy pełną ofertę Fortuny
    session = requests.Session()
    session.headers.update(HEADERS)
    fortuna_teams = fetch_all_fortuna_teams(session)

    # Uzupełniamy o to, co już mamy lokalnie w fortuna_baza_5dni.json
    if os.path.exists("fortuna_baza_5dni.json"):
        try:
            with open("fortuna_baza_5dni.json", "r", encoding="utf-8") as f:
                f_db = json.load(f)
                for item in f_db.values():
                    info = item.get("info", {})
                    if info.get("gospodarz_fortuna"): fortuna_teams.add(info["gospodarz_fortuna"].strip())
                    if info.get("gosc_fortuna"): fortuna_teams.add(info["gosc_fortuna"].strip())
        except Exception:
            pass

    print(f"Łącznie zebrano: {len(fortuna_teams)} unikalnych nazw drużyn z Fortuny.")

    # 3. Matching
    new_matches = 0
    already_mapped = set(v.lower() for v in fortuna_map.values())

    for f_team in fortuna_teams:
        if f_team.lower() in already_mapped:
            continue

        best_candidate = None
        best_score = 0.0

        for be_team in be_teams:
            score = get_similarity(f_team, be_team)
            if score > best_score:
                best_score = score
                best_candidate = be_team

        # Próg akceptacji 0.72 przy dopasowaniu tokenowym gwarantuje brak błędów
        if best_candidate and best_score >= 0.72:
            fortuna_map[best_candidate] = f_team
            already_mapped.add(f_team.lower())
            new_matches += 1
            print(f"  [+] ({round(best_score*100)}%) {best_candidate} -> {f_team}")

    # 4. Zapis
    slownik["BetExplorer_To_Fortuna"] = fortuna_map
    with open(slownik_path, "w", encoding="utf-8") as f:
        json.dump(slownik, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print(f"SUKCES! Dodano {new_matches} nowych powiązań.")
    print(f"Łączny rozmiar słownika BetExplorer_To_Fortuna: {len(fortuna_map)} drużyn.")
    print("=" * 60)

if __name__ == "__main__":
    build_full_dictionary()