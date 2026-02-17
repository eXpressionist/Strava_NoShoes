#!/usr/bin/env python3
"""
Startup script for Strava NoShoes application.
This script provides an easy way to run the application with different configurations.
"""

import argparse
import sys
import os
from pathlib import Path

# Add the app directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

def main():
    parser = argparse.ArgumentParser(description="Run Strava NoShoes application")
    parser.add_argument(
        "--host", 
        default="0.0.0.0", 
        help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", 
        type=int, 
        default=8000, 
        help="Port to bind to (default: 8000)"
    )
    parser.add_argument(
        "--reload", 
        action="store_true", 
        help="Enable auto-reload for development"
    )
    parser.add_argument(
        "--workers", 
        type=int, 
        default=1, 
        help="Number of worker processes (default: 1)"
    )
    parser.add_argument(
        "--log-level", 
        default="info", 
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Log level (default: info)"
    )
    
    args = parser.parse_args()
    
    # Check if .env file exists
    if not Path(".env").exists():
        print("⚠️  Warning: .env file not found!")
        print("📝 Please copy .env.example to .env and configure your Strava API credentials.")
        print("🔗 Get your credentials at: https://www.strava.com/settings/api")
        print()
        
        response = input("Continue anyway? (y/N): ").lower().strip()
        if response not in ['y', 'yes']:
            print("Exiting...")
            sys.exit(1)
    
    # Create necessary directories
    Path("data/gpx").mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    
    print("🏃‍♂️ Starting Strava NoShoes...")
    print(f"🌐 Server will be available at: http://{args.host}:{args.port}")
    print(f"📚 API Documentation: http://{args.host}:{args.port}/docs")
    print(f"📖 ReDoc Documentation: http://{args.host}:{args.port}/redoc")
    print()
    
    try:
        import uvicorn
        uvicorn.run(
            "app.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=args.workers if not args.reload else 1,
            log_level=args.log_level,
            access_log=True
        )
    except ImportError:
        print("❌ Error: uvicorn not installed!")
        print("📦 Install dependencies with: poetry install")
        print("   or: pip install -r requirements.txt")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n👋 Shutting down Strava NoShoes...")
    except Exception as e:
        print(f"❌ Error starting application: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()