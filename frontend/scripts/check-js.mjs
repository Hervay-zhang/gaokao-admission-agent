import { readdir } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

async function collect(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...await collect(full));
    else if (entry.isFile() && entry.name.endsWith(".js")) out.push(full);
  }
  return out;
}

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const files = await collect(path.join(root, "js"));
let failed = 0;
for (const file of files) {
  const result = spawnSync(process.execPath, ["--check", file], { stdio: "inherit" });
  if (result.status !== 0) failed += 1;
}
if (failed) {
  console.error(`JavaScript 语法检查失败：${failed} 个文件`);
  process.exit(1);
}
console.log(`JavaScript 语法检查通过：${files.length} 个文件`);
