# Meta Ads Automation System

> **Safety fork:** Meta writes are disabled by default. Rules create
> recommendations requiring explicit, attributable approval. Start with
> [SAFE_MODE.md](SAFE_MODE.md); do not grant `ads_management` for Phase 1.

An open-source, AI-powered Meta (Facebook/Instagram) ads automation system. Built for solo founders and small teams who want to run high-performance ad campaigns without an agency.

**What it does:**
- Creates and manages Meta ad campaigns via the API
- Auto-kills underperforming ads (zero conversions, high CPL, creative fatigue)
- Auto-detects winners and suggests scaling
- Generates ad creatives (copy + images) using AI
- Tracks full funnel: ad click → registration → purchase
- Sends hourly Telegram updates with performance data
- Runs autonomously via GitHub Actions (hourly pipeline)

**What it's NOT:**
- A dashboard (use Meta Ads Manager for that)
- A multi-platform tool (Meta only, for now)
- A "set it and forget it" system (you still review creative quality)

---

## Architecture

```
├── api/                    Meta Marketing API wrappers
│   ├── meta_client.py      Authentication, rate limiting, pagination
│   ├── campaign_manager.py Campaign/ad set/ad CRUD operations
│   ├── creative_manager.py Image + video upload, creative creation
│   └── insights_fetcher.py Performance data pulling
│
├── engine/                 Intelligence layer
│   ├── rules_engine.py     Kill/scale/alert rules (automated decisions)
│   ├── budget_optimizer.py 70/20/10 budget allocation (Hormozi framework)
│   ├── creative_analyzer.py Hook/format/angle performance scoring
│   ├── testing_framework.py A/B test management + winner detection
│   ├── value_equation.py   Hormozi value equation for creative scoring
│   └── compliance_checker.py Meta policy validation
│
├── scripts/                Automated pipelines
│   ├── auto_rules.py       Hourly: evaluate ads, kill losers, detect winners
│   ├── sync_to_supabase.py Hourly: pull Meta data → Supabase
│   ├── process_actions.py  Hourly: execute queued actions
│   ├── funnel_tracker.py   Hourly: track registration → purchase
│   └── daily_run.py        Daily orchestrator
│
├── creative/               Ad creative generation
│   ├── hook_generator.py   Generate scroll-stopping hooks
│   ├── copy_generator.py   Generate ad copy (Hormozi PAS framework)
│   ├── angle_miner.py      Extract ad angles from community data
│   └── brief_generator.py  Full creative brief generation
│
├── knowledge/              The brain (markdown files)
│   ├── how-to-write-hooks.md
│   ├── how-to-write-captions.md
│   ├── how-to-analyze-content.md
│   ├── how-to-judge-quality.md
│   ├── how-to-create-quotes.md
│   └── budget-rules.md
│
├── notifications/          Alert channels
│   ├── telegram.py
│   ├── slack.py
│   └── email.py
│
├── reports/                Performance reporting
│   ├── daily_report.py
│   ├── weekly_report.py
│   ├── creative_report.py
│   └── community_report.py
│
├── config/                 Configuration
│   ├── settings.py         Environment variables + defaults
│   ├── rules.py            Kill/scale/alert rule definitions
│   └── hormozi.py          Hormozi frameworks (value equation, hooks)
│
├── data/                   Data layer
│   ├── db.py               SQLite database operations
│   ├── models.py           Data models (Campaign, Ad, Insight, etc.)
│   └── supabase_*.sql      Database schemas
│
└── .github/workflows/
    └── sync-ads.yml        Hourly GitHub Actions pipeline
```

---

## How It Works

Every hour, the GitHub Actions pipeline runs:

```
1. Process action queue     → Execute pending changes (pause/activate/budget)
2. Sync Meta data          → Pull latest performance into Supabase
3. Funnel tracker          → Match registrations to purchases per source
4. Auto rules              → Kill losers, detect winners, check budgets
5. Process new actions     → Execute any kills/scales from auto rules
6. Telegram pulse          → Send performance update to your phone
```

### Auto Rules (what runs automatically)

| Rule | Trigger | Action |
|------|---------|--------|
| Zero conversions | Spent > 2x target CPL, 0 leads | Pause ad |
| CPL too high | CPL > 3x target | Pause ad |
| Creative fatigue | Frequency > 4.0 | Pause ad |
| Winner detected | CPL ≤ target, 5+ leads, 7+ days | Telegram alert |
| Budget guardian | Daily spend > €150 | Pause ALL ads |

### Budget Guardian

Hard spending limits enforced every hour:
- Max €120/day total across all ad sets
- Max €60/day per single ad set
- Emergency pause if daily spend exceeds €150
- All limits configurable in `knowledge/budget-rules.md`

---

## Setup

### Prerequisites

- Python 3.11+
- A Meta (Facebook) Ads account with API access
- A Supabase project (free tier works)
- Anthropic API key (for AI analyst + creative generation)
- Optional: Telegram bot (for notifications)
- Optional: fal.ai key (for image generation)

### Installation

```bash
git clone https://github.com/yourusername/meta-ads-system.git
cd meta-ads-system
pip install -r requirements.txt

# Copy and fill in your credentials
cp .env.example .env
cp data/campaign_ids.example.json data/campaign_ids.json
cp data/adset_ids.example.json data/adset_ids.json
```

### Meta API Setup

1. Go to [Meta for Developers](https://developers.facebook.com)
2. Create an app → Business type
3. Add the Marketing API product
4. Generate a long-lived access token with these permissions:
   - `ads_management`
   - `ads_read`
   - `pages_read_engagement`
5. Copy your Ad Account ID (format: `act_123456789`)
6. Add these to your `.env` file

### Supabase Setup

1. Create a project at [supabase.com](https://supabase.com)
2. Run the SQL schemas from `data/supabase_*.sql` in the SQL Editor
3. Copy your project URL and service key to `.env`

### Campaign Structure

Create this campaign structure in Meta Ads Manager:

```
[SCALE]    — Proven winners (70% of budget)
[ITERATE]  — Variations of winners (20%)
[TEST]     — New creative concepts (10%)
[RETARGET] — Website visitors, video viewers
```

Add the campaign and ad set IDs to your JSON config files.

### GitHub Actions

Add these secrets to your repo (Settings → Secrets → Actions):

- `META_ACCESS_TOKEN`
- `META_AD_ACCOUNT_ID`
- `SUPABASE_URL_BACKOFFICE`
- `SUPABASE_SERVICE_KEY_BACKOFFICE`
- `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN` (optional)
- `TELEGRAM_CHAT_ID` (optional)

The pipeline runs hourly automatically via `.github/workflows/sync-ads.yml`.

---

## The Knowledge Layer

The intelligence lives in markdown files in `/knowledge/`, not in code. When you learn something new about what works, update the markdown. Every AI-powered feature reads these files.

- `how-to-write-hooks.md` — What makes a hook stop the scroll
- `how-to-write-captions.md` — Platform-specific voice guidelines
- `how-to-judge-quality.md` — Quality gate before publishing anything
- `budget-rules.md` — Hard spending limits

---

## Customization

### Your business, your rules

1. **Edit `config/rules.py`** — Change kill/scale thresholds (CPL target, frequency caps)
2. **Edit `knowledge/budget-rules.md`** — Set your spending limits
3. **Edit `AD_SYSTEM.md`** — Define your funnel, brand, and CTA rules
4. **Edit `AD_RULES.md`** — Set your creative guidelines

### Adding new ad angles

1. Add hooks to `knowledge/how-to-write-hooks.md`
2. The creative engine reads this file when generating new ad concepts
3. Performance data feeds back into `knowledge/` to improve over time

---

## Credits

Built with:
- [Meta Marketing API](https://developers.facebook.com/docs/marketing-apis/)
- [Anthropic Claude](https://www.anthropic.com/) (AI analysis + creative generation)
- [fal.ai](https://fal.ai/) (image generation)
- [Supabase](https://supabase.com/) (data storage)
- Hormozi's [$100M Offers](https://www.acquisition.com/) framework

---

## License

MIT — use it however you want.
