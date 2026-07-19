# Sentinel — an autonomous crypto & web3 watcher agent

Sentinel wakes itself up every 3 hours, checks crypto prices for big moves,
checks Devpost for new "web3" hackathons, and (when Google Search grounding
is available) checks the live web for newly listed coins — then emails me
only when something actually happened. No dashboard to check, no button to
click. Built entirely on AWS Free Tier services, deployed by hand through
the AWS Console.

## What it watches, and how it decides what "matters"

| Check | Trigger condition | Data source |
|---|---|---|
| Crypto prices | Tracked coin moves ≥5% since the last alert | CoinGecko (free, keyless) |
| Web3 challenges | A new "web3" hackathon appears that wasn't seen before | Devpost's public listings API (free, keyless) |
| New coin listings | A coin was listed on a major exchange in the last ~48h | Gemini + Google Search grounding |

If none of the three conditions are true on a given run, Sentinel sends
nothing — that's the point of a Watcher.

## Architecture

```
 EventBridge Scheduler "MorningScheduler" (rate: every 3 hours)
              │
              ▼
      AWS Lambda "Agentlambda" (Python)
        ├─ CoinGecko API ──────────► crypto prices (no key needed)
        ├─ Devpost API ─────────────► web3 hackathon listings (no key needed)
        ├─ SSM Parameter Store ─────► Gemini API key ("/agent/geminiapi", SecureString)
        ├─ Gemini API (Google Search grounding) ─► new coin listings
        ├─ DynamoDB "Agenttable" ───► price baselines + seen-item tracking
        └─ Amazon SES ──────────────► emails the alert, only if triggered
```

IAM role `Agent` grants exactly: `ses:SendEmail`, `dynamodb:GetItem`/`PutItem`
on `Agenttable`, `ssm:GetParameter` on `/agent/*`, and CloudWatch Logs write
access.

## A known, accepted tradeoff

Google Search grounding is not included in Gemini's unbilled free tier — it
requires a Google Cloud project with billing enabled to receive its free
monthly grounding allowance. On an unbilled project, that specific check
returns a 429 and is **silently skipped** (the code wraps it in a
try/except) without affecting the crypto or web3-challenge checks. This
was a deliberate choice: rather than let one optional check take the whole
agent down, it degrades gracefully. Enabling billing on the Google Cloud
project would activate it fully.

## How it was deployed (AWS Console, no CLI)

1. **SES** — verified sender/recipient email identity
2. **SSM Parameter Store** — stored the Gemini API key as a SecureString at `/agent/geminiapi`
3. **DynamoDB** — table `Agenttable`, partition key `date` (String), on-demand billing — reused as a general key-value store with prefixed keys (`price#`, `web3challenge#`, `listing#`) rather than one table per feature
4. **IAM** — role `Agent`, inline policy scoped to exactly the permissions above
5. **Lambda** — function `Agentlambda` (Python), this repo's `lambda_function.py`, role `Agent` attached, environment variables set (below), timeout raised to 1 minute
6. **EventBridge Scheduler** — schedule `MorningScheduler`, rate-based every 3 hours, targeting `Agentlambda`

This project started as a daily 6 AM "morning brief" agent (Daybreak) and
was iteratively rebuilt into a Watcher through a series of live edits —
including debugging real deployment errors (env var typos, a wrong Gemini
model name, malformed lat/long values, and the grounding billing
requirement above) directly against a running AWS account.

## Environment variables

| Key | Example value |
|---|---|
| `TABLE_NAME` | `Agenttable` |
| `RECIPIENT_EMAIL` | you@example.com |
| `SENDER_EMAIL` | you@example.com |
| `CRYPTO_IDS` | `bitcoin,ethereum,solana` |
| `PRICE_CHANGE_THRESHOLD_PCT` | `5` |
| `GEMINI_API_KEY_PARAM` | `/agent/geminiapi` |
| `GEMINI_MODEL` | `gemini-3-flash` |

## Cost

At one invocation every 3 hours (8/day), this stays comfortably within AWS
Free Tier limits for Lambda, DynamoDB on-demand, SES, and EventBridge
Scheduler. CoinGecko and Devpost's public endpoints are free and keyless.
Gemini usage is minimal since alerts (and thus tokens) are only generated
when something's actually worth reporting.
