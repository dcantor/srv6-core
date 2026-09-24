#!/usr/bin/env python3
"""The looking-glass service: collect, keep, serve.

Started by OpenRC on the lg VM (`rc-service lgd start`), reading the model that tools/lg_deploy.py renders into
/etc/lgd/lgd.json. One process: the collector threads write into SQLite, waitress serves the API and the page.
   lgd.py [/etc/lgd/lgd.json]"""
import json, logging, os, signal, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api import create_app                       # noqa: E402
from collect import Collector                    # noqa: E402
from store import Store                          # noqa: E402

CONF = Path(sys.argv[1] if len(sys.argv) > 1 else "/etc/lgd/lgd.json")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("lgd")
    cfg = json.loads(CONF.read_text())
    Path(cfg["db"]).parent.mkdir(parents=True, exist_ok=True)
    store = Store(cfg["db"], cfg.get("history_days", 30))
    collector = Collector(cfg, store, log=log.warning)
    log.info("lgd starting: %s on %s, %d reflector session(s), %d device(s), db %s",
             cfg["node"], cfg["lab"], len(cfg["collector"]["peers"]), len(cfg["devices"]), cfg["db"])
    collector.run()                               # first collection happens immediately in its own thread
    app = create_app(cfg, store, collector)

    def bye(*_):
        collector.stop.set(); log.info("lgd stopping"); os._exit(0)
    signal.signal(signal.SIGTERM, bye); signal.signal(signal.SIGINT, bye)

    from waitress import serve
    serve(app, host=cfg["listen"]["host"], port=cfg["listen"]["port"], threads=8, ident="lgd")


if __name__ == "__main__":
    main()
