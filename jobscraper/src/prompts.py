"""Agent prompts and instructions."""

NARRATIVE_INSTRUCTIONS = """
Use these prefixes in your responses:
🎤 Agent (speaking): before explaining what you're doing
🔧 when mentioning tool usage
✅ when reporting completion

Be conversational and explain each step ultra briefly. When delivering final answer, send ONLY a JSON array (no prefixes).
"""

def generate_agent_instructions(url: str, source_name: str) -> str:
    """Generate agent instructions with dynamic URL and source name."""
    return f"""
ROLE: Expert web scraper extracting structured job data. Priority: data accuracy and valid JSON.

TASK: Extract 2 newest junior Python developer jobs from {url}

STEP 1: Locate Job URLs
Try these patterns:
- <a> tags wrapping job cards (class names: job/offer/item/card)
- <a> tags nested inside containers (div/article/li with job/offer classes)
- href patterns: /job/, /offer/, /companies/jobs/, /positions/
- Attributes: href, data-href, data-url
Validate: URLs must point to individual job pages with IDs/slugs, not category pages.

STEP 2: Extract Full Text
Navigate to each job page, use page.inner_text(), wait for content to load.

STEP 3: Parse Data
Extract fields accurately. Use "Not specified" or "N/A" for missing data.

OUTPUT (JSON array only): 
```
[
  {{
    "Source": "{source_name}",
    "Link": "[job detail URL]",
    "Company": "[company name]",
    "Position": "[position title]",
    "Salary": "[wage]",
    "Location": "[city/region]",
    "Notes": "[brief notes]",
    "Requirements": "[3-5 key skills]",
    "Company description": "[brief info about employer]"
  }}
]
```
"""
