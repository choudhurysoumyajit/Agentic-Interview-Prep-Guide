# Agentic Interview Prep Guide

Generate a prioritized interview preparation guide from web pages and YouTube videos. The app extracts technical questions, consolidates duplicates, ranks questions by observed frequency, rewrites answers in plain language, creates a question-specific diagram or image, and produces a Word document.

## Features

- Web research through Tavily.
- YouTube search through `yt-dlp`.
- YouTube transcript retrieval through `youtube-transcript-api`, `yt-dlp` captions, and Tavily URL extraction as fallbacks.
- OpenRouter LLM extraction, plain-language explanations, and diagram generation.
- Separate source quotas: up to `N` non-YouTube webpages and `N` YouTube links per technology.
- Optional company name. Leave it blank for general technology interview preparation.
- Frequency-based importance scores from 1 to 10.
- Configurable maximum number of questions in the final document.
- Royal-blue workflow status cards with progress and completion checks.
- Reset button and browser refresh cleanup for generated visuals and Word documents.
- Reset and refresh cancellation of an active analysis at the next safe processing point.

## Requirements

- Python 3.12 or newer.
- `uv`.
- A Graphviz installation available on `PATH` for diagram rendering. If Graphviz cannot render a diagram, the app attempts a Tavily image fallback.
- OpenRouter and Tavily API keys.

## Setup

Install the project dependencies:

```bash
uv sync
```

Create a `.env` file in the project root:

```env
OPENROUTER_API_KEY=your_openrouter_api_key
TAVILY_API_KEY=your_tavily_api_key
OPENROUTER_MODEL=openai/gpt-oss-20b
OPENROUTER_SITE_URL=http://localhost
OPENROUTER_APP_NAME=Agentic Interview Prep Guide
```

`OPENROUTER_MODEL` is optional. A free OpenRouter model can be used, but free models have shared provider limits and may return HTTP 429 rate-limit errors. A paid or more stable model is recommended for larger runs.

## Run

For normal use:

```bash
uv run streamlit run app.py
```

For long-running analysis while the Mac is locked, keep the laptop connected to power and use:

```bash
caffeinate -dimsu uv run streamlit run app.py
```

Screen locking is allowed. `caffeinate` prevents idle sleep from suspending the process and network connection.

## Using the App

1. Enter one or more technologies, such as `Azure Data Factory`, `ADF`, `Databricks`, or `Python`.
2. Optionally enter one or more company names. Leave the field blank for general interview questions.
3. Set the webpage and YouTube source count per technology.
4. Set the maximum number of questions for the final document.
5. Click **Generate Interview Guide**.
6. Download the generated Word document when the workflow completes.

Questions are ordered from highest to lowest importance. Importance is estimated from how often similar questions appear across the collected sources; it is not a guarantee of what a specific employer will ask.

## Research Flow

```text
Web research       YouTube research
	 \             /
	  Consolidate and rank
		     |
	  Plain-language answers
		     |
	 Question-specific visuals
		     |
	     Word document
```

The app reports how many questions were identified from web and YouTube sources. If no questions survive extraction and filtering, no Word document is created and the UI suggests trying another company, technology, or generalized search.

## Reset and Cancellation

The **Reset inputs and visuals** button clears the form, cancels an active pipeline at the next safe checkpoint, deletes generated diagrams/images, and removes generated `interview_prep_*.docx` files.

A browser refresh performs the same reset behavior. An API request already in progress may finish before cancellation is detected; the Streamlit server itself is kept alive so the app can be used again.

## Project Files

- `app.py`: Streamlit user interface and workflow display.
- `backend.py`: provider integrations, extraction, consolidation, visualization, cancellation, and document generation.
- `outputs/`: generated Word documents.
- `outputs/assets/`: generated diagrams and fallback images.
- `pyproject.toml`: project metadata and dependencies.
