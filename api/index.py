"""Vercel's native BaseHTTPRequestHandler entry point."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'engine'))
from lisa.serverless import VercelHandler

class handler(VercelHandler):
    """Explicit class entrypoint for Vercel's Python source detector."""
