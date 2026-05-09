/**
 * Minimal Agent Client Protocol (ACP) client over stdio.
 *
 * ACP is JSON-RPC 2.0 with newline-delimited framing (not LSP's
 * Content-Length headers — each message is one line of JSON, no
 * embedded newlines). The client spawns the agent as a subprocess,
 * writes requests to its stdin, and reads responses + notifications
 * from its stdout. stderr gets piped through to our stderr for
 * visibility into adapter-side errors.
 *
 * This is a *minimal* implementation. It supports:
 *   · initialize / authenticate
 *   · session/new / session/prompt
 *   · streaming session/update notifications
 *   · auto-refusing client-method calls (fs/*, terminal/*,
 *     session/request_permission) — phase 3 is headless; tool support
 *     ships as a separate slice
 *
 * Reference:
 *   https://agentclientprotocol.com/protocol/overview
 *   https://agentclientprotocol.com/protocol/transports
 */

import { spawn } from "child_process";
import { createInterface } from "readline";

export const ACP_PROTOCOL_VERSION = 1;

// JSON-RPC 2.0 error codes used in our refusals.
const ERR_METHOD_NOT_FOUND = -32601;
const ERR_INVALID_REQUEST = -32600;

export class AcpClient {
  /**
   * @param {object} opts
   * @param {string} opts.command — adapter binary, e.g. "npx" or "/usr/bin/claude-code-acp"
   * @param {string[]} [opts.args] — arguments to the binary
   * @param {object} [opts.env] — env vars merged onto process.env for the subprocess
   * @param {string} [opts.cwd]
   * @param {(update: object) => void} [opts.onUpdate] — fires on every session/update
   * @param {(method: string, params: any) => any} [opts.onClientMethod]
   *   — handler for client-side method requests (fs/read_text_file etc).
   *     Return value is the JSON-RPC result; throw to send an error.
   *     If absent, all client methods are refused with method-not-found.
   * @param {(line: string) => void} [opts.onStderrLine] — defaults to console.error.
   */
  constructor({ command, args = [], env, cwd, onUpdate, onClientMethod, onStderrLine }) {
    this.command = command;
    this.args = args;
    this.env = env ? { ...process.env, ...env } : process.env;
    this.cwd = cwd;
    this.onUpdate = onUpdate || (() => {});
    this.onClientMethod = onClientMethod || null;
    this.onStderrLine = onStderrLine || ((line) => console.error(`  [acp:stderr] ${line}`));

    this.child = null;
    this.pending = new Map(); // id → { resolve, reject }
    this.nextId = 1;
    this.closed = false;
    this.exitPromise = null;
    this._stdoutBuffer = "";
  }

  spawn() {
    if (this.child) throw new Error("acp_client: already spawned");
    this.child = spawn(this.command, this.args, {
      stdio: ["pipe", "pipe", "pipe"],
      env: this.env,
      cwd: this.cwd,
    });

    // stdout: parse newline-delimited JSON-RPC.
    this.child.stdout.setEncoding("utf8");
    this.child.stdout.on("data", (chunk) => this._onStdoutChunk(chunk));

    // stderr: line-buffered passthrough so adapter logs surface.
    const errRl = createInterface({ input: this.child.stderr });
    errRl.on("line", (line) => this.onStderrLine(line));

    this.exitPromise = new Promise((resolve) => {
      this.child.on("exit", (code, signal) => {
        this.closed = true;
        for (const { reject } of this.pending.values()) {
          reject(new Error(`acp_client: subprocess exited (code=${code} signal=${signal})`));
        }
        this.pending.clear();
        resolve({ code, signal });
      });
    });
    this.child.on("error", (e) => {
      this.closed = true;
      for (const { reject } of this.pending.values()) reject(e);
      this.pending.clear();
    });
  }

  _onStdoutChunk(chunk) {
    this._stdoutBuffer += chunk;
    let idx;
    while ((idx = this._stdoutBuffer.indexOf("\n")) !== -1) {
      const line = this._stdoutBuffer.slice(0, idx).trim();
      this._stdoutBuffer = this._stdoutBuffer.slice(idx + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch (e) {
        console.error(`  [acp] failed to parse line: ${e.message} — ${line.slice(0, 200)}`);
        continue;
      }
      this._handleMessage(msg);
    }
  }

  _handleMessage(msg) {
    // Response to a request we sent
    if (msg.id !== undefined && (msg.result !== undefined || msg.error !== undefined)) {
      const entry = this.pending.get(msg.id);
      if (!entry) return;
      this.pending.delete(msg.id);
      if (msg.error) {
        entry.reject(
          Object.assign(new Error(msg.error.message || "ACP error"), {
            code: msg.error.code,
            data: msg.error.data,
          })
        );
      } else {
        entry.resolve(msg.result);
      }
      return;
    }
    // Method call from the agent (notification or request)
    if (typeof msg.method === "string") {
      if (msg.method === "session/update") {
        // Notification — fire-and-forget.
        try {
          this.onUpdate(msg.params || {});
        } catch (e) {
          console.error(`  [acp] onUpdate handler threw: ${e.message}`);
        }
        return;
      }
      // Other methods are client-side requests (fs/*, terminal/*,
      // session/request_permission). If a handler is registered, dispatch;
      // otherwise refuse with method-not-found so the agent knows the
      // capability is unavailable.
      if (msg.id !== undefined) {
        this._handleClientRequest(msg).catch((e) =>
          console.error(`  [acp] client request handler crashed: ${e.message}`)
        );
      }
    }
  }

  async _handleClientRequest(msg) {
    if (!this.onClientMethod) {
      this._send({
        jsonrpc: "2.0",
        id: msg.id,
        error: {
          code: ERR_METHOD_NOT_FOUND,
          message: `client method ${msg.method} is not supported in this build`,
        },
      });
      return;
    }
    try {
      const result = await this.onClientMethod(msg.method, msg.params || {});
      this._send({ jsonrpc: "2.0", id: msg.id, result: result ?? null });
    } catch (e) {
      this._send({
        jsonrpc: "2.0",
        id: msg.id,
        error: {
          code: e.code || ERR_INVALID_REQUEST,
          message: e.message || String(e),
        },
      });
    }
  }

  _send(obj) {
    if (this.closed || !this.child || !this.child.stdin.writable) {
      throw new Error("acp_client: subprocess is closed");
    }
    const line = JSON.stringify(obj);
    if (line.includes("\n")) {
      // Hard-fail rather than ship a malformed frame; the protocol
      // requires no embedded newlines.
      throw new Error("acp_client: serialized message contains a newline");
    }
    this.child.stdin.write(line + "\n");
  }

  /** Send a request and await its response. */
  async request(method, params = {}, { timeoutMs = 120_000 } = {}) {
    if (this.closed) throw new Error("acp_client: subprocess is closed");
    const id = this.nextId++;
    let entry;
    const promise = new Promise((resolve, reject) => {
      entry = { resolve, reject };
      this.pending.set(id, entry);
    });
    this._send({ jsonrpc: "2.0", id, method, params });
    let timer = null;
    if (timeoutMs > 0) {
      timer = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          entry.reject(new Error(`acp_client: request ${method} timed out after ${timeoutMs}ms`));
        }
      }, timeoutMs);
      timer.unref?.();
    }
    try {
      return await promise;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  /** Send a notification (no id, no response expected). */
  notify(method, params = {}) {
    this._send({ jsonrpc: "2.0", method, params });
  }

  // ── ACP convenience methods ─────────────────────────────────────

  async initialize({ clientCapabilities = {} } = {}) {
    return await this.request("initialize", {
      protocolVersion: ACP_PROTOCOL_VERSION,
      clientCapabilities,
    });
  }

  async sessionNew({ cwd = process.cwd(), mcpServers = [] } = {}) {
    return await this.request("session/new", { cwd, mcpServers });
  }

  /**
   * Send a prompt and await the final response. session/update
   * notifications fire onUpdate as they stream in.
   */
  async sessionPrompt({ sessionId, prompt }, { timeoutMs = 600_000 } = {}) {
    return await this.request("session/prompt", { sessionId, prompt }, { timeoutMs });
  }

  /** Close stdin to end the subprocess cleanly, then await its exit. */
  async close() {
    if (this.closed) return;
    try {
      this.child.stdin.end();
    } catch {}
    return await this.exitPromise;
  }

  /** Force-kill (SIGTERM by default). */
  kill(signal = "SIGTERM") {
    if (this.closed || !this.child) return;
    try {
      this.child.kill(signal);
    } catch {}
  }
}
