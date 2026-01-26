"""
Hedged Return Converter - FastAPI Backend with Live API Integrations
Production-ready with real market data from:
- ANBIMA (Brazil): SELIC, IPCA, DI Curve
- FRED (US): Fed Funds, SOFR, Treasury Yields  
- ECB (Europe): EURIBOR, €STR
- Bank of England (UK): SONIA, Gilt Yields
"""

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import math
import os
import base64
from pathlib import Path
import logging

# Try to import httpx for async HTTP requests
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    print("⚠️  httpx not installed. Run: pip install httpx")
    print("   Using fallback data until httpx is available.")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# APP SETUP
# ============================================================================

app = FastAPI(
    title="Hedged Return Converter API",
    description="CIP-based hedged return calculations with live market data",
    version="2.0.0"
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# CONFIGURATION - API CREDENTIALS
# ============================================================================

class Config:
    """API credentials configuration"""
    
    # ANBIMA (Brazil) - Your credentials
    ANBIMA_CLIENT_ID = os.getenv("ANBIMA_CLIENT_ID", "UTZ0M5ZlzVjS")
    ANBIMA_CLIENT_SECRET = os.getenv("ANBIMA_CLIENT_SECRET", "Ivydwruxb0B3")
    ANBIMA_BASE_URL = "https://api.anbima.com.br"
    
    # FRED (US) - Free API from St. Louis Fed
    FRED_API_KEY = os.getenv("FRED_API_KEY", "0b8a5bfbf530a745acdc11e69c5d32c4")
    FRED_BASE_URL = "https://api.stlouisfed.org/fred"
    
    # ECB (Europe) - No API key required
    ECB_SDMX_URL = "https://data-api.ecb.europa.eu/service/data"
    
    # Bank of England (UK) - No API key required
    BOE_BASE_URL = "http://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp"
    
    # Cache TTL in seconds
    CACHE_TTL = 3600  # 1 hour

config = Config()

# ============================================================================
# DATA MODELS
# ============================================================================

class RiskFreeRateRequest(BaseModel):
    currency: str = Field(..., description="Currency code (BRL, USD, EUR, GBP)")
    tenor: float = Field(..., gt=0, description="Tenor in years")

class CIPCalculationRequest(BaseModel):
    base_currency: str = Field(..., description="Base currency (BRL, USD, EUR, GBP)")
    base_indexer_key: str = Field(..., description="Indexer key (selic, fed-funds, sofr, etc)")
    target_currency: str = Field(..., description="Target currency")
    spread: float = Field(..., ge=0, le=100, description="Spread in % p.a.")
    tenor: float = Field(..., gt=0, le=50, description="Tenor in years")

class AssumptionItem(BaseModel):
    name: str
    value_pp: Optional[float]
    tenor_label: str
    source_name: str

class CIPCalculationResponse(BaseModel):
    ccy_base: str
    ccy_target: str
    tenor_years: float
    as_of_date: str
    indexer_value: float
    spread_value: float
    all_in_base_pp: float
    risk_free_base: float
    risk_free_target: float
    usd_equiv_pp: float
    total_return_target_pp: float
    total_return_base_pp: float
    total_return_pp: float
    assumptions: List[AssumptionItem]
    warnings: List[str]

class RiskFreeRateResponse(BaseModel):
    currency: str
    tenor: float
    rate: float
    source: str

class IndexerResponse(BaseModel):
    key: str
    label: str
    value: float

# ============================================================================
# SIMPLE CACHE
# ============================================================================

class SimpleCache:
    """In-memory cache for API responses"""
    def __init__(self, ttl: int = 3600):
        self._cache: Dict[str, tuple] = {}
        self._ttl = ttl
    
    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            value, timestamp = self._cache[key]
            if datetime.now().timestamp() - timestamp < self._ttl:
                return value
            del self._cache[key]
        return None
    
    def set(self, key: str, value: Any):
        self._cache[key] = (value, datetime.now().timestamp())
    
    def clear(self):
        self._cache.clear()

cache = SimpleCache(ttl=config.CACHE_TTL)

# ============================================================================
# FALLBACK DATA (used when APIs are unavailable)
# ============================================================================

FALLBACK_INDEXERS = {
    'BRL': [
        {'key': 'none', 'label': 'No indexer', 'value': 0.0},
        {'key': 'selic', 'label': 'SELIC (Brazil)', 'value': 14.25},
        {'key': 'ipca', 'label': 'IPCA (Brazil)', 'value': 4.50}
    ],
    'USD': [
        {'key': 'none', 'label': 'No indexer', 'value': 0.0},
        {'key': 'fed-funds', 'label': 'Fed Funds Rate', 'value': 4.33},
        {'key': 'sofr', 'label': 'SOFR (USD)', 'value': 4.30},
        {'key': 'bond-10yr', 'label': '10Y US Treasury', 'value': 4.60}
    ],
    'EUR': [
        {'key': 'none', 'label': 'No indexer', 'value': 0.0},
        {'key': 'euribor', 'label': 'EURIBOR 12M', 'value': 2.50},
        {'key': 'estr', 'label': '€STR', 'value': 2.90}
    ],
    'GBP': [
        {'key': 'none', 'label': 'No indexer', 'value': 0.0},
        {'key': 'gbp-sonia', 'label': 'SONIA', 'value': 4.45}
    ]
}

FALLBACK_RISK_FREE = {
    'BRL': {1: 14.50, 2: 14.25, 3: 14.00, 4: 13.80, 5: 13.60, 10: 13.00},
    'USD': {1: 4.35, 2: 4.20, 3: 4.10, 4: 4.05, 5: 4.00, 10: 4.60},
    'EUR': {1: 2.50, 2: 2.40, 3: 2.30, 4: 2.20, 5: 2.15, 10: 2.50},
    'GBP': {1: 4.40, 2: 4.25, 3: 4.15, 4: 4.05, 5: 4.00, 10: 4.50}
}

# ============================================================================
# API CLIENTS
# ============================================================================

class ANBIMAClient:
    """Client for ANBIMA API (Brazil)"""
    
    def __init__(self):
        self.base_url = config.ANBIMA_BASE_URL
        self.client_id = config.ANBIMA_CLIENT_ID
        self.client_secret = config.ANBIMA_CLIENT_SECRET
        self._access_token = None
        self._token_expires = None
    
    async def _get_access_token(self) -> Optional[str]:
        """Get OAuth2 access token from ANBIMA"""
        if not HTTPX_AVAILABLE:
            return None
            
        if self._access_token and self._token_expires and datetime.now() < self._token_expires:
            return self._access_token
        
        if not self.client_id or not self.client_secret:
            logger.warning("ANBIMA credentials not configured")
            return None
        
        try:
            credentials = f"{self.client_id}:{self.client_secret}"
            encoded = base64.b64encode(credentials.encode()).decode()
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self.base_url}/oauth/access-token",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Basic {encoded}"
                    },
                    json={"grant_type": "client_credentials"}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    self._access_token = data.get("access_token")
                    self._token_expires = datetime.now() + timedelta(seconds=data.get("expires_in", 3600) - 60)
                    logger.info("✅ ANBIMA authentication successful")
                    return self._access_token
                else:
                    logger.error(f"ANBIMA auth failed: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"ANBIMA auth error: {e}")
        
        return None
    
    async def get_selic_estimate(self) -> Optional[float]:
        """Get ANBIMA's daily SELIC rate estimate"""
        cache_key = "anbima_selic"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        
        token = await self._get_access_token()
        if not token:
            return None
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/feed/precos-indices/v1/titulos-publicos/estimativa-selic",
                    headers={"Authorization": f"Bearer {token}"}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data and len(data) > 0:
                        # ANBIMA returns rate as decimal (e.g., 0.1425 for 14.25%)
                        rate_value = data[0].get("taxa") or data[0].get("taxaAnual") or data[0].get("valor")
                        if rate_value:
                            rate = float(rate_value)
                            # If rate is less than 1, it's in decimal form
                            if rate < 1:
                                rate = rate * 100
                            cache.set(cache_key, rate)
                            logger.info(f"✅ ANBIMA SELIC: {rate}%")
                            return rate
        except Exception as e:
            logger.error(f"Error fetching SELIC: {e}")
        
        return None
    
    async def get_di_curve(self, tenor_years: float) -> Optional[float]:
        """Get DI curve rate for specific tenor"""
        cache_key = f"anbima_di_{tenor_years}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        
        token = await self._get_access_token()
        if not token:
            return None
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/feed/precos-indices/v1/titulos-publicos/curvas-juros",
                    headers={"Authorization": f"Bearer {token}"}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data:
                        rate = self._interpolate_curve(data, tenor_years)
                        if rate:
                            cache.set(cache_key, rate)
                            return rate
        except Exception as e:
            logger.error(f"Error fetching DI curve: {e}")
        
        return None
    
    def _interpolate_curve(self, curve_data: List[dict], tenor_years: float) -> Optional[float]:
        """Interpolate curve to get rate at specific tenor"""
        try:
            points = []
            for point in curve_data:
                # ANBIMA uses business days (252 per year)
                prazo = point.get("prazo") or point.get("vertice") or point.get("dias")
                taxa = point.get("taxa") or point.get("taxaAnual")
                if prazo and taxa:
                    tenor = float(prazo) / 252
                    rate = float(taxa)
                    if rate < 1:
                        rate = rate * 100
                    points.append((tenor, rate))
            
            if not points:
                return None
            
            points.sort(key=lambda x: x[0])
            
            # Linear interpolation
            for i in range(len(points) - 1):
                t1, r1 = points[i]
                t2, r2 = points[i + 1]
                if t1 <= tenor_years <= t2:
                    return r1 + (tenor_years - t1) / (t2 - t1) * (r2 - r1)
            
            if tenor_years < points[0][0]:
                return points[0][1]
            return points[-1][1]
        except Exception as e:
            logger.error(f"Curve interpolation error: {e}")
            return None


class FREDClient:
    """Client for FRED API (US Federal Reserve Economic Data)"""
    
    def __init__(self):
        self.base_url = config.FRED_BASE_URL
        self.api_key = config.FRED_API_KEY
    
    async def get_series_latest(self, series_id: str) -> Optional[float]:
        """Get latest value for a FRED series"""
        if not HTTPX_AVAILABLE or not self.api_key:
            return None
            
        cache_key = f"fred_{series_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/series/observations",
                    params={
                        "series_id": series_id,
                        "api_key": self.api_key,
                        "file_type": "json",
                        "sort_order": "desc",
                        "limit": 5
                    }
                )
                
                if response.status_code == 200:
                    data = response.json()
                    for obs in data.get("observations", []):
                        value = obs.get("value")
                        if value and value != ".":
                            rate = float(value)
                            cache.set(cache_key, rate)
                            logger.info(f"✅ FRED {series_id}: {rate}%")
                            return rate
        except Exception as e:
            logger.error(f"Error fetching FRED {series_id}: {e}")
        
        return None
    
    async def get_fed_funds_rate(self) -> Optional[float]:
        return await self.get_series_latest("DFF")
    
    async def get_sofr(self) -> Optional[float]:
        return await self.get_series_latest("SOFR")
    
    async def get_treasury_yield(self, years: int) -> Optional[float]:
        series_map = {1: "DGS1", 2: "DGS2", 3: "DGS3", 5: "DGS5", 7: "DGS7", 10: "DGS10", 20: "DGS20", 30: "DGS30"}
        if years in series_map:
            return await self.get_series_latest(series_map[years])
        return None


class ECBClient:
    """Client for ECB API (European Central Bank)"""
    
    async def get_euribor(self, tenor_months: int = 12) -> Optional[float]:
        if not HTTPX_AVAILABLE:
            return None
            
        cache_key = f"ecb_euribor_{tenor_months}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        
        try:
            tenor_map = {1: "EURIBOR1MD_", 3: "EURIBOR3MD_", 6: "EURIBOR6MD_", 12: "EURIBOR1YD_"}
            tenor_code = tenor_map.get(tenor_months, "EURIBOR1YD_")
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{config.ECB_SDMX_URL}/FM/M.U2.EUR.RT.MM.{tenor_code}.HSTA",
                    params={"lastNObservations": 1, "format": "jsondata"},
                    headers={"Accept": "application/json"}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    datasets = data.get("dataSets", [{}])
                    if datasets:
                        series = datasets[0].get("series", {})
                        for key, series_data in series.items():
                            obs = series_data.get("observations", {})
                            if obs:
                                latest_key = max(obs.keys())
                                rate = obs[latest_key][0]
                                if rate is not None:
                                    cache.set(cache_key, rate)
                                    logger.info(f"✅ ECB EURIBOR {tenor_months}M: {rate}%")
                                    return rate
        except Exception as e:
            logger.error(f"Error fetching EURIBOR: {e}")
        
        return None


class BOEClient:
    """Client for Bank of England API"""
    
    async def get_series(self, series_code: str) -> Optional[float]:
        if not HTTPX_AVAILABLE:
            return None
            
        cache_key = f"boe_{series_code}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        
        try:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=30)
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    config.BOE_BASE_URL,
                    params={
                        "csv.x": "yes",
                        "Datefrom": start_date.strftime("%d/%b/%Y"),
                        "Dateto": end_date.strftime("%d/%b/%Y"),
                        "SeriesCodes": series_code,
                        "CSVF": "TN",
                        "UsingCodes": "Y",
                        "VPD": "Y",
                        "VFD": "N"
                    },
                    headers={"User-Agent": "HedgedReturnConverter/2.0"}
                )
                
                if response.status_code == 200:
                    lines = response.text.strip().split("\n")
                    for line in reversed(lines[1:] if len(lines) > 1 else []):
                        parts = line.split(",")
                        if len(parts) >= 2:
                            try:
                                rate = float(parts[1])
                                cache.set(cache_key, rate)
                                logger.info(f"✅ BoE {series_code}: {rate}%")
                                return rate
                            except (ValueError, IndexError):
                                continue
        except Exception as e:
            logger.error(f"Error fetching BoE {series_code}: {e}")
        
        return None
    
    async def get_sonia(self) -> Optional[float]:
        return await self.get_series("IUDSOIA")


# Initialize clients
anbima_client = ANBIMAClient()
fred_client = FREDClient()
ecb_client = ECBClient()
boe_client = BOEClient()

# ============================================================================
# DATA FETCHING FUNCTIONS
# ============================================================================

async def get_live_indexers(currency: str) -> List[dict]:
    """Get indexer values - live if available, fallback otherwise"""
    currency = currency.upper()
    indexers = [{'key': 'none', 'label': 'No indexer', 'value': 0.0}]
    
    try:
        if currency == 'BRL':
            selic = await anbima_client.get_selic_estimate()
            if selic is not None:
                indexers.append({'key': 'selic', 'label': 'SELIC (Brazil) [Live]', 'value': round(selic, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['BRL'][1])
            indexers.append(FALLBACK_INDEXERS['BRL'][2])  # IPCA - keep fallback for now
        
        elif currency == 'USD':
            fed_funds = await fred_client.get_fed_funds_rate()
            sofr = await fred_client.get_sofr()
            treasury_10y = await fred_client.get_treasury_yield(10)
            
            if fed_funds is not None:
                indexers.append({'key': 'fed-funds', 'label': 'Fed Funds Rate [Live]', 'value': round(fed_funds, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['USD'][1])
            
            if sofr is not None:
                indexers.append({'key': 'sofr', 'label': 'SOFR (USD) [Live]', 'value': round(sofr, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['USD'][2])
            
            if treasury_10y is not None:
                indexers.append({'key': 'bond-10yr', 'label': '10Y US Treasury [Live]', 'value': round(treasury_10y, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['USD'][3])
        
        elif currency == 'EUR':
            euribor = await ecb_client.get_euribor(12)
            if euribor is not None:
                indexers.append({'key': 'euribor', 'label': 'EURIBOR 12M [Live]', 'value': round(euribor, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['EUR'][1])
            indexers.append(FALLBACK_INDEXERS['EUR'][2])  # €STR fallback
        
        elif currency == 'GBP':
            sonia = await boe_client.get_sonia()
            if sonia is not None:
                indexers.append({'key': 'gbp-sonia', 'label': 'SONIA [Live]', 'value': round(sonia, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['GBP'][1])
        
        else:
            return FALLBACK_INDEXERS.get(currency, [])
    
    except Exception as e:
        logger.error(f"Error getting indexers for {currency}: {e}")
        return FALLBACK_INDEXERS.get(currency, [])
    
    return indexers


def interpolate_fallback_rate(currency: str, tenor: float) -> Optional[float]:
    """Linear interpolation from fallback yield curves"""
    if currency not in FALLBACK_RISK_FREE:
        return None
    
    rates = FALLBACK_RISK_FREE[currency]
    
    if tenor in rates:
        return rates[tenor]
    
    keys = sorted(rates.keys())
    
    for i in range(len(keys) - 1):
        t1, t2 = keys[i], keys[i + 1]
        if t1 <= tenor <= t2:
            r1, r2 = rates[t1], rates[t2]
            return r1 + ((tenor - t1) / (t2 - t1)) * (r2 - r1)
    
    if tenor < keys[0]:
        return rates[keys[0]]
    return rates[keys[-1]]


async def get_risk_free_rate(currency: str, tenor: float) -> tuple[Optional[float], str]:
    """Get risk-free rate with source info"""
    currency = currency.upper()
    
    try:
        if currency == 'BRL':
            rate = await anbima_client.get_di_curve(tenor)
            if rate is not None:
                return round(rate, 4), "ANBIMA DI Curve [Live]"
        
        elif currency == 'USD' and config.FRED_API_KEY:
            # Get closest Treasury rate and interpolate
            if tenor <= 1:
                rate = await fred_client.get_treasury_yield(1)
            elif tenor <= 2:
                rate = await fred_client.get_treasury_yield(2)
            elif tenor <= 5:
                rate = await fred_client.get_treasury_yield(5)
            else:
                rate = await fred_client.get_treasury_yield(10)
            
            if rate is not None:
                return round(rate, 4), "US Treasury [Live via FRED]"
    
    except Exception as e:
        logger.error(f"Error getting risk-free rate: {e}")
    
    # Fallback
    rate = interpolate_fallback_rate(currency, tenor)
    if rate is not None:
        return round(rate, 4), "Fallback Data"
    
    return None, "Not Available"

# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.get("/api/", tags=["Info"])
def root():
    """API root"""
    return {
        "service": "Hedged Return Converter API",
        "version": "2.0.0 (Live Data)",
        "documentation": "/docs",
        "status": "✅ Running",
        "data_sources": {
            "BRL": "ANBIMA (configured)" if config.ANBIMA_CLIENT_ID else "Fallback",
            "USD": "FRED (configured)" if config.FRED_API_KEY else "Fallback - Get free key at fred.stlouisfed.org",
            "EUR": "ECB (no key needed)",
            "GBP": "Bank of England (no key needed)"
        }
    }

@app.get("/api/health", tags=["Info"])
def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/api/config/status", tags=["Info"])
def config_status():
    """Check API configuration"""
    return {
        "anbima_configured": bool(config.ANBIMA_CLIENT_ID and config.ANBIMA_CLIENT_SECRET),
        "fred_configured": bool(config.FRED_API_KEY),
        "ecb_configured": True,
        "boe_configured": True,
        "httpx_available": HTTPX_AVAILABLE,
        "fred_signup_url": "https://fred.stlouisfed.org/docs/api/api_key.html"
    }

@app.get("/api/indexers", tags=["Reference Data"])
async def list_all_indexers():
    """List all indexers with live data"""
    result = {}
    for ccy in ['BRL', 'USD', 'EUR', 'GBP']:
        result[ccy] = await get_live_indexers(ccy)
    return result

@app.get("/api/indexers/{currency}", tags=["Reference Data"])
async def get_indexers_by_currency(currency: str):
    """Get indexers for a specific currency"""
    currency = currency.upper()
    if currency not in ['BRL', 'USD', 'EUR', 'GBP']:
        raise HTTPException(status_code=404, detail=f"Currency {currency} not found")
    
    indexers = await get_live_indexers(currency)
    return {
        "currency": currency,
        "indexers": [IndexerResponse(**idx) for idx in indexers]
    }

@app.get("/api/risk-free-rates/{currency}", tags=["Reference Data"])
async def get_risk_free_curve(currency: str):
    """Get risk-free yield curve"""
    currency = currency.upper()
    if currency not in FALLBACK_RISK_FREE:
        raise HTTPException(status_code=404, detail=f"Currency {currency} not found")
    
    return {
        "currency": currency,
        "curve": {str(k): v for k, v in FALLBACK_RISK_FREE[currency].items()},
        "source": "Reference Data"
    }

@app.post("/api/risk-free-rates/interpolate", tags=["Reference Data"], response_model=RiskFreeRateResponse)
async def interpolate_risk_free_rate(request: RiskFreeRateRequest):
    """Interpolate risk-free rate for specific tenor"""
    rate, source = await get_risk_free_rate(request.currency.upper(), request.tenor)
    
    if rate is None:
        raise HTTPException(status_code=404, detail=f"Rate not available for {request.currency} {request.tenor}Y")
    
    return RiskFreeRateResponse(
        currency=request.currency.upper(),
        tenor=request.tenor,
        rate=rate,
        source=source
    )

# ============================================================================
# MAIN CALCULATION ENDPOINT
# ============================================================================

@app.post("/api/calculate/hedged-return", tags=["Calculations"], response_model=CIPCalculationResponse)
async def calculate_hedged_return(request: CIPCalculationRequest):
    """Calculate CIP-based hedged return conversion"""
    
    # Get live indexers
    indexers = await get_live_indexers(request.base_currency)
    
    # Find requested indexer
    base_idx_data = None
    for idx in indexers:
        if idx['key'] == request.base_indexer_key:
            base_idx_data = idx
            break
    
    if base_idx_data is None:
        raise HTTPException(status_code=400, detail="Invalid indexer key")
    
    # Get risk-free rates
    i_base_t, base_source = await get_risk_free_rate(request.base_currency, request.tenor)
    i_target_t, target_source = await get_risk_free_rate(request.target_currency, request.tenor)
    
    if i_base_t is None or i_target_t is None:
        raise HTTPException(status_code=400, detail="Risk-free rates not available")
    
    # ========== CIP CALCULATION ==========
    
    # Step 1: All-in return in base currency
    spread_decimal = request.spread / 100
    base_indexer_decimal = base_idx_data['value'] / 100
    all_in_base_pp = ((1 + base_indexer_decimal) * (1 + spread_decimal) - 1) * 100
    
    # Step 2: CIP formula for target currency equivalent
    i_base_decimal = i_base_t / 100
    i_target_decimal = i_target_t / 100
    all_in_base_decimal = all_in_base_pp / 100
    
    # CIP: Target_Return = (1 + All_In_Base) * (1 + i_target) / (1 + i_base) - 1
    target_equiv_decimal = (1 + all_in_base_decimal) * ((1 + i_target_decimal) / (1 + i_base_decimal)) - 1
    target_equiv_pp = target_equiv_decimal * 100
    
    # Step 3: Compounded total returns over tenor
    total_return_target_decimal = math.pow(1 + target_equiv_decimal, request.tenor) - 1
    total_return_target_pp = total_return_target_decimal * 100

    total_return_base_decimal = math.pow(1 + all_in_base_decimal, request.tenor) - 1
    total_return_base_pp = total_return_base_decimal * 100
    
    # ========== BUILD RESPONSE ==========
    
    is_live = "[Live]" in base_idx_data.get('label', '')
    
    assumptions = [
        AssumptionItem(
            name=base_idx_data['label'],
            value_pp=base_idx_data['value'],
            tenor_label="Spot",
            source_name="Live API" if is_live else "Reference"
        ),
        AssumptionItem(
            name=f"{request.base_currency} Risk-Free ({request.tenor}Y)",
            value_pp=i_base_t,
            tenor_label="Discount Curve",
            source_name=base_source
        ),
        AssumptionItem(
            name=f"{request.target_currency} Risk-Free ({request.tenor}Y)",
            value_pp=i_target_t,
            tenor_label="Discount Curve",
            source_name=target_source
        ),
        AssumptionItem(
            name="Spread",
            value_pp=request.spread,
            tenor_label="User Input",
            source_name="Input"
        )
    ]
    
    warnings = ["✓ CIP-based hedged conversion applied"]
    
    if not is_live or "Fallback" in base_source or "Fallback" in target_source:
        warnings.insert(0, "⚠️ Some data from reference sources (API unavailable)")
    
    return CIPCalculationResponse(
        ccy_base=request.base_currency,
        ccy_target=request.target_currency,
        tenor_years=request.tenor,
        as_of_date=datetime.now().strftime("%Y-%m-%d"),
        indexer_value=base_idx_data['value'],
        spread_value=request.spread,
        all_in_base_pp=round(all_in_base_pp, 4),
        risk_free_base=i_base_t,
        risk_free_target=i_target_t,
        usd_equiv_pp=round(target_equiv_pp, 4),
        total_return_target_pp=round(total_return_target_pp, 4),
        total_return_base_pp=round(total_return_base_pp, 4),
        total_return_pp=round(total_return_target_pp, 4),
        assumptions=assumptions,
        warnings=warnings
    )

@app.post("/api/cache/clear", tags=["Admin"])
def clear_cache():
    """Clear cached data"""
    cache.clear()
    return {"status": "Cache cleared", "timestamp": datetime.now().isoformat()}

# ============================================================================
# FRONTEND SERVING
# ============================================================================

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/")
def serve_frontend():
    """Serve the HTML frontend"""
    index_file = Path(__file__).parent / "static" / "index.html"
    if index_file.exists():
        return FileResponse(index_file, media_type="text/html")
    else:
        return {
            "message": "Frontend not found",
            "instructions": "Create static/index.html file",
            "api_docs": "Visit /docs for API documentation"
        }

# ============================================================================
# STARTUP
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*60)
    print("🚀 Hedged Return Converter - LIVE DATA VERSION")
    print("="*60)
    print("\n📍 Frontend: http://localhost:8000")
    print("📍 API Docs: http://localhost:8000/docs")
    print("\n⚙️  Configuration:")
    print(f"   ANBIMA: {'✓ Configured' if config.ANBIMA_CLIENT_ID else '✗ Not configured'}")
    print(f"   FRED:   {'✓ Configured' if config.FRED_API_KEY else '✗ Not configured'}")
    print(f"           Get free key: https://fred.stlouisfed.org/docs/api/api_key.html")
    print(f"   ECB:    ✓ No key needed")
    print(f"   BoE:    ✓ No key needed")
    print(f"   httpx:  {'✓ Installed' if HTTPX_AVAILABLE else '✗ Not installed - run: pip install httpx'}")
    print("\nPress CTRL+C to stop\n")
    print("="*60 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
