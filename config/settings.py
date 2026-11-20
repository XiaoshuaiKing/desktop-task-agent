from pathlib import Path
import os
from dotenv import load_dotenv
ROOT=Path(__file__).resolve().parent.parent
load_dotenv(ROOT/'.env')
DATA_DIR=ROOT/'data'
LOG_DIR=ROOT/'logs'
