# developed traditionally with addition of AI assistance

import os
import logging
from datetime import datetime
import multiprocessing
import gc

def garbage_collection(development: bool, location: str):
    if development:
        # Enable debugging flags for leak statistics
        gc.set_debug(gc.DEBUG_LEAK)

    # Before calling collect, get a count of existing objects
    before = len(gc.get_objects())

    # Perform garbage collection
    collected = gc.collect()

    # After calling collect, get a new count of existing objects
    after = len(gc.get_objects())

    # Print or log before and after counts along with number collected
    logging.info(f"Garbage Collection at {location}:")
    logging.info(f"  Before GC: {before} objects")
    logging.info(f"  After GC: {after} objects")
    logging.info(f"  Collected: {collected} objects\n")

# Function to perform exponential backoff
def exponential_backoff(attempt, BASE_WAIT_TIME=None):
    return BASE_WAIT_TIME * (2 ** attempt)

def archive_log(lock, LOGFILE, LOG_DIRECTORY):
    """Archive log if it already exists."""
    if os.path.exists(LOGFILE):
        creation_time = os.path.getctime(LOGFILE)
        creation_date = datetime.fromtimestamp(creation_time).strftime('%Y-%m-%d_%H-%M-%S')
        archived_logfile = os.path.join(LOG_DIRECTORY, f"log_{creation_date}.log")
        
        # Rename with a lock to prevent multiple access
        with lock:
            os.rename(LOGFILE, archived_logfile)