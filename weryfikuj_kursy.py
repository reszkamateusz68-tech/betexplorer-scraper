import sys
import json
import re

def clean_txt(s):
    s = str(s).strip().lower()
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z','é':'e','á':'a','í':'i','ó':'o','ú':'u'}
    for k, v in repl.items(): s = s.replace(k, v)
    return re.sub(r'[^a-z0-9]', '', s)

def inspect_match(home, away, target_bet):
    print(f"\n🔍 AUDYT KURSU DLA: {home} vs {away} | TYP: {target_bet}")
    print("=" * 70)
    
    for bookie, path in [("SUPERBET", "superbet_baza_5dni.json"), ("FORTUNA", "fortuna_baza_5dni.json")]:
        print(f"\n--- 🏬 ŹRÓDŁO: {bookie} ---")
        try:
            with open(path, "r", encoding="utf-8") as f:
                db = json.load(f)
        except Exception as e:
            print(f"Brak pliku {path}: {e}")
            continue

        matched_record = None
        h_c, a_c = clean_txt(home), clean_txt(away)
        for k, v in db.items():
            if "___" in k:
                b_h, b_a = k.split("___")
                if (h_c in clean_txt(b_h) or clean_txt(b_h) in h_c) and (a_c in clean_txt(b_a) or clean_txt(b_a) in a_c):
                    matched_record = (k, v)
                    break

        if not matched_record:
            print("❌ Mecz NIE został znaleziony w bazie! Sprawdź pisownię w słowniku.")
            continue

        key_name, data = matched_record
        print(f"✅ Znaleziono mecz pod kluczem: '{key_name}'")
        
        # Sprawdzanie sekcji 'kursy'
        kursy = data.get("kursy", {})
        if target_bet in kursy:
            print(f"🎯 Sekcja 'kursy' zawiera bezpośredni kurs: {target_bet} -> {kursy[target_bet]}")
            
        # Sprawdzanie sekcji 'rynki'
        rynki = data.get("rynki", {})
        znalezione_w_rynkach = []
        for r_name, r_opts in rynki.items():
            clean_r = r_name.replace("\n", " ")
            for opt_k, opt_v in r_opts.items():
                # Szukamy powiązań z linią lub typem
                line_search = re.findall(r"\d+\.\d+", target_bet)
                if line_search and line_search[0] in f"{clean_r} {opt_k}":
                    znalezione_w_rynkach.append((clean_r, opt_k, opt_v))
                elif target_bet in opt_k or target_bet in clean_r:
                    znalezione_w_rynkach.append((clean_r, opt_k, opt_v))

        if znalezione_w_rynkach:
            print("📑 Dopasowane rynki z sekcji 'rynki':")
            for r_n, opt_k, val in znalezione_w_rynkach[:8]: # Pokaż max 8
                print(f"   • Rynek: [{r_n}] | Opcja: [{opt_k}] -> Kurs: {val}")
        else:
            print("⚠️ Brak pasujących rynków w sekcji szczegółowej.")

if __name__ == "__main__":
    h = sys.argv[1] if len(sys.argv) > 1 else "Rakow"
    a = sys.argv[2] if len(sys.argv) > 2 else "Zaglebie"
    t = sys.argv[3] if len(sys.argv) > 3 else "A_AH+2.5"
    inspect_match(h, a, t)