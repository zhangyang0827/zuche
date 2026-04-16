#!/usr/bin/env python3
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

HOST = "127.0.0.1"
PORT = 8787
TARGET_URL = "http://ibd-travel.17u.cn/gateway/mdscr-order/manage/order/detail/"
CAR_SPU_LIST_URL_DEFAULT = (
    "http://ibd-travel.17u.cn/gateway/mdscr-vehicle/manage/channel/carSpu/list"
)
CALENDAR_PRICES_URL_DEFAULT = (
    "http://mdscr.travel.17usoft.com/revenueapi/dispatch/price/loadChannelAllCalendarPrices"
)
# 门店 storeCode + hatchbackCode（billingSpuId）→ queryBatchChannelPrice（已注释）
# CHANNEL_PRICE_URL_DEFAULT = (
#     "http://mdscr.travel.17usoft.com/revenueapi/dispatch/channelPriceLimit/queryBatchChannelPrice"
# )
ROOT = Path(__file__).resolve().parent

# 尽量贴近真实浏览器，减少网关/WAF 因缺头返回 403
UA_CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def origin_and_referer(target_url: str) -> tuple[str, str]:
    p = urlparse(target_url)
    origin = urlunparse((p.scheme, p.netloc, "", "", "", ""))
    referer = urlunparse((p.scheme, p.netloc, "/", "", "", ""))
    return origin, referer


def normalize_target_url(target_url: str) -> str:
    # 很多 Java/Spring 路由对尾斜杠敏感，统一去掉 path 末尾 /
    p = urlparse(target_url)
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc, path, p.params, p.query, p.fragment))


def extract_model_id_for_spu(obj, billing_spu_id: str):
    """从 carSpu/list 类响应中解析与 billingSpuId 对应的 modelId。"""
    bid = str(billing_spu_id).strip()
    if not bid:
        return None

    def match_spu(d: dict) -> bool:
        if not isinstance(d, dict):
            return False
        for k in ("spuId", "billingSpuId", "id", "carSpuId"):
            v = d.get(k)
            if v is not None and str(v).strip() == bid:
                return True
        return False

    def walk(o):
        if isinstance(o, dict):
            if match_spu(o) and o.get("modelId") is not None:
                return o.get("modelId")
            for v in o.values():
                r = walk(v)
                if r is not None:
                    return r
        elif isinstance(o, list):
            for it in o:
                r = walk(it)
                if r is not None:
                    return r
        return None

    found = walk(obj)
    if found is not None:
        return found
    # 仅一条记录且未带 spuId 等字段时的兜底
    if isinstance(obj, dict):
        data = obj.get("data")
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            mid = data[0].get("modelId")
            if mid is not None:
                return mid
    return None


def build_get_url(target_url: str, order_no: str) -> str:
    p = urlparse(target_url)
    if "{orderNo}" in p.path:
        path = p.path.replace("{orderNo}", quote(order_no, safe=""))
    elif p.path.rstrip("/").endswith(order_no):
        path = p.path
    else:
        path = p.path.rstrip("/") + "/" + quote(order_no, safe="")
    return urlunparse((p.scheme, p.netloc, path, p.params, p.query, p.fragment))


def json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


class ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/", "/index.html"):
            self.send_error(404, "Not Found")
            return
        file_path = ROOT / "index.html"
        if not file_path.is_file():
            self.send_error(500, "index.html missing")
            return
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()

    def do_POST(self):
        if self.path not in (
            "/api/order-detail",
            "/api/car-spu-list",
            "/api/load-channel-calendar-prices",
        ):
            return json_response(self, 404, {"success": False, "message": "Not Found"})

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return json_response(self, 400, {"success": False, "message": "请求体必须是 JSON"})

        if self.path == "/api/car-spu-list":
            return self._handle_car_spu_list(payload)
        if self.path == "/api/load-channel-calendar-prices":
            return self._handle_calendar_prices(payload)
        return self._handle_order_detail(payload)

    def _handle_order_detail(self, payload: dict):
        cookie = (payload.get("cookie") or "").strip()
        order_no = (payload.get("orderNo") or "").strip()
        target_url = normalize_target_url((payload.get("targetUrl") or TARGET_URL).strip())
        request_method = (payload.get("requestMethod") or "GET").strip().upper()
        extra = payload.get("extraHeaders")
        if extra is not None and not isinstance(extra, dict):
            return json_response(self, 400, {"success": False, "message": "extraHeaders 必须是对象（键值均为字符串）"})
        if request_method not in ("GET", "POST"):
            return json_response(self, 400, {"success": False, "message": "requestMethod 仅支持 GET/POST"})

        if not cookie or not order_no:
            return json_response(self, 400, {"success": False, "message": "cookie 和 orderNo 不能为空"})

        origin, referer = origin_and_referer(target_url)
        upstream_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie,
            "User-Agent": UA_CHROME,
            "Origin": origin,
            "Referer": referer,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k is None or v is None:
                    continue
                key = str(k).strip()
                if not key:
                    continue
                upstream_headers[key] = str(v)

        resolved_target_url = target_url
        upstream_payload = None
        if request_method == "GET":
            resolved_target_url = build_get_url(target_url, order_no)
        else:
            upstream_payload = json.dumps({"orderNo": order_no}).encode("utf-8")
        req = Request(
            resolved_target_url,
            data=upstream_payload,
            headers=upstream_headers,
            method=request_method,
        )

        try:
            with urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                status = resp.getcode()
        except HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            status = e.code
        except URLError as e:
            return json_response(
                self, 502, {"success": False, "message": f"上游请求失败: {e.reason}", "data": None}
            )
        except Exception as e:
            return json_response(
                self, 500, {"success": False, "message": f"代理异常: {str(e)}", "data": None}
            )

        try:
            upstream_body = json.loads(text)
        except json.JSONDecodeError:
            upstream_body = {"nonJsonResponse": text}

        return json_response(
            self,
            200,
            {
                "success": True,
                "message": "ok",
                "resolvedTargetUrl": resolved_target_url,
                "resolvedMethod": request_method,
                "upstreamStatus": status,
                "upstreamBody": upstream_body,
            },
        )

    def _handle_car_spu_list(self, payload: dict):
        cookie = (payload.get("cookie") or "").strip()
        billing_spu_id = (payload.get("billingSpuId") or "").strip()
        target_url = normalize_target_url((payload.get("targetUrl") or CAR_SPU_LIST_URL_DEFAULT).strip())
        extra = payload.get("extraHeaders")
        list_body = payload.get("listBody")

        if extra is not None and not isinstance(extra, dict):
            return json_response(self, 400, {"success": False, "message": "extraHeaders 必须是对象（键值均为字符串）"})
        if list_body is not None and not isinstance(list_body, dict):
            return json_response(self, 400, {"success": False, "message": "listBody 必须是对象"})
        if not cookie:
            return json_response(self, 400, {"success": False, "message": "cookie 不能为空"})
        if not billing_spu_id:
            return json_response(self, 400, {"success": False, "message": "billingSpuId 不能为空"})

        default_car_spu_body = {
            "pageNum": 1,
            "pageSize": 10,
            "carSpuId": billing_spu_id,
            "brandId": "",
            "seriesId": "",
            "traceId": str(uuid.uuid4()),
        }
        if isinstance(list_body, dict) and list_body:
            body_obj = {**default_car_spu_body, **list_body}
        else:
            body_obj = dict(default_car_spu_body)
        if not str(body_obj.get("carSpuId") or "").strip():
            body_obj["carSpuId"] = billing_spu_id
        if not str(body_obj.get("traceId") or "").strip():
            body_obj["traceId"] = str(uuid.uuid4())

        origin, referer = origin_and_referer(target_url)
        upstream_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie,
            "User-Agent": UA_CHROME,
            "Origin": origin,
            "Referer": referer,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k is None or v is None:
                    continue
                key = str(k).strip()
                if not key:
                    continue
                upstream_headers[key] = str(v)

        upstream_payload = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        req = Request(target_url, data=upstream_payload, headers=upstream_headers, method="POST")

        try:
            with urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                status = resp.getcode()
        except HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            status = e.code
        except URLError as e:
            return json_response(
                self, 502, {"success": False, "message": f"上游请求失败: {e.reason}", "data": None}
            )
        except Exception as e:
            return json_response(
                self, 500, {"success": False, "message": f"代理异常: {str(e)}", "data": None}
            )

        try:
            upstream_body = json.loads(text)
        except json.JSONDecodeError:
            upstream_body = {"nonJsonResponse": text}

        model_id = extract_model_id_for_spu(upstream_body, billing_spu_id)

        return json_response(
            self,
            200,
            {
                "success": True,
                "message": "ok",
                "resolvedTargetUrl": target_url,
                "resolvedMethod": "POST",
                "requestBody": body_obj,
                "upstreamStatus": status,
                "upstreamBody": upstream_body,
                "extractedModelId": model_id,
            },
        )

    def _handle_calendar_prices(self, payload: dict):
        cookie = (payload.get("cookie") or "").strip()
        start_date = (payload.get("startDate") or "").strip()
        end_date = (payload.get("endDate") or "").strip()
        car_model_id = payload.get("carModelId")
        store_code = (payload.get("storeCode") or "").strip()
        target_url = normalize_target_url((payload.get("targetUrl") or CALENDAR_PRICES_URL_DEFAULT).strip())
        extra = payload.get("extraHeaders")

        if extra is not None and not isinstance(extra, dict):
            return json_response(self, 400, {"success": False, "message": "extraHeaders 必须是对象（键值均为字符串）"})
        if not cookie:
            return json_response(self, 400, {"success": False, "message": "cookie 不能为空"})
        if not start_date or not end_date:
            return json_response(self, 400, {"success": False, "message": "startDate 和 endDate 不能为空"})
        if car_model_id is None or str(car_model_id).strip() == "":
            return json_response(self, 400, {"success": False, "message": "carModelId 不能为空"})
        if not store_code:
            return json_response(self, 400, {"success": False, "message": "storeCode 不能为空"})

        body_obj = {
            "startDate": start_date,
            "endDate": end_date,
            "carModelId": str(car_model_id).strip(),
            "storeCode": store_code,
            "licenseType": 0,
        }

        origin, referer = origin_and_referer(target_url)
        upstream_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie,
            "User-Agent": UA_CHROME,
            "Origin": origin,
            "Referer": referer,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k is None or v is None:
                    continue
                key = str(k).strip()
                if not key:
                    continue
                upstream_headers[key] = str(v)

        upstream_payload = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        req = Request(target_url, data=upstream_payload, headers=upstream_headers, method="POST")

        try:
            with urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                status = resp.getcode()
        except HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            status = e.code
        except URLError as e:
            return json_response(
                self, 502, {"success": False, "message": f"上游请求失败: {e.reason}", "data": None}
            )
        except Exception as e:
            return json_response(
                self, 500, {"success": False, "message": f"代理异常: {str(e)}", "data": None}
            )

        try:
            upstream_body = json.loads(text)
        except json.JSONDecodeError:
            upstream_body = {"nonJsonResponse": text}

        return json_response(
            self,
            200,
            {
                "success": True,
                "message": "ok",
                "resolvedTargetUrl": target_url,
                "resolvedMethod": "POST",
                "requestBody": body_obj,
                "upstreamStatus": status,
                "upstreamBody": upstream_body,
            },
        )

    # def _handle_channel_price_limit(self, payload: dict):
    #     """POST queryBatchChannelPrice，入参 JSON：{storeCode, hatchbackCode}。已整段注释。"""
    #     cookie = (payload.get("cookie") or "").strip()
    #     store_code = (payload.get("storeCode") or "").strip()
    #     hatchback_code = (payload.get("hatchbackCode") or "").strip()
    #     target_url = normalize_target_url((payload.get("targetUrl") or CHANNEL_PRICE_URL_DEFAULT).strip())
    #     extra = payload.get("extraHeaders")
    #     if extra is not None and not isinstance(extra, dict):
    #         return json_response(self, 400, {"success": False, "message": "extraHeaders 必须是对象（键值均为字符串）"})
    #
    #     if not cookie:
    #         return json_response(self, 400, {"success": False, "message": "cookie 不能为空"})
    #     if not store_code or not hatchback_code:
    #         return json_response(self, 400, {"success": False, "message": "storeCode 和 hatchbackCode 不能为空"})
    #
    #     origin, referer = origin_and_referer(target_url)
    #     upstream_headers = {
    #         "Content-Type": "application/json",
    #         "Accept": "application/json, text/plain, */*",
    #         "Cookie": cookie,
    #         "User-Agent": UA_CHROME,
    #         "Origin": origin,
    #         "Referer": referer,
    #         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    #     }
    #     if isinstance(extra, dict):
    #         for k, v in extra.items():
    #             if k is None or v is None:
    #                 continue
    #             key = str(k).strip()
    #             if not key:
    #                 continue
    #             upstream_headers[key] = str(v)
    #
    #     body_obj = {"storeCode": store_code, "hatchbackCode": hatchback_code}
    #     upstream_payload = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    #     req = Request(target_url, data=upstream_payload, headers=upstream_headers, method="POST")
    #
    #     try:
    #         with urlopen(req, timeout=15) as resp:
    #             text = resp.read().decode("utf-8", errors="replace")
    #             status = resp.getcode()
    #     except HTTPError as e:
    #         text = e.read().decode("utf-8", errors="replace")
    #         status = e.code
    #     except URLError as e:
    #         return json_response(
    #             self, 502, {"success": False, "message": f"上游请求失败: {e.reason}", "data": None}
    #         )
    #     except Exception as e:
    #         return json_response(
    #             self, 500, {"success": False, "message": f"代理异常: {str(e)}", "data": None}
    #         )
    #
    #     try:
    #         upstream_body = json.loads(text)
    #     except json.JSONDecodeError:
    #         upstream_body = {"nonJsonResponse": text}
    #
    #     return json_response(
    #         self,
    #         200,
    #         {
    #             "success": True,
    #             "message": "ok",
    #             "resolvedTargetUrl": target_url,
    #             "resolvedMethod": "POST",
    #             "requestBody": body_obj,
    #             "upstreamStatus": status,
    #             "upstreamBody": upstream_body,
    #         },
    #     )

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    print(f"Proxy server listening at http://{HOST}:{PORT}")
    print("GET  /           (页面)")
    print("POST /api/order-detail")
    print("POST /api/car-spu-list")
    print("POST /api/load-channel-calendar-prices")
    # print("POST /api/channel-price-limit")
    server.serve_forever()
