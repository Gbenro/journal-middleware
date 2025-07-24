import os
import httpx
from datetime import datetime
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import logging
import asyncio
import re

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
OBSERVER_URL = os.environ.get("OBSERVER_URL", "http://localhost:8001")
OBSERVER_ENABLED = os.environ.get("OBSERVER_ENABLED", "true").lower() == "true"

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

# Fire-and-forget observer logging function
async def log_to_observer(user_id: str, action_type: str, success: bool = True, 
                         response_time: Optional[float] = None, error_message: Optional[str] = None,
                         metadata: Optional[dict] = None):
    """Fire-and-forget logging to observer service"""
    if not OBSERVER_ENABLED:
        logger.info(f"🔕 Observer disabled - skipping log for {action_type}")
        return
    
    logger.info(f"📤 Attempting to log to observer: {action_type} for user {user_id}")
    
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.post(
                f"{OBSERVER_URL}/observe",
                json={
                    "user_id": user_id,
                    "action_type": action_type,
                    "success": success,
                    "response_time": response_time,
                    "error_message": error_message,
                    "metadata": metadata
                }
            )
            logger.info(f"✅ Observer log successful: {response.status_code}")
    except httpx.ConnectError as e:
        logger.warning(f"🔌 Observer connection failed: {OBSERVER_URL} - {str(e)}")
    except httpx.TimeoutException as e:
        logger.warning(f"⏱️ Observer timeout: {OBSERVER_URL}")
    except Exception as e:
        logger.warning(f"❌ Observer logging failed: {type(e).__name__}: {str(e)}")

@app.get("/test-observer")
async def test_observer():
    """Test endpoint to verify observer connectivity"""
    logger.info("🧪 Testing observer connection...")
    
    # Create a test task
    test_task = asyncio.create_task(log_to_observer(
        user_id="test-user",
        action_type="test",
        success=True,
        response_time=123.45,
        metadata={"test": True, "timestamp": datetime.now().isoformat()}
    ))
    
    # Wait for it to complete (normally we wouldn't wait)
    try:
        await asyncio.wait_for(test_task, timeout=2.0)
        return {
            "status": "test_sent",
            "observer_url": OBSERVER_URL,
            "observer_enabled": OBSERVER_ENABLED,
            "message": "Check observer logs for test interaction"
        }
    except asyncio.TimeoutError:
        return {
            "status": "timeout",
            "observer_url": OBSERVER_URL,
            "observer_enabled": OBSERVER_ENABLED,
            "message": "Observer request timed out"
        }

@app.get("/health")
async def health_check():
    """Check middleware health and backend connectivity"""
    try:
        # Test backend connection
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BACKEND_URL}/health", timeout=5.0)
            backend_health = response.json()
        
        # Test observer connection if enabled
        observer_status = "disabled"
        if OBSERVER_ENABLED:
            try:
                async with httpx.AsyncClient() as client:
                    obs_response = await client.get(f"{OBSERVER_URL}/", timeout=2.0)
                    observer_status = "connected" if obs_response.status_code == 200 else "unreachable"
            except:
                observer_status = "unreachable"
        
        return {
            "status": "healthy",
            "service": "middleware",
            "backend": backend_health.get("status", "unknown"),
            "backend_url": BACKEND_URL,
            "observer": {
                "status": observer_status,
                "url": OBSERVER_URL,
                "enabled": OBSERVER_ENABLED
            },
            "features": backend_health.get("features", []),
            "timestamp": datetime.utcnow()
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "degraded",
            "service": "middleware",
            "backend": "unreachable",
            "observer": {
                "status": "unknown",
                "url": OBSERVER_URL,
                "enabled": OBSERVER_ENABLED
            },
            "error": str(e),
            "timestamp": datetime.utcnow()
        }

@app.post("/save-entry", response_model=EntryResponse, dependencies=[Depends(verify_api_key)])
async def save_entry(entry: EntryCreate):
    """Save a journal entry with tag support via the backend API"""
    start_time = datetime.now()
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
                
                # Parse and process any enhancement suggestions in the content
                enhancements = parse_enhancements_from_content(entry.content, entry.user_id)
                enhancement_count = 0
                
                if enhancements:
                    logger.info(f"💡 Found {len(enhancements)} enhancement suggestions in entry")
                    for enhancement in enhancements:
                        try:
                            # Send enhancement to proxy agent
                            asyncio.create_task(log_enhancement_to_proxy(
                                title=enhancement["title"],
                                description=enhancement["description"],
                                category=enhancement["category"],
                                priority=enhancement["priority"],
                                reasoning=enhancement["reasoning"],
                                triggered_by=enhancement["triggered_by"],
                                user_context=enhancement["user_context"]
                            ))
                            enhancement_count += 1
                            logger.info(f"✨ Enhancement '{enhancement['title']}' queued for processing")
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to process enhancement: {e}")
                
                # Log to observer service (fire-and-forget)
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=entry.user_id,
                    action_type="store",
                    success=True,
                    response_time=response_time,
                    metadata={
                        "tag_count": data.get("tag_count", 0),
                        "auto_tagged": entry.auto_tag,
                        "manual_tags": len(entry.manual_tags) if entry.manual_tags else 0,
                        "enhancement_count": enhancement_count
                    }
                ))
                
                response_message = "Journal entry saved successfully"
                if enhancement_count > 0:
                    response_message += f" with {enhancement_count} enhancement{'s' if enhancement_count != 1 else ''} detected"
                
                return EntryResponse(
                    success=True,
                    message=response_message,
                    id=data.get("message_id"),
                    applied_tags=data.get("applied_tags", [])
                )
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                
                # Log failure to observer
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=entry.user_id,
                    action_type="store",
                    success=False,
                    response_time=response_time,
                    error_message=f"Backend error: {response.status_code}"
                ))
                
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to save entry to backend"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        
        # Log failure to observer
        response_time = (datetime.now() - start_time).total_seconds() * 1000
        asyncio.create_task(log_to_observer(
            user_id=entry.user_id,
            action_type="store",
            success=False,
            response_time=response_time,
            error_message=f"Request error: {str(e)}"
        ))
        
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        
        # Log failure to observer
        response_time = (datetime.now() - start_time).total_seconds() * 1000
        asyncio.create_task(log_to_observer(
            user_id=entry.user_id,
            action_type="store",
            success=False,
            response_time=response_time,
            error_message=f"Unexpected error: {str(e)}"
        ))
        
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred. Please try again."
        )

@app.get("/get-entries/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_entries(user_id: str, limit: Optional[int] = 50, offset: Optional[int] = 0, tags: Optional[str] = None):
    """Retrieve journal entries for a user with optional tag filtering"""
    start_time = datetime.now()
    action_type = "search" if tags else "recall"
    
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
                
                # Log to observer service (fire-and-forget)
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type=action_type,
                    success=True,
                    response_time=response_time,
                    metadata={
                        "entry_count": len(messages),
                        "limit": limit,
                        "offset": offset,
                        "tags_filter": tags if tags else None
                    }
                ))
                
                # Return in a clean format for GPT
                return {
                    "success": True,
                    "entries": messages,
                    "count": len(messages),
                    "filtered_by_tags": data.get("filtered_by_tags")
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                
                # Log failure to observer
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type=action_type,
                    success=False,
                    response_time=response_time,
                    error_message=f"Backend error: {response.status_code}"
                ))
                
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
    start_time = datetime.now()
    
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
                
                # Log to observer service (fire-and-forget)
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="analyze",
                    success=True,
                    response_time=response_time,
                    metadata={
                        "summary_type": "daily",
                        "entry_count": summary.get("entry_count", 0),
                        "dominant_tags": len(summary.get("dominant_tags", []))
                    }
                ))
                
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
                
                # Log failure to observer
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="analyze",
                    success=False,
                    response_time=response_time,
                    error_message=f"Backend error: {response.status_code}",
                    metadata={"summary_type": "daily"}
                ))
                
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

# Mirror Scribe editorial fluency endpoints
@app.put("/refine-entry/{entry_id}", dependencies=[Depends(verify_api_key)])
async def refine_journal_entry(entry_id: int, refinement_data: dict):
    """Allow Mirror Scribe to refine and expand entries"""
    try:
        async with httpx.AsyncClient() as client:
            # Extract refinement details
            new_content = refinement_data.get("content")
            new_tags = refinement_data.get("tags")
            new_energy = refinement_data.get("energy_signature")
            intention_flag = refinement_data.get("intention_flag")
            
            # Call backend update endpoint
            response = await client.put(
                f"{BACKEND_URL}/api/entries/{entry_id}",
                json={
                    "content": new_content,
                    "tags": new_tags,
                    "energy_signature": new_energy,
                    "intention_flag": intention_flag
                },
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"✏️ Refined entry {entry_id}")
                
                return {
                    "success": True,
                    "message": "Entry refined successfully by Mirror Scribe",
                    "entry_id": entry_id,
                    "refinements_applied": {
                        "content": new_content is not None,
                        "tags": new_tags is not None,
                        "energy": new_energy is not None,
                        "intention": intention_flag is not None
                    }
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to refine entry"
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

@app.post("/connect-entries", dependencies=[Depends(verify_api_key)])
async def create_entry_connections(connection_data: dict):
    """Create thematic and temporal connections between entries"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BACKEND_URL}/api/entries/connect",
                json=connection_data,
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"🔗 Connected entries {connection_data.get('from_entry_id')} -> {connection_data.get('to_entry_id')}")
                
                return {
                    "success": True,
                    "message": "Entries connected with sacred geometry",
                    "connection": data.get("connection", {}),
                    "wisdom": f"Sacred thread woven between moments of {connection_data.get('connection_type', 'connection')}"
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to create entry connections"
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

@app.put("/set-intention/{entry_id}", dependencies=[Depends(verify_api_key)])
async def mark_as_intention(entry_id: int, intention_data: dict):
    """Mark entry as daily/weekly/monthly intention"""
    try:
        intention_flag = intention_data.get("intention", True)
        intention_type = intention_data.get("type", "general")
        
        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"{BACKEND_URL}/api/entries/{entry_id}/intention?intention={intention_flag}",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"🎯 {'Set' if intention_flag else 'Removed'} intention flag for entry {entry_id}")
                
                return {
                    "success": True,
                    "message": f"Entry {'marked' if intention_flag else 'unmarked'} as sacred intention",
                    "entry_id": entry_id,
                    "intention_type": intention_type,
                    "sacred_note": f"{'The universe holds your intention' if intention_flag else 'Intention released to the flow'}"
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to set intention flag"
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

@app.get("/relationship-summary/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_relationship_summary(user_id: str, period: str = "weekly"):
    """Get relationship insights for summaries"""
    try:
        # Convert period to days
        period_days = {"daily": 1, "weekly": 7, "monthly": 30}.get(period, 7)
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/relationships/{user_id}?period_days={period_days}",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                insights = data.get("insights", {})
                
                # Transform into sacred language
                sacred_insights = []
                
                most_mentioned = insights.get("most_mentioned", [])
                if most_mentioned:
                    for person in most_mentioned[:3]:  # Top 3
                        emotion = person.get("primary_emotion", "neutral")
                        sacred_insights.append(f"{person['name']} carries {emotion} energy through {person['mentions']} sacred moments")
                
                emotional_patterns = insights.get("emotional_patterns", {})
                dominant_emotion = max(emotional_patterns.items(), key=lambda x: x[1])[0] if emotional_patterns else "balanced"
                
                logger.info(f"👥 Retrieved relationship summary for user {user_id}")
                
                return {
                    "success": True,
                    "period": period,
                    "active_relationships": insights.get("active_relationships", 0),
                    "dominant_emotional_field": dominant_emotion,
                    "sacred_insights": sacred_insights,
                    "relationship_types": insights.get("relationship_types", {}),
                    "wisdom": f"Your {period} field held {insights.get('active_relationships', 0)} sacred connections"
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to retrieve relationship summary"
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

@app.post("/manual-energy/{entry_id}", dependencies=[Depends(verify_api_key)])
async def set_manual_energy(entry_id: int, energy_data: dict):
    """Override auto-detected energy signature"""
    try:
        energy_signature = energy_data.get("energy_signature")
        
        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"{BACKEND_URL}/api/entries/{entry_id}",
                json={"energy_signature": energy_signature},
                timeout=10.0
            )
            
            if response.status_code == 200:
                logger.info(f"⚡ Set manual energy signature for entry {entry_id}: {energy_signature}")
                
                return {
                    "success": True,
                    "message": "Energy signature attuned by Mirror Scribe",
                    "entry_id": entry_id,
                    "energy_signature": energy_signature,
                    "sacred_note": f"Field recalibrated to {energy_signature} resonance"
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to set energy signature"
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

@app.get("/entry-connections/{entry_id}", dependencies=[Depends(verify_api_key)])
async def get_entry_connections(entry_id: int, connection_types: Optional[str] = None):
    """Get sacred connections for an entry"""
    try:
        params = {}
        if connection_types:
            params["connection_types"] = connection_types
            
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/entries/{entry_id}/connected",
                params=params,
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                connections = data.get("connected_entries", [])
                
                # Transform into sacred language
                sacred_connections = []
                for conn in connections:
                    sacred_connections.append({
                        "entry_id": conn.get("id"),
                        "content_preview": conn.get("content", "")[:100] + "..." if len(conn.get("content", "")) > 100 else conn.get("content", ""),
                        "connection_type": conn.get("connection_type"),
                        "strength": conn.get("connection_strength", 1.0),
                        "direction": conn.get("connection_direction"),
                        "timestamp": conn.get("timestamp")
                    })
                
                logger.info(f"🔍 Retrieved {len(connections)} connections for entry {entry_id}")
                
                return {
                    "success": True,
                    "entry_id": entry_id,
                    "connections": sacred_connections,
                    "count": len(connections),
                    "wisdom": f"Sacred web reveals {len(connections)} threads of connection"
                }
            else:
                logger.error(f"Backend returned error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to get entry connections"
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

# Sacred Thread Navigation Endpoints with Poetic Responses
@app.get("/gpt/tags/explore/{user_id}", dependencies=[Depends(verify_api_key)])
async def explore_sacred_threads(user_id: str):
    """Explore user's tag landscape with interpretive insights"""
    start_time = datetime.now()
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/gpt/tags/list/{user_id}",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                tags = data.get("tags", [])
                
                # Add poetic interpretations
                sacred_insights = []
                for tag in tags[:10]:  # Top 10 most used
                    tag_name = tag["tag"]
                    count = tag["count"]
                    first_used = tag["first_used"]
                    
                    # Calculate time since first use
                    if first_used:
                        first_date = datetime.fromisoformat(first_used.replace('Z', '+00:00'))
                        days_ago = (datetime.now(first_date.tzinfo) - first_date).days
                        
                        if count >= 5:
                            insight = f"Your '{tag_name}' thread weaves through {count} entries, growing stronger since {days_ago} days ago"
                        elif count >= 3:
                            insight = f"'{tag_name}' emerges as a gentle pattern across {count} moments"
                        else:
                            insight = f"'{tag_name}' appears {count} times - a seed of awareness"
                    else:
                        insight = f"'{tag_name}' carries the essence of {count} sacred moments"
                    
                    sacred_insights.append({
                        "tag": tag_name,
                        "count": count,
                        "category": tag["category"],
                        "wisdom": insight
                    })
                
                # Log successful operation
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="analyze",
                    success=True,
                    response_time=response_time,
                    metadata={"endpoint": "explore_tags", "tag_count": len(tags)}
                ))
                
                return {
                    "success": True,
                    "user_id": user_id,
                    "total_threads": len(tags),
                    "sacred_landscape": sacred_insights,
                    "wisdom": f"Your consciousness has woven {len(tags)} unique threads through the tapestry of time"
                }
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to explore sacred threads"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Sacred thread exploration unavailable. Please try again later."
        )

@app.get("/gpt/tags/evolution/{user_id}", dependencies=[Depends(verify_api_key)])
async def view_tag_evolution(user_id: str, period: str = "weekly"):
    """View temporal evolution with growth narrative"""
    start_time = datetime.now()
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/gpt/tags/temporal/{user_id}?period={period}",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                periods = data.get("periods", [])
                
                # Create growth narrative
                evolutionary_insights = []
                for period_data in periods[:6]:  # Last 6 periods
                    new_tags = period_data.get("new_tags", [])
                    trending = period_data.get("trending", [])
                    period_name = period_data.get("period")
                    
                    narrative = f"During {period_name}: "
                    if new_tags:
                        narrative += f"New consciousness emerged through '{', '.join(new_tags[:3])}'. "
                    if trending:
                        narrative += f"Energy flowed strongest through '{trending[0]}'"
                    
                    evolutionary_insights.append({
                        "period": period_name,
                        "new_emergence": new_tags,
                        "dominant_flow": trending,
                        "narrative": narrative
                    })
                
                # Log successful operation
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="analyze",
                    success=True,
                    response_time=response_time,
                    metadata={"endpoint": "tag_evolution", "period": period}
                ))
                
                return {
                    "success": True,
                    "user_id": user_id,
                    "period_type": period,
                    "evolution_story": evolutionary_insights,
                    "sacred_wisdom": f"Your {period} journey reveals the dance of expanding awareness through time"
                }
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to access evolutionary timeline"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Evolution timeline unavailable. Please try again later."
        )

@app.get("/gpt/tags/thread/{tag_name}", dependencies=[Depends(verify_api_key)])
async def follow_sacred_thread(tag_name: str, user_id: str):
    """Follow a specific tag thread with sacred storytelling"""
    start_time = datetime.now()
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/gpt/tags/preview/{tag_name}?user_id={user_id}",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                entries = data.get("entries", [])
                total_entries = data.get("total_entries", 0)
                first_use = data.get("first_use")
                
                # Create sacred thread narrative
                thread_story = []
                for i, entry in enumerate(entries):
                    preview = entry["preview"]
                    date = entry["date"]
                    emotion = entry["emotion"]
                    
                    # Create poetic description
                    if i == 0:
                        story_line = f"Most recently, on {date[:10]}, '{tag_name}' manifested as: \"{preview}\" (Energy: {emotion})"
                    elif i < 3:
                        story_line = f"Earlier, this thread wove through: \"{preview}\" (Energy: {emotion})"
                    else:
                        story_line = f"The pattern continues: \"{preview}\""
                    
                    thread_story.append({
                        "date": date,
                        "preview": preview,
                        "emotion": emotion,
                        "story": story_line
                    })
                
                # Calculate thread age
                thread_wisdom = f"The '{tag_name}' thread has been weaving through your consciousness"
                if first_use:
                    first_date = datetime.fromisoformat(first_use.replace('Z', '+00:00'))
                    days_ago = (datetime.now(first_date.tzinfo) - first_date).days
                    thread_wisdom += f" for {days_ago} days, appearing in {total_entries} sacred moments"
                
                # Log successful operation
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="search",
                    success=True,
                    response_time=response_time,
                    metadata={"endpoint": "sacred_thread", "tag": tag_name, "entries": len(entries)}
                ))
                
                return {
                    "success": True,
                    "tag": tag_name,
                    "total_manifestations": total_entries,
                    "thread_story": thread_story,
                    "sacred_wisdom": thread_wisdom,
                    "evolution_insight": f"This thread shows the deepening of your '{tag_name}' awareness across time"
                }
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Sacred thread '{tag_name}' not found or inaccessible"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Sacred thread navigation unavailable. Please try again later."
        )

@app.get("/gpt/tags/web-of-connection/{user_id}", dependencies=[Depends(verify_api_key)])
async def view_sacred_web(user_id: str):
    """View the interconnected web of tag relationships"""
    start_time = datetime.now()
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/gpt/tags/sacred-threads/{user_id}",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                relationships = data.get("tag_relationships", [])
                timeline = data.get("tag_timeline", [])
                insights = data.get("insights", {})
                
                # Create web narrative
                connection_wisdom = []
                for rel in relationships[:5]:  # Top 5 connections
                    tags = rel["tags"]
                    strength = rel["strength"]
                    wisdom = f"'{tags[0]}' and '{tags[1]}' dance together across {strength} shared moments - a sacred partnership"
                    connection_wisdom.append({
                        "connection": tags,
                        "strength": strength,
                        "wisdom": wisdom
                    })
                
                # Timeline insights
                active_threads = [t for t in timeline if t["status"] == "active"]
                dormant_threads = [t for t in timeline if t["status"] == "dormant"]
                
                # Log successful operation
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="analyze",
                    success=True,
                    response_time=response_time,
                    metadata={"endpoint": "sacred_web", "relationships": len(relationships)}
                ))
                
                return {
                    "success": True,
                    "user_id": user_id,
                    "sacred_connections": connection_wisdom,
                    "active_threads": len(active_threads),
                    "dormant_threads": len(dormant_threads),
                    "web_wisdom": f"Your consciousness weaves {len(relationships)} sacred connections, with {len(active_threads)} threads currently flowing",
                    "deepest_connection": relationships[0] if relationships else None
                }
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Sacred web inaccessible"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Sacred web navigation unavailable. Please try again later."
        )

@app.get("/test-enhancement")
async def test_enhancement():
    """Create a test enhancement suggestion and send to proxy agent"""
    try:
        if not OBSERVER_ENABLED:
            return {
                "status": "skipped",
                "message": "Observer is disabled",
                "observer_enabled": OBSERVER_ENABLED
            }
        
        # Create test enhancement
        test_enhancement = {
            "title": "Test Enhancement from Middleware",
            "description": "This is a test enhancement suggestion to verify the complete flow from middleware to proxy agent to backend.",
            "category": "testing",
            "priority": "low",
            "reasoning": "Testing the enhancement system integration",
            "triggered_by": "middleware-test-endpoint",
            "user_context": {
                "test": True,
                "timestamp": datetime.now().isoformat(),
                "source": "middleware"
            }
        }
        
        logger.info(f"📤 Sending test enhancement to observer: {OBSERVER_URL}/enhancements")
        
        # Send to proxy agent
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{OBSERVER_URL}/enhancements",
                json=test_enhancement
            )
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"✅ Test enhancement sent successfully: {result}")
                
                return {
                    "status": "success",
                    "message": "Test enhancement sent to observer proxy",
                    "enhancement": test_enhancement,
                    "observer_response": result,
                    "timestamp": datetime.utcnow().isoformat()
                }
            else:
                logger.error(f"❌ Observer returned error: {response.status_code}")
                return {
                    "status": "error",
                    "message": f"Observer returned status {response.status_code}",
                    "observer_url": OBSERVER_URL
                }
                
    except httpx.ConnectError as e:
        logger.error(f"🔌 Observer connection failed: {OBSERVER_URL} - {str(e)}")
        return {
            "status": "error",
            "message": "Could not connect to observer proxy",
            "observer_url": OBSERVER_URL,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"❌ Test enhancement failed: {e}")
        return {
            "status": "error",
            "message": "Test enhancement failed",
            "error": str(e)
        }

# Enhancement parsing function for GPT responses
def parse_enhancements_from_content(content: str, user_id: str) -> List[dict]:
    """Parse enhancement suggestions from journal entry content"""
    enhancements = []
    
    # Pattern 1: [ENHANCEMENT] block format
    enhancement_pattern = r'\[ENHANCEMENT\](.*?)\[/ENHANCEMENT\]'
    matches = re.findall(enhancement_pattern, content, re.DOTALL | re.IGNORECASE)
    
    for match in matches:
        enhancement = parse_enhancement_block(match.strip(), user_id)
        if enhancement:
            enhancements.append(enhancement)
    
    # Pattern 2: Single line enhancement format
    # [ENHANCEMENT: category] title - description
    single_pattern = r'\[ENHANCEMENT:\s*(\w+)\]\s*([^-]+)\s*-\s*(.+)'
    single_matches = re.findall(single_pattern, content, re.IGNORECASE)
    
    for category, title, description in single_matches:
        enhancement = {
            "title": title.strip(),
            "description": description.strip(),
            "category": category.strip().lower(),
            "priority": "medium",  # Default priority
            "reasoning": "User-suggested enhancement from journal entry",
            "triggered_by": "gpt-journal-entry",
            "user_context": {
                "user_id": user_id,
                "source": "journal_content",
                "timestamp": datetime.now().isoformat()
            }
        }
        enhancements.append(enhancement)
    
    return enhancements

def parse_enhancement_block(block_content: str, user_id: str) -> Optional[dict]:
    """Parse a structured enhancement block"""
    try:
        enhancement = {
            "title": "",
            "description": "",
            "category": "general",
            "priority": "medium",
            "reasoning": "",
            "triggered_by": "gpt-journal-entry",
            "user_context": {
                "user_id": user_id,
                "source": "journal_content",
                "timestamp": datetime.now().isoformat()
            }
        }
        
        # Parse key-value pairs
        lines = block_content.strip().split('\n')
        current_key = None
        current_value = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Check for key: value format
            if ':' in line and line.split(':')[0].lower() in ['title', 'description', 'category', 'priority', 'reasoning']:
                # Save previous key-value
                if current_key and current_value:
                    enhancement[current_key] = ' '.join(current_value).strip()
                
                # Start new key-value
                key, value = line.split(':', 1)
                current_key = key.strip().lower()
                current_value = [value.strip()] if value.strip() else []
            else:
                # Continue previous value
                if current_key:
                    current_value.append(line)
        
        # Save last key-value
        if current_key and current_value:
            enhancement[current_key] = ' '.join(current_value).strip()
        
        # Validate required fields
        if not enhancement["title"] and not enhancement["description"]:
            # Try simple format: first line is title, rest is description
            lines = [l.strip() for l in block_content.strip().split('\n') if l.strip()]
            if lines:
                enhancement["title"] = lines[0]
                if len(lines) > 1:
                    enhancement["description"] = ' '.join(lines[1:])
                else:
                    enhancement["description"] = enhancement["title"]
                enhancement["reasoning"] = "Enhancement suggested in journal entry"
        
        # Ensure we have at least a title
        if not enhancement["title"]:
            return None
            
        # Validate category
        valid_categories = ['performance', 'usability', 'feature', 'bug', 'testing', 'general']
        if enhancement["category"] not in valid_categories:
            enhancement["category"] = "general"
            
        # Validate priority
        valid_priorities = ['low', 'medium', 'high', 'critical']
        if enhancement["priority"] not in valid_priorities:
            enhancement["priority"] = "medium"
        
        return enhancement
        
    except Exception as e:
        logger.warning(f"Failed to parse enhancement block: {e}")
        return None

# Helper function to log enhancement suggestions to proxy
async def log_enhancement_to_proxy(title: str, description: str, category: str, 
                                 priority: str, reasoning: str, triggered_by: str,
                                 user_context: Optional[dict] = None):
    """Log enhancement suggestion to observer proxy agent"""
    if not OBSERVER_ENABLED:
        logger.info(f"🔕 Observer disabled - skipping enhancement log")
        return
    
    logger.info(f"📤 Logging enhancement to observer: {title}")
    
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(
                f"{OBSERVER_URL}/enhancements",
                json={
                    "title": title,
                    "description": description,
                    "category": category,
                    "priority": priority,
                    "reasoning": reasoning,
                    "triggered_by": triggered_by,
                    "user_context": user_context
                }
            )
            logger.info(f"✅ Enhancement logged: {response.status_code}")
    except Exception as e:
        logger.warning(f"❌ Failed to log enhancement: {e}")

@app.get("/")
async def root():
    """Root endpoint - Deployment test 2025-07-21
    Provides comprehensive API documentation for GPT Actions integration"""
    return {
        "service": "Mirror Scribe Middleware API with Full Editorial Fluency",
        "version": "4.0.0",
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
            "growth_tracking",
            "relationship_intelligence",
            "editorial_fluency",
            "entry_refinement",
            "semantic_connections",
            "intention_setting",
            "energy_calibration"
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
            "/get-growth-insights/{user_id}",
            "/refine-entry/{entry_id}",
            "/connect-entries",
            "/set-intention/{entry_id}",
            "/relationship-summary/{user_id}",
            "/manual-energy/{entry_id}",
            "/entry-connections/{entry_id}",
            "/test-enhancement"
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
            "generate_summary": "POST /generate-period-summary",
            "refine_entry": "PUT /refine-entry/{entry_id} with content/tags/energy",
            "connect_entries": "POST /connect-entries with from/to/connection_type",
            "set_intention": "PUT /set-intention/{entry_id} with intention flag",
            "relationship_insights": "GET /relationship-summary/{user_id}",
            "manual_energy": "POST /manual-energy/{entry_id} with energy_signature",
            "entry_connections": "GET /entry-connections/{entry_id}"
        }
    }

@app.on_event("startup")
async def startup_event():
    """Log startup information"""
    logger.info("🚀 Journal Middleware starting up...")
    logger.info(f"📡 Backend URL: {BACKEND_URL}")
    logger.info(f"🔍 Observer URL: {OBSERVER_URL}")
    logger.info(f"🔔 Observer Enabled: {OBSERVER_ENABLED}")
    logger.info(f"🔑 API Key configured: {'Yes' if API_KEY != 'your-secret-api-key' else 'No (using default)'}")
    logger.info(f"📍 Service will listen on port: {os.environ.get('PORT', 8001)}")
    logger.info("✅ Ready to handle requests")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    logger.info(f"🔧 Starting Journal Middleware on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)