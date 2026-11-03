# Helvetra Backend

[![CI](https://github.com/Helvetra/backend/actions/workflows/ci.yml/badge.svg)](https://github.com/Helvetra/backend/actions/workflows/ci.yml)

Translation API powering [Helvetra](https://helvetra.ch), a privacy-first Swiss translation app.

The backend serves two surfaces:

- A **consumer API** (`/api/v1/*`) used by the web app and the iOS app.
- A **B2B public API** (`/api/public/v1/*`) authenticated by API key, billed per character through Stripe meters, and documented at [helvetra.ch/api/public/v1/docs](https://helvetra.ch/api/public/v1/docs).

## Features

### Translation
- Translation API powered by Swiss AI: Apertus 1.5 (70B, `swiss-ai/Apertus-v1.5-70B`) via Infomaniak
- Supported languages: German, French, Italian, English, Romansh, and Swiss German (with Zurich, Bern, Basel, Luzern, St. Gallen, and Wallis dialects)
- Auto-detect source language
- Formality toggle (du/Sie, tu/vous, tu/Lei)
- Incremental translation: `/translate/partial` retranslates only the edited sentence, with the rest of the text as read-only context
- Prompt-injection defence in depth: delimiter-wrapped user input, system-prompt isolation, length-ratio guard, trailing-commentary stripping (EN/DE/FR/IT). See [How Helvetra uses Apertus](#how-helvetra-uses-apertus)
- Tier-based per-request and per-period character limits

### Auth
- Email + password registration with bcrypt + common-password rejection
- HttpOnly+Secure+SameSite=Strict refresh-token cookies scoped to `/api/v1/auth`
- Access tokens (15 min) signed with HS256 via PyJWT
- Refresh-token rotation on every `/refresh`
- CSRF protection (double-submit cookie + `X-CSRF-Token` header) on cookie-authenticated state-changing endpoints
- Per-(email, IP) login lockout to prevent targeted account-lockout DoS
- Email verification with multilingual templates (EN/DE/FR/IT)
- Sign in with Apple (RS256 via PyJWT + Apple JWKS)
- Apple StoreKit 2 subscription verification, currently switched off pending a rewrite against the App Store Root CA chain

### Payments
- **Web (consumer)**: Stripe Checkout for monthly/yearly subscriptions + Customer Portal for self-serve management
- **Web (B2B)**: one self-serve plan with Stripe metered billing, lookup-key-resolved prices, a 14-day trial, and in-line meter events on every translation
- **iOS**: StoreKit 2 (verifier rewrite pending, see above)

### Operations
- Rate limiting in Redis (per-IP global + per-endpoint auth + per-(email,IP) lockout)
- Anonymous usage tracking via atomic Redis Lua script
- B2B usage-alert emails at 80% and 100% of monthly quota
- Stripe webhook signature verification + idempotency dedupe (Apple webhook also deduped by `notificationUUID`)
- Audit log for auth events (login attempts, account deletions, rate-limit hits)

## Tech Stack

- **Framework:** Python 3.11+ / FastAPI
- **Database:** PostgreSQL 15 + SQLAlchemy + Alembic
- **Cache:** Redis 7
- **AI:** Apertus 1.5 70B (`swiss-ai/Apertus-v1.5-70B`) via Infomaniak AI, OpenAI-compatible API
- **Auth:** PyJWT (HS256 + RS256) + Sign in with Apple + Apple StoreKit 2 (gated)
- **Payments:** Stripe (web, consumer + B2B metered) + Apple StoreKit 2 (iOS, gated)

## Setup

```bash
# Clone and configure
cp .env.example .env
# Edit .env: at minimum set JWT_SECRET_KEY, ENCRYPTION_KEY, APERTUS_API_KEY,
# DATABASE_URL, REDIS_URL. App refuses to boot in prod without secrets
# meeting minimum length requirements.

# Run with Docker (Postgres + Redis + backend)
docker compose up -d
docker compose exec api alembic upgrade head   # create/update the database schema

# Or run locally (Python 3.11+)
pip install -r requirements.lock -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload

# API:               http://localhost:8000
# Consumer docs:     http://localhost:8000/docs            (debug-only)
# B2B public docs:   http://localhost:8000/api/public/v1/docs
```

## Running tests

```bash
pip install -r requirements.lock -e ".[dev]"
pytest          # mocks the model, Redis and the database; no API key needed
ruff check app tests
```

The app needs `JWT_SECRET_KEY` (32+ characters) to start, also in tests: set it in `.env` as in Setup, or export it with `DEBUG=true`. CI runs the same steps on every push ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## How Helvetra uses Apertus

Helvetra translates with [Apertus](https://www.apertus-ai.org/), the open model from the Swiss AI Initiative (EPFL, ETH Zurich, CSCS), version 1.5 70B, through Infomaniak's OpenAI-compatible API. Everything below lives in [`app/services/translation.py`](app/services/translation.py).

**One call per translation.** A chat completion with a system prompt (the rules) and a user message (the text). Temperature 0.1; the output budget is sized from the input (512 to 8,192 tokens); one translation gets 90 seconds in total.

**Prompt structure.**
- The user's text sits between `<text>` and `</text>`, followed by an instruction to translate it and never respond to it. Those tags are removed from user input first, so a text cannot close the block and add instructions.
- The system prompt includes short examples (a question, a request, a letter) showing that questions and instructions are translated, not answered.
- For incremental translation, only the edited sentence goes into `<text>`. The full text is given in the system prompt as read-only `<context>`. Putting the context next to the sentence made Apertus translate all of it.
- Automatic source detection asks for JSON (`{"translation": ..., "detected_lang": ...}`). Infomaniak rejects `response_format: json_object` for this model, so the parser accepts code fences and almost-JSON, and rejects anything else.

**Output guards.** Each answer is checked before it is returned. If a check fails, the answer is regenerated once at temperature 0.3; if that fails too, the API returns a specific error code instead of a doubtful translation.
- Name swaps in greetings ("Dear Claudia" becoming "Liebe Alex")
- Invented template placeholders (`[Dein Name]`, `[Datum]`)
- Output more than 3 times the input length (a sign that the model answered instead of translating)
- Translations cut off at the output budget
- Unreadable JSON in auto-detect mode

Cleanup steps remove echoed wrapper tags, trailing notes such as "(Note: ...)", and a repeated source in brackets. German output uses Swiss spelling (ss instead of ß). For casual or formal German, a final reminder keeps du or Sie consistent across the whole text.

**Habits we had to work around.** Answering questions instead of translating them, adding notes, swapping names or inventing placeholders in letters, drifting between du and Sie, and wrapping JSON in Markdown fences. Speed on Infomaniak is about 70 to 90 output tokens per second, which is why one request is limited to 10,000 characters. These protections are prompt structure plus checks, not a guarantee.

## API surfaces

### Consumer API (JWT or session cookie)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/auth/register` | Create account + send verification email |
| POST | `/api/v1/auth/login` | Email/password login (cookie or body refresh token) |
| POST | `/api/v1/auth/refresh` | Rotate refresh token, mint new access token |
| POST | `/api/v1/auth/logout` | Revoke refresh token + clear cookies |
| POST | `/api/v1/auth/verify-email` | Verify email with token from email link |
| POST | `/api/v1/auth/resend-verification` | Resend verification email (rate limited) |
| POST | `/api/v1/auth/apple` | Sign in with Apple (identity-token exchange) |
| GET | `/api/v1/auth/me` | Current authenticated user |
| DELETE | `/api/v1/auth/account` | Delete account + cancel active subscription |
| POST | `/api/v1/translate` | Translate text (anonymous or authenticated) |
| POST | `/api/v1/translate/partial` | Retranslate one sentence, with the surrounding text as context |
| POST | `/api/v1/feedback` | Submit translation feedback (rate-limited per IP) |
| GET | `/api/v1/languages` | List supported languages and dialects |
| GET | `/api/v1/subscription` | Consumer subscription status |
| GET | `/api/v1/subscription/b2b` | B2B subscription status |
| GET | `/api/v1/subscription/b2b/usage-history` | Last 12 monthly usage periods |
| GET | `/api/v1/subscription/limits` | Tier limits for the current user |
| GET | `/api/v1/subscription/anonymous-usage` | Weekly usage without an account |
| POST | `/api/v1/payments/create-gateway` | Start a consumer Stripe checkout |
| POST | `/api/v1/payments/create-b2b-gateway` | Start a B2B Stripe checkout |
| POST | `/api/v1/payments/b2b-portal` | Open Stripe Customer Portal session |
| POST | `/api/v1/api-keys` | Create a B2B API key (one-time copy of plaintext) |
| GET | `/api/v1/api-keys` | List the caller's API keys |
| POST | `/api/v1/api-keys/{id}/rotate` | Rotate an API key (returns new plaintext once) |
| DELETE | `/api/v1/api-keys/{id}` | Revoke an API key |
| POST | `/api/v1/webhooks/stripe` | Stripe webhook receiver |
| POST | `/api/v1/webhooks/apple` | Apple App Store Server Notifications v2 receiver |
| GET | `/api/health` | Health check |

### B2B Public API (API key)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/public/v1/translate` | Translate text (`X-API-Key` auth, billed via Stripe meter) |
| GET | `/api/public/v1/languages` | List supported languages |
| GET | `/api/public/v1/usage` | Current period usage |
| GET | `/api/public/v1/docs` | Swagger UI |
| GET | `/api/public/v1/redoc` | ReDoc |
| GET | `/api/public/v1/openapi.json` | OpenAPI schema (filtered to public routes) |

## Supported Languages

| Code | Language |
|------|----------|
| de | German |
| gsw | Swiss German (with regional dialects) |
| fr | French |
| it | Italian |
| en | English |
| rm | Romansh |

## Project Structure

```
app/
├── main.py             # Application entry point + startup secret validation
├── config.py           # Environment configuration (Pydantic Settings)
├── api/
│   ├── routes/         # FastAPI route handlers
│   ├── public_docs.py  # Filtered OpenAPI schema for the B2B API
│   └── dependencies.py # Auth dependencies, get_client_ip, etc.
├── schemas/            # Pydantic request/response models
├── services/           # Business logic (auth, csrf, translation, stripe, …)
├── models/             # SQLAlchemy models
├── core/
│   ├── database.py     # Async engine + session
│   ├── middleware.py   # IP rate-limit middleware
│   └── tiers.py        # Tier definitions (consumer + B2B)
└── data/               # Static data (common-passwords list)

alembic/                # Database migrations
tests/                  # pytest suite
```

## About

Built by Alex Margot with [Claude Code](https://claude.com/claude-code), reviewed and tested by hand. Issues and questions: gruezi@helvetra.ch.

## License

MIT
