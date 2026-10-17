"""
MCP 工具：经 HTTP 调 ccp-backend 的 REST（/api/v1/*）。

每条 tools/call 一换一铸 session（POST /internal/sessions，5 分钟时效），以令牌属主
身份调平台 API——权限 / 可见范围 / 计费拦截与控制台同一套。返回「给 AI 看的文本」：
列宽对齐、单位带上、数字带上下文，不是给前端的结构体。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.main import ToolError

# ------------------------------------------------------------------ 工具表


TOOLS: list[dict[str, Any]] = [
    {
        "name": "cluster_overview",
        "description": "集群资源总览：节点数、CPU/内存/GPU 三池的总量与已分配，算力紧张与否一眼看到。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_queues",
        "description": "队列（分区）清单：每条队列的算力类型、GPU 型号、总量与已用（核/卡）。提交作业前用它挑队列。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_images",
        "description": "平台可用的容器镜像（提交作业 / 起实例要选镜像），含名称与标签列表。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_job",
        "description": (
            "提交一个计算作业（Volcano Job）。command 用 shell 写法填一行（如 "
            "'python train.py --epochs 10'）。多机多卡：replicas=机器数并给 gpu=每机卡数，"
            "需要任务发现时选 env=true。提交成功返回 namespace 与作业名。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "作业名（小写字母数字连字符）"},
                "project": {"type": "string", "description": "所属项目短名（必填，先 list_projects 拿）"},
                "queue": {"type": "string", "description": "队列/分区名（list_queues 里挑）"},
                "image": {"type": "string", "description": "容器镜像（list_images 里挑）"},
                "command": {"type": "string", "description": "要跑的命令（一行 shell 写法）"},
                "cpu": {"type": "string", "description": "每任务 CPU 核数，如 '8'", "default": "1"},
                "memory": {"type": "string", "description": "每任务内存，如 '16Gi'"},
                "gpu": {"type": "integer", "description": "每任务 GPU 卡数（不要卡就不填）"},
                "replicas": {"type": "integer", "description": "任务副本数（多机多卡=机器数）", "default": 1},
                "nodes": {"type": "integer", "description": "Slurm --nodes：>1 时 Pod 尽量分散到多台节点"},
                "env": {"type": "boolean", "description": "注入 VC_<task>_<i>_HOST 等任务发现变量（多机多卡开）", "default": False},
                "ssh": {"type": "boolean", "description": "开启 SSH 调试插件", "default": False},
            },
            "required": ["name", "project", "queue", "image", "command", "memory"],
        },
    },
    {
        "name": "list_projects",
        "description": "我能提交作业的项目清单（提交作业必填所属项目）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_jobs",
        "description": "我的近期作业：状态（Pending/Running/…）、队列、资源、提交时间。可按状态过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "phase": {"type": "string", "enum": ["Pending", "Running", "Succeeded", "Failed", "Terminated", "Unknown"]},
                "limit": {"type": "integer", "description": "最多返回几条（默认 20）", "default": 20},
            },
        },
    },
    {
        "name": "get_job",
        "description": "一个作业的详情：状态、每个 task 的资源、Pod 列表与事件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "作业名"},
                "namespace": {"type": "string", "description": "namespace（不填按我的租户算）"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "delete_job",
        "description": "删掉一个作业（进终态的照删，没跑完的按 Terminated 归档）。只能删自己提的（管理员不限）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "namespace": {"type": "string", "description": "namespace（不填按我的租户算）"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_gpu_instances",
        "description": "我的开发环境实例（GPU 实例 / 通用实例）：状态、规格、卡型、SSH 可达性。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "my_bill",
        "description": "我的月账单：算力 / Token / 存储三类费用合计与明细，预付余额和授信额度。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "账单月份，YYYY-MM（不填 = 当月）"},
            },
        },
    },
    {
        "name": "my_cost_trend",
        "description": "近 N 天每日费用趋势（算力 / Token / 存储），看钱花在哪天、哪类。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "回看天数（1–180，默认 30）", "default": 30},
            },
        },
    },
]


# ------------------------------------------------------------------ 平台 API 客户端


class Platform:
    """一条工具调用一枚 session，以令牌属主身份调 /api/v1/*。"""

    def __init__(self, settings, user: str) -> None:
        self._cfg = settings
        self._user = user
        self._token: str | None = None

    async def _mint(self) -> str:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                f"{self._cfg.backend.rstrip('/')}/internal/sessions",
                json={"user": self._user},
                headers={"X-CCP-Internal-Key": self._cfg.internal_key},
            )
            if resp.status_code != 200:
                raise ToolError(f"平台认证失败（{resp.status_code}），稍后再试")
            return resp.json()["data"]["token"]

    async def call(self, method: str, path: str, json_body: dict | None = None, params: dict | None = None) -> Any:
        """调 /api/v1/*；401（session 过期）自动重铸一次。"""
        if self._token is None:
            self._token = await self._mint()
        url = f"{self._cfg.backend.rstrip('/')}/api/v1{path}"
        headers = {"Authorization": f"Bearer {self._token}"}
        for attempt in (1, 2):
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                resp = await client.request(method, url, json=json_body, params=params, headers=headers)
            if resp.status_code == 401 and attempt == 1:
                self._token = await self._mint()
                headers = {"Authorization": f"Bearer {self._token}"}
                continue
            if resp.status_code >= 400:
                raise ToolError(_error_message(resp))
            data = resp.json()
            if isinstance(data, dict) and data.get("success") is False:
                # 业务错误：HTTP 200 但信封 success=false（ccp 的惯例）
                raise ToolError(_error_message(resp))
            return data.get("data") if isinstance(data, dict) and "data" in data else data
        raise ToolError("平台调用失败：重试后仍 401")

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self.call("GET", path, params=params)

    async def post(self, path: str, body: dict) -> Any:
        return await self.call("POST", path, json_body=body)

    async def delete(self, path: str) -> Any:
        return await self.call("DELETE", path)


def _error_message(resp: httpx.Response) -> str:
    """把平台业务错误（success=false 信封）翻成给 AI 看的话。"""
    try:
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            msg = payload.get("errorMessage") or payload.get("errorCode") or "请求失败"
            return f"{msg}（{payload.get('errorCode', resp.status_code)}）"
    except Exception:  # noqa: BLE001
        pass
    return f"平台请求失败（HTTP {resp.status_code}）"


# ------------------------------------------------------------------ 工具实现


async def run_tool(name: str, args: dict[str, Any], user: str, settings) -> str:
    """按工具名分发。KeyError = 未知工具（-32602）；ToolError = 业务错（isError）。"""
    if name not in _IMPL:
        raise KeyError(name)
    platform = Platform(settings, user)
    return await _IMPL[name](args, platform)


async def t_cluster_overview(args: dict, p: Platform) -> str:
    data = await p.get("/overview")
    nodes, cpu, mem, gpu = data["nodes"], data["cpu"], data["gpu"], data["memory"]
    lines = [
        f"集群总览：节点 {nodes['ready']}/{nodes['total']} 台就绪",
        f"- CPU：{cpu['used']:.0f}/{cpu['total']:.0f} 核",
        f"- 内存：{mem['used']:.0f}/{mem['total']:.0f} Gi",
        f"- GPU：{gpu['used']:.0f}/{gpu['total']:.0f} 卡",
    ]
    jobs = ", ".join(f"{k} {v}" for k, v in (data.get("jobs") or {}).items() if v)
    if jobs:
        lines.append(f"作业：{jobs}")
    return "\n".join(lines)


async def t_list_queues(args: dict, p: Platform) -> str:
    data = await p.get("/queues")
    queues = data["list"] if isinstance(data, dict) else data
    if not queues:
        return "（集群里还没有队列）"
    lines = ["队列（提交作业时选其中一条）："]
    for q in queues:
        models = " / ".join(q.get("gpuModels") or []) or "纯 CPU"
        capability = q.get("capability") or {}
        allocated = q.get("allocated") or {}
        gpu_cap = capability.get("nvidia.com/gpu")
        gpu = f"，卡 {allocated.get('nvidia.com/gpu', '0')}/{gpu_cap}" if gpu_cap else ""
        lines.append(
            f"- {q['name']}（{q.get('computeClass', '?')}，{models}）："
            f"CPU {allocated.get('cpu', '0')}/{capability.get('cpu', '-')} 核{gpu}"
            f"——{q.get('description', '')}"
        )
    return "\n".join(lines)


async def t_list_images(args: dict, p: Platform) -> str:
    data = await p.get("/store/apps")
    apps = data["list"] if isinstance(data, dict) else data
    if not apps:
        return "（镜像仓库为空）"
    lines = ["可用镜像："]
    for app in apps:
        images = app.get("images") or []
        tags = ", ".join(images[:6]) if images else "latest"
        name = app.get("image") or app.get("name")
        desc = f"——{app['description']}" if app.get("description") else ""
        lines.append(f"- {name}（标签：{tags}）{desc}")
    return "\n".join(lines)


async def t_submit_job(args: dict, p: Platform) -> str:
    gpu = int(args.get("gpu") or 0)
    cpu = str(args.get("cpu") or "1")
    memory = str(args["memory"])
    resources = {"cpu": cpu, "memory": memory}
    if gpu:
        resources["nvidia.com/gpu"] = str(gpu)
    replicas = int(args.get("replicas") or 1)
    plugins: dict[str, list] | None = None
    if args.get("env"):
        plugins = {"ssh": [], "env": []}
    elif args.get("ssh"):
        plugins = {"ssh": []}
    draft = {
        "name": str(args["name"]),
        "project": str(args["project"]),
        "queue": str(args["queue"]),
        "minAvailable": replicas,
        "tasks": [
            {
                "name": "main",
                "replicas": replicas,
                "image": str(args["image"]),
                "command": [str(args["command"])],
                "resources": resources,
            }
        ],
    }
    if args.get("nodes"):
        draft["tasks"][0]["nodes"] = int(args["nodes"])
    if plugins:
        draft["plugins"] = plugins
    created = await p.post("/jobs", draft)
    gpu_text = f" / {gpu} 卡" if gpu else ""
    return (
        f"已提交：{created.get('namespace', '?')}/{created.get('name', '?')}\n"
        f"资源：{replicas} × ({cpu} 核 / {memory}{gpu_text})，队列 {args['queue']}\n"
        f"用 get_job 查进度。"
    )


async def t_list_projects(args: dict, p: Platform) -> str:
    data = await p.get("/projects")
    projects = data["list"] if isinstance(data, dict) else data
    if not projects:
        return "（你还没有可提交的项目——先在控制台建一个，或找管理员把你加进去）"
    lines = ["可提交的项目："]
    for proj in projects:
        lines.append(f"- {proj['slug']}（{proj['name']}，{proj.get('state', '')}）")
    return "\n".join(lines)


async def t_list_jobs(args: dict, p: Platform) -> str:
    params: dict[str, Any] = {}
    if args.get("phase"):
        params["phase"] = args["phase"]
    data = await p.get("/jobs", params=params or None)
    jobs = data["list"] if isinstance(data, dict) else data
    limit = min(int(args.get("limit") or 20), 50)
    if not jobs:
        return "（近期没有作业——用 submit_job 提一个？）"
    lines = ["近期作业："]
    for j in jobs[:limit]:
        created = (j.get("createdAt") or "")[:16].replace("T", " ")
        lines.append(
            f"- {j.get('namespace', '?')}/{j.get('name', '?')}：{j.get('phase', '?')}，"
            f"队列 {j.get('queue', '?')}，{j.get('user') or '?'} 提交于 {created}"
        )
    return "\n".join(lines)


async def _resolve_namespace(p: Platform, name: str, namespace: str | None) -> str:
    """后端 get_job 要 namespace 路径参数；AI 没给就从「我的作业」里按名字找。"""
    if namespace:
        return namespace
    data = await p.get("/jobs")
    for j in data["list"] if isinstance(data, dict) else data:
        if j.get("name") == name:
            return j["namespace"]
    raise ToolError(f"作业不存在：{name}（先用 list_jobs 看看名字对不对）")


async def t_get_job(args: dict, p: Platform) -> str:
    name = str(args["name"])
    ns = await _resolve_namespace(p, name, args.get("namespace"))
    job = await p.get(f"/jobs/{ns}/{name}")
    lines = [
        f"{job.get('namespace', '?')}/{job.get('name', '?')}（作业号 {job.get('jobId', '-')}）："
        f"{job.get('phase', '?')}，队列 {job.get('queue', '?')}",
        f"提交人 {job.get('user') or '-'}，创建于 {(job.get('createdAt') or '')[:16].replace('T', ' ')}"
        + (f"，{job.get('running')} 副本在跑" if job.get("running") else ""),
    ]
    if job.get("pendingReason"):
        lines.append(f"Pending 原因：{job['pendingReason']}")
    for t in job.get("tasks") or []:
        res = t.get("resources") or {}
        gpu = f"、{res['nvidia.com/gpu']} 卡" if res.get("nvidia.com/gpu") else ""
        lines.append(
            f"- task {t.get('name')} × {t.get('replicas')}（{res.get('cpu', '?')} 核 / "
            f"{res.get('memory', '?')}{gpu}，镜像 {t.get('image', '?')}）"
        )
    return "\n".join(lines)


async def t_delete_job(args: dict, p: Platform) -> str:
    name = str(args["name"])
    ns = await _resolve_namespace(p, name, args.get("namespace"))
    await p.delete(f"/jobs/{ns}/{name}")
    return f"已删除：{ns}/{name}"


async def t_list_gpu_instances(args: dict, p: Platform) -> str:
    data = await p.get("/gpu/instances")
    instances = data["list"] if isinstance(data, dict) else data
    if not instances:
        return "（没有在跑的开发环境实例）"
    lines = ["开发环境实例："]
    for inst in instances:
        res = inst.get("resources") or {}
        gpu = res.get("nvidia.com/gpu", "0")
        lines.append(
            f"- {inst.get('name', '?')}：{inst.get('status', inst.get('phase', '?'))}，"
            f"{res.get('cpu', '?')} 核 / {res.get('memory', '?')}，{gpu} 卡"
            f"，创建于 {(inst.get('createdAt') or '')[:16].replace('T', ' ')}"
        )
    return "\n".join(lines)


async def t_my_bill(args: dict, p: Platform) -> str:
    """GET /billing/me → BillSummary：三类费用 + 算力拆解 + 余额/授信 + 明细。"""
    month = args.get("month")
    data = await p.get("/billing/me", params={"month": month} if month else None)
    lines = [
        f"{data['month']} 账单（{data.get('tenant', '')}）：合计 {data['total']:.2f} 元",
        f"- 算力 {data.get('computeCost', 0):.2f}"
        f"（GPU {data.get('gpuCost', 0):.2f} / CPU {data.get('cpuCost', 0):.2f}"
        f" / 内存 {data.get('memoryCost', 0):.2f} / 网络 {data.get('networkCost', 0):.2f}）",
        f"- Token {data.get('tokenCost', 0):.2f}，存储 {data.get('storageCost', 0):.2f}",
        f"预付余额 {data.get('balance', 0):.2f} 元，授信 {data.get('credit', 0):.2f} 元",
    ]
    items = data.get("items") or []
    if items:
        lines.append("费用明细：")
        for it in items[:20]:
            lines.append(
                f"- [{it.get('kind', '?')}] {it.get('subject', '?')}："
                f"{it.get('quantity', '')} → {it.get('amount', 0):.2f} 元（{it.get('detail', '')}）"
            )
        if len(items) > 20:
            lines.append(f"（还有 {len(items) - 20} 项未列出，去控制台账单页看全部）")
    meter = data.get("meter") or {}
    if meter.get("note"):
        lines.append(f"计费说明：{meter['note']}（来源：{meter.get('source', '?')}）")
    return "\n".join(lines)


async def t_my_cost_trend(args: dict, p: Platform) -> str:
    """GET /billing/me/trend → page(list[UsagePoint])：按天汇总成三系列。"""
    days = max(1, min(int(args.get("days") or 30), 180))
    data = await p.get("/billing/me/trend", params={"days": days})
    points = data.get("list") or [] if isinstance(data, dict) else (data or [])
    by_day: dict[str, dict[str, float]] = {}
    for pt in points:
        day = by_day.setdefault(str(pt.get("time", "")), {})
        day[str(pt.get("series", "?"))] = float(pt.get("value") or 0)
    if not by_day:
        return f"（近 {days} 天没有费用记录）"
    lines = [f"近 {days} 天每日费用（元）："]
    total = 0.0
    for day in sorted(by_day):
        series = by_day[day]
        token_c = series.get("Token", 0.0)
        compute = series.get("算力", 0.0)
        storage = series.get("存储", 0.0)
        total += token_c + compute + storage
        lines.append(
            f"- {day}：合计 {token_c + compute + storage:.2f}"
            f"（算力 {compute:.2f} / Token {token_c:.2f} / 存储 {storage:.2f}）"
        )
    lines.append(f"合计：{total:.2f} 元")
    return "\n".join(lines)


_IMPL = {
    "cluster_overview": t_cluster_overview,
    "list_queues": t_list_queues,
    "list_images": t_list_images,
    "submit_job": t_submit_job,
    "list_projects": t_list_projects,
    "list_jobs": t_list_jobs,
    "get_job": t_get_job,
    "delete_job": t_delete_job,
    "list_gpu_instances": t_list_gpu_instances,
    "my_bill": t_my_bill,
    "my_cost_trend": t_my_cost_trend,
}
