# Job Scraper Implementation Plan

## Project Overview

Build a job scraper that loads sources from Airtable and automatically extracts job postings using AI-powered web scraping. Development follows an iterative approach with each phase building on the previous one.

---

## Project Structure

```
jobscraper/
├── .env                    # API keys and secrets
├── main.py                 # Current phase entry point
├── sources_loader.py       # Phase 2+: Load sources from Airtable
├── simple_scraper.py       # Phase 3: Basic web scraping
├── generic_scraper.py      # Phase 4: AI-powered generic scraping
└── README.md              # Usage instructions
```

---

## Phase 1: Dummy Data MVP

### Goal
Verify Airtable connection and field mapping with fake data.

### Setup Steps
1. Create conda environment: `conda create -n venv-jobbuddy python=3.11 -y`
2. Install dependencies: `pip install python-dotenv rich`
3. Install Agents SDK: `pip install git+https://github.com/openai/openai-agents-python`
4. Create `.env` with `OPENAI_API_KEY`, `AIRTABLE_API_KEY`, `AIRTABLE_BASE_ID`

### Implementation Tasks
1. **Create `main.py`** with dummy job data array (2-3 sample jobs)
2. **Add Airtable MCP connection** using `MCPServerStdio`
3. **Create insertion function** that uses Agent to call `create_record` for each dummy job
4. **Use exact field mapping**: source, link, company, position, CV sent, salary, location, notes, date applied, requirements, about company, Local/Rem/Hyb
5. **Add rich console output** to show progress and results

### Success Criteria
- [ ] Script runs without errors
- [ ] 2-3 dummy jobs appear in Airtable "offers" table
- [ ] All fields populated correctly
- [ ] Console shows clear progress

---

## Phase 2: Dynamic Sources

### Goal
Load job sources dynamically from Airtable "sources" table instead of hardcoding.

### Prerequisites
- Phase 1 completed successfully
- "sources" table exists in Airtable with fields: Source/Name, Link/URL

### Implementation Tasks
1. **Create `sources_loader.py`** 
   - Function to connect to Airtable MCP
   - Agent that calls `list_records` on "sources" table
   - Parse response to extract source names and URLs
2. **Update `main.py`**
   - Import and call source loader
   - Create dummy jobs based on loaded sources (1 job per source)
   - Display loaded sources in a table format
3. **Add error handling** for missing sources table or empty results

### Success Criteria
- [ ] Sources loaded from Airtable without hardcoding
- [ ] Dummy jobs created using actual source names
- [ ] Console shows source count and names
- [ ] Works with any number of sources in table

---

## Phase 3: Simple Web Scraping

### Goal
Replace dummy data with real scraped jobs from one specific job board.

### Prerequisites
- Phase 2 completed successfully
- Install Playwright: `pip install playwright && playwright install chromium`

### Implementation Tasks
1. **Create `simple_scraper.py`**
   - Focus on JustJoin.it as first target
   - Use Playwright to navigate and extract jobs
   - Extract: title, company, location, job link
   - Handle basic errors (page load, missing elements)
   - Limit to first 5-10 jobs for testing
2. **Update job insertion logic**
   - Replace dummy job creation with real scraper calls
   - Map scraped fields to Airtable schema
   - Add scraping timestamp to notes field
3. **Add basic validation**
   - Check required fields are not empty
   - Ensure URLs are properly formatted
   - Skip jobs with missing critical data

### Success Criteria
- [ ] Scrapes real jobs from JustJoin.it
- [ ] Extracts minimum viable fields (title, company, location, link)
- [ ] Inserts scraped jobs into Airtable
- [ ] Handles errors without crashing

---

## Phase 4: Generic AI Scraping

### Goal
Make scraper work with any job board URL using AI to understand page structure.

### Prerequisites
- Phase 3 completed successfully
- Playwright MCP server setup working

### Implementation Tasks
1. **Create `generic_scraper.py`**
   - Start both Playwright and Airtable MCP servers
   - For each source URL, create dynamic Agent with instructions
   - Agent navigates to URL and analyzes page structure
   - Agent extracts jobs and calls `create_record` for each
2. **Design AI prompt template**
   - Instructions to navigate to job board URL
   - Extract job listings from any page structure
   - Map extracted data to Airtable fields
   - Handle different site layouts automatically
3. **Add source processing loop**
   - Process sources sequentially with delays
   - Track success/failure per source
   - Display progress and job counts

### Success Criteria
- [ ] Works with multiple job boards without code changes
- [ ] AI adapts to different page structures automatically
- [ ] Processes all sources from Airtable
- [ ] Shows clear progress and results

---

## Phase 5: Production Features

### Goal
Add duplicate detection, error handling, and production-ready features.

### Implementation Tasks
1. **Duplicate Detection**
   - Create SQLite cache for job fingerprints
   - Generate fingerprints from title+company+location
   - Skip jobs already processed
2. **Error Handling & Logging**
   - Comprehensive try/catch blocks
   - Log errors to file with timestamps
   - Continue processing other sources if one fails
3. **CLI Interface**
   - Add command-line options (--dry-run, --verbose, --sources)
   - Configuration file for settings
   - Help and usage documentation
4. **Performance Optimization**
   - Add delays between requests
   - Batch Airtable insertions
   - Memory management for large datasets

### Success Criteria
- [ ] No duplicate jobs inserted
- [ ] Graceful error handling and recovery
- [ ] Professional CLI interface
- [ ] Production-ready performance

---

## Development Guidelines

### Each Phase Should:
1. **Be fully functional** - each phase produces a working system
2. **Build incrementally** - reuse code from previous phases
3. **Have clear success criteria** - you know when it's done
4. **Be testable** - verify each feature works before moving on

### Best Practices:
- **Test frequently** - run after each major change
- **Keep backups** - commit working code before big changes
- **Start simple** - add complexity gradually
- **Use rich console** - clear progress indicators and error messages
- **Handle errors gracefully** - don't let one failure crash everything

### Environment Variables Needed:
```
OPENAI_API_KEY=your_openai_key
AIRTABLE_API_KEY=your_airtable_token
AIRTABLE_BASE_ID=your_base_id
OPENAI_MODEL=gpt-4o-mini
```

### Airtable Setup Requirements:
- **Base name**: "JOB SEARCH"
- **Tables**: "sources" (for URLs), "offers" (for jobs)
- **Offers table fields**: source, link, company, position, CV sent, salary, location, notes, date applied, requirements, about company, Local/Rem/Hyb

---
