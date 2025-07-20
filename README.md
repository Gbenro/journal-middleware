# Journal Middleware Service

FastAPI middleware service for the GPT Agent Journaling System. This service provides API authentication, request routing, and acts as a secure gateway between GPT agents and the backend database service.

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

## 📚 API Endpoints

### Health Check
```
GET /health
```
Returns middleware health and backend connectivity status.

### Save Entry (Authenticated)
```
POST /save-entry
X-API-Key: your-secret-api-key
Content-Type: application/json

{
  "content": "Journal entry content",
  "user_id": "user123"
}
```

### Get Entries (Authenticated)
```
GET /get-entries/{user_id}?limit=50&offset=0
X-API-Key: your-secret-api-key
```
Returns paginated journal entries for a user.

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

## 🤖 GPT Agent Usage

GPT agents can use this service to store and retrieve journal entries:

```python
import requests

headers = {"X-API-Key": "your-secret-api-key"}

# Save an entry
response = requests.post(
    "https://your-middleware.railway.app/save-entry",
    headers=headers,
    json={"content": "Today I learned...", "user_id": "gpt_agent_1"}
)

# Get entries
response = requests.get(
    "https://your-middleware.railway.app/get-entries/gpt_agent_1",
    headers=headers
)
```

## 🔗 Related Services

- **Backend Service**: [journal-backend-repo](../journal-backend-repo) - Database and core operations
- **Main Project**: [Scribe_agent](../) - Complete journaling system documentation

## 📦 Dependencies

- FastAPI - Web framework
- httpx - Async HTTP client for backend communication
- Uvicorn - ASGI server

## 🏷️ Version

1.0.0