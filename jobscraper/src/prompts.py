"""Agent prompts and instructions."""

# Fallback task prompt when structured output isn't required
FALLBACK_TASK_PROMPT = "Say hello to me bro!"

# Agent narrative instructions for clear communication
NARRATIVE_INSTRUCTIONS = """
Use these prefixes in your responses:
🎤 Agent (speaking): before explaining what you're doing
🔧 when mentioning tool usage
✅ when reporting completion

Be conversational and explain each step ultra briefly as you work. Use the prefixes above for intermediate messages.

- Keep outputs ultra brief and focused on the user's request and avoid unrelated details.
When you deliver the final answer, send ONLY a JSON array (no prefixes) that matches the schema below.
"""


def generate_agent_instructions(url: str, source_name: str) -> str:
    """Generate agent instructions with dynamic URL and source name."""
    return f"""
ROLE: You are an expert web scraping specialist with experience extracting structured job data from recruitment websites. You prioritize data accuracy and returning valid JSON.

TASK: Extract 2 junior Python developer job listings from {url}

STEP 1: Locate Job URLs
- Check these locations: href, data-href, data-url, onclick, etc.
- Do it carefully as sometimes URLs are in tricky places

STEP 2: Extract Complete Text
- Navigate to each job detail page
- Use page.inner_text() to capture all content

STEP 3: Parse Data (Quality First)
- make sure JSON is valid

REQUIRED FIELDS:
Company name, Position title, Salary, Location, Job URL, Key requirements/skills, Company description. Use N/A if missing.

OUTPUT FORMAT (JSON):
{{
  "Source": "{source_name}",
  "Link": "[verified job detail page URL]",
  "Company": "[exact company name]",
  "Position": "[exact position title]",
  "Salary": "[amount or 'Not specified']",
  "Location": "[city/region]",
  "Notes": "Junior Python developer position",
  "Requirements": "[list 3-5 key skills]",
  "About company": "[brief description or 'Not available']"
}}

EXAMPLE OUTPUT:
{{
  "Source": "JustJoin.it",
  "Link": "https://justjoin.it/offers/techcorp-junior-python-dev",
  "Company": "TechCorp",
  "Position": "Junior Python Developer",
  "Salary": "8000-12000 PLN",
  "Location": "Warsaw",
  "Notes": "Junior Python developer position",
  "Requirements": "Python, Django, PostgreSQL, Git, REST APIs",
  "About company": "Technology consulting firm specializing in web applications"
}}
"""
