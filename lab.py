#!/usr/bin/env python3
"""
Apre Options Lab nel browser con tutti i dati salvati in options.db.

  python3 lab.py            # avvia e apre il sito
  python3 lab.py --port 8800

Lascia il terminale aperto mentre usi il sito; Ctrl+C per chiudere.
Dopo un nuovo scarico con ibkr_chain_api.py premi "Aggiorna dati" nel sito.
"""
import argparse
import functools
import http.server
import os
import socketserver
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))


class NoCache(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):  # niente log a ogni richiesta
        pass


def main():
    ap = argparse.ArgumentParser(description="Apre Options Lab con options.db")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default="options.db", help="database da aprire (nella cartella dello script)")
    a = ap.parse_args()
    if not os.path.exists(os.path.join(HERE, "options_lab.html")):
        raise SystemExit("options_lab.html non trovato accanto a lab.py")
    if not os.path.exists(os.path.join(HERE, a.db)):
        print(f"Attenzione: {a.db} non esiste ancora: il sito partirà con i dati incorporati.")
    handler = functools.partial(NoCache, directory=HERE)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", a.port), handler) as srv:
        url = f"http://localhost:{a.port}/options_lab.html?db={a.db}"
        print(f"Options Lab: {url}\nCtrl+C per chiudere.")
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nChiuso.")


if __name__ == "__main__":
    main()
