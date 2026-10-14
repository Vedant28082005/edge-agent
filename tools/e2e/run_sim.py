"""Runs the S2c simulator (tools/sim-esp32) until a stop file appears, then exits cleanly so it prints its per-node
stats (cmds_applied, acks_sent, ...). Windows has no SIGINT for a background process, hence the stop file.
Usage: python run_sim.py STOPFILE <simesp32 args...>"""
import _thread
import os
import sys
import threading
import time

stop = sys.argv[1]
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim-esp32"))
from simesp32.cli import main  # noqa: E402


def watch():
    while not os.path.exists(stop):
        time.sleep(0.2)
    _thread.interrupt_main()


threading.Thread(target=watch, daemon=True).start()
sys.exit(main(sys.argv[2:]))
