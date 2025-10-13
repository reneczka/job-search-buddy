# Job Search Buddy

An intelligent job scraping application that automatically collects and organizes job listings from Polish job boards. Built with OpenAI Agents SDK and Playwright MCP, this tool scrapes job postings, extracts structured data, and syncs results to Airtable for easy management and tracking.

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
- **Multi-Source Support**: Scrapes from multiple job boards (JustJoin.it, NoFluffJobs, Pracuj.pl, etc.)
- **Concurrent Processing**: Scrapes multiple sources simultaneously for faster results
- **Airtable Integration**: Automatically syncs job listings to Airtable with duplicate detection
- **Smart Data Extraction**: Extracts company, position, salary, location, requirements, and more
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
│       ├── main.py - Main application entry point
│       ├── agent_runner.py - Agent creation and execution logic
│       ├── server_manager.py - Playwright MCP server management
│       ├── airtable_client.py - Airtable API integration
│       ├── cleanup_duplicates.py - Duplicate removal utility
│       ├── config.py - Configuration constants
│       ├── environment_setup.py - Environment validation
│       ├── mcp_utils.py - MCP utility functions
│       └── prompts.py - Agent instruction templates
├── requirements.txt - Python dependencies
├── .env - Environment variables (not tracked)
└── README.md - Project documentation
```

## Setup and Installation

### Prerequisites

- Python 3.11 or higher
- Conda/Anaconda (recommended for environment management)
- Node.js and npm (for Playwright MCP)
- Airtable account with API access
- OpenAI API key (or OpenRouter for alternative models)

### Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/reneczka/job-search-buddy.git
   cd job-search-buddy
   ```

2. **Create and activate conda environment:**

   ```bash
   conda create -n job-search-buddy python=3.11
   conda activate job-search-buddy
   ```

3. **Install Python dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

4. **Install OpenAI Agents SDK:**

   ```bash
   pip install git+https://github.com/openai/openai-agents-python
   ```

5. **Set up environment variables:**

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

6. **Install Playwright browsers:**

   ```bash
   playwright install chromium
   ```

## Usage

### Running the Scraper

Navigate to the source directory and run the main script:

```bash
cd jobscraper/src
conda activate job-search-buddy
python3 main.py
```

The scraper will:
1. Fetch job sources from Airtable
2. Scrape each source concurrently
3. Extract structured job data
4. Sync results to Airtable (skipping duplicates)
5. Display timing statistics

### Cleaning Duplicates

To remove duplicate job listings from Airtable:

**Dry run (preview only):**
```bash
python3 cleanup_duplicates.py --dry-run
```

**Actually delete duplicates:**
```bash
python3 cleanup_duplicates.py
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

