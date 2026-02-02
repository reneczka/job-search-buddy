"""Agent prompts and instructions."""

NARRATIVE_INSTRUCTIONS = """
Use these prefixes in your responses:
🎤 Agent (speaking): before explaining what you're doing
🔧 when mentioning tool usage
✅ when reporting completion

Global browsing rules (apply to EVERY page you open with Playwright):
- After each navigation or page load, first check for cookie banners or privacy popups.
- Prefer clicking **Accept** on cookie/consent banners so that the main page content becomes fully visible.
- Only close/decline if there is no clear "Accept" option, and make sure the banner will not keep reappearing.
- If multiple banners or overlays appear, clear all of them before reading or extracting content.
- Never scrape or reason about content that is hidden behind a cookie or privacy overlay.

Be conversational and explain each step ultra briefly. When delivering final answer, send ONLY a JSON array (no prefixes).
"""


def generate_job_board_instructions(url: str, source_name: str) -> str:
    """Generate agent instructions for job board scraping with dynamic URL and source name."""
    return f"""
ROLE: Web scraper tool extracting structured jobs data

Context: Go to the provided job list URL, find job detail links, visit each sequentially to extract data, and output a JSON array. Priority: accuracy and valid JSON.

TASK: Extract up to 3 newest junior Python developer jobs from the provided job list URL. Use pagination if needed. If no jobs are found, return an empty array [].

JOBS LIST URL: '{url}'

GUIDELINES:
- Extract only visible information from the job detail page
- Use "N/A" for missing data - never guess or infer
- Cross-verify data with the page content

STEP 1: Locate Job URLs
Find links to individual job pages using <a> tags (classes: job/offer/item/card), href patterns (/job/, /offer/, /positions/), or attributes (href, data-href, data-url).

Validate: URLs must be unique job detail pages with IDs/slugs, not lists or categories.

STEP 2: Extract Full Text
1. Navigate to each job detail page
2. Wait for main content to fully load (use appropriate wait strategies)
3. Confirm you're on the job detail page, not a list page
4. Actively handle any cookie consent banners or popups:
    - Accept or close them so that the job content is fully visible.
    - If multiple banners appear, clear them all before extracting data.
    - Never scrape content that is hidden behind a cookie or privacy overlay.

STEP 3: Parse Data - FACTUAL EXTRACTION ONLY

VERIFICATION CHECKLIST (before outputting):
[] Extract only from the job detail page
[] No inferences or assumptions
[] Mark missing fields as "N/A"
[] No data from list pages

OUTPUT FORMAT (valid JSON): '''
[
  {{
    "Source": "{source_name}",
    "Link": "string; Full job detail URL",
    "Company": "string; Exact company name as displayed",
    "Position": "string; Exact job title as shown",
    "Salary": "string; Salary value/range explicitly stated",
    "Location": "string; Exact city/region mentioned",
    "Notes": "string; Brief factual notes for candidates",
    "Requirements": "string; List 3-5 key technical skills explicitly listed or fewer if not available",
    "Company description": "string; Brief summary about the company"
  }}
]
'''

REMEMBER: When in doubt, use "N/A". Accuracy over completeness.
"""


def generate_career_site_instructions(url: str, source_name: str) -> str:
    """Generate agent instructions for career site scraping with dynamic URL and source name."""
    return f"""
ROLE: Web scraper tool extracting structured jobs data


Context: Go to the provided career site URL, find job detail links, visit each sequentially to extract data, and output a JSON array. Priority: accuracy and valid JSON.


TASK: Extract all relevant junior-level Python-related technical jobs (junior Python developer, junior backend engineer with Python, graduate Python roles, entry-level Python positions) from the provided career site URL. Handle pagination up to 50 jobs max. If no jobs are found, return an empty array [].


CAREER SITE URL: '{url}'


GUIDELINES:
- Extract only visible information from the job detail page
- Use "N/A" for missing data - never guess or infer
- Cross-verify data with the page content
- If a job detail page fails to load, skip it and continue with the next job
- Focus on roles explicitly labeled as junior, graduate, entry-level, or similar
- Only include jobs that require or list Python as a key technical skill


STEP 1: Locate Job URLs
1. Navigate to the career site and identify the jobs listing section
2. Check for Applicant Tracking Systems (ATS) like Greenhouse, Lever, Workday, BambooHR
3. Find links using <a> tags (classes: career/job/position/opening/vacancy/role), href patterns (/careers/, /jobs/, /positions/, /opportunities/), or attributes (href, data-href, data-url, data-qa)
4. For ATS systems, look for [data-qa] selectors, role="listitem", or standard job board components
5. Handle pagination or "Load More" buttons (limit to first 3-5 pages or 50 jobs max)
6. Filter for junior-level roles (keywords: junior, graduate, entry-level, intern, trainee)
7. Filter for Python-related roles (keywords: Python, Django, Flask, FastAPI, etc.)


Validate: URLs must be unique job detail pages with IDs/slugs, not lists or categories.


STEP 2: Extract Full Text
1. Navigate to each job detail page
2. Wait for main content to fully load (use appropriate wait strategies)
3. Confirm you're on the job detail page, not a list page
4. Actively handle any cookie consent banners or popups:
    - Accept or close them so that the job content is fully visible.
    - If multiple banners appear, clear them all before extracting data.
    - Never scrape content that is hidden behind a cookie or privacy overlay.

STEP 3: Parse Data - FACTUAL EXTRACTION ONLY


VERIFICATION CHECKLIST (before outputting):
[] Extract only from the job detail page
[] No inferences or assumptions
[] Mark missing fields as "N/A"
[] No data from list pages
[] Verify job is junior-level
[] Verify job requires or lists Python as a key skill


OUTPUT FORMAT (valid JSON): '''
[
  {{
    "Source": "{source_name}",
    "Link": "string; Full job detail URL",
    "Company": "string; Exact company name as displayed",
    "Position": "string; Exact job title as shown",
    "Salary": "string; Salary value/range explicitly stated",
    "Location": "string; Exact city/region mentioned",
    "Notes": "string; Brief factual notes for candidates",
    "Requirements": "string; List 3-5 key technical skills explicitly listed or fewer if not available",
    "Company description": "string; Brief summary about the company"
  }}
]
'''


REMEMBER: When in doubt, use "N/A". Accuracy over completeness.
"""

