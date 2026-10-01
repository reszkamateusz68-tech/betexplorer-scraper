"""
====================================================================================================
PROJEKT: STATLAB ANALYTICS - SYSTEM DIAGNOSTYKI I AUDYTU KURSÓW
MODUŁ: weryfikuj_kursy.py
OPIS: Zaawansowane narzędzie CLI do precyzyjnego audytu kursów Superbet i Fortuna.
      Pełna obsługa 6 poziomów kontroli:
      1. Weryfikacja obecności linii w ofercie bukmachera
      2. Wykrywanie braku całego meczu w bazie
      3. Smart Fallback: dobór najbliższej linii alternatywnej + kalkulacja delty szans (Poisson)
      4. Detekcja błędów mapowania nazw drużyn (częściowe dopasowanie z podpowiedzią do słownika)
      5. Weryfikacja specyficznego zapisu rynków i opcji (Handicapy z plusem/bez plusa w nawiasie)
      6. Wykrywanie anomalii kursowych (odchylenia od szacunku i rozbieżności między bukmacherami)
====================================================================================================
"""

import sys
import json
import os
import re
import math
import glob

def clean_txt(s):
    t = str(s).strip().lower()
    for pl, en in [('ą','a'),('ć','c'),('ę','e'),('ł','l'),('ń','n'),('ó','o'),('ś','s'),('ź','z'),('ż','z')]:
        t = t.replace(pl, en)
    t = re.sub(r'\b(fc|ks|gks|mks|ac|as|cf|ss|sc|sa|sp|vfb|tsv|sv|fk|sk|stade|de|hsc)\b', '', t)
    return re.sub(r'[^a-z0-9]', '', t)

def get_tokens(name):
    c = clean_txt(name)
    toks = {c, c[:4]} if len(c) >= 3 else {c}
    for w in re.findall(r'[a-zA-Z0-9]+', str(name).lower()):
        cw = clean_txt(w)
        if len(cw) >= 3 and cw not in ["team", "club", "city", "town", "utd", "stade"]:
            toks.add(cw)
            toks.add(cw[:4])
    return list(toks)

def is_odd_sane(typ_kod, odd_val):
    if odd_val is None or odd_val <= 1.005: return False
    k = str(typ_kod).strip().upper()
    if k == "U6.5" and odd_val > 1.06: return False
    if k == "U5.5" and odd_val > 1.18: return False
    if k == "U4.5" and odd_val > 1.48: return False
    if k == "U3.5" and odd_val > 2.30: return False
    if k == "O0.5" and odd_val > 1.15: return False
    if k.startswith("MG_") and odd_val > 1.60: return False
    if (k.startswith("HC_") or k.startswith("AC_")) and odd_val > 8.0: return False
    return True

def calc_poisson_cdf(lam, k):
    if lam <= 0 or k < 0: return 0.0
    return sum((math.exp(-lam) * (lam ** i)) / math.factorial(i) for i in range(int(k) + 1))

def extract_all_lines(m_data, market_keywords, forbidden_kws, is_under=True):
    avail = {}
    rynki = m_data.get("rynki", {})
    kw_target = ["mniej", "ponizej", "-"] if is_under else ["wiecej", "powyzej", "+"]
    
    for r_name, r_opts in rynki.items():
        r_clean = r_name.lower().replace("\n", " ")
        r_norm = clean_txt(r_clean)
        
        if not any(kw in r_norm for kw in market_keywords): continue
        if any(fb in r_clean or fb in r_norm for fb in forbidden_kws): continue
        
        for opt_k, opt_v in r_opts.items():
            opt_norm = clean_txt(opt_k)
            nums = re.findall(r"\d+\.\d+", str(r_name) + " " + str(opt_k))
            if nums:
                try:
                    line_val = float(nums[0])
                    val = float(str(opt_v).replace(',', '.'))
                    has_dir = any(kw in opt_norm for kw in kw_target) or (opt_k.strip().startswith("-") if is_under else opt_k.strip().startswith("+"))
                    if has_dir and val > 1.005:
                        avail[line_val] = val
                except Exception:
                    pass
    return avail

def parsuj_pojedynczy_kurs(match_data, typ_kod, home, away):
    if not match_data or not isinstance(match_data, dict): return None, ""
    typ_k = str(typ_kod).strip()
    rynki = match_data.get("rynki", {})
    kursy = match_data.get("kursy", {})
    info = match_data.get("info", {})

    h_buk = info.get("gospodarz_fortuna", info.get("gospodarz_sb", home))
    a_buk = info.get("gosc_fortuna", info.get("gosc_sb", away))
    home_tokens = list(set(get_tokens(home) + get_tokens(h_buk)))
    away_tokens = list(set(get_tokens(away) + get_tokens(a_buk)))

    is_team_goal = typ_k.startswith(("HU", "AU", "HO", "AO"))
    is_half_goal = typ_k.startswith(("HT_U", "HT_O", "2H_U", "2H_O"))
    is_shots = typ_k.startswith(("S_", "ST_", "H_S_", "A_S_", "H_ST_", "A_ST_"))
    is_corners = typ_k.startswith(("C_", "HC_", "AC_"))
    is_handicap = "_AH+" in typ_k or typ_k.startswith(("H_AH", "A_AH"))
    is_multigol = typ_k.startswith("MG_") or typ_k in ["1-5", "1-6", "1-4", "2-4", "2-5"]
    is_under_over = (typ_k.startswith("U") or typ_k.startswith("O")) and not any([is_shots, is_corners, is_handicap, is_multigol, is_team_goal, is_half_goal])

    # 1. HANDICAPY AZJATYCKIE I EUROPEJSKIE (FT, HT, 2H)
    if is_handicap:
        is_ht = "HT_" in typ_k
        is_2h = "2H_" in typ_k
        is_ft = not is_ht and not is_2h
        is_home_h = "_H_AH+" in typ_k or typ_k.startswith("H_AH+")
        line = typ_k.split("+")[1].strip()
        target_tokens = home_tokens if is_home_h else away_tokens

        for r_name, r_opts in rynki.items():
            r_norm = clean_txt(r_name)
            if "handicap" not in r_norm or any(fb in r_norm for fb in ["rozn", "kartk"]):
                continue

            has_1h = any(p in r_norm for p in ["1polowa", "1pol"])
            has_2h = any(p in r_norm for p in ["2polowa", "2pol"])
            if is_ht and not has_1h: continue
            if is_2h and not has_2h: continue
            if is_ft and (has_1h or has_2h): continue

            for opt_k, opt_v in r_opts.items():
                opt_norm = clean_txt(opt_k)
                combo_norm = f"{r_norm} {opt_norm}"
                
                # Wykluczenie handicapów ujemnych dla danej linii
                if f"-{line}" in combo_norm and f"+{line}" not in combo_norm:
                    continue

                has_team = any(tok in opt_norm for tok in target_tokens) or (opt_k.strip().startswith("1") if is_home_h else opt_k.strip().startswith("2"))
                if has_team:
                    if (f"+{line}" in combo_norm) or (f"({line})" in combo_norm) or (f" {line}" in opt_k and "-" not in opt_k) or (f"({line})" in str(opt_k)):
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): 
                                return val, f"[{r_name.replace(chr(10), ' ')}] -> {opt_k}"
                        except Exception: pass
                    
                    target_lead = int(float(line) + 0.5)
                    if not is_home_h:
                        if f"0{target_lead}" in r_norm and opt_k.strip().startswith("2"):
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(typ_k, val): return val, f"[{r_name.replace(chr(10), ' ')}] -> {opt_k}"
                            except Exception: pass
                    if is_home_h:
                        if f"{target_lead}0" in r_norm and opt_k.strip().startswith("1"):
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(typ_k, val): return val, f"[{r_name.replace(chr(10), ' ')}] -> {opt_k}"
                            except Exception: pass

    # 2. RZUTY ROŻNE
    if is_corners:
        is_home_c = typ_k.startswith("HC_")
        is_away_c = typ_k.startswith("AC_")
        is_match_c = typ_k.startswith("C_")
        parts = typ_k.split("_")
        line_part = parts[1]
        is_u = line_part.startswith("U")
        line = line_part[1:]
        kw_target = ["mniej", "ponizej", "-"] if is_u else ["wiecej", "powyzej", "+"]
        forbidden_corners = ["wygra", "obie", "btts", "gol", "bramk", "kartk", "spalon", "faul", "kombin", "combo", "podwojna", "dwojtyp"]

        for r_name, r_opts in rynki.items():
            r_clean = r_name.lower().replace("\n", " ")
            r_norm = clean_txt(r_clean)
            if "rozn" not in r_norm or any(fb in r_clean or fb in r_norm for fb in forbidden_corners): continue
            has_h = any(tok in r_norm for tok in home_tokens)
            has_a = any(tok in r_norm for tok in away_tokens)
            if is_match_c and (has_h or has_a): continue
            if is_home_c and not has_h: continue
            if is_away_c and not has_a: continue

            for opt_k, opt_v in r_opts.items():
                opt_norm = clean_txt(opt_k)
                tokens = re.findall(r"\d+(?:\.\d+)?", str(r_name) + " " + str(opt_k))
                if line in tokens:
                    has_dir = any(kw in opt_norm for kw in kw_target) or (opt_k.strip().startswith("-") if is_u else opt_k.strip().startswith("+"))
                    if has_dir:
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val, f"[{r_name.replace(chr(10), ' ')}] -> {opt_k}"
                        except Exception: pass

        if is_match_c and typ_k in kursy:
            try:
                val = float(str(kursy[typ_k]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val, f"[Sekcja 'kursy'] -> {typ_k}"
            except Exception: pass

    # 3. GOLE DRUŻYNOWE
    if is_team_goal:
        is_home = typ_k.startswith("H")
        is_under = "U" in typ_k[:2]
        tokens_num = re.findall(r"\d*\.?\d+", typ_k)
        line = tokens_num[0] if tokens_num else ""
        if line.startswith("."): line = "0" + line

        target_tokens = home_tokens if is_home else away_tokens
        opp_tokens = away_tokens if is_home else home_tokens
        kw_target = ["mniej", "ponizej", "-"] if is_under else ["wiecej", "powyzej", "+"]
        forbidden = ["polow", "kartk", "rozn", "strzal", "faul", "spalon", "&", "lub", "wygra", "dokladny", "/", "remis"]

        for r_name, r_opts in rynki.items():
            r_clean = r_name.lower().replace("\n", " ")
            r_norm = clean_txt(r_clean)
            if any(fb in r_clean for fb in forbidden): continue
            if "liczbagoli" not in r_norm and "liczbabramek" not in r_norm and "gole" not in r_norm: continue
            has_target = any(tok in r_norm for tok in target_tokens) or ("gospodarz" in r_norm if is_home else ("gosc" in r_norm or "gość" in r_clean))
            has_opp = any(tok in r_norm for tok in opp_tokens if tok not in target_tokens)
            if not has_target or has_opp: continue

            for opt_k, opt_v in r_opts.items():
                opt_clean = opt_k.lower().replace("\n", " ")
                opt_norm = clean_txt(opt_clean)
                tokens = re.findall(r"\d+(?:\.\d+)?", r_clean + " " + opt_clean)
                if line in tokens:
                    has_dir = any(kw in opt_norm for kw in kw_target) or (opt_k.strip().startswith("-") if is_under else opt_k.strip().startswith("+"))
                    if has_dir:
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val, f"[{r_name.replace(chr(10), ' ')}] -> {opt_k}"
                        except Exception: pass

        if typ_k in kursy:
            try:
                val = float(str(kursy[typ_k]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val, f"[Sekcja 'kursy'] -> {typ_k}"
            except Exception: pass

    # 4. GOLE POŁÓWKOWE
    if is_half_goal:
        is_ht = "HT_" in typ_k
        is_u = "_U" in typ_k
        line = typ_k[4:].strip()
        kw_list = ["ponizej", "mniej", "-"] if is_u else ["powyzej", "wiecej", "+"]
        time_kw = ["1. połowa", "1.połowa", "1 połowa"] if is_ht else ["2. połowa", "2.połowa", "2 połowa"]
        forbidden_half = ["&", "lub", "oraz", "/", "wygra", "btts", "obie", "kartk", "rozn", "dokladny", "handicap", "mecz"]

        for r_name, r_opts in rynki.items():
            r_clean = r_name.lower().replace("\n", " ")
            r_norm = clean_txt(r_clean)
            if any(fb in r_clean for fb in forbidden_half): continue
            if not any(p in r_clean for p in time_kw): continue
            if is_ht and any(p in r_clean for p in ["2. połowa", "2.połowa", "2 połowa"]): continue
            if not is_ht and any(p in r_clean for p in ["1. połowa", "1.połowa", "1 połowa"]): continue

            if "liczbagoli" in r_norm or "liczbabramek" in r_norm or "sumagoli" in r_norm:
                for opt_k, opt_v in r_opts.items():
                    opt_norm = clean_txt(opt_k)
                    tokens = re.findall(r"\d+(?:\.\d+)?", str(r_name) + " " + str(opt_k))
                    if line in tokens and any(kw in opt_norm for kw in kw_list):
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val, f"[{r_name.replace(chr(10), ' ')}] -> {opt_k}"
                        except Exception: pass

        if typ_k in kursy:
            try:
                val = float(str(kursy[typ_k]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val, f"[Sekcja 'kursy'] -> {typ_k}"
            except Exception: pass

    # 5. GOLE MECZOWE UNDER / OVER
    if is_under_over:
        lv = typ_k[1:].strip()
        is_u = typ_k.startswith("U")
        kw_list = ["ponizej", "mniej", "-"] if is_u else ["powyzej", "wiecej", "+"]
        forbidden = ["rozn", "polow", "kartk", "faul", "strzal", "spalon", "dwojtyp", "zolt", "&", "wygra"]

        for r_name, r_opts in rynki.items():
            r_norm = clean_txt(r_name)
            if any(fb in r_norm for fb in forbidden): continue
            if any(tok in r_norm for tok in (home_tokens + away_tokens)) or "gospodarz" in r_norm or "gosc" in r_norm:
                continue

            if any(kw in r_norm for kw in ["liczbagoli", "sumagoli", "liczbabramek", "meczliczbagoli"]):
                for opt_k, opt_v in r_opts.items():
                    opt_norm = clean_txt(opt_k)
                    tokens = re.findall(r"\d+(?:\.\d+)?", str(r_name) + " " + str(opt_k))
                    if lv in tokens and any(kw in opt_norm for kw in kw_list):
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val, f"[{r_name.replace(chr(10), ' ')}] -> {opt_k}"
                        except Exception: pass

        if typ_k in kursy:
            try:
                val = float(str(kursy[typ_k]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val, f"[Sekcja 'kursy'] -> {typ_k}"
            except Exception: pass

    # 6. STRZAŁY
    if is_shots:
        is_sot = ("ST" in typ_k)
        is_home_t = typ_k.startswith("H_")
        is_away_t = typ_k.startswith("A_")
        is_match_t = not is_home_t and not is_away_t

        if typ_k in ["S_1", "S_2", "ST_1", "ST_2"]:
            is_target_home = typ_k.endswith("_1")
            target_tokens = home_tokens if is_target_home else away_tokens
            opp_tokens = away_tokens if is_target_home else home_tokens

            for r_name, r_opts in rynki.items():
                r_norm = clean_txt(r_name)
                if "strzal" not in r_norm: continue
                has_sot_kw = any(w in r_norm for w in ["celn", "swiatlo"])
                if is_sot and not has_sot_kw: continue
                if not is_sot and has_sot_kw: continue
                
                if any(w in r_norm for w in ["wiecej", "najwiecej", "h2h"]):
                    for opt_k, opt_v in r_opts.items():
                        opt_norm = clean_txt(opt_k)
                        if "remis" in opt_norm or "rowno" in opt_norm: continue
                        matched = False
                        if is_target_home and (opt_k.strip().startswith("1") or any(tok in opt_norm for tok in target_tokens)):
                            matched = True
                        elif not is_target_home and (opt_k.strip().startswith("2") or any(tok in opt_norm for tok in target_tokens)):
                            matched = True

                        if matched and not any(op in opt_norm for op in opp_tokens if op not in target_tokens):
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(typ_k, val): return val, f"[{r_name.replace(chr(10), ' ')}] -> {opt_k}"
                            except Exception: pass

        tokens_line = re.findall(r"\d*\.?\d+", typ_k)
        tokens_line = [t for t in tokens_line if t and t != '.']
        if tokens_line:
            line = tokens_line[0]
            if line.startswith("."): line = "0" + line
            is_u = "_U" in typ_k
            kw_target = ["mniej", "ponizej", "-"] if is_u else ["wiecej", "powyzej", "+"]

            for r_name, r_opts in rynki.items():
                r_norm = clean_txt(r_name)
                if "strzal" not in r_norm: continue
                has_sot_kw = any(w in r_norm for w in ["celn", "swiatlo"])
                if is_sot and not has_sot_kw: continue
                if not is_sot and has_sot_kw: continue

                has_h = any(tok in r_norm for tok in home_tokens)
                has_a = any(tok in r_norm for tok in away_tokens)

                if is_match_t and (has_h or has_a): continue
                if is_home_t and not has_h: continue
                if is_away_t and not has_a: continue

                for opt_k, opt_v in r_opts.items():
                    opt_norm = clean_txt(opt_k)
                    tokens = re.findall(r"\d+(?:\.\d+)?", str(r_name) + " " + str(opt_k))
                    if line in tokens:
                        has_dir = any(kw in opt_norm for kw in kw_target) or (opt_k.strip().startswith("-") if is_u else opt_k.strip().startswith("+"))
                        if has_dir:
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(typ_k, val): return val, f"[{r_name.replace(chr(10), ' ')}] -> {opt_k}"
                            except Exception: pass

    # 7. MULTIGOLE
    if is_multigol:
        range_target = typ_k.replace("MG_", "").strip()
        if typ_k in kursy:
            try:
                val = float(str(kursy[typ_k]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val, f"[Sekcja 'kursy'] -> {typ_k}"
            except Exception: pass
        if range_target in kursy:
            try:
                val = float(str(kursy[range_target]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val, f"[Sekcja 'kursy'] -> {range_target}"
            except Exception: pass

        for r_name, r_opts in rynki.items():
            r_low = r_name.lower().replace("\n", " ")
            if any(kw in r_low for kw in ["multigol", "przedział", "przedzial", "zakres", "liczba goli"]):
                for opt_k, opt_v in r_opts.items():
                    opt_str = str(opt_k).strip().lower()
                    if opt_str.startswith(range_target) or f"{range_target} |" in opt_str or f"{range_target} goli" in opt_str or opt_str == range_target:
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val, f"[{r_name}] -> {opt_k}"
                        except Exception: pass

    # 8. 1X2 i Podwójna Szansa
    if typ_k in ["1", "X", "2", "1X", "X2"]:
        for r_name, r_opts in rynki.items():
            r_norm = clean_txt(r_name)
            if any(kw in r_norm for kw in ["mecz", "1x2", "wynik", "podwojnaszansa", "szansa"]):
                for opt_k, opt_v in r_opts.items():
                    opt_low = str(opt_k).lower().strip()
                    matched = False
                    if typ_k == "1X" and opt_low in ["1x", "1-x", "10", "1/x"]: matched = True
                    elif typ_k == "X2" and opt_low in ["x2", "x-2", "02", "x/2"]: matched = True
                    elif typ_k == "1" and opt_low in ["1", "gospodarz", "home"]: matched = True
                    elif typ_k == "2" and opt_low in ["2", "gość", "gosc", "away"]: matched = True
                    elif typ_k == "X" and opt_low in ["x", "0", "remis", "draw"]: matched = True
                    if matched:
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val, f"[{r_name.replace(chr(10), ' ')}] -> {opt_k}"
                        except Exception: pass

    return None, ""

def inspect_match(home, away, target_bet, est_odd=None):
    print("=" * 85)
    print(f"🔍 STATLAB AUDITOR PRO | SPOTKANIE: {home} vs {away} | BADANY TYP: {target_bet}")
    print(f"   KURS OSZACOWANY (SZACUNKOWY): {est_odd if est_odd else 'Nie podano'}")
    print("=" * 85)

    h_tokens = get_tokens(home)
    a_tokens = get_tokens(away)
    detected_odds = {}

    for bookie, path_patterns in [("SUPERBET", ["superbet_baza*.json"]), ("FORTUNA", ["fortuna_baza*.json"])]:
        print(f"\n[{bookie}] Rozpoczynam audyt...")
        files = []
        for pat in path_patterns:
            files.extend(glob.glob(pat))
        
        if not files:
            print(f"  ❌ Brak plików bazy danych dla bukmachera {bookie}.")
            continue

        baza = {}
        for f_path in files:
            try:
                with open(f_path, "r", encoding="utf-8") as f:
                    baza.update(json.load(f))
            except Exception: pass

        matched_data = None
        match_key = None
        partial_h, partial_a = [], []

        for k, v in baza.items():
            if "___" in k:
                b_h, b_a = k.split("___")
                cb_h, cb_a = clean_txt(b_h), clean_txt(b_a)
                h_ok = any(t in cb_h for t in h_tokens)
                a_ok = any(t in cb_a for t in a_tokens)
                if h_ok and a_ok:
                    matched_data = v
                    match_key = k
                    break
                elif h_ok and not a_ok:
                    partial_h.append(b_a)
                elif not h_ok and a_ok:
                    partial_a.append(b_h)

        if not matched_data:
            if partial_h:
                print(f"  ⚠️ BŁĄD MAPOWANIA (Pkt 4): Rozpoznano gospodarza, ale gość u buka ma nazwę: '{partial_h[0]}'.")
                print(f"     👉 Dodaj do slownik_druzyn.json: \"{away}\": \"{partial_h[0]}\"")
            elif partial_a:
                print(f"  ⚠️ BŁĄD MAPOWANIA (Pkt 4): Rozpoznano gościa, ale gospodarz u buka ma nazwę: '{partial_a[0]}'.")
                print(f"     👉 Dodaj do slownik_druzyn.json: \"{home}\": \"{partial_a[0]}\"")
            else:
                print(f"  ❌ BRAK MECZU W OFERCIE (Pkt 2): Mecz w ogóle nie został wystawiony przez {bookie}.")
            continue

        print(f"  ✅ Mecz zidentyfikowany poprawnie: [{match_key}]")
        val, source_desc = parsuj_pojedynczy_kurs(matched_data, target_bet, home, away)

        if val is not None:
            detected_odds[bookie] = val
            print(f"  🎯 ZNALEZIONO KURS 1:1: {val:.2f} | Ścieżka: {source_desc}")
            
            if est_odd:
                try:
                    eo = float(est_odd)
                    if val >= eo * 2.0 and val >= 2.20:
                        print(f"  🚨 ANOMALIA KURSOWA (Pkt 6): Kurs ({val:.2f}) jest ponad 2x wyższy niż szacunek ({eo:.2f})!")
                        print("     Możliwa zamiana stron faworyta lub błąd w źródle.")
                    elif val <= eo * 0.55 and eo >= 1.40:
                        print(f"  🚨 ANOMALIA KURSOWA (Pkt 6): Kurs ({val:.2f}) drastycznie zaniżony względem modelu ({eo:.2f})!")
                except Exception: pass
        else:
            print(f"  ❌ BRAK DOKŁADNEJ LINII/TYPU: '{target_bet}' (Pkt 1 / Pkt 5).")
            
            if (target_bet.startswith("U") or target_bet.startswith("O")) and "_" not in target_bet:
                line_target = float(target_bet[1:])
                is_u = target_bet.startswith("U")
                avail_lines = extract_all_lines(matched_data, ["liczbagoli", "sumagoli", "liczbabramek"], ["rozn", "kartk", "polow", "strzal"], is_under=is_u)
                
                if avail_lines:
                    print(f"     Dostępne linie w ofercie: {sorted(avail_lines.keys())}")
                    closest_l = min(avail_lines.keys(), key=lambda x: abs(x - line_target))
                    closest_odd = avail_lines[closest_l]
                    
                    lam_default = 2.6741
                    p_orig = calc_poisson_cdf(lam_default, int(math.floor(line_target))) if is_u else (1.0 - calc_poisson_cdf(lam_default, int(math.floor(line_target))))
                    p_alt = calc_poisson_cdf(lam_default, int(math.floor(closest_l))) if is_u else (1.0 - calc_poisson_cdf(lam_default, int(math.floor(closest_l))))
                    delta = round((p_alt - p_orig) * 100, 1)
                    sign = "+" if delta >= 0 else ""

                    print(f"  💡 SMART FALLBACK (Pkt 3):")
                    print(f"     Najbliższa alternatywa: {('U' if is_u else 'O')}{closest_l} @ {closest_odd:.2f}")
                    print(f"     Oszacowana zmiana szans: {sign}{delta}% względem pierwotnego założenia.")
                else:
                    print("     Bukmacher w ogóle nie wystawił rynku liczby goli na to spotkanie.")

            if any(target_bet.startswith(pfx) for pfx in ["S_", "ST_"]):
                has_s = any("strzal" in clean_txt(r) for r in matched_data.get("rynki", {}).keys())
                if not has_s:
                    print("     ⚠️ Informacja: Bukmacher nie oferuje zakładów na strzały dla tego spotkania.")
            if any(target_bet.startswith(pfx) for pfx in ["C_", "HC_", "AC_"]):
                has_c = any("rozn" in clean_txt(r) for r in matched_data.get("rynki", {}).keys())
                if not has_c:
                    print("     ⚠️ Informacja: Bukmacher nie oferuje zakładów na rzuty rożne dla tego spotkania.")

    if "SUPERBET" in detected_odds and "FORTUNA" in detected_odds:
        k_sb, k_ft = detected_odds["SUPERBET"], detected_odds["FORTUNA"]
        diff = abs(k_sb - k_ft) / min(k_sb, k_ft)
        if diff >= 0.35:
            print(f"\n🚨 ALERT ROZBIEŻNOŚCI MIĘDZY BUKMACHERAMI: Superbet ({k_sb:.2f}) vs Fortuna ({k_ft:.2f}) różnią się o {diff*100:.1f}%!")

if __name__ == "__main__":
    h = sys.argv[1] if len(sys.argv) > 1 else "Brentford"
    a = sys.argv[2] if len(sys.argv) > 2 else "Chelsea"
    t = sys.argv[3] if len(sys.argv) > 3 else "H_AH+2.5"
    e = float(sys.argv[4]) if len(sys.argv) > 4 else 1.12
    inspect_match(h, a, t, e)
