from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ("sample1.fasta", "sample2.fasta", "sample3.fasta")
ANALYSES = ("seq_stats", "find_orf")
UPLOAD_LIMIT = 5 * 1024 * 1024
LOG_LIMIT = 8 * 1024


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "var"
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = field(default="", repr=False)
    job_timeout: float = 60
    job_workers: int = 2

    @classmethod
    def load(cls) -> "Settings":
        values = dotenv_values(ROOT / ".env", encoding="utf-8-sig", interpolate=False)

        def value(key: str, default: str = "") -> str:
            return (os.environ.get(key, values.get(key)) or default).strip()

        return cls(
            data_dir=(ROOT / value("APP_DATA_DIR", "var")).resolve(),
            model=value("DEEPSEEK_MODEL", "deepseek-flash"),
            base_url=value("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            api_key=value("DEEPSEEK_API_KEY"),
        )
