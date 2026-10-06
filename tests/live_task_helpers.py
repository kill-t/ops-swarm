"""现场任务测试用的受限真实适配器与固定 HTTP 响应。"""

from datetime import UTC, datetime, timedelta
from threading import Barrier, get_ident

import httpx2

from opsswarm.investigation.changes import ChangeApiOnlyInvestigationDataSource
from opsswarm.investigation.live import LiveInvestigationDataSource
from opsswarm.investigation.loki import LokiOnlyInvestigationDataSource
from opsswarm.investigation.prometheus import PrometheusOnlyInvestigationDataSource
from tests.unit.investigation import test_changes as change
from tests.unit.investigation import test_loki as loki
from tests.unit.investigation import test_prometheus as prom


class LiveHttpFixture:
    def __init__(
        self, cutoff, clock, *, concurrent=False, failure=None, mcp_logs=None, mcp_metrics=None
    ):
        self.requests = []
        self.thread_ids = set()
        self.failure = failure
        self.barrier = Barrier(3) if concurrent else None
        self.entered = set()
        self.cutoff, self.clock = cutoff, clock
        logs = LokiOnlyInvestigationDataSource(
            transport=httpx2.MockTransport(self.logs), clock=lambda: clock().timestamp()
        )
        metrics = PrometheusOnlyInvestigationDataSource(
            transport=httpx2.MockTransport(self.metrics), clock=lambda: clock().timestamp()
        )
        self.source = LiveInvestigationDataSource(
            logs=logs,
            # 既有端到端测试在进程内验证任务恢复；第 101 步另以真实 MCP 进程覆盖传输。
            mcp_logs=mcp_logs if mcp_logs is not None else logs,
            metrics=metrics,
            mcp_metrics=mcp_metrics if mcp_metrics is not None else metrics,
            changes=ChangeApiOnlyInvestigationDataSource(
                transport=httpx2.MockTransport(self.changes),
                runtime_transport=httpx2.MockTransport(self.no_runtime),
                clock=lambda: clock().timestamp(),
            ),
        )

    def begin(self, kind, request):
        self.requests.append((kind, request))
        self.thread_ids.add(get_ident())
        if self.barrier is not None and kind not in self.entered:
            self.entered.add(kind)
            self.barrier.wait(timeout=10)
        if self.failure == kind:
            raise httpx2.ConnectError("测试数据源不可用", request=request)

    def logs(self, request):
        self.begin("log", request)
        observed = self.cutoff - timedelta(seconds=1)
        transport, _ = loki._transport(
            streams=[
                {
                    "stream": {"service": "order-service", "source": "fault-lab"},
                    "values": [
                        [
                            str(int(observed.timestamp()) * 10**9),
                            loki._line("redis_unavailable", timestamp=observed.isoformat()),
                        ]
                    ],
                }
            ]
        )
        return transport.handle_request(request)

    def metrics(self, request):
        self.begin("metric", request)
        transport, _ = prom._transport()
        payload = transport.handle_request(request).json()
        for series in payload["data"]["result"]:
            for value in series.get("values", [series.get("value")]):
                value[0] += int(self.cutoff.timestamp()) - prom.NOW
        return httpx2.Response(200, json=payload)

    def changes(self, request):
        self.begin("change", request)
        body = change._response(generated_at=self.clock()).model_dump(mode="json")
        shift = self.cutoff - change.GENERATED_AT
        for item in body["items"]:
            for key in ("occurred_at", "recorded_at"):
                item[key] = (datetime.fromisoformat(item[key]) + shift).astimezone(UTC).isoformat()
        body["window"] = {
            "window_start": (self.cutoff - timedelta(hours=6)).isoformat(),
            "window_end": self.cutoff.isoformat(),
        }
        return httpx2.Response(200, json=body)

    @staticmethod
    def no_runtime(request):
        raise AssertionError("固定历史窗口不得读取当前运行清单")

    def assert_windows(self):
        for kind, request in self.requests:
            params = request.url.params
            if kind == "log":
                assert (
                    int(params["start"])
                    == int((self.cutoff - timedelta(minutes=15)).timestamp()) * 10**9
                )
                assert int(params["end"]) == int(self.cutoff.timestamp()) * 10**9 + 1
            elif kind == "metric":
                if request.url.path.endswith("query_range"):
                    assert int(params["start"]) == int(self.cutoff.timestamp()) - 900
                    assert int(params["end"]) == int(self.cutoff.timestamp())
                else:
                    assert int(params["time"]) == int(self.cutoff.timestamp())
            else:
                assert request.url.path == "/api/v1/changes/window"
                assert datetime.fromisoformat(params["window_start"]) == self.cutoff - timedelta(
                    hours=6
                )
                assert datetime.fromisoformat(params["window_end"]) == self.cutoff
