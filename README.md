# Excell Redlight — Autonomous B2B Lead Generation & Waterfall Enrichment Pipeline

An end-to-end data processing and outbound GTM infrastructure built for a high-ticket Red Light Therapy system client in Boston, MA (in partnership with an ex-owner of Suncapsule).

This system shifts away from low-margin, transactional e-commerce ad spend into targeted, consultative high-ticket B2B sales by automating data ingestion, hygiene, account scoring, and multi-provider waterfall enrichment.

At the core, this repo processes raw Outscraper gym data by U.S. state — filtering down to real fitness centers and gyms, segmenting accounts by type, scoring independent operators into fit tiers based on their websites, and enriching decision makers for Clay handoff and outbound.

Run the scripts **in order**: `01_process_outscraper.py` → `02_fit_scorer.py` → `03_dm_enricher.py`.

---

## System Architecture & Data Flow

1. **Ingestion (Outscraper)**
   - Pulls raw location, website, and metadata for target businesses (fitness centers and independent gyms) across targeted U.S. states.
   - Input: per-state CSV exports (e.g. `ma-fitness-gyms.csv`) or future multi-state batch files.

2. **Filtering, Segmentation & Market Analysis (`01_process_outscraper.py`)**
   - Filters to exact `fitness center` / `gym` categories, deduplicates by address, and classifies accounts: National/Franchise → Regional (5+) → Independent (standalone vs. multi-unit).
   - Produces a 6-tab market analysis workbook per state plus a cross-state **master summary** (`master_market_analysis_summary.xlsx`).

3. **Account Scoring & Fit Tiering (`02_fit_scorer.py`)**
   - Crawls independent gym websites and parses unstructured page text for red light therapy, recovery amenities, and wellness signals.
   - Scores operators into Tier A / B / C priority buckets based on predefined conversion indicators.

4. **Pass 1 Enrichment & Clay Routing (`03_dm_enricher.py`)**
   - Discovers decision makers via Serper (LinkedIn + Google) and website crawl fallback.
   - Attaches the best available email per DM, explodes to one row per contact, and routes rows: **SUCCESS** (Tier 1 named work email) → outreach-ready, **NEEDS_CLAY** → waterfall, **DROPPED** → Tier C without viable email.

5. **Waterfall Enrichment & Outbound (Clay & Instantly)**
   - Clay-ready CSV splits (`tier_1_ready`, `generic_emails`, `dm_no_email`) are imported into Clay for multi-provider waterfall enrichment (verified direct emails and phone numbers).
   - Enriched contacts are pushed to Instantly to orchestrate automated, personalized cold outreach campaigns at scale.

---

## Repository Structure

```
excell-redlight/
├── 01_process_outscraper.py      # Step 1 — filter, segment, market analysis + master summary
├── 02_fit_scorer.py              # Step 2 — website crawl + fit tier scoring
├── 03_dm_enricher.py             # Step 3 — DM discovery, email attach, Clay routing splits
├── .env.example                  # Serper API key template
├── {state}-fitness-gyms.csv      # Raw Outscraper inputs (one file per state)
├── cleaned_output/
│   ├── master_market_analysis_summary.xlsx
│   ├── {state}/                  # All outputs for a state (analysis, scored, enriched)
│   └── trial/                    # Trial / test runs
└── README.md
```

---

## Key Results & Impact

- **Horizontally scalable** across U.S. states — each state runs independently with a unified master summary for market sizing comparisons.
- **Vertically adaptable** — the same ingest → filter → score → enrich pattern can be retargeted to adjacent wellness/fitness verticals by swapping Outscraper queries and keyword lists.
- **Automated list hygiene** — removes non-gyms, deduplicates addresses, segments nationals from independents, and prioritizes Tier A/B accounts before any enrichment spend.
- **Reduced manual data entry** — market analysis workbooks, fit-scored CSVs, and Clay-ready splits are generated programmatically, freeing pipeline capacity for high-ticket consultations and closing.
- **Cost-aware enrichment** — Pass 1 resolves Tier 1 named work emails in-house via Serper + site crawl; only unresolved rows hit Clay's paid waterfall.

---

## Prerequisites

Install dependencies once:

```bash
pip install openpyxl httpx beautifulsoup4 pandas python-dotenv
```

Place your raw Outscraper CSV in the project folder. Name it by state, for example:

```
ma-fitness-gyms.csv
```

All generated files are saved under `cleaned_output/{state}/` (e.g. `cleaned_output/ma/`, `cleaned_output/ct/`). The state folder is created automatically on first run. Trial/test outputs go in `cleaned_output/trial/`.

```
cleaned_output/
├── master_market_analysis_summary.xlsx   ← all states compared on one sheet
├── ct/
│   ├── ct-fitness-gyms_filtered_gyms_pre_dedup.xlsx
│   ├── ct-fitness-gyms_market_analysis.xlsx
│   ├── ct-fitness-gyms_fit_scored.csv
│   └── ct-fitness-gyms_pass1_dm_enriched*.csv
├── ma/
│   └── ...
├── ny/
│   └── ...
└── trial/
    └── trial_*_scored.csv
```

---

## Step 1 — Filter & Segment Raw Data

**Script:** `01_process_outscraper.py`

**Purpose:** Take a state's raw Outscraper export and produce a clean market analysis workbook. This step ensures you are working only with fitness centers and gyms, removes duplicate addresses, and categorizes every account.

### What it does

1. **Loads** the raw Outscraper CSV for a state
2. **Filters** to records where category is exactly `fitness center` or `gym`
3. **Deduplicates** by physical street address (one row per unique location)
4. **Groups** locations into chains using brand name, website domain, or social media page
5. **Classifies** each account into one of three categories:
   - **National/Franchise** — matches a known national brand (e.g. Planet Fitness, CrossFit, YMCA)
   - **Regional** — 5 or more locations in the state
   - **Independent** — fewer than 5 locations and not a known national brand
6. **Splits independents** into two sub-groups:
   - **Standalone** — exactly 1 location
   - **Multi-Units** — 2 to 4 locations

### Command — process a state

```bash
python3 01_process_outscraper.py --file ma-fitness-gyms.csv
```

Replace `ma-fitness-gyms.csv` with your state's raw CSV filename. This regenerates that state's market analysis workbook **and** refreshes the master summary at the end.

### Command — rebuild master only

Rebuild the master file from all existing state workbooks **without** reprocessing any CSV:

```bash
python3 01_process_outscraper.py --rebuild-master
```

| Action | `--file` | `--rebuild-master` |
|---|---|---|
| Process raw CSV | Yes — one state | No |
| Regenerate `{state}_market_analysis.xlsx` | Yes — that state only | No |
| Update `master_market_analysis_summary.xlsx` | Yes | Yes |

`--rebuild-master` scans `cleaned_output/{state}/` for `*_market_analysis.xlsx` files, sorts states A–Z, reads each state's account-tab totals, and rewrites the master sheet. It does **not** reprocess raw CSVs or regenerate individual state workbooks.

### Outputs

| File | Description |
|---|---|
| `cleaned_output/{state}/{state}-fitness-gyms_filtered_gyms_pre_dedup.xlsx` | Filtered gym records before address deduplication |
| `cleaned_output/{state}/{state}-fitness-gyms_market_analysis.xlsx` | Main deliverable — 6-tab Excel workbook |
| `cleaned_output/master_market_analysis_summary.xlsx` | **Master summary** — all states' Summary Page metrics in one sheet (auto-updated) |

### Market analysis workbook tabs

| Tab | Contents |
|---|---|
| **Summary Page** | State-level **Total Clubs** and **%** by segment (live Excel formulas tied to account tabs) |
| **National Accounts** | Grouped national/franchise chains (includes **TOTAL** row summing locations) |
| **Regional Accounts** | Grouped regional chains, 5+ locations (includes **TOTAL** row) |
| **Independent Accounts** | All independent locations combined |
| **Independent Standalone** | One-location independents (name, website, address, phone; **TOTAL** row) |
| **Independent Multi-Units** | 2–4 location independents (includes **TOTAL** row) |

**Summary Page layout** — rows are always in this order:

| Row | Metric |
|---|---|
| 1 | *(header)* Metric \| Total Clubs \| % |
| 2 | Total Fitness Clubs / Gyms (Filtered & Unique Addresses) |
| 3 | Independent Standalone (1 Location) |
| 4 | National / Franchise Locations |
| 5 | Independent Multi-Unit (2-4 Locations) |
| 6 | Regional Accounts (5+ Locations) |

Total Clubs and % on the Summary Page update automatically when you edit location counts on the account tabs (each grouped tab has a **TOTAL** row at the bottom).

The **Independent Standalone** and **Independent Multi-Units** tabs are the inputs used by Step 2.

### Master summary workbook

**File:** `cleaned_output/master_market_analysis_summary.xlsx`  
**Sheet:** Master Summary

Wide comparison layout — metrics listed **once** in column A; each state gets **Total Clubs** and **%** columns to the right:

```
         | CT              | IN              | KY              | ...
         | Total Clubs | % | Total Clubs | % | Total Clubs | % |
Metric   |             |   |             |   |             |   |
Total Fitness Clubs / Gyms        | ... | ... | ... | ... | ...
Independent Standalone (1 Location)| ... | ... | ... | ... | ...
National / Franchise Locations     | ... | ... | ... | ... | ...
Independent Multi-Unit (2-4)       | ... | ... | ... | ... | ...
Regional Accounts (5+ Locations)   | ... | ... | ... | ... | ...
```

- State codes (CT, IN, KY, …) appear as merged headers above each **Total Clubs | %** pair.
- States are sorted A–Z by folder name under `cleaned_output/`.
- **Total Clubs** values are read from each state's account tabs when the master is built or refreshed. **%** columns use simple in-sheet formulas (e.g. `=B4/$B$3`) so percentages stay correct if you edit a total manually.
- New states are picked up automatically the next time you run Step 1 for that state or `--rebuild-master`.
- To refresh all master numbers after editing state workbooks, run `--rebuild-master` again.

**Outdated state workbooks:** The master reads location totals from each state's account tabs (`National Accounts`, `Regional Accounts`, etc.). States processed before the current Summary Page / TOTAL-row layout may still work if those tabs exist, but re-run Step 1 for any state you want fully aligned with the current format:

```bash
python3 01_process_outscraper.py --file ct-fitness-gyms.csv
```

---

## Step 2 — Score Independents into Fit Tiers

**Script:** `02_fit_scorer.py`

**Purpose:** Crawl independent gym websites and assign each chain a fit tier. This step takes the two independent tabs from Step 1 and buckets them into Tier A, B, or C based on what services their website mentions.

### What it does

1. **Reads** the Independent Standalone and Independent Multi-Units tabs from the Step 1 workbook
2. **Crawls** each unique website domain (homepage plus `/services`, `/amenities`, `/recovery`, `/modalities`)
3. **Scans** page text for red light therapy, recovery amenities, and general wellness keywords
4. **Assigns** a fit tier to each chain
5. **Exports** a scored CSV with standalone and multi-unit rows combined

### Fit tier definitions

| Tier | Criteria |
|---|---|
| **Tier A** | Website mentions red light therapy **or** recovery amenities (sauna, cryo, cold plunge, compression, etc.) |
| **Tier B** | No Tier A signals, but site mentions fitness/wellness terms (fitness, gym, health, wellness, clinic, chiropractic) |
| **Tier C** | Site is reachable but none of the above keywords were found |
| **Tier C (Unreachable)** | Website could not be crawled |
| **Tier C (No Website)** | No website on record |

### Command — full run

```bash
python3 02_fit_scorer.py --file cleaned_output/ma/ma-fitness-gyms_market_analysis.xlsx
```

### Command — trial run (recommended first)

Test on a small sample before running the full state:

```bash
python3 02_fit_scorer.py --file cleaned_output/ma/ma-fitness-gyms_market_analysis.xlsx --limit 10
```

### Optional flags

| Flag | Description | Example |
|---|---|---|
| `--limit N` | Only crawl the first N unique domains (for testing) | `--limit 10` |
| `--sheet KEYWORD` | Target one tab only by keyword match | `--sheet standalone` or `--sheet multi` |

### Outputs

| File | Description |
|---|---|
| `cleaned_output/{state}/{state}-fitness-gyms_fit_scored.csv` | **Main deliverable** — scored independents with `account_type` column |

### Fit scored CSV columns

| Column | Description |
|---|---|
| `account_type` | `Standalone` or `Multi-Unit` |
| `chain_name` | Business/chain name |
| `website` | Website URL |
| `total_locations` | Number of locations (1 for standalone, 2–4 for multi-units) |
| `clean_root_domain` | Normalized domain used for crawling |
| `matched_keywords` | Up to 3 keywords found on the website |
| `has_rlt` | Whether red light therapy was detected |
| `has_recovery_zone` | Whether recovery amenities were detected |
| `fit_tier` | Assigned tier (A, B, or C) |

The scored CSV is the input for Step 3.

---

## Step 3 — Multi-DM Enrichment (Pass 1 + Google Backup)

**Script:** `03_dm_enricher.py`

**Purpose:** Discover **all** matching decision makers per company, attach the best available email per DM, and route rows to Clay or drop. Emits **one row per DM** (row explosion), plus **Clay-ready CSV splits**.

### Setup

Create a `.env` file in the project root (see `.env.example`):

```bash
SERPER_API_KEY=your_serper_api_key_here
```

Serper powers Pass 1 (LinkedIn search + Google backup). Without a key, the script skips Serper and uses website crawl only.

### What it does

1. **Pass 1 — LinkedIn / Serper (Priority 1)**
   * Uses `chain_name` + `clean_root_domain` to find company LinkedIn URL and executive profile URLs
   * Parses LinkedIn results for matching titles

2. **Pass 1b — Google backup (when LinkedIn finds 0 DMs)**
   * Runs Google searches via Serper (e.g. `"Chain Name" founder OR owner OR CEO`)
   * Parses snippets, reviews, and answer boxes for owner/founder names
   * Expands first-name-only matches to full names when possible (e.g. Jade → Jade Wilkins)

3. **Pass 2 — Website crawl (Priority 2)**
   * **Always runs** to collect emails from the site (even when LinkedIn found DMs)
   * **DM extraction from HTML** only runs if Pass 1 found zero DMs
   * Tier A/B: homepage + team/about/contact subpages
   * Tier C: homepage + footer only

4. **Row explosion** — one output row per DM (or one null row if none found)

5. **Clay routing (`pass_1_status`)**
   * **SUCCESS** — only when a **Tier 1 Named Work** email is found (`name@customdomain.com`)
   * **NEEDS_CLAY** — Tier A/B with generic work email (`info@`), personal email (`@gmail.com`), DM-only, or no email
   * **DROPPED** — Tier C without a Tier 1 named work email

### Target DM titles

`Owner`, `Founder`, `Co-Owner`, `General Manager`, `Managing Partner`, `President`, `Director of Operations`

### Pass 1 status definitions

| Status | Meaning |
|---|---|
| **SUCCESS** | Tier 1 named work email found (`mark@gym.com`) — ready for outreach, skip Clay |
| **NEEDS_CLAY** | Tier A/B but email is generic, personal, missing, or only a LinkedIn DM was found — send to Clay waterfall |
| **DROPPED** | Tier C without a Tier 1 named work email |

### DM discovery priority

| Value | Meaning |
|---|---|
| **`1_linkedin`** | DM found via LinkedIn/Serper (Pass 1) |
| **`2_website`** | DM found via website HTML crawl (Pass 2, only when LinkedIn found none) |
| **`none`** | No DM found |

### Command — full run

```bash
python3 03_dm_enricher.py --file cleaned_output/ma/ma-fitness-gyms_fit_scored.csv
```

### Command — trial run

```bash
python3 03_dm_enricher.py --file cleaned_output/ma/ma-fitness-gyms_fit_scored.csv --limit 10
```

### Optional flags

| Flag | Description | Example |
|---|---|---|
| `--file PATH` | Input fit scorer CSV (required) | `--file cleaned_output/ma/ma-fitness-gyms_fit_scored.csv` |
| `--output PATH` | Optional override for main enriched CSV | `--output cleaned_output/ma/custom.csv` |
| `--limit N` | Only process first N **companies** (testing) | `--limit 10` |

### Outputs

Outputs are written to a **state subfolder** derived from the input filename (e.g. `ct-fitness-gyms_fit_scored.csv` → `cleaned_output/ct/`).

| File | Description |
|---|---|
| `cleaned_output/{state}/{state}-fitness-gyms_pass1_dm_enriched.csv` | **Main deliverable** — all enriched rows (one row per DM) |
| `cleaned_output/{state}/{state}-fitness-gyms_pass1_dm_enriched_tier_1_ready.csv` | Tier 1 named work emails — ready for outreach, skip Clay |
| `cleaned_output/{state}/{state}-fitness-gyms_pass1_dm_enriched_generic_emails.csv` | Generic/personal emails — send to Clay waterfall |
| `cleaned_output/{state}/{state}-fitness-gyms_pass1_dm_enriched_dm_no_email.csv` | DM names found, no email yet — send to Clay people lookup |

Example for Connecticut:

```
cleaned_output/ct/ct-fitness-gyms_pass1_dm_enriched.csv
cleaned_output/ct/ct-fitness-gyms_pass1_dm_enriched_tier_1_ready.csv
cleaned_output/ct/ct-fitness-gyms_pass1_dm_enriched_generic_emails.csv
cleaned_output/ct/ct-fitness-gyms_pass1_dm_enriched_dm_no_email.csv
```

Clay split files use the same base name as `--output`. If `--output` is omitted, paths are derived automatically from `--file`.

### Output CSV columns

| Column | Description |
|---|---|
| `chain_name` | Business/chain name |
| `website` | Website URL |
| `total_locations` | Location count |
| `clean_root_domain` | Normalized domain |
| `matched_keywords` | Fit scorer keywords |
| `has_rlt` | Red light therapy flag |
| `recovery_zone` | Recovery amenities flag |
| `fit_tier` | Fit tier from Step 2 |
| `dm_first_name` | DM first name |
| `dm_last_name` | DM last name |
| `dm_title` | Matched executive title |
| `dm_email` | Tier 1 named work email for this DM (`name@customdomain.com`) |
| `email_tier` | Tier of `dm_email` (`Tier 1: Named Work` or `None`) |
| `non_personal_email` | Generic work or personal-provider email found (`info@`, `@gmail.com`, etc.) — Clay waterfall |
| `dm_linkedin_url` | LinkedIn profile URL (Serper or site) |
| `company_linkedin_url` | Company LinkedIn page (Serper) |
| `dm_discovery_priority` | `1_linkedin`, `2_website`, or `none` |
| `enrichment_source` | `serper_google`, `site_scrape`, or `none` |
| `pass_1_status` | `SUCCESS`, `NEEDS_CLAY`, or `DROPPED` |

---

## End-to-End Example (Massachusetts)

```bash
# Step 1 — Filter raw Outscraper data and build market analysis (+ refresh master)
python3 01_process_outscraper.py --file ma-fitness-gyms.csv

# Optional — rebuild master only after processing multiple states
python3 01_process_outscraper.py --rebuild-master

# Step 2 — Trial run on 10 domains to verify output looks correct
python3 02_fit_scorer.py --file cleaned_output/ma/ma-fitness-gyms_market_analysis.xlsx --limit 10

# Step 2 — Full run on all independent accounts
python3 02_fit_scorer.py --file cleaned_output/ma/ma-fitness-gyms_market_analysis.xlsx

# Step 3 — Trial DM enrichment on 10 rows
python3 03_dm_enricher.py --file cleaned_output/ma/ma-fitness-gyms_fit_scored.csv --limit 10

# Step 3 — Full Pass 1 enrichment for Clay handoff
python3 03_dm_enricher.py --file cleaned_output/ma/ma-fitness-gyms_fit_scored.csv
```

---

## Workflow Summary

```
Raw Outscraper CSV (state)
        │
        ▼
01_process_outscraper.py
        │
        ├── Filter to fitness center / gym only
        ├── Deduplicate by address
        ├── Classify: National → Regional → Independent
        ├── Split independents: Standalone (1 loc) | Multi-Units (2–4 loc)
        └── Refresh master_market_analysis_summary.xlsx
        │
        ├──► master_market_analysis_summary.xlsx  (all states, wide summary)
        │
        ▼
{state}_market_analysis.xlsx  (in cleaned_output/{state}/)
        │
        ▼
02_fit_scorer.py
        │
        ├── Crawl independent websites
        └── Assign fit tiers: A → B → C
        │
        ▼
{state}_fit_scored.csv  (in cleaned_output/{state}/)
        │
        ▼
03_dm_enricher.py
        │
        ├── Pass 1: LinkedIn/Serper DM search (priority 1)
        ├── Pass 1b: Google backup if LinkedIn finds 0 DMs
        ├── Pass 2: website crawl (emails always; HTML DMs if Pass 1 empty)
        ├── Explode: one row per DM
        ├── Route: SUCCESS | NEEDS_CLAY | DROPPED
        └── Export Clay-ready CSV splits
        │
        ▼
pass1_dm_enriched.csv (+ Clay split CSVs in cleaned_output/{state}/)
```
