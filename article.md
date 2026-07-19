# Weekend Agent Challenge: Sentinel

**Tags:** #agents

## Vision & What the Agent Does

Crypto markets and web3 hackathons both move fast, and both are the kind of
thing you only need to know about *when something's actually happening* —
not every hour, and not through five different apps. Sentinel is an agent
that watches both quietly in the background and only interrupts me when
there's something real to say.

It wakes itself up every three hours, with nobody clicking anything. On
each run it checks three things: whether any of my tracked cryptocurrencies
(Bitcoin, Ethereum, Solana) has moved 5% or more since the last time it
alerted me, whether a new "web3" hackathon has appeared on Devpost that
wasn't there before, and — when available — whether any coin was newly
listed on a major exchange in the last couple of days, using Gemini's live
web-search capability rather than stale training data. If none of those
three things are true, it does nothing at all. That silence is the feature:
a watcher that pings you constantly is just noise with extra steps. When
something does clear the bar, it emails me a plain-text summary, and that's
sitting in my inbox whenever I next check it.

## How it was Built

This project didn't start as Sentinel — it started as a completely
different agent, a 6 AM "morning brief" that summarized weather and tech
headlines. I built that first, end to end, deployed entirely by hand
through the AWS Console rather than infrastructure-as-code, which turned
out to be a genuinely good way to *learn* the services rather than just
invoke them: SES identity verification, SSM Parameter Store for secrets,
DynamoDB table creation, IAM role and inline policy authoring, Lambda
configuration, and EventBridge Scheduler cron syntax — one screen at a
time, with real errors along the way (a mixed-up Key/Value pair in the
Lambda console, a stray `°N`/`°E` in a coordinate field, a model name that
had quietly been deprecated for new accounts).

Once that foundation was solid, I repurposed it rather than starting over.
The DynamoDB table, IAM role, and SES identities didn't need to change at
all — only the Lambda's code and the schedule's frequency did. That's a
useful lesson in itself: most of an agent's *infrastructure* is reusable
scaffolding, and swapping its *purpose* is often just a code change plus a
few new environment variables.

The trickiest real bug was a genuine production issue: Google's Search
grounding tool, which lets Gemini check the live web instead of relying on
its training data, turned out to require a billed Google Cloud project to
receive any free quota at all — an unbilled key returns a 429 no matter how
little you've used it. Rather than let that one feature take down the
whole agent, I wrapped it in its own try/except so a grounding failure
degrades gracefully: the crypto and hackathon checks keep working
regardless, and the listings feature simply sits dormant until billing is
enabled.

## AWS Services Used / Architecture Overview

- **Amazon EventBridge Scheduler** — rate-based trigger, fires every 3 hours, no manual invocation
- **AWS Lambda** (Python) — fetches data, evaluates thresholds, calls Gemini, sends the email
- **Amazon DynamoDB** — one shared table, prefixed keys separate price baselines, seen-hackathon IDs, and seen-listing IDs
- **AWS SSM Parameter Store** — encrypted storage for the Gemini API key
- **Amazon SES** — delivers alert emails, only when triggered

```
EventBridge Scheduler (rate: 3 hours)
        │
        ▼
      Lambda ──► CoinGecko (crypto prices)
        │    ──► Devpost API (web3 hackathons)
        │    ──► SSM (Gemini key)
        │    ──► Gemini + Google Search grounding (new listings)
        ▼
     DynamoDB (baselines / seen-item tracking)      SES (send alert, only if triggered)
```

## What i Learned

The biggest lesson was that reliability for an unattended agent comes from
how it fails, not just how it succeeds. Every external call in this agent —
price API, hackathon API, search grounding — is wrapped so that one
source going down doesn't take the others with it. Nobody's watching a
dashboard to notice a silent failure, so the code has to assume something
*will* eventually error out and keep going anyway.

I also learned, the hard way, that "free tier" isn't always as flat as it
sounds — some capabilities (like search grounding) sit behind a billing
requirement even when the underlying model is nominally free, and that's
the kind of thing you only find by actually shipping and hitting the 429,
not by reading the marketing page.

