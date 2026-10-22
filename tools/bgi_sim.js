#!/usr/bin/env node
/**
 * BGI JS 导出模拟器（03 §4.4）。
 *
 * 用法：
 *   node tools/bgi_sim.js <导出目录>          # 目录内含 main.js / sheet.json / settings.json
 *   node tools/bgi_sim.js <main.js> [sheet.json]
 *
 * 原理：用虚拟时钟 mock `Date.now` + `sleep`，在 vm 里跑 main.js，
 * 收集 keyDown/keyUp，输出 JSON 事件序列（t 为虚拟毫秒）。
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

function resolveInputs(arg) {
  const p = path.resolve(arg);
  if (fs.statSync(p).isDirectory()) {
    return {
      dir: p,
      main: path.join(p, "main.js"),
      sheet: path.join(p, "sheet.json"),
    };
  }
  return {
    dir: path.dirname(p),
    main: p,
    sheet: process.argv[3] ? path.resolve(process.argv[3]) : path.join(path.dirname(p), "sheet.json"),
  };
}

async function main() {
  const input = process.argv[2];
  if (!input) {
    console.error("用法: node tools/bgi_sim.js <导出目录|main.js> [sheet.json]");
    process.exit(2);
  }

  const { dir, main: mainPath, sheet: sheetPath } = resolveInputs(input);
  const events = [];
  let virtualNow = 0;
  let sleepCalls = 0;

  const sandbox = {
    console,
    JSON,
    Math,
    Promise,
    Date: { now: () => virtualNow },
    file: {
      readTextSync: (name) => {
        const full = path.isAbsolute(name) ? name : path.join(dir, name);
        return fs.readFileSync(full, "utf8");
      },
    },
    sleep: (ms) => {
      sleepCalls += 1;
      if (sleepCalls > 5_000_000) throw new Error("sleep 次数异常，疑似死循环");
      virtualNow += Math.max(0, Number(ms) || 0);
      return Promise.resolve();
    },
    keyDown: (k) => events.push({ t: virtualNow, key: String(k), down: true }),
    keyUp: (k) => events.push({ t: virtualNow, key: String(k), down: false }),
    log: { info: () => {}, warn: () => {}, error: (...a) => events.push({ t: virtualNow, error: a.join(" ") }) },
  };

  const code = fs.readFileSync(mainPath, "utf8");
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: mainPath });

  // 让所有已 resolve 的 await 链在同一 microtask checkpoint 内跑完
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));

  const sheet = JSON.parse(fs.readFileSync(sheetPath, "utf8"));
  const out = {
    script: mainPath,
    sheet: sheetPath,
    virtual_ms: virtualNow,
    event_count: events.filter((e) => !e.error).length,
    errors: events.filter((e) => e.error),
    events,
  };
  process.stdout.write(JSON.stringify(out, null, 2) + "\n");
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
