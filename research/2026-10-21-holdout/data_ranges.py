"""Print first/last date, rows and gaps of local candle files (run inside the Freqtrade image)."""
import sys
from pathlib import Path

import pandas as pd

for path in sorted(Path(sys.argv[1]).rglob("*.feather")):
    if not any(p in path.name for p in sys.argv[2].split(",")):
        continue
    frame = pd.read_feather(path)
    if "date" not in frame:
        print(path, "no date column", list(frame.columns)); continue
    d = pd.to_datetime(frame.date, utc=True).sort_values()
    step = d.diff().mode().iloc[0] if len(d) > 1 else None
    gaps = int((d.diff() > step).sum()) if step is not None else 0
    print(f"{str(path.relative_to(sys.argv[1])):70s} rows={len(d):6d} {d.min()} .. {d.max()} step={step} gaps={gaps}")
