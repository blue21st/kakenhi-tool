#!/usr/bin/env python3
import http.server
import json
import urllib.request
import urllib.parse
import os
import sys
from http.server import HTTPServer

PORT = int(os.environ.get("PORT", 8080))
CINII_APPID = os.environ.get("CINII_APPID", "").strip()

class WebHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args, file=sys.stderr, flush=True)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/":
            self.serve_html()
        elif parsed.path == "/api/search":
            self.handle_search(parsed)
        else:
            self.send_error(404)

    def serve_html(self):
        with open("index_web.html", encoding="utf-8") as f:
            html = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def handle_search(self, parsed):
        if not CINII_APPID:
            self.send_json({"error": "CINII_APPID is not set"}, 500)
            return

        params = urllib.parse.parse_qs(parsed.query)
        name = params.get("name", [""])[0].strip()
        institution = params.get("institution", [""])[0].strip()

        if not name:
            self.send_json({"error": "研究者名を入力してください"}, 400)
            return

        result = self.search_cinii(name)
        self.send_json(result)

    def search_cinii(self, name):
        url = (
            "https://cir.nii.ac.jp/opensearch/projects?"
            + urllib.parse.urlencode({
                "q": name,
                "format": "json",
                "count": 50,
                "appid": CINII_APPID,
                "dataSourceType": "KAKEN"
            })
        )

        data = json.loads(
            urllib.request.urlopen(url).read().decode("utf-8")
        )

        projects = []
        for item in data.get("items", []):
            projects.append({
                "title": item.get("title", ""),
                "link": item.get("link", {}).get("@id", ""),
                "total_amount": 0
            })

        return {
            "researcher_name": name,
            "total_projects": len(projects),
            "total_amount": 0,
            "projects": projects
        }

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

if __name__ == "__main__":
    print(f"WEB SERVER STARTED ON PORT {PORT}", flush=True)
    HTTPServer(("", PORT), WebHandler).serve_forever()
``
