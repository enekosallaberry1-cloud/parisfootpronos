# Calendrier + analyse automatique (stats + IA) de foot — installation

Ce projet a maintenant DEUX niveaux, tous les deux automatiques, qui tournent
indépendamment :

1. **Calendrier + résultats + stats brutes** (gratuit, toutes les 6h) — forme sur
   10 matchs (lissée), buts, confrontations directes, invincibilité domicile/
   extérieur, fatigue, effet de relâchement, enjeux de classement, style de jeu
   approximatif, **et un vrai modèle de probabilités (loi de Poisson)** donnant
   les vraies % de victoire/nul/défaite, plus de 2,5 buts, et BTTS.
2. **Analyse qualitative approfondie par l'IA Claude** (~0,20 à 1$/mois, une
   fois par semaine) — un vrai texte d'analyse (façon Claude), basé sur toutes
   les statistiques du point 1, qui respecte la méthodologie complète.
3. **Suivi de fiabilité 100% automatique** — dès qu'un match programmé reçoit
   un pronostic, il est enregistré ; dès que le match est terminé, le script
   compare le score réel au pronostic et calcule si c'était correct, sans
   aucune action de ta part. Le taux de réussite réel s'affiche sur le site.
4. **Recherche par équipe** et **export calendrier (.ics)** par match, pour
   suivre facilement les affiches qui t'intéressent.
5. **Compétitions bonus** (gratuit, 2 fois par jour) — Supercoupe de l'UEFA,
   Supercoupe d'Allemagne, Supercoupe d'Espagne, Coupe d'Espagne, Coupe
   d'Italie, Coupe de France, Trophée des Champions, Ligue des Nations. Via
   une deuxième API (API-Football), avec une analyse plus légère que le reste
   (pas d'invincibilité ni de modèle Poisson, pour respecter un quota de
   100 requêtes/jour) mais un vrai pronostic double chance quand même.

## Étape 1 — Créer ta clé API gratuite football-data.org (2 minutes)

1. Va sur https://www.football-data.org/client/register
2. Crée un compte gratuit (email + mot de passe, pas de carte bancaire)
3. Copie ta clé API sur ton tableau de bord

## Étape 2 — Créer ta clé API Claude et configurer un plafond (5 minutes)

1. Va sur https://console.anthropic.com et crée un compte
2. Ajoute un moyen de paiement et achète un premier lot de crédits (10$ est largement suffisant, voir les calculs de coût plus bas)
3. **Avant toute chose**, va dans Settings → Billing (ou Limits) et configure un
   **plafond de dépense mensuel** (recommandé : 5-10$) avec une alerte email à 80% —
   ça garantit que même en cas de bug, la dépense ne peut jamais s'emballer
4. Va dans API Keys, crée une nouvelle clé, et copie-la (elle ne sera plus affichée ensuite)

## Étape 2bis — Créer ta clé API-Football (pour les compétitions bonus, 3 minutes)

1. Va sur https://www.api-football.com/, crée un compte gratuit (ou passe par RapidAPI si tu préfères)
2. Récupère ta clé API sur ton tableau de bord ("API-KEY")
3. Aucune carte bancaire requise pour le plan gratuit (100 requêtes/jour)

## Étape 3 — Créer ton dépôt GitHub (5 minutes)

1. Crée un compte gratuit sur https://github.com si tu n'en as pas
2. Clique sur "New repository", donne-lui un nom (ex. `mon-calendrier-foot`), coche "Public", puis "Create repository"
3. Sur la page de ton nouveau dépôt vide, clique sur "uploading an existing file"
4. Glisse-dépose **tous les fichiers et dossiers de ce projet** (en gardant la structure) puis clique "Commit changes"

## Étape 4 — Ajouter tes deux clés en secrets GitHub

1. Dans ton dépôt, va dans **Settings** → **Secrets and variables** → **Actions**
2. Clique sur **New repository secret**, crée :
   - `FOOTBALL_DATA_TOKEN` = ta clé football-data.org
   - `ANTHROPIC_API_KEY` = ta clé Claude API
   - `API_FOOTBALL_KEY` = ta clé API-Football (étape 2bis)

## Étape 5 — Activer GitHub Pages

1. **Settings** → **Pages**
2. Source : **Deploy from a branch**, branche `main`, dossier `/ (root)`, **Save**
3. Après une minute ou deux, ton site est en ligne à
   `https://ton-pseudo.github.io/mon-calendrier-foot/`

## Étape 6 — Lancer les deux robots une première fois

1. Onglet **Actions** → workflow **"Mise à jour des matchs"** → **Run workflow**
   (attends ~1-2 minutes selon le nombre de matchs à venir)
2. Onglet **Actions** → workflow **"Analyse IA hebdomadaire (Claude API)"** →
   **Run workflow** (attends quelques minutes, le temps que l'API Batch traite la demande)
3. Onglet **Actions** → workflow **"Compétitions bonus (supercoupes, coupes, Ligue des Nations)"** →
   **Run workflow** (celui-ci est plus récent que les autres — regarde les logs
   au premier essai, il est possible qu'un ajustement soit nécessaire)
4. Va sur ton site : les matchs et les analyses IA doivent apparaître

## Calcul détaillé du coût de l'analyse IA

- Modèle utilisé : Haiku 4.5 (le moins cher : 1$/5$ par million de tokens en entrée/sortie)
- Un seul appel groupé par semaine pour tous les matchs à venir (pas un appel par match)
- Volume réaliste sur une saison complète (5 grands championnats + Ligue des Champions,
  hors Coupe du Monde qui est quadriennale) : environ 1950 matchs/saison sur 10 mois actifs
- Coût brut estimé pour la saison : ~6,40$ ; avec l'API Batch (-50%, déjà utilisée par ce
  script) ça descend autour de 3,20$ pour la saison
- **Résultat réaliste : environ 0,30 à 1$/mois**, largement sous le plafond de sécurité
  configuré à l'étape 2

## Et ensuite ?

- Le calendrier/stats se met à jour tout seul toutes les 6h, gratuitement, pour toujours
- L'analyse IA se régénère tout seule chaque lundi matin, pour quelques centimes
- Si tu veux changer la fréquence, modifie les lignes `cron:` dans les deux fichiers
  `.github/workflows/*.yml`
- Pour ajuster la méthodologie de l'IA (facteurs pris en compte, longueur des analyses),
  modifie `SYSTEM_PROMPT` dans `scripts/generate_ai_analysis.py`
- Reviens me voir dans Claude à tout moment pour affiner un match précis en profondeur,
  ou pour ajuster la formule statistique / le prompt IA

## Limites à connaître

- Le plan gratuit football-data.org autorise 10 requêtes/minute — le script fait une
  pause de 7 secondes entre CHAQUE appel, donc aucun risque de dépassement.
- L'analyse statistique poussée (forme + H2H + invincibilité) n'est calculée que pour
  les 20 prochains matchs à venir maximum (`MAX_DEEP_ANALYSIS`), pour limiter la durée
  du job. Les autres matchs affichent juste le calendrier/résultat.
- L'analyse IA utilise l'API Batch d'Anthropic (asynchrone) : le script attend jusqu'à
  30 minutes que le lot soit traité. Si ce n'est pas terminé à temps, il s'arrête
  proprement sans erreur et réessaiera automatiquement la semaine suivante.
- Le plafond de dépense configuré à l'étape 2 est ta vraie protection contre tout
  dérapage — configure-le avant de lancer quoi que ce soit.
- Le modèle Poisson est une version simplifiée (pas de normalisation par la
  moyenne du championnat comme le ferait un modèle professionnel) — c'est une
  estimation basée sur 10 matchs, pas une certitude. Plus l'échantillon est
  petit (ex. sélections nationales qui jouent peu), moins l'estimation est fiable.
- Le style de jeu affiché est une étiquette approximative déduite uniquement des
  buts marqués/encaissés — pas une vraie analyse tactique (formations, possession,
  etc.), qui nécessiterait une source de données payante.
- Le fichier `data/prediction_log.json` grossit avec le temps (un peu partout
  entre quelques Ko et quelques Mo par an) — sans conséquence pratique à moyen
  terme, mais à surveiller sur plusieurs années.
- Si football-data.org ou l'API Anthropic changent leurs conditions un jour, reviens
  me voir pour adapter les scripts.
