#!/usr/bin/env node
/**
 * fathom-agent — local agent for the Fathom memory lake.
 *
 * Watches local files and pushes deltas to your lake.
 *
 * Config: ~/.fathom/agent.json
 * Env: FATHOM_API_URL, FATHOM_API_KEY (override config)
 *
 * Usage:
 *   fathom-agent run                  Start watching
 *   fathom-agent run --vault ~/notes  Watch a specific directory
 *   fathom-agent init                 Create default config
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { homedir } from "os";
import { join } from "path";
import { Pusher } from "./pusher.js";
import vault from "./plugins/vault.js";

const CONFIG_DIR = join(homedir(), ".fathom");
const CONFIG_PATH = join(CONFIG_DIR, "agent.json");

// ── Config ───────────────────────────────────────

function loadConfig() {
  const defaults = {
    api_url: "http://localhost:8201",
    api_key: "",
    plugins: {},
  };

  if (existsSync(CONFIG_PATH)) {
    try {
      return { ...defaults, ...JSON.parse(readFileSync(CONFIG_PATH, "utf8")) };
    } catch (e) {
      console.error(`Warning: failed to parse ${CONFIG_PATH}: ${e.message}`);
    }
  }
  return defaults;
}

function saveConfig(config) {
  mkdirSync(CONFIG_DIR, { recursive: true });
  writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2) + "\n");
}

// ── CLI arg parsing ──────────────────────────────

function parseArgs() {
  const args = process.argv.slice(2);
  const result = { command: null, vaultPaths: [] };

  if (!args.length) {
    result.command = "help";
    return result;
  }

  const cmd = args[0];
  if (["run", "--run", "init", "--init", "install", "--install", "uninstall", "--uninstall", "status", "--status", "help", "--help", "-h"].includes(cmd)) {
    result.command = cmd.replace(/^-+/, "");
    let i = 1;
    while (i < args.length) {
      if (args[i] === "--vault" && args[i + 1]) {
        result.vaultPaths.push(args[i + 1]);
        i += 2;
      } else {
        i++;
      }
    }
  } else if (cmd === "--vault") {
    result.command = "run";
    let i = 0;
    while (i < args.length) {
      if (args[i] === "--vault" && args[i + 1]) {
        result.vaultPaths.push(args[i + 1]);
        i += 2;
      } else {
        i++;
      }
    }
  } else {
    console.error(`Unknown command: ${cmd}\nRun 'fathom-agent help' for usage.`);
    process.exit(1);
  }
  return result;
}

function showHelp() {
  console.log(`fathom-agent — local agent for the Fathom memory lake

Commands:
  fathom-agent run                   Start watching (uses ~/.fathom/agent.json)
  fathom-agent run --vault ~/notes   Watch a specific directory
  fathom-agent init                  Create default config
  fathom-agent install               Install as system service (auto-start)
  fathom-agent uninstall             Remove system service
  fathom-agent status                Show config and connection status
  fathom-agent help                  Show this help

Config: ${CONFIG_PATH}
Env:    FATHOM_API_URL, FATHOM_API_KEY (override config values)

Examples:
  fathom-agent init                          # create config
  fathom-agent run                           # start watching
  fathom-agent run --vault ~/obsidian
  fathom-agent install                       # persist as service

Custom plugins: drop .js files in ~/.fathom/plugins/
`);
}

// ── Service installer ────────────────────────────

function getNodePath() {
  return process.execPath;
}

function getAgentScript() {
  return new URL("index.js", import.meta.url).pathname;
}

function installService(config) {
  const platform = process.platform;
  const nodePath = getNodePath();
  const scriptPath = getAgentScript();
  const apiUrl = process.env.FATHOM_API_URL || config.api_url || "http://localhost:8201";
  const apiKey = process.env.FATHOM_API_KEY || config.api_key || "";

  if (platform === "linux") {
    installSystemd(nodePath, scriptPath, apiUrl, apiKey);
  } else if (platform === "darwin") {
    installLaunchd(nodePath, scriptPath, apiUrl, apiKey);
  } else if (platform === "win32") {
    installWindows(nodePath, scriptPath, apiUrl, apiKey);
  } else {
    console.error(`Unsupported platform: ${platform}`);
    process.exit(1);
  }
}

function uninstallService() {
  const platform = process.platform;
  if (platform === "linux") uninstallSystemd();
  else if (platform === "darwin") uninstallLaunchd();
  else if (platform === "win32") uninstallWindows();
  else { console.error(`Unsupported platform: ${platform}`); process.exit(1); }
}

function installSystemd(nodePath, scriptPath, apiUrl, apiKey) {
  const unit = `[Unit]
Description=Fathom Agent — local memory lake watcher
After=network.target

[Service]
Type=simple
ExecStart=${nodePath} ${scriptPath} run
Environment=FATHOM_API_URL=${apiUrl}
Environment=FATHOM_API_KEY=${apiKey}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
`;
  const dir = join(homedir(), ".config", "systemd", "user");
  const path = join(dir, "fathom-agent.service");
  mkdirSync(dir, { recursive: true });
  writeFileSync(path, unit);
  console.log(`Written: ${path}`);
  console.log("\nRun:");
  console.log("  systemctl --user daemon-reload");
  console.log("  systemctl --user enable fathom-agent");
  console.log("  systemctl --user start fathom-agent");
  console.log("  systemctl --user status fathom-agent");
}

function uninstallSystemd() {
  const path = join(homedir(), ".config", "systemd", "user", "fathom-agent.service");
  if (existsSync(path)) {
    console.log("Run:");
    console.log("  systemctl --user stop fathom-agent");
    console.log("  systemctl --user disable fathom-agent");
    console.log(`  rm ${path}`);
    console.log("  systemctl --user daemon-reload");
  } else {
    console.log("No systemd service found.");
  }
}

function installLaunchd(nodePath, scriptPath, apiUrl, apiKey) {
  const label = "com.fathom.agent";
  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${nodePath}</string>
    <string>${scriptPath}</string>
    <string>run</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>FATHOM_API_URL</key>
    <string>${apiUrl}</string>
    <key>FATHOM_API_KEY</key>
    <string>${apiKey}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${join(homedir(), ".fathom", "agent.log")}</string>
  <key>StandardErrorPath</key>
  <string>${join(homedir(), ".fathom", "agent.err")}</string>
</dict>
</plist>
`;
  const dir = join(homedir(), "Library", "LaunchAgents");
  const path = join(dir, `${label}.plist`);
  mkdirSync(dir, { recursive: true });
  writeFileSync(path, plist);
  console.log(`Written: ${path}`);
  console.log("\nRun:");
  console.log(`  launchctl load ${path}`);
  console.log(`  launchctl start ${label}`);
}

function uninstallLaunchd() {
  const label = "com.fathom.agent";
  const path = join(homedir(), "Library", "LaunchAgents", `${label}.plist`);
  if (existsSync(path)) {
    console.log("Run:");
    console.log(`  launchctl stop ${label}`);
    console.log(`  launchctl unload ${path}`);
    console.log(`  rm ${path}`);
  } else {
    console.log("No launchd agent found.");
  }
}

function installWindows(nodePath, scriptPath, apiUrl, apiKey) {
  const batPath = join(homedir(), ".fathom", "fathom-agent.bat");
  const bat = `@echo off
set FATHOM_API_URL=${apiUrl}
set FATHOM_API_KEY=${apiKey}
"${nodePath}" "${scriptPath}" run
`;
  mkdirSync(join(homedir(), ".fathom"), { recursive: true });
  writeFileSync(batPath, bat);
  console.log(`Written: ${batPath}`);
  console.log("\nRun:");
  console.log(`  schtasks /create /tn "FathomAgent" /tr "${batPath}" /sc onlogon /rl limited`);
  console.log(`  schtasks /run /tn "FathomAgent"`);
}

function uninstallWindows() {
  console.log("Run:");
  console.log('  schtasks /delete /tn "FathomAgent" /f');
  const batPath = join(homedir(), ".fathom", "fathom-agent.bat");
  if (existsSync(batPath)) console.log(`  del "${batPath}"`);
}

// ── Main ─────────────────────────────────────────

async function main() {
  const cliArgs = parseArgs();
  const config = loadConfig();

  const apiUrl = process.env.FATHOM_API_URL || config.api_url;
  const apiKey = process.env.FATHOM_API_KEY || config.api_key;

  // ── Command routing ──
  if (cliArgs.command === "help" || cliArgs.command === "h") {
    showHelp();
    process.exit(0);
  }

  if (cliArgs.command === "install") {
    installService(config);
    process.exit(0);
  }

  if (cliArgs.command === "uninstall") {
    uninstallService();
    process.exit(0);
  }

  if (cliArgs.command === "init") {
    const home = homedir();
    config.api_url = apiUrl;
    config.api_key = apiKey;
    config.plugins = {
      vault: {
        enabled: false,
        paths: [
          join(home, "Documents", "notes"),
          join(home, "Documents", "obsidian"),
        ],
        source: "vault",
        tags: ["vault-note"],
        _comment: "Watch markdown directories. Paths that don't exist are ignored.",
      },
    };
    saveConfig(config);
    console.log(`Config written to ${CONFIG_PATH}`);
    console.log(`Enable plugins and adjust paths, then run 'fathom-agent run'.`);
    process.exit(0);
  }

  if (cliArgs.command === "status") {
    console.log(`\nConfig: ${CONFIG_PATH}`);
    console.log(`API:    ${apiUrl}`);
    console.log(`Key:    ${apiKey ? apiKey.slice(0, 8) + "…" : "(not set)"}`);
    const plugins = config.plugins || {};
    console.log(`\nPlugins:`);
    for (const [name, p] of Object.entries(plugins)) {
      const status = p.enabled ? "enabled" : "disabled";
      const detail = p.paths ? ` (${p.paths.length} paths)` : "";
      console.log(`  ${name}: ${status}${detail}`);
    }
    try {
      const r = await fetch(`${apiUrl}/health`);
      console.log(`\nConnection: ${r.ok ? "ok" : r.status}`);
    } catch (e) {
      console.log(`\nConnection: failed (${e.message})`);
    }
    console.log();
    process.exit(0);
  }

  // ── Run ──
  if (cliArgs.command !== "run") {
    showHelp();
    process.exit(0);
  }

  try {
    const r = await fetch(`${apiUrl}/health`);
    if (!r.ok) throw new Error(`${r.status}`);
    console.log(`\nfathom-agent connected to ${apiUrl}`);
  } catch (e) {
    console.error(`\nCannot connect to ${apiUrl}: ${e.message}`);
    console.error("Set FATHOM_API_URL and FATHOM_API_KEY, or edit ~/.fathom/agent.json");
    process.exit(1);
  }

  const pusher = new Pusher(apiUrl, apiKey);
  pusher.start();

  const running = [];

  // CLI quick-start overrides config
  if (cliArgs.vaultPaths.length) {
    const handle = vault.start({ paths: cliArgs.vaultPaths }, pusher);
    if (handle) running.push(handle);
  } else {
    // Load from config
    const plugins = config.plugins || {};

    if (plugins.vault?.enabled && plugins.vault.paths?.length) {
      const handle = vault.start(plugins.vault, pusher);
      if (handle) running.push(handle);
    }

    // Load custom plugins from ~/.fathom/plugins/
    const customDir = join(CONFIG_DIR, "plugins");
    if (existsSync(customDir)) {
      const { readdirSync } = await import("fs");
      for (const file of readdirSync(customDir)) {
        if (!file.endsWith(".js")) continue;
        try {
          const mod = await import(join(customDir, file));
          const plugin = mod.default;
          console.log(`  custom: ${plugin.name || file}`);
          const handle = plugin.start(plugins[plugin.name?.toLowerCase()] || {}, pusher);
          if (handle) running.push(handle);
        } catch (e) {
          console.error(`  custom plugin ${file} failed: ${e.message}`);
        }
      }
    }
  }

  if (!running.length) {
    console.log("\nNo watchers configured. Try:");
    console.log("  fathom-agent run --vault ~/Documents/notes");
    console.log("  fathom-agent init  (create config file)");
    process.exit(0);
  }

  console.log(`\nWatching... (Ctrl+C to stop)\n`);

  setInterval(() => {
    if (pusher.stats.pushed > 0 || pusher.stats.failed > 0) {
      console.log(`  [${new Date().toLocaleTimeString()}] pushed: ${pusher.stats.pushed}, failed: ${pusher.stats.failed}`);
    }
  }, 30000);

  process.on("SIGINT", () => {
    console.log("\nShutting down...");
    running.forEach((h) => h.stop?.());
    pusher.stop();
    process.exit(0);
  });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
