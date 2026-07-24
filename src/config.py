from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field


def _load_yaml_configs() -> dict:
    """Load config.yaml with optional docker override."""

    base_config = {}
    override_config = {}

    base_path = Path(__file__).parent.parent / "config.yaml"
    if base_path.exists():
        with open(base_path, "r") as f:
            base_config = yaml.safe_load(f) or {}

    docker_path = Path("/alpyca/config.yaml")
    if docker_path.exists():
        with open(docker_path, "r") as f:
            override_config = yaml.safe_load(f) or {}

    def deep_merge(base: dict, override: dict) -> dict:
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    return deep_merge(base_config, override_config)


class DeviceConfig(BaseModel):
    """Configuration for a single Birger EF-232 focuser."""

    entity: str = Field(default="Focuser")
    device_number: int = Field(default=0)
    port: str = Field(default="/dev/ttyUSB0")
    baud: int = Field(default=115200)
    timeout: float = Field(default=2.0)
    move_timeout: float = Field(default=60.0)
    # `la` is only needed when the lens changes or the controller reports its
    # stored range is bad — it keeps the learned range across power cycles, and
    # each relearn shifts the bounds a few counts, moving every stored position
    # with them. Off by default; the driver still relearns on ERR24.
    auto_learn: bool = Field(default=False)
    # Position to drive to on connect, in the same 0..MaxStep scale as Move.
    # MaxStep is not known until the range is read at connect, so the upper
    # bound is validated there rather than here. Null leaves the lens alone.
    initial_focus: Optional[int] = Field(default=None, ge=0)


class ServerConfig(BaseModel):
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=5000)


class Config(BaseModel):
    entity: str = Field(default="birger")
    server: ServerConfig = Field(default_factory=ServerConfig)
    log_level: str = Field(default="INFO")
    devices: List[DeviceConfig] = Field(default_factory=list)

    @classmethod
    def load(cls) -> "Config":
        return cls(**_load_yaml_configs())

    def get_device(self, device_number: int) -> Optional[DeviceConfig]:
        for device in self.devices:
            if device.device_number == device_number:
                return device
        return None


config = Config.load()
