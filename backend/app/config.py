from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import os
import re
import unicodedata

from dotenv import dotenv_values
from dotenv.parser import parse_stream
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ("sample1.fasta", "sample2.fasta", "sample3.fasta")
ANALYSES = ("seq_stats", "find_orf")
CUSTOM_ANALYSIS = "custom_python"
TASK_TYPE_PATTERN = re.compile(r"[\w-]{1,64}")
UPLOAD_LIMIT = 5 * 1024 * 1024
LOG_LIMIT = 8 * 1024
DEFAULT_INPUT_TOKEN_BUDGET = 24_000
STDIO_SETTING = "MCP_STDIO_SERVERS"
STDIO_CONFIG_ERROR = "MCP_STDIO_SERVERS 必须是以服务标识为键的命令配置 JSON 对象。"


def normalize_task_type(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip().casefold())


def parse_stdio_servers(value: str | None) -> dict[str, StdioServerParameters]:
    try:
        profiles = json.loads((value or "{}").strip())
        if not isinstance(profiles, dict) or any(not re.fullmatch(r"[\w-]{1,80}", name) for name in profiles):
            raise ValueError
        servers = {name: StdioServerParameters.model_validate(profile) for name, profile in profiles.items()}
        if any(not profile.command.strip() for profile in servers.values()):
            raise ValueError
    except ValueError:
        raise ValueError(STDIO_CONFIG_ERROR) from None
    return servers


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "var"
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = field(default="", repr=False)
    job_timeout: float = 60
    job_workers: int = 2
    input_token_budget: int = DEFAULT_INPUT_TOKEN_BUDGET
    job_max_auto_retries: int = 2
    job_retry_delay: float = 3
    mcp_http_endpoints: tuple[str, ...] = ()
    mcp_stdio_servers: dict[str, StdioServerParameters] = field(default_factory=dict, repr=False)
    _env_file: Path | None = field(default=None, repr=False)

    def reload_stdio_servers(self) -> "Settings":
        if self._env_file is None:
            return self
        value = os.environ.get(STDIO_SETTING)
        if value is None:
            try:
                with self._env_file.open(encoding="utf-8-sig") as stream:
                    for entry in parse_stream(stream):
                        if entry.key == STDIO_SETTING:
                            value = entry.value
                        if entry.error and re.match(
                            r"\s*(?:export\s+)?(?:MCP_STDIO_SERVERS|'MCP_STDIO_SERVERS')(?=\s|=|$)",
                            entry.original.string,
                        ):
                            raise ValueError(STDIO_CONFIG_ERROR)
            except FileNotFoundError:
                pass
        return replace(self, mcp_stdio_servers=parse_stdio_servers(value))

    @classmethod
    def load(cls) -> "Settings":
        env_file = ROOT / ".env"
        values = dotenv_values(env_file, encoding="utf-8-sig", interpolate=False)

        def value(key: str, default: str = "") -> str:
            return (os.environ.get(key, values.get(key)) or default).strip()

        try:
            input_budget = int(value("DEEPSEEK_INPUT_TOKEN_BUDGET", str(DEFAULT_INPUT_TOKEN_BUDGET)))
            if input_budget < 4096:
                raise ValueError
        except ValueError:
            raise ValueError("DEEPSEEK_INPUT_TOKEN_BUDGET 必须是至少 4096 的整数。") from None

        endpoints = value("MCP_HTTP_ENDPOINTS").split(",")
        return cls(
            data_dir=(ROOT / value("APP_DATA_DIR", "var")).resolve(),
            model=value("DEEPSEEK_MODEL", "deepseek-flash"),
            base_url=value("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            api_key=value("DEEPSEEK_API_KEY"),
            input_token_budget=input_budget,
            mcp_http_endpoints=tuple(dict.fromkeys(url.strip() for url in endpoints if url.strip())),
            mcp_stdio_servers=parse_stdio_servers(value(STDIO_SETTING, "{}")),
            _env_file=env_file,
        )
