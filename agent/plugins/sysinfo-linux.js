/**
 * System health plugin (Linux only).
 *
 * Polls CPU temp, load, memory, disk, battery, and network.
 * Pushes a health snapshot as a single delta on each interval.
 *
 * Config:
 *   interval    — ms between polls (default: 300000 / 5 min)
 *   expiry_days — delta expiry (default: 1)
 *   tags        — extra tags (default: [])
 *   disks       — mount points to check (default: ["/", "/home"])
 *   servers     — URLs to ping (default: [])
 */

import { readFileSync, existsSync, readdirSync } from "fs";
import { execSync } from "child_process";
import { hostname } from "os";

const DEFAULT_INTERVAL = 300000; // 5 minutes
const DEFAULT_DISKS = ["/", "/home"];

export default {
  name: "Sysinfo",
  category: "source",
  icon: "💻",
  type: "poll",
  description: "System health metrics (Linux). CPU temp, load, memory, disk, battery.",

  defaults: {
    interval: 300000,
    expiry_days: 1,
    disks: ["/", "/home"],
    servers: [],
    source: "sysinfo",
    tags: ["health"],
  },

  start(config, pusher) {
    if (process.platform !== "linux") {
      console.log("  sysinfo: skipped (Linux only)");
      return null;
    }

    const interval = config.interval || DEFAULT_INTERVAL;
    const disks = config.disks || DEFAULT_DISKS;
    const servers = config.servers || [];
    const source = config.source || "sysinfo";
    const baseTags = ["health", hostname(), ...(config.tags || [])];

    async function poll() {
      const lines = [];

      // Thermal
      const temps = readTemps();
      if (temps.length) {
        lines.push("Thermal: " + temps.map(t => `${t.label} ${t.value}C`).join(", "));
      }

      // Load
      try {
        const load = readFileSync("/proc/loadavg", "utf8").trim().split(" ");
        lines.push(`Load: ${load[0]} / ${load[1]} / ${load[2]} (1m/5m/15m)`);
      } catch {}

      // Memory
      try {
        const meminfo = readFileSync("/proc/meminfo", "utf8");
        const total = parseInt(meminfo.match(/MemTotal:\s+(\d+)/)?.[1] || "0") / 1024;
        const available = parseInt(meminfo.match(/MemAvailable:\s+(\d+)/)?.[1] || "0") / 1024;
        const swap = parseInt(meminfo.match(/SwapFree:\s+(\d+)/)?.[1] || "0");
        const used = total - available;
        const pct = total > 0 ? Math.round((used / total) * 100) : 0;
        lines.push(`Memory: ${(used / 1024).toFixed(1)}/${(total / 1024).toFixed(1)} GB (${pct}%), swap ${swap} MB`);
      } catch {}

      // Disk
      for (const mount of disks) {
        try {
          const df = execSync(`df -BG "${mount}" 2>/dev/null | tail -1`, { encoding: "utf8" }).trim().split(/\s+/);
          if (df.length >= 5) {
            const usedPct = df[4];
            const avail = df[3];
            lines.push(`Disk ${mount}: ${usedPct} used, ${avail} free`);
          }
        } catch {}
      }

      // IO wait
      try {
        const stat = readFileSync("/proc/stat", "utf8");
        const cpu = stat.match(/^cpu\s+(.+)/m)?.[1]?.split(/\s+/).map(Number);
        if (cpu && cpu.length >= 5) {
          const total = cpu.reduce((a, b) => a + b, 0);
          const iowait = total > 0 ? ((cpu[4] / total) * 100).toFixed(1) : "0.0";
          lines.push(`IO wait: ${iowait}%`);
        }
      } catch {}

      // Battery
      try {
        const bat = readBattery();
        if (bat) lines.push(`Battery: ${bat}`);
      } catch {}

      // Network
      try {
        const route = execSync("ip route show default 2>/dev/null", { encoding: "utf8" }).trim();
        const iface = route.match(/dev\s+(\S+)/)?.[1] || "unknown";
        lines.push(`Network: route up via ${iface}`);
      } catch {}

      // Server pings
      for (const url of servers) {
        try {
          const r = await fetch(url, { signal: AbortSignal.timeout(5000) });
          lines.push(`Server ${new URL(url).hostname}: up (${r.status})`);
        } catch (e) {
          lines.push(`Server ${new URL(url).hostname}: down (${e.constructor.name})`);
        }
      }

      if (!lines.length) return;

      const ts = new Date().toISOString().replace(/\.\d+Z$/, "Z");
      const content = `Laptop health check — ${ts}\n\n${lines.join("\n")}`;

      const delta = { content, tags: baseTags, source };
      const expiryDays = config.expiry_days;
      if (expiryDays != null) {
        delta.expires_at = new Date(Date.now() + expiryDays * 86400000).toISOString();
      }
      pusher.push(delta);

      console.log(`  💻 health snapshot (${lines.length} metrics)`);
    }

    // Poll immediately, then on interval
    poll();
    const timer = setInterval(poll, interval);

    console.log(`  sysinfo: polling every ${interval / 1000}s`);
    return { stop: () => clearInterval(timer) };
  },
};

// ── Helpers ──────────────────────────────────────

function readTemps() {
  const temps = [];
  const base = "/sys/class/thermal";
  if (!existsSync(base)) return temps;

  try {
    for (const zone of readdirSync(base)) {
      if (!zone.startsWith("thermal_zone")) continue;
      const dir = `${base}/${zone}`;
      try {
        const type = readFileSync(`${dir}/type`, "utf8").trim();
        const raw = parseInt(readFileSync(`${dir}/temp`, "utf8").trim());
        const value = raw > 1000 ? raw / 1000 : raw;
        const label = type.includes("cpu") || type.includes("x86") ? "CPU" :
                      type.includes("nvme") ? "NVMe" :
                      type.includes("gpu") ? "GPU" : type;
        temps.push({ label, value: value.toFixed(1) });
      } catch {}
    }
  } catch {}

  return temps;
}

function readBattery() {
  const bat = "/sys/class/power_supply/BAT0";
  if (!existsSync(bat)) return null;

  try {
    const capacity = readFileSync(`${bat}/capacity`, "utf8").trim();
    const status = readFileSync(`${bat}/status`, "utf8").trim();

    let ac = "unknown";
    for (const supply of ["AC0", "ADP0", "ADP1", "ACAD"]) {
      const path = `/sys/class/power_supply/${supply}/online`;
      if (existsSync(path)) {
        ac = readFileSync(path, "utf8").trim() === "1" ? "AC connected" : "AC disconnected";
        break;
      }
    }

    return `${capacity}% (${status}), ${ac}`;
  } catch {
    return null;
  }
}
