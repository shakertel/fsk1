# FSK — Film Searcher Kurdistan

Deploy as a Render Background Worker.

Required environment variables:
TELEGRAM_TOKEN
SUPABASE_URL
SUPABASE_SECRET_KEY
GEMINI_KEY_1 ... GEMINI_KEY_14

Build:
pip install -r requirements.txt

Start:
python main.py

The search engine is based on the V7 recommendation flow:
experience detection, 40/60 scoring, direct-experience star logic,
candidate deduplication, final ranking up to 100, and IMDb as display-only.
Telegram shows 3 results at a time and includes the stored story description.
A stuck search is released after 15 minutes.
