/**
 * Inbox client — host-bound API surface helpers use to pull pending
 * dispatches and post replies.
 *
 * Replaces direct lake queries in helper plugins. Plugins that consume
 * dispatches (kitty for claude-code, the future acp plugin) construct
 * one of these via `agent.inbox` and use `.fetch()` / `.reply()`
 * instead of going through the Pusher's lake-write authorization.
 *
 * Why a separate client (not bolted onto Pusher):
 *   · Different token. Pusher carries the lake-scope api_key; Inbox
 *     carries the helper-scope helper_token. Mixing them in one
 *     object would invite the wrong token reaching the wrong call.
 *   · Different shape. Pusher speaks raw delta semantics; Inbox
 *     speaks dispatch / reply. Helper plugins shouldn't know about
 *     lake tags — that's the whole point of phase 2.
 *
 * If `helperToken` is empty, every method throws with a clear message
 * pointing at the agent.json field. We don't fall back to pusher —
 * the security model REQUIRES the helper-scoped path.
 */

export class Inbox {
  constructor({ apiUrl, helperToken, host }) {
    this.apiUrl = (apiUrl || "").replace(/\/$/, "");
    this.helperToken = helperToken || "";
    this.host = host || "";
  }

  _checkConfigured() {
    if (!this.apiUrl) throw new Error("inbox: api_url not configured");
    if (!this.host) throw new Error("inbox: host not configured");
    if (!this.helperToken) {
      throw new Error(
        "inbox: helper_token not configured — mint one via " +
          "POST /v1/admin/helpers/<host>/tokens and add it to agent.json " +
          "as `helper_token`",
      );
    }
  }

  _headers() {
    return {
      "Content-Type": "application/json",
      Authorization: `Bearer ${this.helperToken}`,
    };
  }

  /**
   * Pull pending dispatches for this host.
   *
   * @param {object} opts
   * @param {string} [opts.since] — ISO8601 cursor; pass back the previous response's `cursor`.
   * @param {number} [opts.limit] — max items (default 50, capped at 200 server-side).
   * @param {string} [opts.role] — optional role filter for plugins handling one role.
   * @param {number} [opts.timeoutMs] — request timeout (default 5000).
   * @returns {Promise<{items: object[], cursor: string}>}
   */
  async fetch({ since, limit = 50, role, timeoutMs = 5000 } = {}) {
    this._checkConfigured();
    const url = new URL(`${this.apiUrl}/v1/helpers/${encodeURIComponent(this.host)}/inbox`);
    if (since) url.searchParams.set("since", since);
    if (limit) url.searchParams.set("limit", String(limit));
    if (role) url.searchParams.set("role", role);
    const r = await fetch(url, {
      headers: this._headers(),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!r.ok) {
      const body = await r.text().catch(() => "");
      throw new Error(`inbox.fetch: HTTP ${r.status} ${r.statusText} — ${body.slice(0, 300)}`);
    }
    return await r.json();
  }

  /**
   * Post a reply for a dispatch — update / complete / error.
   *
   * @param {string} corr — task correlation id from the inbox item.
   * @param {object} body
   * @param {"update"|"complete"|"error"} body.kind
   * @param {string} body.content — reply text.
   * @param {string[]} [body.extra_tags] — additional tags; server-side allowlist drops anything off-list.
   * @returns {Promise<{delta_id: string}>}
   */
  async reply(corr, { kind, content, extra_tags = [] } = {}) {
    this._checkConfigured();
    if (!corr) throw new Error("inbox.reply: corr is required");
    if (!kind) throw new Error("inbox.reply: kind is required");
    const url = `${this.apiUrl}/v1/helpers/${encodeURIComponent(this.host)}/inbox/${encodeURIComponent(corr)}/reply`;
    const r = await fetch(url, {
      method: "POST",
      headers: this._headers(),
      body: JSON.stringify({ kind, content: content || "", extra_tags }),
      signal: AbortSignal.timeout(5000),
    });
    if (!r.ok) {
      const body = await r.text().catch(() => "");
      throw new Error(`inbox.reply: HTTP ${r.status} ${r.statusText} — ${body.slice(0, 300)}`);
    }
    return await r.json();
  }
}
