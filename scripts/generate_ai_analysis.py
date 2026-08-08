#!/usr/bin/env python3
"""
Génère une analyse qualitative approfondie (façon Claude) pour les prochains
matchs, en un seul appel groupé via l'API Batch d'Anthropic avec le modèle
Haiku 4.5 — la combinaison la moins chère possible :

  - Haiku 4.5 plutôt que Sonnet/Opus (le moins cher par token)
  - UN SEUL appel contenant tous les matchs (pas un appel par match : on ne
    paie les instructions/méthodologie qu'une seule fois)
  - API Batch (-50% sur le prix normal), asynchrone : le script soumet le lot
    puis attend la fin du traitement (généralement quelques minutes pour un
    aussi petit volume)

Coût réel estimé : de l'ordre de 0,20 à 0,80 $/mois pour analyser TOUS les
matchs des 5 grands championnats + Ligue des Champions + Coupe du Monde,
une fois par semaine. Reste TOUJOURS sous le plafond de dépense que tu as
configuré dans la Console Anthropic — configure-le avant de lancer ceci.

Ce script lit data/matches.json (déjà rempli par fetch_matches.py, gratuit,
sans IA) et écrit data/ai_analysis.json séparément, pour ne jamais entrer en
conflit avec les mises à jour automatiques du calendrier toutes les 6h.

Variables d'environnement requises :
    ANTHROPIC_API_KEY   ta clé API (Console Anthropic -> API Keys)

Lancement local pour tester :
    export ANTHROPIC_API_KEY="ta_cle"
    python3 scripts/generate_ai_analysis.py
"""

import json
import os
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# Le modèle le moins cher disponible, largement suffisant pour une analyse
# structurée à partir de statistiques déjà calculées (pas de génération
# créative complexe nécessaire ici).
MODEL = "claude-haiku-4-5-20251001"

MAX_TOKENS_PER_MATCH = 350   # limite la longueur de sortie -> limite le coût
POLL_INTERVAL_SECONDS = 20
MAX_POLL_MINUTES = 30

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MATCHES_PATH = os.path.join(DATA_DIR, "matches.json")
OUTPUT_PATH = os.path.join(DATA_DIR, "ai_analysis.json")

SYSTEM_PROMPT = """Tu es un analyste de paris sportifs expérimenté. Pour chaque match fourni,
rédige une analyse en français basée UNIQUEMENT sur les statistiques données (ne pas inventer de
statistiques absentes).

RÈGLE ABSOLUE ET NON NÉGOCIABLE SUR LE TERRAIN — à respecter avant toute autre considération :
Chaque match fournit un champ "terrain_note" qui indique la VÉRITÉ VÉRIFIÉE sur qui joue à domicile
pour CE match précis. Cette vérité prime totalement sur ta connaissance générale du football (ex. le
fait de "savoir" qu'une équipe joue habituellement à domicile dans son championnat national n'a AUCUNE
pertinence ici si le match a lieu ailleurs — Coupe du Monde sur un site neutre, finale sur terrain
neutre, etc.). Si "terrain_note" dit que c'est neutre, tu DOIS écrire explicitement que le terrain est
neutre et ne JAMAIS mentionner un avantage du terrain pour une des deux équipes, même en passant, même
comme facteur secondaire. Si tu hésites entre ta connaissance générale et la donnée fournie, la donnée
fournie a TOUJOURS raison.

Méthodologie obligatoire pour le reste de l'analyse :
- Le pari conseillé doit TOUJOURS être une double chance (équipe A ou nul, ou équipe B ou nul) —
  jamais une victoire sèche.
- Prends en compte : forme récente (5 derniers matchs), buts marqués/encaissés, taux d'invincibilité
  et série en cours spécifiquement à domicile (pour l'équipe recevante) et à l'extérieur (pour
  l'équipe visiteuse) sur les 10 derniers matchs dans ce contexte, historique des confrontations
  directes, fatigue/enchaînement de matchs (un repos de 3 jours ou moins est un vrai facteur
  défavorable, surtout en période de chevauchement championnat/coupes/Ligue des Champions),
  l'effet de relâchement possible après une victoire marquante à l'extérieur (facteur à mentionner
  s'il est signalé, sans jamais le traiter comme une certitude), l'enjeu réel du match pour
  chaque équipe (course à la Ligue des Champions, lutte pour le maintien, ou déjà sans grand enjeu),
  et le style de jeu approximatif de chaque équipe ("home_style_tag"/"away_style_tag", déduit des
  buts marqués/encaissés — match ouvert probable si les deux styles sont offensifs/friables,
  match fermé probable si les deux sont défensifs).
- Le champ "poisson_model_probabilities" (quand présent) donne de VRAIES probabilités statistiques
  (victoire/nul/défaite, plus de 2,5 buts, les deux équipes marquent) calculées par un modèle de
  Poisson à partir des taux de buts réels — utilise ces pourcentages pour appuyer ton analyse et,
  si pertinent, mentionne la probabilité "plus de 2,5 buts" ou "BTTS" comme option de pari
  complémentaire à la double chance.
- Ne favorise pas systématiquement le favori statistique : si les données suggèrent un match plus
  équilibré qu'il n'y paraît, ou un vrai risque de surprise, dis-le clairement. Le football produit
  des résultats surprenants ; une analyse honnête l'admet plutôt que de forcer un pronostic.
- Sois concis mais concret : 3 à 5 phrases d'analyse par match, qui justifient le choix.

Réponds UNIQUEMENT avec un tableau JSON valide, sans texte avant/après, sans balises markdown,
au format exact :
[{"key": "...", "pick": "Double chance ...", "confidence": "Faible|Moyenne|Élevée", "analysis": "..."}]

Le champ "key" doit être recopié EXACTEMENT tel que fourni pour chaque match."""


def api_request(path, method="GET", body=None):
    url = f"{API_BASE}{path}"
    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        print(f"[erreur API] {method} {path} -> HTTP {e.code} : {e.read().decode('utf-8', 'ignore')}")
        return None


def build_match_payload(matches):
    """Construit la liste compacte des matchs + stats à envoyer au modèle."""
    payload = []
    for m in matches:
        a = m["analysis"]
        key = f"{m['homeTeam']}__{m['awayTeam']}__{m['utcDate']}"

        # Note en langage naturel, explicite et impossible à mal interpréter,
        # plutôt que de compter sur le modèle pour bien lire un champ technique
        # true_home_advantage isolé au milieu des données. Le modèle a souvent
        # une connaissance générale du football (ex. "l'Argentine joue tel jour")
        # qui peut le pousser à halluciner un avantage du terrain même si la
        # donnée dit le contraire — cette note prime explicitement sur tout ça.
        adv = a.get("true_home_advantage")
        if adv == "home":
            terrain_note = (f"{m['homeTeam']} joue RÉELLEMENT à domicile pour ce match "
                             f"(avantage du terrain réel, vérifié à partir du lieu exact de la rencontre).")
        elif adv == "away":
            terrain_note = (f"{m['awayTeam']} joue RÉELLEMENT à domicile pour ce match, bien qu'il soit "
                             f"désigné comme 'équipe extérieure' dans les données brutes — c'est cette équipe "
                             f"qui bénéficie du vrai avantage du terrain, pas l'autre.")
        else:
            terrain_note = (f"TERRAIN NEUTRE — ni {m['homeTeam']} ni {m['awayTeam']} ne joue à domicile pour "
                             f"ce match (ex. Coupe du Monde hors nation hôte, ou finale sur stade neutre). "
                             f"N'écris JAMAIS qu'une des deux équipes a un avantage du terrain ou joue "
                             f"'à domicile' dans ton analyse de ce match précis, même si tu sais que l'une "
                             f"des deux joue habituellement à domicile dans son propre championnat.")

        payload.append({
            "key": key,
            "competition": m["competition"],
            "home_team": m["homeTeam"],
            "away_team": m["awayTeam"],
            "kickoff_utc": m["utcDate"],
            "terrain_note": terrain_note,
            "home_form_last5": a.get("home_form"),
            "away_form_last5": a.get("away_form"),
            "head_to_head": a.get("head_to_head"),
            "rest_days_home": a.get("rest_days_home"),
            "rest_days_away": a.get("rest_days_away"),
            "fatigue_home": a.get("fatigue_home"),
            "fatigue_away": a.get("fatigue_away"),
            "letdown_effect_home": a.get("letdown_home"),
            "letdown_effect_away": a.get("letdown_away"),
            "home_unbeaten_record_at_home_last10": a.get("home_venue_record"),
            "away_unbeaten_record_away_last10": a.get("away_venue_record"),
            "stakes_home": a.get("stakes_home"),
            "stakes_away": a.get("stakes_away"),
            "home_style_tag": (a.get("home_form") or {}).get("style_tag"),
            "away_style_tag": (a.get("away_form") or {}).get("style_tag"),
            "poisson_model_probabilities": a.get("poisson"),
        })
    return payload


def submit_batch(matches_payload):
    user_content = (
        "Voici les matchs à analyser (données JSON) :\n\n"
        + json.dumps(matches_payload, ensure_ascii=False)
    )
    body = {
        "requests": [
            {
                "custom_id": "weekly-analysis",
                "params": {
                    "model": MODEL,
                    "max_tokens": MAX_TOKENS_PER_MATCH * max(len(matches_payload), 1),
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_content}],
                },
            }
        ]
    }
    result = api_request("/messages/batches", method="POST", body=body)
    if not result or "id" not in result:
        print("ERREUR : impossible de soumettre le lot à l'API Batch.")
        sys.exit(1)
    print(f"Lot soumis : {result['id']} (statut : {result.get('processing_status')})")
    return result["id"]


def wait_for_batch(batch_id):
    waited = 0
    max_seconds = MAX_POLL_MINUTES * 60
    while waited < max_seconds:
        status = api_request(f"/messages/batches/{batch_id}")
        if not status:
            sys.exit(1)
        state = status.get("processing_status")
        print(f"  ... statut du lot : {state} ({waited}s écoulées)")
        if state == "ended":
            return status
        time.sleep(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS
    print(f"ATTENTION : le lot n'est pas terminé après {MAX_POLL_MINUTES} minutes. "
          f"Nouvelle tentative au prochain passage programmé.")
    sys.exit(0)  # on ne fait pas échouer le job : on réessaiera à la prochaine exécution


def fetch_batch_results(results_url):
    headers = {"x-api-key": API_KEY, "anthropic-version": ANTHROPIC_VERSION}
    req = Request(results_url, headers=headers)
    with urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    # Le résultat est au format JSONL (une ligne JSON par requête du lot)
    lines = [json.loads(line) for line in raw.strip().split("\n") if line.strip()]
    return lines


def extract_text(result_line):
    result = result_line.get("result", {})
    if result.get("type") != "succeeded":
        print(f"[attention] Requête {result_line.get('custom_id')} : {result.get('type')}")
        return None
    content_blocks = result["message"]["content"]
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    return text


def parse_model_json(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"ERREUR : réponse du modèle non-JSON valide : {e}")
        print(cleaned[:500])
        return []


def main():
    if not API_KEY:
        print("ERREUR : la variable d'environnement ANTHROPIC_API_KEY n'est pas définie.")
        sys.exit(1)

    if not os.path.exists(MATCHES_PATH):
        print("ERREUR : data/matches.json introuvable — lance d'abord fetch_matches.py.")
        sys.exit(1)

    with open(MATCHES_PATH, encoding="utf-8") as f:
        data = json.load(f)

    candidates = [m for m in data.get("matches", [])
                  if m.get("analysis") and m.get("status") in ("SCHEDULED", "TIMED")]

    if not candidates:
        print("Aucun match à analyser pour l'instant (rien de programmé avec assez de données).")
        # on écrit quand même un fichier vide/à jour pour que le site sache qu'on est passé
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump({"generated_at": None, "analyses": {}}, f, ensure_ascii=False, indent=2)
        return

    print(f"{len(candidates)} match(s) à analyser via l'API Batch (modèle {MODEL})...")
    matches_payload = build_match_payload(candidates)

    batch_id = submit_batch(matches_payload)
    status = wait_for_batch(batch_id)

    results_url = status.get("results_url")
    if not results_url:
        print("ERREUR : pas d'URL de résultats fournie par l'API.")
        sys.exit(1)

    lines = fetch_batch_results(results_url)
    analyses_by_key = {}
    for line in lines:
        text = extract_text(line)
        if not text:
            continue
        parsed = parse_model_json(text)
        for item in parsed:
            key = item.get("key")
            if key:
                analyses_by_key[key] = {
                    "pick": item.get("pick"),
                    "confidence": item.get("confidence"),
                    "analysis": item.get("analysis"),
                }

    output = {
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "analyses": analyses_by_key,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"OK : {len(analyses_by_key)} analyse(s) IA écrite(s) -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
