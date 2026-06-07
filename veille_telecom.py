"""
ALLEGEO — Veille offres télécom à venir
========================================
Scrape les sites spécialisés télécom (RSS), détecte les annonces
d'offres à venir, extrait les détails via Claude API, écrit dans
Google Sheets et envoie un email récap.

Conçu pour tourner 1×/jour via GitHub Actions.
"""

import os
import json
import time
import datetime as dt
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from anthropic import Anthropic


# ─── CONFIG ───────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.json"

# ID du Google Sheets ALLEGEO existant
SHEET_ID = "1ZZSblRrnpc97pc2qu8cvdfKXitSDkwKZjdt4dLFhZv0"
SHEET_TAB = "Veille_offres_a_venir"

# Marqueurs d'annonce d'une nouvelle offre / offre future
KEYWORDS = [
    "nouveau forfait", "nouvelle offre", "nouvelle box", "nouvelle série",
    "lance", "lancement", "lancera",
    "dès le", "à partir du", "disponible le", "disponible dès",
    "annonce", "annoncé", "annoncera", "dévoile", "dévoilé",
    "prochainement", "à venir", "bientôt",
    "promo", "promotion", "offre spéciale", "vente flash",
    "baisse", "baisse de prix", "augmentation",
]

# Exclusions (articles non pertinents même s'ils contiennent un mot-clé)
EXCLUSIONS = [
    "rétrospective", "bilan de", "top 10", "top 5",
    "comparatif des meilleur", "meilleurs forfaits du mois",
    "test ", "review", "avis sur",
    "histoire de", "rumeur",
]

# Modèle Claude (haiku = rapide + bon marché pour de l'extraction)
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# ─── SECRETS (env vars) ───────────────────────────────────────────────
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
EMAIL_TO = os.environ.get("EMAIL_TO", SMTP_USER)


# ─── HELPERS ──────────────────────────────────────────────────────────
def load_sources():
    return json.loads(SOURCES_FILE.read_text(encoding="utf-8"))


def is_candidate(title, summary):
    """Filtre rapide par mot-clé avant d'appeler l'API."""
    text = f"{title} {summary}".lower()
    if any(excl in text for excl in EXCLUSIONS):
        return False
    return any(kw in text for kw in KEYWORDS)


def fetch_article_text(url, max_chars=4500):
    """Récupère le contenu principal d'un article."""
    try:
        r = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                )
            },
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        article = soup.find("article") or soup.find("main") or soup.body
        text = article.get_text(separator="\n", strip=True) if article else ""
        return text[:max_chars]
    except Exception as e:
        print(f"    ⚠ Erreur fetch ({type(e).__name__}) : {e}")
        return ""


def extract_offer(client, title, content, url, source):
    """Demande à Claude d'extraire les détails. Retourne None si pas une offre."""
    prompt = f"""Tu analyses un article d'actualité télécom français.

Détermine s'il annonce une **nouvelle offre commerciale grand public** (forfait mobile, box internet, promotion, lancement) avec des détails concrets (opérateur, prix ou data, date).

NE RETOURNE PAS d'offre si l'article est :
- un comparatif général / classement / top X
- un test, avis, review
- une actualité corporate sans offre commerciale (résultats financiers, rachat, etc.)
- une simple rumeur sans détails tarifaires
- déjà ancien (offre lancée il y a plus de 2 semaines)

Retourne UNIQUEMENT du JSON valide, rien d'autre, pas de markdown, pas de ``` :

{{"is_offer": true ou false, "operateur": "Free / Orange / SFR / Bouygues / Sosh / Red / B&You / Prixtel / autre", "type": "mobile" ou "box" ou "convergent", "nom_offre": "nom commercial", "prix_mensuel_eur": nombre ou null, "data_go": nombre ou null, "debit": "fibre 1 Gbit/s par ex, ou null", "engagement_mois": nombre ou null, "date_lancement": "YYYY-MM-DD ou texte si flou", "promotion": "détail promo si applicable", "resume": "1 phrase factuelle"}}

Si is_offer=false, remplis seulement is_offer.

TITRE : {title}
SOURCE : {source}
URL : {url}

CONTENU :
{content}"""

    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        if data.get("is_offer"):
            return data
        return None
    except json.JSONDecodeError as e:
        print(f"    ⚠ JSON invalide : {e}")
        return None
    except Exception as e:
        print(f"    ⚠ Erreur extraction ({type(e).__name__}) : {e}")
        return None


def get_sheet_client():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)


def get_or_create_worksheet(gc):
    sh = gc.open_by_key(SHEET_ID)
    try:
        return sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB, rows=2000, cols=14)
        ws.append_row(
            [
                "Date détection", "Opérateur", "Type", "Nom offre",
                "Prix €/mois", "Data Go", "Débit", "Engagement (mois)",
                "Date lancement", "Promotion", "Résumé", "Source", "URL",
            ]
        )
        ws.format("A1:M1", {"textFormat": {"bold": True}})
        return ws


def get_existing_urls(ws):
    """URLs déjà trackées dans le Sheet (col M = 13e)."""
    try:
        urls = ws.col_values(13)
        return set(urls[1:])  # skip header
    except Exception:
        return set()


def append_offers(ws, offers):
    if not offers:
        return
    today = dt.date.today().isoformat()
    rows = [
        [
            today,
            o.get("operateur", ""),
            o.get("type", ""),
            o.get("nom_offre", ""),
            o.get("prix_mensuel_eur"),
            o.get("data_go"),
            o.get("debit", "") or "",
            o.get("engagement_mois"),
            o.get("date_lancement", "") or "",
            o.get("promotion", "") or "",
            o.get("resume", "") or "",
            o["_source"],
            o["_url"],
        ]
        for o in offers
    ]
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def build_email_html(offers):
    today = dt.date.today().strftime("%d/%m/%Y")
    if not offers:
        return (
            f"📡 ALLEGEO veille télécom — RAS ({today})",
            f"""<div style="font-family:Inter,Arial,sans-serif;color:#1a1a1a;max-width:600px">
            <h2 style="color:#0C7024;border-bottom:2px solid #0C7024;padding-bottom:8px">
              Veille télécom · {today}
            </h2>
            <p>Aucune nouvelle offre détectée aujourd'hui.</p>
            <p style="color:#666;font-size:13px;margin-top:32px">
              ALLEGEO · veille automatisée quotidienne
            </p></div>""",
        )

    subject = f"📡 ALLEGEO veille télécom — {len(offers)} offre(s) détectée(s) ({today})"

    cards = []
    for o in offers:
        details = []
        if o.get("prix_mensuel_eur") is not None:
            details.append(f"<strong>{o['prix_mensuel_eur']}€/mois</strong>")
        if o.get("data_go"):
            details.append(f"{o['data_go']} Go")
        if o.get("debit"):
            details.append(f"débit {o['debit']}")
        if o.get("engagement_mois") is not None:
            eng = o["engagement_mois"]
            details.append(f"engagement {eng} mois" if eng else "sans engagement")

        date_html = (
            f'<div style="color:#0C7024;font-weight:600;margin:4px 0">📅 Lancement : {o["date_lancement"]}</div>'
            if o.get("date_lancement")
            else ""
        )
        promo_html = (
            f'<div style="background:#FEF3C7;padding:6px 10px;border-radius:4px;margin:6px 0;font-size:14px">💸 {o["promotion"]}</div>'
            if o.get("promotion")
            else ""
        )

        cards.append(
            f"""<div style="border:1px solid #E2E8F0;border-radius:8px;padding:16px;margin:12px 0">
              <div style="font-size:18px;font-weight:700;color:#0C7024">
                {o.get('operateur', '?')} · {o.get('nom_offre', '')}
              </div>
              <div style="color:#475569;margin:6px 0;font-size:14px">{' · '.join(details)}</div>
              {date_html}
              {promo_html}
              <p style="margin:8px 0;color:#1a1a1a">{o.get('resume', '')}</p>
              <a href="{o['_url']}" style="color:#0C7024;font-size:13px">→ {o['_source']}</a>
            </div>"""
        )

    body = f"""<div style="font-family:Inter,Arial,sans-serif;color:#1a1a1a;max-width:640px">
      <h2 style="color:#0C7024;border-bottom:2px solid #0C7024;padding-bottom:8px">
        Veille télécom · {today}
      </h2>
      <p><strong>{len(offers)}</strong> nouvelle(s) offre(s) détectée(s) ce matin.</p>
      {''.join(cards)}
      <p style="color:#666;font-size:13px;margin-top:32px">
        Toutes les offres sont aussi enregistrées dans <a href="https://docs.google.com/spreadsheets/d/{SHEET_ID}">le Google Sheets ALLEGEO</a>.<br>
        ALLEGEO · veille automatisée quotidienne
      </p></div>"""

    return subject, body


def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


# ─── MAIN ─────────────────────────────────────────────────────────────
def run():
    start = dt.datetime.now()
    print(f"=== Veille ALLEGEO télécom — {start.isoformat(timespec='seconds')} ===\n")

    sources = load_sources()
    client = Anthropic(api_key=ANTHROPIC_KEY)
    gc = get_sheet_client()
    ws = get_or_create_worksheet(gc)
    existing_urls = get_existing_urls(ws)
    print(f"📋 {len(existing_urls)} URLs déjà trackées dans le Sheet\n")

    found = []
    candidates_count = 0

    for src in sources:
        print(f"→ {src['name']}")
        try:
            feed = feedparser.parse(src["url"])
        except Exception as e:
            print(f"  ⚠ Impossible de lire le flux : {e}\n")
            continue

        if not feed.entries:
            print(f"  ⚠ Flux vide ou inaccessible\n")
            continue

        for entry in feed.entries[:25]:  # 25 plus récents
            url = entry.get("link")
            if not url or url in existing_urls:
                continue
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            if not is_candidate(title, summary):
                continue
            candidates_count += 1
            print(f"  ✓ Candidat : {title[:80]}")
            content = fetch_article_text(url) or summary
            offer = extract_offer(client, title, content, url, src["name"])
            if offer:
                offer["_source"] = src["name"]
                offer["_url"] = url
                found.append(offer)
                print(f"    🎯 OFFRE : {offer.get('operateur')} · {offer.get('nom_offre')}")
            time.sleep(1.5)  # politesse côté serveurs sources
        print()

    print(f"=== Bilan : {candidates_count} candidat(s) analysé(s), {len(found)} offre(s) retenue(s) ===\n")

    if found:
        append_offers(ws, found)
        print("✓ Google Sheets mis à jour")

    subject, body = build_email_html(found)
    send_email(subject, body)
    print(f"✓ Email envoyé à {EMAIL_TO}")

    elapsed = (dt.datetime.now() - start).total_seconds()
    print(f"\nDurée totale : {elapsed:.1f}s")


if __name__ == "__main__":
    run()
