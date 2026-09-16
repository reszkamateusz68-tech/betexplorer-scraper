import json
import glob
import re
import math

def norm_team(s):
    if not s: return ""
    t = str(s).lower().strip()
    t = t.replace('madrid', 'madryt').replace('atletico', 'atl').replace('athletic', 'ath')
    diacritics = {
        'ä': 'a', 'ö': 'o', 'ü': 'u', 'é': 'e', 'è': 'e', 'á': 'a', 'í': 'i', 'ó': 'o', 'ú': 'u',
        'ñ': 'n', 'ã': 'a', 'õ': 'o', 'â': 'a', 'ê': 'e', 'î': 'i', 'ô': 'o', 'û': 'u',
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ś': 's', 'ź': 'z', 'ż': 'z'
    }
    for k, v in diacritics.items(): t = t.replace(k, v)
    t = re.sub(r'[\.\-\_\/\(\)]', ' ', t)
    t = re.sub(r'\b(fc|cf|fk|sk|ac|cd|sc|sp|sv|as|de|da|do|rs|u20|u19|aif|ifk|ik)\b', '', t)
    return re.sub(r'[^a-z0-9]', '', t)

def clean_txt(s):
    if not s: return ""
    t = str(s).replace('\xa0', ' ').strip().lower()
    t = t.replace('ł', 'l').replace('ó', 'o').replace('ę', 'e').replace('ą', 'a').replace('ś', 's').replace('ć', 'c').replace('ż', 'z').replace('ź', 'z').replace('ń', 'n')
    return t

sb_baza, ft_baza = {}, {}
for f in sorted(glob.glob("superbet_baza*.json")):
    try: sb_baza.update(json.load(open(f, encoding="utf-8")))
    except: pass
for f in sorted(glob.glob("fortuna_baza*.json")):
    try: ft_baza.update(json.load(open(f, encoding="utf-8")))
    except: pass

map_sb, map_ft = {}, {}
if glob.glob("slownik_druzyn.json"):
    try:
        d = json.load(open("slownik_druzyn.json", encoding="utf-8"))
        map_sb, map_ft = d.get("BetExplorer_To_Superbet", {}), d.get("BetExplorer_To_Fortuna", {})
    except: pass

def iter_all_markets(m_data):
    if not isinstance(m_data, dict): return
    for k, v in m_data.items():
        if k in ['info', 'kursy']: continue
        if isinstance(v, dict):
            if k == 'rynki':
                for sub_k, sub_v in v.items():
                    if isinstance(sub_v, dict): yield sub_k, sub_v
            else:
                yield k, v
                for sub_k, sub_v in v.items():
                    if isinstance(sub_v, dict): yield sub_k, sub_v

def is_odd_sane(typ_kod, odd_val):
    if odd_val is None: return False
    try: odd_val = float(str(odd_val).replace(',', '.'))
    except: return False
    return odd_val > 1.005

def parsuj_kurs(m_data, sub_k, home, away):
    if not m_data: return None
    kursy = m_data.get("kursy", {})
    if sub_k in kursy and is_odd_sane(sub_k, kursy[sub_k]):
        return float(str(kursy[sub_k]).replace(',', '.'))

    alt_quick = {
        "1X": ["1X", "10"], "X2": ["X2", "02"], "1": ["1"], "2": ["2"], "X": ["X", "0"],
        "U0.5": ["U0.5", "U0"], "O0.5": ["O0.5", "O0"], "U1.5": ["U1.5", "U1"], "O1.5": ["O1.5", "O1"],
        "U2.5": ["U2.5", "U2"], "O2.5": ["O2.5", "O2"], "U3.5": ["U3.5", "U3"], "O3.5": ["O3.5", "O3"],
        "U4.5": ["U4.5", "U4"], "O4.5": ["O4.5", "O4"], "U5.5": ["U5.5", "U5"], "O5.5": ["O5.5", "O5"]
    }
    for ak in alt_quick.get(sub_k, []):
        if ak in kursy and is_odd_sane(sub_k, kursy[ak]):
            return float(str(kursy[ak]).replace(',', '.'))

    h_n, a_n = norm_team(home), norm_team(away)
    is_ht = "HT_" in sub_k
    is_2h = "2H_" in sub_k
    is_match = not is_ht and not is_2h

    for m_name, m_dict in iter_all_markets(m_data):
        raw_name = str(m_name).lower()
        clean_name = clean_txt(m_name)

        # BEZWZGLĘDNY FILTR HORYZONTU CZASOWEGO
        has_1h = any(w in clean_name for w in ["1.pol", "1 pol", "1st", "pierwsz"]) or any(w in raw_name for w in ["1.poł", "1 poł"])
        has_2h = any(w in clean_name for w in ["2.pol", "2 pol", "2nd", "drug"]) or any(w in raw_name for w in ["2.poł", "2 poł"])

        if is_match and (has_1h or has_2h): continue
        if is_ht and not has_1h: continue
        if is_2h and not has_2h: continue

        # 1. HANDICAPY (FT, HT, 2H)
        if any(tag in sub_k for tag in ["_AH+", "H_AH+", "A_AH+"]):
            is_h = "_H_AH+" in sub_k
            line = sub_k.split("+")[1].strip()
            side_tag = "1" if is_h else "2"
            target_team = h_n if is_h else a_n
            ceil_val = str(math.ceil(float(line)))
            euro_tag = f"{ceil_val}:0" if is_h else f"0:{ceil_val}"

            if "handicap" in clean_name:
                # Format europejski bramkowy (np. 0:3 lub 0:2)
                if euro_tag in clean_name.replace(" ", ""):
                    for opt_k, opt_v in m_dict.items():
                        opt_c = clean_txt(opt_k)
                        if any(r in opt_c for r in ["remis", "0 |", "x |", "rowno"]): continue
                        if opt_c.startswith(side_tag) or (len(target_team) >= 4 and target_team[:5] in norm_team(opt_c)):
                            if is_odd_sane(sub_k, opt_v): return float(str(opt_v).replace(',', '.'))

                # Format dziesiętny azjatycki (+2.5)
                for opt_k, opt_v in m_dict.items():
                    opt_c = clean_txt(opt_k)
                    # Sprawdzenie selekcji drużyny
                    side_ok = (opt_c.startswith(f"{side_tag} ") or 
                               opt_c.startswith(f"{side_tag}(") or 
                               opt_c.startswith(f"{side_tag}|") or 
                               f"{side_tag} |" in opt_c or 
                               (len(target_team) >= 4 and target_team[:5] in norm_team(opt_c)))
                    if not side_ok: continue

                    # Musi zawierać dokładnie naszą linię (np. 2.5 lub +2.5) i NIE mieć minusa
                    has_line = (f"+{line}" in opt_c or f"+ {line}" in opt_c or f"({line})" in opt_c or f" {line}" in opt_c)
                    has_minus = (f"-{line}" in opt_c or f"- {line}" in opt_c)

                    if has_line and not has_minus:
                        if is_odd_sane(sub_k, opt_v):
                            return float(str(opt_v).replace(',', '.'))

        # 2. STRZAŁY 1X2 I STRZAŁY CELNE (S_1, ST_1)
        if sub_k in ["S_1", "S_2", "S_X", "ST_1", "ST_2", "ST_X"]:
            is_sot = "ST" in sub_k
            target_side = sub_k[-1]
            target_team = h_n if target_side == "1" else a_n

            if not ("strzal" in clean_name): continue
            has_sot = any(w in clean_name for w in ["celn", "swiatlo", "na bramk"])
            if is_sot and not has_sot: continue
            if not is_sot and has_sot: continue
            if any(w in clean_name for w in ["liczba", "suma", "ilosc", "handicap", "zawodnik", "gracz", "spalon", "faul", "kartk"]): continue
            if not any(w in clean_name for w in ["wiecej", "kto", "1x2", "wynik"]): continue

            for opt_k, opt_v in m_dict.items():
                opt_c = clean_txt(opt_k)
                is_draw = any(r in opt_c for r in ["remis", "rowno", "0 |", "x |"])
                if target_side == "X" and is_draw and is_odd_sane(sub_k, opt_v):
                    return float(str(opt_v).replace(',', '.'))
                elif target_side in ["1", "2"] and not is_draw:
                    side_ok = (opt_c.startswith(f"{target_side} ") or 
                               opt_c.startswith(f"{target_side}|") or 
                               f"{target_side} |" in opt_c or 
                               (len(target_team) >= 4 and target_team[:5] in norm_team(opt_c)))
                    if side_ok and is_odd_sane(sub_k, opt_v):
                        return float(str(opt_v).replace(',', '.'))

        # 3. GOLE UNDER / OVER FT (U5.5 itp.)
        if (sub_k.startswith("U") or sub_k.startswith("O")) and "_" not in sub_k and not sub_k.startswith(("HC","AC","HT","2H","HU","AU")):
            nums = re.findall(r"\d+\.\d+", sub_k)
            line = nums[0] if nums else None
            is_u = sub_k.startswith("U")
            kw = "ponizej" if is_u else "powyzej"

            if any(g in clean_name for g in ["liczba goli", "ilosc bramek", "suma goli", "gole"]):
                if any(ex in clean_name for ex in ["druzyn", "kartk", "rozn"]): continue
                for opt_k, opt_v in m_dict.items():
                    opt_c = clean_txt(opt_k)
                    if line and line in opt_c and (kw in opt_c or "mniej" in opt_c or "wiecej" in opt_c or ("under" if is_u else "over") in opt_c):
                        if is_odd_sane(sub_k, opt_v): return float(str(opt_v).replace(',', '.'))

    return None

def test_mecz(home, away, typ_k):
    h_sb, a_sb = map_sb.get(home, home), map_sb.get(away, away)
    h_ft, a_ft = map_ft.get(home, home), map_ft.get(away, away)

    def find_in(baza, h, a):
        hn, an = norm_team(h), norm_team(a)
        k_ex = f"{hn}___{an}"
        for k, v in baza.items():
            if not isinstance(v, dict): continue
            kn = norm_team(k)
            if kn == k_ex: return v
            info = v.get('info', {})
            hb = norm_team(info.get('gospodarz_sb', '') or info.get('gospodarz_fortuna', '') or info.get('gospodarz_be', '') or k.split('___')[0])
            ab = norm_team(info.get('gosc_sb', '') or info.get('gosc_fortuna', '') or info.get('gosc_be', '') or (k.split('___')[1] if '___' in k else ''))
            if (hn in hb or hb in hn or (len(hn)>=4 and hn[:5]==hb[:5])) and (an in ab or ab in an or (len(an)>=4 and an[:5]==ab[:5])):
                return v
        return None

    m_sb = find_in(sb_baza, h_sb, a_sb)
    m_ft = find_in(ft_baza, h_ft, a_ft)

    odd_sb = parsuj_kurs(m_sb, typ_k, home, away)
    odd_ft = parsuj_kurs(m_ft, typ_k, home, away)

    status = "✅ SB + Fortuna 1:1" if (odd_sb and odd_ft) else ("✅ Superbet 1:1" if odd_sb else ("✅ Fortuna 1:1" if odd_ft else "❌ Brak w ofercie"))
    print(f"Mecz: {home:<14} vs {away:<12} | Typ: {typ_k:<13} | SB: {str(odd_sb):<6} | FT: {str(odd_ft):<6} | {status}")

if __name__ == "__main__":
    print("=" * 85)
    print(f"BŁYSKAWICZNY AUDYT KURSÓW | Baza SB: {len(sb_baza)} spotkań | Baza FT: {len(ft_baza)} spotkań")
    print("=" * 85)
    test_mecz("Atl. Madrid", "Osasuna", "A_AH+2.5")
    test_mecz("Atl. Madrid", "Osasuna", "HT_A_AH+1.5")
    test_mecz("Atl. Madrid", "Osasuna", "ST_1")
    test_mecz("Atl. Madrid", "Osasuna", "S_1")
    test_mecz("Atl. Madrid", "Osasuna", "U5.5")
    print("=" * 85)