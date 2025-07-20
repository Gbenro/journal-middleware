# Journal Middleware Service with Intelligent Tags

FastAPI middleware service for the GPT Agent Journaling System with comprehensive tagging capabilities. This service provides API authentication, request routing, and acts as a secure gateway between GPT agents and the intelligent tagging backend.

## 🚀 Railway Deployment

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/new)

### Quick Deploy
1. Click the Railway button above or go to [Railway](https://railway.app)
2. Connect this GitHub repository
3. Set required environment variables
4. Deploy!

### Environment Variables
- `BACKEND_URL`: URL of the backend service (e.g., `https://your-backend.railway.app`)
- `API_KEY`: Secret API key for GPT agent authentication
- `PORT`: Service port (auto-configured by Railway)

## 🏗️ Architecture

This middleware service:
- Authenticates GPT agents via API keys
- Routes requests to the backend service
- Provides security layer and request validation
- Handles error responses and logging

## 📚 Enhanced API Endpoints

### Health Check
```
GET /health
```
Returns middleware health, backend connectivity, and available features.

### Save Entry with Tags (Authenticated)
```
POST /save-entry
X-API-Key: your-secret-api-key
Content-Type: application/json

{
  "content": "Had a breakthrough with my coding project today!",
  "user_id": "user123",
  "manual_tags": ["coding", "work"],
  "auto_tag": true
}
```

### Get Entries with Tag Filtering (Authenticated)
```
GET /get-entries/{user_id}?limit=50&offset=0&tags=work,coding
X-API-Key: your-secret-api-key
```
Returns entries for a user, optionally filtered by tags.

### Get Entries by Specific Tags
```
GET /get-entries-by-tags/{user_id}/{tag_names}
X-API-Key: your-secret-api-key
```

### Tag Management
```
GET /get-tags - Get all available tags
GET /get-tags-by-category - Get tags grouped by category
POST /create-tag - Create new custom tag
POST /suggest-tags - Get AI tag suggestions for content
```

## 🔐 Authentication

All protected endpoints require the `X-API-Key` header with a valid API key. Configure the API key via the `API_KEY` environment variable.

## 🔧 Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export BACKEND_URL="http://localhost:8000"
export API_KEY="your-secret-api-key"

# Run the service
uvicorn main:app --reload --port 8001
```

## 🤖 Enhanced GPT Agent Usage

GPT agents can use intelligent tagging features:

```python
import requests

headers = {"X-API-Key": "your-secret-api-key"}

# Save entry with manual tags and auto-tagging
response = requests.post(
    "https://your-middleware.railway.app/save-entry",
    headers=headers,
    json={
        "content": "Fixed the API bug in production deployment",
        "user_id": "gpt_agent_1",
        "manual_tags": ["work", "coding"],
        "auto_tag": True
    }
)

# Get entries filtered by tags
response = requests.get(
    "https://your-middleware.railway.app/get-entries/gpt_agent_1?tags=work,coding",
    headers=headers
)

# Get tag suggestions
response = requests.post(
    "https://your-middleware.railway.app/suggest-tags",
    headers=headers,
    json={"content": "Had coffee with my sister and talked about family"}
)

# Get all available tags
response = requests.get(
    "https://your-middleware.railway.app/get-tags",
    headers=headers
)
```

## 🏷️ Tag Categories

The system includes predefined tags in multiple categories:

- **Emotions**: gratitude, joy, stress, reflection, accomplishment
- **Life Areas**: work, family, friends, health, learning
- **Activities**: coding, reading, exercise, travel, breakthrough
- **Goals**: goal-setting, progress, challenge, milestone

## 🔗 Related Services

- **Backend Service**: [journal-backend-repo](../journal-backend-repo) - Database and core operations
- **Main Project**: [Scribe_agent](../) - Complete journaling system documentation

## 📦 Dependencies

- FastAPI - Web framework
- httpx - Async HTTP client for backend communication
- Uvicorn - ASGI server

## 🏷️ Version

1.0.0