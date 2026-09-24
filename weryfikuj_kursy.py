"""
MODUŁ: weryfikuj_kursy.py
OPIS: W pełni uniwersalny, kompletny audytor kursów Superbet i Fortuna.
      Obsługuje 100% rynków systemu StatLab:
      1. Rzuty rożne pojedyncze i mieszane (C_U, C_O, HC_U, HC_O, AC_U, AC_O)
      2. BetBuilder bramkowy i rożnych (kopuła korelacyjna rho=0.55 / model brzegowy)
      3. Gole drużynowe (HU, AU, HO, AO)
      4. Gole meczowe Under/Over (U, O)
      5. Handicapy FT, HT, 2H (azjatyckie i europejskie)
      6. Multigole (MG_)
      7. Strzały ogółem i celne (S_1, S_2, ST_1, ST_2, linie drużynowe i meczowe)
      8. 1X2 i Podwójna Szansa
"""

import sys
import json
import os
import re

def clean_txt(s):
    t = str(s).strip().lower()
    for pl, en in [('ą','a'),('ć','c'),('ę','e'),('ł','l'),('ń','n'),('ó','o'),('ś','s'),('ź','z'),('ż','z')]:
        t = t.replace(pl, en)
    t = re.sub(r'\b(fc|ks|gks|mks|ac|as|cf|ss|sc|sa|sp|vfb|tsv|sv|fk|sk|stade|de|hsc)\b', '', t)
    return re.sub(r'[^a-z0-9]', '', t)

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

def get_team_tokens(team_name):
    tokens = set()
    cleaned = clean_txt(team_name)
    if len(cleaned) >= 3:
        tokens.add(cleaned)
        tokens.add(cleaned[:4])
    
    raw_words = re.findall(r'[a-zA-Z0-9]+', str(team_name).lower())
    for w in raw_words:
        cw = clean_txt(w)
        if len(cw) >= 3 and cw not in ["team", "club", "city", "town", "utd", "stade"]:
            tokens.add(cw)
            tokens.add(cw[:4])
    return list(tokens)

# =========================================================================
# KALKULACJA BETBUILDER ZGODNA Z DOKUMENTEM KALIBRACYJNYM
# =========================================================================
def calc_betbuilder_copula(odds_list, rho=0.55):
    valid_odds = [float(o) for o in odds_list if float(o) > 1.005]
    if not valid_odds: return 1.00
    if len(valid_odds) == 1: return valid_odds[0]

    q_list = [1.0 / o for o in valid_odds]
    q_list.sort(reverse=True)
    
    q_joint = q_list[0]
    for q_next in q_list[1:]:
        gamma = 1.0 - rho * (1.0 - min(q_joint, q_next))
        q_joint = q_joint * (q_next ** gamma)
        
    calc_odd = max(1.02, round(1.0 / q_joint, 2))
    return max(calc_odd, max(valid_odds))

def calc_betbuilder_nested(sub_odds_dict, tpl):
    tokens = [t.strip() for t in tpl.split("+")]
    real_sub_odds = [float(sub_odds_dict.get(t, 1.0)) for t in tokens if float(sub_odds_dict.get(t, 1.0)) > 1.005]
    max_single_odd = max(real_sub_odds) if real_sub_odds else 1.00

    # 1. BetBuilder rożnych lub rynków mieszanych
    if any(t.startswith(("C_", "HC_", "AC_")) for t in tokens) or any(t.startswith(("S_", "ST_")) for t in tokens):
        valid = [float(sub_odds_dict.get(t, 1.0)) for t in tokens if float(sub_odds_dict.get(t, 1.0)) > 1.005]
        calc = calc_betbuilder_copula(valid, rho=0.55) if valid else 1.08
        return max(calc, max_single_odd)

    # 2. BetBuilder bramkowy (szablony zagnieżdżone)
    main_under = next((t for t in tokens if t in ['U6.5', 'U5.5', 'U4.5', 'U3.5']), None)
    base_odd = 1.04
    if main_under:
        val_u = float(sub_odds_dict.get(main_under, 1.0))
        if val_u > 1.005:
            base_odd = val_u
        else:
            defaults = {'U6.5': 1.015, 'U5.5': 1.03, 'U4.5': 1.09, 'U3.5': 1.25}
            base_odd = defaults.get(main_under, 1.04)

    if 'O0.5' in tokens:
        odd_o05 = float(sub_odds_dict.get('O0.5', 1.04))
        if odd_o05 <= 1.005: odd_o05 = 1.04
        q_u = 1.0 / base_odd
        q_o = 1.0 / odd_o05
        q_joint = max(0.60, q_u + q_o - 1.0)
        base_odd = max(1.04, round(1.0 / q_joint, 2))

    extra_boost = 0.0
    for st in [t for t in tokens if t not in ['O0.5', main_under]]:
        k_val = float(sub_odds_dict.get(st, 1.0))
        if k_val > 1.005:
            extra_boost += (k_val - 1.0) * 0.20

    final_odd = base_odd + extra_boost
    final_odd = max(final_odd, max_single_odd)

    if "U6.5" in tpl: final_odd = min(final_odd, 1.15)
    elif "U5.5" in tpl and "O0.5" in tpl: final_odd = min(final_odd, 1.25)
    elif "U4.5" in tpl: final_odd = min(final_odd, 1.35)

    return max(1.02, round(final_odd, 2))

# =========================================================================
# GŁÓWNY PARSER POJEDYNCZYCH KURSÓW
# =========================================================================
def parsuj_pojedyncze_zdarzenie(match_data, typ_k, home, away):
    if not match_data: return None, ""
    
    rynki = match_data.get("rynki", {})
    kursy = match_data.get("kursy", {})
    info = match_data.get("info", {})

    h_buk = info.get("gospodarz_fortuna", info.get("gospodarz_sb", home))
    a_buk = info.get("gosc_fortuna", info.get("gosc_sb", away))

    home_tokens = list(set(get_team_tokens(home) + get_team_tokens(h_buk)))
    away_tokens = list(set(get_team_tokens(away) + get_team_tokens(a_buk)))

    is_team_goal = typ_k.startswith(("HU", "AU", "HO", "AO"))
    is_half_goal = typ_k.startswith(("HT_U", "HT_O", "2H_U", "2H_O"))
    is_shots = typ_k.startswith(("S_", "ST_", "H_S_", "A_S_", "H_ST_", "A_ST_"))
    is_corners = typ_k.startswith(("C_", "HC_", "AC_"))
    is_handicap = "_AH+" in typ_k or typ_k.startswith(("H_AH", "A_AH"))
    is_multigol = typ_k.startswith("MG_") or typ_k in ["1-5", "1-6", "1-4", "2-4", "2-5"]
    is_under_over = (typ_k.startswith("U") or typ_k.startswith("O")) and not any([is_shots, is_corners, is_handicap, is_multigol, is_team_goal, is_half_goal])

    # 1. RZUTY ROŻNE (C_U/O, HC_U/O, AC_U/O)
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

            if "rozn" not in r_norm: continue
            if any(fb in r_clean or fb in r_norm for fb in forbidden_corners): continue

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
                            if is_odd_sane(typ_k, val):
                                return val, f"[{r_name.replace(chr(10), ' ')}] -> {opt_k}"
                        except Exception: pass

        if is_match_c and typ_k in kursy:
            try:
                val = float(str(kursy[typ_k]).replace(',', '.'))
                if is_odd_sane(typ_k, val): return val, f"[Sekcja 'kursy'] -> {typ_k}"
            except Exception: pass

    # 2. GOLE DRUŻYNOWE (HU, AU, HO, AO)
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

    # 3. GOLE POŁÓWKOWE (HT_U, 2H_U)
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
                    tokens = re.findall(r"\d+(?:\.\d+)?", r_clean + " " + str(opt_k))
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

    # 4. GOLE MECZOWE UNDER / OVER
    if is_under_over:
        lv = typ_k[1:].strip()
        is_u = typ_k.startswith("U")
        kw_list = ["ponizej", "mniej", "-"] if is_u else ["powyzej", "wiecej", "+"]
        forbidden = ["rozn", "polow", "kartk", "faul", "strzal", "spalon", "dwojtyp", "zolt", "&", "wygra"]

        for r_name, r_opts in rynki.items():
            r_norm = clean_txt(r_name)
            if any(fb in r_norm for fb in forbidden): continue
            if any(tok in r_norm for tok in (home_tokens + away_tokens)) or "gospodarz" in r_norm or "gość" in r_norm or "gosc" in r_norm:
                continue

            if "liczbagoli" in r_norm or "sumagoli" in r_norm or "liczbabramek" in r_norm or "meczliczbagoli" in r_norm:
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

    # 5. STRZAŁY
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

    # 6. MULTIGOLE
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

    # 7. HANDICAPY AZJATYCKIE I EUROPEJSKIE
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
                if f"-{line}" in combo_norm and f"+{line}" not in combo_norm:
                    continue

                has_team = any(tok in opt_norm for tok in target_tokens) or (opt_k.strip().startswith("1") if is_home_h else opt_k.strip().startswith("2"))
                if has_team:
                    if (f"+{line}" in combo_norm) or (f"({line})" in combo_norm) or (f"{line}" in opt_k and "-" not in opt_k):
                        try:
                            val = float(str(opt_v).replace(',', '.'))
                            if is_odd_sane(typ_k, val): return val, f"[{r_name}] -> {opt_k}"
                        except Exception: pass
                    if not is_home_h:
                        target_lead = int(float(line) + 0.5)
                        if f"0{target_lead}" in r_norm and opt_k.strip().startswith("2"):
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(typ_k, val): return val, f"[{r_name}] -> {opt_k}"
                            except Exception: pass
                    if is_home_h:
                        target_lead = int(float(line) + 0.5)
                        if f"{target_lead}0" in r_norm and opt_k.strip().startswith("1"):
                            try:
                                val = float(str(opt_v).replace(',', '.'))
                                if is_odd_sane(typ_k, val): return val, f"[{r_name}] -> {opt_k}"
                            except Exception: pass

    # 8. ZAPASOWO ZE SŁOWNIKA 'KURSY'
    if typ_k in kursy and not any([is_under_over, is_handicap, is_multigol, is_corners, is_shots, is_team_goal, is_half_goal]):
        try:
            val = float(str(kursy[typ_k]).replace(',', '.'))
            if is_odd_sane(typ_k, val): return val, f"[Sekcja 'kursy'] -> {typ_k}"
        except Exception: pass

    return None, ""

# =========================================================================
# CENTRALNY AUDYTOR KURSU (POJEDYNCZEGO LUB BETBUILDER)
# =========================================================================
def inspect_match(home, away, target_bet):
    print(f"\n🔍 AUDYT KURSU DLA: {home} vs {away} | TYP: {target_bet}")
    print("=" * 75)

    is_betbuilder = "+" in target_bet and not any(tag in target_bet for tag in ["_AH+", "H_AH", "A_AH"])

    for bookie, path in [("SUPERBET", "superbet_baza_5dni.json"), ("FORTUNA", "fortuna_baza_5dni.json")]:
        if not os.path.exists(path):
            print(f"[{bookie}] ❌ Brak pliku bazy: {path}")
            continue

        with open(path, "r", encoding="utf-8") as f:
            db = json.load(f)

        matched_data = None
        h_tokens = get_team_tokens(home)
        a_tokens = get_team_tokens(away)

        for k, v in db.items():
            if "___" in k:
                b_h, b_a = k.split("___")
                cb_h, cb_a = clean_txt(b_h), clean_txt(b_a)
                if any(tok in cb_h for tok in h_tokens) and any(tok in cb_a for tok in a_tokens):
                    matched_data = v
                    break

        if not matched_data:
            print(f"[{bookie}] ❌ Mecz nie został znaleziony w ofercie.")
            continue

        if is_betbuilder:
            skladniki = [s.strip() for s in target_bet.split("+")]
            sub_odds = {}
            print(f"\n--- 🏬 [{bookie}] ROZBICIE KLOCKÓW BETBUILDER ---")
            
            for sk in skladniki:
                val, desc = parsuj_pojedyncze_zdarzenie(matched_data, sk, home, away)
                if val is not None:
                    sub_odds[sk] = val
                    print(f"   • {sk:10} -> Kurs: {val:.2f}  ({desc})")
                else:
                    sub_odds[sk] = 1.00
                    print(f"   • {sk:10} -> Kurs: 1.00  (pominięty - brak bezpośredniego rynku w ofercie)")

            final_bb_odd = calc_betbuilder_nested(sub_odds, target_bet)
            print(f"⚡ [{bookie}] ŁĄCZNY KURS BETBUILDER (z korelacją): {final_bb_odd:.2f}")

        else:
            val, opis = parsuj_pojedyncze_zdarzenie(matched_data, target_bet, home, away)
            if val is not None:
                print(f"[{bookie}] ✅ Znaleziony kurs oficjalny: {val:.2f}  |  {opis}")
            else:
                print(f"[{bookie}] ❌ Brak w ofercie (kurs na ten rynek nie istnieje)")

if __name__ == "__main__":
    h = sys.argv[1] if len(sys.argv) > 1 else "Wisla"
    a = sys.argv[2] if len(sys.argv) > 2 else "Slask Wroclaw"
    t = sys.argv[3] if len(sys.argv) > 3 else "C_U12.5+HC_U9.5+AC_U8.5"
    inspect_match(h, a, t)