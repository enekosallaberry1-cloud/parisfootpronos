#!/usr/bin/env python3
"""
Récupère le calendrier, les résultats, et une analyse LÉGÈRE (forme, H2H,
fatigue, effet de relâchement, vrai avantage du terrain — mais PAS
d'invincibilité ni de modèle Poisson, trop coûteux en requêtes) pour des
compétitions non couvertes par football-data.org : supercoupes, coupes
nationales, Ligue des Nations.

Utilise API-Football (api-sports.io), dont le plan GRATUIT donne accès à
TOUTES les compétitions (contrairement à football-data.org qui verrouille
par compétition) — la contrainte ici est un quota de 100 requêtes/jour,
pas une liste de compétitions autorisées.

Comme ces compétitions ont très peu de matchs simultanés (une supercoupe =
1 match/an, une coupe nationale = quelques journées espacées dans la saison),
le quota de 100/jour reste large la plupart du temps.

Écrit dans data/extra_competitions.json, séparément du reste, pour ne jamais
risquer de perturber le pipeline principal (football-data.org) qui fonctionne déjà.

Variable d'environnement requise :
    API_FOOTBALL_KEY   ta clé API-Football (api-sports.io ou RapidAPI)

Lancement local pour tester :
    export API_FOOTBALL_KEY="ta_cle"
    python3 scripts/fetch_extra_competitions.py
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API_KEY = os.environ.get("API_FOOTBALL_KEY")
API_BASE = "https://v3.football.api-sports.io"

# (nom à rechercher dans l'API, pays si utile pour désambiguïser, nom d'affichage FR)
EXTRA_COMPETITIONS = [
    ("UEFA Super Cup", None, "Supercoupe de l'UEFA"),
    ("DFL-Supercup", "Germany", "Supercoupe d'Allemagne"),
    ("Supercopa de España", "Spain", "Supercoupe d'Espagne"),
    ("Copa del Rey", "Spain", "Coupe d'Espagne"),
    ("Coppa Italia", "Italy", "Coupe d'Italie"),
    ("Coupe de France", "France", "Coupe de France"),
    ("Trophee des Champions", "France", "Trophée des Champions"),
    ("UEFA Nations League", None, "Ligue des Nations"),
]

DATE_FROM = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
DATE_TO = (datetime.now(timezone.utc) + timedelta(days=21)).strftime("%Y-%m-%d")
CURRENT_YEAR = datetime.now(timezone.utc).year

SLEEP_BETWEEN_CALLS = 2  # quota journalier, pas besoin d'une pause aussi longue que par minute

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "extra_competitions.json")
LEAGUE_ID_CACHE_PATH = os.path.join(DATA_DIR, "extra_league_ids_cache.json")

_requests_used = 0
_team_cache = {}


def api_get(path, params=None):
    """Appel générique à l'API-Football, avec compteur de requêtes utilisées
    (affiché en fin d'exécution pour surveiller le quota de 100/jour)."""
    global _requests_used
    url = f"{API_BASE}{path}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{query}"
    req = Request(url, headers={"x-apisports-key": API_KEY})
    try:
        with urlopen(req, timeout=20) as resp:
            _requests_used += 1
            time.sleep(SLEEP_BETWEEN_CALLS)
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        _requests_used += 1
        time.sleep(SLEEP_BETWEEN_CALLS)
        print(f"[erreur] {path} -> HTTP {e.code} : {e.read().decode('utf-8', 'ignore')[:200]}")
        return None
    except Exception as e:
        print(f"[erreur] {path} -> {e}")
        return None


def load_league_id_cache():
    if os.path.exists(LEAGUE_ID_CACHE_PATH):
        try:
            with open(LEAGUE_ID_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_league_id_cache(cache):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LEAGUE_ID_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def resolve_league_id(search_name, country, cache):
    """Recherche l'identifiant numérique d'une compétition par son nom (mis en
    cache dans un fichier committé au dépôt, pour ne payer ce coût qu'une seule
    fois plutôt qu'à chaque exécution)."""
    if search_name in cache:
        return cache[search_name]

    data = api_get("/leagues", {"search": search_name.replace(" ", "%20")})
    if not data or not data.get("response"):
        print(f"[attention] Compétition introuvable dans API-Football : {search_name}")
        cache[search_name] = None
        return None

    candidates = data["response"]
    match = None
    if country:
        for c in candidates:
            if c.get("country", {}).get("name", "").lower() == country.lower():
                match = c
                break
    if not match:
        match = candidates[0]

    league_id = match.get("league", {}).get("id")
    cache[search_name] = league_id
    print(f"[info] '{search_name}' -> id API-Football {league_id}")
    return league_id


def fetch_fixtures(league_id):
    """Récupère les matchs d'une compétition sur la fenêtre de dates, en
    essayant la saison en cours puis l'année précédente (les compétitions à
    cheval sur deux années civiles sont parfois indexées sous l'année de
    début de saison)."""
    all_fixtures = []
    seen_ids = set()
    for season in (CURRENT_YEAR, CURRENT_YEAR - 1):
        data = api_get("/fixtures", {
            "league": league_id, "season": season,
            "from": DATE_FROM, "to": DATE_TO,
        })
        if data and data.get("response"):
            for f in data["response"]:
                fid = f.get("fixture", {}).get("id")
                if fid and fid not in seen_ids:
                    seen_ids.add(fid)
                    all_fixtures.append(f)
    return all_fixtures


def get_team_form(team_id):
    if team_id in _team_cache:
        return _team_cache[team_id]
    data = api_get("/fixtures", {"team": team_id, "last": 10, "status": "FT"})
    if not data or not data.get("response"):
        _team_cache[team_id] = None
        return None

    fixtures = sorted(data["response"], key=lambda f: f["fixture"]["date"])
    points, gf, ga, results = 0, 0, 0, []
    last_match_away_win = False
    for f in fixtures:
        is_home = f["teams"]["home"]["id"] == team_id
        my_score = f["goals"]["home"] if is_home else f["goals"]["away"]
        opp_score = f["goals"]["away"] if is_home else f["goals"]["home"]
        if my_score is None or opp_score is None:
            continue
        gf += my_score
        ga += opp_score
        if my_score > opp_score:
            points += 3
            results.append("V")
        elif my_score == opp_score:
            points += 1
            results.append("N")
        else:
            results.append("D")
    if fixtures:
        last = fixtures[-1]
        last_is_home = last["teams"]["home"]["id"] == team_id
        lm = last["goals"]["home"] if last_is_home else last["goals"]["away"]
        lo = last["goals"]["away"] if last_is_home else last["goals"]["home"]
        if lm is not None and lo is not None and not last_is_home and lm > lo:
            last_match_away_win = True

    counted = len(results)
    result = {
        "points": round(points / counted * 5, 2) if counted else 0,
        "goals_for": round(gf / counted * 5, 2) if counted else 0,
        "goals_against": round(ga / counted * 5, 2) if counted else 0,
        "results": results[-5:],
        "last_match_date": fixtures[-1]["fixture"]["date"] if fixtures else None,
        "last_match_away_win": last_match_away_win,
    }
    _team_cache[team_id] = result
    return result


def get_head_to_head(home_id, away_id):
    data = api_get("/fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": 5})
    if not data or not data.get("response"):
        return None
    home_wins = away_wins = draws = 0
    for f in data["response"]:
        hs, aws = f["goals"]["home"], f["goals"]["away"]
        if hs is None or aws is None:
            continue
        fh_id = f["teams"]["home"]["id"]
        if hs == aws:
            draws += 1
        elif (hs > aws) == (fh_id == home_id):
            home_wins += 1
        else:
            away_wins += 1
    return {"total_matches": home_wins + away_wins + draws, "home_wins": home_wins, "away_wins": away_wins, "draws": draws}


def get_team_venue_city(team_id):
    data = api_get("/teams", {"id": team_id})
    if not data or not data.get("response"):
        return None
    return (data["response"][0].get("venue", {}) or {}).get("city")


def rest_days_before(form, upcoming_iso_date):
    if not form or not form.get("last_match_date") or not upcoming_iso_date:
        return None
    try:
        last = datetime.fromisoformat(form["last_match_date"])
        upcoming = datetime.fromisoformat(upcoming_iso_date)
        return (upcoming - last).days
    except Exception:
        return None


# Ces 4 compétitions n'ont qu'UN SEUL match par édition (donc toujours "la finale")
SINGLE_MATCH_COMPETITIONS = {"UEFA Super Cup", "DFL-Supercup", "Supercopa de España", "Trophee des Champions"}


def determine_home_advantage(search_name, stage, home_id, away_id, match_city):
    """Comme pour la Coupe du Monde : une finale n'a PAS un avantage du terrain
    automatique. Neutre par défaut, sauf si la ville du match correspond
    vraiment à la ville de l'une des deux équipes.

    Attention : on vérifie une correspondance EXACTE avec "FINAL" (pas une
    simple sous-chaîne), sinon "1/8-Finals" ou "Semi-finals" déclencheraient
    ce cas à tort à cause du mot "final" qu'ils contiennent aussi."""
    normalized_stage = (stage or "").strip().upper()
    is_final_like = search_name in SINGLE_MATCH_COMPETITIONS or normalized_stage == "FINAL"
    if not is_final_like:
        return "home"  # coupe nationale en phase normale : logique habituelle

    if not match_city:
        return None
    home_city = get_team_venue_city(home_id)
    away_city = get_team_venue_city(away_id)
    mc = match_city.strip().lower()
    if home_city and home_city.strip().lower() == mc:
        return "home"
    if away_city and away_city.strip().lower() == mc:
        return "away"
    return None


def compute_suggestion(home_form, away_form, h2h, rest_home, rest_away, home_name, away_name, advantage_side):
    if not home_form or not away_form:
        return None

    score_home = home_form["points"]
    score_away = away_form["points"]
    if advantage_side == "home":
        score_home += 2.0
    elif advantage_side == "away":
        score_away += 2.0

    score_home += (home_form["goals_for"] - home_form["goals_against"]) * 0.3
    score_away += (away_form["goals_for"] - away_form["goals_against"]) * 0.3

    if h2h and h2h["total_matches"] > 0:
        diff = (h2h["home_wins"] - h2h["away_wins"]) / h2h["total_matches"]
        score_home += diff * 2
        score_away -= diff * 2

    fatigue_home = fatigue_away = None
    if rest_home is not None and rest_home <= 3:
        score_home -= 1.8
        fatigue_home = "enchaînement serré"
    if rest_away is not None and rest_away <= 3:
        score_away -= 1.8
        fatigue_away = "enchaînement serré"

    letdown_home = bool(home_form.get("last_match_away_win"))
    letdown_away = bool(away_form.get("last_match_away_win"))
    if letdown_home:
        score_home -= 1.0
    if letdown_away:
        score_away -= 1.0

    diff = score_home - score_away
    if diff >= 2.5:
        pick, confidence = f"Double chance : {home_name} ou nul", "Moyenne" if diff < 5 else "Élevée"
    elif diff <= -2.5:
        pick, confidence = f"Double chance : {away_name} ou nul", "Moyenne" if diff > -5 else "Élevée"
    else:
        pick, confidence = "Match équilibré / risque de surprise", "Faible"

    return {
        "suggested_pick": pick, "confidence": confidence,
        "true_home_advantage": advantage_side,
        "rest_days_home": rest_home, "rest_days_away": rest_away,
        "fatigue_home": fatigue_home, "fatigue_away": fatigue_away,
        "letdown_home": letdown_home, "letdown_away": letdown_away,
    }


def main():
    if not API_KEY:
        print("ERREUR : la variable d'environnement API_FOOTBALL_KEY n'est pas définie.")
        sys.exit(1)

    league_cache = load_league_id_cache()
    all_matches = []

    for search_name, country, display_name in EXTRA_COMPETITIONS:
        league_id = resolve_league_id(search_name, country, league_cache)
        save_league_id_cache(league_cache)  # sauvegarde au fur et à mesure
        if not league_id:
            continue

        fixtures = fetch_fixtures(league_id)
        # on ne calcule l'analyse légère que pour les 6 prochains matchs max
        # (contrôle strict du quota — ces compétitions ont peu de matchs de
        # toute façon, donc cette limite ne sera presque jamais atteinte)
        upcoming = sorted(
            [f for f in fixtures if f["fixture"]["status"]["short"] in ("NS", "TBD")],
            key=lambda f: f["fixture"]["date"]
        )
        deep_ids = {f["fixture"]["id"] for f in upcoming[:6]}

        for f in fixtures:
            fixture = f["fixture"]
            teams = f["teams"]
            goals = f["goals"]
            entry = {
                "competition": display_name,
                "utcDate": fixture["date"],
                "status": "FINISHED" if fixture["status"]["short"] == "FT"
                          else "IN_PLAY" if fixture["status"]["short"] in ("1H", "2H", "HT")
                          else "SCHEDULED",
                "stage": f.get("league", {}).get("round"),
                "homeTeam": teams["home"]["name"], "homeCrest": teams["home"]["logo"],
                "awayTeam": teams["away"]["name"], "awayCrest": teams["away"]["logo"],
                "homeScore": goals["home"], "awayScore": goals["away"],
                "analysis": None,
            }

            if fixture["id"] in deep_ids:
                home_id, away_id = teams["home"]["id"], teams["away"]["id"]
                home_form = get_team_form(home_id)
                away_form = get_team_form(away_id)
                h2h = get_head_to_head(home_id, away_id)
                rest_home = rest_days_before(home_form, fixture["date"])
                rest_away = rest_days_before(away_form, fixture["date"])
                match_city = (fixture.get("venue") or {}).get("city")
                advantage = determine_home_advantage(search_name, entry["stage"], home_id, away_id, match_city)
                suggestion = compute_suggestion(
                    home_form, away_form, h2h, rest_home, rest_away,
                    teams["home"]["name"], teams["away"]["name"], advantage,
                )
                if suggestion:
                    entry["analysis"] = {"home_form": home_form, "away_form": away_form, "head_to_head": h2h, **suggestion}

            all_matches.append(entry)

    all_matches.sort(key=lambda m: m["utcDate"] or "")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(all_matches),
        "requests_used_this_run": _requests_used,
        "matches": all_matches,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"OK : {len(all_matches)} matchs écrits -> {OUTPUT_PATH}")
    print(f"Requêtes API-Football utilisées cette exécution : {_requests_used} / 100 par jour")


if __name__ == "__main__":
    main()
