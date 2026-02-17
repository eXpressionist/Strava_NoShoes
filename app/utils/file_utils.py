"""File management utilities."""

import os
import aiofiles
from pathlib import Path
from typing import List, Optional
from app.config import settings


class FileManager:
    """Utility class for file operations."""
    
    def __init__(self):
        self.gpx_storage_path = Path(settings.gpx_storage_path)
        self.gpx_storage_path.mkdir(parents=True, exist_ok=True)
    
    async def save_gpx_file(self, content: bytes, filename: str) -> str:
        """Save GPX content to file."""
        file_path = self.gpx_storage_path / filename
        
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(content)
        
        return str(file_path)
    
    async def read_gpx_file(self, filename: str) -> Optional[bytes]:
        """Read GPX file content."""
        file_path = self.gpx_storage_path / filename
        
        if not file_path.exists():
            return None
        
        async with aiofiles.open(file_path, 'rb') as f:
            return await f.read()
    
    def list_gpx_files(self) -> List[str]:
        """List all GPX files in storage."""
        if not self.gpx_storage_path.exists():
            return []
        
        return [f.name for f in self.gpx_storage_path.glob("*.gpx")]
    
    def delete_gpx_file(self, filename: str) -> bool:
        """Delete a GPX file."""
        file_path = self.gpx_storage_path / filename
        
        if file_path.exists():
            file_path.unlink()
            return True
        
        return False
    
    def get_file_size(self, filename: str) -> Optional[int]:
        """Get file size in bytes."""
        file_path = self.gpx_storage_path / filename
        
        if file_path.exists():
            return file_path.stat().st_size
        
        return None
    
    def cleanup_old_files(self, days: int = 30) -> int:
        """Clean up GPX files older than specified days."""
        import time
        
        if not self.gpx_storage_path.exists():
            return 0
        
        current_time = time.time()
        cutoff_time = current_time - (days * 24 * 60 * 60)
        deleted_count = 0
        
        for file_path in self.gpx_storage_path.glob("*.gpx"):
            if file_path.stat().st_mtime < cutoff_time:
                file_path.unlink()
                deleted_count += 1
        
        return deleted_count
    
    def get_storage_stats(self) -> dict:
        """Get storage statistics."""
        if not self.gpx_storage_path.exists():
            return {
                "total_files": 0,
                "total_size_bytes": 0,
                "total_size_mb": 0.0
            }
        
        files = list(self.gpx_storage_path.glob("*.gpx"))
        total_size = sum(f.stat().st_size for f in files)
        
        return {
            "total_files": len(files),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2)
        }