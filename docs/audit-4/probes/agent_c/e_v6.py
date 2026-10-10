import socket, threading
for bind in [("127.0.0.1", socket.AF_INET), ("::", socket.AF_INET6)]:
    s = socket.socket(bind[1]); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind[0], 0)); s.listen(); port = s.getsockname()[1]
    for target in ["::7f00:1", "::ffff:0:7f00:1", "64:ff9b::7f00:1", "::ffff:127.0.0.1"]:
        c = socket.socket(socket.AF_INET6); c.settimeout(1)
        try:
            c.connect((target, port)); r = "CONNECTED"
        except Exception as e:
            r = repr(e)[:60]
        c.close()
        print(f"listener {bind[0]:10s} -> {target:18s}: {r}")
    s.close()
