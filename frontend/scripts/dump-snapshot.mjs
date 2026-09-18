/**
 * dump-snapshot.mjs —— 无浏览器验证"解析 JSON → 同行坐标"核心逻辑。
 * 用 esbuild 把 lib/levelLayout.ts 打包成 mjs，对样本 workflow.json 断言：
 *   1) 同一 Level 的节点 Y 坐标严格相等（同级同行不变量）
 *   2) 同行内 X 按 NODE_W+GAP_X 均匀分布
 *   3) assertSameRow() 自证通过
 * 跑法：cd frontend && node scripts/dump-snapshot.mjs
 */
import { build } from "esbuild";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(fileURLToPath(new URL("..", import.meta.url)));

// 1) 打包 TS 布局模块
await build({
  entryPoints: [path.join(root, "src/lib/levelLayout.ts")],
  bundle: true,
  format: "esm",
  outfile: path.join(root, "node_modules/.cache/levelLayout.mjs"),
});
const { layoutAll, assertSameRow, NODE_W, GAP_X } = await import(
  pathToFileURL(path.join(root, "node_modules/.cache/levelLayout.mjs")).href
);

// 2) 解析样本 JSON（= 画布加载 workflow.json 的同一份数据）
const sample = JSON.parse(readFileSync(path.join(root, "src/sample/mock-demo.json"), "utf-8"));
const layouts = layoutAll(sample.levels);

let fails = 0;
for (const lv of layouts) {
  const ys = new Set(lv.nodes.map((n) => n.position.y));
  if (ys.size !== 1) {
    console.error(`FAIL L${lv.levelIndex}: Y 坐标不一致 ${[...ys]}`);
    fails++;
  }
  // X 均匀分布：相邻节点 ΔX = NODE_W + GAP_X
  for (let i = 1; i < lv.nodes.length; i++) {
    const dx = lv.nodes[i].position.x - lv.nodes[i - 1].position.x;
    if (dx !== NODE_W + GAP_X) {
      console.error(`FAIL L${lv.levelIndex}: 节点 ${i} ΔX=${dx} ≠ ${NODE_W + GAP_X}`);
      fails++;
    }
  }
  console.log(
    `OK L${lv.levelIndex} "${lv.levelName}" y=${lv.nodes[0].position.y} ` +
    lv.nodes.map((n) => `${n.nodeId}@x${n.position.x}`).join(" "),
  );
}
const violations = assertSameRow(layouts);
if (violations.length) { console.error("FAIL 自证:", violations); fails++; }

if (fails === 0) console.log("\n★ 同行不变量全通过（Y 锁行 / X 均布 / 自证 OK）");
process.exit(fails ? 1 : 0);
