#!/usr/bin/env python3
"""
科研費取得合計算出ツール - バックエンドサーバー v2.0
CiNii Research API (projects検索 + KAKEN) を使用

使い方:
  python3 server.py
  ブラウザで http://localhost:8080 を開く
"""

import http.server
import json
import urllib.request
import urllib.parse
import os
import re
import sys
import time
import traceback
from http.server import HTTPServer

PORT = 8080


def log(level, msg):
    print(f"  [{level}] {msg}", file=sys.stderr, flush=True)


class KakenHandler(http.server.SimpleHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log("HTTP", fmt % args)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/search":
            self.handle_search(parsed)
        elif parsed.path in ("/", "/index.html"):
            self.serve_html()
        else:
            super().do_GET()

    def serve_html(self):
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
        except Exception as e:
            self.send_error(500, str(e))

    def handle_search(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        name = params.get("name", [""])[0].replace("\u3000", " ").strip()
        institution = params.get("institution", [""])[0].replace("\u3000", " ").strip()
        appid = params.get("appid", [""])[0].strip()

        if not name:
            self.send_json({"error": "研究者名を入力してください"}, 400)
            return

        if not appid:
            self.send_json({"error": "CiNii Application IDを入力してください（API設定から入力）"}, 400)
            return

        log("INFO", f"===== 検索開始: '{name}' (所属: '{institution}') =====")

        try:
            result = self.search_cinii_projects(name, institution, appid)
            if "error" in result:
                self.send_json(result)
                return
            log("INFO", f"===== 検索完了: {result['total_projects']}件, 合計 {result['total_amount']:,}円 =====")
            self.send_json(result)
        except Exception as e:
            log("ERROR", f"検索エラー: {e}")
            traceback.print_exc(file=sys.stderr)
            self.send_json({"error": f"検索エラー: {str(e)}"}, 500)

    # ─── CiNii Research API (projects) ───

    def search_cinii_projects(self, name, institution, appid):
        """CiNii Research APIでKAKEN課題を検索"""
        all_projects = []
        start = 1
        per_page = 50
        total = None

        for page_num in range(1, 21):  # 最大20ページ (50×20=1000件)
            # CiNii Research API URL構築
            params = {
                "q": name,
                "format": "json",
                "count": str(per_page),
                "start": str(start),
                "appid": appid,
                "dataSourceType": "KAKEN",
            }
            if institution:
                params["affiliation"] = institution

            url = "https://cir.nii.ac.jp/opensearch/projects?" + urllib.parse.urlencode(params)
            log("INFO", f"API呼び出し (ページ{page_num}): {url}")

            data = self.fetch_json(url)
            if data is None:
                if page_num == 1:
                    return {"error": "CiNii Research APIへのアクセスに失敗しました。appidを確認するか、数分待ってから再試行してください。"}
                break

            # 初回: デバッグ出力
            if page_num == 1:
                total_results = data.get("opensearch:totalResults", 0)
                log("INFO", f"検索ヒット総数: {total_results}件")
                if total_results == 0:
                    log("INFO", "0件でした。名前の表記を変えて試してみてください。")
                items = data.get("items", [])
                if items:
                    log("DEBUG", f"最初の課題サンプル: {json.dumps(items[0], ensure_ascii=False)[:300]}")

            if total is None:
                total = data.get("opensearch:totalResults", 0)

            items = data.get("items", [])
            log("INFO", f"ページ{page_num}: {len(items)}件取得 (累計 {len(all_projects) + len(items)}件)")

            for item in items:
                p = self.parse_cinii_item(item)
                if p:
                    p["_search_name"] = name  # 役割判定用に検索名を保持
                    all_projects.append(p)

            if not items or (total and len(all_projects) >= total):
                break

            start += per_page
            time.sleep(1)  # レート制限対策

        # 各課題のJSON-LDから詳細（金額）を取得
        if all_projects:
            log("INFO", f"各課題の詳細（金額情報）を取得中... ({len(all_projects)}件)")
            self.enrich_with_detail(all_projects, appid)

        return self.aggregate(name, institution, all_projects)

    def parse_cinii_item(self, item):
        """CiNii Research JSONレスポンスのitemをパース"""
        p = {
            "title": item.get("title", ""),
            "link": "",
            "project_number": "",
            "category": "",
            "period": "",
            "institution": "",
            "role": "",
            "total_amount": 0,
            "direct_cost": 0,
            "indirect_cost": 0,
        }

        # リンク
        link_obj = item.get("link", {})
        if isinstance(link_obj, dict):
            p["link"] = link_obj.get("@id", "")
        elif isinstance(link_obj, str):
            p["link"] = link_obj

        # JSON-LD詳細URL
        see_also = item.get("rdfs:seeAlso", {})
        if isinstance(see_also, dict):
            p["_detail_url"] = see_also.get("@id", "")

        # 著者
        creators = item.get("dc:creator", [])
        if isinstance(creators, list) and creators:
            p["_creators"] = creators

        # 出版日 → 研究期間
        pub_date = item.get("prism:publicationDate", "")
        if pub_date:
            p["period"] = pub_date

        # 課題番号をリンクから抽出
        if p["link"]:
            m = re.search(r"/crid/(\d+)", p["link"])
            if m:
                p["_crid"] = m.group(1)

        return p if p["title"] else None

    # ─── 詳細取得（金額情報） ───

    def enrich_with_detail(self, projects, appid):
        """各課題のJSON-LDから金額情報を取得（3並列）"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # まずURLを準備
        tasks = []
        for i, p in enumerate(projects):
            detail_url = p.pop("_detail_url", "")
            p.pop("_creators", None)
            crid = p.pop("_crid", "")

            if not detail_url:
                if crid:
                    detail_url = f"https://cir.nii.ac.jp/crid/{crid}.json"
                elif p.get("link") and "/crid/" in p["link"]:
                    detail_url = p["link"].rstrip("/") + ".json"

            if detail_url:
                sep = "&" if "?" in detail_url else "?"
                full_url = f"{detail_url}{sep}appid={appid}"
                tasks.append((i, p, full_url))

        # 3並列で取得
        done = 0
        def fetch_detail(args):
            idx, proj, url = args
            time.sleep(0.2)  # 軽いスロットリング
            return idx, proj, self.fetch_json(url)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(fetch_detail, t): t for t in tasks}
            for future in as_completed(futures):
                idx, proj, detail = future.result()
                if detail:
                    self.extract_kaken_detail(detail, proj)
                    if proj.get("total_amount", 0) > 0:
                        log("DEBUG", f"  課題{idx+1}: {proj.get('project_number','')} → {proj['total_amount']:,}円")
                done += 1
                if done % 10 == 0:
                    log("INFO", f"  詳細取得: {done}/{len(tasks)}件完了")

    def extract_kaken_detail(self, detail, p):
        """JSON-LD詳細から金額・課題情報を抽出"""

        # ── 金額: allocationAmount.totalCost.amount ──
        alloc = detail.get("allocationAmount", {})
        if alloc:
            total_cost = alloc.get("totalCost", {})
            if isinstance(total_cost, dict):
                amt_str = total_cost.get("amount", "0")
                try:
                    p["total_amount"] = int(amt_str)
                    log("DEBUG", f"  金額取得: {p['total_amount']:,}円")
                except (ValueError, TypeError):
                    pass

            # 直接経費・間接経費
            breakdown = alloc.get("breakdownCost", [])
            if isinstance(breakdown, list):
                for item in breakdown:
                    notation = item.get("notation", [])
                    label = ""
                    for n in notation:
                        if isinstance(n, dict):
                            label = n.get("@value", "")
                            break
                    amt_str = item.get("amount", "0")
                    try:
                        amt = int(amt_str)
                    except:
                        amt = 0
                    if "直接" in label or "Direct" in label:
                        p["direct_cost"] = amt
                    elif "間接" in label or "Indirect" in label:
                        p["indirect_cost"] = amt

        # ── 課題番号: projectIdentifier ──
        proj_ids = detail.get("projectIdentifier", [])
        if isinstance(proj_ids, list):
            for pid in proj_ids:
                if isinstance(pid, dict) and pid.get("@type") == "KAKEN":
                    p["project_number"] = pid.get("@value", "")
                    break

        # ── 研究種目: grant.jpcoar:fundingStream ──
        grant = detail.get("grant", {})
        if grant:
            streams = grant.get("jpcoar:fundingStream", [])
            if isinstance(streams, list):
                for s in streams:
                    if isinstance(s, dict) and s.get("@language") == "ja":
                        p["category"] = s.get("@value", "")
                        break

        # ── 研究期間: since / until ──
        since = detail.get("since", "")
        until = detail.get("until", "")
        if since:
            # "2024-04-01" → "2024"
            start_year = since[:4]
            end_year = until[:4] if until else ""
            if end_year:
                p["period"] = f"{start_year} - {end_year}"
            else:
                p["period"] = start_year

        # ── 所属機関: institution ──
        institutions = detail.get("institution", [])
        if isinstance(institutions, list) and institutions:
            inst = institutions[0]
            notations = inst.get("notation", [])
            for n in notations:
                if isinstance(n, dict) and n.get("@language") == "ja":
                    p["institution"] = n.get("@value", "")
                    break

        # ── KAKENリンク: url ──
        urls = detail.get("url", [])
        if isinstance(urls, list):
            for u in urls:
                url_id = u.get("@id", "")
                if "kaken.nii.ac.jp" in url_id:
                    p["link"] = url_id
                    break

        # ── 役割: researcher[].role ──
        # 検索した研究者名とマッチするresearcherのroleを取得
        researchers = detail.get("researcher", [])
        if isinstance(researchers, list) and "_search_name" in p:
            search_name = p.pop("_search_name")
            for r in researchers:
                names = r.get("foaf:name", [])
                for name_obj in names:
                    if isinstance(name_obj, dict):
                        name_val = name_obj.get("@value", "")
                        # スペースを除去して比較
                        if name_val.replace(" ", "").replace("　", "") == search_name.replace(" ", "").replace("　", ""):
                            role_raw = r.get("role", "")
                            if role_raw == "principal_investigator":
                                p["role"] = "代表"
                            elif role_raw == "co_investigator_buntan":
                                p["role"] = "分担"
                            elif role_raw == "co_investigator_renkei":
                                p["role"] = "連携"
                            else:
                                p["role"] = role_raw
                            break
                if p["role"]:
                    break

    # ─── ユーティリティ ───

    def fetch_json(self, url):
        """URLからJSONデータを取得"""
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Kakenhi-Aggregator/2.0 (local research tool)",
                "Accept": "application/json, */*",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                ct = resp.headers.get("Content-Type", "")
                log("DEBUG", f"HTTP {resp.status}, Content-Type: {ct}")
                data = resp.read()
                enc = "utf-8"
                if "charset=" in ct:
                    enc = ct.split("charset=")[-1].split(";")[0].strip()
                text = data.decode(enc)
                return json.loads(text)
        except urllib.error.HTTPError as e:
            log("ERROR", f"HTTP {e.code}: {e.reason}")
            if e.code == 403:
                log("ERROR", "アクセス制限されています。数分待ってから再試行してください。")
            elif e.code == 400:
                log("ERROR", "リクエストが不正です。appidを確認してください。")
            return None
        except json.JSONDecodeError as e:
            log("ERROR", f"JSONパースエラー: {e}")
            return None
        except Exception as e:
            log("ERROR", f"通信エラー: {e}")
            return None

    def aggregate(self, name, institution, projects):
        total_amount = 0
        pi_amount = 0
        pi_count = 0
        co_count = 0
        ok = ng = 0
        for p in projects:
            a = p.get("total_amount", 0) or 0
            if a > 0:
                total_amount += a
                ok += 1
                if p.get("role") == "代表":
                    pi_amount += a
            else:
                ng += 1
            if p.get("role") == "代表":
                pi_count += 1
            elif p.get("role") in ("分担", "連携"):
                co_count += 1
        return {
            "researcher_name": name,
            "institution": institution,
            "total_projects": len(projects),
            "amount_available_count": ok,
            "amount_unavailable_count": ng,
            "total_amount": total_amount,
            "pi_amount": pi_amount,
            "pi_count": pi_count,
            "co_count": co_count,
            "projects": projects,
        }

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print("=" * 60)
    print("  科研費取得合計算出ツール v2.0")
    print(f"  サーバー: http://localhost:{PORT}")
    print("  終了: Ctrl+C")
    print("=" * 60)
    print()
    print("  CiNii Research API (projects + KAKEN) を使用")
    print("  ※ CiNii Application ID が必要です")
    print()

    server = HTTPServer(("", PORT), KakenHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  サーバーを停止しました")
        server.server_close()


if __name__ == "__main__":
    main()
