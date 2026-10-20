"""List where zero-volume 1h futures candles sit (by month)."""
import sys

import pandas as pd

frame = pd.read_feather(sys.argv[1])
zero = frame.loc[frame.volume <= 0, "date"]
print(len(zero), pd.to_datetime(zero, utc=True).dt.strftime("%Y-%m").value_counts().sort_index().to_dict())
