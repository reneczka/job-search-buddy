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
Scrape 2 junior Python jobs from {url}

Important extraction guidelines:

1. Intelligently locate job offer URLs: Job detail page links are sometimes hidden, unconventional, or not immediately visible. They may be nested within JavaScript events, data attributes (data-href, data-url, data-link), onclick handlers, or dynamically loaded elements. Check multiple locations including href attributes, data-* attributes, onclick handlers, and monitor network requests to identify the correct URLs. Sometimes links are buried in nested div elements or require hovering/clicking to reveal.

2. Extract plain text content first: Before attempting to parse specific job details, extract the full plain text content of each job detail page using methods like page.inner_text() or page.text_content(). This ensures all information is captured even if the page structure is complex, uses dynamic rendering, or has unconventional layouts.

3. Only generate JSON when confident: Do NOT generate a JSON object unless you are certain that:
   - The job offer URL is valid and correctly extracted
   - You have successfully accessed the actual job details page
   - All required fields contain legitimate, verified data
   If uncertain about any job offer, skip it entirely rather than generating incomplete or incorrect data. Better to return fewer valid entries than include questionable data.

For each job offer, go to its details page and extract:
- Company name
- Position title
- Salary (or "Not specified")
- Location
- Job link (the actual detail page URL, intelligently extracted)
- Key requirements/skills
- Company description (or "Not available")

Return the final data using this JSON structure:
```
{{
  "Source": "{source_name}",
  "Link": "[valid job detail page URL]",
  "Company": "[company name]",
  "Position": "[position title]",
  "Salary": "[salary or 'Not specified']",
  "Location": "[location]",
  "Notes": "Junior Python developer position",
  "Requirements": "[key skills]",
  "About company": "[description or 'Not available']"
}}
"""
