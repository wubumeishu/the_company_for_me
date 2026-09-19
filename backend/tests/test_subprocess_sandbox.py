
"""3b-① 地基实测：A) 离线 FakeRunner（零真进程） B) 真 git + 真 venv 链路
★ 红线：main head 只在「全绿 squash」时推进；任何红灯路径 main 毫发未动。
★ 冲突场景：b/n3 与 b/n4 从同一 main 基准各自 add shared.py（内容不同）= add/add 冲突；
  n3 先 squash 进 main 后，n4 再 squash 必撞冲突。
"""
import asyncio, sys, tempfile
from pathlib import Path

ROOT = Path(r"H:\project\company\backend")
sys.path.insert(0, str(ROOT))

from app.engine.events import EventBus
from app.engine.subprocess_sandbox import (
    SandboxRepo, SubprocessResult, SandboxGitError, build_sandbox_env, shell_cmd,
)

passed, failed = [], []
def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS" if cond else "FAIL"), name, detail)

# ============ A) 离线 FakeRunner：git 调用全部打桩，零真进程 ============
class FakeRunner:
    """按 argv 语义打桩（忽略 -C <path> 前缀）。"""
    def __init__(self):
        self.calls = []
        self.worktrees: set[str] = set()
        self.dead_branches: set[str] = set()
        self.merge_conflict_branches = {"b/evil", "b/n4"}   # 红灯桩
    def _args(self, args):
        toks = [str(a) for a in args]
        if toks[:1] == ["git"]:
            toks = toks[1:]
        out, i = [], 0
        while i < len(toks):
            if toks[i] == "-C":
                i += 2
                continue
            out.append(toks[i]); i += 1
        return out
    async def __call__(self, args, cwd=None, env=None, timeout=None, on_line=None, **kw):
        toks = self._args(args)
        self.calls.append(toks)
        if not toks:
            return SubprocessResult(0, "", "")
        sub = toks[0]
        if sub == "worktree":
            act = toks[1]
            if act == "add":
                # git worktree add <path> [-b <br>] [base]
                path = toks[2]
                self.worktrees.add(Path(path).name)
            elif act == "remove":
                path = toks[3] if toks[2] == "--force" else toks[2]
                self.worktrees.discard(Path(path).name)
        elif sub == "branch" and len(toks) > 2 and toks[1] == "-D":
            self.dead_branches.add(toks[2])
            return SubprocessResult(1, "", "branch not fully merged (kept for forensics)")
        elif sub == "merge" and len(toks) > 2 and toks[1] == "--squash":
            br = toks[2]
            if br in self.merge_conflict_branches:
                return SubprocessResult(1, "", f"CONFLICT (add/add): Merge conflict in {br}/shared.py")
        elif sub == "rev-parse":
            return SubprocessResult(0, "sha-fake-0001\n", "")
        elif sub == "merge-tree" and toks[-1] in self.merge_conflict_branches:
            return SubprocessResult(1, "", f"CONFLICT (add/add) {toks[-1]}/shared.py")
        return SubprocessResult(0, "", "")

async def test_offline():
    print("\n== A) 离线 FakeRunner（零真进程） ==")
    tmp = Path(tempfile.mkdtemp(prefix="company-sbx-"))
    # 铁律 fail-fast：trusted=False 的 SandboxRepo 拒绝一切非 .company/git-sandbox 布局
    try:
        SandboxRepo(tmp / "real-git", trusted=False)
        check("铁律：拒绝指向真实仓（构造期 fail-fast）", False)
    except SandboxGitError:
        check("铁律：拒绝指向真实仓（构造期 fail-fast）", True)

    fake = FakeRunner()
    repo = SandboxRepo(tmp / ".company" / "git-sandbox", bus=EventBus(), runner=fake, trusted=True)
    await repo.ensure_repo()
    check("init 序列：git init / checkout -B main / 本地身份 / 基准提交",
          any(c[0] == "init" for c in fake.calls)
          and any(c[0:2] == ["checkout", "-B"] for c in fake.calls)
          and any(c[0] == "commit" and "chore" in " ".join(c) for c in fake.calls))

    await repo.add_worktree("n1_dev")
    check("工位：worktree add worktrees/n1_dev @ b/n1_dev", "n1_dev" in fake.worktrees)
    sha = await repo.commit_in_worktree("n1_dev", "n1_dev: feat A", {"mod_a.py": "print('A')"})
    check("commit_in_worktree 返回 sha（引擎代劳，-C 工位内 add/commit/rev-parse）",
          sha == "sha-fake-0001", sha)
    try:
        await repo.commit_in_worktree("n1_dev", "x", {"../../escape.py": "evil"})
        check("铁律：产物路径逃逸 worktree 物理拒绝", False)
    except SandboxGitError:
        check("铁律：产物路径逃逸 worktree 物理拒绝", True)

    await repo.merge_squash("n1_dev", "merge b/n1_dev")
    check("全绿：merge --squash + commit（main 推进）",
          any(c[0:2] == ["merge", "--squash"] and c[2] == "b/n1_dev" for c in fake.calls))

    try:
        await repo.merge_squash("evil", "merge b/evil")
        check("红灯：squash 冲突 → SandboxGitError（main 未动）", False)
    except SandboxGitError:
        check("红灯：squash 冲突 → SandboxGitError（main 未动）", True)
    ok, detail = await repo.merge_dry_run("n4")
    check("dry-run（merge-tree）红灯可预判", not ok, detail[:50])

    await repo.prune_worktree("n1_dev")
    check("收工位：worktree remove + branch -D + prune 三连",
          "n1_dev" not in fake.worktrees and "b/n1_dev" in fake.dead_branches)

# ============ B) 真链路：tmp 影子仓真起 git + 真 venv ============
async def test_real():
    print("\n== B) 真 git + 真 venv（tmp 影子仓，绝不碰主仓库 .git） ==")
    tmp = Path(tempfile.mkdtemp(prefix="company-real-"))
    bus = EventBus()
    evs: list[str] = []
    bus.subscribe(lambda e: evs.append(e.type))
    repo = SandboxRepo(tmp / ".company" / "git-sandbox", bus=bus, trusted=False)
    await repo.ensure_repo()
    head0 = await repo.main_head()
    check("影子仓 init（main 有基准 commit）", head0 != "", head0[:12])

    wt = await repo.add_worktree("n1_fe")
    check("真 worktree 物理目录已建", wt.is_dir())
    sha = await repo.commit_in_worktree("n1_fe", "n1_fe: feat 模块A", {"mod_a.py": "A = 1\n"})
    check("真 commit 落 b/n1_fe（40 位 sha）", len(sha) == 40, sha[:12])
    check("Dev 提交期间 main 未动", await repo.main_head() == head0)

    await repo.add_worktree("n2_be")
    await repo.commit_in_worktree("n2_be", "n2_be: feat 模块B", {"mod_b.py": "B = 1\n"})
    head_green = await repo.merge_squash("n1_fe", "merge b/n1_fe")
    check("全绿 squash → main 推进", head_green != head0, head_green[:12])
    check("main 日志含 squash 证据", "merge b/n1_fe" in await repo.log("HEAD", 10))

    # ★ add/add 冲突链：n3、n4 从同一 main 基准（无 shared.py）各自 add shared.py，内容不同
    h_base = await repo.main_head()
    await repo.add_worktree("n3"); await repo.add_worktree("n4")   # 都基于 h_base
    await repo.commit_in_worktree("n3", "n3: shared v1", {"shared.py": "x = 1\n"})
    head_n3 = await repo.merge_squash("n3", "merge b/n3")          # main 收编 shared v1
    check("n3（shared v1）squash 成功", head_n3 != h_base, head_n3[:12])
    await repo.commit_in_worktree("n4", "n4: shared v2", {"shared.py": "y = 2\n"})
    try:
        await repo.merge_squash("n4", "merge b/n4")
        check("红灯：n4 add/add 冲突 → squash 被拒", False, "竟然合进去了?!")
    except SandboxGitError:
        check("红灯：n4 add/add 冲突 → squash 被拒（git 证据）", True)
    check("★ 红线：红灯路径 main 全程未动（head 仍 = n3 squash 后）",
          await repo.main_head() == head_n3)
    check("★ 红灯可预判：dry-run 先亮（引擎 pr_gate 检查①用）",
          (await repo.merge_dry_run("n4"))[0] is False)
    check("main 工作区无残留冲突标记", "UU" not in (await repo.log("HEAD", 3)))

    # 层③ venv 运行时隔离
    py = await repo.ensure_venv()
    check("venv 隔离层建成功（--without-pip 零网络）", py.exists())
    r = await repo.run_in_venv("import sys; print(sys.executable)", node_id="n1_fe")
    check("checkScript 走 venv python（隔离 cwd）", r.returncode == 0 and "git-sandbox" in r.stdout,
          r.stdout.strip()[:70])

    for nid in ("n1_fe", "n2_be", "n3", "n4"):
        await repo.prune_worktree(nid, delete_branch=False)
    wt_dir = repo.root / "worktrees"
    left = [d.name for d in wt_dir.iterdir() if d.is_dir()] if wt_dir.exists() else []
    check("收工位后 worktrees 目录清空", not left, str(left))
    check("事件流齐全（sandbox_init/worktree_add/prune/squash_failed/shell_log）",
          all(t in evs for t in ("git_sandbox_init", "git_worktree_add", "git_worktree_prune",
                                 "git_squash_failed", "shell_log")),
          sorted(set(evs)))

    env = build_sandbox_env()
    leaked = [k for k in env if any(s in k.upper() for s in ("TOKEN", "SECRET", "KEY"))]
    check("沙盒 env 最小化（无 TOKEN/SECRET/KEY 泄漏）", not leaked, str(leaked))
    check("shell 包装按平台（Windows=cmd /c）", shell_cmd("x")[0] == "cmd")

async def main():
    await test_offline()
    await test_real()
    print(f"\n{'='*46}\n总计 {len(passed)+len(failed)} 项，通过 {len(passed)}，失败 {len(failed)}")
    for n in failed:
        print("  x", n)
    return 1 if failed else 0

sys.exit(asyncio.run(main()))
