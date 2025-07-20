import os
import httpx
from datetime import datetime
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI app
app = FastAPI(title="Journal Middleware API")

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
API_KEY = os.environ.get("API_KEY", "your-secret-api-key")

# Pydantic models
class EntryCreate(BaseModel):
    content: str
    user_id: str

class EntryResponse(BaseModel):
    success: bool
    message: str
    id: Optional[int] = None

class MessageResponse(BaseModel):
    id: int
    content: str
    user_id: str
    timestamp: str  # SQLite returns ISO string, not datetime object

# Dependency for API key authentication
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
    """Save a journal entry via the backend API"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BACKEND_URL}/api/save",
                json={
                    "content": entry.content,
                    "user_id": entry.user_id
                },
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Entry saved successfully for user {entry.user_id}")
                return EntryResponse(
                    success=True,
                    message="Journal entry saved successfully",
                    id=data.get("message_id")
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
async def get_entries(user_id: str, limit: Optional[int] = 50, offset: Optional[int] = 0):
    """Retrieve journal entries for a user via the backend API"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/messages/{user_id}",
                params={"limit": limit, "offset": offset},
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
                    "count": len(messages)
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

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Journal Middleware API",
        "version": "1.0.0",
        "endpoints": [
            "/health",
            "/save-entry",
            "/get-entries/{user_id}"
        ],
        "authentication": "API key required in X-API-Key header"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)