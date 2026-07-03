import logging
from logging.handlers import RotatingFileHandler
import os

def setup_app_logging():
    log_dir = "/var/log/czentrix"
    os.makedirs(log_dir, exist_ok=True)
    
    file_handler = RotatingFileHandler(
        f"{log_dir}/qa_smart_admin.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB limit
        backupCount=5               # Keep 5 rotated files
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    
    # Configure root logger and force override of any existing configurations
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler], force=True)
