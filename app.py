"""
Hedged Return Converter - Complete FastAPI Backend
Production-ready with proper calculations
"""

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
import math
import os
from pathlib import Path

# ============================================================================
# APP SETUP
# ============================================================================

app = FastAPI(
    title="Hedged Return Converter API",
    description="CIP-based hedged return calculations across currencies",
    version="1.0.0"
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
# DATA REPOSITORIES
# ============================================================================

INDEXERS_BY_CCY = {
    'BRL': [
        {'key': 'selic', 'label': 'SELIC (Brazil)', 'value': 15.0},
        {'key': 'ipca', 'label': 'IPCA (Brazil)', 'value': 4.26}
    ],
    'USD': [
        {'key': 'fed-funds', 'label': 'Fed Funds Rate', 'value': 3.75},
        {'key': 'sofr', 'label': 'SOFR (USD)', 'value': 3.65},
        {'key': 'bond-10yr', 'label': '10Y US Treasury', 'value': 4.25}
    ],
    'EUR': [
        {'key': 'euribor', 'label': 'EURIBOR 12M', 'value': 2.50}
    ],
    'GBP': [
        {'key': 'gbp-sonia', 'label': 'GBP SONIA', 'value': 4.9}
    ]
}

INDEXERS_BY_KEY = {}
for ccy in INDEXERS_BY_CCY:
    for idx in INDEXERS_BY_CCY[ccy]:
        INDEXERS_BY_KEY[idx['key']] = idx

RISK_FREE_RATES = {
    'BRL': {1: 15.00, 2: 14.75, 4: 14.50, 5: 14.25, 10: 13.75},
    'USD': {1: 5.20, 2: 4.95, 4: 4.50, 5: 4.35, 10: 4.20},
    'EUR': {1: 3.60, 2: 3.45, 4: 3.30, 5: 3.20, 10: 3.10},
    'GBP': {1: 5.50, 2: 5.25, 4: 4.95, 5: 4.80, 10: 4.50}
}

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def get_risk_free_rate(ccy: str, tenor: float) -> Optional[float]:
    """Get risk-free rate for a currency and tenor with linear interpolation"""
    if ccy not in RISK_FREE_RATES:
        return None
    
    rates = RISK_FREE_RATES[ccy]
    if tenor in rates:
        return rates[tenor]
    
    keys = sorted([k for k in rates.keys()])
    for i in range(len(keys) - 1):
        t1, t2 = keys[i], keys[i + 1]
        if t1 <= tenor <= t2:
            r1, r2 = rates[t1], rates[t2]
            return r1 + ((tenor - t1) / (t2 - t1)) * (r2 - r1)
    
    return None

# ============================================================================
# HEALTH & INFO ENDPOINTS
# ============================================================================

@app.get("/api/", tags=["Info"])
def root():
    """API root - returns service info"""
    return {
        "service": "Hedged Return Converter API",
        "version": "1.0.0",
        "documentation": "/docs",
        "status": "✅ Running"
    }

@app.get("/api/health", tags=["Info"])
def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

# ============================================================================
# INDEXER ENDPOINTS
# ============================================================================

@app.get("/api/indexers", tags=["Reference Data"])
def list_all_indexers():
    """List all available indexers grouped by currency"""
    return {ccy: indexers for ccy, indexers in INDEXERS_BY_CCY.items()}

@app.get("/api/indexers/{currency}", tags=["Reference Data"])
def get_indexers_by_currency(currency: str):
    """Get indexers for a specific currency"""
    currency = currency.upper()
    if currency not in INDEXERS_BY_CCY:
        raise HTTPException(status_code=404, detail=f"Currency {currency} not found")
    
    return {
        "currency": currency,
        "indexers": [IndexerResponse(**idx) for idx in INDEXERS_BY_CCY[currency]]
    }

# ============================================================================
# RISK-FREE RATE ENDPOINTS
# ============================================================================

@app.get("/api/risk-free-rates/{currency}", tags=["Reference Data"])
def get_risk_free_curve(currency: str):
    """Get risk-free yield curve for a currency"""
    currency = currency.upper()
    if currency not in RISK_FREE_RATES:
        raise HTTPException(status_code=404, detail=f"Currency {currency} not found")
    
    rates = RISK_FREE_RATES[currency]
    return {
        "currency": currency,
        "curve": {str(k): v for k, v in rates.items()},
        "source": "Mock Data"
    }

@app.post("/api/risk-free-rates/interpolate", tags=["Reference Data"], response_model=RiskFreeRateResponse)
def interpolate_risk_free_rate(request: RiskFreeRateRequest):
    """Interpolate risk-free rate for specific tenor"""
    rate = get_risk_free_rate(request.currency, request.tenor)
    if rate is None:
        raise HTTPException(
            status_code=404,
            detail=f"Risk-free rate not available for {request.currency} tenor {request.tenor}Y"
        )
    
    return RiskFreeRateResponse(
        currency=request.currency,
        tenor=request.tenor,
        rate=rate,
        source="Mock Data (Linear Interpolation)"
    )

# ============================================================================
# CIP CALCULATION ENDPOINT - THE CORE LOGIC
# ============================================================================

@app.post("/api/calculate/hedged-return", tags=["Calculations"], response_model=CIPCalculationResponse)
def calculate_hedged_return(request: CIPCalculationRequest):
    """Calculate CIP-based hedged return conversion"""
    
    # Validate indexer
    if request.base_indexer_key not in INDEXERS_BY_KEY:
        raise HTTPException(status_code=400, detail="Invalid indexer key")
    
    base_idx_data = INDEXERS_BY_KEY[request.base_indexer_key]
    
    # Get risk-free rates
    i_base_t = get_risk_free_rate(request.base_currency, request.tenor)
    i_target_t = get_risk_free_rate(request.target_currency, request.tenor)
    
    if i_base_t is None or i_target_t is None:
        raise HTTPException(
            status_code=400,
            detail="Risk-free rates not available for requested currencies/tenor"
        )
    
    # ========== CORE CIP CALCULATION ==========
    
    # Step 1: Calculate all-in return in base currency
    spread_decimal = request.spread / 100
    base_indexer_decimal = base_idx_data['value'] / 100
    all_in_base_pp = ((1 + base_indexer_decimal) * (1 + spread_decimal) - 1) * 100
    
    # Step 2: Apply CIP formula for USD equivalent return
    i_base_decimal = i_base_t / 100
    i_target_decimal = i_target_t / 100
    all_in_base_decimal = all_in_base_pp / 100
    
    # CIP: USD_Return = (1 + All_In_Base) * (1 + i_target) / (1 + i_base) - 1
    usd_equiv_decimal = (1 + all_in_base_decimal) * ((1 + i_target_decimal) / (1 + i_base_decimal)) - 1
    usd_equiv_pp = usd_equiv_decimal * 100
    
    # Step 3: Calculate total return over tenor (compound)
    total_return_decimal = math.pow(1 + usd_equiv_decimal, request.tenor) - 1
    total_return_pp = total_return_decimal * 100
    
    # ========== BUILD RESPONSE ==========
    
    assumptions = [
        AssumptionItem(
            name=base_idx_data['label'],
            value_pp=base_idx_data['value'],
            tenor_label="Spot",
            source_name="Mock Data"
        ),
        AssumptionItem(
            name=f"{request.base_currency} Risk-Free ({request.tenor}Y)",
            value_pp=i_base_t,
            tenor_label="Discount Curve",
            source_name="Mock DI/Treasury"
        ),
        AssumptionItem(
            name=f"{request.target_currency} Risk-Free ({request.tenor}Y)",
            value_pp=i_target_t,
            tenor_label="Discount Curve",
            source_name="Mock Treasury"
        ),
        AssumptionItem(
            name="Spread",
            value_pp=request.spread,
            tenor_label="User Input",
            source_name="Input"
        )
    ]
    
    warnings = [
        "⚠️ Mock data for testing",
        "✓ CIP-based hedged conversion applied",
        "Basis adjustment: 0 bp (not included)"
    ]
    
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
        usd_equiv_pp=round(usd_equiv_pp, 4),
        total_return_pp=round(total_return_pp, 4),
        assumptions=assumptions,
        warnings=warnings
    )

# ============================================================================
# FRONTEND SERVING
# ============================================================================

# Mount static files (CSS, JS, etc)
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
# RUN THE SERVER
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*60)
    print("🚀 Hedged Return Converter - STARTING")
    print("="*60)
    print("\n📍 Frontend: http://localhost:8000")
    print("📍 API Docs: http://localhost:8000/docs")
    print("📍 API Endpoints: http://localhost:8000/api/\n")
    print("Press CTRL+C to stop the server\n")
    print("="*60 + "\n")
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
