"""镜像映射表 + Harbor 解析 + override 生成（计划文档 §5.4 主方案）。

主方案（全 benchmark 统一，无 tag 步骤）：

1. 映射表（JSONL）：每行 ``{"benchmark", "task_id", "docker_image"}``，
   ``docker_image`` 为 ``bench/task`` 形式的相对仓库路径，**无 tag、无
   registry 前缀**；查询键 ``(benchmark, task_id)`` 先精确匹配，未命中再回退
   同 benchmark 下 ``task_id`` 为空的通配条目（每 benchmark 至多一条）。
2. Harbor API 解析：列 tag —— 唯一则用之；多个 → episode 标
   ``skipped: ambiguous_tags``；零 tag（镜像不存在）视为映射表缺失，起跑前报错。
3. digest 固定：tag 解析后查 Harbor API 固化 digest（``:local`` 等可变标签
   必须记 digest 保证 baseline 可复现）。
4. 生成 override：``image`` 直写 Harbor 全路径（``registry/docker_image:tag``
   或 ``name@sha256:...``），其余字段按白名单翻译（``cwd``/``timeout`` 直传，
   ``user: N`` → ``run_args: ["--user", "N"]``；``runtime`` 等未知字段丢弃，
   禁止透传 trace 的 environment dict）。

``harbor_tag``（备选，tag 回 trace 原名）暂未实现，`image_source` 枚举保留。
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_REGISTRY = "harbor.internal"


class MappingError(RuntimeError):
    """映射表缺失/损坏：起跑前必须报错。"""


# ------------------------------------------------------------------ 映射表


@dataclass(frozen=True)
class ImageEntry:
    benchmark: str
    task_id: str
    docker_image: str


@dataclass(frozen=True)
class ImageResolution:
    """解析结果：replay 实际使用名 + digest + 溯源。"""

    image: str
    """replay 实际使用名（主方案 = Harbor 全路径 ``registry/repo:tag``）。"""

    docker_image: str
    """映射表中的相对仓库路径（bench/task）。"""

    tag: str
    digest: str
    """``sha256:...``；Harbor 未返回时为空串。"""

    source_image: str
    """trace 原名（如有）。"""

    image_source: str = "harbor_direct"


@dataclass(frozen=True)
class ImageSkipped:
    """episode 因镜像歧义被跳过（不静默猜 latest）。"""

    reason: str
    docker_image: str = ""
    tags: tuple[str, ...] = ()


class ImageMap:
    def __init__(self, entries: list[ImageEntry]):
        self.entries = entries
        self._validate()

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "ImageMap":
        entries = []
        for line in Path(path).read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            unknown = set(row) - {"benchmark", "task_id", "docker_image"}
            if unknown:
                raise MappingError(f"unknown image_map fields: {sorted(unknown)}")
            if not row.get("benchmark") or not row.get("docker_image"):
                raise MappingError(f"benchmark/docker_image required: {row}")
            # null/缺失统一归一为 ""（benchmark 级通配，见 tasks.jsonl 惯例）
            entries.append(
                ImageEntry(
                    benchmark=row["benchmark"],
                    task_id=row.get("task_id") or "",
                    docker_image=row["docker_image"],
                )
            )
        return cls(entries)

    def _validate(self) -> None:
        wildcards: dict[str, int] = {}
        for entry in self.entries:
            if entry.task_id == "":
                wildcards[entry.benchmark] = wildcards.get(entry.benchmark, 0) + 1
        duplicates = {benchmark for benchmark, count in wildcards.items() if count > 1}
        if duplicates:
            raise MappingError(f"multiple wildcard entries per benchmark: {sorted(duplicates)}")

    def lookup(self, benchmark: str, task_id: str) -> ImageEntry | None:
        for entry in self.entries:
            if entry.benchmark == benchmark and entry.task_id == task_id:
                return entry
        for entry in self.entries:
            if entry.benchmark == benchmark and entry.task_id == "":
                return entry
        return None


# ------------------------------------------------------------------ Harbor


class HarborAPI(Protocol):
    def list_tags(self, repo_path: str) -> list[str]: ...

    def digest(self, repo_path: str, tag: str) -> str: ...


class HarborClient:
    """Harbor v2.0 REST 客户端（stdlib urllib，可配 CA 与 robot token）。"""

    def __init__(
        self,
        registry: str = DEFAULT_REGISTRY,
        *,
        ca_cert: str = "",
        token: str = "",
        username: str = "",
        password: str = "",
        timeout: float = 30.0,
    ):
        if token and (username or password):
            raise ValueError("Harbor bearer token and username/password are mutually exclusive")
        if bool(username) != bool(password):
            raise ValueError("Harbor username and password must be provided together")
        self.registry = registry
        self.timeout = timeout
        context = None
        if ca_cert:
            context = ssl.create_default_context(cafile=ca_cert)
        self._context = context
        if token:
            self._headers = {"Authorization": f"Bearer {token}"}
        elif username:
            basic = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
            self._headers = {"Authorization": f"Basic {basic}"}
        else:
            self._headers = {}

    def _get(self, path: str) -> list[dict]:
        url = f"https://{self.registry}/api/v2.0{path}"
        request = urllib.request.Request(url, headers=self._headers)
        with urllib.request.urlopen(request, timeout=self.timeout, context=self._context) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _project_repo(repo_path: str) -> tuple[str, str]:
        project, _, repo = repo_path.partition("/")
        if not repo:
            raise MappingError(f"docker_image must be 'project/repo': {repo_path}")
        return project, repo

    def list_tags(self, repo_path: str) -> list[str]:
        project, repo = self._project_repo(repo_path)
        path = (
            f"/projects/{urllib.parse.quote(project)}/repositories/"
            f"{urllib.parse.quote(repo)}/artifacts?page_size=100"
        )
        artifacts = self._get(path)
        tags = [tag["name"] for artifact in artifacts for tag in artifact.get("tags", [])]
        return tags

    def digest(self, repo_path: str, tag: str) -> str:
        project, repo = self._project_repo(repo_path)
        path = (
            f"/projects/{urllib.parse.quote(project)}/repositories/"
            f"{urllib.parse.quote(repo)}/artifacts?page_size=100"
        )
        artifacts = self._get(path)
        for artifact in artifacts:
            if any(item["name"] == tag for item in artifact.get("tags", [])):
                return artifact.get("digest", "")
        return ""


# ------------------------------------------------------------------ 解析


def resolve_image(
    entry: ImageEntry,
    *,
    registry: str,
    harbor: HarborAPI,
    source_image: str = "",
) -> ImageResolution | ImageSkipped:
    """映射表条目 → Harbor 解析；唯一 tag 解析成功，多 tag 跳过，零 tag 报错。"""
    tags = harbor.list_tags(entry.docker_image)
    if not tags:
        raise MappingError(
            f"image_map entry has no tags in Harbor: {entry.benchmark}/{entry.task_id} "
            f"-> {entry.docker_image}"
        )
    if len(tags) > 1:
        return ImageSkipped("ambiguous_tags", docker_image=entry.docker_image, tags=tuple(tags))
    tag = tags[0]
    digest = harbor.digest(entry.docker_image, tag)
    return ImageResolution(
        image=f"{registry}/{entry.docker_image}:{tag}",
        docker_image=entry.docker_image,
        tag=tag,
        digest=digest,
        source_image=source_image,
        image_source="harbor_direct",
    )


# ------------------------------------------------------------------ override


def build_override(
    image: str,
    *,
    source_environment: dict | None = None,
    environment_class: str = "docker",
) -> dict:
    """生成 replay 的 ``--environment-config`` override（字段白名单翻译）。

    ``runtime: "bounded Docker"`` 等未知字段一律丢弃，禁止透传 trace 环境 dict。
    ``--rm`` 一并写入 run_args，避免 guest 内容器残留。
    """
    override: dict = {"environment_class": environment_class, "image": image}
    source = source_environment or {}
    if source.get("cwd"):
        override["cwd"] = source["cwd"]
    if source.get("timeout") is not None:
        override["timeout"] = source["timeout"]
    run_args: list[str] = ["--rm"]
    if source.get("user") is not None:
        run_args.extend(["--user", str(source["user"])])
    override["run_args"] = run_args
    return override
