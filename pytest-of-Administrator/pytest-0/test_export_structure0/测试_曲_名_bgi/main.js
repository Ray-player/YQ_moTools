// 由 YQ_moTools 生成：在乐器界面运行，使用游戏默认键位。
(async function () {
  try {
    const sheet = JSON.parse(file.readTextSync("sheet.json"));
    let settings = { countdown_ms: 3000 };
    try {
      settings = JSON.parse(file.readTextSync("settings.json"));
    } catch (e) { /* 用默认值 */ }
    const t0 = Date.now() + (settings.countdown_ms || 0);
    for (const e of sheet.events) {
      const target = t0 + e.t;
      while (Date.now() < target) await sleep(Math.min(target - Date.now(), 40));
      if (e.down) keyDown(e.key); else keyUp(e.key);
    }
    log.info(`播放完成：${sheet.name}`);
  } catch (err) {
    log.error(`播放失败: ${err}`);
  } finally {
    for (const k of "ASDFGHJQWERTYUZXCVBNM") keyUp(k);  // 防卡键
  }
})();
