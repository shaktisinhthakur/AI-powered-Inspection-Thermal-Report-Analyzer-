# DDR Report Generator

An AI-powered system that takes two PDF inputs (Inspection Report + Thermal Report) and generates a structured, client-ready Detailed Diagnostic Report (DDR) as a `.docx` file with embedded images.

Supports **four LLM providers** — Anthropic, OpenAI, Groq, and Gemini — all via **client-supplied API keys** (Bring Your Own Key). No server-side keys are required or shipped.

---

## Quick Start

### Local (venv)

```bash
python -m venv .venv
source .venv/bin/activate    # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 in your browser.

### Docker

```bash
docker build -t ddr-generator .
docker run -p 8000:8000 ddr-generator
```

---

## Multi-Provider BYOK Usage

This app does **not** ship with any API key. Pass your own key for whichever provider you choose via the `X-Provider` and `X-API-Key` headers on **every request**.

### curl Examples

**Anthropic:**
```bash
curl -X POST http://localhost:8000/generate-ddr \
  -H "X-Provider: anthropic" -H "X-API-Key: sk-ant-..." \
  -F "inspection_report=@inspection.pdf" -F "thermal_report=@thermal.pdf"
```

**OpenAI:**
```bash
curl -X POST http://localhost:8000/generate-ddr \
  -H "X-Provider: openai" -H "X-API-Key: sk-..." \
  -F "inspection_report=@inspection.pdf" -F "thermal_report=@thermal.pdf"
```

**Groq:**
```bash
curl -X POST http://localhost:8000/generate-ddr \
  -H "X-Provider: groq" -H "X-API-Key: gsk_..." \
  -F "inspection_report=@inspection.pdf" -F "thermal_report=@thermal.pdf"
```

**Gemini:**
```bash
curl -X POST http://localhost:8000/generate-ddr \
  -H "X-Provider: gemini" -H "X-API-Key: AIza..." \
  -F "inspection_report=@inspection.pdf" -F "thermal_report=@thermal.pdf"
```

### Validate a Key Before Uploading

```bash
curl -X POST http://localhost:8000/validate-key \
  -H "X-Provider: openai" -H "X-API-Key: sk-..."
```

Returns `{ "provider": "openai", "valid": true }` or `{ "provider": "openai", "valid": false }`.

---

## Vision Support by Provider

| Provider  | Default Model              | Vision? | Vision Model (if different)       |
|-----------|----------------------------|---------|-----------------------------------|
| Anthropic | `claude-sonnet-4-20250514` | Yes     | Same                              |
| OpenAI    | `gpt-4o`                   | Yes     | Same                              |
| Groq      | `llama-3.2-90b-vision-preview` | Yes | `llama-3.2-90b-vision-preview`    |
| Gemini    | `gemini-2.0-flash`         | Yes     | Same                              |

If the provider or model combination does not support image input, Stage 3 (image captioning) is skipped gracefully — matched images in the report show "Image Not Available" and the pipeline continues without error.

---

## Architecture

```
                         ┌──────────────┐
                         │   User sends  │
                         │ PDFs + Headers│
                         └──────┬───────┘
                                │
                         ┌──────▼───────┐
                         │   main.py    │  ← validates X-Provider, X-API-Key
                         │  (FastAPI)   │     creates provider instance
                         └──────┬───────┘
                                │
          ┌─────────────────────┼──────────────────────┐
          │                     │                      │
    ┌─────▼──────┐    ┌────────▼───────┐    ┌─────────▼─────────┐
    │  Stage 1   │    │   Stage 2      │    │    Stage 3        │
    │  Parse PDF │    │  Extract Obs   │    │  Caption + Match  │
    │ (PyMuPDF + │    │ (LLM via       │    │  Images (LLM via  │
    │ pdfplumber)│    │  provider)     │    │  provider)        │
    └─────┬──────┘    └────────┬───────┘    └─────────┬─────────┘
          │                    │                      │
          └────────────────────┼──────────────────────┘
                               │
                        ┌──────▼───────┐
                        │   Stage 4    │
                        │  Merge &     │
                        │  Reason      │
                        │ (LLM via     │
                        │  provider)   │
                        └──────┬───────┘
                               │
                        ┌──────▼───────┐
                        │   Stage 5    │
                        │  Render DOCX │
                        │ (python-docx)│
                        └──────┬───────┘
                               │
                        ┌──────▼───────┐
                        │  /output/    │
                        │ {job_id}.docx│
                        └──────────────┘

         ┌──────────────────────────────────────┐
         │      Provider Abstraction Layer      │
         │  (providers.py)                      │
         │                                      │
         │  LLMProvider (ABC)                   │
         │   ├─ AnthropicProvider (tool_use)    │
         │   ├─ OpenAIProvider (json_schema)    │
         │   ├─ GroqProvider (function calling) │
         │   └─ GeminiProvider (response_schema)│
         │                                      │
         │  All return normalized schemas       │
         └──────────────────────────────────────┘
```

---

## API Endpoints

| Method | Path               | Description                                    |
|--------|--------------------|------------------------------------------------|
| POST   | `/generate-ddr`    | Generate DDR from two PDFs                     |
| POST   | `/validate-key`    | Check if an API key is valid for a provider    |
| GET    | `/providers`       | List supported provider names                  |
| GET    | `/download/{job_id}` | Download a generated DDR .docx file          |
| GET    | `/`                | Redirects to the HTML upload form              |

All POST endpoints require `X-Provider` and `X-API-Key` headers.

---

## DDR Output Schema (7 Sections)

1. **Property Issue Summary** — Executive summary of all issues
2. **Area-wise Observations** — Per-area findings with embedded images
3. **Probable Root Cause** — Likely underlying cause(s)
4. **Severity Assessment** — Level + detailed reasoning
5. **Recommended Actions** — Concrete next steps
6. **Additional Notes** — Supplementary context
7. **Missing or Unclear Information** — Explicit "Not Available" list

---

## Error Handling & Reliability

- **Retry with exponential backoff** (max 2 retries) on transient LLM errors (rate limits, timeouts, 5xx).
- **Malformed JSON retry** — Stage 2 retries once with a correction hint if the initial output is invalid.
- **Scanned/no-text PDFs** — logs a warning, tries pdfplumber table fallback, and still attempts image-based fallback via vision captioning.
- **No hardcoded section/area names** — all matching is pattern-based (headings, keywords, image proximity) so the system generalizes to different report formats.
- **API keys never logged, persisted, or echoed** in any response or log.

---

## Known Limitations

- **Image matching is heuristic** — uses page number + keyword overlap scoring. May mismatch images in complex multi-area reports.
- **Scanned/image-only PDFs** rely on vision-only fallback, which is less reliable than text extraction.
- **Output quality varies by provider** — each provider uses a different structured-output mechanism (tool_use, json_schema, function calling, response_schema), so schema adherence and output quality may differ slightly.
- **Large reports** with many images may hit provider token or rate limits.

---

## Project Structure

```
├── app/
│   ├── main.py              # FastAPI routes, BYOK header handling
│   ├── providers.py          # LLM abstraction + 4 provider impls
│   ├── parsing.py            # Stage 1: PDF parsing
│   ├── extraction.py         # Stage 2: structured extraction
│   ├── image_matching.py     # Stage 3: image caption + matching
│   ├── merge_reasoning.py    # Stage 4: merge & reason
│   ├── render.py             # Stage 5: DOCX rendering
│   ├── schemas.py            # Pydantic models + internal dataclasses
│   └── prompts.py            # LLM system prompts
├── static/
│   └── index.html            # Minimal upload form
├── output/                   # Generated .docx files (gitignored)
├── Dockerfile
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```
