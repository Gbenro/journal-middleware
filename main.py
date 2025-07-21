import os
import httpx
from datetime import datetime
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import logging

# Configure logging - Enhanced for Railway deployment monitoring
# Version 2025-07-21: Added for consistent storage testing
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI app
app = FastAPI(title="Journal Middleware API with Tags")

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration - Environment-based settings for Railway deployment
# Backend URL dynamically configured for production/development environments
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
API_KEY = os.environ.get("API_KEY", "your-secret-api-key")

# Pydantic models
class EntryCreate(BaseModel):
    content: str
    user_id: str
    manual_tags: Optional[List[str]] = []
    auto_tag: Optional[bool] = True

class EntryResponse(BaseModel):
    success: bool
    message: str
    id: Optional[int] = None
    applied_tags: Optional[List[dict]] = []

class TagCreate(BaseModel):
    name: str
    category: Optional[str] = "custom"
    color: Optional[str] = "#808080"
    description: Optional[str] = ""

class TagSuggestionRequest(BaseModel):
    content: str
    limit: Optional[int] = 5

# Dependency for API key authentication - Security layer for GPT Actions
# Updated 2025-07-21: Ensures secure access from authorized GPT instances
async def verify_api_key(x_api_key: str = Header(...)):
    """Verify the API key for GPT authentication"""
    if x_api_key != API_KEY:
        logger.warning("Invalid API key attempted")
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key

@app.get("/health")
async def health_check():
    """Check middleware health and backend connectivity"""
    try:
        # Test backend connection
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BACKEND_URL}/health", timeout=5.0)
            backend_health = response.json()
        
        return {
            "status": "healthy",
            "service": "middleware",
            "backend": backend_health.get("status", "unknown"),
            "backend_url": BACKEND_URL,
            "features": backend_health.get("features", []),
            "timestamp": datetime.utcnow()
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "degraded",
            "service": "middleware",
            "backend": "unreachable",
            "error": str(e),
            "timestamp": datetime.utcnow()
        }

@app.post("/save-entry", response_model=EntryResponse, dependencies=[Depends(verify_api_key)])
async def save_entry(entry: EntryCreate):
    """Save a journal entry with tag support via the backend API"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BACKEND_URL}/api/save",
                json={
                    "content": entry.content,
                    "user_id": entry.user_id,
                    "manual_tags": entry.manual_tags,
                    "auto_tag": entry.auto_tag
                },
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Entry saved successfully for user {entry.user_id} with {data.get('tag_count', 0)} tags")
                return EntryResponse(
                    success=True,
                    message="Journal entry saved successfully",
                    id=data.get("message_id"),
                    applied_tags=data.get("applied_tags", [])
                )
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to save entry to backend"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

@app.get("/get-entries/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_entries(user_id: str, limit: Optional[int] = 50, offset: Optional[int] = 0, tags: Optional[str] = None):
    """Retrieve journal entries for a user with optional tag filtering"""
    try:
        async with httpx.AsyncClient() as client:
            params = {"limit": limit, "offset": offset}
            if tags:
                params["tags"] = tags
                
            response = await client.get(
                f"{BACKEND_URL}/api/messages/{user_id}",
                params=params,
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                messages = data.get("messages", [])
                logger.info(f"Retrieved {len(messages)} entries for user {user_id}")
                
                # Return in a clean format for GPT
                return {
                    "success": True,
                    "entries": messages,
                    "count": len(messages),
                    "filtered_by_tags": data.get("filtered_by_tags")
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to retrieve entries from backend"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

@app.get("/get-entries-by-tags/{user_id}/{tag_names}", dependencies=[Depends(verify_api_key)])
async def get_entries_by_tags(user_id: str, tag_names: str, limit: Optional[int] = 50, offset: Optional[int] = 0):
    """Get entries filtered by specific tags"""
    return await get_entries(user_id, limit, offset, tag_names)

@app.get("/get-tags", dependencies=[Depends(verify_api_key)])
async def get_all_tags():
    """Get all available tags"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BACKEND_URL}/api/tags", timeout=10.0)
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Retrieved {data.get('count', 0)} tags")
                return data
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to retrieve tags from backend"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

@app.get("/get-tags-by-category", dependencies=[Depends(verify_api_key)])
async def get_tags_by_category():
    """Get tags grouped by category"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BACKEND_URL}/api/tags/categories", timeout=10.0)
            
            if response.status_code == 200:
                data = response.json()
                logger.info("Retrieved tags grouped by category")
                return data
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to retrieve tags by category from backend"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

@app.post("/create-tag", dependencies=[Depends(verify_api_key)])
async def create_tag(tag: TagCreate):
    """Create a new custom tag"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BACKEND_URL}/api/tags",
                json={
                    "name": tag.name,
                    "category": tag.category,
                    "color": tag.color,
                    "description": tag.description
                },
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Created tag: {tag.name}")
                return data
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                error_detail = response.json().get("detail", "Failed to create tag")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=error_detail
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

@app.post("/suggest-tags", dependencies=[Depends(verify_api_key)])
async def suggest_tags(request: TagSuggestionRequest):
    """Get tag suggestions for given content"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BACKEND_URL}/api/tags/suggestions",
                json={
                    "content": request.content,
                    "limit": request.limit
                },
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Generated {data.get('count', 0)} tag suggestions")
                return data
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to get tag suggestions from backend"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

# Summary endpoints for sacred insights
@app.get("/get-daily-summary/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_daily_summary(user_id: str):
    """Get today's sacred reflection"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/summary/{user_id}/current/daily",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                summary = data.get("summary")
                logger.info(f"Retrieved daily summary for user {user_id}")
                
                return {
                    "success": True,
                    "sacred_summary": summary.get("sacred_summary"),
                    "entry_count": summary.get("entry_count", 0),
                    "dominant_themes": summary.get("dominant_tags", []),
                    "energy_signature": summary.get("energy_signature", {}),
                    "wisdom_insights": summary.get("wisdom_insights", [])
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to retrieve daily summary"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

@app.get("/get-weekly-summary/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_weekly_summary(user_id: str):
    """Get current week's pattern insights"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/summary/{user_id}/current/weekly",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                summary = data.get("summary")
                logger.info(f"Retrieved weekly summary for user {user_id}")
                
                return {
                    "success": True,
                    "sacred_summary": summary.get("sacred_summary"),
                    "entry_count": summary.get("entry_count", 0),
                    "dominant_themes": summary.get("dominant_tags", []),
                    "energy_signature": summary.get("energy_signature", {}),
                    "patterns": summary.get("patterns", {}),
                    "wisdom_insights": summary.get("wisdom_insights", [])
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to retrieve weekly summary"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

@app.get("/get-monthly-summary/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_monthly_summary(user_id: str):
    """Get current month's wisdom insights"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/summary/{user_id}/current/monthly",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                summary = data.get("summary")
                logger.info(f"Retrieved monthly summary for user {user_id}")
                
                return {
                    "success": True,
                    "sacred_summary": summary.get("sacred_summary"),
                    "entry_count": summary.get("entry_count", 0),
                    "dominant_themes": summary.get("dominant_tags", []),
                    "energy_signature": summary.get("energy_signature", {}),
                    "patterns": summary.get("patterns", {}),
                    "wisdom_insights": summary.get("wisdom_insights", [])
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to retrieve monthly summary"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

@app.post("/generate-period-summary", dependencies=[Depends(verify_api_key)])
async def generate_period_summary(period_type: str, user_id: str, target_date: Optional[str] = None):
    """Generate a sacred summary for any specific period"""
    try:
        if period_type not in ["daily", "weekly", "monthly"]:
            raise HTTPException(status_code=400, detail="Period type must be daily, weekly, or monthly")
            
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BACKEND_URL}/api/generate-summary/{period_type}",
                json={
                    "user_id": user_id,
                    "target_date": target_date
                },
                timeout=15.0
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Generated {period_type} summary for user {user_id}")
                
                return {
                    "success": True,
                    "period_type": period_type,
                    "sacred_summary": data.get("sacred_summary"),
                    "analysis": data.get("analysis"),
                    "wisdom_insights": data.get("wisdom_insights", [])
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to generate {period_type} summary"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

@app.get("/get-growth-insights/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_growth_insights(user_id: str):
    """Get growth tracking and transformation insights"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/insights/{user_id}/growth",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Retrieved growth insights for user {user_id}")
                return data
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to retrieve growth insights"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

@app.get("/")
async def root():
    """Root endpoint - Deployment test 2025-07-21
    Provides comprehensive API documentation for GPT Actions integration"""
    return {
        "service": "Mirror Scribe Middleware API with Sacred Summaries",
        "version": "3.0.0",
        "features": [
            "intelligent_tagging",
            "auto_tag_suggestions",
            "manual_tags", 
            "tag_filtering",
            "custom_tags",
            "category_organization",
            "sacred_summaries",
            "pattern_analysis",
            "energy_signatures",
            "wisdom_extraction",
            "growth_tracking"
        ],
        "endpoints": [
            "/health",
            "/save-entry",
            "/get-entries/{user_id}",
            "/get-entries-by-tags/{user_id}/{tag_names}",
            "/get-tags",
            "/get-tags-by-category",
            "/create-tag",
            "/suggest-tags",
            "/get-daily-summary/{user_id}",
            "/get-weekly-summary/{user_id}",
            "/get-monthly-summary/{user_id}",
            "/generate-period-summary",
            "/get-growth-insights/{user_id}"
        ],
        "authentication": "API key required in X-API-Key header",
        "gpt_usage": {
            "save_with_tags": "POST /save-entry with manual_tags array",
            "auto_tagging": "Set auto_tag=true in save request",
            "filter_by_tags": "GET /get-entries/{user_id}?tags=work,coding",
            "get_suggestions": "POST /suggest-tags with content",
            "create_custom_tags": "POST /create-tag",
            "daily_reflection": "GET /get-daily-summary/{user_id}",
            "weekly_patterns": "GET /get-weekly-summary/{user_id}",
            "monthly_wisdom": "GET /get-monthly-summary/{user_id}",
            "growth_insights": "GET /get-growth-insights/{user_id}",
            "generate_summary": "POST /generate-period-summary"
        }
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)