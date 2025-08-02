#!/usr/bin/env python3
"""Minimal test app to debug Railway deployment issues"""

from fastapi import FastAPI
import uvicorn
import os

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Minimal middleware is running"}

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "minimal-middleware"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001))
    print(f"Starting minimal middleware on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)