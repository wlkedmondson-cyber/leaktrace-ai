# LeakTrace AI V1

Standalone Flask starter build for an AI-assisted water intrusion investigation platform.

## Modes
- Consumer
- Contractor
- Insurance

## What is included
- Premium landing page
- Guided investigation wizard
- SQLite database schema
- Photo upload handling
- AI-ready diagnosis service
- Results dashboard
- Feedback capture
- Modern dark UI

## Quick Start

```bash
cd leaktrace_ai_v1
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

## Optional AI Setup

Create a `.env` file:

```env
OPENAI_API_KEY=your_key_here
```

The app currently includes a rule-based fallback so it runs even without an API key.
