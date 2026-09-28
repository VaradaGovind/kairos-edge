"""Determinism and config logging utilities for Kairos experiments."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

try:
    import numpy as np
except Exception:  # pragma: no cover - optional dependency
    np = None

try:
    import torch
except Exception:  # pragma: no cover - optional dependency
    torch = None


@dataclass(frozen=True)
class DeterminismReport:
    seed: int
    python_seeded: bool
    numpy_seeded: bool
    torch_seeded: bool
    cuda_seeded: bool
    deterministic_algorithms: bool
    cublas_workspace_config: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "seed": self.seed,
            "python_seeded": self.python_seeded,
            "numpy_seeded": self.numpy_seeded,
            "torch_seeded": self.torch_seeded,
            "cuda_seeded": self.cuda_seeded,
            "deterministic_algorithms": self.deterministic_algorithms,
            "cublas_workspace_config": self.cublas_workspace_config,
        }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _config_fingerprint(config: Mapping[str, Any]) -> str:
    serialized = json.dumps(_json_safe(dict(config)), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def capture_runtime_environment() -> Dict[str, Any]:
    return {
        "cwd": str(Path.cwd()),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "environment": {
            "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED", ""),
            "CUBLAS_WORKSPACE_CONFIG": os.environ.get("CUBLAS_WORKSPACE_CONFIG", ""),
            "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        },
    }


def set_global_determinism(
    seed: int,
    *,
    deterministic_algorithms: bool = True,
) -> DeterminismReport:
    """Enforce deterministic seeds/flags for Python, NumPy, and PyTorch."""
    os.environ["PYTHONHASHSEED"] = str(seed)
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

    random.seed(seed)

    numpy_seeded = False
    if np is not None:
        np.random.seed(seed)
        numpy_seeded = True

    torch_seeded = False
    cuda_seeded = False
    if torch is not None:
        torch.manual_seed(seed)
        torch_seeded = True

        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
            cuda_seeded = True

        if hasattr(torch.backends, "cudnn"):
            torch.backends.cudnn.benchmark = False
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.allow_tf32 = False

        if hasattr(torch.backends, "cuda") and hasattr(torch.backends.cuda, "matmul"):
            torch.backends.cuda.matmul.allow_tf32 = False

        if deterministic_algorithms:
            torch.use_deterministic_algorithms(True, warn_only=False)

    return DeterminismReport(
        seed=seed,
        python_seeded=True,
        numpy_seeded=numpy_seeded,
        torch_seeded=torch_seeded,
        cuda_seeded=cuda_seeded,
        deterministic_algorithms=deterministic_algorithms,
        cublas_workspace_config=os.environ.get("CUBLAS_WORKSPACE_CONFIG", ""),
    )


def log_run_configuration(
    config: Mapping[str, Any],
    *,
    output_dir: str | Path,
    run_label: str = "run",
    extra_metadata: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Dump exact runtime configuration and metadata to a timestamped JSON file."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fingerprint = _config_fingerprint(config)

    runtime_metadata = capture_runtime_environment()

    payload: Dict[str, Any] = {
        "timestamp_utc": timestamp,
        "config_fingerprint_sha256": fingerprint,
        "run_label": run_label,
        "host": runtime_metadata,
        "config": _json_safe(dict(config)),
        "snapshot_immutable": True,
    }

    if extra_metadata:
        payload["metadata"] = _json_safe(dict(extra_metadata))

    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    file_path = output_path / f"{timestamp}_{run_label}_{fingerprint[:12]}.json"

    with file_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")

    try:
        if os.name != "nt":
            os.chmod(file_path, 0o444)
    except OSError:
        pass

    return file_path


def initialize_deterministic_run(
    config: Mapping[str, Any],
    *,
    output_dir: str | Path,
    run_label: str = "run",
) -> tuple[DeterminismReport, Path]:
    """Set seeds deterministically and record the active configuration."""
    experiment_cfg = config.get("experiment", {}) if isinstance(config, dict) else {}
    seed = int(experiment_cfg.get("seed", config.get("seed", 3407)))

    det_report = set_global_determinism(seed=seed, deterministic_algorithms=True)
    config_path = log_run_configuration(
        config,
        output_dir=output_dir,
        run_label=run_label,
        extra_metadata={
            "determinism": det_report.as_dict(),
            "config_fingerprint_sha256": _config_fingerprint(config),
            "runtime_environment": capture_runtime_environment(),
        },
    )
    return det_report, config_path

