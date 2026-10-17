import sys, argparse
from sdmmodern.server import run
ap = argparse.ArgumentParser(description='Sim Data Manager')
ap.add_argument('--db', help='path to the database file (default: sdm.db in the program folder)')
ap.add_argument('--no-window', action='store_true', help='do not open the window; just serve')
ap.add_argument('--port', type=int, default=0)
a = ap.parse_args()
run(a.db, not a.no_window, a.port)
