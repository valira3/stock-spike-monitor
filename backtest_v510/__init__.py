"""v5.10.4 \u2014 Eye-of-the-Tiger full-algorithm replay.

This package is the offline counterpart to the live wiring in
trade_genius.py. It exercises every section of the v5.10.0 algorithm
(I Global Permit, II Volume Bucket + Boundary Hold, III Entry 1/2,
IV Sovereign Brake / Velocity Fuse, V Triple-Lock Phase A/B/C,
VI Machine rules) against the bar archive under /data/bars/.
"""
