import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.getenv("BIDDING_DATA_DIR", "data")).resolve()
SKILL = ROOT / "skills" / "tender-acquisition" / "SKILL.md"
