"""User take-profit prices that SafeTrendLive reads from freqtrade/user_data/manual_targets.json.

The strategy exits a trade at market once the price touches its target; the exchange stop stays
in place until then. Keys are Freqtrade trade ids. Format: {"take_profit": {"<trade id>": price}}.
"""

import json
import math
import os
import tempfile
from pathlib import Path

PATH = Path("freqtrade/user_data/manual_targets.json")  # relative to the repo root (panel cwd)


def load(path: Path = PATH) -> dict[int, float]:
    """Saved targets; a missing or malformed file means none."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = data.get("take_profit") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    targets = {}
    for key, value in raw.items():
        try:
            trade_id = int(key)
        except (TypeError, ValueError):
            continue
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            targets[trade_id] = float(value)
    return targets


def _save(targets: dict[int, float], path: Path) -> None:
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump({"take_profit": {str(key): value for key, value in sorted(targets.items())}}, temporary)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def set_target(trade_id: int, price: float | None, open_ids: set[int], path: Path = PATH) -> dict[int, float]:
    """Set (or clear with None) one trade's target and drop targets of trades no longer open."""
    targets = {key: value for key, value in load(path).items() if key in open_ids}
    if price is None:
        targets.pop(trade_id, None)
    else:
        targets[trade_id] = float(price)
    _save(targets, path)
    return targets
