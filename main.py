import os
import httpx
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
import logging
import asyncio
import re
import hashlib
import json
from functools import wraps
import time
from collections import defaultdict, deque
import uuid

# Configure logging - Enhanced for Railway deployment monitoring
# Version 2025-07-21: Added for consistent storage testing
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== PERFORMANCE OPTIMIZATION LAYER =====

class ResponseCache:
    """In-memory response cache with TTL and size limits"""
    def __init__(self, max_size: int = 1000, default_ttl: int = 300):
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.max_size = max_size
        self.default_ttl = default_ttl
        
    def _generate_key(self, endpoint: str, params: Dict[str, Any]) -> str:
        """Generate cache key from endpoint and parameters"""
        params_str = json.dumps(params, sort_keys=True)
        return hashlib.md5(f"{endpoint}:{params_str}".encode()).hexdigest()
    
    def get(self, endpoint: str, params: Dict[str, Any]) -> Optional[Any]:
        """Get cached response if valid"""
        key = self._generate_key(endpoint, params)
        if key in self.cache:
            cached = self.cache[key]
            if cached['expires'] > time.time():
                logger.info(f"📊 Cache HIT: {endpoint}")
                return cached['data']
            else:
                # Expired
                del self.cache[key]
                logger.info(f"📊 Cache EXPIRED: {endpoint}")
        return None
    
    def set(self, endpoint: str, params: Dict[str, Any], data: Any, ttl: Optional[int] = None) -> None:
        """Cache response with TTL"""
        if len(self.cache) >= self.max_size:
            # Remove oldest entries
            oldest_keys = sorted(self.cache.keys(), key=lambda k: self.cache[k]['created'])[:10]
            for old_key in oldest_keys:
                del self.cache[old_key]
        
        key = self._generate_key(endpoint, params)
        ttl = ttl or self.default_ttl
        self.cache[key] = {
            'data': data,
            'created': time.time(),
            'expires': time.time() + ttl
        }
        logger.info(f"📊 Cache SET: {endpoint} (TTL: {ttl}s)")
    
    def invalidate_pattern(self, pattern: str) -> None:
        """Invalidate cache entries matching pattern"""
        keys_to_remove = [k for k in self.cache.keys() if pattern in k]
        for key in keys_to_remove:
            del self.cache[key]
        logger.info(f"📊 Cache INVALIDATED: {len(keys_to_remove)} entries matching '{pattern}'")

class CircuitBreaker:
    """Circuit breaker for external service calls"""
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "closed"  # closed, open, half-open
    
    def call(self, func):
        """Decorator for circuit breaker functionality"""
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if self.state == "open":
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    self.state = "half-open"
                    logger.info(f"🔄 Circuit breaker HALF-OPEN for {func.__name__}")
                else:
                    raise HTTPException(
                        status_code=503, 
                        detail=f"Circuit breaker OPEN for {func.__name__}"
                    )
            
            try:
                result = await func(*args, **kwargs)
                if self.state == "half-open":
                    self.state = "closed"
                    self.failure_count = 0
                    logger.info(f"✅ Circuit breaker CLOSED for {func.__name__}")
                return result
            except Exception as e:
                self.failure_count += 1
                self.last_failure_time = time.time()
                
                if self.failure_count >= self.failure_threshold:
                    self.state = "open" 
                    logger.error(f"🚨 Circuit breaker OPEN for {func.__name__} after {self.failure_count} failures")
                
                raise e
        return wrapper

class OptimizedHTTPClient:
    """HTTP client with connection pooling and retry logic"""
    def __init__(self):
        # Connection pool limits
        limits = httpx.Limits(
            max_keepalive_connections=20,
            max_connections=50,
            keepalive_expiry=30.0
        )
        
        # Timeout configuration
        timeout = httpx.Timeout(
            connect=5.0,
            read=10.0,
            write=5.0,
            pool=5.0
        )
        
        self.client = httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            headers={"User-Agent": "Journal-Middleware/2.0"}
        )
        
        self.retry_config = {
            "max_retries": 3,
            "backoff_factor": 1.0,
            "status_forcelist": [429, 500, 502, 503, 504]
        }
    
    async def request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make HTTP request with exponential backoff retry"""
        last_exception = None
        
        for attempt in range(self.retry_config["max_retries"] + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
                
                if response.status_code not in self.retry_config["status_forcelist"]:
                    return response
                    
                if attempt == self.retry_config["max_retries"]:
                    return response
                    
                # Exponential backoff
                wait_time = self.retry_config["backoff_factor"] * (2 ** attempt)
                logger.warning(f"🔄 Retrying {method} {url} in {wait_time}s (attempt {attempt + 1})")
                await asyncio.sleep(wait_time)
                
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_exception = e
                if attempt == self.retry_config["max_retries"]:
                    raise e
                
                wait_time = self.retry_config["backoff_factor"] * (2 ** attempt)
                logger.warning(f"🔄 Connection retry {method} {url} in {wait_time}s (attempt {attempt + 1})")
                await asyncio.sleep(wait_time)
        
        if last_exception:
            raise last_exception
    
    async def close(self):
        """Close the HTTP client"""
        await self.client.aclose()

class PerformanceMonitor:
    """Performance monitoring and metrics collection"""
    def __init__(self):
        self.request_times = defaultdict(list)
        self.error_counts = defaultdict(int)
        self.cache_stats = {"hits": 0, "misses": 0, "sets": 0}
    
    def record_request_time(self, endpoint: str, duration: float):
        """Record request duration"""
        self.request_times[endpoint].append(duration)
        # Keep only last 100 measurements per endpoint
        if len(self.request_times[endpoint]) > 100:
            self.request_times[endpoint] = self.request_times[endpoint][-100:]
    
    def record_error(self, endpoint: str):
        """Record error occurrence"""
        self.error_counts[endpoint] += 1
    
    def get_stats(self) -> Dict[str, Any]:
        """Get performance statistics"""
        stats = {}
        for endpoint, times in self.request_times.items():
            if times:
                stats[endpoint] = {
                    "avg_response_time": round(sum(times) / len(times), 3),
                    "min_response_time": round(min(times), 3),
                    "max_response_time": round(max(times), 3),
                    "request_count": len(times),
                    "error_count": self.error_counts.get(endpoint, 0)
                }
        return {
            "endpoint_stats": stats,
            "cache_stats": self.cache_stats
        }

class AsyncJobQueue:
    """Simple async job queue for background processing"""
    def __init__(self, max_workers: int = 3):
        self.queue = deque()
        self.running_jobs = {}
        self.completed_jobs = {}
        self.max_workers = max_workers
        self.workers_started = False
        
    async def enqueue(self, job_func, *args, **kwargs) -> str:
        """Enqueue a job for background processing"""
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "func": job_func,
            "args": args,
            "kwargs": kwargs,
            "created_at": time.time(),
            "status": "queued"
        }
        
        self.queue.append(job)
        logger.info(f"🔄 Job {job_id} queued: {job_func.__name__}")
        
        # Start workers if not already started
        if not self.workers_started:
            asyncio.create_task(self._start_workers())
            self.workers_started = True
            
        return job_id
    
    async def _start_workers(self):
        """Start background workers"""
        for i in range(self.max_workers):
            asyncio.create_task(self._worker(f"worker-{i}"))
    
    async def _worker(self, worker_name: str):
        """Background worker to process jobs"""
        logger.info(f"🔧 Starting worker: {worker_name}")
        
        while True:
            try:
                if self.queue:
                    job = self.queue.popleft()
                    job_id = job["id"]
                    
                    self.running_jobs[job_id] = {
                        **job,
                        "status": "running",
                        "started_at": time.time(),
                        "worker": worker_name
                    }
                    
                    logger.info(f"🔧 {worker_name} processing job {job_id}: {job['func'].__name__}")
                    
                    try:
                        # Execute the job
                        if asyncio.iscoroutinefunction(job["func"]):
                            result = await job["func"](*job["args"], **job["kwargs"])
                        else:
                            result = job["func"](*job["args"], **job["kwargs"])
                        
                        # Mark as completed
                        self.completed_jobs[job_id] = {
                            **self.running_jobs[job_id],
                            "status": "completed",
                            "completed_at": time.time(),
                            "result": result
                        }
                        
                        logger.info(f"✅ {worker_name} completed job {job_id}")
                        
                    except Exception as e:
                        # Mark as failed
                        self.completed_jobs[job_id] = {
                            **self.running_jobs[job_id],
                            "status": "failed",
                            "completed_at": time.time(),
                            "error": str(e)
                        }
                        
                        logger.error(f"❌ {worker_name} failed job {job_id}: {e}")
                    
                    finally:
                        # Remove from running jobs
                        if job_id in self.running_jobs:
                            del self.running_jobs[job_id]
                        
                        # Clean up old completed jobs (keep last 100)
                        if len(self.completed_jobs) > 100:
                            oldest_jobs = sorted(self.completed_jobs.items(), key=lambda x: x[1]["completed_at"])[:20]
                            for old_job_id, _ in oldest_jobs:
                                del self.completed_jobs[old_job_id]
                else:
                    # No jobs, wait a bit
                    await asyncio.sleep(1)
                    
            except Exception as e:
                logger.error(f"❌ Worker {worker_name} error: {e}")
                await asyncio.sleep(5)
    
    def get_job_status(self, job_id: str) -> Optional[dict]:
        """Get status of a job"""
        if job_id in self.running_jobs:
            return self.running_jobs[job_id]
        elif job_id in self.completed_jobs:
            return self.completed_jobs[job_id]
        else:
            # Check if still in queue
            for job in self.queue:
                if job["id"] == job_id:
                    return job
            return None
    
    def get_queue_stats(self) -> dict:
        """Get queue statistics"""
        return {
            "queued_jobs": len(self.queue),
            "running_jobs": len(self.running_jobs),
            "completed_jobs": len(self.completed_jobs),
            "total_workers": self.max_workers
        }

# Initialize optimization components
response_cache = ResponseCache(max_size=1000, default_ttl=300)  # 5 minutes default TTL
circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
http_client = OptimizedHTTPClient()
performance_monitor = PerformanceMonitor()
job_queue = AsyncJobQueue(max_workers=3)

# FastAPI app
app = FastAPI(
    title="Journal Middleware API with Performance Optimization",
    description="Optimized middleware with caching, connection pooling, and circuit breakers",
    version="2.0.0"
)

# Add compression middleware
app.add_middleware(GZipMiddleware, minimum_size=1000)

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

# Optimized observer logging with circuit breaker and caching
@circuit_breaker.call
async def log_to_observer(user_id: str, action_type: str, success: bool = True, 
                         response_time: Optional[float] = None, error_message: Optional[str] = None,
                         metadata: Optional[dict] = None):
    """Fire-and-forget logging to observer service with optimization"""
    if not OBSERVER_ENABLED:
        logger.info(f"🔕 Observer disabled - skipping log for {action_type}")
        return
    
    logger.info(f"📤 Attempting to log to observer: {action_type} for user {user_id}")
    
    try:
        response = await http_client.request_with_retry(
            "POST",
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
    except Exception as e:
        logger.warning(f"❌ Observer logging failed: {type(e).__name__}: {str(e)}")
        performance_monitor.record_error("log_to_observer")

# Performance monitoring decorator
def monitor_performance(endpoint_name: str, cache_ttl: Optional[int] = None):
    """Decorator to monitor endpoint performance and optionally cache responses"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start_time = time.time()
            
            # Check cache if enabled
            if cache_ttl:
                cache_params = {"args": args, "kwargs": kwargs}
                cached_response = response_cache.get(endpoint_name, cache_params)
                if cached_response:
                    performance_monitor.cache_stats["hits"] += 1
                    return cached_response
                performance_monitor.cache_stats["misses"] += 1
            
            try:
                result = await func(*args, **kwargs)
                duration = time.time() - start_time
                performance_monitor.record_request_time(endpoint_name, duration)
                
                # Cache successful responses
                if cache_ttl and hasattr(result, 'get') and result.get('success'):
                    cache_params = {"args": args, "kwargs": kwargs}
                    response_cache.set(endpoint_name, cache_params, result, cache_ttl)
                    performance_monitor.cache_stats["sets"] += 1
                
                logger.info(f"⚡ {endpoint_name} completed in {duration:.3f}s")
                return result
                
            except Exception as e:
                duration = time.time() - start_time
                performance_monitor.record_request_time(endpoint_name, duration)
                performance_monitor.record_error(endpoint_name)
                logger.error(f"❌ {endpoint_name} failed in {duration:.3f}s: {e}")
                raise e
        return wrapper
    return decorator

# Optimized backend request function
async def make_backend_request(method: str, endpoint: str, **kwargs) -> dict:
    """Make optimized request to backend with retry logic and monitoring"""
    url = f"{BACKEND_URL}{endpoint}"
    
    try:
        response = await http_client.request_with_retry(method, url, **kwargs)
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"Backend returned error: {response.status_code}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Backend error: {response.status_code}"
            )
    except httpx.RequestError as e:
        logger.error(f"Request to backend failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Backend service unavailable. Please try again later."
        )

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
    """Simple health check for Railway deployment - no external dependencies"""
    return {
        "status": "healthy",
        "service": "middleware",
        "version": "2.0.0",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/health/detailed")
async def detailed_health_check():
    """Detailed health check with backend connectivity - for monitoring"""
    try:
        # Test backend connection with optimized client
        response = await http_client.request_with_retry("GET", f"{BACKEND_URL}/health")
        backend_health = response.json()
        
        # Test observer connection if enabled
        observer_status = "disabled"
        if OBSERVER_ENABLED:
            try:
                obs_response = await http_client.request_with_retry("GET", f"{OBSERVER_URL}/")
                observer_status = "connected" if obs_response.status_code == 200 else "unreachable"
            except:
                observer_status = "unreachable"
        
        # Get performance stats
        perf_stats = performance_monitor.get_stats()
        
        return {
            "status": "healthy",
            "service": "middleware",
            "version": "2.0.0",
            "backend": backend_health.get("status", "unknown"),
            "backend_url": BACKEND_URL,
            "observer": {
                "status": observer_status,
                "url": OBSERVER_URL,
                "enabled": OBSERVER_ENABLED
            },
            "features": backend_health.get("features", []),
            "performance": perf_stats,
            "optimizations": {
                "caching_enabled": True,
                "connection_pooling": True,
                "circuit_breaker": circuit_breaker.state,
                "compression": True
            },
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Detailed health check failed: {e}")
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
            "timestamp": datetime.utcnow().isoformat()
        }

@app.get("/performance-stats")
async def get_performance_stats():
    """Get detailed performance statistics"""
    return {
        "performance_stats": performance_monitor.get_stats(),
        "cache_info": {
            "cache_size": len(response_cache.cache),
            "max_cache_size": response_cache.max_size,
            "default_ttl": response_cache.default_ttl
        },
        "circuit_breaker_state": circuit_breaker.state,
        "http_client_info": {
            "max_connections": 50,
            "max_keepalive": 20,
            "keepalive_expiry": 30.0
        },
        "timestamp": datetime.utcnow().isoformat()
    }

@app.post("/clear-cache")
async def clear_cache(pattern: Optional[str] = None):
    """Clear cache entries (optionally by pattern)"""
    if pattern:
        response_cache.invalidate_pattern(pattern)
        return {"message": f"Cache cleared for pattern: {pattern}"}
    else:
        response_cache.cache.clear()
        return {"message": "All cache cleared"}

@app.get("/job-queue-stats")
async def get_job_queue_stats():
    """Get job queue statistics"""
    return {
        "queue_stats": job_queue.get_queue_stats(),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/job/{job_id}")
async def get_job_status(job_id: str):
    """Get status of a specific background job"""
    job_status = job_queue.get_job_status(job_id)
    if job_status:
        return job_status
    else:
        raise HTTPException(status_code=404, detail="Job not found")

@app.post("/save-entry", response_model=EntryResponse, dependencies=[Depends(verify_api_key)])
@monitor_performance("save_entry")
async def save_entry(entry: EntryCreate):
    """Save a journal entry with tag support via the backend API"""
    start_time = datetime.now()
    try:
        # Invalidate related caches when new entry is saved
        response_cache.invalidate_pattern(f"get_entries_{entry.user_id}")
        response_cache.invalidate_pattern(f"get_daily_summary_{entry.user_id}")
        response_cache.invalidate_pattern("get_tags")
        
        # Use httpx directly to call backend
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
                        "manual_tags": len(entry.manual_tags) if entry.manual_tags else 0
                    }
                ))
                
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
@monitor_performance("get_entries", cache_ttl=120)  # Cache for 2 minutes
async def get_entries(user_id: str, limit: Optional[int] = 50, offset: Optional[int] = 0, tags: Optional[str] = None):
    """Retrieve journal entries for a user with optional tag filtering"""
    start_time = datetime.now()
    action_type = "search" if tags else "recall"
    
    try:
        params = {"limit": limit, "offset": offset}
        if tags:
            params["tags"] = tags
            
        # Use optimized backend request
        endpoint = f"/api/messages/{user_id}"
        response = await http_client.request_with_retry(
            "GET",
            f"{BACKEND_URL}{endpoint}",
            params=params
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
@monitor_performance("get_tags", cache_ttl=600)  # Cache for 10 minutes
async def get_all_tags():
    """Get all available tags"""
    try:
        data = await make_backend_request("GET", "/api/tags")
        logger.info(f"Retrieved {data.get('count', 0)} tags")
        return data
                
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
            "/test-enhancement",
            "/temporal-state/{user_id}",
            "/temporal-summary/{user_id}",
            "/temporal-signals/{user_id}",
            "/missing-temporal-signals/{user_id}",
            "/timestamp-validation/{user_id}",
            "/correct-timestamp/{entry_id}",
            "/temporal-context/{entry_id}",
            "/timezone-settings/{user_id}"
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
    """Log startup information and initialize optimization components"""
    logger.info("🚀 Journal Middleware starting up with performance optimizations...")
    logger.info(f"📡 Backend URL: {BACKEND_URL}")
    logger.info(f"🔍 Observer URL: {OBSERVER_URL}")
    logger.info(f"🔔 Observer Enabled: {OBSERVER_ENABLED}")
    logger.info(f"🔑 API Key configured: {'Yes' if API_KEY != 'your-secret-api-key' else 'No (using default)'}")
    logger.info(f"📍 Service will listen on port: {os.environ.get('PORT', 8001)}")
    
    # Initialize optimization components
    logger.info("🎯 Initializing performance optimizations:")
    logger.info(f"  📊 Response cache: {response_cache.max_size} entries, {response_cache.default_ttl}s TTL")
    logger.info(f"  🔄 Circuit breaker: {circuit_breaker.failure_threshold} failure threshold")
    logger.info(f"  🔗 HTTP client: {50} max connections, {20} keepalive")
    logger.info(f"  🔧 Job queue: {job_queue.max_workers} workers")
    logger.info(f"  🗜️ Compression: GZip enabled")
    
    logger.info("✅ Ready to handle requests with enhanced performance")

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup resources on shutdown"""
    logger.info("🛑 Journal Middleware shutting down...")
    
    try:
        # Close HTTP client
        await http_client.close()
        logger.info("✅ HTTP client closed")
    except Exception as e:
        logger.error(f"❌ Error closing HTTP client: {e}")
    
    logger.info("✅ Shutdown complete")

# ========================================
# TEMPORAL AWARENESS ENDPOINTS (GPT-Friendly)
# ========================================

@app.get("/temporal-state/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_user_temporal_state(user_id: str):
    """Get user's current temporal awareness state with sacred interpretation"""
    start_time = datetime.now()
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/temporal/state/{user_id}",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                boundaries = data.get("boundaries", {})
                
                # Transform into sacred language
                sacred_state = {}
                interpretations = []
                
                # Interpret day boundaries
                if boundaries.get("last_day_start") and boundaries.get("last_day_end"):
                    sacred_state["daily_rhythm"] = "flowing"
                    interpretations.append("Your daily rhythm flows with conscious beginnings and intentional closures")
                elif boundaries.get("last_day_start"):
                    sacred_state["daily_rhythm"] = "awakening"
                    interpretations.append("You honor the dawn of new days with mindful attention")
                elif boundaries.get("last_day_end"):
                    sacred_state["daily_rhythm"] = "closing"
                    interpretations.append("Evening reflections mark your sacred transitions")
                else:
                    sacred_state["daily_rhythm"] = "unanchored"
                    interpretations.append("Consider marking the sacred boundaries of your days")
                
                # Interpret weekly patterns
                if boundaries.get("last_week_start") or boundaries.get("last_week_end"):
                    sacred_state["weekly_awareness"] = "present"
                    interpretations.append("Your consciousness spans the greater rhythms of weekly cycles")
                else:
                    sacred_state["weekly_awareness"] = "emerging"
                    interpretations.append("Weekly reflection awaits your exploration")
                
                # Interpret monthly cycles
                if boundaries.get("last_month_start") or boundaries.get("last_month_end"):
                    sacred_state["lunar_connection"] = "attuned"
                    interpretations.append("You honor the larger lunar and seasonal cycles")
                else:
                    sacred_state["lunar_connection"] = "dormant"
                    interpretations.append("Monthly reflection could deepen your temporal wisdom")
                
                # Log to observer
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="analyze",
                    success=True,
                    response_time=response_time,
                    metadata={"endpoint": "temporal_state", "daily_rhythm": sacred_state["daily_rhythm"]}
                ))
                
                return {
                    "success": True,
                    "user_id": user_id,
                    "timezone": data.get("timezone", "America/Chicago"),
                    "sacred_state": sacred_state,
                    "interpretations": interpretations,
                    "raw_boundaries": boundaries,
                    "wisdom": "Your relationship with time reflects your growing consciousness"
                }
            else:
                logger.error(f"Backend temporal state failed: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to retrieve temporal state"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Temporal state request failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Temporal awareness service unavailable"
        )

@app.get("/temporal-summary/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_temporal_summary(user_id: str, period: str = "weekly"):
    """Get temporally-aware period summary with sacred insights"""
    start_time = datetime.now()
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/temporal/summary/{user_id}?period_type={period}",
                timeout=15.0
            )
            
            if response.status_code == 200:
                data = response.json()
                summary = data.get("summary", {})
                
                # Transform temporal analysis into sacred language
                temporal_analysis = summary.get("temporal_analysis", {})
                theme_analysis = summary.get("theme_analysis", {})
                emotional_journey = summary.get("emotional_journey", {})
                
                sacred_summary = {
                    "period_essence": summary.get("sacred_summary", ""),
                    "temporal_awareness_score": temporal_analysis.get("temporal_awareness_score", 0),
                    "consciousness_threads": [tag[0] for tag in theme_analysis.get("dominant_tags", [])[:3]],
                    "emotional_river": [emotion[0] for emotion in emotional_journey.get("dominant_emotions", [])[:2]],
                    "signal_count": temporal_analysis.get("signal_counts", {}),
                    "growth_insights": summary.get("growth_insights", []),
                    "wisdom_insights": summary.get("wisdom_insights", [])
                }
                
                # Generate temporal wisdom
                temporal_wisdom = []
                if temporal_analysis.get("temporal_awareness_score", 0) > 0.7:
                    temporal_wisdom.append("Your temporal awareness shines like a beacon, marking sacred transitions with clarity")
                elif temporal_analysis.get("temporal_awareness_score", 0) > 0.4:
                    temporal_wisdom.append("Your growing awareness of time's sacred rhythms guides your journey")
                else:
                    temporal_wisdom.append("The sacred art of temporal marking awaits your exploration")
                
                # Log to observer
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="analyze",
                    success=True,
                    response_time=response_time,
                    metadata={
                        "endpoint": "temporal_summary", 
                        "period": period,
                        "temporal_score": temporal_analysis.get("temporal_awareness_score", 0)
                    }
                ))
                
                return {
                    "success": True,
                    "period_type": period,
                    "sacred_summary": sacred_summary,
                    "temporal_wisdom": temporal_wisdom,
                    "entry_count": summary.get("entry_count", 0),
                    "signal_count": summary.get("temporal_signals", 0),
                    "full_analysis": summary
                }
            else:
                logger.error(f"Backend temporal summary failed: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to generate {period} temporal summary"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Temporal summary request failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Temporal summary service unavailable"
        )

@app.get("/missing-temporal-signals/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_missing_temporal_signals(user_id: str, days_back: int = 7):
    """Get suggestions for missing temporal boundaries with gentle guidance"""
    start_time = datetime.now()
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/temporal/missing-signals/{user_id}?days_back={days_back}",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                missing_signals = data.get("missing_signals", [])
                suggestions = data.get("suggestions", [])
                temporal_score = data.get("temporal_awareness_score", 0)
                
                # Transform into gentle, sacred guidance
                sacred_guidance = []
                gentle_suggestions = []
                
                for suggestion in suggestions:
                    signal_type = suggestion.get("signal_type")
                    guidance = suggestion.get("suggestion")
                    
                    # Transform into sacred language
                    if "day_start" in signal_type:
                        sacred_guidance.append("🌅 Morning reflections create sacred containers for your daily journey")
                        gentle_suggestions.append("Consider greeting each dawn with a moment of conscious intention")
                    elif "day_end" in signal_type:
                        sacred_guidance.append("🌙 Evening gratitude transforms ordinary days into sacred passages")
                        gentle_suggestions.append("Let twilight invite you to reflect on the day's gifts and lessons")
                    elif "week_start" in signal_type:
                        sacred_guidance.append("📅 Weekly planning aligns your soul's purpose with time's rhythm")
                        gentle_suggestions.append("Monday mornings offer perfect moments for setting weekly intentions")
                    elif "month_start" in signal_type:
                        sacred_guidance.append("🌙 Monthly reflection honors the larger cycles of growth and renewal")
                        gentle_suggestions.append("New months invite you to pause and witness your evolving journey")
                
                # Generate overall wisdom
                if temporal_score > 0.7:
                    overall_wisdom = "Your temporal awareness flows like a sacred river - deep, consistent, and life-giving"
                elif temporal_score > 0.4:
                    overall_wisdom = "Your growing awareness of time's sacred rhythms is a beautiful unfolding practice"
                else:
                    overall_wisdom = "The sacred art of temporal awareness awaits your gentle exploration"
                
                # Log to observer
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="analyze",
                    success=True,
                    response_time=response_time,
                    metadata={
                        "endpoint": "missing_temporal_signals",
                        "missing_count": len(missing_signals),
                        "temporal_score": temporal_score
                    }
                ))
                
                return {
                    "success": True,
                    "user_id": user_id,
                    "temporal_awareness_score": temporal_score,
                    "missing_signal_count": len(missing_signals),
                    "sacred_guidance": sacred_guidance,
                    "gentle_suggestions": gentle_suggestions,
                    "overall_wisdom": overall_wisdom,
                    "invitation": "These are gentle invitations, not requirements. Your temporal practice unfolds at its own perfect pace."
                }
            else:
                logger.error(f"Backend missing signals failed: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to analyze missing temporal signals"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Missing signals request failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Temporal analysis service unavailable"
        )

# ========================================
# TIMESTAMP MANAGEMENT ENDPOINTS (GPT-Friendly)
# ========================================

@app.get("/timestamp-validation/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_timestamp_validation_report(user_id: str, days_back: int = 7):
    """Review timestamp validation issues with gentle, natural language guidance"""
    start_time = datetime.now()
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/timestamp/validation-report/{user_id}?days_back={days_back}",
                timeout=15.0
            )
            
            if response.status_code == 200:
                data = response.json()
                validation_issues = data.get("validation_issues", [])
                
                # Transform technical validation data into natural language
                gentle_guidance = []
                sacred_insights = []
                
                for issue in validation_issues[:5]:  # Top 5 issues
                    entry_id = issue.get("entry_id")
                    content_preview = issue.get("content_preview", "")
                    validation = issue.get("current_validation", {})
                    severity = validation.get("severity", "ok")
                    message = validation.get("message", "")
                    
                    if severity == "warning":
                        gentle_guidance.append({
                            "entry_id": entry_id,
                            "content_preview": content_preview,
                            "gentle_message": f"The timing of this entry seems unusual: {message.lower()}",
                            "suggestion": "Consider if the timestamp accurately reflects when you wrote this entry",
                            "urgency": "low"
                        })
                    elif severity == "suspicious":
                        gentle_guidance.append({
                            "entry_id": entry_id,
                            "content_preview": content_preview,
                            "gentle_message": f"This entry's timing raised some questions: {message.lower()}",
                            "suggestion": "You might want to verify the date and time for this entry",
                            "urgency": "medium"
                        })
                
                # Generate sacred insights
                if len(validation_issues) == 0:
                    sacred_insights.append("🕐 Your temporal awareness flows harmoniously - all entries align beautifully with their intended time")
                elif len(validation_issues) <= 2:
                    sacred_insights.append("✨ Your relationship with time shows gentle consistency, with only minor adjustments needed")
                else:
                    sacred_insights.append("🌙 Some entries call for temporal realignment - an opportunity for deeper mindfulness")
                
                # Log to observer
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="analyze",
                    success=True,
                    response_time=response_time,
                    metadata={"endpoint": "timestamp_validation", "issues_found": len(validation_issues)}
                ))
                
                return {
                    "success": True,
                    "user_id": user_id,
                    "period_analyzed_days": days_back,
                    "issues_found": len(validation_issues),
                    "gentle_guidance": gentle_guidance,
                    "sacred_insights": sacred_insights,
                    "overall_message": "Time flows through your journal like a gentle river - these insights help ensure perfect alignment with your authentic moments"
                }
            else:
                logger.error(f"Backend timestamp validation failed: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to retrieve timestamp validation report"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Timestamp validation request failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Timestamp validation service unavailable"
        )

@app.post("/correct-timestamp/{entry_id}", dependencies=[Depends(verify_api_key)])
async def correct_entry_timestamp(entry_id: int, new_timestamp: str, 
                                 timezone_name: Optional[str] = None, 
                                 reason: Optional[str] = ""):
    """Simple, conversational timestamp correction interface"""
    start_time = datetime.now()
    
    try:
        # Prepare the correction request
        correction_data = {
            "entry_id": entry_id,
            "new_timestamp": new_timestamp,
            "reason": reason or "Timestamp corrected for accuracy"
        }
        
        if timezone_name:
            correction_data["timezone_name"] = timezone_name
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BACKEND_URL}/api/timestamp/override",
                json=correction_data,
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                
                # Log to observer
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id="timestamp_correction",  # Would normally extract from auth
                    action_type="store",
                    success=True,
                    response_time=response_time,
                    metadata={"endpoint": "correct_timestamp", "entry_id": entry_id}
                ))
                
                return {
                    "success": True,
                    "message": "✨ Timestamp gracefully aligned with your authentic moment",
                    "entry_id": entry_id,
                    "corrected_timestamp": new_timestamp,
                    "timezone": data.get("timezone"),
                    "sacred_note": "Time now flows in harmony with your true experience",
                    "correction_reason": reason
                }
            else:
                logger.error(f"Backend timestamp correction failed: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to correct timestamp"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Timestamp correction request failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Timestamp correction service unavailable"
        )

@app.get("/temporal-context/{entry_id}", dependencies=[Depends(verify_api_key)])
async def get_temporal_context(entry_id: int):
    """Rich temporal context for entries with natural language insights"""
    start_time = datetime.now()
    
    try:
        # First get the entry details
        async with httpx.AsyncClient() as client:
            # This would need to be implemented in backend - getting single entry with full context
            response = await client.get(
                f"{BACKEND_URL}/api/entries/{entry_id}/context",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                
                # Transform into natural language context
                temporal_story = []
                
                utc_time = data.get("utc_timestamp")
                local_time = data.get("local_timestamp")
                timezone = data.get("timezone")
                validation_score = data.get("temporal_validation_score", 0.5)
                
                if utc_time and local_time:
                    # Parse timestamps
                    try:
                        local_dt = datetime.fromisoformat(local_time)
                        hour = local_dt.hour
                        weekday = local_dt.weekday()
                        
                        # Create narrative
                        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
                        day_name = day_names[weekday]
                        
                        if 5 <= hour < 12:
                            time_essence = "morning light"
                        elif 12 <= hour < 17:
                            time_essence = "afternoon flow"
                        elif 17 <= hour < 21:
                            time_essence = "evening reflection"
                        else:
                            time_essence = "night's embrace"
                        
                        temporal_story.append(f"Written in {time_essence} on {day_name}")
                        temporal_story.append(f"Your local time: {local_dt.strftime('%I:%M %p')}")
                        temporal_story.append(f"Timezone: {timezone}")
                        
                        # Validation context
                        if validation_score > 0.8:
                            temporal_story.append("🌟 This timestamp flows in perfect harmony with the content")
                        elif validation_score > 0.6:
                            temporal_story.append("✨ The timing feels authentic and aligned")
                        else:
                            temporal_story.append("🌙 The timing might benefit from gentle review")
                    
                    except Exception as e:
                        temporal_story.append("Time flows through this entry in its own mysterious way")
                
                # Log to observer
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id="temporal_context",
                    action_type="search",
                    success=True,
                    response_time=response_time,
                    metadata={"endpoint": "temporal_context", "entry_id": entry_id}
                ))
                
                return {
                    "success": True,
                    "entry_id": entry_id,
                    "temporal_story": temporal_story,
                    "raw_data": {
                        "utc_timestamp": utc_time,
                        "local_timestamp": local_time,
                        "timezone": timezone,
                        "validation_score": validation_score
                    },
                    "sacred_insight": "Every moment carries its own perfect signature in the flow of time"
                }
            else:
                # Fallback response if endpoint doesn't exist yet
                return {
                    "success": True,
                    "entry_id": entry_id,
                    "temporal_story": ["This entry exists in the eternal now"],
                    "sacred_insight": "Time is but a river, and your words are precious stones cast into its flow"
                }
                
    except httpx.RequestError as e:
        logger.error(f"Temporal context request failed: {e}")
        return {
            "success": True,
            "entry_id": entry_id,
            "temporal_story": ["Time flows mysteriously through this entry"],
            "sacred_insight": "In the absence of perfect timing data, we honor the essence of the eternal moment"
        }

@app.get("/timezone-settings/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_timezone_settings(user_id: str):
    """Get user's timezone settings with friendly suggestions"""
    start_time = datetime.now()
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BACKEND_URL}/api/temporal/state/{user_id}",
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                current_timezone = data.get("timezone", "America/Chicago")
                
                # Get timezone suggestions
                suggestions_response = await client.get(
                    f"{BACKEND_URL}/api/timezone/suggestions",
                    timeout=5.0
                )
                
                suggestions = []
                if suggestions_response.status_code == 200:
                    suggestions_data = suggestions_response.json()
                    suggestions = suggestions_data.get("suggestions", [])
                
                # Transform into friendly format
                timezone_wisdom = []
                if "America/Chicago" in current_timezone:
                    timezone_wisdom.append("🌾 You flow with Central Time - the heartland rhythm")
                elif "America/New_York" in current_timezone:
                    timezone_wisdom.append("🗽 You dance with Eastern Time - where the day begins")
                elif "America/Los_Angeles" in current_timezone:
                    timezone_wisdom.append("🌊 You breathe with Pacific Time - where dreams meet the ocean")
                else:
                    timezone_wisdom.append(f"🌍 Your time flows through {current_timezone} - a unique rhythm in the global symphony")
                
                # Log to observer
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="search",
                    success=True,
                    response_time=response_time,
                    metadata={"endpoint": "timezone_settings", "current_timezone": current_timezone}
                ))
                
                return {
                    "success": True,
                    "user_id": user_id,
                    "current_timezone": current_timezone,
                    "timezone_wisdom": timezone_wisdom,
                    "common_suggestions": suggestions[:8],
                    "guidance": "Your timezone shapes how your temporal awareness flows through each day",
                    "change_note": "Timezone changes affect how your journal entries align with natural rhythms"
                }
            else:
                logger.error(f"Backend timezone settings failed: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to retrieve timezone settings"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Timezone settings request failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Timezone settings service unavailable"
        )

@app.put("/timezone-settings/{user_id}", dependencies=[Depends(verify_api_key)])
async def update_timezone_settings(user_id: str, new_timezone: str, 
                                  bulk_correct_existing: bool = False):
    """Update user's timezone with gentle confirmation"""
    start_time = datetime.now()
    
    try:
        async with httpx.AsyncClient() as client:
            # Update timezone
            response = await client.put(
                f"{BACKEND_URL}/api/user/{user_id}/timezone",
                json={"timezone_name": new_timezone},
                timeout=10.0
            )
            
            if response.status_code == 200:
                update_result = response.json()
                
                # Optionally bulk correct existing entries
                bulk_result = None
                if bulk_correct_existing:
                    bulk_response = await client.post(
                        f"{BACKEND_URL}/api/timestamp/bulk-correct",
                        json={
                            "user_id": user_id,
                            "new_timezone": new_timezone,
                            "reason": "Timezone change - bulk correction"
                        },
                        timeout=30.0
                    )
                    
                    if bulk_response.status_code == 200:
                        bulk_result = bulk_response.json()
                
                # Generate friendly confirmation
                timezone_blessing = []
                if "America" in new_timezone:
                    timezone_blessing.append(f"🌎 Your temporal awareness now flows with {new_timezone}")
                elif "Europe" in new_timezone:
                    timezone_blessing.append(f"🏰 Your time consciousness aligns with {new_timezone}")
                elif "Asia" in new_timezone:
                    timezone_blessing.append(f"🏯 Your temporal rhythm dances with {new_timezone}")
                else:
                    timezone_blessing.append(f"🌍 Your awareness embraces the rhythm of {new_timezone}")
                
                if bulk_result:
                    corrections_made = bulk_result.get("corrections_made", 0)
                    timezone_blessing.append(f"✨ {corrections_made} past entries gracefully realigned with your new temporal flow")
                
                # Log to observer
                response_time = (datetime.now() - start_time).total_seconds() * 1000
                asyncio.create_task(log_to_observer(
                    user_id=user_id,
                    action_type="store",
                    success=True,
                    response_time=response_time,
                    metadata={
                        "endpoint": "update_timezone", 
                        "new_timezone": new_timezone,
                        "bulk_correct": bulk_correct_existing
                    }
                ))
                
                return {
                    "success": True,
                    "message": "Timezone gracefully updated",
                    "user_id": user_id,
                    "new_timezone": new_timezone,
                    "timezone_blessing": timezone_blessing,
                    "bulk_correction": bulk_result,
                    "sacred_note": "Your relationship with time evolves as you journey through life's rhythms"
                }
            else:
                logger.error(f"Backend timezone update failed: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to update timezone settings"
                )
                
    except httpx.RequestError as e:
        logger.error(f"Timezone update request failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Timezone update service unavailable"
        )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    logger.info(f"🔧 Starting Journal Middleware with Temporal Awareness and Timestamp Synchronization on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)