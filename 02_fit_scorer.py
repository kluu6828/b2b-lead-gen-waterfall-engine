import pandas as pd
import httpx
from bs4 import BeautifulSoup
import re
import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

RLT_KEYWORDS = [
    r'red\s*light', r'photobiomodulation', r'contour\s*light',
    r'infrared\s*bed', r'light\s*therapy', r'pbm\s*therapy'
]

RECOVERY_KEYWORDS = [
    r'sauna', r'cryo', r'cold\s*plunge', r'hydro\s*massage',
    r'hydromassage', r'compression', r'normatec', r'hyperbaric'
]

TIER_B_KEYWORDS = [
    "chiropractic", "fitness", "gym", "health", "wellness", "clinic"
]

CRAWL_PATHS_FAST = ["", "/services", "/amenities", "/recovery", "/modalities"]
CRAWL_PATHS_PRIMARY = ["", "/services", "/amenities", "/recovery", "/about"]
CRAWL_PATHS_FALLBACK = ["/modalities", "/about-us", "/programs", "/wellness", "/classes"]

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

FAST_TIMEOUT = httpx.Timeout(5.0)
DEEP_TIMEOUT = httpx.Timeout(12.0, connect=8.0)
CRAWL_WORKERS = 12


def find_matched_keywords(scraped_text, limit=3):
    """Return up to `limit` unique keyword matches found in scraped page text."""
    if not scraped_text.strip():
        return []

    matched = []
    for pattern in RLT_KEYWORDS + RECOVERY_KEYWORDS:
        for hit in re.finditer(pattern, scraped_text, re.IGNORECASE):
            matched.append(hit.group(0).strip())

    for term in TIER_B_KEYWORDS:
        if term in scraped_text:
            matched.append(term)

    seen = set()
    unique = []
    for keyword in matched:
        key = keyword.lower()
        if key not in seen:
            seen.add(key)
            unique.append(keyword)
            if len(unique) >= limit:
                break

    return unique


def has_tier_signal(text):
    """True when we have enough keyword signal to assign a tier without more crawling."""
    if not text.strip():
        return False
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in RLT_KEYWORDS + RECOVERY_KEYWORDS):
        return True
    return any(term in text for term in TIER_B_KEYWORDS)


def extract_root_domain(url_str):
    if not isinstance(url_str, str) or not url_str.strip():
        return ''
    url = url_str.strip().lower()
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    try:
        netloc = urlparse(url).netloc
        netloc = re.sub(r'^www\.', '', netloc)
        parts = netloc.split('.')
        if len(parts) >= 2:
            if len(parts) > 2 and parts[-2] in ['co', 'com', 'org', 'gov', 'edu', 'net']:
                return '.'.join(parts[-3:])
            return '.'.join(parts[-2:])
        return netloc
    except Exception:
        return ''


def normalize_crawl_url(url_str):
    url = url_str.strip()
    if not url:
        return ''
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    return url


def build_crawl_url_candidates(url_str):
    """Prefer https + original host, then www, then http fallbacks."""
    url = normalize_crawl_url(url_str)
    if not url:
        return []

    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or ''
    query = f'?{parsed.query}' if parsed.query else ''

    if host.startswith('www.'):
        host_variants = [host, host[4:]]
    else:
        host_variants = [host, f'www.{host}']

    candidates = []
    seen = set()
    for scheme in ('https', 'http'):
        for host_variant in host_variants:
            candidate = f'{scheme}://{host_variant}{path}{query}'
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def extract_page_text(html):
    """Pull readable text from HTML, including title and meta descriptions."""
    soup = BeautifulSoup(html, 'html.parser')
    chunks = []

    if soup.title and soup.title.string:
        chunks.append(soup.title.string)

    for meta in soup.find_all('meta'):
        key = (meta.get('name') or meta.get('property') or '').lower()
        if key in ('description', 'og:description', 'twitter:description'):
            content = meta.get('content')
            if content:
                chunks.append(content)

    for tag in soup(['script', 'style', 'nav', 'footer', 'noscript', 'svg']):
        tag.decompose()

    body_text = soup.get_text(separator=' ', strip=True)
    if body_text:
        chunks.append(body_text)

    return re.sub(r'\s+', ' ', ' '.join(chunks)).strip().lower()


def fetch_page_text(client, url):
    try:
        response = client.get(url)
        if 200 <= response.status_code < 300 and response.text:
            return extract_page_text(response.text)
    except Exception:
        return ''
    return ''


def crawl_paths(client, base_url, paths, scraped_parts):
    """Fetch paths in order, stopping early once tier keywords are found."""
    for path in paths:
        target_url = base_url.rstrip('/') + path
        page_text = fetch_page_text(client, target_url)
        if page_text:
            scraped_parts.append(page_text)
        combined = re.sub(r'\s+', ' ', ' '.join(scraped_parts)).strip()
        if has_tier_signal(combined):
            return combined
    return re.sub(r'\s+', ' ', ' '.join(scraped_parts)).strip()


def crawl_website_fast(url_str):
    """Quick first pass: single https URL and core paths only."""
    url = normalize_crawl_url(url_str)
    if not url:
        return ''

    scraped_parts = []
    try:
        with httpx.Client(
            timeout=FAST_TIMEOUT,
            follow_redirects=True,
            headers=BROWSER_HEADERS,
        ) as client:
            for path in CRAWL_PATHS_FAST:
                try:
                    target_url = url.rstrip('/') + path
                    response = client.get(target_url)
                    if response.status_code == 200 and response.text:
                        soup = BeautifulSoup(response.text, 'html.parser')
                        for tag in soup(['script', 'style', 'nav', 'footer', 'head']):
                            tag.extract()
                        page_text = soup.get_text(separator=' ', strip=True).lower()
                        if page_text:
                            scraped_parts.append(page_text)
                except Exception:
                    continue
    except Exception:
        pass

    return re.sub(r'\s+', ' ', ' '.join(scraped_parts)).strip()


def crawl_website_deep(url_str):
    """Expanded second pass for sites that failed the fast crawl."""
    scraped_parts = []
    base_candidates = build_crawl_url_candidates(url_str)
    if not base_candidates:
        return ''

    for verify_ssl in (True, False):
        try:
            with httpx.Client(
                timeout=DEEP_TIMEOUT,
                follow_redirects=True,
                headers=BROWSER_HEADERS,
                verify=verify_ssl,
            ) as client:
                for base_url in base_candidates:
                    scraped_parts.clear()

                    combined = crawl_paths(client, base_url, CRAWL_PATHS_PRIMARY, scraped_parts)
                    if has_tier_signal(combined):
                        return combined
                    if combined:
                        return combined

                    combined = crawl_paths(client, base_url, CRAWL_PATHS_FALLBACK, scraped_parts)
                    if combined:
                        return combined
        except Exception:
            continue

        if scraped_parts:
            break

    return re.sub(r'\s+', ' ', ' '.join(scraped_parts)).strip()


def score_from_text(scraped_text):
    """Return tier result tuple, or None if no readable text was found."""
    if not scraped_text.strip():
        return None

    has_rlt = any(re.search(pattern, scraped_text, re.IGNORECASE) for pattern in RLT_KEYWORDS)
    has_recovery = any(re.search(pattern, scraped_text, re.IGNORECASE) for pattern in RECOVERY_KEYWORDS)
    matched_keywords = find_matched_keywords(scraped_text)

    if has_rlt or has_recovery:
        fit_tier = "Tier A"
    elif any(term in scraped_text for term in TIER_B_KEYWORDS):
        fit_tier = "Tier B"
    else:
        fit_tier = "Tier C"

    return has_rlt, has_recovery, fit_tier, matched_keywords


def analyze_domain(website_url):
    if not isinstance(website_url, str) or not website_url.strip():
        return False, False, "Tier C (No Website)", []

    fast_result = score_from_text(crawl_website_fast(website_url))
    if fast_result is not None:
        return fast_result

    deep_result = score_from_text(crawl_website_deep(website_url))
    if deep_result is not None:
        return deep_result

    return False, False, "Tier C (Unreachable)", []


def build_preview_df(scored_df, site_col):
    """Return scored chain rows for terminal preview and CSV export."""
    if site_col in scored_df.columns and site_col != 'website':
        scored_df = scored_df.rename(columns={site_col: 'website'})

    if 'total_locations' not in scored_df.columns:
        scored_df = scored_df.copy()
        scored_df['total_locations'] = 1

    preview_cols = [
        c for c in [
            'chain_name', 'website', 'total_locations', 'clean_root_domain',
            'matched_keywords', 'has_rlt', 'has_recovery_zone', 'fit_tier'
        ]
        if c in scored_df.columns
    ]
    preview = scored_df[preview_cols].copy()
    preview['total_locations'] = preview['total_locations'].fillna(1).astype(int)
    preview['matched_keywords'] = preview['matched_keywords'].fillna('')
    return preview


def split_scored_by_independent_type(scored_df, site_col):
    preview_df = build_preview_df(scored_df, site_col)
    standalone_df = preview_df[
        scored_df['source_sheet'].str.contains('standalone', case=False, na=False)
    ].reset_index(drop=True)
    multi_unit_df = preview_df[
        scored_df['source_sheet'].str.contains('multi', case=False, na=False)
    ].reset_index(drop=True)
    return standalone_df, multi_unit_df


def process_file(file_path, sheet_keyword=None, limit=None):
    print(f"📂 Loading Excel file: {file_path}")
    excel_file = pd.ExcelFile(file_path)
    sheet_names = excel_file.sheet_names

    if sheet_keyword:
        target_sheets = [s for s in sheet_names if sheet_keyword.lower() in s.lower()]
        if not target_sheets:
            raise ValueError(f"❌ No sheet matching '{sheet_keyword}' found. Available sheets: {sheet_names}")
    else:
        target_sheets = [s for s in sheet_names if "standalone" in s.lower() or "multi" in s.lower()]
        if not target_sheets and len(sheet_names) >= 6:
            target_sheets = [sheet_names[4], sheet_names[5]]

    print(f"🎯 Targeted sheet(s) for run: {target_sheets}")

    dfs = []
    for sheet in target_sheets:
        temp_df = pd.read_excel(file_path, sheet_name=sheet)
        temp_df['source_sheet'] = sheet
        dfs.append(temp_df)

    combined_df = pd.concat(dfs, ignore_index=True)

    name_col = next((c for c in ['chain_name', 'name', 'title'] if c in combined_df.columns), None)
    site_col = next((c for c in ['website', 'final_website', 'root_domain', 'site', 'domain'] if c in combined_df.columns), None)
    if not site_col:
        raise ValueError("❌ Could not find a website/domain column in the sheets.")

    if 'total_locations' not in combined_df.columns:
        combined_df['total_locations'] = 1
    else:
        combined_df['total_locations'] = combined_df['total_locations'].fillna(1)

    combined_df['clean_root_domain'] = combined_df[site_col].apply(extract_root_domain)

    valid_domains_df = (
        combined_df[combined_df[site_col].fillna('').astype(str).str.strip() != '']
        .sort_values(by=name_col if name_col else site_col)
        .drop_duplicates(subset=['clean_root_domain'], keep='first')
        .copy()
    )

    if limit and limit > 0:
        print(f"🧪 TRIAL MODE: Restricting run to first {limit} unique domains from {target_sheets}...")
        valid_domains_df = valid_domains_df.head(limit)

    print(f"⚡ Crawling {len(valid_domains_df)} unique websites ({CRAWL_WORKERS} workers)...")
    results = {}
    crawl_targets = valid_domains_df[['clean_root_domain', site_col]].to_dict('records')
    completed = 0
    total = len(crawl_targets)

    with ThreadPoolExecutor(max_workers=CRAWL_WORKERS) as executor:
        future_to_domain = {
            executor.submit(analyze_domain, row[site_col]): row['clean_root_domain']
            for row in crawl_targets
        }
        for future in as_completed(future_to_domain):
            domain = future_to_domain[future]
            completed += 1
            if completed % 25 == 0 or completed == total:
                print(f"   ... {completed}/{total} websites crawled")
            try:
                has_rlt, has_rec, tier, matched_keywords = future.result()
                results[domain] = {
                    'has_rlt': has_rlt,
                    'has_recovery_zone': has_rec,
                    'fit_tier': tier,
                    'matched_keywords': ', '.join(matched_keywords)
                }
            except Exception:
                results[domain] = {
                    'has_rlt': False,
                    'has_recovery_zone': False,
                    'fit_tier': "Tier C (Error)",
                    'matched_keywords': ''
                }

    res_df = pd.DataFrame.from_dict(results, orient='index').reset_index()
    res_df.rename(columns={'index': 'clean_root_domain'}, inplace=True)

    scored_df = pd.merge(valid_domains_df, res_df, on='clean_root_domain', how='left')

    standalone_df, multi_unit_df = split_scored_by_independent_type(scored_df, site_col)

    print("\n🔍 --- SAMPLE TRIAL RESULTS PREVIEW ---")
    preview_df = build_preview_df(scored_df, site_col)
    print(preview_df.to_string(index=False))

    unreachable = (preview_df['fit_tier'] == 'Tier C (Unreachable)').sum()
    if unreachable:
        print(f"\n⚠️  {unreachable} site(s) still unreachable after deep-crawl retry.")

    state_prefix = os.path.basename(file_path).rsplit('.', 1)[0]
    state_prefix = state_prefix.replace('_market_analysis', '')
    state_match = re.match(r'^([a-z]{2})-', state_prefix, re.I)
    state_slug = state_match.group(1).lower() if state_match else state_prefix.split('-')[0].lower()
    output_dir = os.path.join("cleaned_output", state_slug)
    os.makedirs(output_dir, exist_ok=True)
    print(f"🗂️  State output folder: {output_dir}/")

    standalone_out = standalone_df.copy()
    standalone_out.insert(0, 'account_type', 'Standalone')
    multi_unit_out = multi_unit_df.copy()
    multi_unit_out.insert(0, 'account_type', 'Multi-Unit')

    output_csv = os.path.join(output_dir, f"{state_prefix}_fit_scored.csv")
    pd.concat([standalone_out, multi_unit_out], ignore_index=True).to_csv(output_csv, index=False)

    print(f"\n✅ Scoring complete!")
    print(f"  📄 CSV: {output_csv}")
    print(f"     - Standalone rows: {len(standalone_df)}")
    print(f"     - Multi-Unit rows: {len(multi_unit_df)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score Independent Accounts from Excel Tabs")
    parser.add_argument("--file", type=str, required=True, help="Path to Excel file (.xlsx)")
    parser.add_argument("--sheet", type=str, default=None, help="Target specific tab keyword (e.g., 'multi' or 'standalone')")
    parser.add_argument("--limit", type=int, default=None, help="Number of rows to test (e.g. 10)")
    args = parser.parse_args()
    process_file(args.file, sheet_keyword=args.sheet, limit=args.limit)
