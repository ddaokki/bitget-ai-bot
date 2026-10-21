"""User take-profit prices that SafeTrendLive reads from freqtrade/user_data/manual_targets.json.

The strategy exits a trade at market once the price touches its target; the exchange stop stays
in place until then. Each target names its exact trade (Freqtrade id, open time in ms, mode),
because the dry and live databases both number trades from 1.
Format: {"take_profit": {"<trade id>": {"price": p, "open_ts": ms, "dry_run": bool}}}.
"""

import json
import os
import tempfile
from pathlib import Path

PATH = Path("freqtrade/user_data/manual_targets.json")  # relative to the repo root (panel cwd)


def load(path: Path = PATH) -> dict[int, dict]:
    """Saved targets; a missing or malformed file means none (same rules as the strategy)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = data.get("take_profit") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    targets = {}
    for key, entry in raw.items():
        try:
            trade_id = int(key)
        except (TypeError, ValueError, OverflowError):
            continue
        if not isinstance(entry, dict):
            continue
        price, open_ts, dry_run = entry.get("price"), entry.get("open_ts"), entry.get("dry_run")
        try:
            valid_price = type(price) in (int, float) and 0 < float(price) < float("inf")
        except OverflowError:
            valid_price = False
        if valid_price and type(open_ts) is int and open_ts > 0 and type(dry_run) is bool:
            targets[trade_id] = {"price": float(price), "open_ts": open_ts, "dry_run": dry_run}
    return targets


def price_for(targets: dict[int, dict], trade_id: int, open_ts, dry_run: bool) -> float | None:
    entry = targets.get(trade_id)
    if entry is None or entry["open_ts"] != open_ts or entry["dry_run"] is not dry_run:
        return None
    return entry["price"]


def _save(targets: dict[int, dict], path: Path) -> None:
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
        # Readable by the bot container (uid 1000) even when the panel runs with a 077 umask.
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def set_target(trade_id: int, price: float | None, open_ts: int, dry_run: bool,
               open_trades: dict[int, int], path: Path = PATH) -> dict[int, dict]:
    """Set (or clear with None) one trade's target; drop targets of other modes and closed trades.

    open_trades maps each open trade id (current mode) to its open time in ms.
    """
    targets = {key: entry for key, entry in load(path).items()
               if entry["dry_run"] is dry_run and open_trades.get(key) == entry["open_ts"]}
    if price is None:
        targets.pop(trade_id, None)
    else:
        targets[trade_id] = {"price": float(price), "open_ts": open_ts, "dry_run": dry_run}
    _save(targets, path)
    return targets
