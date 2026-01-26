"""
Hedged Return Converter - FastAPI Backend with Live API Integrations
Production-ready with real market data from:
- BCB (Brazil): SELIC Target Rate (FREE, no auth!)
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
from pathlib import Path
import logging

# Try to import httpx for async HTTP requests
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    print("⚠️  httpx not installed. Run: pip install httpx")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# APP SETUP
# ============================================================================

app = FastAPI(
    title="Hedged Return Converter API",
    description="CIP-based hedged return calculations with live market data",
    version="2.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    # BCB (Brazil) - FREE, no authentication needed!
    BCB_BASE_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs"
    
    # FRED (US) - Your API key
    FRED_API_KEY = os.getenv("FRED_API_KEY", "0b8a5bfbf530a745acdc11e69c5d32c4")
    FRED_BASE_URL = "https://api.stlouisfed.org/fred"
    
    # ECB (Europe) - No API key required
    ECB_SDMX_URL = "https://data-api.ecb.europa.eu/service/data"
    
    # Bank of England (UK) - No API key required
    BOE_BASE_URL = "http://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp"
    
    # Cache TTL
    CACHE_TTL = 1800  # 30 minutes

config = Config()

# ============================================================================
# DATA MODELS
# ============================================================================

class RiskFreeRateRequest(BaseModel):
    currency: str = Field(..., description="Currency code")
    tenor: float = Field(..., gt=0, description="Tenor in years")

class CIPCalculationRequest(BaseModel):
    base_currency: str
    base_indexer_key: str
    target_currency: str
    spread: float = Field(..., ge=0, le=100)
    tenor: float = Field(..., gt=0, le=50)

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
# CACHE
# ============================================================================

class SimpleCache:
    def __init__(self, ttl: int = 1800):
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
# FALLBACK DATA (current as of Jan 2025)
# ============================================================================

FALLBACK_INDEXERS = {
    'BRL': [
        {'key': 'none', 'label': 'No indexer', 'value': 0.0},
        {'key': 'selic', 'label': 'SELIC (Brazil)', 'value': 13.25},
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
    ],
    'CHF': [
        {'key': 'none', 'label': 'No indexer', 'value': 0.0},
        {'key': 'saron', 'label': 'SARON (Swiss)', 'value': 0.45},
        {'key': 'snb-rate', 'label': 'SNB Policy Rate', 'value': 0.50}
    ],
    'JPY': [
        {'key': 'none', 'label': 'No indexer', 'value': 0.0},
        {'key': 'tonar', 'label': 'TONAR (Japan)', 'value': 0.23},
        {'key': 'boj-rate', 'label': 'BoJ Policy Rate', 'value': 0.25}
    ],
    'CNY': [
        {'key': 'none', 'label': 'No indexer', 'value': 0.0},
        {'key': 'lpr-1y', 'label': 'LPR 1Y (China)', 'value': 3.10},
        {'key': 'lpr-5y', 'label': 'LPR 5Y (China)', 'value': 3.60}
    ],
    'CAD': [
        {'key': 'none', 'label': 'No indexer', 'value': 0.0},
        {'key': 'corra', 'label': 'CORRA (Canada)', 'value': 3.20},
        {'key': 'boc-rate', 'label': 'BoC Policy Rate', 'value': 3.25}
    ],
    'AUD': [
        {'key': 'none', 'label': 'No indexer', 'value': 0.0},
        {'key': 'aonia', 'label': 'AONIA (Australia)', 'value': 4.10},
        {'key': 'rba-rate', 'label': 'RBA Cash Rate', 'value': 4.10}
    ]
}

FALLBACK_RISK_FREE = {
    'BRL': {1: 15.00, 2: 14.80, 3: 14.60, 5: 14.20, 10: 13.50},
    'USD': {1: 4.35, 2: 4.20, 3: 4.10, 5: 4.00, 10: 4.60},
    'EUR': {1: 2.50, 2: 2.40, 3: 2.30, 5: 2.15, 10: 2.50},
    'GBP': {1: 4.40, 2: 4.25, 3: 4.15, 5: 4.00, 10: 4.50},
    'CHF': {1: 0.50, 2: 0.55, 3: 0.60, 5: 0.70, 10: 0.85},
    'JPY': {1: 0.40, 2: 0.55, 3: 0.65, 5: 0.80, 10: 1.10},
    'CNY': {1: 1.80, 2: 1.90, 3: 2.00, 5: 2.20, 10: 2.50},
    'CAD': {1: 3.20, 2: 3.10, 3: 3.00, 5: 2.90, 10: 3.20},
    'AUD': {1: 4.10, 2: 4.00, 3: 3.95, 5: 3.90, 10: 4.20}
}

# ============================================================================
# API CLIENTS
# ============================================================================

class BCBClient:
    """Client for Banco Central do Brasil API - FREE, no auth needed!"""
    
    async def get_series_latest(self, series_code: int) -> Optional[float]:
        """Get latest value from BCB SGS series"""
        if not HTTPX_AVAILABLE:
            return None
            
        cache_key = f"bcb_{series_code}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        
        try:
            url = f"{config.BCB_BASE_URL}.{series_code}/dados/ultimos/1?formato=json"
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                
                if response.status_code == 200:
                    data = response.json()
                    if data and len(data) > 0:
                        value = float(data[0].get("valor", 0))
                        cache.set(cache_key, value)
                        logger.info(f"✅ BCB series {series_code}: {value}")
                        return value
        except Exception as e:
            logger.error(f"BCB API error for series {series_code}: {e}")
        
        return None
    
    async def get_selic_target(self) -> Optional[float]:
        """Get SELIC Target Rate (Meta SELIC)"""
        return await self.get_series_latest(432)
    
    async def get_ipca(self) -> Optional[float]:
        """Get latest IPCA (monthly)"""
        return await self.get_series_latest(433)


class FREDClient:
    """Client for FRED API"""
    
    async def get_series_latest(self, series_id: str) -> Optional[float]:
        if not HTTPX_AVAILABLE or not config.FRED_API_KEY:
            return None
            
        cache_key = f"fred_{series_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{config.FRED_BASE_URL}/series/observations",
                    params={
                        "series_id": series_id,
                        "api_key": config.FRED_API_KEY,
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
            logger.error(f"FRED error {series_id}: {e}")
        
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
    """Client for ECB API"""
    
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
                                    logger.info(f"✅ ECB EURIBOR: {rate}%")
                                    return rate
        except Exception as e:
            logger.error(f"ECB error: {e}")
        
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
            
            # Use the correct BoE IADB URL
            url = "https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp"
            
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    url,
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
                    headers={"User-Agent": "CrossFXYield/2.3"},
                    follow_redirects=True
                )
                
                if response.status_code == 200:
                    text = response.text.strip()
                    lines = text.split("\n")
                    # Parse CSV - format is: DATE, VALUE
                    for line in reversed(lines):
                        if not line.strip() or line.startswith("DATE"):
                            continue
                        parts = line.split(",")
                        if len(parts) >= 2:
                            try:
                                value_str = parts[1].strip()
                                if value_str and value_str != ".." and value_str != ".":
                                    rate = float(value_str)
                                    cache.set(cache_key, rate)
                                    logger.info(f"✅ BoE {series_code}: {rate}%")
                                    return rate
                            except (ValueError, IndexError):
                                continue
        except Exception as e:
            logger.error(f"BoE error for {series_code}: {e}")
        
        return None
    
    async def get_sonia(self) -> Optional[float]:
        """Get SONIA rate - try FRED first (more reliable), then BoE"""
        # SONIA is also on FRED as IUDSOIA
        if config.FRED_API_KEY:
            rate = await fred_client.get_series_latest("IUDSOIA")
            if rate is not None:
                return rate
        # Fallback to BoE direct
        return await self.get_series("IUDSOIA")


# Initialize clients
bcb_client = BCBClient()
fred_client = FREDClient()
ecb_client = ECBClient()
boe_client = BOEClient()

# ============================================================================
# DATA FETCHING
# ============================================================================

async def get_live_indexers(currency: str) -> List[dict]:
    """Get indexer values with live data"""
    currency = currency.upper()
    indexers = [{'key': 'none', 'label': 'No indexer', 'value': 0.0}]
    
    try:
        if currency == 'BRL':
            selic = await bcb_client.get_selic_target()
            if selic is not None:
                indexers.append({'key': 'selic', 'label': 'SELIC Target [Live]', 'value': round(selic, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['BRL'][1])
            indexers.append(FALLBACK_INDEXERS['BRL'][2])  # IPCA fallback
        
        elif currency == 'USD':
            fed_funds = await fred_client.get_fed_funds_rate()
            sofr = await fred_client.get_sofr()
            treasury_10y = await fred_client.get_treasury_yield(10)
            
            if fed_funds is not None:
                indexers.append({'key': 'fed-funds', 'label': 'Fed Funds Rate [Live]', 'value': round(fed_funds, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['USD'][1])
            
            if sofr is not None:
                indexers.append({'key': 'sofr', 'label': 'SOFR [Live]', 'value': round(sofr, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['USD'][2])
            
            if treasury_10y is not None:
                indexers.append({'key': 'bond-10yr', 'label': '10Y Treasury [Live]', 'value': round(treasury_10y, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['USD'][3])
        
        elif currency == 'EUR':
            euribor = await ecb_client.get_euribor(12)
            if euribor is not None:
                indexers.append({'key': 'euribor', 'label': 'EURIBOR 12M [Live]', 'value': round(euribor, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['EUR'][1])
            indexers.append(FALLBACK_INDEXERS['EUR'][2])
        
        elif currency == 'GBP':
            sonia = await boe_client.get_sonia()
            if sonia is not None:
                indexers.append({'key': 'gbp-sonia', 'label': 'SONIA [Live]', 'value': round(sonia, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['GBP'][1])
        
        elif currency == 'CHF':
            # Swiss short-term rate from FRED (OECD data)
            chf_short = await fred_client.get_series_latest("IRSTCI01CHM156N")
            if chf_short is not None:
                indexers.append({'key': 'saron', 'label': 'CHF Short Rate [Live]', 'value': round(chf_short, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['CHF'][1])
            indexers.append(FALLBACK_INDEXERS['CHF'][2])
        
        elif currency == 'JPY':
            # Japan policy rate from FRED
            boj_rate = await fred_client.get_series_latest("IRSTCI01JPM156N")
            if boj_rate is not None:
                indexers.append({'key': 'tonar', 'label': 'Japan Short Rate [Live]', 'value': round(boj_rate, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['JPY'][1])
            indexers.append(FALLBACK_INDEXERS['JPY'][2])
        
        elif currency == 'CNY':
            # China rates - using fallback (PBOC doesn't have easy API)
            indexers.append(FALLBACK_INDEXERS['CNY'][1])
            indexers.append(FALLBACK_INDEXERS['CNY'][2])
        
        elif currency == 'CAD':
            # Canada overnight rate from FRED
            corra = await fred_client.get_series_latest("IRSTCI01CAM156N")
            if corra is not None:
                indexers.append({'key': 'corra', 'label': 'Canada Rate [Live]', 'value': round(corra, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['CAD'][1])
            indexers.append(FALLBACK_INDEXERS['CAD'][2])
        
        elif currency == 'AUD':
            # Australia RBA rate from FRED
            rba_rate = await fred_client.get_series_latest("IRSTCI01AUM156N")
            if rba_rate is not None:
                indexers.append({'key': 'aonia', 'label': 'Australia Rate [Live]', 'value': round(rba_rate, 2)})
            else:
                indexers.append(FALLBACK_INDEXERS['AUD'][1])
            indexers.append(FALLBACK_INDEXERS['AUD'][2])
        
        else:
            return FALLBACK_INDEXERS.get(currency, [])
    
    except Exception as e:
        logger.error(f"Error getting indexers for {currency}: {e}")
        return FALLBACK_INDEXERS.get(currency, [])
    
    return indexers


def interpolate_fallback_rate(currency: str, tenor: float) -> Optional[float]:
    """Linear interpolation from fallback curves"""
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


def interpolate_rate(curve: dict, tenor: float) -> Optional[float]:
    """Linear interpolation from a rate curve dict {tenor: rate}"""
    if not curve:
        return None
    
    if tenor in curve:
        return curve[tenor]
    
    keys = sorted(curve.keys())
    
    for i in range(len(keys) - 1):
        t1, t2 = keys[i], keys[i + 1]
        if t1 <= tenor <= t2:
            r1, r2 = curve[t1], curve[t2]
            return r1 + ((tenor - t1) / (t2 - t1)) * (r2 - r1)
    
    if tenor < keys[0]:
        return curve[keys[0]]
    return curve[keys[-1]]


async def get_live_risk_free_curve_usd() -> Optional[dict]:
    """Fetch live US Treasury curve from FRED"""
    if not HTTPX_AVAILABLE or not config.FRED_API_KEY:
        return None
    
    cache_key = "risk_free_curve_usd"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    try:
        tenors = {1: "DGS1", 2: "DGS2", 3: "DGS3", 5: "DGS5", 7: "DGS7", 10: "DGS10"}
        curve = {}
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            for years, series_id in tenors.items():
                response = await client.get(
                    f"{config.FRED_BASE_URL}/series/observations",
                    params={
                        "series_id": series_id,
                        "api_key": config.FRED_API_KEY,
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
                            curve[years] = float(value)
                            break
        
        if len(curve) >= 3:
            cache.set(cache_key, curve)
            logger.info(f"✅ FRED Treasury curve: {curve}")
            return curve
    except Exception as e:
        logger.error(f"FRED curve error: {e}")
    
    return None


async def get_live_risk_free_curve_brl() -> Optional[dict]:
    """Fetch live Brazilian DI curve from BCB"""
    if not HTTPX_AVAILABLE:
        return None
    
    cache_key = "risk_free_curve_brl"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    try:
        # BCB series for swap rates (DI x Pre)
        # Using SELIC target as short-term proxy and building curve
        selic = await bcb_client.get_selic_target()
        if selic:
            # Approximate curve based on SELIC with typical term premium
            curve = {
                1: selic,
                2: selic - 0.2,
                3: selic - 0.4,
                5: selic - 0.8,
                10: selic - 1.5
            }
            cache.set(cache_key, curve)
            logger.info(f"✅ BCB BRL curve (SELIC-based): {curve}")
            return curve
    except Exception as e:
        logger.error(f"BCB curve error: {e}")
    
    return None


async def get_live_risk_free_curve_eur() -> Optional[dict]:
    """Fetch live EUR curve from ECB"""
    if not HTTPX_AVAILABLE:
        return None
    
    cache_key = "risk_free_curve_eur"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    try:
        # Use EURIBOR 12M as base and estimate curve
        euribor = await ecb_client.get_euribor(12)
        if euribor:
            # Approximate EUR curve
            curve = {
                1: euribor,
                2: euribor - 0.1,
                3: euribor - 0.15,
                5: euribor - 0.2,
                10: euribor
            }
            cache.set(cache_key, curve)
            logger.info(f"✅ ECB EUR curve (EURIBOR-based): {curve}")
            return curve
    except Exception as e:
        logger.error(f"ECB curve error: {e}")
    
    return None


async def get_live_risk_free_curve_gbp() -> Optional[dict]:
    """Fetch live GBP curve - use FRED for gilt yields"""
    if not HTTPX_AVAILABLE:
        return None
    
    cache_key = "risk_free_curve_gbp"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    try:
        curve = {}
        
        # Get SONIA for short end
        sonia = await boe_client.get_sonia()
        if sonia:
            curve[1] = sonia
        
        # Get Gilt yields from FRED (Bank of England data mirrored on FRED)
        # FRED series: IRLTLT01GBM156N (10Y), etc.
        gilt_10y = await fred_client.get_series_latest("IRLTLT01GBM156N")
        if gilt_10y:
            curve[10] = gilt_10y
        
        # Try BoE direct for more tenors
        tenors_boe = {
            5: "IUDMNPY",   # 5Y nominal par yield
            20: "IUDLNPY"   # 20Y nominal par yield
        }
        
        for years, series_code in tenors_boe.items():
            rate = await boe_client.get_series(series_code)
            if rate is not None:
                curve[years] = rate
        
        if len(curve) >= 2:
            # Interpolate missing points
            if 1 in curve and 10 in curve and 5 not in curve:
                curve[5] = (curve[1] + curve[10]) / 2
            if 1 in curve and 5 in curve and 2 not in curve:
                curve[2] = curve[1] + (curve[5] - curve[1]) * 0.25
            
            cache.set(cache_key, curve)
            logger.info(f"✅ GBP curve: {curve}")
            return curve
    except Exception as e:
        logger.error(f"GBP curve error: {e}")
    
    return None


async def get_live_risk_free_curve_chf() -> Optional[dict]:
    """Fetch live CHF curve from FRED (OECD data)"""
    if not HTTPX_AVAILABLE or not config.FRED_API_KEY:
        return None
    
    cache_key = "risk_free_curve_chf"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    try:
        curve = {}
        # Swiss rates from FRED (OECD data)
        chf_short = await fred_client.get_series_latest("IRSTCI01CHM156N")  # Overnight/call rate
        chf_3m = await fred_client.get_series_latest("IR3TIB01CHM156N")     # 3-month interbank
        chf_10y = await fred_client.get_series_latest("IRLTLT01CHM156N")    # 10Y govt bond
        
        if chf_short is not None:
            curve[1] = chf_short
        if chf_3m is not None:
            curve[0.25] = chf_3m  # 3 months
        if chf_10y is not None:
            curve[10] = chf_10y
        
        if len(curve) >= 2:
            # Interpolate missing points
            if 1 in curve and 10 in curve:
                curve[2] = curve[1] + (curve[10] - curve[1]) * 0.1
                curve[5] = curve[1] + (curve[10] - curve[1]) * 0.4
            cache.set(cache_key, curve)
            logger.info(f"✅ CHF curve: {curve}")
            return curve
    except Exception as e:
        logger.error(f"CHF curve error: {e}")
    
    return None


async def get_live_risk_free_curve_jpy() -> Optional[dict]:
    """Fetch live JPY curve from FRED"""
    if not HTTPX_AVAILABLE or not config.FRED_API_KEY:
        return None
    
    cache_key = "risk_free_curve_jpy"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    try:
        curve = {}
        # Japan government bond yields from FRED
        jpy_10y = await fred_client.get_series_latest("IRLTLT01JPM156N")
        jpy_short = await fred_client.get_series_latest("IRSTCI01JPM156N")
        
        if jpy_short:
            curve[1] = jpy_short
        if jpy_10y:
            curve[10] = jpy_10y
        
        if len(curve) >= 1:
            if 1 in curve and 10 in curve:
                curve[2] = curve[1] + (curve[10] - curve[1]) * 0.1
                curve[5] = curve[1] + (curve[10] - curve[1]) * 0.4
            cache.set(cache_key, curve)
            logger.info(f"✅ JPY curve: {curve}")
            return curve
    except Exception as e:
        logger.error(f"JPY curve error: {e}")
    
    return None


async def get_live_risk_free_curve_cad() -> Optional[dict]:
    """Fetch live CAD curve from FRED"""
    if not HTTPX_AVAILABLE or not config.FRED_API_KEY:
        return None
    
    cache_key = "risk_free_curve_cad"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    try:
        curve = {}
        # Canada government bond yields from FRED
        cad_10y = await fred_client.get_series_latest("IRLTLT01CAM156N")
        cad_short = await fred_client.get_series_latest("IRSTCI01CAM156N")
        
        if cad_short:
            curve[1] = cad_short
        if cad_10y:
            curve[10] = cad_10y
        
        if len(curve) >= 1:
            if 1 in curve and 10 in curve:
                curve[2] = curve[1] + (curve[10] - curve[1]) * 0.1
                curve[5] = curve[1] + (curve[10] - curve[1]) * 0.4
            cache.set(cache_key, curve)
            logger.info(f"✅ CAD curve: {curve}")
            return curve
    except Exception as e:
        logger.error(f"CAD curve error: {e}")
    
    return None


async def get_live_risk_free_curve_aud() -> Optional[dict]:
    """Fetch live AUD curve from FRED"""
    if not HTTPX_AVAILABLE or not config.FRED_API_KEY:
        return None
    
    cache_key = "risk_free_curve_aud"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    try:
        curve = {}
        # Australia government bond yields from FRED
        aud_10y = await fred_client.get_series_latest("IRLTLT01AUM156N")
        aud_short = await fred_client.get_series_latest("IRSTCI01AUM156N")
        
        if aud_short:
            curve[1] = aud_short
        if aud_10y:
            curve[10] = aud_10y
        
        if len(curve) >= 1:
            if 1 in curve and 10 in curve:
                curve[2] = curve[1] + (curve[10] - curve[1]) * 0.1
                curve[5] = curve[1] + (curve[10] - curve[1]) * 0.4
            cache.set(cache_key, curve)
            logger.info(f"✅ AUD curve: {curve}")
            return curve
    except Exception as e:
        logger.error(f"AUD curve error: {e}")
    
    return None


async def get_risk_free_rate(currency: str, tenor: float) -> tuple[Optional[float], str]:
    """Get risk-free rate with live data, falling back to reference curves"""
    currency = currency.upper()
    
    # Try to get live curve first
    live_curve = None
    source = "Reference Curve"
    
    try:
        if currency == "USD":
            live_curve = await get_live_risk_free_curve_usd()
        elif currency == "BRL":
            live_curve = await get_live_risk_free_curve_brl()
        elif currency == "EUR":
            live_curve = await get_live_risk_free_curve_eur()
        elif currency == "GBP":
            live_curve = await get_live_risk_free_curve_gbp()
        elif currency == "CHF":
            live_curve = await get_live_risk_free_curve_chf()
        elif currency == "JPY":
            live_curve = await get_live_risk_free_curve_jpy()
        elif currency == "CAD":
            live_curve = await get_live_risk_free_curve_cad()
        elif currency == "AUD":
            live_curve = await get_live_risk_free_curve_aud()
        # CNY uses fallback only (no easy public API)
        
        if live_curve:
            rate = interpolate_rate(live_curve, tenor)
            if rate is not None:
                return round(rate, 4), "Live"
    except Exception as e:
        logger.error(f"Live curve fetch failed for {currency}: {e}")
    
    # Fallback to reference data
    rate = interpolate_fallback_rate(currency, tenor)
    if rate is not None:
        return round(rate, 4), source
    
    return None, "Not Available"

# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.get("/api/", tags=["Info"])
def root():
    return {
        "service": "Hedged Return Converter API",
        "version": "2.1.0",
        "status": "✅ Running",
        "data_sources": {
            "BRL": "BCB (Banco Central) - FREE",
            "USD": "FRED (St. Louis Fed)",
            "EUR": "ECB Data Portal",
            "GBP": "Bank of England"
        }
    }

@app.get("/api/health", tags=["Info"])
def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/api/config/status", tags=["Info"])
def config_status():
    return {
        "bcb_configured": True,
        "fred_configured": bool(config.FRED_API_KEY),
        "ecb_configured": True,
        "boe_configured": True,
        "httpx_available": HTTPX_AVAILABLE
    }

@app.get("/api/indexers", tags=["Reference Data"])
async def list_all_indexers():
    result = {}
    for ccy in ['BRL', 'USD', 'EUR', 'GBP', 'CHF', 'JPY', 'CNY', 'CAD', 'AUD']:
        result[ccy] = await get_live_indexers(ccy)
    return result

@app.get("/api/indexers/{currency}", tags=["Reference Data"])
async def get_indexers_by_currency(currency: str):
    currency = currency.upper()
    supported = ['BRL', 'USD', 'EUR', 'GBP', 'CHF', 'JPY', 'CNY', 'CAD', 'AUD']
    if currency not in supported:
        raise HTTPException(status_code=404, detail=f"Currency {currency} not found")
    
    indexers = await get_live_indexers(currency)
    return {"currency": currency, "indexers": [IndexerResponse(**idx) for idx in indexers]}

@app.get("/api/risk-free-rates/{currency}", tags=["Reference Data"])
async def get_risk_free_curve(currency: str):
    currency = currency.upper()
    if currency not in FALLBACK_RISK_FREE:
        raise HTTPException(status_code=404, detail=f"Currency {currency} not found")
    
    return {
        "currency": currency,
        "curve": {str(k): v for k, v in FALLBACK_RISK_FREE[currency].items()},
        "source": "Reference Data"
    }


@app.get("/api/curves/{currency}", tags=["Reference Data"])
async def get_yield_curve_for_chart(currency: str):
    """Get full yield curve data for charting (tenors 1-30 years)"""
    currency = currency.upper()
    supported = ['BRL', 'USD', 'EUR', 'GBP', 'CHF', 'JPY', 'CNY', 'CAD', 'AUD']
    if currency not in supported:
        raise HTTPException(status_code=404, detail=f"Currency {currency} not supported")
    
    # Standard tenors for the chart
    tenors = [1, 2, 3, 5, 7, 10, 15, 20, 30]
    curve_data = []
    source = "Reference"
    
    # Try to get live curve first
    live_curve = None
    try:
        if currency == "USD":
            live_curve = await get_live_risk_free_curve_usd()
        elif currency == "BRL":
            live_curve = await get_live_risk_free_curve_brl()
        elif currency == "EUR":
            live_curve = await get_live_risk_free_curve_eur()
        elif currency == "GBP":
            live_curve = await get_live_risk_free_curve_gbp()
        elif currency == "CHF":
            live_curve = await get_live_risk_free_curve_chf()
        elif currency == "JPY":
            live_curve = await get_live_risk_free_curve_jpy()
        elif currency == "CAD":
            live_curve = await get_live_risk_free_curve_cad()
        elif currency == "AUD":
            live_curve = await get_live_risk_free_curve_aud()
        
        if live_curve:
            source = "Live"
    except Exception as e:
        logger.error(f"Error fetching live curve for {currency}: {e}")
    
    # Build curve data for each tenor
    for t in tenors:
        rate = None
        if live_curve:
            rate = interpolate_rate(live_curve, t)
        if rate is None:
            rate = interpolate_fallback_rate(currency, t)
        
        if rate is not None:
            curve_data.append({"tenor": t, "rate": round(rate, 4)})
    
    return {
        "currency": currency,
        "source": source,
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "curve": curve_data
    }

@app.post("/api/risk-free-rates/interpolate", tags=["Reference Data"], response_model=RiskFreeRateResponse)
async def interpolate_risk_free_rate(request: RiskFreeRateRequest):
    rate, source = await get_risk_free_rate(request.currency.upper(), request.tenor)
    
    if rate is None:
        raise HTTPException(status_code=404, detail=f"Rate not available")
    
    return RiskFreeRateResponse(currency=request.currency.upper(), tenor=request.tenor, rate=rate, source=source)

# ============================================================================
# MAIN CALCULATION
# ============================================================================

@app.post("/api/calculate/hedged-return", tags=["Calculations"], response_model=CIPCalculationResponse)
async def calculate_hedged_return(request: CIPCalculationRequest):
    """Calculate CIP-based hedged return conversion"""
    
    indexers = await get_live_indexers(request.base_currency)
    
    base_idx_data = None
    for idx in indexers:
        if idx['key'] == request.base_indexer_key:
            base_idx_data = idx
            break
    
    if base_idx_data is None:
        raise HTTPException(status_code=400, detail="Invalid indexer key")
    
    i_base_t, base_source = await get_risk_free_rate(request.base_currency, request.tenor)
    i_target_t, target_source = await get_risk_free_rate(request.target_currency, request.tenor)
    
    if i_base_t is None or i_target_t is None:
        raise HTTPException(status_code=400, detail="Risk-free rates not available")
    
    # CIP Calculation
    spread_decimal = request.spread / 100
    base_indexer_decimal = base_idx_data['value'] / 100
    all_in_base_pp = ((1 + base_indexer_decimal) * (1 + spread_decimal) - 1) * 100
    
    i_base_decimal = i_base_t / 100
    i_target_decimal = i_target_t / 100
    all_in_base_decimal = all_in_base_pp / 100
    
    target_equiv_decimal = (1 + all_in_base_decimal) * ((1 + i_target_decimal) / (1 + i_base_decimal)) - 1
    target_equiv_pp = target_equiv_decimal * 100
    
    total_return_target_pp = (math.pow(1 + target_equiv_decimal, request.tenor) - 1) * 100
    total_return_base_pp = (math.pow(1 + all_in_base_decimal, request.tenor) - 1) * 100
    
    is_live = "[Live]" in base_idx_data.get('label', '')
    
    assumptions = [
        AssumptionItem(name=base_idx_data['label'], value_pp=base_idx_data['value'], tenor_label="Spot", source_name="Live" if is_live else "Reference"),
        AssumptionItem(name=f"{request.base_currency} Risk-Free ({request.tenor}Y)", value_pp=i_base_t, tenor_label="Curve", source_name=base_source),
        AssumptionItem(name=f"{request.target_currency} Risk-Free ({request.tenor}Y)", value_pp=i_target_t, tenor_label="Curve", source_name=target_source),
        AssumptionItem(name="Spread", value_pp=request.spread, tenor_label="Input", source_name="User")
    ]
    
    warnings = ["✓ CIP-based hedged conversion applied"]
    if not is_live:
        warnings.insert(0, "⚠️ Using reference data for some inputs")
    
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
    cache.clear()
    return {"status": "Cache cleared", "timestamp": datetime.now().isoformat()}

# ============================================================================
# FRONTEND
# ============================================================================

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/")
def serve_frontend():
    index_file = Path(__file__).parent / "static" / "index.html"
    if index_file.exists():
        return FileResponse(index_file, media_type="text/html")
    return {"message": "Frontend not found", "api_docs": "/docs"}

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*60)
    print("🚀 Hedged Return Converter v2.1 - LIVE DATA")
    print("="*60)
    print("\n📍 Frontend: http://localhost:8000")
    print("📍 API Docs: http://localhost:8000/docs")
    print("\n⚙️  Data Sources:")
    print("   BRL: BCB (Banco Central) ✓ FREE")
    print("   USD: FRED ✓")
    print("   EUR: ECB ✓")
    print("   GBP: BoE ✓")
    print(f"   httpx: {'✓' if HTTPX_AVAILABLE else '✗ pip install httpx'}")
    print("\n" + "="*60 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
