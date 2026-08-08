#!/usr/bin/env python3
"""
Récupère les matchs (calendrier + résultats) des compétitions suivies via l'API
gratuite football-data.org, calcule automatiquement une analyse statistique
(forme récente, buts marqués/encaissés, confrontations directes, avantage du
terrain) et une suggestion de pari en double chance, puis écrit tout ça dans
data/matches.json.

Aucune cote n'est utilisée : la "confiance" et le pari suggéré viennent d'un
calcul statistique simple (transparent, pas une boîte noire), pas d'une IA ni
d'un bookmaker. Pense à toujours vérifier la cote réelle sur Winamax avant de
parier, et à garder un œil critique sur la suggestion automatique.

Ce script est fait pour tourner via GitHub Actions (voir
.github/workflows/update-matches.yml), mais tu peux aussi le lancer en local :

    export FOOTBALL_DATA_TOKEN="ta_cle_api"
    python3 scripts/fetch_matches.py
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API_BASE = "https://api.football-data.org/v4"
TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN")

# Codes de compétitions football-data.org.
# WC = Coupe du Monde, CL = Ligue des Champions, PL/PD/BL1/SA/FL1 = les 5 grands championnats.
# EL (Europa League) n'est pas garantie sur le plan gratuit : si l'API répond 403,
# le script l'ignore simplement et continue.
COMPETITIONS = {
    "WC":  "Coupe du Monde",
    "CL":  "Ligue des Champions",
    "PL":  "Premier League",
    "PD":  "Liga",
    "BL1": "Bundesliga",
    "SA":  "Serie A",
    "FL1": "Ligue 1",
    "EL":  "Europa League",
}

# Fenêtre de dates : de 3 jours en arrière (pour garder les derniers résultats)
# à 21 jours à venir (pour voir le calendrier proche).
DATE_FROM = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
DATE_TO   = (datetime.now(timezone.utc) + timedelta(days=21)).strftime("%Y-%m-%d")

# Nombre max de matchs à venir pour lesquels on calcule l'analyse poussée
# (forme + H2H = 2-3 appels API supplémentaires par match). On limite pour
# rester large sous la limite de 10 requêtes/minute du plan gratuit et pour
# que le job GitHub Actions ne tourne pas trop longtemps.
MAX_DEEP_ANALYSIS = 20

# Pause de sécurité entre CHAQUE appel API (secondes). Avec 10 req/min autorisées,
# une pause de 7s garantit ~8,5 requêtes/minute maximum : jamais de blocage.
SLEEP_BETWEEN_CALLS = 7

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "matches.json")
PREDICTION_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "prediction_log.json")

_team_form_cache = {}


def api_get(path):
    url = f"{API_BASE}{path}"
    req = Request(url, headers={"X-Auth-Token": TOKEN})
    try:
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            time.sleep(SLEEP_BETWEEN_CALLS)
            return data
    except HTTPError as e:
        time.sleep(SLEEP_BETWEEN_CALLS)
        if e.code == 403:
            print(f"[info] Accès refusé (plan gratuit) pour {path}")
        elif e.code == 429:
            print(f"[attention] Limite de requêtes atteinte pour {path}")
        else:
            print(f"[erreur] {path} -> HTTP {e.code}")
        return None
    except Exception as e:
        time.sleep(SLEEP_BETWEEN_CALLS)
        print(f"[erreur] {path} -> {e}")
        return None


def fetch_competition_matches(code):
    data = api_get(f"/competitions/{code}/matches?dateFrom={DATE_FROM}&dateTo={DATE_TO}")
    return data.get("matches", []) if data else []


def get_team_form(team_id):
    """Retourne la forme des 10 derniers matchs terminés d'une équipe (historique
    élargi par rapport aux 5 précédents, pour lisser les séries exceptionnelles) :
    points par match, buts marqués/encaissés par match, et la date du dernier
    match joué (pour calculer le repos avant le prochain match). Les champs
    "points"/"goals_for"/"goals_against" restent exprimés en équivalent-5-matchs
    (points_par_match * 5) pour ne pas avoir à retoucher tous les seuils de la
    formule ailleurs dans le script, tout en bénéficiant d'un échantillon plus
    stable de 10 matchs. Mis en cache pour ne pas refaire le même appel si
    l'équipe apparaît dans plusieurs matchs analysés."""
    if team_id in _team_form_cache:
        return _team_form_cache[team_id]

    data = api_get(f"/teams/{team_id}/matches?status=FINISHED&limit=10")
    if not data or not data.get("matches"):
        result = None
    else:
        matches_sorted = sorted(data["matches"], key=lambda m: m.get("utcDate") or "")
        points, gf, ga, results = 0, 0, 0, []
        counted = 0
        last_match_away_win = False
        for m in matches_sorted:
            is_home = m["homeTeam"]["id"] == team_id
            my_score = m["score"]["fullTime"]["home"] if is_home else m["score"]["fullTime"]["away"]
            opp_score = m["score"]["fullTime"]["away"] if is_home else m["score"]["fullTime"]["home"]
            if my_score is None or opp_score is None:
                continue
            counted += 1
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
        # Détection de l'effet de relâchement : une victoire à l'extérieur au
        # dernier match peut annoncer un contrecoup (pas systématique, juste un
        # facteur de risque à signaler). On regarde spécifiquement le tout
        # dernier match joué (dernier élément une fois trié par date).
        if matches_sorted:
            last = matches_sorted[-1]
            last_is_home = last["homeTeam"]["id"] == team_id
            last_my_score = last["score"]["fullTime"]["home"] if last_is_home else last["score"]["fullTime"]["away"]
            last_opp_score = last["score"]["fullTime"]["away"] if last_is_home else last["score"]["fullTime"]["home"]
            if (last_my_score is not None and last_opp_score is not None
                    and not last_is_home and last_my_score > last_opp_score):
                last_match_away_win = True
        last_match_date = matches_sorted[-1].get("utcDate") if matches_sorted else None

        goals_for_avg = round(gf / counted, 2) if counted else None
        goals_against_avg = round(ga / counted, 2) if counted else None
        points_5eq = round(points / counted * 5, 2) if counted else 0
        goals_for_5eq = round(goals_for_avg * 5, 2) if goals_for_avg is not None else 0
        goals_against_5eq = round(goals_against_avg * 5, 2) if goals_against_avg is not None else 0

        result = {
            "points": points_5eq, "goals_for": goals_for_5eq, "goals_against": goals_against_5eq,
            "goals_for_avg": goals_for_avg, "goals_against_avg": goals_against_avg,
            "matches_counted": counted,
            "results": results[-5:],  # affichage limité aux 5 plus récents, lisibilité
            "last_match_date": last_match_date,
            "last_match_away_win": last_match_away_win,
            "style_tag": compute_style_tag(goals_for_avg, goals_against_avg),
        }

    _team_form_cache[team_id] = result
    return result


def compute_style_tag(goals_for_avg, goals_against_avg):
    """Étiquette de style de jeu approximative, déduite uniquement des buts
    marqués/encaissés par match (gratuit, pas de donnée tactique réelle) :
    une équipe qui encaisse beaucoup annonce des matchs ouverts, une équipe qui
    marque peu mais n'encaisse presque rien annonce des matchs fermés/tactiques."""
    if goals_for_avg is None or goals_against_avg is None:
        return None
    if goals_against_avg <= 0.8 and goals_for_avg <= 1.3:
        return "Registre fermé et défensif"
    if goals_for_avg >= 2.0 and goals_against_avg >= 1.3:
        return "Matchs ouverts, beaucoup de buts des deux côtés"
    if goals_for_avg >= 1.8 and goals_against_avg <= 1.0:
        return "Équipe dominante, efficace et solide"
    if goals_against_avg >= 1.8:
        return "Défense friable, susceptible d'encaisser"
    return "Profil équilibré"


def rest_days_before(form, upcoming_utc_date):
    """Nombre de jours de repos avant le prochain match. Renvoie None si la
    donnée manque (pas de matchs récents trouvés)."""
    if not form or not form.get("last_match_date") or not upcoming_utc_date:
        return None
    try:
        last = datetime.fromisoformat(form["last_match_date"].replace("Z", "+00:00"))
        upcoming = datetime.fromisoformat(upcoming_utc_date.replace("Z", "+00:00"))
        return (upcoming - last).days
    except Exception:
        return None


def get_venue_record(team_id, venue):
    """Calcule le bilan d'une équipe spécifiquement à domicile ou à l'extérieur
    sur ses 10 derniers matchs dans ce contexte : taux d'invincibilité (victoires
    + nuls), série d'invincibilité en cours, ET moyenne de buts marqués/encaissés
    dans ce contexte précis (utilisée par le modèle de probabilités Poisson,
    plus pertinent qu'une moyenne générale car les taux de buts diffèrent
    souvent nettement entre domicile et extérieur). Mis en cache par équipe+contexte."""
    cache_key = f"{team_id}_{venue}"
    if cache_key in _team_form_cache:
        return _team_form_cache[cache_key]

    data = api_get(f"/teams/{team_id}/matches?status=FINISHED&venue={venue}&limit=10")
    if not data or not data.get("matches"):
        result = None
    else:
        matches_sorted = sorted(data["matches"], key=lambda m: m.get("utcDate") or "", reverse=True)
        total = 0
        unbeaten = 0
        streak = 0
        streak_broken = False
        gf_sum = 0
        ga_sum = 0
        for m in matches_sorted:
            is_home = m["homeTeam"]["id"] == team_id
            my_score = m["score"]["fullTime"]["home"] if is_home else m["score"]["fullTime"]["away"]
            opp_score = m["score"]["fullTime"]["away"] if is_home else m["score"]["fullTime"]["home"]
            if my_score is None or opp_score is None:
                continue
            total += 1
            gf_sum += my_score
            ga_sum += opp_score
            lost = my_score < opp_score
            if not lost:
                unbeaten += 1
                if not streak_broken:
                    streak += 1
            else:
                streak_broken = True
        result = {
            "matches_played": total,
            "unbeaten_count": unbeaten,
            "unbeaten_rate": round(unbeaten / total, 2) if total else None,
            "current_unbeaten_streak": streak,
            "avg_goals_for": round(gf_sum / total, 2) if total else None,
            "avg_goals_against": round(ga_sum / total, 2) if total else None,
        }

    _team_form_cache[cache_key] = result
    return result


def get_head_to_head(match_id):
    data = api_get(f"/matches/{match_id}/head2head?limit=5")
    if not data:
        return None
    agg = data.get("aggregates", {})
    return {
        "total_matches": agg.get("numberOfMatches", 0),
        "home_wins": agg.get("homeTeam", {}).get("wins", 0),
        "away_wins": agg.get("awayTeam", {}).get("wins", 0),
        "draws": agg.get("draws", 0),
    }


# Nations hôtes de la Coupe du Monde 2026 : seules ces équipes jouent vraiment
# "à domicile" pendant le tournoi. Toutes les autres affiches sont sur terrain
# neutre, même quand l'API désigne une équipe comme "homeTeam" par convention
# administrative (ce champ ne veut PAS dire qu'elle joue chez elle).
WORLD_CUP_HOST_NATIONS = {"united states", "usa", "mexico", "méxico", "canada"}

# Mots-clés indiquant une phase à élimination directe (finale, demi-finale, etc.) :
# pour ces matchs, l'avantage du terrain n'est PAS automatique — il faut vérifier
# si le stade correspond vraiment au terrain de l'une des deux équipes (ex. la
# finale de la DFL-Supercup 2025 s'est jouée à Dortmund, donc le Borussia y était
# vraiment à domicile ; d'autres finales sont sur un stade neutre pour tout le monde).
KNOCKOUT_STAGE_KEYWORDS = ("FINAL", "SEMI", "QUARTER", "LAST_16", "LAST_32", "ROUND_OF", "PLAYOFF", "THIRD_PLACE")

_team_venue_cache = {}


def get_team_stadium(team_id):
    """Récupère le nom du stade habituel d'une équipe (pour vérifier si un match
    à enjeu neutre par défaut se joue en fait chez l'une des deux équipes)."""
    if team_id in _team_venue_cache:
        return _team_venue_cache[team_id]
    data = api_get(f"/teams/{team_id}")
    venue = data.get("venue") if data else None
    _team_venue_cache[team_id] = venue
    return venue


def determine_home_advantage(competition_code, stage, home_id, home_name,
                              away_id, away_name, match_venue):
    """Renvoie 'home', 'away', ou None (terrain neutre) selon qui bénéficie
    RÉELLEMENT d'un avantage du terrain — jamais une supposition automatique.

    - Coupe du Monde : seules les 3 nations hôtes (États-Unis, Mexique, Canada)
      ont un vrai avantage, et seulement quand c'est elles qui jouent. Toutes
      les autres affiches sont neutres, quel que soit le champ "homeTeam" de
      l'API.
    - Finales / phases à élimination directe (toutes compétitions) : neutre par
      défaut, sauf si le stade du match correspond vraiment au stade de l'une
      des deux équipes (comparaison textuelle avec son stade habituel).
    - Tous les autres matchs (championnat, phase de ligue C1, etc.) : l'avantage
      du terrain habituel s'applique normalement à l'équipe "domicile".
    """
    if competition_code == "WC":
        if home_name and home_name.strip().lower() in WORLD_CUP_HOST_NATIONS:
            return "home"
        if away_name and away_name.strip().lower() in WORLD_CUP_HOST_NATIONS:
            return "away"
        return None

    if stage and any(k in stage.upper() for k in KNOCKOUT_STAGE_KEYWORDS):
        if not match_venue:
            return None  # pas d'info fiable -> on ne suppose rien, par sécurité
        home_stadium = get_team_stadium(home_id)
        away_stadium = get_team_stadium(away_id)
        mv = match_venue.strip().lower()
        if home_stadium and home_stadium.strip().lower() in mv:
            return "home"
        if away_stadium and away_stadium.strip().lower() in mv:
            return "away"
        return None

    return "home"  # championnat classique, phase de ligue C1 : comportement normal


_standings_cache = {}


def get_standings(competition_code):
    if competition_code in _standings_cache:
        return _standings_cache[competition_code]
    data = api_get(f"/competitions/{competition_code}/standings")
    _standings_cache[competition_code] = data
    return data


def compute_stakes(competition_code, stage, team_id):
    """Évalue l'enjeu réel du match pour une équipe : course à la Ligue des
    Champions, lutte pour le maintien, zone Europe, ou milieu de tableau sans
    grand enjeu. Se base sur le classement actuel du championnat."""
    if competition_code == "WC" or (stage and any(k in stage.upper() for k in KNOCKOUT_STAGE_KEYWORDS)):
        return "Match à élimination directe — enjeu maximal (une défaite = fin du parcours)"

    standings = get_standings(competition_code)
    if not standings:
        return None

    for group in standings.get("standings", []):
        table = group.get("table", [])
        total = len(table)
        if total < 4:
            continue
        for row in table:
            if row.get("team", {}).get("id") != team_id:
                continue
            position = row.get("position")
            if position <= 4:
                return f"Course à la Ligue des Champions ({position}e place actuelle)"
            if position > total - 3:
                return f"Lutte pour le maintien ({position}e place actuelle)"
            if position <= 6:
                return f"Zone Europe / Europa League ({position}e place actuelle)"
            return f"Milieu de tableau, enjeu limité ({position}e place actuelle)"
    return None


def _poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def compute_poisson_probabilities(lambda_home, lambda_away, max_goals=8):
    """Modèle statistique standard (loi de Poisson) qui transforme des taux de
    buts marqués/encaissés en VRAIES probabilités de résultat — contrairement
    au système de points par tranches utilisé ailleurs dans ce script, ceci
    donne un vrai pourcentage pour chaque issue (victoire/nul/défaite), plus
    la probabilité "plus de 2,5 buts" et "les deux équipes marquent".

    Limite honnête : ce modèle simplifié ne normalise pas par rapport à la
    moyenne de la ligue (une vraie modélisation Poisson professionnelle calcule
    une "force d'attaque/défense" relative à la moyenne du championnat) — ici,
    on utilise directement les moyennes de buts du contexte pertinent (domicile
    ou extérieur selon le vrai avantage du terrain). Sur un échantillon de 10
    matchs, ça reste une estimation, pas une certitude.
    """
    if lambda_home is None or lambda_away is None or lambda_home <= 0 or lambda_away <= 0:
        return None

    home_win = draw = away_win = over_2_5 = btts = 0.0
    for h in range(max_goals + 1):
        ph = _poisson_pmf(h, lambda_home)
        for a in range(max_goals + 1):
            pa = _poisson_pmf(a, lambda_away)
            p = ph * pa
            if h > a:
                home_win += p
            elif h == a:
                draw += p
            else:
                away_win += p
            if h + a > 2.5:
                over_2_5 += p
            if h > 0 and a > 0:
                btts += p

    # normalisation légère (la troncature à max_goals laisse une masse résiduelle négligeable)
    total = home_win + draw + away_win
    if total <= 0:
        return None

    return {
        "home_win_pct": round(home_win / total * 100, 1),
        "draw_pct": round(draw / total * 100, 1),
        "away_win_pct": round(away_win / total * 100, 1),
        "over_2_5_pct": round(over_2_5 / total * 100, 1),
        "btts_pct": round(btts / total * 100, 1),
        "lambda_home": round(lambda_home, 2),
        "lambda_away": round(lambda_away, 2),
    }


def compute_match_lambdas(home_id, away_id, advantage_side):
    """Détermine le contexte pertinent (domicile ou extérieur) pour CHAQUE
    équipe selon le vrai avantage du terrain (jamais une supposition — voir
    determine_home_advantage), récupère les moyennes de buts dans ce contexte,
    puis calcule les taux de buts attendus (lambda) pour le modèle Poisson.
    Terrain neutre -> les deux équipes sont évaluées sur leurs stats à
    l'extérieur (la situation la plus proche d'une absence de confort du terrain)."""
    if advantage_side == "home":
        home_context, away_context = "HOME", "AWAY"
    elif advantage_side == "away":
        home_context, away_context = "AWAY", "HOME"
    else:
        home_context, away_context = "AWAY", "AWAY"

    home_record = get_venue_record(home_id, home_context)
    away_record = get_venue_record(away_id, away_context)

    if not home_record or not away_record:
        return None, None
    if home_record.get("avg_goals_for") is None or away_record.get("avg_goals_for") is None:
        return None, None

    lambda_home = (home_record["avg_goals_for"] + away_record["avg_goals_against"]) / 2
    lambda_away = (away_record["avg_goals_for"] + home_record["avg_goals_against"]) / 2
    return lambda_home, lambda_away


def compute_suggestion(home_form, away_form, h2h, rest_home, rest_away,
                        home_name, away_name,
                        home_venue_record=None, away_venue_record=None,
                        advantage_side="home", stakes_home=None, stakes_away=None):
    """Calcul transparent (pas une IA) : additionne des points de forme, un bonus
    de terrain, un ajustement selon l'historique direct, un ajustement de
    fatigue selon le nombre de jours de repos avant le match (les enchaînements
    à 3 jours, fréquents en février-avril avec la Ligue des Champions + les
    championnats + les coupes nationales qui se chevauchent, pèsent sur les
    organismes), un léger malus « effet de relâchement » pour une équipe qui
    vient de décrocher une victoire à l'extérieur (pas systématique, mais
    documenté : ex. Bayern vainqueur à Paris puis nul contre l'Union Berlin,
    Paris FC vainqueur à Monaco puis battu par Rennes), ET un bonus lié au
    taux d'invincibilité spécifique domicile/extérieur (une équipe increvable
    chez elle depuis X matchs, ou à l'inverse très friable à l'extérieur, ça
    compte). Renvoie None si pas assez de données.

    Important : ceci reste une formule statistique simple, pas une prédiction
    fiable à 100%. Le foot produit régulièrement des surprises (un petit
    Nation qui tient 0-0 face à un favori, par exemple) qu'aucune formule ne
    peut anticiper de façon fiable — la zone "pas de tendance claire" est donc
    volontairement large plutôt que de forcer un favori à chaque match.
    """
    if not home_form or not away_form:
        return None

    HOME_ADVANTAGE_BONUS = 2.0
    FATIGUE_THRESHOLD_SEVERE = 3   # 3 jours ou moins entre 2 matchs = enchaînement serré
    FATIGUE_THRESHOLD_LIGHT = 4
    FATIGUE_PENALTY_SEVERE = -1.8
    FATIGUE_PENALTY_LIGHT = -0.8
    FRESHNESS_BONUS = 0.5          # plus de 7 jours de repos = équipe fraîche
    LETDOWN_PENALTY = -1.0         # petit malus après une victoire marquante à l'extérieur

    score_home = home_form["points"]
    score_away = away_form["points"]

    # avantage du terrain : appliqué UNIQUEMENT au camp qui joue vraiment chez
    # lui (jamais une supposition automatique — voir determine_home_advantage)
    if advantage_side == "home":
        score_home += HOME_ADVANTAGE_BONUS
    elif advantage_side == "away":
        score_away += HOME_ADVANTAGE_BONUS
    # advantage_side == None -> terrain neutre, aucun bonus des deux côtés

    # différentiel de buts sur les 5 derniers matchs
    score_home += (home_form["goals_for"] - home_form["goals_against"]) * 0.3
    score_away += (away_form["goals_for"] - away_form["goals_against"]) * 0.3

    # effet de relâchement : une victoire à l'extérieur juste avant peut annoncer
    # un contrecoup (pas systématique — Bayern/PSG puis nul contre l'Union
    # Berlin, Paris FC/Monaco puis défaite contre Rennes en sont des exemples,
    # mais ça n'arrive pas à chaque fois). On applique donc un malus léger,
    # pas éliminatoire.
    letdown_home = bool(home_form.get("last_match_away_win"))
    letdown_away = bool(away_form.get("last_match_away_win"))
    if letdown_home:
        score_home += LETDOWN_PENALTY
    if letdown_away:
        score_away += LETDOWN_PENALTY

    # taux d'invincibilité domicile/extérieur : une équipe increvable chez elle
    # (ou une équipe qui ne perd presque jamais à l'extérieur) mérite un bonus ;
    # le poids reste modéré pour ne pas écraser les autres facteurs.
    INVINCIBILITY_WEIGHT = 3.0
    STREAK_BONUS_PER_MATCH = 0.15
    STREAK_BONUS_CAP = 1.5

    if home_venue_record and home_venue_record.get("unbeaten_rate") is not None:
        score_home += home_venue_record["unbeaten_rate"] * INVINCIBILITY_WEIGHT
        score_home += min(home_venue_record["current_unbeaten_streak"] * STREAK_BONUS_PER_MATCH, STREAK_BONUS_CAP)
    if away_venue_record and away_venue_record.get("unbeaten_rate") is not None:
        score_away += away_venue_record["unbeaten_rate"] * INVINCIBILITY_WEIGHT
        score_away += min(away_venue_record["current_unbeaten_streak"] * STREAK_BONUS_PER_MATCH, STREAK_BONUS_CAP)

    # confrontations directes
    if h2h and h2h["total_matches"] > 0:
        h2h_diff = (h2h["home_wins"] - h2h["away_wins"]) / h2h["total_matches"]
        score_home += h2h_diff * 2
        score_away -= h2h_diff * 2

    # fatigue / enchaînement des matchs
    fatigue_flag_home = fatigue_flag_away = None
    if rest_home is not None:
        if rest_home <= FATIGUE_THRESHOLD_SEVERE:
            score_home += FATIGUE_PENALTY_SEVERE
            fatigue_flag_home = "enchaînement serré"
        elif rest_home <= FATIGUE_THRESHOLD_LIGHT:
            score_home += FATIGUE_PENALTY_LIGHT
            fatigue_flag_home = "repos réduit"
        elif rest_home >= 7:
            score_home += FRESHNESS_BONUS
            fatigue_flag_home = "fraîche"
    if rest_away is not None:
        if rest_away <= FATIGUE_THRESHOLD_SEVERE:
            score_away += FATIGUE_PENALTY_SEVERE
            fatigue_flag_away = "enchaînement serré"
        elif rest_away <= FATIGUE_THRESHOLD_LIGHT:
            score_away += FATIGUE_PENALTY_LIGHT
            fatigue_flag_away = "repos réduit"
        elif rest_away >= 7:
            score_away += FRESHNESS_BONUS
            fatigue_flag_away = "fraîche"

    diff = score_home - score_away

    # Zone "pas de tendance claire" volontairement large : le foot produit des
    # surprises régulièrement, une formule ne doit pas prétendre à une certitude
    # qu'elle n'a pas. Le pick utilise toujours le nom réel de l'équipe plutôt
    # que "domicile/extérieur", pour ne jamais laisser croire à un avantage du
    # terrain qui n'existe pas (terrain neutre notamment).
    if diff >= 5:
        pick, confidence, predicted_side = f"Double chance : {home_name} ou nul", "Élevée", "home"
    elif diff >= 2.5:
        pick, confidence, predicted_side = f"Double chance : {home_name} ou nul", "Moyenne", "home"
    elif diff <= -5:
        pick, confidence, predicted_side = f"Double chance : {away_name} ou nul", "Élevée", "away"
    elif diff <= -2.5:
        pick, confidence, predicted_side = f"Double chance : {away_name} ou nul", "Moyenne", "away"
    else:
        pick, confidence, predicted_side = "Match équilibré / risque de surprise — aucune tendance statistique fiable", "Faible", None

    # signal explicite si un déséquilibre de fatigue ou de relâchement va à
    # l'encontre du favori statistique
    surprise_risk = False
    if fatigue_flag_home == "enchaînement serré" and diff > 0:
        surprise_risk = True
    if fatigue_flag_away == "enchaînement serré" and diff < 0:
        surprise_risk = True
    if letdown_home and diff > 0:
        surprise_risk = True
    if letdown_away and diff < 0:
        surprise_risk = True

    return {
        "suggested_pick": pick,
        "confidence": confidence,
        "predicted_side": predicted_side,
        "score_diff": round(diff, 1),
        "rest_days_home": rest_home,
        "rest_days_away": rest_away,
        "fatigue_home": fatigue_flag_home,
        "fatigue_away": fatigue_flag_away,
        "letdown_home": letdown_home,
        "letdown_away": letdown_away,
        "home_venue_record": home_venue_record,
        "away_venue_record": away_venue_record,
        "surprise_risk": surprise_risk,
        "true_home_advantage": advantage_side,  # 'home', 'away', ou None (terrain neutre)
        "stakes_home": stakes_home,
        "stakes_away": stakes_away,
    }


def normalize(match, competition_code, competition_name, deep=False):
    home = match.get("homeTeam", {}) or {}
    away = match.get("awayTeam", {}) or {}
    score = match.get("score", {}).get("fullTime", {}) or {}

    entry = {
        "competition": competition_name,
        "utcDate": match.get("utcDate"),
        "status": match.get("status"),
        "matchday": match.get("matchday"),
        "stage": match.get("stage"),
        "homeTeam": home.get("name"),
        "homeCrest": home.get("crest"),
        "awayTeam": away.get("name"),
        "awayCrest": away.get("crest"),
        "homeScore": score.get("home"),
        "awayScore": score.get("away"),
        "analysis": None,
    }

    if deep and home.get("id") and away.get("id"):
        home_form = get_team_form(home["id"])
        away_form = get_team_form(away["id"])
        h2h = get_head_to_head(match.get("id"))
        rest_home = rest_days_before(home_form, match.get("utcDate"))
        rest_away = rest_days_before(away_form, match.get("utcDate"))
        home_venue_record = get_venue_record(home["id"], "HOME")
        away_venue_record = get_venue_record(away["id"], "AWAY")

        advantage_side = determine_home_advantage(
            competition_code, match.get("stage"),
            home["id"], home.get("name"), away["id"], away.get("name"),
            match.get("venue"),
        )
        stakes_home = compute_stakes(competition_code, match.get("stage"), home["id"])
        stakes_away = compute_stakes(competition_code, match.get("stage"), away["id"])

        suggestion = compute_suggestion(
            home_form, away_form, h2h, rest_home, rest_away,
            home.get("name"), away.get("name"),
            home_venue_record, away_venue_record,
            advantage_side, stakes_home, stakes_away,
        )
        if suggestion:
            lambda_home, lambda_away = compute_match_lambdas(home["id"], away["id"], advantage_side)
            poisson = compute_poisson_probabilities(lambda_home, lambda_away)
            entry["analysis"] = {
                "home_form": home_form,
                "away_form": away_form,
                "head_to_head": h2h,
                "poisson": poisson,
                **suggestion,
            }

    return entry


def match_key(m):
    return f"{m['homeTeam']}__{m['awayTeam']}__{m['utcDate']}"


def load_prediction_log():
    if os.path.exists(PREDICTION_LOG_PATH):
        try:
            with open(PREDICTION_LOG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_prediction_log(log):
    os.makedirs(os.path.dirname(PREDICTION_LOG_PATH), exist_ok=True)
    with open(PREDICTION_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def update_prediction_log(all_matches):
    """Suivi de fiabilité 100% automatique : on enregistre chaque pronostic au
    moment où il est fait (avant le match), et dès que le match apparaît comme
    terminé, on compare le score réel au pronostic pour déterminer s'il était
    correct — aucune saisie manuelle nécessaire, contrairement au tableau de
    bord Coupe du Monde où c'est fait à la main. Une double chance "home" est
    correcte si l'équipe désignée gagne OU fait match nul (et inversement)."""
    log = load_prediction_log()

    # on enregistre les nouveaux pronostics (matchs à venir avec une suggestion)
    for m in all_matches:
        a = m.get("analysis")
        if not a or not a.get("predicted_side"):
            continue
        key = match_key(m)
        if key not in log:
            log[key] = {
                "competition": m["competition"],
                "homeTeam": m["homeTeam"],
                "awayTeam": m["awayTeam"],
                "kickoff": m["utcDate"],
                "predicted_side": a["predicted_side"],
                "pick_label": a["suggested_pick"],
                "confidence": a["confidence"],
                "status": "pending",
                "final_score": None,
            }

    # on évalue les pronostics en attente dont le match est maintenant terminé
    for m in all_matches:
        if m.get("status") != "FINISHED":
            continue
        key = match_key(m)
        if key in log and log[key]["status"] == "pending":
            hs, aws = m.get("homeScore"), m.get("awayScore")
            if hs is None or aws is None:
                continue
            side = log[key]["predicted_side"]
            correct = (hs >= aws) if side == "home" else (aws >= hs)
            log[key]["status"] = "correct" if correct else "incorrect"
            log[key]["final_score"] = f"{hs}-{aws}"

    save_prediction_log(log)


def main():
    if not TOKEN:
        print("ERREUR : la variable d'environnement FOOTBALL_DATA_TOKEN n'est pas définie.")
        sys.exit(1)

    raw_matches = []  # (raw_match_dict, competition_code, competition_name)
    for code, name in COMPETITIONS.items():
        for m in fetch_competition_matches(code):
            raw_matches.append((m, code, name))

    # on choisit les prochains matchs non encore joués, triés par date, pour
    # leur appliquer l'analyse statistique poussée (dans la limite fixée plus haut)
    upcoming = sorted(
        [rm for rm in raw_matches if rm[0].get("status") in ("SCHEDULED", "TIMED")],
        key=lambda rm: rm[0].get("utcDate") or ""
    )
    deep_ids = {rm[0]["id"] for rm in upcoming[:MAX_DEEP_ANALYSIS]}

    all_matches = [
        normalize(m, code, name, deep=(m.get("id") in deep_ids))
        for m, code, name in raw_matches
    ]
    all_matches.sort(key=lambda m: m["utcDate"] or "")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date_from": DATE_FROM,
        "date_to": DATE_TO,
        "count": len(all_matches),
        "deep_analysis_count": len(deep_ids),
        "matches": all_matches,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    update_prediction_log(all_matches)

    print(f"OK : {len(all_matches)} matchs écrits, dont {len(deep_ids)} avec analyse poussée -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
