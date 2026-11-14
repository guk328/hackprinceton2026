"""Run from the repository root: python -m physics.plot_normal_day.

Plotting requires physics/requirements-plot.txt; the model itself is stdlib-only.
"""

from pathlib import Path

from .model import (
    DECAY_PER_H, MAX_DEMAND_M3_H, MAX_DOSE_G_H, PUMP_FLOW_M3_H,
    TANK_CAPACITY_M3, PlantState, step,
)


def simulate_normal_day():
    """Return 24 h of states plus cumulative volume/mass balance residuals."""
    dt_s = 10.0
    dt_h = dt_s / 3600.0
    initial = PlantState()
    states = [initial]
    times_h = [0.0]
    volume_change_m3 = 0.0
    mass_change_g = 0.0
    for index in range(24 * 3600 // int(dt_s)):
        before = states[-1]
        after = step(before, dt_s=dt_s)
        volume_m3 = before.level_pct / 100 * TANK_CAPACITY_M3
        mass_g = volume_m3 * before.chlorine_mgl
        inflow_m3_h = PUMP_FLOW_M3_H if after.pump_on else 0.0
        demand_m3_h = MAX_DEMAND_M3_H * before.demand_pct / 100
        dose_g_h = (
            MAX_DOSE_G_H * before.doser_rate_pct / 100
            if after.pump_on and before.doser_on else 0.0
        )
        # This normal-day fixture never empties: all requested demand is delivered.
        volume_change_m3 += (inflow_m3_h - demand_m3_h) * dt_h
        mass_change_g += (
            dose_g_h - demand_m3_h * before.chlorine_mgl - DECAY_PER_H * mass_g
        ) * dt_h
        states.append(after)
        times_h.append((index + 1) * dt_h)

    initial_volume_m3 = initial.level_pct / 100 * TANK_CAPACITY_M3
    final_volume_m3 = states[-1].level_pct / 100 * TANK_CAPACITY_M3
    water_error_m3 = final_volume_m3 - initial_volume_m3 - volume_change_m3
    chlorine_error_g = (
        final_volume_m3 * states[-1].chlorine_mgl
        - initial_volume_m3 * initial.chlorine_mgl - mass_change_g
    )
    return times_h, states, water_error_m3, chlorine_error_g


def main():
    # Keep matplotlib optional and outside physics' public import/model path.
    import matplotlib
    matplotlib.use("Agg")  # Save a PNG without opening a GUI window.
    from matplotlib import pyplot as plt

    times_h, states, water_error_m3, chlorine_error_g = simulate_normal_day()
    levels = [state.level_pct for state in states]
    chlorine = [state.chlorine_mgl for state in states]
    pumps = [int(state.pump_on) for state in states]
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(10, 8), layout="constrained")
    fig.suptitle("Normal-day model sanity check\n40% demand · 10% dose · automatic pump control")
    axes[0].plot(times_h, levels, color="tab:blue")
    axes[0].axhline(states[0].level_low_sp, color="gray", linestyle="--", label="Low setpoint: 40%")
    axes[0].axhline(states[0].level_high_sp, color="black", linestyle="--", label="High setpoint: 85%")
    axes[0].set_ylabel("Tank level [%]")
    axes[0].legend(loc="upper right")
    axes[1].plot(times_h, chlorine, color="tab:green")
    axes[1].set_ylabel("Chlorine [mg/L]")
    # A stored pump state describes the interval that just ended, hence 'pre'.
    axes[2].step(times_h, pumps, where="pre", color="tab:orange")
    axes[2].set_ylabel("Pump state")
    axes[2].set_yticks([0, 1], labels=["OFF", "ON"])
    axes[2].set_ylim(-0.1, 1.1)
    axes[2].set_xlabel("Plant time [h]")
    for axis in axes:
        axis.set_xlim(0, 24)
        axis.grid(alpha=0.25)
    output = Path(__file__).resolve().parents[1] / "docs" / "plots" / "normal_day.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=160)
    plt.close(fig)

    print(f"Simulated 24 plant-h in {len(states) - 1} steps of 10 s")
    print(f"Level range: {min(levels):.6f}–{max(levels):.6f}%")
    print(f"Chlorine range: {min(chlorine):.6f}–{max(chlorine):.6f} mg/L")
    for index in range(1, len(states)):
        if pumps[index] != pumps[index - 1]:
            print(f"Pump {'ON' if pumps[index] else 'OFF'} at {times_h[index - 1]:.6f} plant-h")
    print(f"Water balance residual: {water_error_m3:.3e} m³")
    print(f"Chlorine mass balance residual: {chlorine_error_g:.3e} g")
    print(f"Saved {output}")


if __name__ == "__main__":
    main()
