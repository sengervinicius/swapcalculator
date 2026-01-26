# 🚀 SETUP INSTRUCTIONS

## Quick Start

### 1. Install the new dependency
```bash
pip install httpx==0.27.0
```

Or install everything fresh:
```bash
pip install -r requirements.txt
```

### 2. Replace your files

Your folder should look like this:
```
Hedge Software/
├── app.py              ← Replace with new app.py
├── requirements.txt    ← Replace with new requirements.txt
├── launcher.bat        ← Keep as is
└── static/
    └── index.html      ← Replace with new index.html
```

### 3. Start the server
```bash
python app.py
```

### 4. Open your browser
Go to: http://localhost:8000

---

## What's New

### ✅ Live Data Integration
- **BRL**: ANBIMA API (your credentials are already built-in!)
- **USD**: FRED API (optional - see below)
- **EUR**: ECB API (no key needed)
- **GBP**: Bank of England API (no key needed)

### ✅ Status Indicators
The top bar shows colored dots:
- 🟢 Green = Live data connected
- 🟡 Yellow = Using fallback data

### ✅ Live Badges
When data comes from live APIs, you'll see a green "LIVE" badge next to it.

---

## Optional: Get FRED API Key (Free)

To get live US data (Fed Funds, SOFR, Treasury yields):

1. Go to: https://fred.stlouisfed.org/docs/api/api_key.html
2. Click "Request API Key"
3. Create an account (takes 2 minutes)
4. Copy your 32-character key

Then set it before running:
```bash
# Windows
set FRED_API_KEY=your_key_here
python app.py

# Mac/Linux
export FRED_API_KEY=your_key_here
python app.py
```

Or add it directly to `app.py` line 62:
```python
FRED_API_KEY = os.getenv("FRED_API_KEY", "your_key_here")
```

---

## Your ANBIMA Credentials

Already configured in app.py:
- Client ID: UTZ0M5ZlzVjS
- Client Secret: Ivydwruxb0B3

The app will automatically authenticate and fetch live SELIC and DI curve data!

---

## Troubleshooting

### "httpx not installed" message
Run: `pip install httpx==0.27.0`

### APIs not connecting
- Check your internet connection
- The app will use fallback data if APIs are unavailable
- Look at the console for ✅ or ❌ messages

### "Frontend not found"
Make sure you have:
- A folder called `static` in the same directory as `app.py`
- A file called `index.html` inside that `static` folder

---

## Testing

1. Select **BRL** as base currency
2. Select **SELIC** as indexer
3. You should see "[Live]" in the dropdown if ANBIMA is connected
4. Select **USD** as target currency
5. Click Calculate
6. Check the results - assumptions will show "Live API" sources

Enjoy! 🎉

