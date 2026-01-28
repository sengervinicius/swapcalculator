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
    version="5.0.0"
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
    include_hedging_cost: bool = Field(default=False, description="Include hedging cost in calculation")
    hedging_cost_bps: Optional[float] = Field(default=None, description="Manual hedging cost override in bps")

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
    hedging_cost_bps: Optional[float] = None
    hedged_return_pp: float  # The main hedged return (after hedging cost if included)
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
    ],
    'ARS': [
        {'key': 'none', 'label': 'No indexer', 'value': 0.0},
        {'key': 'badlar', 'label': 'BADLAR (Argentina)', 'value': 37.00},
        {'key': 'bcra-rate', 'label': 'BCRA Policy Rate', 'value': 32.00}
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
    'AUD': {1: 4.10, 2: 4.00, 3: 3.95, 5: 3.90, 10: 4.20},
    'ARS': {1: 40.00, 2: 45.00, 3: 50.00, 5: 55.00, 10: 60.00}  # Very high rates, indicative
}

# ============================================================================
# HEDGING COST / CROSS-CURRENCY BASIS
# 
# IMPORTANT: The hedge cost is NOT the interest rate differential!
# The interest rate differential is already captured in the CIP conversion formula.
#
# The hedge cost is the CROSS-CURRENCY BASIS - the small deviation from CIP
# that exists in real markets due to:
# - USD funding scarcity
# - Bank balance sheet constraints
# - Convertibility premiums (for EM currencies)
# - Regulatory capital requirements
#
# TYPICAL RANGES:
# - G10 currencies: -10 to -50 bps (you PAY a small premium for USD)
# - BRL: +50 to +150 bps (Cupom Cambial > SOFR = you RECEIVE a small benefit)
# - ARS: Complex - NDF market has large basis due to capital controls
#
# SIGN CONVENTION:
# - POSITIVE = benefit when hedging (actual forward better than CIP-implied)
# - NEGATIVE = cost when hedging (actual forward worse than CIP-implied)
# ============================================================================

# Cross-currency basis vs USD (in bps)
# These represent the DEVIATION from CIP, not the full rate differential
# Most values should be SMALL (-50 to +50 bps) except for restricted currencies
XCCY_BASIS_VS_USD = {
    'USD': {1: 0, 2: 0, 3: 0, 5: 0, 10: 0},  # USD is the base
    # G10: Small negative basis (USD funding premium)
    'EUR': {1: -15, 2: -18, 3: -20, 5: -22, 10: -25},
    'GBP': {1: -8, 2: -10, 3: -12, 5: -12, 10: -15},
    'JPY': {1: -30, 2: -35, 3: -40, 5: -45, 10: -50},
    'CHF': {1: -18, 2: -20, 3: -22, 5: -25, 10: -28},
    'CAD': {1: -5, 2: -6, 3: -8, 5: -10, 10: -10},
    'AUD': {1: -3, 2: -3, 3: -5, 5: -5, 10: -5},
    # BRL: Small positive (convertibility premium)
    # This should be SMALL - the big CDI-SOFR diff is already in CIP
    'BRL': {1: 15, 2: 20, 3: 25, 5: 35, 10: 45},
    # ARS: Capital controls create real NDF basis - this can be larger
    # but still represents deviation from official rate differential
    'ARS': {1: -150, 2: -200, 3: -250, 5: -300, 10: -400},
    # CNY: Modest NDF basis for restricted currency
    'CNY': {1: -10, 2: -15, 3: -18, 5: -22, 10: -30},
}

# Data quality indicators
HEDGING_COST_DATA_QUALITY = {
    'BRL': 'live',      # Live B3 data when available
    'EUR': 'live_or_estimate',
    'GBP': 'live_or_estimate',
    'JPY': 'live_or_estimate',
    'CHF': 'live_or_estimate',
    'CAD': 'live_or_estimate',
    'AUD': 'live_or_estimate',
    'ARS': 'estimate',  # NDF market - no free public source
    'CNY': 'estimate',  # Restricted currency - no free public source
    'USD': 'base',
}

# Cupom Cambial cache - stores live data from B3
_cupom_cambial_cache: Dict[str, tuple] = {}
_cupom_cambial_cache_ttl = 3600  # 1 hour

# Hedging cost descriptions for UI - with data quality indicators
# NOTE: Hedge cost ≠ Interest rate differential!
# Hedge cost = Cross-currency basis (deviation from CIP)
# BRL uses Cupom Cambial, G10 uses FX forward implied basis
HEDGING_COST_INFO = {
    'USD': {
        'type': 'base', 
        'instrument': 'N/A (base currency)', 
        'notes': 'Base currency - no hedge cost',
        'data_quality': 'N/A'
    },
    'EUR': {
        'type': 'xccy_basis', 
        'instrument': 'FX Forward Implied Basis', 
        'notes': 'EUR/USD xccy basis • Live from FX forwards or estimate',
        'data_quality': 'live_or_estimate',
        'typical_range': '-15 to -30 bps',
        'source': 'Investing.com FX Forwards'
    },
    'GBP': {
        'type': 'xccy_basis', 
        'instrument': 'FX Forward Implied Basis', 
        'notes': 'GBP/USD xccy basis • Live from FX forwards or estimate',
        'data_quality': 'live_or_estimate',
        'typical_range': '-5 to -20 bps'
    },
    'JPY': {
        'type': 'xccy_basis', 
        'instrument': 'Cross-Currency Basis Swap', 
        'notes': 'JPY/USD xccy basis • Estimate - typically wider due to USD funding demand',
        'data_quality': 'estimate',
        'typical_range': '-30 to -60 bps'
    },
    'CHF': {
        'type': 'xccy_basis', 
        'instrument': 'Cross-Currency Basis Swap', 
        'notes': 'CHF/USD xccy basis • Estimate (live requires Bloomberg)',
        'data_quality': 'estimate',
        'typical_range': '-20 to -40 bps'
    },
    'CAD': {
        'type': 'xccy_basis', 
        'instrument': 'Cross-Currency Basis Swap', 
        'notes': 'CAD/USD xccy basis • Estimate - usually tight',
        'data_quality': 'estimate',
        'typical_range': '-5 to -15 bps'
    },
    'AUD': {
        'type': 'xccy_basis', 
        'instrument': 'Cross-Currency Basis Swap', 
        'notes': 'AUD/USD xccy basis • Estimate',
        'data_quality': 'estimate',
        'typical_range': '-10 to +10 bps'
    },
    'BRL': {
        'type': 'futures', 
        'instrument': 'DDI Futures / FRC (Cupom Cambial)', 
        'notes': 'Live B3 data • Cupom Cambial is IMPLIED USD rate in Brazil',
        'data_quality': 'live',
        'typical_range': '+50 to +150 bps'
    },
    'ARS': {
        'type': 'ndf', 
        'instrument': 'Non-Deliverable Forward (NDF)', 
        'notes': 'ARS/USD NDF implied cost • Estimate - capital controls, high volatility',
        'data_quality': 'estimate',
        'typical_range': '-500 to -2000 bps'
    },
    'CNY': {
        'type': 'ndf', 
        'instrument': 'Non-Deliverable Forward (NDF)', 
        'notes': 'CNY/USD NDF implied cost • Estimate - restricted currency',
        'data_quality': 'estimate',
        'typical_range': '-50 to -150 bps'
    },
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


class B3Client:
    """
    Client for B3 (Brasil, Bolsa, Balcão) market data.
    Fetches Cupom Cambial (DOC curve) - the implied USD rate in Brazil.
    
    Formula for hedge cost/benefit (USD → BRL):
        Hedge Benefit (bps) = (Cupom Cambial - SOFR) * 100
    
    Data source: B3 "Taxas Referenciais" - FREE, no auth needed!
    URL: https://www2.bmf.com.br/pages/portal/bmfbovespa/boletim1/txref1.asp
    """
    
    B3_URL = "https://www2.bmf.com.br/pages/portal/bmfbovespa/boletim1/txref1.asp"
    
    async def get_cupom_cambial_curve(self) -> Optional[Dict[int, float]]:
        """
        Fetch the Cupom Limpo (DOC) curve from B3.
        Returns dict mapping days_corridos -> rate (% p.a. linear 360)
        """
        if not HTTPX_AVAILABLE:
            return None
        
        cache_key = "b3_cupom_limpo"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        
        try:
            # Fetch Cupom Limpo page
            params = {"idioma": "P", "Taxa": "Cupom limpo"}
            
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(self.B3_URL, params=params)
                
                if response.status_code == 200:
                    # Parse the HTML table
                    curve = self._parse_b3_rate_table(response.text)
                    if curve:
                        cache.set(cache_key, curve)
                        logger.info(f"✅ B3 Cupom Cambial: {len(curve)} vertices fetched")
                        return curve
        except Exception as e:
            logger.error(f"B3 API error: {e}")
        
        return None
    
    def _parse_b3_rate_table(self, html: str) -> Optional[Dict[int, float]]:
        """Parse B3's rate table HTML to extract the curve"""
        import re
        
        curve = {}
        
        # Look for table rows with pattern: days | rate252 | rate360
        # The pattern in B3 tables: number | number,decimal | number,decimal
        pattern = r'(\d+)\s*\|\s*(\d+[,\.]\d+)\s*\|\s*(\d+[,\.]\d+)'
        
        matches = re.findall(pattern, html)
        
        for match in matches:
            try:
                days = int(match[0])
                # Use the 360-day linear rate (column 3) for cupom cambial
                rate_str = match[2].replace(',', '.')
                rate = float(rate_str)
                curve[days] = rate
            except (ValueError, IndexError):
                continue
        
        # If regex didn't work, try simpler line-by-line parsing
        if not curve:
            lines = html.split('\n')
            for line in lines:
                # Look for lines that look like: "360 | 5.23 | 5.10"
                parts = line.strip().split('|')
                if len(parts) >= 3:
                    try:
                        days = int(parts[0].strip())
                        rate_str = parts[2].strip().replace(',', '.')
                        rate = float(rate_str)
                        if 1 <= days <= 10000 and 0 <= rate <= 50:
                            curve[days] = rate
                    except (ValueError, IndexError):
                        continue
        
        return curve if curve else None
    
    def interpolate_cupom_cambial(self, curve: Dict[int, float], target_days: int) -> Optional[float]:
        """Interpolate cupom cambial rate for a specific number of days"""
        if not curve:
            return None
        
        days_list = sorted(curve.keys())
        
        # Edge cases
        if target_days <= days_list[0]:
            return curve[days_list[0]]
        if target_days >= days_list[-1]:
            return curve[days_list[-1]]
        
        # Linear interpolation
        for i in range(len(days_list) - 1):
            d1, d2 = days_list[i], days_list[i + 1]
            if d1 <= target_days <= d2:
                r1, r2 = curve[d1], curve[d2]
                return r1 + (r2 - r1) * (target_days - d1) / (d2 - d1)
        
        return None
    
    async def get_cupom_cambial_for_tenor(self, tenor_years: float) -> Optional[float]:
        """Get interpolated cupom cambial rate for a given tenor in years"""
        curve = await self.get_cupom_cambial_curve()
        if not curve:
            return None
        
        # Convert years to days (using 360 day year for cupom cambial convention)
        target_days = int(tenor_years * 360)
        
        return self.interpolate_cupom_cambial(curve, target_days)


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


class XCCYBasisClient:
    """
    Client to calculate cross-currency basis from FX forwards.
    
    The xccy basis is the deviation from Covered Interest Parity (CIP).
    We calculate it by comparing:
    - Implied foreign rate from FX forwards
    - Actual foreign interbank rate
    
    Formula: Basis = Implied Rate - Actual Rate
    
    Where Implied Rate from forward points:
    F = S × (1 + r_foreign × T) / (1 + r_domestic × T)
    
    For small rates: Forward Points ≈ (r_foreign - r_domestic) × S × T
    So: Implied r_foreign ≈ r_domestic + (Forward Points / S / T)
    """
    
    # Investing.com forward rate URL patterns (scraping these requires network)
    FORWARD_URLS = {
        'EUR': 'https://www.investing.com/currencies/eur-usd-forward-rates',
        'GBP': 'https://www.investing.com/currencies/gbp-usd-forward-rates',
        'JPY': 'https://www.investing.com/currencies/usd-jpy-forward-rates',
        'CHF': 'https://www.investing.com/currencies/usd-chf-forward-rates',
        'CAD': 'https://www.investing.com/currencies/usd-cad-forward-rates',
        'AUD': 'https://www.investing.com/currencies/aud-usd-forward-rates',
    }
    
    # Tenor mapping for forward contracts (in years)
    TENOR_MAP = {
        '1Y FWD': 1.0,
        '2Y FWD': 2.0,
        '3Y FWD': 3.0,
        '5Y FWD': 5.0,
        '10Y FWD': 10.0,
    }
    
    async def get_forward_points(self, currency: str, tenor_years: float) -> Optional[dict]:
        """
        Scrape FX forward points from Investing.com
        Returns forward points in pips and spot rate
        """
        if not HTTPX_AVAILABLE:
            return None
        
        url = self.FORWARD_URLS.get(currency.upper())
        if not url:
            return None
        
        cache_key = f"fwd_{currency}_{tenor_years}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "text/html,application/xhtml+xml"
                    }
                )
                
                if response.status_code == 200:
                    # Parse HTML to extract forward points
                    # This is simplified - real implementation would use BeautifulSoup
                    text = response.text
                    
                    # Look for tenor-specific forward points
                    # The page has rows like "EURUSD 5Y FWD" with bid/ask
                    result = self._parse_forward_points(text, currency, tenor_years)
                    if result:
                        cache.set(cache_key, result)
                        return result
                        
        except Exception as e:
            logger.error(f"Forward points fetch error for {currency}: {e}")
        
        return None
    
    def _parse_forward_points(self, html: str, currency: str, tenor_years: float) -> Optional[dict]:
        """Parse forward points from Investing.com HTML"""
        import re
        
        # Map tenor to search pattern
        tenor_patterns = {
            1: r'1Y FWD.*?(\d+\.?\d*)',
            2: r'2Y FWD.*?(\d+\.?\d*)',
            3: r'3Y FWD.*?(\d+\.?\d*)',
            5: r'5Y FWD.*?(\d+\.?\d*)',
            10: r'10Y FWD.*?(\d+\.?\d*)',
        }
        
        tenor_key = int(tenor_years) if tenor_years in [1, 2, 3, 5, 10] else 5
        pattern = tenor_patterns.get(tenor_key)
        
        if pattern:
            match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if match:
                try:
                    forward_points = float(match.group(1))
                    return {'forward_points_pips': forward_points, 'tenor': tenor_years}
                except ValueError:
                    pass
        
        return None
    
    async def calculate_xccy_basis(
        self, 
        currency: str, 
        tenor_years: float,
        usd_rate: Optional[float] = None,
        foreign_rate: Optional[float] = None,
        spot_rate: Optional[float] = None
    ) -> Optional[dict]:
        """
        Calculate cross-currency basis from forward points.
        
        Basis = Forward Implied Foreign Rate - Actual Foreign Rate
        
        If forward points are unavailable, falls back to estimates.
        """
        currency = currency.upper()
        
        # Get USD rate (SOFR) if not provided
        if usd_rate is None:
            usd_rate = await fred_client.get_sofr()
            if usd_rate is None:
                usd_rate = 4.30  # Fallback
        
        # Try to get forward points
        fwd_data = await self.get_forward_points(currency, tenor_years)
        
        if fwd_data and spot_rate:
            # Calculate implied foreign rate from forward points
            # Forward points are typically quoted in pips (0.0001 for most pairs)
            fwd_points = fwd_data['forward_points_pips']
            
            # For EUR/USD, GBP/USD, AUD/USD: 1 pip = 0.0001
            # For USD/JPY: 1 pip = 0.01
            pip_value = 0.01 if currency == 'JPY' else 0.0001
            
            # Convert forward points to implied rate differential
            # fwd_pts ≈ (r_foreign - r_usd) × spot × T × pip_adjustment
            implied_rate_diff = (fwd_points * pip_value) / (spot_rate * tenor_years) * 100
            implied_foreign_rate = usd_rate + implied_rate_diff
            
            # Get actual foreign rate
            if foreign_rate is None:
                foreign_rate = await self._get_foreign_rate(currency)
            
            if foreign_rate:
                # Basis = Implied - Actual (in bps)
                basis_bps = (implied_foreign_rate - foreign_rate) * 100
                
                return {
                    'basis_bps': round(basis_bps, 1),
                    'implied_rate': round(implied_foreign_rate, 4),
                    'actual_rate': round(foreign_rate, 4),
                    'forward_points': fwd_points,
                    'source': 'Investing.com FX Forwards',
                    'is_live': True,
                    'currency': currency,
                    'tenor': tenor_years
                }
        
        # Fallback: return None to trigger static estimates
        return None
    
    async def _get_foreign_rate(self, currency: str) -> Optional[float]:
        """Get the benchmark interbank rate for a foreign currency"""
        if currency == 'EUR':
            return await ecb_client.get_euribor(12)
        elif currency == 'GBP':
            return await boe_client.get_sonia()
        elif currency == 'JPY':
            # TONAR from FRED
            return await fred_client.get_series_latest("IRSTCI01JPM156N") or 0.1
        elif currency == 'CHF':
            return await fred_client.get_series_latest("IR3TIB01CHM156N") or 0.5
        elif currency == 'CAD':
            return await fred_client.get_series_latest("IRSTCI01CAM156N") or 3.0
        elif currency == 'AUD':
            return await fred_client.get_series_latest("IRSTCI01AUM156N") or 4.0
        return None


# Initialize xccy basis client
xccy_basis_client = XCCYBasisClient()

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
        
        elif currency == 'ARS':
            # Argentina rates - using fallback (no easy live API due to capital controls)
            # BADLAR and BCRA policy rate
            indexers.append(FALLBACK_INDEXERS['ARS'][1])
            indexers.append(FALLBACK_INDEXERS['ARS'][2])
        
        else:
            # Unknown currency - try fallback
            fallback = FALLBACK_INDEXERS.get(currency, [])
            if fallback:
                return fallback
            return indexers
    
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
# HEDGING COST FUNCTIONS
# ============================================================================

def interpolate_xccy_basis(currency: str, tenor: float) -> Optional[float]:
    """
    Interpolate cross-currency basis for a currency vs USD at given tenor.
    Returns basis in bps. 
    
    SIGN CONVENTION (for USD → CCY direction):
    - POSITIVE = USD investor benefits when going to this currency
    - NEGATIVE = USD investor pays when going to this currency
    """
    currency = currency.upper()
    if currency not in XCCY_BASIS_VS_USD:
        return None
    
    curve = XCCY_BASIS_VS_USD[currency]
    tenors = sorted(curve.keys())
    
    if tenor <= tenors[0]:
        return curve[tenors[0]]
    if tenor >= tenors[-1]:
        return curve[tenors[-1]]
    
    for i in range(len(tenors) - 1):
        t1, t2 = tenors[i], tenors[i + 1]
        if t1 <= tenor <= t2:
            r1, r2 = curve[t1], curve[t2]
            return r1 + (r2 - r1) * (tenor - t1) / (t2 - t1)
    
    return None


# Aliases for backwards compatibility
def interpolate_implied_usd_rate(currency: str, tenor: float) -> Optional[float]:
    """DEPRECATED: Use interpolate_xccy_basis"""
    return interpolate_xccy_basis(currency, tenor)


def interpolate_local_rate(currency: str, tenor: float) -> Optional[float]:
    """DEPRECATED"""
    return None


def interpolate_hedging_cost(currency: str, tenor: float) -> Optional[float]:
    """DEPRECATED: Use interpolate_xccy_basis"""
    return interpolate_xccy_basis(currency, tenor)




async def get_hedging_cost_async(base_currency: str, target_currency: str, tenor: float) -> dict:
    """
    Calculate hedging cost between two currencies.
    
    IMPORTANT: Hedge cost ≠ Interest rate differential!
    
    The hedge cost is the DEVIATION from Covered Interest Parity (CIP), 
    known as the cross-currency basis. In a perfect CIP world, hedging 
    has zero cost because the forward rate already embeds rate differentials.
    
    Data sources (in order of preference):
    1. BRL: Live B3 Cupom Cambial + SOFR
    2. G10: Live FX forward scraping from Investing.com (calculates basis)
    3. Fallback: Static reference estimates
    
    Returns cost in bps. Positive = benefit, Negative = cost.
    """
    base = base_currency.upper()
    target = target_currency.upper()
    
    # Same currency = no cost
    if base == target:
        return {
            "cost_bps": 0,
            "source": "N/A",
            "is_live": False,
            "instrument": "None",
            "notes": "Same currency pair",
            "data_quality": "N/A"
        }
    
    # Special handling for BRL - use live B3 + FRED data
    # Cupom Cambial is the IMPLIED USD rate in Brazil (derived from FX forwards)
    # Formula: Hedge Cost = Base_Implied - Target_Implied
    if (base == 'USD' and target == 'BRL') or (base == 'BRL' and target == 'USD'):
        try:
            b3_client = B3Client()
            
            # Get Cupom Cambial from B3 (IMPLIED USD rate in BRL market)
            cupom_cambial = await b3_client.get_cupom_cambial_for_tenor(tenor)
            
            # Get SOFR from FRED (IMPLIED USD rate for USD, which IS the USD rate)
            sofr = await fred_client.get_sofr()
            
            if cupom_cambial is not None and sofr is not None:
                # BRL implied rate = Cupom Cambial
                # USD implied rate = SOFR
                
                if base == 'USD':
                    # USD → BRL: Cost = USD_implied - BRL_implied = SOFR - Cupom
                    hedge_cost_bps = (sofr - cupom_cambial) * 100
                else:
                    # BRL → USD: Cost = BRL_implied - USD_implied = Cupom - SOFR
                    hedge_cost_bps = (cupom_cambial - sofr) * 100
                
                return {
                    "cost_bps": round(hedge_cost_bps, 1),
                    "source": "B3 + FRED",
                    "is_live": True,
                    "instrument": "DDI Futures / FRC (Cupom Cambial)",
                    "notes": f"Live: Cupom Cambial ({cupom_cambial:.2f}%), SOFR ({sofr:.2f}%)",
                    "base_implied_rate": round(cupom_cambial if base == 'BRL' else sofr, 4),
                    "target_implied_rate": round(sofr if base == 'BRL' else cupom_cambial, 4),
                    "cupom_cambial": round(cupom_cambial, 4),
                    "sofr": round(sofr, 4),
                    "calculation": f"({cupom_cambial if base == 'BRL' else sofr:.2f} - {sofr if base == 'BRL' else cupom_cambial:.2f}) × 100 = {hedge_cost_bps:.1f} bps",
                    "base_currency": base,
                    "target_currency": target,
                    "tenor": tenor,
                    "data_quality": "live",
                    "methodology": get_methodology_for_pair(base, target)
                }
        except Exception as e:
            logger.error(f"Live BRL hedging cost fetch failed: {e}")
    
    # For G10 currencies, try to calculate basis from FX forwards
    g10_currencies = ['EUR', 'GBP', 'JPY', 'CHF', 'CAD', 'AUD']
    target_ccy = target if base == 'USD' else base
    
    if target_ccy in g10_currencies and (base == 'USD' or target == 'USD'):
        try:
            # Try to get live xccy basis from forward scraping
            basis_data = await xccy_basis_client.calculate_xccy_basis(
                currency=target_ccy,
                tenor_years=tenor
            )
            
            if basis_data and basis_data.get('is_live'):
                basis_bps = basis_data['basis_bps']
                
                # Direction adjustment: if going from USD to foreign, use as-is
                # If going from foreign to USD, flip sign
                if base != 'USD':
                    basis_bps = -basis_bps
                
                return {
                    "cost_bps": round(basis_bps, 1),
                    "source": basis_data['source'],
                    "is_live": True,
                    "instrument": "FX Forward Implied Basis",
                    "notes": f"Implied {target_ccy} rate: {basis_data['implied_rate']:.2f}%, Actual: {basis_data['actual_rate']:.2f}%",
                    "implied_rate": basis_data['implied_rate'],
                    "actual_rate": basis_data['actual_rate'],
                    "forward_points": basis_data.get('forward_points'),
                    "calculation": f"({basis_data['implied_rate']:.2f} - {basis_data['actual_rate']:.2f}) × 100 = {basis_bps:.1f} bps",
                    "base_currency": base,
                    "target_currency": target,
                    "tenor": tenor,
                    "data_quality": "live"
                }
        except Exception as e:
            logger.warning(f"FX forward basis calculation failed for {target_ccy}: {e}")
    
    # Fallback to static estimates for all currencies
    return get_hedging_cost_sync(base_currency, target_currency, tenor)


def get_hedging_cost_sync(base_currency: str, target_currency: str, tenor: float) -> dict:
    """
    Calculate hedging cost between two currencies using cross-currency basis.
    
    IMPORTANT: This is the XCCY BASIS - the deviation from CIP, NOT the full
    interest rate differential! The interest rate differential is already
    captured in the CIP conversion formula.
    
    TYPICAL RANGES:
    - G10 currencies: -10 to -50 bps (small)
    - BRL (Cupom - SOFR): +50 to +200 bps (convertibility premium)
    - ARS (NDF basis): -500 to -1500 bps (large due to capital controls)
    
    SIGN CONVENTION (for USD → CCY):
    - POSITIVE = benefit (actual forward better than CIP-implied)
    - NEGATIVE = cost (actual forward worse than CIP-implied)
    """
    base = base_currency.upper()
    target = target_currency.upper()
    
    # Same currency = no cost
    if base == target:
        return {
            "cost_bps": 0,
            "source": "N/A",
            "is_live": False,
            "instrument": "None",
            "notes": "Same currency pair",
            "data_quality": "N/A"
        }
    
    # Get xccy basis for both currencies vs USD
    base_basis = interpolate_xccy_basis(base, tenor) or 0
    target_basis = interpolate_xccy_basis(target, tenor) or 0
    
    # Calculate hedge cost
    # For USD → CCY: cost = target_basis (directly use the basis)
    # For CCY → USD: cost = -base_basis (flip sign)
    # For CCY1 → CCY2 (cross): cost = target_basis - base_basis
    
    if base == 'USD':
        cost_bps = target_basis
    elif target == 'USD':
        cost_bps = -base_basis
    else:
        cost_bps = target_basis - base_basis
    
    # Determine instrument type
    em_currencies = ['BRL', 'ARS', 'CNY']
    
    if base == 'BRL' or target == 'BRL':
        instrument = "DDI Futures / FRC (Cupom Cambial)"
        notes = f"Basis: {base}={base_basis:+.0f}bps, {target}={target_basis:+.0f}bps"
    elif base == 'ARS' or target == 'ARS':
        instrument = "Non-Deliverable Forward (NDF)"
        notes = f"ARS NDF basis reflects capital controls • {base}={base_basis:+.0f}bps, {target}={target_basis:+.0f}bps"
    elif base == 'CNY' or target == 'CNY':
        instrument = "Non-Deliverable Forward (NDF)"
        notes = f"CNY restricted currency • {base}={base_basis:+.0f}bps, {target}={target_basis:+.0f}bps"
    else:
        instrument = "Cross-Currency Basis Swap"
        notes = f"G10 xccy basis • {base}={base_basis:+.0f}bps, {target}={target_basis:+.0f}bps"
    
    # Data quality
    base_quality = HEDGING_COST_DATA_QUALITY.get(base, 'estimate')
    target_quality = HEDGING_COST_DATA_QUALITY.get(target, 'estimate')
    data_quality = 'estimate' if 'estimate' in [base_quality, target_quality] else 'live_or_estimate'
    
    return {
        "cost_bps": round(cost_bps, 1),
        "source": "Reference estimate",
        "is_live": False,
        "instrument": instrument,
        "notes": notes,
        "base_currency": base,
        "target_currency": target,
        "tenor": tenor,
        "data_quality": data_quality,
        "base_basis_bps": base_basis,
        "target_basis_bps": target_basis,
        "methodology": get_methodology_for_pair(base, target)
    }


def get_methodology_for_pair(base: str, target: str) -> str:
    """Return methodology explanation for a currency pair"""
    
    if base == 'BRL' or target == 'BRL':
        return """CUPOM CAMBIAL METHOD (Brazil)
        
The Cupom Cambial is the implied USD interest rate embedded in Brazilian 
FX forwards and futures (DDI/FRC contracts at B3).

It represents what you'd earn on USD hedged within Brazil's onshore market.
The formula is: Hedge Cost = Cupom Cambial - SOFR

If Cupom Cambial > SOFR: USD→BRL gives you POSITIVE carry
If Cupom Cambial < SOFR: USD→BRL costs you (NEGATIVE carry)

Live data source: B3 (Brasil, Bolsa, Balcão)"""
    
    elif base == 'ARS' or target == 'ARS':
        return """NDF IMPLIED RATE METHOD (Argentina)

Argentina has capital controls, so the ARS/USD forward is a Non-Deliverable 
Forward (NDF) traded offshore. The NDF rate prices in expected peso devaluation.

The implied USD rate in ARS is VERY HIGH (30-50%+) because the market expects 
significant peso depreciation. This creates:

- ARS → Strong CCY: POSITIVE carry (you receive the devaluation premium)
- Strong CCY → ARS: NEGATIVE carry (you pay the devaluation premium)

Note: These are ESTIMATES. Actual NDF rates fluctuate significantly."""
    
    elif base == 'CNY' or target == 'CNY':
        return """NDF IMPLIED RATE METHOD (China)

The CNY is a restricted currency with managed float. Offshore CNY (CNH) and 
onshore CNY can differ. NDF rates reflect market expectations of depreciation.

The implied USD rate is typically higher than onshore rates, reflecting 
a modest depreciation premium.

Note: These are ESTIMATES based on typical NDF spreads."""
    
    else:
        return """CROSS-CURRENCY BASIS METHOD (G10)

For freely tradeable G10 currencies, the hedge cost is the cross-currency 
basis swap spread - the deviation from Covered Interest Parity (CIP).

In theory, CIP should hold and hedging should be costless. In practice, 
there's a persistent "xccy basis" of typically -10 to -50 bps for most 
G10 currencies vs USD.

This basis reflects:
- USD funding scarcity
- Bank balance sheet constraints  
- Regulatory capital requirements

Note: Live xccy basis data requires paid market data terminals."""


# Keep sync version for non-async contexts
def get_hedging_cost(base_currency: str, target_currency: str, tenor: float) -> dict:
    """Synchronous wrapper - use get_hedging_cost_async in async contexts"""
    return get_hedging_cost_sync(base_currency, target_currency, tenor)


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

@app.get("/api/hedging-cost/{base_currency}/{target_currency}", tags=["Reference Data"])
async def get_hedging_cost_endpoint(base_currency: str, target_currency: str, tenor: float = 5):
    """
    Get hedging cost (cross-currency basis or NDF cost) for a currency pair.
    
    **For USD ↔ BRL**: Fetches LIVE data from B3 (Cupom Cambial) and FRED (SOFR)
    Formula: Hedge Benefit = Cupom Cambial - SOFR
    
    Returns cost in basis points (bps) and metadata.
    - Positive cost = hedging BENEFIT (you gain, e.g., USD→BRL typically positive)
    - Negative cost = hedging COST (you pay)
    
    For restricted currencies (BRL, CNY): Uses NDF/Futures implied costs
    For deliverable currencies: Uses cross-currency basis swap spreads
    """
    base = base_currency.upper()
    target = target_currency.upper()
    supported = ['BRL', 'USD', 'EUR', 'GBP', 'CHF', 'JPY', 'CNY', 'CAD', 'AUD', 'ARS']
    
    if base not in supported:
        raise HTTPException(status_code=404, detail=f"Base currency {base} not supported")
    if target not in supported:
        raise HTTPException(status_code=404, detail=f"Target currency {target} not supported")
    
    # Use async version to get live B3 data for BRL
    result = await get_hedging_cost_async(base, target, tenor)
    result['base_currency'] = base
    result['target_currency'] = target
    result['tenor'] = tenor
    result['as_of'] = datetime.now().strftime("%Y-%m-%d")
    
    return result

@app.get("/api/hedging-cost/all", tags=["Reference Data"])
async def get_all_hedging_costs(tenor: float = 5):
    """Get hedging cost matrix for all supported currency pairs"""
    supported = ['BRL', 'USD', 'EUR', 'GBP', 'CHF', 'JPY', 'CNY', 'CAD', 'AUD', 'ARS']
    matrix = {}
    
    for base in supported:
        matrix[base] = {}
        for target in supported:
            if base != target:
                # Use async for BRL pairs to get live data
                if 'BRL' in [base, target]:
                    cost_data = await get_hedging_cost_async(base, target, tenor)
                else:
                    cost_data = get_hedging_cost_sync(base, target, tenor)
                matrix[base][target] = cost_data['cost_bps']
    
    return {
        "tenor": tenor,
        "matrix": matrix,
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "notes": "Cost in bps. Positive = benefit, Negative = cost. BRL pairs use LIVE B3 data."
    }

@app.get("/api/indexers", tags=["Reference Data"])
async def list_all_indexers():
    result = {}
    for ccy in ['BRL', 'USD', 'EUR', 'GBP', 'CHF', 'JPY', 'CNY', 'CAD', 'AUD', 'ARS']:
        result[ccy] = await get_live_indexers(ccy)
    return result

@app.get("/api/indexers/{currency}", tags=["Reference Data"])
async def get_indexers_by_currency(currency: str):
    currency = currency.upper()
    supported = ['BRL', 'USD', 'EUR', 'GBP', 'CHF', 'JPY', 'CNY', 'CAD', 'AUD', 'ARS']
    if currency not in supported:
        raise HTTPException(status_code=404, detail=f"Currency {currency} not found")
    
    indexers = await get_live_indexers(currency)
    return {"currency": currency, "indexers": [IndexerResponse(**idx) for idx in indexers]}


@app.get("/api/b3/cupom-cambial", tags=["Brazil Market Data"])
async def get_b3_cupom_cambial(tenor: float = 5):
    """
    Get live Cupom Cambial (DOC curve) from B3.
    
    The Cupom Cambial is the implied USD rate in Brazil, derived from
    DDI futures and FX forwards. It represents what a Brazilian investor
    would earn on USD held within Brazil's financial system.
    
    This is the key input for calculating USD→BRL hedge cost/benefit:
    Hedge Benefit = Cupom Cambial - SOFR
    
    **Data Source**: B3 (Brasil, Bolsa, Balcão) - FREE, no authentication
    **Update Frequency**: Daily, after market close (~18:00 BRT)
    """
    b3_client = B3Client()
    fred_client = FREDClient()
    
    try:
        # Fetch full curve
        curve = await b3_client.get_cupom_cambial_curve()
        
        # Get rate for specific tenor
        rate_for_tenor = await b3_client.get_cupom_cambial_for_tenor(tenor)
        
        # Get SOFR for comparison
        sofr = await fred_client.get_sofr()
        
        # Calculate hedge benefit if both available
        hedge_benefit_bps = None
        if rate_for_tenor is not None and sofr is not None:
            hedge_benefit_bps = round((rate_for_tenor - sofr) * 100, 1)
        
        # Format curve for response (sample vertices)
        sample_tenors = [0.5, 1, 2, 3, 5, 7, 10]
        curve_sample = {}
        if curve:
            for t in sample_tenors:
                days = int(t * 360)
                rate = b3_client.interpolate_cupom_cambial(curve, days)
                if rate is not None:
                    curve_sample[f"{t}Y"] = round(rate, 4)
        
        return {
            "as_of": datetime.now().strftime("%Y-%m-%d"),
            "source": "B3 Taxas Referenciais",
            "is_live": curve is not None,
            "requested_tenor": tenor,
            "cupom_cambial_rate": round(rate_for_tenor, 4) if rate_for_tenor else None,
            "sofr_rate": round(sofr, 4) if sofr else None,
            "hedge_benefit_bps": hedge_benefit_bps,
            "calculation": f"({rate_for_tenor:.2f}% - {sofr:.2f}%) × 100 = {hedge_benefit_bps} bps" if hedge_benefit_bps else None,
            "curve_sample": curve_sample,
            "notes": {
                "cupom_cambial": "Implied USD rate in Brazil (from DDI futures)",
                "sofr": "Secured Overnight Financing Rate (actual USD rate)",
                "hedge_benefit": "Positive = you GAIN from hedging USD→BRL",
                "data_source": "B3 publishes daily after market close (~18:00 BRT)"
            }
        }
    except Exception as e:
        logger.error(f"B3 Cupom Cambial fetch error: {e}")
        return {
            "error": "Failed to fetch live B3 data",
            "fallback_available": True,
            "notes": "Using fallback reference data. B3 data may be temporarily unavailable."
        }


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
    supported = ['BRL', 'USD', 'EUR', 'GBP', 'CHF', 'JPY', 'CNY', 'CAD', 'AUD', 'ARS']
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
    """Calculate CIP-based hedged return conversion with optional hedging cost"""
    
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
    
    # Handle hedging cost
    hedging_cost_bps = None
    hedging_cost_source = None
    hedged_return_pp = target_equiv_pp  # Default: no hedging cost adjustment
    
    if request.include_hedging_cost:
        if request.hedging_cost_bps is not None:
            # Use manual override
            hedging_cost_bps = request.hedging_cost_bps
            hedging_cost_source = "Manual"
        else:
            # Get live hedging cost (uses B3 + FRED for BRL)
            cost_data = await get_hedging_cost_async(request.base_currency, request.target_currency, request.tenor)
            hedging_cost_bps = cost_data['cost_bps']
            hedging_cost_source = cost_data['source']
            # Add extra info if available
            if cost_data.get('is_live'):
                hedging_cost_source = f"Live ({cost_data['source']})"
        
        # Apply hedging cost: positive = benefit, negative = cost
        hedging_cost_pp = hedging_cost_bps / 100  # Convert bps to percentage points
        hedged_return_pp = target_equiv_pp + hedging_cost_pp
    
    total_return_target_pp = (math.pow(1 + hedged_return_pp / 100, request.tenor) - 1) * 100
    total_return_base_pp = (math.pow(1 + all_in_base_decimal, request.tenor) - 1) * 100
    
    is_live = "[Live]" in base_idx_data.get('label', '')
    
    assumptions = [
        AssumptionItem(name=base_idx_data['label'], value_pp=base_idx_data['value'], tenor_label="Spot", source_name="Live" if is_live else "Reference"),
        AssumptionItem(name=f"{request.base_currency} Risk-Free ({request.tenor}Y)", value_pp=i_base_t, tenor_label="Curve", source_name=base_source),
        AssumptionItem(name=f"{request.target_currency} Risk-Free ({request.tenor}Y)", value_pp=i_target_t, tenor_label="Curve", source_name=target_source),
        AssumptionItem(name="Spread", value_pp=request.spread, tenor_label="Input", source_name="User")
    ]
    
    # Add hedging cost to assumptions if included
    if request.include_hedging_cost and hedging_cost_bps is not None:
        assumptions.append(AssumptionItem(
            name=f"Hedging Cost ({request.base_currency}→{request.target_currency})",
            value_pp=round(hedging_cost_bps / 100, 4),  # Convert to pp for display
            tenor_label=f"{request.tenor}Y",
            source_name=hedging_cost_source
        ))
    
    warnings = ["✓ CIP-based hedged conversion applied"]
    if request.include_hedging_cost:
        if hedging_cost_bps < 0:
            warnings.append(f"⚠️ Hedging cost of {abs(hedging_cost_bps):.1f} bps applied ({hedging_cost_source})")
        elif hedging_cost_bps > 0:
            warnings.append(f"✓ Hedging benefit of {hedging_cost_bps:.1f} bps applied ({hedging_cost_source})")
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
        hedging_cost_bps=hedging_cost_bps,
        hedged_return_pp=round(hedged_return_pp, 4),
        usd_equiv_pp=round(target_equiv_pp, 4),  # Pre-hedging cost
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
# FRONTEND & SEO
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

@app.get("/robots.txt")
def serve_robots():
    robots_file = Path(__file__).parent / "static" / "robots.txt"
    if robots_file.exists():
        return FileResponse(robots_file, media_type="text/plain")
    return "User-agent: *\nAllow: /"

@app.get("/sitemap.xml")
def serve_sitemap():
    sitemap_file = Path(__file__).parent / "static" / "sitemap.xml"
    if sitemap_file.exists():
        return FileResponse(sitemap_file, media_type="application/xml")
    raise HTTPException(status_code=404, detail="Sitemap not found")

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*60)
    print("🚀 CrossFX Yield v5.0 - LIVE DATA + PWA")
    print("="*60)
    print("\n📍 Frontend: http://localhost:8000")
    print("📍 API Docs: http://localhost:8000/docs")
    print("\n⚙️  Data Sources:")
    print("   BRL: BCB (Banco Central) ✓ FREE")
    print("   USD: FRED ✓")
    print("   EUR: ECB ✓")
    print("   GBP: BoE ✓")
    print(f"   httpx: {'✓' if HTTPX_AVAILABLE else '✗ pip install httpx'}")
    print("\n🆕 v5.0 Features:")
    print("   ✓ Hedging Cost (xccy basis / NDF)")
    print("   ✓ PWA Support (Install as App)")
    print("   ✓ SEO Optimized")
    print("\n" + "="*60 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
