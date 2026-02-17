"""GPX file cleanup utility for managing disk space."""

import os
import logging
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)


def format_file_size(bytes_size: int) -> str:
    """Convert bytes to human-readable format.
    
    Args:
        bytes_size: Size in bytes
        
    Returns:
        Human-readable size string (e.g., "1.5 MB")
    """
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} TB"


def cleanup_all_gpx_files(storage_path: str) -> Dict[str, Any]:
    """Delete all GPX files from the storage directory.
    
    This function removes all .gpx files to manage disk space,
    especially useful when GPX files are large.
    
    Args:
        storage_path: Path to the GPX storage directory
        
    Returns:
        Dictionary with cleanup statistics:
        - files_deleted: Number of files deleted
        - space_freed: Total space freed in bytes
        - space_freed_human: Human-readable space freed
        - errors: List of error messages (if any)
    """
    stats = {
        'files_deleted': 0,
        'space_freed': 0,
        'space_freed_human': '0 B',
        'errors': []
    }
    
    # Ensure path exists
    storage_dir = Path(storage_path)
    if not storage_dir.exists():
        logger.warning(f"GPX storage path does not exist: {storage_path}")
        stats['errors'].append(f"Storage path does not exist: {storage_path}")
        return stats
    
    if not storage_dir.is_dir():
        logger.error(f"GPX storage path is not a directory: {storage_path}")
        stats['errors'].append(f"Storage path is not a directory: {storage_path}")
        return stats
    
    # Find all GPX files
    try:
        gpx_files = list(storage_dir.glob('*.gpx'))
        logger.info(f"Found {len(gpx_files)} GPX file(s) to delete")
        
        # Delete each file
        for gpx_file in gpx_files:
            try:
                file_size = gpx_file.stat().st_size
                gpx_file.unlink()
                stats['files_deleted'] += 1
                stats['space_freed'] += file_size
                logger.debug(f"Deleted: {gpx_file.name} ({format_file_size(file_size)})")
            except Exception as e:
                error_msg = f"Error deleting {gpx_file.name}: {str(e)}"
                logger.error(error_msg)
                stats['errors'].append(error_msg)
        
        stats['space_freed_human'] = format_file_size(stats['space_freed'])
        
        logger.info(
            f"GPX cleanup completed: {stats['files_deleted']} file(s) deleted, "
            f"{stats['space_freed_human']} freed"
        )
        
    except Exception as e:
        error_msg = f"Error during GPX cleanup: {str(e)}"
        logger.error(error_msg)
        stats['errors'].append(error_msg)
    
    return stats
