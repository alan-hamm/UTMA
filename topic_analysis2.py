# topic_analysis.py - Scalable Topic Analysis Script
# Author: Alan Hamm
# Date: April 2024
#
# Description:
# This script performs large-scale topic modeling analysis on document corpora,
# utilizing Dask and Gensim within the Scalable LDA Insights Framework (SLIF).
#
# Usage:
# Run this script from the terminal with specific parameters for analysis.
# Example: python topic_analysis.py --input_dir=<path> --output_dir=<path>
#
# Dependencies:
# - Requires PostgreSQL for database operations
# - Python libraries: SLIF, Dask, Gensim, SQLAlchemy, etc.
#
# Developed with AI assistance.

#%%
from SLIF import *

import argparse

from dask.distributed import Client, LocalCluster, performance_report, wait
from distributed import Future
import dask
import socket
import tornado
import re

import yaml # used for logging configuration file
import logging
import logging.config
import sqlalchemy

import sys
import os

from datetime import datetime

from tqdm import tqdm
from time import time, sleep

import random
import math
import numpy as np
import pandas as pd

import itertools

import hashlib

import pickle

# Dask dashboard throws deprecation warnings w.r.t. Bokeh
import warnings
from bokeh.util.deprecation import BokehDeprecationWarning

# pyLDAvis throws errors when using jc_PCoA(instead use MMD5)
from numpy import ComplexWarning

#import multiprocessing

###################################
# BEGIN SCRIPT CONFIGURATION HERE #
###################################
def task_callback(future):
    if future.status == 'error':
        print(f"Task failed with exception: {future.exception()}")
    else:
        print("Task completed successfully")

def parse_args():
    """Parse command-line arguments for configuring the topic analysis script."""
    parser = argparse.ArgumentParser(description="Configure the topic analysis script using command-line arguments.")
    
    # Database Connection Arguments
    parser.add_argument("--username", type=str, help="Username for accessing the PostgreSQL database.")
    parser.add_argument("--password", type=str, help="Password for the specified PostgreSQL username.")
    parser.add_argument("--host", type=str, help="Hostname or IP address of the PostgreSQL server (e.g., 'localhost' or '192.168.1.1').")
    parser.add_argument("--port", type=int, help="Port number for the PostgreSQL server (default is 5432).")
    parser.add_argument("--database", type=str, help="Name of the PostgreSQL database to connect to.")
    
    # Corpus and Data Arguments
    parser.add_argument("--corpus_label", type=str, help="Unique label used to identify the corpus in outputs and logs. Must be suitable as a PostgreSQL table name.")
    parser.add_argument("--data_source", type=str, help="File path to the JSON file containing the data for analysis.")
    parser.add_argument("--train_ratio", type=float, help="Fraction of data to use for training (e.g., 0.8 for 80% training and 20% testing).")
    parser.add_argument("--validation_ratio", type=float, help="Fraction of data to use for validation.")

    # Topic Modeling Parameters
    parser.add_argument("--start_topics", type=int, help="Starting number of topics for evaluation.")
    parser.add_argument("--end_topics", type=int, help="Ending number of topics for evaluation.")
    parser.add_argument("--step_size", type=int, help="Incremental step size for increasing the topic count between start_topics and end_topics.")

    # System Resource Management
    parser.add_argument("--num_workers", type=int, help="Minimum number of CPU cores to utilize for parallel processing.")
    parser.add_argument("--max_workers", type=int, help="Maximum number of CPU cores allocated for parallel processing.")
    parser.add_argument("--num_threads", type=int, help="Maximum number of threads per core for efficient use of resources.")
    parser.add_argument("--max_memory", type=int, help="Maximum RAM (in GB) allowed per core for processing.")
    parser.add_argument("--mem_threshold", type=int, help="Memory usage threshold (in GB) to trigger data spill to disk.")
    parser.add_argument("--max_cpu", type=int, help="Maximum CPU utilization percentage to prevent overuse of resources.")
    parser.add_argument("--mem_spill", type=str, help="Directory for temporarily storing data when memory limits are exceeded.")

    # Gensim Model Settings
    parser.add_argument("--passes", type=int, help="Number of complete passes through the data for the Gensim topic model.")
    parser.add_argument("--iterations", type=int, help="Total number of iterations for the model to converge.")
    parser.add_argument("--update_every", type=int, help="Frequency (in number of documents) to update model parameters during training.")
    parser.add_argument("--eval_every", type=int, help="Frequency (in iterations) for evaluating model perplexity and logging progress.")
    parser.add_argument("--random_state", type=int, help="Seed value to ensure reproducibility of results.")
    parser.add_argument("--per_word_topics", type=bool, help="Whether to compute per-word topic probabilities (True/False).")

    # Batch Processing Parameters
    parser.add_argument("--futures_batches", type=int, help="Number of batches to process concurrently.")
    parser.add_argument("--base_batch_size", type=int, help="Initial number of documents processed in parallel in each batch.")
    parser.add_argument("--max_batch_size", type=int, help="Maximum batch size, representing the upper limit of documents processed in parallel.")
    parser.add_argument("--increase_factor", type=float, help="Percentage increase in batch size after successful processing.")
    parser.add_argument("--decrease_factor", type=float, help="Percentage decrease in batch size after failed processing.")
    parser.add_argument("--max_retries", type=int, help="Maximum attempts to retry failed batch processing.")
    parser.add_argument("--base_wait_time", type=int, help="Initial wait time in seconds for exponential backoff during retries.")

    # Directories and Logging
    parser.add_argument("--log_dir", type=str, help="Directory path for saving log files.")
    parser.add_argument("--root_dir", type=str, help="Root directory for saving project outputs, metadata, and temporary files.")

    args = parser.parse_args()

    # Validate corpus_label against PostgreSQL table naming conventions
    if args.corpus_label:
        if not re.match(r'^[a-z][a-z0-9_]{0,62}$', args.corpus_label):
            error_msg = "Invalid corpus_label: must start with a lowercase letter, can only contain lowercase letters, numbers, and underscores, and be up to 63 characters long."
            logging.error(error_msg)
            print(error_msg)
            sys.exit(1)

    return args

# Parse CLI arguments
args = parse_args()

# Define required arguments with corresponding error messages
required_args = {
    "username": "No value was entered for username",
    "password": "No value was entered for password",
    "database": "No value was entered for database",
    "corpus_label": "No value was entered for corpus_label",
    "data_source": "No value was entered for data_source",
    "end_topics": "No value was entered for end_topics",
    "step_size": "No value was entered for step_size",
    "max_memory": "No value was entered for max_memory",
    "mem_threshold": "No value was entered for mem_threshold",
    "futures_batches": "No value was entered for futures_batches",
}

# Check for required arguments and log error if missing
for arg, error_msg in required_args.items():
    if getattr(args, arg) is None:
        logging.error(error_msg)
        print(error_msg)
        sys.exit(1)

# Load and define parameters based on arguments or apply defaults
USERNAME = args.username
PASSWORD = args.password
HOST = args.host if args.host is not None else "localhost"
PORT = args.port if args.port is not None else 5432
DATABASE = args.database
CONNECTION_STRING = f"postgresql://{USERNAME}:{PASSWORD}@{HOST}:{PORT}/{DATABASE}"

CORPUS_LABEL = args.corpus_label
DATA_SOURCE = args.data_source

TRAIN_RATIO = args.train_ratio if args.train_ratio is not None else 0.70
VALIDATION_RATIO = args.validation_ratio if args.validation_ratio is not None else 0.15

START_TOPICS = args.start_topics if args.start_topics is not None else 1
END_TOPICS = args.end_topics
STEP_SIZE = args.step_size

CORES = args.num_workers if args.num_workers is not None else 1
MAXIMUM_CORES = args.max_workers if args.max_workers is not None else 1
THREADS_PER_CORE = args.num_threads if args.num_threads is not None else 1
# Convert max_memory to a string with "GB" suffix for compatibility with Dask LocalCluster() object
RAM_MEMORY_LIMIT = f"{args.max_memory}GB" if args.max_memory is not None else "4GB"  # Default to "4GB" if not provided
MEMORY_UTILIZATION_THRESHOLD = (args.mem_threshold * (1024 ** 3)) if args.mem_threshold else 4 * (1024 ** 3)
CPU_UTILIZATION_THRESHOLD = args.max_cpu if args.max_cpu is not None else 100
DASK_DIR = args.mem_spill if args.mem_spill else os.path.expanduser("~/temp/slif/max_spill")
os.makedirs(DASK_DIR, exist_ok=True)

# Model configurations
PASSES = args.passes if args.passes is not None else 15
ITERATIONS = args.iterations if args.iterations is not None else 100
UPDATE_EVERY = args.update_every if args.update_every is not None else 5
EVAL_EVERY = args.eval_every if args.eval_every is not None else 5
RANDOM_STATE = args.random_state if args.random_state is not None else 50
PER_WORD_TOPICS = args.per_word_topics if args.per_word_topics is not None else True

# Batch configurations
FUTURES_BATCH_SIZE = args.futures_batches # number of input docuemtns to read in batches
BATCH_SIZE = args.base_batch_size if args.base_batch_size is not None else FUTURES_BATCH_SIZE # number of documents used in each iteration of creating/training/saving 
MAX_BATCH_SIZE = args.max_batch_size if args.max_batch_size is not None else FUTURES_BATCH_SIZE * 10 # the maximum number of documents(ie batches) assigned depending upon sys performance
MIN_BATCH_SIZE = max(1, math.ceil(MAX_BATCH_SIZE * .10)) # the fewest number of docs(ie batches) to be processed if system is under stress

# Batch size adjustments and retry logic
INCREASE_FACTOR = args.increase_factor if args.increase_factor is not None else 1.05
DECREASE_FACTOR = args.decrease_factor if args.decrease_factor is not None else 0.10
MAX_RETRIES = args.max_retries if args.max_retries is not None else 5
BASE_WAIT_TIME = args.base_wait_time if args.base_wait_time is not None else 30

# Ensure required directories exist
ROOT_DIR = args.root_dir or os.path.expanduser("~/temp/slif/")
LOG_DIRECTORY = args.log_dir or os.path.join(ROOT_DIR, "log")
IMAGE_DIR = os.path.join(ROOT_DIR, "visuals")
PYLDA_DIR = os.path.join(IMAGE_DIR, 'pyLDAvis')
PCOA_DIR = os.path.join(IMAGE_DIR, 'PCoA')
METADATA_DIR = os.path.join(ROOT_DIR, "metadata")
TEXTS_ZIP_DIR = os.path.join(ROOT_DIR, "texts_zip")

for directory in [ROOT_DIR, LOG_DIRECTORY, IMAGE_DIR, PYLDA_DIR, PCOA_DIR, METADATA_DIR, TEXTS_ZIP_DIR]:
    os.makedirs(directory, exist_ok=True)

# Set JOBLIB_TEMP_FOLDER based on ROOT_DIR and CORPUS_LABEL
JOBLIB_TEMP_FOLDER = os.path.join(ROOT_DIR, "log", "joblib") if CORPUS_LABEL else os.path.join(ROOT_DIR, "log", "joblib")
os.makedirs(JOBLIB_TEMP_FOLDER, exist_ok=True)
os.environ['JOBLIB_TEMP_FOLDER'] = JOBLIB_TEMP_FOLDER


###############################
###############################
# DO NOT EDIT BELOW THIS LINE #
###############################
###############################

# to escape: distributed.nanny - WARNING - Worker process still alive after 4.0 seconds, killing
# https://github.com/dask/dask-jobqueue/issues/391
scheduler_options={"host":socket.gethostname()}

# Ensure the LOG_DIRECTORY exists
if args.log_dir: LOG_DIRECTORY = args.log_dir
if args.root_dir: ROOT_DIR = args.root_dir
os.makedirs(ROOT_DIR, exist_ok=True)
os.makedirs(LOG_DIRECTORY, exist_ok=True)


#print("Script started successfully.")
#sys.exit(0)

# Define the top-level directory and subdirectories
LOG_DIR = os.path.join(ROOT_DIR, "log")
IMAGE_DIR = os.path.join(ROOT_DIR, "visuals")
PYLDA_DIR = os.path.join(IMAGE_DIR, 'pyLDAvis')
PCOA_DIR = os.path.join(IMAGE_DIR, 'PCoA')
METADATA_DIR = os.path.join(ROOT_DIR, "metadata")
TEXTS_ZIP_DIR = os.path.join(ROOT_DIR, "texts_zip")

# Ensure that all necessary directories exist
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(PYLDA_DIR, exist_ok=True)
os.makedirs(PCOA_DIR, exist_ok=True)
os.makedirs(METADATA_DIR, exist_ok=True)
os.makedirs(TEXTS_ZIP_DIR, exist_ok=True)

# Redirect stderr to the file
#sys.stderr = open(f"{LOG_DIR}/stderr.log", "w")

# Get the current date and time for log filename
#now = datetime.now()

# Format the date and time as per your requirement
# Note: %w is the day of the week as a decimal (0=Sunday, 6=Saturday)
#       %Y is the four-digit year
#       %m is the two-digit month (01-12)
#       %H%M is the hour (00-23) followed by minute (00-59) in 24hr format
#log_filename = now.strftime('log-%w-%m-%Y-%H%M.log')
#log_filename = 'log-0250.log'
# Check if the environment variable is already set
if 'LOG_START_TIME' not in os.environ:
    os.environ['LOG_START_TIME'] = datetime.now().strftime('%w-%m-%Y-%H%M')

# Use the fixed timestamp from the environment variable
log_filename = f"log-{os.environ['LOG_START_TIME']}.log"
LOGFILE = os.path.join(LOG_DIRECTORY, log_filename)  # Directly join log_filename with LOG_DIRECTORY

# Configure logging to write to a file with this name
logging.basicConfig(
    filename=LOGFILE,
    filemode='a',  # Append mode if you want to keep adding to the same file during the day
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    #level=logging.INFO  # Set to DEBUG for detailed logging
    level=logging.DEBUG  # Set to DEBUG for detailed logging
)

##########################################
# Filter out the specific warning message
##########################################
# Suppress ComplexWarnings generated in create_vis() function with pyLDAvis, note: this 
# is caused by using js_PCoA in the prepare() method call. Intsead of js_PCoA, MMDS is 
# implemented.
warnings.simplefilter('ignore', ComplexWarning)

# Get the logger for 'distributed' package
distributed_logger = logging.getLogger('distributed')

# Disable Bokeh deprecation warnings
warnings.filterwarnings("ignore", category=BokehDeprecationWarning)
# Set the logging level for distributed.utils_perf to suppress warnings
logging.getLogger('distributed.utils_perf').setLevel(logging.ERROR)
warnings.filterwarnings("ignore", module="distributed.utils_perf")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="distributed.worker")  # Adjust the module parameter as needed

# Suppress specific SettingWithCopyWarning from pyLDAvis internals
# line 299: A value is trying to be set on a copy of a slice from a DataFrame.
#   Try using .loc[row_indexer,col_indexer] = value instead
# line 300: A value is trying to be set on a copy of a slice from a DataFrame. Try using .loc[row_indexer,col_indexer] = value instead
warnings.filterwarnings("ignore", category=Warning, module=r"pyLDAvis\._prepare")


# Suppress StreamClosedError warnings from Tornado
# \Lib\site-packages\distributed\comm\tcp.py", line 225, in read
#   frames_nosplit_nbytes_bin = await stream.read_bytes(fmt_size)
#                                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#tornado.iostream.StreamClosedError: Stream is closed
#warnings.filterwarnings("ignore", category=tornado.iostream.StreamClosedError)
logging.getLogger('tornado').setLevel(logging.ERROR)

# Get the logger for 'sqlalchemy.engine' which is used by SQLAlchemy to log SQL queries
sqlalchemy_logger = logging.getLogger('sqlalchemy.engine')

# Remove all handlers associated with 'sqlalchemy.engine' (this includes StreamHandler)
for handler in sqlalchemy_logger.handlers[:]:
    sqlalchemy_logger.removeHandler(handler)

# Add a NullHandler to prevent default StreamHandler from being added later on
null_handler = logging.NullHandler()
sqlalchemy_logger.addHandler(null_handler)

# Optionally set a higher level if you want to ignore INFO logs from sqlalchemy.engine
# sqlalchemy_logger.setLevel(logging.WARNING)

# Archive log only once if running in main process
#if multiprocessing.current_process().name == 'MainProcess':
#archive_log(logger, LOGFILE, LOG_DIR)

# Enable serialization optimizations 
dask.config.set(scheduler='distributed', serialize=True) #note: could this be causing the pyLDAvis creation problems??
dask.config.set({'logging.distributed': 'error'})
dask.config.set({"distributed.scheduler.worker-ttl": '30m'})
dask.config.set({'distributed.worker.daemon': False})

#These settings disable automatic spilling but allow for pausing work when 80% of memory is consumed and terminating workers at 99%.
dask.config.set({'distributed.worker.memory.target': False,
                 'distributed.worker.memory.spill': False,
                 'distributed.worker.memory.pause': 0.8
                 ,'distributed.worker.memory.terminate': 0.99})



# https://distributed.dask.org/en/latest/worker-memory.html#memory-not-released-back-to-the-os
if __name__=="__main__":
    # Multiprocessing (processes=True): This mode creates multiple separate Python processes, 
    # each with its own Python interpreter and memory space. Since each process has its own GIL, 
    # they can execute CPU-bound tasks in true parallel on multiple cores without being affected 
    # by the GIL. This typically results in better utilization of multi-core CPUs for compute-intensive tasks.
    
    #Multithreading (processes=False): This mode uses threads within a single Python process for 
    # concurrent execution. While this is efficient for I/O-bound tasks due to low overhead in 
    # switching between threads, it's less effective for CPU-bound tasks because of Python's Global 
    # Interpreter Lock (GIL). The GIL prevents multiple native threads from executing Python bytecodes 
    # at once, which can limit the performance gains from multithreading when running CPU-intensive workloads.
    cluster = LocalCluster(
            n_workers=CORES,
            threads_per_worker=THREADS_PER_CORE,
            processes=True,
            memory_limit=RAM_MEMORY_LIMIT,
            local_directory=DASK_DIR,
            #dashboard_address=None,
            dashboard_address=":8787",
            protocol="tcp",
            death_timeout='1000s',  # Increase timeout before forced kill
    )


    # Create the distributed client
    client = Client(cluster, timeout='1000s')

    # set for adaptive scaling
    client.cluster.adapt(minimum=CORES, maximum=MAXIMUM_CORES)
    
    # Get information about workers from scheduler
    workers_info = client.scheduler_info()["workers"]

    # Iterate over workers and set their memory limits
    for worker_id, worker_info in workers_info.items():
        worker_info["memory_limit"] = RAM_MEMORY_LIMIT

    # Check if the Dask client is connected to a scheduler:
    if client.status == "running":
        logging.info("Dask client is connected to a scheduler.")
        # Scatter the embedding vectors across Dask workers
    else:
        logging.error("Dask client is not connected to a scheduler.")
        logging.error("The system is shutting down.")
        client.close()
        cluster.close()
        sys.exit()

    # Check if Dask workers are running:
    if len(client.scheduler_info()["workers"]) > 0:
        logging.info(f"{CORES} Dask workers are running.")
    else:
        logging.error("No Dask workers are running.")
        logging.error("The system is shutting down.")
        client.close()
        cluster.close()
        sys.exit()


    print("Creating training and evaluation samples...")

    started = time()
    
    scattered_train_data_futures = []
    scattered_validation_data_futures = []
    scattered_test_data_futures = []
    all_futures = []

    # Process each batch as it is generated
    for batch_info in futures_create_lda_datasets(DATA_SOURCE, TRAIN_RATIO, VALIDATION_RATIO, FUTURES_BATCH_SIZE):
        #print(f"Received batch: {batch_info['type']}")  # Debugging output
        if batch_info['type'] == "train":
            # Handle training data
            #print("We are inside the IF/ELSE block for producing TRAIN scatter.")
            try:
                scattered_future = client.scatter(batch_info['data'])
                #scattered_future.add_done_callback(task_callback)
                # After yielding each batch
                #print(f"Submitted {batch_info['type']} batch of size {len(batch_info['data'])} to Dask.")

                scattered_train_data_futures.append(scattered_future)
                
            except Exception as e:
                logging.error(f"There was an issue with creating the TRAIN scattered_future list: {e}")

        elif batch_info['type'] == 'validation':
            # Handle validation data
            try:
                scattered_future = client.scatter(batch_info['data'])
                #scattered_future.add_done_callback(task_callback)
                # After yielding each batch
                #print(f"Submitted {batch_info['type']} batch of size {len(batch_info['data'])} to Dask.")

                scattered_validation_data_futures.append(scattered_future)
            except Exception as e:
                logging.error(f"There was an issue with creating the VALIDATION scattererd_future list: {e}")

        elif batch_info['type'] == 'test':
            # Handle test data
            try:
                scattered_future = client.scatter(batch_info['data'])
                #scattered_future.add_done_callback(task_callback)
                # After yielding each batch
                #print(f"Submitted {batch_info['type']} batch of size {len(batch_info['data'])} to Dask.")

                scattered_test_data_futures.append(scattered_future)
            except Exception as e:
                logging.error(f"There was an issue with creating the TEST scattererd_future list: {e}")
        else:
            print("There are documents not being scattered across the workers.")
        
    #print(f"Completed creation of train-validation-test split in {round((time() - started)/60,2)} minutes.\n")
    logging.info(f"Completed creation of train-validation-test split in {round((time() - started)/60,2)} minutes.\n")
    #print("Document scatter across workers complete...\n")
    logging.info("Document scatter across workers complete...")
    print(f"\nFinal count - Number of training futures: {len(scattered_train_data_futures)}, "
      f"Number of validation futures: {len(scattered_validation_data_futures)}, "
      f"Number of test futures: {len(scattered_test_data_futures)}\n")

    train_futures = []  # List to store futures for training
    validation_futures = []  # List to store futures for validation
    test_futures = []  # List to store futures for testing
   
    num_topics = len(range(START_TOPICS, END_TOPICS + 1, STEP_SIZE))

    #####################################################
    # PROCESS AND CONVERT ALPHA AND ETA PARAMETER VALUES
    #####################################################
    # Calculate numeric_alpha for symmetric prior
    numeric_symmetric = 1.0 / num_topics
    # Calculate numeric_alpha for asymmetric prior (using best judgment)
    numeric_asymmetric = 1.0 / (num_topics + np.sqrt(num_topics))
    # Create the list with numeric values
    numeric_alpha = [numeric_symmetric, numeric_asymmetric] + np.arange(0.01, 1, 0.3).tolist()
    numeric_beta = [numeric_symmetric] + np.arange(0.01, 1, 0.3).tolist()

    # The parameter `alpha` in Latent Dirichlet Allocation (LDA) represents the concentration parameter of the Dirichlet 
    # prior distribution for the topic-document distribution.
    # It controls the sparsity of the resulting document-topic distributions.
    # A lower value of `alpha` leads to sparser distributions, meaning that each document is likely to be associated
    # with fewer topics. Conversely, a higher value of `alpha` encourages documents to be associated with more
    # topics, resulting in denser distributions.

    # The choice of `alpha` affects the balance between topic diversity and document specificity in LDA modeling.
    alpha_values = ['symmetric', 'asymmetric']
    alpha_values += np.arange(0.01, 1, 0.3).tolist()

    # In Latent Dirichlet Allocation (LDA) topic analysis, the beta parameter represents the concentration 
    # parameter of the Dirichlet distribution used to model the topic-word distribution. It controls the 
    # sparsity of topics by influencing how likely a given word is to be assigned to a particular topic.
    # A higher value of beta encourages topics to have a more uniform distribution over words, resulting in more 
    # general and diverse topics. Conversely, a lower value of beta promotes sparser topics with fewer dominant words.

    # The choice of beta can impact the interpretability and granularity of the discovered topics in LDA.
    beta_values = ['symmetric']
    beta_values += np.arange(0.01, 1, 0.3).tolist()

    #################################################
    # CREATE PARAMETER COMBINATIONS FOR GRID SEARCH
    #################################################
    # Create a list of all combinations of n_topics, alpha_value, beta_value, and train_eval
    phases = ["train", "validation", "test"]
    combinations = list(itertools.product(range(START_TOPICS, END_TOPICS + 1, STEP_SIZE), alpha_values, beta_values, phases))

    # Define sample size for overall combinations if needed
    sample_fraction = 0.375
    # Ensure that `sample_size` doesn’t exceed the number of total combinations
    sample_size = min(max(1, int(len(combinations) * sample_fraction)), len(combinations))

    # Generate random combinations
    random_combinations = random.sample(combinations, sample_size)

    # Determine undrawn combinations
    undrawn_combinations = list(set(combinations) - set(random_combinations))

    print(f"The random sample combinations contain {len(random_combinations)}. This leaves {len(undrawn_combinations)} undrawn combinations.\n")

    # Randomly sample from the entire set of combinations
    random_combinations = random.sample(combinations, sample_size)

    # Determine undrawn combinations
    undrawn_combinations = list(set(combinations) - set(random_combinations))

    print(f"The random sample combinations contain {len(random_combinations)}. This leaves {len(undrawn_combinations)} undrawn combinations.\n")
    #for record in random_combinations:
    #    print("This is the random combination", record)
    for i, item in enumerate(random_combinations):
        if not isinstance(item, tuple) or len(item) != 4:
            print(f"Issue at index {i}: {item}")

    # Create empty lists to store all future objects for training and evaluation
    train_futures = []
    validation_futures = []
    test_futures = []

    TOTAL_COMBINATIONS = len(random_combinations) * (len(scattered_train_data_futures) + len(scattered_validation_data_futures) + len(scattered_test_data_futures))
    progress_bar = tqdm(total=TOTAL_COMBINATIONS, desc="Creating and saving models", file=sys.stdout)
    # Iterate over the combinations and submit tasks
    #print(f"Length of random_combinations: {len(random_combinations)}")

    #random_combinations = [
    #    (35, 0.61, 0.61, 'test'),
    #    (45, 0.31, 0.91, 'validation'),
    #    # (add more items for testing if needed)
    #] * 150  # Ensure list length is sufficient for testing
    #print("the length of the random combinations", len(random_combinations))
    train_models_dict = {}
    for i, (n_topics, alpha_value, beta_value, train_eval_type) in enumerate(random_combinations):
        print(f"Loop iteration {i+1} - Number of topics: {n_topics}, Alpha: {alpha_value}, Beta: {beta_value}, Type: {train_eval_type}")

        # determine if throttling is needed
        logging.info("Evaluating if adaptive throttling is necessary (method exponential backoff)...")
        started, throttle_attempt = time(), 0

        # https://distributed.dask.org/en/latest/worker-memory.html#memory-not-released-back-to-the-os
        
        while throttle_attempt < MAX_RETRIES:
            scheduler_info = client.scheduler_info()
            all_workers_below_cpu_threshold = all(worker['metrics']['cpu'] < CPU_UTILIZATION_THRESHOLD for worker in scheduler_info['workers'].values())
            all_workers_below_memory_threshold = all(worker['metrics']['memory'] < MEMORY_UTILIZATION_THRESHOLD for worker in scheduler_info['workers'].values())

            if not (all_workers_below_cpu_threshold and all_workers_below_memory_threshold):
                logging.warning(f"Adaptive throttling (attempt {throttle_attempt} of {MAX_RETRIES-1})")
                # Uncomment the next line if you want to log hyperparameters information as well.
                logging.warning(f"for LdaModel hyperparameters combination -- type: {train_eval_type}, topic: {n_topics}, ALPHA: {alpha_values} and ETA {beta_values}")
                sleep(exponential_backoff(throttle_attempt, BASE_WAIT_TIME=BASE_WAIT_TIME))
                throttle_attempt += 1
            else:
                break

        if throttle_attempt == MAX_RETRIES:
            logging.warning("Maximum retries reached. The workers are still above the CPU or Memory threshold.")
            #garbage_collection(False, 'Max Retries - throttling attempt')
        else:
            logging.info("Proceeding with workload as workers are below the CPU and Memory thresholds.")


        # TRAINING PHASE
        num_workers = len(client.scheduler_info()["workers"])
        for scattered_data in scattered_train_data_futures:
            try:
                future = client.submit(
                    train_model_v2, n_topics, alpha_value, beta_value, scattered_data, "train",
                    RANDOM_STATE, PASSES, ITERATIONS, UPDATE_EVERY, EVAL_EVERY, num_workers, PER_WORD_TOPICS
                )
                train_futures.append(future)
                logging.info(f"Training future appended. Total train futures: {len(train_futures)}")
            except Exception as e:
                logging.error(f"An error occurred in train_model() during training phase: {e}")
                logging.error(f"TYPE: train -- n_topics: {n_topics}, alpha: {alpha_value}, beta: {beta_value}, phase: train")
                raise
        done_train, _ = wait(train_futures, timeout=None)

        # Gather the completed training futures with error handling
        completed_train_futures = [done.result() for done in done_train]

        # Organize completed_train_futures by n_topics, alpha_value, beta_value for easy lookup
        for train_result in completed_train_futures:
            # Debugging output to inspect each result
            #print(f"\nType of train_result: {type(train_result)}")
            #print(f"Content of train_result: {train_result}")

            # Extract values, handling cases where they might be lists
            n_topics = train_result['topics']
            #print(f"\nThe Number of Topics: {n_topics}")
            # Extract the first item if alpha_str or beta_str is a list, otherwise use the value as-is
            alpha_value = train_result['alpha_str'][0] if isinstance(train_result['alpha_str'], list) else train_result['alpha_str']
            beta_value = train_result['beta_str'][0] if isinstance(train_result['beta_str'], list) else train_result['beta_str']
            ldamodel = train_result['lda_model']

            # Debugging output for the extracted values
            #print(f"n_topics: {n_topics}, alpha_value: {alpha_value}, beta_value: {beta_value}")

            # Use a tuple of n_topics, alpha_value, and beta_value as the key
            try:
                # Debugging output for the extracted values
                #print(f"n_topics: {n_topics}, alpha_value: {alpha_value}, beta_value: {beta_value}")
                train_models_dict[(n_topics, alpha_value, beta_value)] = ldamodel
            except TypeError as e:
                print(f"TypeError encountered: {e} - Key: {(n_topics, alpha_value, beta_value)}, Model: {ldamodel}")
        # Check the keys in train_models_dict
        print("Keys in train_models_dict:", list(train_models_dict.keys()))

        # VALIDATION PHASE
        validation_batches = [
            {'data': tokenized_list, 'n_topics': n, 'alpha': alpha_value, 'beta': beta_value}
            for tokenized_list, (n, alpha_value, beta_value, _) in zip(scattered_validation_data_futures, random_combinations)
        ]

        for batch in validation_batches:
            try:
                #print("We are before the compute statement")

                # Extract the batch data and hyperparameters
                validation_data = batch['data']  # This is the list of tokenized sentences
                n_topics = batch['n_topics']
                alpha_value = batch['alpha']
                beta_value = batch['beta']
                #print(" we made it pass the assignment statements ")

                # Look up the corresponding ldamodel from the training phase
                model_key = (n_topics, alpha_value, beta_value)
                if model_key in train_models_dict:
                    ldamodel = train_models_dict[model_key]
                    #print(" we made it into the IF block ")
                else:
                    logging.error(f"Model for key {model_key} not found in training results.")
                    continue  # Skip this batch if no corresponding model is found
                

                # Submit validation task with the matched ldamodel
                future = client.submit(
                    train_model_v2, n_topics, alpha_value, beta_value, validation_data, "validation",
                    RANDOM_STATE, PASSES, ITERATIONS, UPDATE_EVERY, EVAL_EVERY, num_workers, PER_WORD_TOPICS, ldamodel=ldamodel
                )
                validation_futures.append(future)
                logging.info(f"Validation future appended. Total validation futures: {len(validation_futures)}")

            except Exception as e:
                logging.error(f"An error in train_model() for validation phase: {e}")
                logging.error(f"TYPE: validation -- n_topics: {n_topics}, alpha: {alpha_value}, beta: {beta_value}, phase: validation")


        # Wait for validation futures to complete
        done_validation, _ = wait(validation_futures, timeout=None)
        completed_validation_futures = [done.result() for done in done_validation]
        #for c in completed_validation_futures:
        #    print(f"\nThis is validation list contents: {c}")
        #sys.exit()

        # TEST PHASE
        for scattered_data in scattered_test_data_futures:
            try:
                # Retrieve relevant parameters for each test batch
                test_result = scattered_data.result()  # Access the result of the Future object
                n_topics = test_result['n_topics']
                alpha_value = test_result['alpha_value']
                beta_value = test_result['beta_value']

                # Look up the corresponding ldamodel from the training phase
                model_key = (n_topics, alpha_value, beta_value)
                if model_key in train_models_dict:
                    ldamodel = train_models_dict[model_key]
                else:
                    logging.error(f"Model for key {model_key} not found in training results.")
                    continue  # Skip this batch if no corresponding model is found

                # Submit test task with the matched ldamodel
                future = client.submit(
                    train_model_v2, n_topics, alpha_value, beta_value, scattered_data, "test",
                    RANDOM_STATE, PASSES, ITERATIONS, UPDATE_EVERY, EVAL_EVERY, num_workers, PER_WORD_TOPICS, ldamodel=ldamodel
                )
                test_futures.append(future)
                logging.info(f"Test future appended. Total test futures: {len(test_futures)}")
            except Exception as e:
                logging.error(f"An error in train_model() for test phase: {e}")
                logging.error(f"TYPE: test -- n_topics: {n_topics}, alpha: {alpha_values}, beta: {beta_values}, phase: test")

        # Wait for test futures to complete
        done_test, _ = wait(test_futures, timeout=None)
        completed_test_futures = [done.result() for done in done_test]


  
        ########################
        # PROCESS VISUALIZATIONS
        ########################
        # Generate performance log filenames for each phase
        time_of_vis_call = pd.to_datetime('now').strftime('%Y%m%d%H%M%S%f')
        PERFORMANCE_TRAIN_LOG = os.path.join(IMAGE_DIR, f"vis_perf_train_{time_of_vis_call}.html")
        PERFORMANCE_VALIDATION_LOG = os.path.join(IMAGE_DIR, f"vis_perf_validation_{time_of_vis_call}.html")
        PERFORMANCE_TEST_LOG = os.path.join(IMAGE_DIR, f"vis_perf_test_{time_of_vis_call}.html")
        del time_of_vis_call
        
        #print(f"Number of train futures for visualization: {len(completed_train_futures)}")
        #print(f"Number of validation futures for visualization: {len(completed_validation_futures)}")
        #print(f"Number of test futures for visualization: {len(completed_test_futures)}\n")

        num_workers = len(client.scheduler_info()["workers"])
        
        #for record in completed_train_futures:
        #    print(f"{record['time_key']}")
        #print(f"Train futures: {len(completed_train_futures)}, Validation futures: {len(completed_validation_futures)}, Test futures: {len(completed_test_futures)}")
        # Run visualizations for each phase
        # Check if it's entering the visualization processing
        print(f"Entering visualization process for iteration {i+1}")
        try:
            train_pylda_vis, train_pcoa_vis = process_visualizations(client, completed_train_futures, "TRAIN", PERFORMANCE_TRAIN_LOG, num_workers, PYLDA_DIR, PCOA_DIR)
        except KeyError as e:
            print(f"KeyError encountered: {e}")
            continue  # Proceed to the next iteration
        except Exception as e:
            print(f"Unexpected error: {e}")
            break  # Stop the loop if there’s an unexpected error

        print(f"Completed visualization process for iteration {i+1}")
        """
        #progress_bar.update()
        validation_pylda_vis, validation_pcoa_vis = process_visualizations(client, completed_validation_futures, "VALIDATION", PERFORMANCE_VALIDATION_LOG, num_workers, PYLDA_DIR, PCOA_DIR)
        #progress_bar.update()
        test_pylda_vis, test_pcoa_vis = process_visualizations(client, completed_test_futures, "TEST", PERFORMANCE_TEST_LOG, num_workers, PYLDA_DIR, PCOA_DIR)
        #progress_bar.update()
        #############################
        # END PROCESS VISUALIZATIONS
        #############################            

        started = time()
        completed_pylda_vis = train_pylda_vis + validation_pylda_vis + test_pylda_vis
        completed_pcoa_vis = train_pcoa_vis + validation_pcoa_vis + test_pcoa_vis

        
        num_workers = len(client.scheduler_info()["workers"])
        logging.info(f"Writing processed completed futures to disk.")
        futures_length = (len(completed_train_futures)+len(completed_validation_futures)+len(completed_test_futures))
        completed_train_futures, completed_validation_futures, completed_test_futures = process_completed_futures(CONNECTION_STRING, \
                                                                                    CORPUS_LABEL, \
                                                                                    completed_train_futures, \
                                                                                    completed_validation_futures, \
                                                                                    completed_test_futures, \
                                                                                    futures_length, \
                                                                                    num_workers, \
                                                                                    BATCH_SIZE, \
                                                                                    TEXTS_ZIP_DIR, \
                                                                                    vis_pylda=completed_pylda_vis,
                                                                                    vis_pcoa=completed_pcoa_vis)
        """ 
        elapsed_time = round(((time() - started) / 60), 2)
        logging.info(f"Finished write processed completed futures to disk in  {elapsed_time} minutes")

        progress_bar.update()

        completed_train_futures.clear()
        completed_validation_futures.clear()
        completed_test_futures.clear()
        test_futures.clear()
        validation_futures.clear()
        train_futures.clear()
        client.rebalance()
         
    #garbage_collection(False, "Cleaning WAIT -> done, not_done")     
    progress_bar.close()
            
    client.close()
    cluster.close()