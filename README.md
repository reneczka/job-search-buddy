# Job Search Buddy

An intelligent job scraping application that automatically collects and organizes job listings from Polish job boards and company career sites. Built with OpenAI Agents SDK and Playwright MCP, this tool scrapes job postings, extracts structured data, and syncs results to Airtable for easy management and tracking.

## Table of Contents

- [About the Project](#about-the-project)
- [Features](#features)
- [Technologies Used](#technologies-used)
- [Project Structure](#project-structure)
- [Setup and Installation](#setup-and-installation)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
- [Usage](#usage)
  - [Running the Scraper](#running-the-scraper)
  - [Cleaning Duplicates](#cleaning-duplicates)
- [Configuration](#configuration)
- [Future Improvements](#future-improvements)

## About the Project

Job Search Buddy automates the tedious process of searching for developer jobs across multiple Polish job boards. It uses AI-powered web scraping to intelligently navigate job sites, extract relevant information, and organize everything in Airtable. The project features concurrent scraping for efficiency, smart duplicate detection, and structured data extraction.

## Features

- **AI-Powered Scraping**: Uses OpenAI Agents SDK to intelligently navigate and extract job data
- **Dual Pipeline Support**: `main.py` orchestrates a job boards phase and a career sites phase for complete coverage
- **Airtable Integration**: Automatically syncs job listings to Airtable with duplicate detection
- **Extended Data Extraction**: Career site scraping captures additional details like teams, employment types, and benefits when available
- **Duplicate Cleanup**: Built-in tool to identify and remove duplicate job listings
- **Configurable**: Easy configuration for different models, rate limits, and scraping behavior

## Technologies Used

- **Python 3.11+**: Core programming language
- **OpenAI Agents SDK**: AI-driven web scraping and data extraction
- **Playwright MCP**: Browser automation for web scraping
- **Airtable API**: Data storage and organization via pyairtable
- **Rich**: Beautiful console output and progress tracking
- **AsyncIO**: Concurrent scraping for improved performance
- **python-dotenv**: Environment variable management

## Project Structure

```
job-search-buddy/
├── jobscraper/
│   ├── sources_loader.py - Loads job sources from Airtable
│   └── src/
│       ├── main.py - Orchestrates job boards and career sites scraping
│       ├── job_boards_scraper.py - Standalone job board scraper entry point
│       ├── career_sites_scraper.py - Standalone career site scraper entry point
│       ├── agent_runner.py - Agent creation and execution logic
│       ├── server_manager.py - Playwright MCP server management
│       ├── airtable_client.py - Airtable API integration
│       ├── cleanup_duplicates.py - Duplicate removal utility
│       ├── config.py - Configuration constants
│       ├── environment_setup.py - Environment validation
│       ├── mcp_utils.py - MCP utility functions
│       └── prompts.py - Agent instruction templates
├── pyproject.toml - Project metadata and dependencies
├── poetry.lock - Locked dependency versions
├── .env - Environment variables (not tracked)
└── README.md - Project documentation
```

## Setup and Installation

### Prerequisites

- Python 3.11 or higher
- [Poetry](https://python-poetry.org/) (dependency and virtualenv management)
- Node.js and npm (for Playwright MCP)
- Airtable account with API access
- OpenAI API key (or OpenRouter for alternative models)

### Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/reneczka/job-search-buddy.git
   cd job-search-buddy
   ```

2. **Install Python dependencies with Poetry:**

   ```bash
   poetry install
   ```

3. **(Optional) Activate the Poetry virtual environment:**

   ```bash
   poetry shell
   ```

   Alternatively, prefix commands with `poetry run` (shown below).

4. **Set up environment variables:**

   Create a `.env` file in the project root:

   ```env
   # OpenAI Configuration
   OPENAI_API_KEY=your_api_key_here
   OPENAI_API_BASE=https://openrouter.ai/api/v1  # Optional: for OpenRouter
   OPENAI_MODEL=gpt-4o-mini  # Or your preferred model
   
   # Airtable Configuration
   AIRTABLE_API_KEY=your_airtable_api_key
   AIRTABLE_BASE_ID=your_base_id
   AIRTABLE_OFFERS_TABLE_ID=your_offers_table_id
   AIRTABLE_SOURCES_TABLE_ID=your_sources_table_id
   ```

5. **Install Playwright browsers:**

   ```bash
   poetry run playwright install chromium
   ```

## Usage

### Running the Scrapers

1. Navigate to the scraper sources directory:
```bash
cd jobscraper/src
```

2. Choose how you want to run the scrapers:
```bash
poetry run python main.py                   # run both job boards and career sites sequentially
poetry run python job_boards_scraper.py     # scrape job boards only
poetry run python career_sites_scraper.py   # scrape company career sites only
```

Running `main.py` will:
- **Fetch sources**: load the `sources` table from Airtable and detect which columns are populated.
- **Phase 1 — Job Boards**: scrape all records with a `Job Boards` URL concurrently.
- **Phase 2 — Career Sites**: scrape all records with a `Career Sites` URL concurrently using the extended prompt.
- **Sync to Airtable**: merge the combined results, skip duplicates, and create new entries in the `offers` table.
- **Report timings**: print per-phase and total execution timings for transparency.

### Cleaning Duplicates

To remove duplicate job listings from Airtable:

**Dry run (preview only):**
```bash
poetry run python cleanup_duplicates.py --dry-run
```

**Actually delete duplicates:**
```bash
poetry run python cleanup_duplicates.py
```

The cleanup script will:
- Identify duplicate jobs based on normalized URLs
- Keep the most recent posting
- Show you what will be deleted before confirmation
- Delete duplicates in batches

## Configuration

Edit `jobscraper/src/config.py` to customize:

- **Model Selection**: Choose between different AI models
- **Rate Limiting**: Configure API request limits
- **Timeouts**: Adjust browser and MCP timeouts
- **Verbosity**: Enable/disable detailed logging
- **Scraping Behavior**: Modify max turns, delays, etc.

