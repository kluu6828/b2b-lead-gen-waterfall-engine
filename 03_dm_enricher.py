import argparse
import asyncio
import html
import os
import re
from urllib.parse import urlparse

import httpx
import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_TITLES = [
    "Director of Operations",
    "General Manager",
    "Managing Partner",
    "Co-Owner",
    "President",
    "Founder",
    "Owner",
]

SUBPAGE_PATHS = [
    "/about", "/about-us", "/our-team", "/team",
    "/staff", "/leadership", "/contact",
]

GENERIC_EMAIL_PREFIXES = {
    "owner", "info", "contact", "admin", "hello", "support", "office", "team",
}

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "icloud.com",
    "outlook.com", "live.com", "aol.com", "me.com",
}

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
LINKEDIN_PROFILE_REGEX = re.compile(r"https?://(?:[\w.]+)?linkedin\.com/in/[^\s\"'<>?#]+", re.I)
LINKEDIN_COMPANY_REGEX = re.compile(r"https?://(?:[\w.]+)?linkedin\.com/company/[^\s\"'<>?#]+", re.I)

SERPER_URL = "https://google.serper.dev/search"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 5.0
MAX_CONCURRENCY = 15
PROGRESS_INTERVAL = 25

OUTPUT_COLUMNS = [
    "chain_name", "website", "total_locations", "clean_root_domain",
    "matched_keywords", "has_rlt", "recovery_zone", "fit_tier",
    "dm_first_name", "dm_last_name", "dm_title", "dm_email", "email_tier",
    "non_personal_email", "dm_linkedin_url", "company_linkedin_url",
    "dm_discovery_priority", "enrichment_source", "pass_1_status",
]

TITLE_OR_CLAUSE = " OR ".join(f'"{title}"' for title in sorted(TARGET_TITLES, key=len))


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def sanitize_field(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = html.unescape(str(value))
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    text = re.sub(r"\s+", " ", text).strip().strip('"').strip("'")
    return text


def normalize_fit_tier(tier):
    return sanitize_field(tier)


def is_tier_a_or_b(tier):
    tier_upper = normalize_fit_tier(tier).upper()
    return tier_upper.startswith("TIER A") or tier_upper.startswith("TIER B")


def is_tier_c(tier):
    return normalize_fit_tier(tier).upper().startswith("TIER C")


def normalize_website_url(website, root_domain=""):
    website = sanitize_field(website)
    root_domain = sanitize_field(root_domain)
    url = website or root_domain
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def build_page_urls(base_url, paths):
    base = base_url.rstrip("/")
    return [base if path == "" else f"{base}{path}" for path in paths]


def domain_matches_root(email_domain, root_domain):
    if not email_domain or not root_domain:
        return False
    email_domain = email_domain.lower().strip(".")
    root_domain = root_domain.lower().strip(".")
    return (
        email_domain == root_domain
        or email_domain.endswith("." + root_domain)
        or root_domain.endswith("." + email_domain)
    )


def match_target_title(text):
    text_lower = sanitize_field(text).lower()
    if not text_lower:
        return ""
    for title in TARGET_TITLES:
        if title.lower() in text_lower:
            return title
    return ""


def parse_person_name(name_text):
    text = sanitize_field(name_text)
    text = re.sub(r"\s*(,|\||\-|\u2013|\u2014)\s*.*$", "", text)
    text = re.sub(r"\s+(Owner|Founder|Co-Owner|President|General Manager|Partner|Director).*$", "", text, flags=re.I)
    parts = [p for p in text.split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def dm_dedup_key(first_name, last_name, title):
    return (
        sanitize_field(first_name).lower(),
        sanitize_field(last_name).lower(),
        sanitize_field(title).lower(),
    )


def add_dm(
    candidates, seen, first_name, last_name, title,
    linkedin_url="", source="serper_google", discovery_priority="1_linkedin",
):
    first_name = sanitize_field(first_name)
    last_name = sanitize_field(last_name)
    title = sanitize_field(title)
    linkedin_url = sanitize_field(linkedin_url)

    if not first_name and not last_name:
        return

    if not is_valid_person_name(first_name, last_name, linkedin_url):
        return

    if not title:
        return

    key = dm_dedup_key(first_name, last_name, title)
    if key in seen:
        return
    seen.add(key)

    candidates.append({
        "dm_first_name": first_name,
        "dm_last_name": last_name,
        "dm_title": title,
        "dm_linkedin_url": linkedin_url,
        "enrichment_source": source,
        "dm_discovery_priority": discovery_priority,
    })


# ---------------------------------------------------------------------------
# Email extraction & tiering
# ---------------------------------------------------------------------------

def clean_email(raw_email):
    email = sanitize_field(raw_email).lower().rstrip(".,;)")
    email = email.split("?")[0]
    if not EMAIL_REGEX.fullmatch(email):
        return ""
    local, _, domain = email.partition("@")
    if any(bad in domain for bad in ("example.com", "sentry.io", "wixpress.com", "domain.com")):
        return ""
    if len(local) < 2:
        return ""
    return email


def extract_emails_from_html(html_content):
    if not html_content:
        return []

    soup = BeautifulSoup(html_content, "html.parser")
    emails = set()

    for anchor in soup.select('a[href^="mailto:"]'):
        href = anchor.get("href", "")
        cleaned = clean_email(href.replace("mailto:", ""))
        if cleaned:
            emails.add(cleaned)

    for match in EMAIL_REGEX.findall(soup.get_text(" ", strip=True)):
        cleaned = clean_email(match)
        if cleaned:
            emails.add(cleaned)

    return sorted(emails)


def extract_footer_html(html_content):
    soup = BeautifulSoup(html_content or "", "html.parser")
    footer = soup.find("footer")
    if footer:
        return str(footer)
    for node in soup.find_all(["div", "section"], limit=50):
        node_id = (node.get("id") or "").lower()
        node_class = " ".join(node.get("class") or []).lower()
        if "footer" in node_id or "footer" in node_class:
            return str(node)
    return ""


def classify_email_tier(email, root_domain, dm_first_name="", dm_last_name=""):
    local, _, domain = email.partition("@")
    local_base = local.split("+")[0]

    if domain in FREE_EMAIL_DOMAINS:
        return "Tier 3: Personal Email"

    if not domain_matches_root(domain, root_domain):
        return None

    first = sanitize_field(dm_first_name).lower()
    last = sanitize_field(dm_last_name).lower()

    if first and first in local_base:
        return "Tier 1: Named Work"
    if last and len(last) > 2 and last in local_base:
        return "Tier 1: Named Work"
    if first and last and f"{first}.{last}" in local_base:
        return "Tier 1: Named Work"
    if first and last and f"{first[0]}{last}" in local_base:
        return "Tier 1: Named Work"

    if local_base in GENERIC_EMAIL_PREFIXES:
        return "Tier 2: Generic Work"

    # Work-domain email that does not match DM name permutations
    return "Tier 2: Generic Work"


def tier_rank(email_tier):
    return {
        "Tier 1: Named Work": 1,
        "Tier 2: Generic Work": 2,
        "Tier 3: Personal Email": 3,
    }.get(email_tier, 99)


def select_best_email(emails, root_domain, dm_first_name="", dm_last_name=""):
    ranked = []
    for email in emails:
        tier = classify_email_tier(email, root_domain, dm_first_name, dm_last_name)
        if tier:
            ranked.append((tier_rank(tier), email, tier))

    if not ranked:
        return "", "None"

    ranked.sort(key=lambda item: (item[0], item[1]))
    _, best_email, best_tier = ranked[0]
    return best_email, best_tier


def partition_emails_for_dm(emails, root_domain, dm_first_name="", dm_last_name=""):
    """
    Split scraped emails into:
    - dm_email: Tier 1 named work email matching this DM
    - non_personal_email: best generic work or personal-provider email (Clay waterfall)
    """
    ranked = []
    for email in emails:
        tier = classify_email_tier(email, root_domain, dm_first_name, dm_last_name)
        if tier:
            ranked.append((tier_rank(tier), email, tier))

    if not ranked:
        return "", "None", ""

    ranked.sort(key=lambda item: (item[0], item[1]))

    dm_email = ""
    email_tier = "None"
    non_personal_email = ""

    for _, email, tier in ranked:
        if tier == "Tier 1: Named Work":
            dm_email = email
            email_tier = tier
            break

    for _, email, tier in ranked:
        if tier in ("Tier 2: Generic Work", "Tier 3: Personal Email"):
            non_personal_email = email
            break

    return dm_email, email_tier, non_personal_email


def determine_pass_1_status(fit_tier, email_tier):
    """
    SUCCESS  = Tier 1 named work email on the business domain (Clay not required).
    NEEDS_CLAY = Tier A/B but only generic, personal, or missing email — Clay waterfall.
    DROPPED  = Tier C without a Tier 1 named work email.
    """
    if email_tier == "Tier 1: Named Work":
        return "SUCCESS"
    if is_tier_a_or_b(fit_tier):
        return "NEEDS_CLAY"
    if is_tier_c(fit_tier):
        return "DROPPED"
    return "NEEDS_CLAY"


# ---------------------------------------------------------------------------
# Website DM extraction
# ---------------------------------------------------------------------------

def extract_decision_makers_from_html(html_content, chain_name="", root_domain=""):
    if not html_content:
        return []

    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    candidates = []
    seen = set()

    def scan_text(text):
        text = sanitize_field(text)
        if not text or len(text) > 320:
            return
        lowered = text.lower()
        if lowered in {"meet the founder", "contact the founder"}:
            return
        for parsed in parse_snippet_dms(text, chain_name, root_domain):
            add_dm(
                candidates, seen,
                parsed["dm_first_name"], parsed["dm_last_name"], parsed["dm_title"],
                source="site_scrape", discovery_priority="2_website",
            )

    for block in soup.find_all(["div", "section", "article", "li", "p", "h1", "h2", "h3", "h4", "span"]):
        block_text = block.get_text(" ", strip=True)
        if block_text and len(block_text) <= 320:
            scan_text(block_text)

    return candidates


# ---------------------------------------------------------------------------
# Serper.dev
# ---------------------------------------------------------------------------

def parse_linkedin_profile_from_result(
    result, chain_name="", root_domain="", state_slug="", allow_chain_match=False,
):
    link = sanitize_field(result.get("link", ""))
    title_text = sanitize_field(result.get("title", ""))
    snippet = sanitize_field(result.get("snippet", ""))
    combined = f"{title_text} {snippet}"

    if "linkedin.com/in/" not in link.lower():
        return None

    dm_title = match_target_title(combined)
    if not dm_title:
        return None

    if not profile_matches_company(
        combined, chain_name, root_domain, state_slug,
        allow_chain_match=allow_chain_match,
    ):
        return None

    name_part = title_text.split(" - ")[0].split(" | ")[0]
    first_name, last_name = parse_person_name(name_part)
    if not first_name:
        slug = link.rstrip("/").split("/")[-1]
        slug_name = slug.replace("-", " ")
        first_name, last_name = parse_person_name(slug_name.title())

    if not is_valid_person_name(first_name, last_name, link):
        return None

    return {
        "dm_first_name": first_name,
        "dm_last_name": last_name,
        "dm_title": dm_title,
        "dm_linkedin_url": link.split("?")[0],
        "enrichment_source": "serper_google",
    }


NAME_TOKEN = r"[A-Z][a-z\-]+(?:\s+[A-Z](?:[a-z\-]+)?)?"
NAME_STOPWORDS = {
    "the", "our", "we", "a", "an", "hi", "im", "i", "count", "love", "life",
    "move", "movement", "about", "fitness", "health", "wellness", "and", "or",
    "is", "ms", "mr", "ceo", "team", "for", "with", "meet", "cards", "gift",
    "click", "view", "read", "learn", "shop", "book", "join", "explore",
    "co", "textile", "weaving", "pool",
}

GENERIC_CHAIN_WORDS = {
    "fitness", "gym", "training", "studio", "health", "wellness", "athletic",
    "sport", "sports", "center", "centre", "club", "personal", "performance",
    "airport", "crossfit", "yoga", "pilates", "cycle", "cycling", "barbell",
    "strength", "conditioning", "movement", "life", "body", "fit",
}

STATE_LOCATION_HINTS = {
    "ma": ["massachusetts", "martha's vineyard", "marthas vineyard", "cape cod", "boston", "vineyard haven"],
    "ny": ["new york", "nyc", "brooklyn", "manhattan"],
    "nj": ["new jersey", "jersey city", "newark"],
    "ct": ["connecticut", "hartford", "stamford"],
}

FOREIGN_COMPANY_MARKERS = (
    " gmbh", " ag", " und wellness", " s.a.", " s.l.", " b.v.", " oy ",
    "switzerland", " uk", " ltd", " plc", " pty",
)


def is_valid_person_name(first_name, last_name="", linkedin_url=""):
    first_name = sanitize_field(first_name)
    last_name = sanitize_field(last_name)
    linkedin_url = sanitize_field(linkedin_url)
    if not first_name:
        return False
    if first_name.lower() in NAME_STOPWORDS:
        return False
    if last_name and last_name.lower() in NAME_STOPWORDS:
        return False
    if first_name.lower() == "the" or (last_name and last_name.lower() == "the"):
        return False
    if len(first_name) <= 2 and not linkedin_url:
        return False
    if not last_name and not linkedin_url:
        return False
    if last_name and len(last_name) <= 2 and not linkedin_url:
        return False
    return True


def _requires_strict_domain_match(chain_name, root_domain):
    tokens = [
        token for token in re.split(r"[^a-z0-9]+", sanitize_field(chain_name).lower())
        if len(token) > 2
    ]
    distinctive = [token for token in tokens if token not in GENERIC_CHAIN_WORDS]
    if root_domain:
        stem = root_domain.split(".")[0].lower().replace("-", "")
        distinctive = [
            token for token in distinctive
            if token not in stem and stem not in token
        ]
    return len(distinctive) == 0


def _matches_root_domain(text, root_domain):
    text_lower = sanitize_field(text).lower()
    if not text_lower or not root_domain:
        return False
    if root_domain.lower() in text_lower:
        return True
    stem = root_domain.split(".")[0].lower()
    if stem and len(stem) >= 5 and stem.replace("-", "") in text_lower.replace("-", ""):
        return True
    return False


def _matches_state_location(text, state_slug):
    if not state_slug:
        return False
    text_lower = sanitize_field(text).lower()
    slug = state_slug.lower()
    if re.search(rf",\s*{re.escape(slug.upper())}\b", text):
        return True
    if re.search(rf"\b{re.escape(slug.upper())}\b", text):
        return True
    if re.search(rf"\b{re.escape(slug)}\b", text_lower):
        return True
    return any(hint in text_lower for hint in STATE_LOCATION_HINTS.get(slug, []) if hint)


def profile_matches_company(
    text, chain_name, root_domain, state_slug="", allow_chain_match=False,
):
    text_lower = sanitize_field(text).lower()
    if not text_lower:
        return False
    if any(marker in text_lower for marker in FOREIGN_COMPANY_MARKERS):
        return False
    if _matches_root_domain(text, root_domain):
        return True
    if not allow_chain_match:
        return False

    chain_lower = sanitize_field(chain_name).lower()
    if not chain_lower or chain_lower not in text_lower:
        return chain_matches_profile(text, chain_name, root_domain) and _matches_state_location(text, state_slug)

    if not _matches_state_location(text, state_slug):
        return False
    return True


def _company_linkedin_matches(item, chain_name, root_domain, state_slug="", *, allow_chain_match=False):
    text = _organic_item_text(item)
    if _matches_root_domain(text, root_domain):
        return True
    link = sanitize_field(item.get("link", "")).lower()
    if root_domain and root_domain.lower() in link:
        return True
    if not allow_chain_match:
        return False
    return profile_matches_company(
        text, chain_name, root_domain, state_slug, allow_chain_match=True,
    )


def build_chain_search_variants(chain_name, root_domain="", state_slug=""):
    variants = []
    seen = set()

    def add_variant(value):
        value = sanitize_field(value)
        if not value:
            return
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        variants.append(value)

    add_variant(chain_name)
    if state_slug:
        add_variant(f"{chain_name} {state_slug.upper()}")
        if state_slug.lower() in {"ny", "ca", "tx", "fl", "nj", "pa", "ma", "ct"}:
            city_map = {"ny": "NYC", "ca": "LA", "tx": "Houston", "fl": "Miami"}
            city = city_map.get(state_slug.lower())
            if city:
                add_variant(f"{chain_name} {city}")

    if root_domain:
        stem = root_domain.split(".")[0].replace("-", " ")
        if stem and stem.lower() not in chain_name.lower():
            add_variant(stem.title())

    return variants


def chain_matches_profile(text, chain_name, root_domain):
    text_lower = sanitize_field(text).lower()
    if not text_lower:
        return False
    if root_domain and root_domain.lower() in text_lower:
        return True

    for variant in build_chain_search_variants(chain_name, root_domain):
        variant_lower = variant.lower()
        if variant_lower and variant_lower in text_lower:
            return True
        tokens = [token for token in re.split(r"[^a-z0-9]+", variant_lower) if len(token) > 2]
        if tokens and sum(token in text_lower for token in tokens) >= min(2, len(tokens)):
            return True
    return False


def _company_markers(chain_name, root_domain):
    markers = set()
    chain_lower = sanitize_field(chain_name).lower()
    if chain_lower:
        markers.add(chain_lower)
    if root_domain:
        markers.add(root_domain.lower())
        markers.add(root_domain.split(".")[0].lower())
    for variant in build_chain_search_variants(chain_name, root_domain):
        markers.add(variant.lower())
    return {marker for marker in markers if marker}


def _is_relevant_search_text(text, chain_name, root_domain, state_slug=""):
    if _matches_root_domain(text, root_domain):
        return True
    text_lower = sanitize_field(text).lower()
    if not text_lower:
        return False
    chain_lower = sanitize_field(chain_name).lower()
    if chain_lower and chain_lower in text_lower:
        return _matches_state_location(text, state_slug)
    chain_tokens = [token for token in re.split(r"[^a-z0-9]+", chain_lower) if len(token) > 2]
    if chain_tokens and sum(token in text_lower for token in chain_tokens) >= 2:
        return _matches_state_location(text, state_slug)
    return False


def _append_snippet_dm(found, seen, first_name, last_name, title_found):
    first_name = sanitize_field(first_name)
    last_name = sanitize_field(last_name)
    if first_name.lower() in NAME_STOPWORDS or last_name.lower() in NAME_STOPWORDS:
        return
    if not last_name and first_name:
        first_name, last_name = parse_person_name(first_name)
    if first_name.lower() in NAME_STOPWORDS:
        return
    title_found = match_target_title(title_found) or sanitize_field(title_found)
    if not first_name or title_found not in TARGET_TITLES:
        return
    key = dm_dedup_key(first_name, last_name, title_found)
    if key in seen:
        return
    seen.add(key)
    found.append({
        "dm_first_name": first_name,
        "dm_last_name": last_name,
        "dm_title": title_found,
        "dm_linkedin_url": "",
        "enrichment_source": "serper_google",
    })


def _mentions_other_company_as_founder(text, chain_name, root_domain):
    match = re.search(r"(?:owner and )?founder of\s+([^.;,\n]+)", text, re.I)
    if not match:
        return False
    company = sanitize_field(match.group(1)).lower()
    markers = _company_markers(chain_name, root_domain)
    return not any(marker and marker in company for marker in markers)


def _organic_item_text(item):
    return " ".join(
        sanitize_field(item.get(key, "")) for key in ("title", "snippet", "link")
    )


def _rank_organic_items(results, chain_name, root_domain):
    ranked = []
    for item in results.get("organic", []):
        text = _organic_item_text(item)
        if not _is_relevant_search_text(text, chain_name, root_domain, state_slug):
            continue
        link = sanitize_field(item.get("link", "")).lower()
        priority = 2
        if root_domain and root_domain.lower() in link:
            priority = 0
        elif root_domain and root_domain.lower() in text.lower():
            priority = 1
        ranked.append((priority, item))
    ranked.sort(key=lambda entry: entry[0])
    return [item for _, item in ranked]


def parse_snippet_dms(text, chain_name="", root_domain=""):
    found = []
    seen = set()
    text = sanitize_field(text)
    if not text:
        return found

    title_pattern = "|".join(re.escape(title) for title in TARGET_TITLES)

    for match in re.finditer(
        rf"({NAME_TOKEN}(?:\s+[A-Z][a-z\-]+){{0,2}})\s*[,|\-\u2013\u2014]\s*"
        rf"({title_pattern}(?:\s+and\s+CEO)?)",
        text,
    ):
        _append_snippet_dm(found, seen, match.group(1), "", match.group(2))

    for match in re.finditer(
        rf"({title_pattern})\s*[:\-\u2013\u2014]\s*({NAME_TOKEN}(?:\s+[A-Z][a-z\-]+){{0,2}})?",
        text,
    ):
        if match.group(2):
            _append_snippet_dm(found, seen, match.group(2), "", match.group(1))

    for match in re.finditer(
        rf"\b(?:Ms\.|Mr\.)?\s*({NAME_TOKEN})\s+the\s+(?:founder|owner|CEO|president|general manager)\b",
        text,
    ):
        _append_snippet_dm(found, seen, match.group(1), "", match.group(0))

    for match in re.finditer(
        rf"\b(?:owner|founder|CEO)\s+(?:Ms\.|Mr\.)?\s*({NAME_TOKEN})",
        text,
    ):
        title_found = match_target_title(match.group(0)) or "Owner"
        _append_snippet_dm(found, seen, match.group(1), "", title_found)

    for match in re.finditer(rf"Hi,?\s+I'm\s+({NAME_TOKEN})", text):
        context = text[match.start():match.end() + 80]
        if _mentions_other_company_as_founder(context, chain_name, root_domain):
            continue
        title_found = match_target_title(context)
        if title_found:
            _append_snippet_dm(found, seen, match.group(1), "", title_found)

    for match in re.finditer(
        rf"({NAME_TOKEN})\s+(?:is|was)\s+(?:the\s+)?(?:founder|owner|CEO|president)\b",
        text,
        flags=re.I,
    ):
        title_found = match_target_title(match.group(0)) or "Founder"
        _append_snippet_dm(found, seen, match.group(1), "", title_found)

    return found


TITLE_PREFERENCE = {
    "Founder": 0,
    "Owner": 1,
    "Co-Owner": 2,
    "President": 3,
    "General Manager": 4,
    "Managing Partner": 5,
    "Director of Operations": 6,
}

DOMAIN_TITLE_SEARCH_TERMS = [
    "founder",
    "owner",
    "co-owner",
    "president",
    "general manager",
    "managing partner",
    "director of operations",
]


def build_domain_title_queries(root_domain):
    """Manual-style Google lookups: `{domain} founder`, `{domain} owner`, etc."""
    if not root_domain:
        return []
    queries = []
    seen = set()
    for term in DOMAIN_TITLE_SEARCH_TERMS:
        query = f"{root_domain} {term}"
        if query not in seen:
            seen.add(query)
            queries.append(query)
    return queries


def _organic_result_weight(index, link, root_domain):
    weight = max(1, 6 - min(index, 5))
    if root_domain and root_domain.lower() in sanitize_field(link).lower():
        weight += 2
    return weight


def rank_consensus_dms(weighted_hits):
    """Pick the most likely owner/founder when multiple page-1 sources agree."""
    by_person = {}

    for dm, score in weighted_hits:
        person_key = (
            sanitize_field(dm["dm_first_name"]).lower(),
            sanitize_field(dm["dm_last_name"]).lower(),
        )
        if not person_key[0]:
            continue

        if person_key not in by_person:
            by_person[person_key] = {"dm": dict(dm), "score": 0}

        entry = by_person[person_key]
        entry["score"] += score

        current_title = entry["dm"].get("dm_title", "")
        new_title = dm.get("dm_title", "")
        if TITLE_PREFERENCE.get(new_title, 99) < TITLE_PREFERENCE.get(current_title, 99):
            entry["dm"]["dm_title"] = new_title
        if not entry["dm"].get("dm_last_name") and dm.get("dm_last_name"):
            entry["dm"]["dm_last_name"] = dm["dm_last_name"]
        if not entry["dm"].get("dm_linkedin_url") and dm.get("dm_linkedin_url"):
            entry["dm"]["dm_linkedin_url"] = dm["dm_linkedin_url"]

    ranked = sorted(
        by_person.values(),
        key=lambda entry: (
            -entry["score"],
            TITLE_PREFERENCE.get(entry["dm"].get("dm_title", ""), 99),
            -len(sanitize_field(entry["dm"].get("dm_last_name", ""))),
        ),
    )

    by_title = {}
    for entry in ranked:
        title = entry["dm"].get("dm_title", "")
        if title not in TARGET_TITLES:
            continue
        prev = by_title.get(title)
        if not prev or entry["score"] > prev[0]:
            by_title[title] = (entry["score"], entry["dm"])

    return [
        dm for _, dm in sorted(
            by_title.values(),
            key=lambda item: TITLE_PREFERENCE.get(item[1].get("dm_title", ""), 99),
        )
    ]


def _score_dm_candidate(dm, root_domain="", chain_name="", state_slug=""):
    score = 0
    if sanitize_field(dm.get("dm_linkedin_url", "")):
        score += 20
    last_name = sanitize_field(dm.get("dm_last_name", ""))
    if len(last_name) >= 3:
        score += 10
    elif last_name:
        score += 2
    if root_domain:
        stem = root_domain.split(".")[0].lower()
        blob = " ".join(
            sanitize_field(dm.get(key, "")).lower()
            for key in ("dm_linkedin_url", "dm_first_name", "dm_last_name")
        )
        if stem in blob.replace("-", ""):
            score += 5
    return score


def _names_same_person(dm_a, dm_b):
    first_a = sanitize_field(dm_a.get("dm_first_name", "")).lower()
    first_b = sanitize_field(dm_b.get("dm_first_name", "")).lower()
    if not first_a or first_a != first_b:
        return False

    last_a = sanitize_field(dm_a.get("dm_last_name", "")).lower()
    last_b = sanitize_field(dm_b.get("dm_last_name", "")).lower()
    if last_a == last_b:
        return True
    if not last_a or not last_b:
        return True
    return last_a.startswith(last_b) or last_b.startswith(last_a)


def _merge_dm_record(existing, incoming):
    merged = dict(existing)
    for key in ("dm_first_name", "dm_last_name", "dm_title", "dm_linkedin_url", "enrichment_source", "dm_discovery_priority"):
        incoming_val = sanitize_field(incoming.get(key, ""))
        existing_val = sanitize_field(merged.get(key, ""))
        if key == "dm_last_name" and len(incoming_val) > len(existing_val):
            merged[key] = incoming_val
        elif key == "dm_title":
            if TITLE_PREFERENCE.get(incoming_val, 99) < TITLE_PREFERENCE.get(existing_val, 99):
                merged[key] = incoming_val
        elif not existing_val and incoming_val:
            merged[key] = incoming_val
    return merged


def consolidate_dm_candidates(candidates, chain_name="", root_domain="", state_slug=""):
    """
    One row per person (highest-precedence title) and one person per title.
    E.g. same person as Owner+Founder+President becomes Founder only;
    a different person can still fill General Manager.
    """
    if not candidates:
        return []

    merged = []
    for dm in candidates:
        placed = False
        for index, existing in enumerate(merged):
            if _names_same_person(existing, dm):
                merged[index] = _merge_dm_record(existing, dm)
                placed = True
                break
        if not placed:
            merged.append(dict(dm))

    people = [
        dm for dm in merged
        if is_valid_person_name(
            dm.get("dm_first_name", ""),
            dm.get("dm_last_name", ""),
            dm.get("dm_linkedin_url", ""),
        )
    ]

    by_title = {}
    for dm in people:
        title = sanitize_field(dm.get("dm_title", ""))
        if title not in TARGET_TITLES:
            continue
        score = _score_dm_candidate(dm, root_domain, chain_name, state_slug)
        prev = by_title.get(title)
        if not prev or score > prev[0]:
            by_title[title] = (score, dm)

    assigned_people = set()
    result = []
    for title in sorted(by_title, key=lambda t: TITLE_PREFERENCE.get(t, 99)):
        dm = by_title[title][1]
        person_key = (
            sanitize_field(dm.get("dm_first_name", "")).lower(),
            sanitize_field(dm.get("dm_last_name", "")).lower(),
        )
        if person_key in assigned_people:
            continue
        assigned_people.add(person_key)
        result.append(dm)

    return result


def parse_google_first_page(results, chain_name, root_domain, trust_first_page=False, state_slug=""):
    """Extract DM candidates from a Google page-1 payload."""
    weighted_hits = []

    answer_box = results.get("answerBox") or {}
    for key in ("snippet", "title", "answer"):
        blob = sanitize_field(answer_box.get(key, ""))
        if not blob:
            continue
        if _mentions_other_company_as_founder(blob, chain_name, root_domain):
            continue
        for parsed in parse_snippet_dms(blob, chain_name, root_domain):
            weighted_hits.append((parsed, 10))

    for item in results.get("peopleAlsoAsk", []):
        blob = sanitize_field(item.get("snippet", ""))
        if not blob or _mentions_other_company_as_founder(blob, chain_name, root_domain):
            continue
        for parsed in parse_snippet_dms(blob, chain_name, root_domain):
            weighted_hits.append((parsed, 6))

    for index, item in enumerate(results.get("organic", [])[:10]):
        text = _organic_item_text(item)
        if _mentions_other_company_as_founder(text, chain_name, root_domain):
            continue
        link = sanitize_field(item.get("link", "")).lower()
        if trust_first_page:
            if root_domain and root_domain.lower() not in text.lower() and root_domain.lower() not in link:
                continue
        elif not _is_relevant_search_text(text, chain_name, root_domain, state_slug):
            continue

        weight = _organic_result_weight(index, item.get("link", ""), root_domain)
        for parsed in parse_snippet_dms(text, chain_name, root_domain):
            weighted_hits.append((parsed, weight))

    return weighted_hits


def parse_google_backup_dms(results, chain_name, root_domain, trust_first_page=False, state_slug=""):
    return [
        dm for dm, _score in parse_google_first_page(
            results, chain_name, root_domain, trust_first_page=trust_first_page, state_slug=state_slug,
        )
    ]


async def google_domain_title_discovery(
    client, chain_name, root_domain, api_key, semaphore, state_slug="", chain_fallback=False,
):
    """
    Run `{domain} founder`, `{domain} owner`, etc., then use page-1 consensus.
    Chain-name backup queries run only when chain_fallback=True.
    """
    if not root_domain or not api_key:
        return []

    weighted_hits = []
    seen_queries = set()

    for query in build_domain_title_queries(root_domain):
        if query in seen_queries:
            continue
        seen_queries.add(query)
        results = await serper_search(client, query, api_key, semaphore)
        weighted_hits.extend(
            parse_google_first_page(
                results, chain_name, root_domain, trust_first_page=True, state_slug=state_slug,
            )
        )

    domain_ranked = rank_consensus_dms(weighted_hits)
    if domain_ranked or not chain_fallback:
        return domain_ranked

    return await google_chain_backup_discovery(
        client, chain_name, root_domain, api_key, semaphore, state_slug, seen_queries,
    )


async def google_chain_backup_discovery(
    client, chain_name, root_domain, api_key, semaphore, state_slug="", seen_queries=None,
):
    if not api_key:
        return []

    seen_queries = seen_queries or set()
    chain_hits = []
    for query in build_google_backup_queries(chain_name, root_domain, state_slug):
        if query in seen_queries:
            continue
        seen_queries.add(query)
        results = await serper_search(client, query, api_key, semaphore)
        chain_hits.extend(
            parse_google_first_page(
                results, chain_name, root_domain, trust_first_page=False, state_slug=state_slug,
            )
        )

    return rank_consensus_dms(chain_hits)


def build_google_backup_queries(chain_name, root_domain, state_slug=""):
    queries = []
    seen = set()

    for variant in build_chain_search_variants(chain_name, root_domain, state_slug):
        query = f'"{variant}" founder OR owner OR CEO'
        if query not in seen:
            seen.add(query)
            queries.append(query)

    return queries


def build_domain_linkedin_queries(root_domain):
    if not root_domain:
        return []
    stem = root_domain.split(".")[0]
    return [
        f"site:linkedin.com/in/ {root_domain} ({TITLE_OR_CLAUSE})",
        f'site:linkedin.com/in/ "{stem}" owner OR founder',
    ]


def build_chain_linkedin_queries(chain_name, root_domain="", state_slug=""):
    queries = []
    seen = set()
    for variant in build_chain_search_variants(chain_name, root_domain, state_slug):
        query = f'site:linkedin.com/in/ "{variant}" ({TITLE_OR_CLAUSE})'
        if query not in seen:
            seen.add(query)
            queries.append(query)
    return queries


def extract_first_name_from_email(email):
    local = clean_email(email).split("@")[0]
    if not local or local in GENERIC_EMAIL_PREFIXES:
        return ""
    if not re.fullmatch(r"[a-z]{2,}", local):
        return ""
    return local.capitalize()


async def infer_dms_from_named_emails(client, site_emails, chain_name, root_domain, api_key, semaphore, state_slug=""):
    inferred = []
    seen = set()
    checked = set()

    for email in site_emails:
        first = extract_first_name_from_email(email)
        if not first or first.lower() in checked:
            continue
        checked.add(first.lower())

        queries = [
            f'"{first}" "{root_domain}" owner OR founder OR CEO',
        ]
        domain_found = False
        for query in queries:
            results = await serper_search(client, query, api_key, semaphore)
            for parsed in parse_google_backup_dms(
                results, chain_name, root_domain, state_slug=state_slug,
            ):
                if parsed["dm_first_name"].lower() != first.lower() and not parsed["dm_last_name"]:
                    continue
                key = dm_dedup_key(parsed["dm_first_name"], parsed["dm_last_name"], parsed["dm_title"])
                if key in seen:
                    continue
                seen.add(key)
                inferred.append(parsed)
            if inferred:
                domain_found = True
                break

        if domain_found:
            continue

        for variant in build_chain_search_variants(chain_name, root_domain, state_slug)[:2]:
            query = f'"{first}" "{variant}" founder OR owner'
            results = await serper_search(client, query, api_key, semaphore)
            for parsed in parse_google_backup_dms(
                results, chain_name, root_domain, state_slug=state_slug,
            ):
                if parsed["dm_first_name"].lower() != first.lower() and not parsed["dm_last_name"]:
                    continue
                key = dm_dedup_key(parsed["dm_first_name"], parsed["dm_last_name"], parsed["dm_title"])
                if key in seen:
                    continue
                seen.add(key)
                inferred.append(parsed)
            if inferred:
                break

    return inferred


async def expand_dm_last_names_from_google(
    client, dm_candidates, chain_name, root_domain, api_key, semaphore, state_slug="",
):
    for dm in dm_candidates:
        if dm.get("dm_last_name") or not dm.get("dm_first_name"):
            continue
        first = dm["dm_first_name"]
        followup_queries = [
            f'"{first}" "{root_domain}"',
            f'"{chain_name}" "{first}" owner OR founder OR CEO',
        ]
        for query in followup_queries:
            followup_results = await serper_search(client, query, api_key, semaphore)
            for parsed in parse_google_backup_dms(
                followup_results, chain_name, root_domain, state_slug=state_slug,
            ):
                if (
                    parsed["dm_first_name"].lower() == first.lower()
                    and parsed["dm_last_name"]
                ):
                    dm["dm_last_name"] = parsed["dm_last_name"]
                    break
            if dm.get("dm_last_name"):
                break

            for item in followup_results.get("organic", []):
                text = " ".join(
                    sanitize_field(item.get(key, "")) for key in ("title", "snippet")
                )
                if not _is_relevant_search_text(text, chain_name, root_domain, state_slug):
                    continue
                match = re.search(rf"\b{re.escape(first)}\s+([A-Z][a-z]+)\b", text)
                if match and match.group(1).lower() not in NAME_STOPWORDS:
                    dm["dm_last_name"] = match.group(1)
                    break
            if dm.get("dm_last_name"):
                break


async def serper_search(client, query, api_key, semaphore):
    if not api_key:
        return {}

    async with semaphore:
        try:
            response = await client.post(
                SERPER_URL,
                json={"q": query, "num": 10},
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                return response.json()
        except Exception:
            return {}
    return {}


async def serper_enrich(client, row, api_key, semaphore, state_slug=""):
    chain_name = sanitize_field(row.get("chain_name", ""))
    root_domain = sanitize_field(row.get("clean_root_domain", ""))

    dm_candidates = []
    seen = set()
    company_linkedin_url = ""

    # Phase 1: root-domain discovery (LinkedIn + Google)
    for title_query in build_domain_linkedin_queries(root_domain):
        profile_results = await serper_search(client, title_query, api_key, semaphore)
        for item in profile_results.get("organic", [])[:3]:
            parsed = parse_linkedin_profile_from_result(
                item, chain_name, root_domain, state_slug=state_slug, allow_chain_match=False,
            )
            if parsed:
                add_dm(
                    dm_candidates, seen,
                    parsed["dm_first_name"], parsed["dm_last_name"], parsed["dm_title"],
                    linkedin_url=parsed["dm_linkedin_url"], source="serper_google",
                )

    if root_domain and api_key:
        for dm in await google_domain_title_discovery(
            client, chain_name, root_domain, api_key, semaphore, state_slug,
            chain_fallback=False,
        ):
            add_dm(
                dm_candidates, seen,
                dm["dm_first_name"], dm["dm_last_name"], dm["dm_title"],
                linkedin_url=dm.get("dm_linkedin_url", ""), source="serper_google",
            )

    if root_domain and api_key:
        company_results = await serper_search(
            client, f'site:linkedin.com/company/ "{root_domain}"', api_key, semaphore,
        )
        for item in company_results.get("organic", []):
            link = sanitize_field(item.get("link", ""))
            if "linkedin.com/company/" not in link.lower():
                continue
            if _company_linkedin_matches(
                item, chain_name, root_domain, state_slug, allow_chain_match=False,
            ):
                company_linkedin_url = link.split("?")[0]
                break

    # Phase 2: chain-name fallback only when domain pass found nobody
    if not dm_candidates and root_domain and api_key:
        for title_query in build_chain_linkedin_queries(chain_name, root_domain, state_slug):
            profile_results = await serper_search(client, title_query, api_key, semaphore)
            for item in profile_results.get("organic", [])[:3]:
                parsed = parse_linkedin_profile_from_result(
                    item, chain_name, root_domain, state_slug=state_slug, allow_chain_match=True,
                )
                if parsed:
                    add_dm(
                        dm_candidates, seen,
                        parsed["dm_first_name"], parsed["dm_last_name"], parsed["dm_title"],
                        linkedin_url=parsed["dm_linkedin_url"], source="serper_google",
                    )

        for dm in await google_chain_backup_discovery(
            client, chain_name, root_domain, api_key, semaphore, state_slug,
        ):
            add_dm(
                dm_candidates, seen,
                dm["dm_first_name"], dm["dm_last_name"], dm["dm_title"],
                linkedin_url=dm.get("dm_linkedin_url", ""), source="serper_google",
            )

    if not company_linkedin_url:
        for variant in build_chain_search_variants(chain_name, root_domain, state_slug):
            company_query = f'site:linkedin.com/company/ "{variant}"'
            company_results = await serper_search(client, company_query, api_key, semaphore)
            for item in company_results.get("organic", []):
                link = sanitize_field(item.get("link", ""))
                if "linkedin.com/company/" not in link.lower():
                    continue
                if _company_linkedin_matches(
                    item, chain_name, root_domain, state_slug, allow_chain_match=True,
                ):
                    company_linkedin_url = link.split("?")[0]
                    break
            if company_linkedin_url:
                break

    if dm_candidates:
        await expand_dm_last_names_from_google(
            client, dm_candidates, chain_name, root_domain, api_key, semaphore, state_slug,
        )

    return consolidate_dm_candidates(
        dm_candidates, chain_name, root_domain, state_slug=state_slug,
    ), company_linkedin_url


# ---------------------------------------------------------------------------
# Async crawling & row building
# ---------------------------------------------------------------------------

async def fetch_html(client, url, semaphore):
    async with semaphore:
        try:
            response = await client.get(url, timeout=REQUEST_TIMEOUT)
            if 200 <= response.status_code < 300:
                return response.text
        except Exception:
            return ""
    return ""


async def crawl_pages(client, urls, semaphore):
    pages = await asyncio.gather(*[fetch_html(client, url, semaphore) for url in urls])
    return [page for page in pages if page]


def build_company_base_row(row):
    return {
        "chain_name": sanitize_field(row.get("chain_name", "")),
        "website": sanitize_field(row.get("website", "")),
        "total_locations": row.get("total_locations", 1),
        "clean_root_domain": sanitize_field(row.get("clean_root_domain", "")),
        "matched_keywords": sanitize_field(row.get("matched_keywords", "")),
        "has_rlt": row.get("has_rlt", False),
        "recovery_zone": row.get("has_recovery_zone", row.get("recovery_zone", False)),
        "fit_tier": sanitize_field(row.get("fit_tier", "")),
    }


def build_dm_output_rows(base_row, dm_list, site_emails, company_linkedin_url=""):
    output_rows = []

    if not dm_list:
        dm_email, email_tier, non_personal_email = partition_emails_for_dm(
            site_emails, base_row["clean_root_domain"]
        )
        row = {**base_row}
        row.update({
            "dm_first_name": "",
            "dm_last_name": "",
            "dm_title": "",
            "dm_email": dm_email,
            "email_tier": email_tier,
            "non_personal_email": non_personal_email,
            "dm_linkedin_url": "",
            "company_linkedin_url": sanitize_field(company_linkedin_url),
            "dm_discovery_priority": "none",
            "enrichment_source": "none",
            "pass_1_status": determine_pass_1_status(base_row["fit_tier"], email_tier),
        })
        output_rows.append(row)
        return output_rows

    for dm in dm_list:
        dm_email, email_tier, non_personal_email = partition_emails_for_dm(
            site_emails,
            base_row["clean_root_domain"],
            dm.get("dm_first_name", ""),
            dm.get("dm_last_name", ""),
        )

        row = {**base_row}
        row.update({
            "dm_first_name": sanitize_field(dm.get("dm_first_name", "")),
            "dm_last_name": sanitize_field(dm.get("dm_last_name", "")),
            "dm_title": sanitize_field(dm.get("dm_title", "")),
            "dm_email": dm_email,
            "email_tier": email_tier,
            "non_personal_email": non_personal_email,
            "dm_linkedin_url": sanitize_field(dm.get("dm_linkedin_url", "")),
            "company_linkedin_url": sanitize_field(company_linkedin_url),
            "dm_discovery_priority": sanitize_field(dm.get("dm_discovery_priority", "none")),
            "enrichment_source": sanitize_field(dm.get("enrichment_source", "none")),
            "pass_1_status": determine_pass_1_status(base_row["fit_tier"], email_tier),
        })
        output_rows.append(row)

    return output_rows


async def enrich_company(client, row, semaphore, serper_api_key, state_slug=""):
    base_row = build_company_base_row(row)
    fit_tier = base_row["fit_tier"]
    root_domain = base_row["clean_root_domain"]
    chain_name = base_row["chain_name"]
    base_url = normalize_website_url(base_row["website"], root_domain)

    dm_candidates = []
    seen = set()
    company_linkedin_url = ""
    site_emails = []

    # Pass 1 (Priority 1): LinkedIn / Serper using chain name + root domain
    if serper_api_key and root_domain:
        serper_dms, company_linkedin_url = await serper_enrich(
            client, row, serper_api_key, semaphore, state_slug=state_slug,
        )
        for dm in serper_dms:
            add_dm(
                dm_candidates, seen,
                dm["dm_first_name"], dm["dm_last_name"], dm["dm_title"],
                linkedin_url=dm.get("dm_linkedin_url", ""),
                source="serper_google",
                discovery_priority="1_linkedin",
            )

    # Pass 2 (Priority 2): Website crawl — always for emails; DMs only if LinkedIn found none
    if base_url:
        paths = ([""] + SUBPAGE_PATHS) if is_tier_a_or_b(fit_tier) else [""]
        urls = build_page_urls(base_url, paths)
        html_pages = await crawl_pages(client, urls, semaphore)

        for page in html_pages:
            site_emails.extend(extract_emails_from_html(page))
            if not dm_candidates:
                for dm in extract_decision_makers_from_html(page, chain_name, root_domain):
                    add_dm(
                        dm_candidates, seen,
                        dm["dm_first_name"], dm["dm_last_name"], dm["dm_title"],
                        linkedin_url=dm.get("dm_linkedin_url", ""),
                        source="site_scrape",
                        discovery_priority="2_website",
                    )

        if is_tier_c(fit_tier) and html_pages:
            site_emails.extend(extract_emails_from_html(extract_footer_html(html_pages[0])))

    site_emails = sorted(set(site_emails))

    if serper_api_key and site_emails and not dm_candidates:
        for parsed in await infer_dms_from_named_emails(
            client, site_emails, chain_name, root_domain, serper_api_key, semaphore, state_slug,
        ):
            add_dm(
                dm_candidates, seen,
                parsed["dm_first_name"], parsed["dm_last_name"], parsed["dm_title"],
                linkedin_url=parsed.get("dm_linkedin_url", ""),
                source="serper_google",
                discovery_priority="1_linkedin",
            )

    dm_candidates = consolidate_dm_candidates(
        dm_candidates, chain_name, root_domain, state_slug=state_slug,
    )

    return build_dm_output_rows(base_row, dm_candidates, site_emails, company_linkedin_url)


async def enrich_dataframe(df, limit=None, serper_api_key="", state_slug=""):
    if limit and limit > 0:
        df = df.head(limit).copy()

    total = len(df)
    rows = [row._asdict() for row in df.itertuples(index=False)]
    exploded_rows = []
    completed = 0
    status_counts = {"SUCCESS": 0, "NEEDS_CLAY": 0, "DROPPED": 0}
    dm_rows_total = 0

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    async with httpx.AsyncClient(follow_redirects=True, headers=BROWSER_HEADERS) as client:

        async def enrich_indexed(index, row):
            result_rows = await enrich_company(client, row, semaphore, serper_api_key, state_slug)
            return index, result_rows

        tasks = [asyncio.create_task(enrich_indexed(i, row)) for i, row in enumerate(rows)]

        for finished in asyncio.as_completed(tasks):
            _, result_rows = await finished
            exploded_rows.extend(result_rows)
            completed += 1
            dm_rows_total += len(result_rows)

            for result_row in result_rows:
                status = result_row.get("pass_1_status", "")
                if status in status_counts:
                    status_counts[status] += 1

            if completed % PROGRESS_INTERVAL == 0 or completed == total:
                print(
                    f"   ... {completed}/{total} companies processed | "
                    f"{dm_rows_total} DM rows emitted | "
                    f"SUCCESS: {status_counts['SUCCESS']} | "
                    f"NEEDS_CLAY: {status_counts['NEEDS_CLAY']} | "
                    f"DROPPED: {status_counts['DROPPED']}"
                )

    return pd.DataFrame(exploded_rows, columns=OUTPUT_COLUMNS)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_input_csv(file_path):
    df = pd.read_csv(file_path)
    df = df.rename(columns={"recovery_zone": "has_recovery_zone"})

    required = {
        "chain_name", "website", "total_locations", "clean_root_domain",
        "matched_keywords", "has_rlt", "fit_tier",
    }
    if "has_recovery_zone" not in df.columns:
        raise ValueError("Missing required column: has_recovery_zone (or recovery_zone)")

    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    return df


def _column_has_value(series):
    return series.astype(str).str.strip().replace("nan", "").ne("")


def build_clay_export_tabs(df):
    has_tier1_email = _column_has_value(df["dm_email"]) & (df["pass_1_status"] == "SUCCESS")
    has_generic_email = (
        _column_has_value(df["non_personal_email"])
        & (df["pass_1_status"] != "SUCCESS")
    )
    has_dm_name = _column_has_value(df["dm_first_name"]) | _column_has_value(df["dm_last_name"])
    dm_no_email = (
        has_dm_name
        & ~_column_has_value(df["dm_email"])
        & ~_column_has_value(df["non_personal_email"])
    )

    return {
        "All Rows": df,
        "Tier 1 Ready": df[has_tier1_email].copy(),
        "Generic Emails": df[has_generic_email].copy(),
        "DM No Email": df[dm_no_email].copy(),
    }


CLAY_EXPORT_SUFFIXES = {
    "All Rows": "",
    "Tier 1 Ready": "_tier_1_ready",
    "Generic Emails": "_generic_emails",
    "DM No Email": "_dm_no_email",
}


def derive_state_prefix(input_path):
    basename = os.path.basename(input_path).rsplit(".", 1)[0]
    return basename.replace("_fit_scored", "").replace("_market_analysis", "")


def derive_state_slug(state_prefix):
    match = re.match(r"^([a-z]{2})-", state_prefix, re.I)
    if match:
        return match.group(1).lower()
    return state_prefix.split("-")[0].lower()


def resolve_output_path(input_path, output_path=None):
    state_prefix = derive_state_prefix(input_path)
    state_slug = derive_state_slug(state_prefix)

    if output_path:
        return normalize_csv_output_path(output_path), state_slug, state_prefix

    default_path = os.path.join(
        "cleaned_output",
        state_slug,
        f"{state_prefix}_pass1_dm_enriched.csv",
    )
    return default_path, state_slug, state_prefix


def normalize_csv_output_path(output_path):
    base, ext = os.path.splitext(output_path)
    if ext.lower() == ".xlsx":
        return f"{base}.csv"
    if ext.lower() != ".csv":
        return f"{output_path}.csv"
    return output_path


def write_enriched_output(df, output_path):
    output_path = normalize_csv_output_path(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    base, _ = os.path.splitext(output_path)
    tabs = build_clay_export_tabs(df)
    written_paths = {}

    for sheet_name, tab_df in tabs.items():
        suffix = CLAY_EXPORT_SUFFIXES[sheet_name]
        path = f"{base}{suffix}.csv"
        tab_df.to_csv(path, index=False)
        written_paths[sheet_name] = path

    return written_paths["All Rows"], tabs, written_paths


def print_summary(df, tabs=None):
    print("\n📊 Pass 1 Summary (by output row)")
    print(df["pass_1_status"].value_counts().to_string())

    print("\n🔎 DM Discovery Priority")
    print(df["dm_discovery_priority"].value_counts().to_string())

    print("\n🔎 Enrichment Source Breakdown")
    print(df["enrichment_source"].value_counts().to_string())

    print("\n👤 DM Rows With LinkedIn URL")
    linkedin_rows = df[df["dm_linkedin_url"].astype(str).str.strip() != ""]
    print(f"{len(linkedin_rows)} rows")

    print("\n📬 Non-Personal Emails Captured (for Clay)")
    non_personal = df[df["non_personal_email"].astype(str).str.strip() != ""]
    print(f"{len(non_personal)} rows")

    print("\n📬 DM Email Tier Breakdown (Tier 1 named work only)")
    emailed = df[df["dm_email"].astype(str).str.strip() != ""]
    if len(emailed):
        print(emailed["email_tier"].value_counts().to_string())
    else:
        print("No Tier 1 named work emails found.")

    if tabs:
        print("\n📑 Clay Export CSVs")
        for sheet_name, tab_df in tabs.items():
            if sheet_name == "All Rows":
                continue
            print(f"  {sheet_name}: {len(tab_df)} rows")


def process_file(input_path, output_path=None, limit=None):
    serper_api_key = os.getenv("SERPER_API_KEY", "").strip()
    output_path, state_slug, state_prefix = resolve_output_path(input_path, output_path)

    print(f"📂 Loading fit scorer results: {input_path}")
    print(f"🗂️  State output folder: cleaned_output/{state_slug}/")
    df = load_input_csv(input_path)
    total_rows = len(df) if not limit else min(limit, len(df))

    tier_a_b = df.head(total_rows)["fit_tier"].apply(is_tier_a_or_b).sum()
    tier_c = df.head(total_rows)["fit_tier"].apply(is_tier_c).sum()

    print(f"🎯 Starting Pass 1 multi-DM enrichment on {total_rows} companies (of {len(df)} in file)")
    print(f"   Tier A/B (deep crawl): {tier_a_b} | Tier C (homepage only): {tier_c}")
    print(f"⚡ Crawling with {MAX_CONCURRENCY} concurrent workers")
    if serper_api_key:
        print("🔎 Pass 1: LinkedIn/Serper (priority 1) → Pass 2: website crawl (priority 2, DMs only if LinkedIn empty)")
    else:
        print("⚠️  SERPER_API_KEY not set — skipping LinkedIn pass; website crawl only")
    print()

    enriched_df = asyncio.run(
        enrich_dataframe(df, limit=limit, serper_api_key=serper_api_key, state_slug=state_slug),
    )

    main_csv_path, tabs, written_paths = write_enriched_output(enriched_df, output_path)

    print(f"\n✅ Pass 1 enrichment complete!")
    print(f"  📄 Output: {main_csv_path}")
    for sheet_name, path in written_paths.items():
        if sheet_name == "All Rows":
            continue
        print(f"  📄 {sheet_name}: {path}")
    print(f"  📈 {len(enriched_df)} total rows written ({total_rows} companies processed)")
    print_summary(enriched_df, tabs=tabs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pass 1 multi-DM + email enrichment for fit scorer leads")
    parser.add_argument(
        "--file",
        type=str,
        required=True,
        help="Path to fit scorer CSV input (e.g. cleaned_output/ct-fitness-gyms_fit_scored.csv)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional main enriched CSV path; defaults to cleaned_output/{state}/{state}-fitness-gyms_pass1_dm_enriched.csv",
    )
    parser.add_argument("--limit", type=int, default=None, help="Optional company limit for trial runs")
    args = parser.parse_args()
    process_file(args.file, args.output, limit=args.limit)
