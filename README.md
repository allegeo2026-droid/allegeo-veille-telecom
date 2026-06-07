# Veille ALLEGEO — Télécom (pilote)

Script quotidien qui détecte automatiquement les nouvelles offres mobile/box annoncées sur les sites spécialisés français, et alimente :
- une feuille **Veille_offres_a_venir** dans le Google Sheets ALLEGEO existant
- un **email récap quotidien** à 8h

---

## Architecture

```
Sources RSS (Univers Freebox, FrAndroid, ZoneADSL, Ariase…)
         ↓
Filtre mots-clés ("lance", "dès le", "nouveau forfait"…)
         ↓
Extraction structurée via Claude Haiku 4.5
         ↓
Google Sheets (dédup par URL) + Email HTML récap
```

**Coût API** : ~5 à 10€/an (Haiku 4.5, ~15 articles analysés/jour).

---

## Setup (45 min, une seule fois)

### 1. Créer un compte de service Google (15 min)

Le script doit pouvoir écrire dans ton Sheets sans ton mot de passe → service account.

1. Va sur **https://console.cloud.google.com**
2. Crée un projet : nom **"allegeo-veille"**
3. Active l'API Google Sheets : menu hamburger → **APIs & Services → Library** → cherche "Google Sheets API" → **Enable**
4. Crée un service account : **IAM & Admin → Service Accounts → Create Service Account**
   - Nom : `veille-allegeo`
   - Rôle : aucun rôle nécessaire au niveau projet
   - Clique **Done**
5. Génère une clé JSON : clique sur le service account → onglet **Keys → Add Key → JSON**
   - Un fichier `xxx.json` se télécharge → **garde-le précieusement**
6. **Partage ton Sheets ALLEGEO** avec l'adresse email du service account (du type `veille-allegeo@allegeo-veille.iam.gserviceaccount.com`) — droits **Éditeur**

### 2. Préparer un email SMTP pour l'envoi (10 min)

Le plus simple : **Gmail avec mot de passe d'application**.

1. Active la 2FA sur ton compte Gmail
2. Va sur **https://myaccount.google.com/apppasswords**
3. Génère un mot de passe pour "Mail" → "Autre" → "ALLEGEO veille"
4. **Note les 16 caractères** → c'est ton `SMTP_PASS`

Alternative : SMTP de ton domaine `allegeo.fr` (OVH/Wix selon ton config), même principe.

### 3. Déployer sur GitHub Actions (15 min, gratuit)

1. Crée un repo GitHub **privé** : `allegeo-veille-telecom`
2. Pousse les fichiers du dossier (`veille_telecom.py`, `sources.json`, `requirements.txt`, `.github/workflows/veille.yml`)
3. Dans le repo : **Settings → Secrets and variables → Actions → New repository secret** (un par un) :

| Nom du secret | Valeur |
|---|---|
| `ANTHROPIC_API_KEY` | ta clé Anthropic (commence par `sk-ant-`) |
| `GOOGLE_CREDENTIALS_JSON` | **tout le contenu** du fichier JSON téléchargé à l'étape 1.5 |
| `SMTP_HOST` | `smtp.gmail.com` (ou ton SMTP) |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | ton adresse email d'envoi |
| `SMTP_PASS` | le mot de passe d'app (16 caractères Gmail) |
| `EMAIL_TO` | l'adresse où tu reçois le récap |

4. **Test manuel** : onglet **Actions** → **Veille ALLEGEO Télécom** → **Run workflow** → bouton vert
   - Tu vois l'exécution en live
   - Vérifie que l'email arrive et que le Sheets est rempli

5. Le cron quotidien tourne automatiquement à 8h Paris dès le lendemain.

---

## Utilisation au quotidien

**Rien à faire.** Chaque matin à 8h :
- Tu reçois un email récap (RAS s'il n'y a rien de neuf, ou les offres détectées avec opérateur/prix/data/date de lancement)
- Le Sheets se remplit en cumulé → tu construis ta base "veille marché" historisée

**Pour ajouter une source** : édite `sources.json`, ajoute un objet avec `name` et `url` (RSS), commit, push. Pas besoin de toucher au code.

**Pour ajuster les mots-clés** : variables `KEYWORDS` et `EXCLUSIONS` en haut de `veille_telecom.py`.

---

## Extensions prévues

Une fois le pilote télécom rodé (2-3 semaines), réplication pour **énergie**, **assurance**, **crédit** :
- même squelette, juste sources.json différent par secteur
- même Sheets, onglets séparés par secteur
- email récap unifié (sections par secteur)

---

## Aspects légaux

✅ **Lecture de flux RSS publics** : autorisée par construction (les sites publient explicitement pour la consommation tierce).
✅ **Scraping HTML modéré** (1 requête / article, max ~20/jour, user-agent identifié) : tolérée tant que tu respectes les robots.txt et que tu ne republies pas le contenu intégral.
✅ **Stockage interne** des offres extraites (faits, pas de contenu rédactionnel) : aucun problème.
⚠️ **Ne republie pas** les résumés intégraux des articles sur ton site sans accord — ils restent à usage interne pour la veille client.
