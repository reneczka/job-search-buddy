"""Agent prompts and instructions."""

NARRATIVE_INSTRUCTIONS = """
Use these prefixes in your responses:
🎤 Agent (speaking): before explaining what you're doing
🔧 when mentioning tool usage
✅ when reporting completion

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
4. Dismiss any sign-in prompts, cookie banners, or overlays to access content

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
ROLE: Career site scraper extracting structured jobs data

Context: Navigate to a company's career site, locate job listings, visit each job detail page to extract comprehensive data, and output a JSON array. Priority: accuracy, completeness, and valid JSON.

TASK: Extract all relevant technical jobs from the provided career site URL. Focus on development, engineering, and technical roles. If no jobs are found, return an empty array [].

COMPANY CAREER SITE URL: '{url}'

GUIDELINES:
- Extract only visible information from the job detail page
- Use "N/A" for missing data - never guess or infer
- Cross-verify data with the page content
- Career sites often have more detailed information than job boards
- If a job detail page fails to load or returns an error, skip it and continue with the next job

STEP 0: Initial Navigation
1. Navigate to the career site URL
2. Identify the jobs listing section
3. Check if the site uses an Applicant Tracking System (ATS) like Greenhouse, Lever, Workday, or BambooHR
4. If you encounter iframes, job listings may be embedded - inspect iframe content
5. Handle pagination or "Load More" buttons (limit to first 3-5 pages or 50 jobs max to avoid timeouts)

STEP 1: Locate Job URLs
Find links to individual job pages using common patterns:
- Look for career/jobs/positions sections
- Check for <a> tags with classes like: career, job, position, opening, vacancy, role
- Look for href patterns: /careers/, /jobs/, /positions/, /opportunities/, /openings/
- Check attributes: href, data-href, data-url, data-qa
- For ATS systems, look for selectors like [data-qa], role="listitem", or standard job board components

Validate: URLs must be unique job detail pages with IDs/slugs, not lists or categories.
Avoid duplicates: If the same position appears multiple times (e.g., different locations), ensure Link URLs are distinct before extracting.

STEP 2: Extract Full Text from Each Job
1. Navigate to each job detail page
2. Wait for main content to fully load:
   - Wait for network idle state
   - Wait for key selectors to appear (job title, description, requirements sections)
   - Use timeouts of 5-10 seconds max per page
3. Confirm you're on the job detail page, not a list page
4. Dismiss any sign-in prompts, cookie banners, or overlays to access content
5. If page fails to load, log error and continue to next job

STEP 3: Parse Data - FACTUAL EXTRACTION ONLY

FIELD-SPECIFIC GUIDANCE:
- Position: Extract exact job title as shown
- Location: If Remote/Hybrid/Relocation offered, explicitly include this. For hybrid, extract office location. For multiple locations, list primary ones.
- Salary: Only include if explicitly stated (range or exact value)
- Requirements: Prioritize programming languages, frameworks, tools, years of experience. Format as comma-separated list. Focus on hard skills over soft skills. List 3-5 key technical skills.
- Notes: Include application deadline (if mentioned), notable perks (e.g., "Visa sponsorship available", "Remote-first company"), unique requirements (e.g., "Must be based in EU")
- Team/Department: Look for "Engineering", "Backend Team", "Product Team", "Data Science", etc.
- Employment Type: Common values: "Full-time", "Part-time", "Contract", "Internship", "Temporary"
- Benefits: Extract top 3-5 benefits like "Health insurance", "Stock options", "Flexible hours", "401k", "Learning budget", etc.

VERIFICATION CHECKLIST (before outputting):
[] Extract only from the job detail page
[] No inferences or assumptions
[] Mark missing fields as "N/A"
[] No data from list pages
[] Extract extended fields when available (Team/Department, Employment Type, Benefits)
[] Ensure no duplicate entries (check Link URLs are unique)
[] All JSON fields properly formatted and escaped

OUTPUT FORMAT (valid JSON): '''
[
  {{
    "Source": "{source_name}",
    "Link": "string; Full job detail URL",
    "Company": "string; Exact company name as displayed",
    "Position": "string; Exact job title as shown",
    "Salary": "string; Salary value/range explicitly stated",
    "Location": "string; Exact location (include Remote/Hybrid/Relocation if applicable)",
    "Notes": "string; Application deadline, notable perks, unique requirements",
    "Requirements": "string; Comma-separated list of 3-5 key technical skills",
    "Company description": "string; Brief summary about the company",
    "Team/Department": "string; Specific team or department (e.g., 'Engineering', 'Backend Team')",
    "Employment Type": "string; Full-time/Part-time/Contract/Internship",
    "Benefits": "string; Top 3-5 benefits (e.g., 'Health insurance, Stock options, Flexible hours')"
  }}
]
'''

REMEMBER: When in doubt, use "N/A". Accuracy over completeness. Do not let failed pages stop the entire scraping process. Career sites often provide rich information - extract extended fields when available.
"""
