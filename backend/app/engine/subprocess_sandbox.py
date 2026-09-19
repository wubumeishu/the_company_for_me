"""
subprocess_sandbox.py —— Phase 3b-①：subprocess 真 git 地基
（PHASE_3_ENGINEERING_DEFENSE §1.2 分层隔离 + §2.1 Q1 三层叠加 + §7 拍板记录）。

主人拍板的三层隔离（本模块落地层①②，层③默认 venv）：
  层① 进程隔离   run_subprocess 原语 —— asyncio 子进程 + 最小 env + 超时 + 进程组 kill
                （AutoGen 式执行面沙盒：挂起/失控的子进程随父死，不泄漏）
  层② Git 状态隔离  SandboxRepo 影子仓（选项 A：.company/git-sandbox/ 独立 git init，
                每 Dev 一个物理 worktree，共享对象库、HEAD/工作区独立）
                —— "每个员工一张独立工位，工位炸了办公室不倒"
  层③ 运行时隔离  ensure_venv / run_in_venv —— checkScripts 默认走 venv（LOCAL-FIRST），
                Docker 可选档由 3b-③ 显式 execSandbox:"docker" 接入（本层不实现）

铁律（引擎权威层，与 §1.5 中介授权层同源）：
  - 绝不动主人真实 .git：SandboxRepo(trusted=False) 只接受 <...>/.company/git-sandbox/
  - main head 唯一合法推进路径 = merge_squash（全绿才允许调用；任何其它写 main 的
    入口在 3b-② SubprocessGitPolicy 中拒绝——单一权威写入口）
  - 全动作事件可回放：git_sandbox_init / git_worktree_add / git_worktree_prune /
    git_squash_failed / shell_log（复用既有 shell_log 词汇，前端 LiveLogPanel 零改动）
  - runner 可注入：单测用 FakeRunner 全程不发真进程（同 2c transport 注入思路）；
    真跑链路测试才 tmp 影子仓真起 git。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .events import EventBus

# 子进程执行器签名：(args, cwd=..., env=..., timeout=..., on_line=...) -> SubprocessResult
# 生产 = run_subprocess；单测 = FakeRunner（离线断言分支路由/门禁红绿，不发真进程）
Runner = Callable[..., "SubprocessResult"]


class SandboxGitError(RuntimeError):
    """影子仓/worktree/git 动作失败，或沙盒安全校验拒绝。"""


# ---------------------------------------------------------------- 层① 进程隔离
@dataclass
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def build_sandbox_env() -> dict[str, str]:
    """最小化 env（隔离核心）：只留进程必需变量，剥掉一切密钥/凭据（TOKEN 类绝不进 AI 子进程）。"""
    if os.name == "nt":
        keep = ("PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP")
    else:
        keep = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
    return {k: os.environ[k] for k in keep if k in os.environ}


async def run_subprocess(
    args: list[str],
    *,
    cwd: Optional[Path | str] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: float = 120.0,
    on_line: Optional[Callable[[str, str], None]] = None,
) -> SubprocessResult:
    """进程隔离原语（层①）：asyncio 子进程 + 最小 env + 超时 + 进程组 kill。

    - POSIX：start_new_session 独立进程组，超时 os.killpg 整组清场（孙进程不泄漏）；
    - Windows：无进程组，降级为直杀子进程（本机 LOCAL-FIRST 够用，Docker 档才是硬隔离）。
    - on_line(line, stream)：流式日志回调（shell_log 事件的数据源，大盘实时流）。
    """
    base_env = dict(env if env is not None else build_sandbox_env())
    popen_kw: dict[str, Any] = dict(
        cwd=str(cwd) if cwd else None,
        env=base_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if hasattr(os, "killpg"):
        popen_kw["start_new_session"] = True

    try:
        proc = await asyncio.create_subprocess_exec(*[str(a) for a in args], **popen_kw)
    except (OSError, ValueError) as e:
        raise SandboxGitError(f"无法启动子进程 {list(args)[:3]}: {e}") from e

    out_buf: list[bytes] = []
    err_buf: list[bytes] = []

    async def _pump(stream, sink: list[bytes], name: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            sink.append(line)
            if on_line is not None:
                on_line(line.decode("utf-8", "replace").rstrip(), name)

    try:
        await asyncio.wait_for(
            asyncio.gather(_pump(proc.stdout, out_buf, "stdout"),
                           _pump(proc.stderr, err_buf, "stderr")),
            timeout=timeout,
        )
        await proc.wait()
        timed_out = False
    except asyncio.TimeoutError:
        # 超时清场：POSIX 整组 SIGTERM（挂起子进程不泄漏），Windows 直杀
        if hasattr(os, "killpg") and proc.pid:
            try:
                os.killpg(os.getpgid(proc.pid), 15)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        proc.kill()
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
        timed_out = True

    def _join(sink: list[bytes]) -> str:
        return b"".join(sink).decode("utf-8", "replace")

    return SubprocessResult(
        returncode=int(proc.returncode) if proc.returncode is not None else -1,
        stdout=_join(out_buf),
        stderr=_join(err_buf),
        timed_out=timed_out,
    )


def shell_cmd(script: str) -> list[str]:
    """checkScripts 是 shell 字符串（如 "python -m pytest ..."）：按平台包一层解释器。"""
    return ["cmd", "/c", script] if os.name == "nt" else ["/bin/sh", "-c", script]


async def run_shell(script: str, **kw: Any) -> SubprocessResult:
    return await run_subprocess(shell_cmd(script), **kw)


# ---------------------------------------------------------------- 层② 影子仓
def _sanitize(node_id: str) -> str:
    """worktree 目录名 / 分支名安全化（Dev 节点 id 里可能带 . / 空格等）。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", node_id)


class SandboxRepo:
    """影子仓（层②，选项 A 拍板）：.company/git-sandbox/ 独立 init，每 Dev 独立物理 worktree。

    布局（root = <project_root>/.company/git-sandbox）：
        root/.git                  共享对象库（worktree 秒开，省空间）
        root/worktrees/<node>/     每 Dev 一张独立工位（HEAD/工作区物理隔离）
        root/.venv/                层③ 默认 venv 运行时隔离
    分支模型：main（引擎唯一可写主干）+ b/<node_id>（Dev 专属，worktree 挂其上）

    seed_from（项目根）：init 时把主仓代码树（排除 .git/.company/node_modules/…）
      拷进影子仓做基准 commit —— 影子仓自包含完整代码库，checkScripts 在影子仓根
      + venv 下真跑，且 Dev 工作区/主干都隔离于主仓（LOCAL-FIRST 零污染）。
    """

    SANDBOX_NAME = "git-sandbox"
    VENV_DIR = ".venv"
    # seed 拷贝排除项（防递归 + 只拷源码不拷运行态/依赖树）
    SEED_EXCLUDE = (".git", ".company", "node_modules", "dist", ".vite",
                    ".venv", "__pycache__", ".DS_Store")

    def __init__(self, root: Path | str, *, base_branch: str = "main",
                 bus: Optional[EventBus] = None,
                 runner: Optional[Runner] = None,
                 trusted: bool = False,
                 timeout: float = 120.0,
                 seed_from: Optional[Path | str] = None):
        self.root = Path(root)
        self.base = base_branch
        self.bus = bus
        self._runner: Runner = runner or run_subprocess
        self.trusted = trusted          # 单测 tmp 仓专用；生产一律 False（安全校验开启）
        self._timeout = timeout
        self._branch_of: dict[str, str] = {}    # node_id → 实际分支名（add_worktree 时记录）
        self._seed_from = Path(seed_from) if seed_from else None   # 项目根（init 时拷代码树）
        self._init_lock: Optional[asyncio.Lock] = None            # 懒加载：init 全程串行（并发协程 guard）
        # 铁律 fail-fast：构造期就校验影子仓位置（真实 .git 在构造时即被拒绝，不留到调用期）
        self._assert_sandbox_root()

    # ---- 安全校验（铁律：绝不动主人真实 .git）
    def _assert_sandbox_root(self) -> None:
        if self.trusted:
            return
        if self.root.name != self.SANDBOX_NAME:
            raise SandboxGitError(
                f"影子仓 root 必须名为 {self.SANDBOX_NAME}/（trusted=False 拒绝指向真实仓库）: {self.root}")
        if not any(p.name == ".company" for p in self.root.parents):
            raise SandboxGitError(
                f"影子仓必须位于 .company/ 之下（LOCAL-FIRST 红线，不碰主人真实 .git）: {self.root}")

    # ---- 内部：可注入 runner + 统一错误面
    async def _git(self, *args: str, check: bool = True,
                   on_line: Optional[Callable[[str, str], None]] = None) -> SubprocessResult:
        res = await self._runner(
            ["git", *[str(a) for a in args]], cwd=str(self.root), env=build_sandbox_env(),
            timeout=self._timeout, on_line=on_line)
        if check and (res.timed_out or res.returncode != 0):
            raise SandboxGitError(
                f"git {' '.join(args[:2])} 失败 rc={res.returncode} timed_out={res.timed_out}\n"
                f"{res.stderr.strip()[:400]}")
        return res

    def _log(self, node_id: str) -> Callable[[str, str], None]:
        """子进程流式日志 → shell_log 事件（前端大盘复用既有词汇，零改动）。"""
        if self.bus is None:
            return lambda _l, _s: None

        def _emit(line: str, stream: str) -> None:
            async def _p() -> None:
                await self.bus.publish("shell_log", node_id=node_id, stream=stream,
                                       line=line[:300])
            try:
                asyncio.get_running_loop().create_task(_p())
            except RuntimeError:
                pass      # 无事件循环时静默（同步测试环境）
        return _emit

    # ---- 初始化
    async def ensure_repo(self) -> None:
        """影子仓 init（幂等 + 并发安全）：独立 git 库 + main 基准 + 本地 git 身份。
        seed_from 非空时先拷主仓代码树做基准。
        ★ 并发安全：L0 多 Dev 经 asyncio.gather 并发，可能同时穿过「.git exists?」守卫
        同时 git init → git-for-windows 模板 hook 拷贝撞车（File exists）。用懒加载锁
        把 init 全程串行化（worktree/commit 仍设计内并行，各工位独立）。"""
        self._assert_sandbox_root()
        if self.root.joinpath(".git").exists():
            return
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self.root.joinpath(".git").exists():   # 双重检查：拿锁期间可能已被并发协程建好
                return
            self.root.mkdir(parents=True, exist_ok=True)
            await self._git("init", "-q")
            await self._git("checkout", "-B", self.base)
            await self._git("config", "user.name", "Company Engine")
            await self._git("config", "user.email", "engine@company.local")
            await self._git("config", "commit.gpgsign", "false")
            (self.root / ".gitignore").write_text("worktrees/\n.venv/\n", encoding="utf-8")
            self._seed_copy()
            await self._git("add", "-A")
            await self._git("commit", "-q", "-m", "chore: git-sandbox init (seeded from project root)")
            if self.bus:
                await self.bus.publish("git_sandbox_init", root=str(self.root), base=self.base,
                                       seeded=bool(self._seed_from))

    def _seed_copy(self) -> None:
        """把 seed_from（项目根）的代码树拷进影子仓根（排除 SEED_EXCLUDE）。
        非破坏：只写影子仓内，主仓零改动。无 seed_from = 空仓 init（离线测试路径）。"""
        if not self._seed_from or not self._seed_from.exists():
            return
        import shutil
        for entry in self._seed_from.iterdir():
            if entry.name in self.SEED_EXCLUDE:
                continue
            dest = self.root / entry.name
            try:
                if entry.is_dir():
                    shutil.copytree(entry, dest, dirs_exist_ok=True,
                                     ignore=shutil.ignore_patterns(*self.SEED_EXCLUDE))
                else:
                    shutil.copy2(entry, dest)
            except (OSError, shutil.Error):
                continue      # 单个文件拷失败不阻塞 init（seed 是优化项，非阻断）

    async def main_head(self) -> str:
        """main 当前 head sha（初始 commit 前 = 空串；红灯路径证明 main 未动的基准）。"""
        res = await self._git("rev-parse", "HEAD", check=False)
        return res.stdout.strip() if res.ok else ""

    def has_file(self, rel: str) -> bool:
        """影子仓根下是否有 <rel>（种子自包含验证 / checkScripts 前置诊断）。"""
        return (self.root / rel).exists()

    # ---- Dev 工位（选项 A：每 Dev 独立 worktree 物理隔离）
    def worktree_path(self, node_id: str) -> Path:
        return self.root / "worktrees" / _sanitize(node_id)

    def branch_for(self, node_id: str) -> str:
        """node_id 的实际分支（add_worktree 时记录过 = 自定义 branch 字段，如 dev-a；
        否则缺省 b/<node_id>）。"""
        return self._branch_of.get(node_id, f"b/{_sanitize(node_id)}")

    async def add_worktree(self, node_id: str, branch: Optional[str] = None) -> Path:
        """给 Dev 开独立工位：worktrees/<node> @ branch（缺省 b/<node>；可传自定义如 dev-a）。
        幂等：工位已存在则复用（纠错重试语义——同节点重跑叠在原 worktree 上，分支保留）。

        ★ flake 根因修复（3b② 收口后 3-round 序列复现）：不再 try/except 盲猜"分支在不在"。
        旧逻辑 `worktree add -b br main` 一失败就假设"分支已存在"走 fallback，但 -b 失败
        可能是并发/瞬时（同仓并行 worktree-add 抢 .git/worktrees/.lock），此时分支其实还没建
        出来 → fallback `worktree add br` 报 "invalid reference: <br>"。
        正解（rev-parse 显式判断，教训来自 9-19）：先问"分支在不在"，在=直接挂，不在=从 base 新建。"""
        await self.ensure_repo()
        br = _sanitize(branch) if branch else self.branch_for(node_id)
        self._branch_of[node_id] = br
        path = self.worktree_path(node_id)
        if path.exists():
            return path                       # 工位复用（纠错重试叠在原 worktree 上）
        if await self._branch_exists(br):
            # 分支已在（工位目录被 prune 清过但分支保留）：直接挂现有分支，不再 -b
            await self._git("worktree", "add", str(path), br)
        else:
            # 分支不存在：从 base 新建
            await self._git("worktree", "add", str(path), "-b", br, self.base)
        if self.bus:
            await self.bus.publish("git_worktree_add", node_id=node_id, branch=br, path=str(path))
        return path

    async def _branch_exists(self, name: str) -> bool:
        """分支是否存在（权威判断，非 try/except 猜）：rev-parse --verify refs/heads/<name>。
        0=在 / 非0=不在。用 refs/heads 全限定，避免与 tag/remote 同名歧义。"""
        res = await self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False)
        return res.returncode == 0

    async def commit_in_worktree(self, node_id: str, message: str,
                                 files: Mapping[str, str]) -> str:
        """Dev 产物提交：只发生在自己的 worktree（引擎代劳，Dev 不直接碰 git）。
        files = 相对路径 -> 内容；越界（绝对路径/..）物理拒绝。返回 commit sha。"""
        wt = await self.add_worktree(node_id)
        wt_real = wt.resolve()
        for rel, content in files.items():
            target = (wt / rel).resolve()
            try:
                target.relative_to(wt_real)
            except ValueError:
                raise SandboxGitError(f"产物路径逃逸 worktree（隔离破坏，拒绝）: {rel}") from None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        await self._git("-C", str(wt), "add", "-A")
        await self._git("-C", str(wt), "commit", "-q", "-m", message)
        res = await self._git("-C", str(wt), "rev-parse", "HEAD")
        return res.stdout.strip()

    # ---- 主干推进（唯一合法写 main 的路径；红灯调用方自行拒绝）
    async def merge_squash(self, node_id: str, message: str) -> str:
        """全绿后 git merge --squash b/<node> 进 main + commit（QA 可溯源 sha）。
        冲突/无内容 = 红灯信号：merge 后立即 reset 清空暂存区，main 毫发未动，
        抛 SandboxGitError 供上层转 rejected（单写入口铁律的物理保证）。"""
        br = self.branch_for(node_id)
        # 前置：main index 干净（防上次残留 staged 内容混进本次 squash）
        await self._git("reset", "-q", check=False)
        res = await self._git("merge", "--squash", br, check=False)
        if res.returncode != 0:
            await self._git("reset", "-q", check=False)      # 清掉冲突暂存区，main 恢复原状
            if self.bus:
                await self.bus.publish("git_squash_failed", node_id=node_id,
                                       detail=res.stderr.strip()[:300])
            raise SandboxGitError(f"merge --squash {br} 失败（红灯，main 未动）: "
                                   f"{res.stderr.strip()[:300]}")
        # squash 成功 = 改动全部 staged 进 main index → commit 收编
        # （git merge --squash 不自动 commit，这是文档规定的两步式）
        await self._git("commit", "-q", "-m", message or f"merge {br}")
        return (await self._git("rev-parse", "HEAD")).stdout.strip()

    async def merge_dry_run(self, node_id: str) -> tuple[bool, str]:
        """3b-② pr_gate 冲突 dry-run 入口（§2.2 检查 ①）：git merge-tree 判定。
        冲突 → (False, detail)；无冲突 → (True, "")。不改动 main。"""
        br = self.branch_for(node_id)
        res = await self._git("merge-tree", "--write-tree", "HEAD", br, check=False)
        if res.returncode != 0:
            return False, res.stderr.strip()[:300] or res.stdout.strip()[:300]
        return True, ""

    # ---- 三段式：stage → gate → commit/discard（3b-② pr_gate 专用）
    # 多 Dev 分支叠加合入 = 顺序 stage 进 main index 物理检测冲突；
    # gate（checkScripts）跑在 staged 状态上；全绿 commit 收编，红灯 discard 清空，main 未动。
    async def stage_squash(self, node_id: str) -> bool:
        """把 b/<node> 的改动 squash-stage 进 main index（不 commit）。
        冲突 → hard reset 恢复 index/工作区原状，返回 False（门禁前即亮红灯，不打 dirty index）。"""
        br = self.branch_for(node_id)
        res = await self._git("merge", "--squash", br, check=False)
        if res.returncode != 0:
            await self._git("reset", "--hard", "-q", "HEAD", check=False)
            return False
        return True

    async def has_staged(self) -> bool:
        """main index 是否有待 commit 的 staged 改动（空合入判定：无内容=无操作，不算冲突）。"""
        res = await self._git("diff", "--cached", "--quiet", check=False)
        return res.returncode != 0      # diff --quiet：有差异 rc=1，无差异 rc=0

    async def commit_squash(self, message: str) -> Optional[str]:
        """全绿收编：把 staged 改动 commit 进 main（引擎唯一合法写主干路径，QA 可溯源 sha）。
        无内容可提交（index 空）= 返回 None（无操作，非红灯）；其它失败抛错。"""
        res = await self._git("commit", "-q", "-m", message, check=False)
        if res.returncode != 0:
            if "nothing to commit" in (res.stderr + res.stdout):
                return None
            raise SandboxGitError(f"main commit 失败（无 staged 内容或 index 异常）: "
                                   f"{res.stderr.strip()[:300]}")
        return (await self._git("rev-parse", "HEAD")).stdout.strip()

    async def discard_squash(self) -> None:
        """红灯打回：hard reset 清空 index + 工作区，恢复 HEAD 原状（冲突标记必须
        --hard 才清得干净，物理保证 main 毫发未动）。"""
        await self._git("reset", "--hard", "-q", "HEAD", check=False)

    # ---- ref 原语（3b-② preview 合入用：合并发生在临时分支，main 只在全绿时 ff 推进）
    async def checkout(self, ref: str) -> None:
        await self._git("checkout", "-q", ref)

    async def branch(self, name: str, ref: str) -> None:
        await self._git("branch", "-q", name, ref, check=False)

    async def delete_branch(self, name: str) -> None:
        await self._git("branch", "-q", "-D", name, check=False)

    async def merge_ff(self, branch: str) -> str:
        """当前分支 ff-only 推进到 <branch>（preview 全绿后 main 收编；非快进=红灯抛错）。"""
        res = await self._git("merge", "--ff-only", "-q", branch, check=False)
        if res.returncode != 0:
            raise SandboxGitError(f"ff-only {branch} 失败（非快进，main 未动）: {res.stderr.strip()[:300]}")
        return (await self._git("rev-parse", "HEAD")).stdout.strip()

    async def prune_worktree(self, node_id: str, delete_branch: bool = True) -> None:
        """收工位：worktree remove（--force 允许未提交残留）+ 可选删分支 + prune 清理注册表。"""
        res = await self._git("worktree", "remove", "--force",
                              str(self.worktree_path(node_id)), check=False)
        if res.returncode != 0 and "is not a working tree" not in res.stderr:
            # 工位未注册（如整仓重建过）时 remove 会报"不是工作树"，无害；其它错误保留证据
            pass
        if delete_branch:
            await self._git("branch", "-D", self.branch_for(node_id), check=False)
        await self._git("worktree", "prune", check=False)
        if self.bus:
            await self.bus.publish("git_worktree_prune", node_id=node_id)

    async def log(self, ref: str = "HEAD", n: int = 20) -> str:
        """主干/分支提交证据（站会大盘 Git 实时流 + QA 溯源）。"""
        res = await self._git("log", "--oneline", f"-n{min(max(n,1),50)}", ref, check=False)
        return res.stdout.strip()

    # ---- 层③ 运行时隔离（venv 默认档，拍板 #2）
    def venv_python(self) -> Path:
        return self.root / self.VENV_DIR / ("Scripts/python.exe" if os.name == "nt"
                                            else "bin/python")

    async def ensure_venv(self, with_pip: bool = False) -> Path:
        """venv 隔离层（幂等，LOCAL-FIRST）：--system-site-packages 复用系统已装依赖
        （零网络），venv 仍隔离执行环境（独立目录/PYTHONPATH/PATH 注入）。Docker 硬隔离
        档由 3b-③ execSandbox:"docker" 显式接入；with_pip=True 才装 pip。"""
        pyexe = self.venv_python()
        if pyexe.exists():
            return pyexe
        cmd = [sys.executable, "-m", "venv", "--system-site-packages",
               str(self.root / self.VENV_DIR)]
        if not with_pip:
            cmd.insert(-1, "--without-pip")
        res = await self._runner(cmd, cwd=str(self.root), env=build_sandbox_env(),
                                 timeout=600.0)
        if not pyexe.exists():
            raise SandboxGitError(f"venv 创建失败: {res.stderr.strip()[:300]}")
        return pyexe

    async def run_in_venv(self, script: str, *, node_id: str = "sandbox",
                          with_pip: bool = False) -> SubprocessResult:
        """checkScripts 执行入口（层③）：venv python 隔离跑，流式日志进 shell_log。
        超时/非零 rc 不抛错，由 SubprocessGitPolicy（3b-②）转成门禁红灯 + red_flags。"""
        pyexe = await self.ensure_venv(with_pip=with_pip)
        return await self._runner(
            [str(pyexe), "-c", script], cwd=str(self.root),
            env=build_sandbox_env(), timeout=self._timeout,
            on_line=self._log(node_id))

    def venv_bin(self) -> Path:
        """venv 可执行目录（Windows=Scripts/，POSIX=bin/）。"""
        return self.root / self.VENV_DIR / ("Scripts" if os.name == "nt" else "bin")

    def venv_env(self) -> dict[str, str]:
        """checkScripts 用的最小 env + venv 注入：venv bin 置顶 PATH，
        让 'python'/'pytest' 等解析到 venv 解释器（隔离 Python 依赖，拍板 #2）。"""
        env = build_sandbox_env()
        env["PATH"] = f"{self.venv_bin()}{os.pathsep}{env.get('PATH', '')}"
        return env

    async def run_shell_in_venv(self, script: str, *, node_id: str = "sandbox",
                                with_pip: bool = False,
                                check_timeout: Optional[float] = None,
                                cwd: Optional[Path | str] = None,
                                env_extra: Optional[dict[str, str]] = None,
                                on_line: Optional[Callable[[str, str], None]] = None
                                ) -> SubprocessResult:
        """checkScripts 真跑（层③ venv 档）：shell 脚本 + venv PATH 注入，隔离 cwd。
        非零 rc / 超时不抛错，由 SubprocessGitPolicy 转门禁红灯 + red_flags（可溯源）。"""
        await self.ensure_venv(with_pip=with_pip)
        env = self.venv_env()
        if env_extra:
            env.update(env_extra)
        return await self._runner(
            shell_cmd(script), cwd=str(cwd or self.root), env=env,
            timeout=check_timeout or self._timeout,
            on_line=on_line or (self._log(node_id)))
