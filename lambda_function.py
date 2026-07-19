"""
Sentinel — an autonomous crypto & web3 watcher agent.

Wakes itself up every 3 hours on an EventBridge schedule (no button click,
no prompt) and checks three things:

1. Crypto prices: alerts when a tracked coin moves 5%+ since the last alert
   (baseline resets only when an alert fires, so slow drifts are caught
   too, not just sudden jumps between two consecutive runs).
2. Web3 challenges: alerts when a new "web3" hackathon/challenge appears on
   Devpost that wasn't there last time.
3. New coin listings: uses Gemini with Google Search grounding to check the
   live web for cryptocurrencies newly listed on major exchanges in the
   last day or two. NOTE: this check requires a billed Google Cloud
   project to receive its free grounding quota; on an unbilled project it
   will return a 429 and is silently skipped without affecting checks 1
   and 2 (deliberate, documented tradeoff — see README).

Checks 1 and 2 build alert text directly from real data — no LLM — since
prices and hackathon listings are factual/numeric and should be reported
exactly, not paraphrased. Check 3 is the only place an LLM is used, and
only to search + summarize current news, not to invent facts.

Stays silent unless something real happened — that's the whole point of a
Watcher. State (price baselines, seen challenges, seen listings) lives in
one shared DynamoDB table, keyed by prefix per check.
"""

import json
import logging
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
ses = boto3.client("ses")
ssm = boto3.client("ssm")

TABLE_NAME = os.environ["TABLE_NAME"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]
SENDER_EMAIL = os.environ["SENDER_EMAIL"]

CRYPTO_IDS = [c.strip() for c in os.environ.get("CRYPTO_IDS", "bitcoin,ethereum,solana").split(",") if c.strip()]
PRICE_CHANGE_THRESHOLD_PCT = float(os.environ.get("PRICE_CHANGE_THRESHOLD_PCT", "5"))

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash")
GEMINI_API_KEY_PARAM = os.environ.get("GEMINI_API_KEY_PARAM", "/agent/geminiapi")

PRICE_KEY_PREFIX = "price#"
CHALLENGE_KEY_PREFIX = "web3challenge#"
LISTING_KEY_PREFIX = "listing#"


def _http_get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "sentinel-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_gemini_api_key():
    resp = ssm.get_parameter(Name=GEMINI_API_KEY_PARAM, WithDecryption=True)
    return resp["Parameter"]["Value"]


# ---------------------------------------------------------------------------
# 1. Crypto price watcher
# ---------------------------------------------------------------------------

def fetch_crypto_prices():
    ids_param = ",".join(CRYPTO_IDS)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids_param}&vs_currencies=usd"
    try:
        return _http_get_json(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Crypto price fetch failed: %s", exc)
        return {}


def check_crypto_moves(table, prices):
    alerts = []
    for coin_id, data in prices.items():
        current_price = data.get("usd")
        if current_price is None:
            continue

        key = PRICE_KEY_PREFIX + coin_id
        resp = table.get_item(Key={"date": key})
        baseline = resp.get("Item", {}).get("price")

        if baseline is None:
            table.put_item(Item={"date": key, "price": Decimal(str(current_price))})
            continue

        baseline = float(baseline)
        pct_change = ((current_price - baseline) / baseline) * 100

        if abs(pct_change) >= PRICE_CHANGE_THRESHOLD_PCT:
            direction = "up" if pct_change > 0 else "down"
            alerts.append(
                f"{coin_id.capitalize()} is {direction} {abs(pct_change):.1f}% "
                f"(${baseline:,.2f} -> ${current_price:,.2f})"
            )
            table.put_item(Item={"date": key, "price": Decimal(str(current_price))})

    return alerts


# ---------------------------------------------------------------------------
# 2. Web3 challenge watcher
# ---------------------------------------------------------------------------

def fetch_web3_challenges(limit=15):
    url = "https://devpost.com/api/hackathons?search=web3&status[]=open&order_by=recently-added"
    try:
        data = _http_get_json(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Devpost fetch failed: %s", exc)
        return []

    challenges = []
    for h in data.get("hackathons", [])[:limit]:
        challenges.append({
            "id": str(h.get("id")),
            "title": h.get("title", "Untitled challenge"),
            "url": h.get("url", ""),
            "prize": h.get("prize_amount", "unspecified prize"),
        })
    return challenges


def check_new_challenges(table, challenges):
    new_ones = []
    for c in challenges:
        key = CHALLENGE_KEY_PREFIX + c["id"]
        resp = table.get_item(Key={"date": key})
        if "Item" not in resp:
            new_ones.append(c)
            table.put_item(Item={"date": key, "title": c["title"], "seen_at": datetime.now(timezone.utc).isoformat()})
    return new_ones


# ---------------------------------------------------------------------------
# 3. New coin listing watcher (Gemini + Google Search grounding)
# ---------------------------------------------------------------------------

def fetch_new_coin_listings(api_key):
    prompt = (
        "Search the web for cryptocurrencies or tokens that were newly listed "
        "on a major exchange (e.g. Binance, Coinbase, Kraken, Upbit, OKX) in "
        "roughly the last 48 hours. Only include listings you can confirm from "
        "real, current search results — do not guess or use older knowledge. "
        "If you find none, return an empty JSON array.\n\n"
        "Respond with ONLY a JSON array, no markdown fences, no other text, "
        "in this exact shape:\n"
        '[{"ticker": "XYZ", "name": "Example Token", "exchange": "Binance", '
        '"note": "one short plain sentence"}]'
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": 800},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        logger.error("Gemini API error %s: %s", exc.code, body)
        return []

    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "\n".join(p.get("text", "") for p in parts if "text" in p).strip()
    except (KeyError, IndexError):
        logger.warning("Unexpected Gemini response shape for listings check.")
        return []

    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()

    try:
        listings = json.loads(text)
        return listings if isinstance(listings, list) else []
    except json.JSONDecodeError:
        logger.warning("Could not parse Gemini listings JSON: %s", text[:300])
        return []


def check_new_listings(table, listings):
    new_ones = []
    for item in listings:
        ticker = str(item.get("ticker", "")).strip().upper()
        exchange = str(item.get("exchange", "")).strip().lower()
        if not ticker or not exchange:
            continue
        key = LISTING_KEY_PREFIX + f"{ticker}-{exchange}"
        resp = table.get_item(Key={"date": key})
        if "Item" not in resp:
            new_ones.append(item)
            table.put_item(Item={"date": key, "seen_at": datetime.now(timezone.utc).isoformat()})
    return new_ones


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_alert(crypto_alerts, new_challenges, new_listings):
    lines = []
    if crypto_alerts:
        lines.append("CRYPTO PRICE MOVES:")
        lines.extend(f"- {line}" for line in crypto_alerts)
        lines.append("")
    if new_listings:
        lines.append("NEW COIN LISTINGS:")
        for item in new_listings:
            lines.append(
                f"- {item.get('name', item.get('ticker'))} ({item.get('ticker')}) "
                f"on {item.get('exchange')} — {item.get('note', '')}"
            )
        lines.append("")
    if new_challenges:
        lines.append("NEW WEB3 CHALLENGES:")
        for c in new_challenges:
            lines.append(f"- {c['title']} (prize: {c['prize']}) — {c['url']}")

    body_text = "\n".join(lines)
    subject_bits = []
    if crypto_alerts:
        subject_bits.append(f"{len(crypto_alerts)} price move(s)")
    if new_listings:
        subject_bits.append(f"{len(new_listings)} new listing(s)")
    if new_challenges:
        subject_bits.append(f"{len(new_challenges)} new web3 challenge(s)")
    subject = "Sentinel alert — " + " & ".join(subject_bits)

    ses.send_email(
        Source=f"Sentinel <{SENDER_EMAIL}>",
        Destination={"ToAddresses": [RECIPIENT_EMAIL]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body_text, "Charset": "UTF-8"}},
        },
    )


def lambda_handler(event, context):
    table = dynamodb.Table(TABLE_NAME)

    prices = fetch_crypto_prices()
    crypto_alerts = check_crypto_moves(table, prices) if prices else []

    challenges = fetch_web3_challenges()
    new_challenges = check_new_challenges(table, challenges) if challenges else []

    new_listings = []
    try:
        api_key = get_gemini_api_key()
        raw_listings = fetch_new_coin_listings(api_key)
        new_listings = check_new_listings(table, raw_listings) if raw_listings else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("New-listing check failed, continuing without it: %s", exc)

    if not crypto_alerts and not new_challenges and not new_listings:
        logger.info("Nothing worth alerting on this run.")
        return {"status": "quiet"}

    send_alert(crypto_alerts, new_challenges, new_listings)
    logger.info(
        "Sent alert: %d price moves, %d new challenges, %d new listings.",
        len(crypto_alerts), len(new_challenges), len(new_listings),
    )
    return {
        "status": "alert_sent",
        "price_moves": len(crypto_alerts),
        "new_challenges": len(new_challenges),
        "new_listings": len(new_listings),
    }
