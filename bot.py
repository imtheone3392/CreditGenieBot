"""Compatibility entry point for the member and wallet Mini App service."""
import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run("app:api", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
