#!/usr/bin/env python3
"""Plot mixed-tenant thermal deficit and budget dynamics."""

from __future__ import annotations

import json
from pathlib import Path
import matplotlib.pyplot as plt


def main() -> None:
    root_dir = Path(__file__).resolve().parents[1]
    input_path = root_dir / "logs" / "l5_reports" / "l5_emulation_results.json"
    output_path = root_dir / "Images" / "l5_deficits.png"

    print(f"Loading data from {input_path}...")
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    steps: list[int] = []
    t1_def: list[float] = []
    t2_def: list[float] = []
    t3_def: list[float] = []
    budget: list[float] = []

    for row in data:
        steps.append(row["step"])
        budget.append(row["thermal_budget"])
        t1_def.append(row["accumulated_deficits"]["t1_sensor_fusion"])
        t2_def.append(row["accumulated_deficits"]["t2_conversational"])
        t3_def.append(row["accumulated_deficits"]["t3_background_logs"])

    print("Plotting...")
    fig, ax1 = plt.subplots(figsize=(10, 5))

    color = "tab:red"
    ax1.set_xlabel("Time Steps (s)")
    ax1.set_ylabel("Accumulated Deficit (s)", color=color)
    ax1.plot(steps, t1_def, label="T1 (Sensor Fusion)", color="tab:red", linewidth=2)
    ax1.plot(steps, t2_def, label="T2 (Conversational)", color="tab:orange", linewidth=2, linestyle="--")
    ax1.plot(steps, t3_def, label="T3 (Background Logs)", color="tab:brown", linewidth=2, linestyle=":")
    ax1.tick_params(axis="y", labelcolor=color)
    ax1.legend(loc="upper left")

    ax2 = ax1.twinx()
    color = "tab:blue"
    ax2.set_ylabel("Thermal Budget", color=color)
    ax2.plot(steps, budget, label="Thermal Budget", color=color, linewidth=2, alpha=0.5)
    ax2.tick_params(axis="y", labelcolor=color)
    ax2.set_ylim(0, 1.1)
    ax2.legend(loc="upper right")

    fig.tight_layout()
    plt.title("Mixed-Tenant Thermal Deficit and Budget over Time")
    plt.grid(True, alpha=0.3)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=300)
    print(f"Saved {output_path}")


if __name__ == "__main__":
    main()
