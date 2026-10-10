# pacat stand-in: consumes stdin in real time (44.1 kHz s16 mono) in 20 ms pieces, logs (t, mean |x|) per piece
import sys, time, os
import numpy as np
log = open(sys.argv[1], "a", buffering=1)
fd = sys.stdin.fileno(); BLK = 1764  # 20 ms
t0 = None; played = 0; buf = b""
while True:
    d = os.read(fd, BLK - len(buf))
    if not d:
        break
    buf += d
    if len(buf) < BLK:
        continue
    if t0 is None: t0 = time.time()
    x = np.frombuffer(buf, dtype=np.int16).astype(np.int32)
    played += BLK
    log.write(f"{time.time():.3f} {int(np.abs(x).mean())}\n")
    buf = b""
    lag = t0 + played / 88200 - time.time()
    if lag > 0: time.sleep(lag)
log.write("EOF\n")
