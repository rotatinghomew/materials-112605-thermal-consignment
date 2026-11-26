"""寄售履约查询服务：向采购、库管和出账系统暴露片级处理结果。

只读本机交换资料，复用 ``consignment`` 领域逻辑。监听回环地址，
与 ``partner.py`` 的对接方回执回放配套联调。
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from partner import parse_time, DATA_PATH
from consignment import ConsignmentService, ExchangeData, to_jsonable, FAR_FUTURE


def make_handler(data: ExchangeData):
    class ConsignmentHandler(BaseHTTPRequestHandler):
        def reply(self, status, payload):
            body = json.dumps(to_jsonable(payload), ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            target = urlsplit(self.path)
            query = parse_qs(target.query)
            at_values = query.get("at", [])
            as_of = FAR_FUTURE
            if at_values:
                try:
                    as_of = parse_time(at_values[0])
                except ValueError:
                    self.reply(400, {"message": "at 须为带时区偏移的时间"})
                    return
            try:
                svc = ConsignmentService(data, as_of=as_of)
                if target.path == "/health":
                    self.reply(200, {"status": "ok"})
                elif target.path == "/v1/warehouse":
                    self.reply(200, {"pieces": svc.warehouse_view()})
                elif target.path == "/v1/procurement":
                    self.reply(200, {"pieces": to_jsonable(svc.procurement_view())})
                elif target.path == "/v1/invoices":
                    self.reply(200, {"invoices": svc.invoices_view(),
                                     "arrears": svc.billing.arrears()})
                elif target.path == "/v1/claims":
                    self.reply(200, {"claims": svc.claims_view()})
                elif target.path == "/v1/scope":
                    self.reply(200, to_jsonable(svc.scope_report()))
                else:
                    self.reply(404, {"message": "路径不存在"})
            except ValueError as exc:
                self.reply(422, {"message": f"交换资料处理失败: {exc}"})

        def do_POST(self):
            target = urlsplit(self.path)
            if target.path != "/v1/recovered-usage":
                self.reply(404, {"message": "路径不存在"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or not isinstance(
                        payload.get("usages"), list):
                    raise ValueError
                now = parse_time(payload["at"])
            except (ValueError, UnicodeError, KeyError):
                self.reply(400, {"message": "请求须含 at 与 usages 数组"})
                return
            try:
                svc = ConsignmentService(data, as_of=now)
                result = svc.apply_recovered_usage(payload["usages"], now)
                self.reply(202, result)
            except ValueError as exc:
                self.reply(422, {"message": str(exc)})

        def log_message(self, format, *args):
            pass

    return ConsignmentHandler


def main():
    parser = argparse.ArgumentParser(description="导热片寄售履约查询服务")
    parser.add_argument("--port", type=int, default=8088,
                        help="回环地址端口，0 表示自动分配")
    parser.add_argument("--data", type=Path, default=DATA_PATH,
                        help="交换资料 JSON 路径")
    args = parser.parse_args()
    data = ExchangeData.load(args.data)
    with ThreadingHTTPServer(("127.0.0.1", args.port),
                            make_handler(data)) as server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
