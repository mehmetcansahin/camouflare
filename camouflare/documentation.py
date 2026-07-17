from __future__ import annotations

V1_ENDPOINT_DESCRIPTION = """
Run a FlareSolverr-compatible command.

Supported commands:
- `sessions.create`: create or reuse a persistent browser session.
- `sessions.list`: list active persistent session ids.
- `sessions.destroy`: close and remove a persistent browser session.
- `request.get`: navigate to a URL and return the solved page payload.
- `request.post`: submit form or raw JSON data to a URL and return the solved page payload.

The endpoint always returns the Camouflare envelope with `status`, `message`,
timestamps, and `version`. Browser capacity timeouts return HTTP 503. Invalid
commands, missing required command fields, and session errors return the same
envelope with HTTP 500.
""".strip()

V1_REQUEST_EXAMPLES = {
    "request.get": {
        "summary": "Fetch a page",
        "description": "Navigate to a URL without a persistent session.",
        "value": {
            "cmd": "request.get",
            "url": "https://example.com",
            "maxTimeout": 60000,
        },
    },
    "request.post": {
        "summary": "Submit a form",
        "description": (
            "Submit URL-encoded form data by default, or raw JSON when the "
            "target Content-Type is application/json or +json. Requires both "
            "url and postData."
        ),
        "value": {
            "cmd": "request.post",
            "url": "https://example.com/login",
            "postData": "username=alice&password=secret",
        },
    },
    "sessions.create": {
        "summary": "Create a session",
        "description": "Create or reuse a persistent browser context.",
        "value": {"cmd": "sessions.create", "session": "account-a"},
    },
    "sessions.list": {
        "summary": "List sessions",
        "description": "Return sorted active persistent session ids.",
        "value": {"cmd": "sessions.list"},
    },
    "sessions.destroy": {
        "summary": "Destroy a session",
        "description": "Close and remove a persistent browser context.",
        "value": {"cmd": "sessions.destroy", "session": "account-a"},
    },
}

DOCUMENTATION_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Camouflare API Documentation</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --ink: #182230;
      --muted: #5d6b82;
      --line: #d8dee8;
      --accent: #126b58;
      --accent-soft: #e5f4ef;
      --code-bg: #111827;
      --code-ink: #edf2f7;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family:
        Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      line-height: 1.55;
    }

    header {
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }

    main,
    .hero {
      width: min(1120px, calc(100% - 32px));
      margin: 0 auto;
    }

    .hero {
      padding: 40px 0 28px;
    }

    h1,
    h2,
    h3 {
      line-height: 1.18;
      margin: 0;
      letter-spacing: 0;
    }

    h1 {
      max-width: 780px;
      font-size: clamp(2rem, 6vw, 4.2rem);
    }

    h2 {
      padding-top: 28px;
      margin-top: 24px;
      border-top: 1px solid var(--line);
      font-size: 1.45rem;
    }

    h3 {
      margin-top: 22px;
      font-size: 1.05rem;
    }

    p {
      max-width: 820px;
      margin: 10px 0;
    }

    a {
      color: var(--accent);
      font-weight: 650;
    }

    .lede {
      color: var(--muted);
      font-size: 1.08rem;
    }

    .links {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 22px;
    }

    .links a,
    .pill {
      display: inline-flex;
      min-height: 36px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 7px 11px;
      background: var(--panel);
      text-decoration: none;
    }

    main {
      display: grid;
      grid-template-columns: 230px minmax(0, 1fr);
      gap: 34px;
      padding: 28px 0 56px;
    }

    nav {
      position: sticky;
      top: 12px;
      align-self: start;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 14px;
    }

    nav a {
      display: block;
      padding: 7px 8px;
      border-radius: 6px;
      color: var(--muted);
      text-decoration: none;
    }

    nav a:hover {
      background: var(--accent-soft);
      color: var(--accent);
    }

    section {
      min-width: 0;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin: 14px 0;
    }

    .box {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 14px;
    }

    .method {
      display: inline-flex;
      min-width: 54px;
      justify-content: center;
      border-radius: 6px;
      padding: 3px 7px;
      background: var(--accent-soft);
      color: var(--accent);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.82rem;
      font-weight: 700;
    }

    code,
    pre {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }

    code {
      border-radius: 5px;
      background: #eef2f6;
      padding: 2px 5px;
      font-size: 0.92em;
    }

    pre {
      overflow: auto;
      border-radius: 8px;
      margin: 12px 0;
      padding: 15px;
      background: var(--code-bg);
      color: var(--code-ink);
      font-size: 0.9rem;
    }

    pre code {
      background: transparent;
      padding: 0;
      color: inherit;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      margin: 12px 0 18px;
      background: var(--panel);
    }

    th,
    td {
      border: 1px solid var(--line);
      padding: 9px 10px;
      text-align: left;
      vertical-align: top;
    }

    th {
      background: #eef2f6;
    }

    ul {
      margin: 8px 0 16px;
      padding-left: 22px;
    }

    @media (max-width: 780px) {
      main {
        display: block;
      }

      nav {
        position: static;
        margin-bottom: 18px;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="hero">
      <p class="pill">FlareSolverr-compatible API</p>
      <h1>Camouflare API Documentation</h1>
      <p class="lede">
        Camouflare exposes a FastAPI service for browser-backed requests,
        persistent sessions, proxy routing, cookie capture, screenshots,
        lightweight liveness checks, browser-readiness checks, optional challenge
        handling, passive diagnostics, and optional Prometheus metrics.
      </p>
      <p class="lede">
        Use Camouflare only on systems you own, administer, or have permission
        to test. Active challenge handling is disabled by default and must be
        enabled explicitly with <code>CHALLENGE_SOLVER=click</code>.
      </p>
      <div class="links">
        <a href="/docs">Swagger UI</a>
        <a href="/redoc">ReDoc</a>
        <a href="/openapi.json">OpenAPI JSON</a>
        <a href="/health">Health</a>
        <a href="/ready">Ready</a>
        <a href="/diagnostics">Diagnostics</a>
      </div>
    </div>
  </header>

  <main>
    <nav aria-label="Documentation sections">
      <a href="#quick-start">Quick start</a>
      <a href="#authentication">Authentication</a>
      <a href="#endpoints">Endpoints</a>
      <a href="#commands">Commands</a>
      <a href="#command-examples">Command examples</a>
      <a href="#request-fields">Request fields</a>
      <a href="#responses">Responses</a>
      <a href="#error-reference">Errors</a>
      <a href="#sessions">Sessions</a>
      <a href="#proxy">Proxy</a>
      <a href="#configuration">Configuration</a>
      <a href="#compatibility">Compatibility</a>
    </nav>

    <section>
      <h2 id="quick-start">Quick Start</h2>
      <p>
        Send all FlareSolverr-style commands to <code>POST /v1</code>.
        The response keeps the familiar <code>status</code>,
        <code>message</code>, <code>solution</code>,
        <code>startTimestamp</code>, and <code>endTimestamp</code> envelope.
      </p>
<pre><code>curl -L -X POST 'http://localhost:8191/v1' \\
  -H 'Content-Type: application/json' \\
  -H 'Authorization: Bearer &lt;token&gt;' \\
  --data-raw '{
    "cmd": "request.get",
    "url": "https://example.com",
    "maxTimeout": 60000
  }'</code></pre>

      <h2 id="authentication">Authentication</h2>
      <p>
        Set <code>CAMOUFLARE_API_TOKEN</code> to require a token for every
        endpoint except <code>/health</code>. Send the token as
        <code>Authorization: Bearer &lt;token&gt;</code> or
        <code>X-API-Token: &lt;token&gt;</code>. When the environment variable is
        unset, Camouflare keeps the unauthenticated local-development behavior
        and binds to loopback. A non-loopback <code>HOST</code> requires a token.
      </p>

      <h2 id="endpoints">Endpoints</h2>
      <div class="grid">
        <div class="box">
          <p><span class="method">GET</span> <code>/</code></p>
          <p>Returns service metadata, including the configured version.</p>
        </div>
        <div class="box">
          <p><span class="method">GET</span> <code>/documentation</code></p>
          <p>Serves this expanded documentation page.</p>
        </div>
        <div class="box">
          <p><span class="method">GET</span> <code>/health</code></p>
          <p>
            Returns a lightweight process-only liveness response without reading
            browser state.
          </p>
        </div>
        <div class="box">
          <p><span class="method">GET</span> <code>/ready</code></p>
          <p>Runs the browser-readiness probe by creating a page and evaluating JS.</p>
        </div>
        <div class="box">
          <p><span class="method">GET</span> <code>/diagnostics</code></p>
          <p>
            Returns a passive, token-protected pool, session, cleanup, and runtime
            snapshot without leasing browser capacity. A successful snapshot uses
            HTTP 200 even when <code>capacity_state</code> is not available.
          </p>
        </div>
        <div class="box">
          <p><span class="method">GET</span> <code>/metrics</code></p>
          <p>Returns Prometheus metrics when <code>PROMETHEUS_ENABLED=true</code>.</p>
        </div>
        <div class="box">
          <p><span class="method">POST</span> <code>/v1</code></p>
          <p>Runs all session and browser request commands.</p>
        </div>
      </div>

      <h2 id="commands">Commands</h2>
      <table>
        <thead>
          <tr>
            <th>Command</th>
            <th>Required fields</th>
            <th>Use case</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><code>sessions.create</code></td>
            <td>Optional <code>session</code></td>
            <td>Create a persistent browser context for later requests.</td>
          </tr>
          <tr>
            <td><code>sessions.list</code></td>
            <td>None</td>
            <td>List active persistent session identifiers.</td>
          </tr>
          <tr>
            <td><code>sessions.destroy</code></td>
            <td><code>session</code></td>
            <td>Close and remove a persistent browser context.</td>
          </tr>
          <tr>
            <td><code>request.get</code></td>
            <td><code>url</code></td>
            <td>Open a URL and return the browser-backed page response.</td>
          </tr>
          <tr>
            <td><code>request.post</code></td>
            <td><code>url</code>, <code>postData</code></td>
            <td>Submit form or raw JSON data and return the browser-backed page response.</td>
          </tr>
        </tbody>
      </table>

      <h2 id="command-examples">Command Examples</h2>
      <h3 id="sessions-create"><code>sessions.create</code></h3>
      <p>
        Creates a named persistent browser context. If the session already
        exists, Camouflare returns the same id with an ok envelope.
      </p>
      <pre><code>{
  "cmd": "sessions.create",
  "session": "account-a",
  "session_ttl_minutes": 240,
  "proxy": {
    "url": "http://proxy.example:8080",
    "username": "user",
    "password": "pass"
  }
}</code></pre>

      <h3 id="sessions-list"><code>sessions.list</code></h3>
      <p>Returns sorted active session ids.</p>
      <pre><code>{
  "cmd": "sessions.list"
}</code></pre>
      <pre><code>{
  "status": "ok",
  "sessions": ["account-a"],
  "version": "1.2.0"
}</code></pre>

      <h3 id="sessions-destroy"><code>sessions.destroy</code></h3>
      <p>
        Closes the persistent browser context and removes the session id.
        Missing sessions return the standard error envelope.
      </p>
      <pre><code>{
  "cmd": "sessions.destroy",
  "session": "account-a"
}</code></pre>

      <h3 id="request-get"><code>request.get</code></h3>
      <p>
        Opens <code>url</code>, waits for the DOM content event, performs a
        best-effort network-idle wait, optionally waits
        <code>waitInSeconds</code>, then collects HTML, headers, cookies,
        user agent, and optional screenshot.
      </p>
      <pre><code>{
  "cmd": "request.get",
  "url": "https://example.com",
  "maxTimeout": 60000,
  "returnScreenshot": true,
  "disableMedia": true
}</code></pre>

      <h3 id="request-post"><code>request.post</code></h3>
      <p>
        Submits <code>postData</code> to <code>url</code>. This command requires both
        <code>url</code> and <code>postData</code>. By default, the body is sent as
        URL-encoded form data. If <code>headers.Content-Type</code> is
        <code>application/json</code> or another <code>+json</code> media type,
        <code>postData</code> is sent as the raw request body.
        The POST is performed as a browser document navigation so JavaScript,
        waits, screenshots, and challenge handling apply to the resulting page.
      </p>
      <pre><code>{
  "cmd": "request.post",
  "url": "https://example.com/login",
  "postData": "username=alice&amp;password=secret",
  "maxTimeout": 60000
}</code></pre>

      <h2 id="request-fields">Request Fields</h2>
      <table>
        <thead>
          <tr>
            <th>Field</th>
            <th>Type</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><code>cmd</code></td>
            <td>string</td>
            <td>One of the supported commands listed above.</td>
          </tr>
          <tr>
            <td><code>url</code></td>
            <td>string</td>
            <td>Required for <code>request.get</code> and <code>request.post</code>.</td>
          </tr>
          <tr>
            <td><code>maxTimeout</code></td>
            <td>integer</td>
            <td>Maximum request time in milliseconds. Default: <code>60000</code>.</td>
          </tr>
          <tr>
            <td><code>session</code></td>
            <td>string</td>
            <td>Reuse or target a persistent session.</td>
          </tr>
          <tr>
            <td><code>session_ttl_minutes</code></td>
            <td>integer</td>
            <td>Override the default session TTL for the requested session.</td>
          </tr>
          <tr>
            <td><code>proxy</code></td>
            <td>object</td>
            <td>Supports <code>url</code> or <code>server</code>, plus credentials.</td>
          </tr>
          <tr>
            <td><code>cookies</code></td>
            <td>array</td>
            <td>Cookies to inject before navigation.</td>
          </tr>
          <tr>
            <td><code>headers</code></td>
            <td>object</td>
            <td>
              HTTP headers to apply before navigation. <code>User-Agent</code>
              configures the context, and <code>Referer</code>/<code>Referrer</code>
              is passed to navigation.
            </td>
          </tr>
          <tr>
            <td><code>userAgent</code></td>
            <td>string</td>
            <td>
              Browser User-Agent override for new contexts. Takes precedence over
              <code>User-Agent</code> in <code>headers</code>.
            </td>
          </tr>
          <tr>
            <td><code>returnOnlyCookies</code></td>
            <td>boolean</td>
            <td>
              When true, returnOnlyCookies omits response HTML, headers, and
              screenshots from <code>solution</code>.
            </td>
          </tr>
          <tr>
            <td><code>returnScreenshot</code></td>
            <td>boolean</td>
            <td>Include a screenshot in the solution payload.</td>
          </tr>
          <tr>
            <td><code>waitInSeconds</code></td>
            <td>integer</td>
            <td>Wait after page load before collecting the response.</td>
          </tr>
          <tr>
            <td><code>disableMedia</code></td>
            <td>boolean</td>
            <td>Block media resources for lighter page loads.</td>
          </tr>
          <tr>
            <td><code>postData</code></td>
            <td>string</td>
            <td>
              Required for <code>request.post</code>. Use URL-encoded form data
              such as <code>username=alice&amp;password=secret</code>, or raw JSON
              when the target <code>Content-Type</code> is <code>application/json</code>
              or another <code>+json</code> media type.
            </td>
          </tr>
        </tbody>
      </table>

      <h2 id="responses">Responses</h2>
      <p>Successful requests return <code>status: "ok"</code> and a solution.</p>
      <pre><code>{
  "status": "ok",
  "message": "Challenge solved!",
  "solution": {
    "url": "https://example.com",
    "status": 200,
    "response": "&lt;html&gt;...&lt;/html&gt;",
    "cookies": [],
    "userAgent": "Mozilla/5.0 ..."
  },
  "startTimestamp": 1770000000000,
  "endTimestamp": 1770000001500,
  "version": "1.2.0"
}</code></pre>
      <p>
        Errors use the same envelope with <code>status: "error"</code>.
        Pool saturation returns HTTP 503; malformed commands return HTTP 500.
      </p>

      <h2 id="error-reference">Error Reference</h2>
      <table>
        <thead>
          <tr>
            <th>Condition</th>
            <th>HTTP status</th>
            <th>Envelope</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>Browser pool capacity is unavailable before timeout.</td>
            <td>HTTP 503</td>
            <td><code>{"status":"error","message":"Error: ..."}</code></td>
          </tr>
          <tr>
            <td>Missing <code>cmd</code>, invalid command, missing required field.</td>
            <td>HTTP 500</td>
            <td><code>{"status":"error","message":"Error: ..."}</code></td>
          </tr>
          <tr>
            <td><code>sessions.destroy</code> targets an unknown session.</td>
            <td>HTTP 500</td>
            <td><code>{"status":"error","message":"Error: The session doesn't exist."}</code></td>
          </tr>
          <tr>
            <td>Challenge solve or requested wait exceeds <code>maxTimeout</code>.</td>
            <td>HTTP 500</td>
            <td>May include a partial <code>solution</code> for debugging.</td>
          </tr>
        </tbody>
      </table>

      <h2 id="sessions">Sessions</h2>
      <p>
        Sessions keep a persistent browser context behind a named id. Requests
        that share a session are serialized with a per-session lock, so cookies
        and browser state are preserved without concurrent page races.
      </p>
      <pre><code>curl -L -X POST 'http://localhost:8191/v1' \\
  -H 'Content-Type: application/json' \\
  --data-raw '{"cmd":"sessions.create","session":"account-a"}'</code></pre>

      <h2 id="proxy">Proxy</h2>
      <p>
        Configure a default proxy with environment variables or send a
        request-level <code>proxy</code> object. Session rotation preserves the
        session's existing proxy so later requests keep the same egress path.
        Request-level proxy settings take precedence over environment defaults.
        Use <code>url</code> or <code>server</code> for the proxy endpoint.
      </p>
      <pre><code>{
  "cmd": "request.get",
  "url": "https://example.com",
  "proxy": {
    "url": "http://proxy.example:8080",
    "username": "user",
    "password": "pass"
  }
}</code></pre>

      <h2 id="configuration">Configuration</h2>
      <table>
        <thead>
          <tr>
            <th>Variable</th>
            <th>Default</th>
            <th>Purpose</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><code>HOST</code></td>
            <td><code>127.0.0.1</code></td>
            <td>Bind host. Non-loopback values require an API token.</td>
          </tr>
          <tr>
            <td><code>PORT</code></td>
            <td><code>8191</code></td>
            <td>Bind port.</td>
          </tr>
          <tr>
            <td><code>LOG_LEVEL</code></td>
            <td><code>INFO</code></td>
            <td>Logging level.</td>
          </tr>
          <tr>
            <td><code>CAMOUFLARE_API_TOKEN</code></td>
            <td>unset</td>
            <td>
              Required for every endpoint except <code>/health</code> when set,
              and required whenever <code>HOST</code> is not loopback.
            </td>
          </tr>
          <tr>
            <td><code>HEADLESS</code></td>
            <td>Linux: <code>virtual</code>; other OSes: <code>true</code></td>
            <td>
              Camoufox mode. Accepts <code>true</code>, <code>false</code>, or
              <code>virtual</code>; <code>virtual</code> uses Xvfb and is Linux-only.
            </td>
          </tr>
          <tr>
            <td><code>PROXY_URL</code> / <code>PROXY_SERVER</code></td>
            <td>unset</td>
            <td>Default proxy endpoint for browser contexts.</td>
          </tr>
          <tr>
            <td><code>PROXY_USERNAME</code></td>
            <td>unset</td>
            <td>Default proxy username.</td>
          </tr>
          <tr>
            <td><code>PROXY_PASSWORD</code></td>
            <td>unset</td>
            <td>Default proxy password.</td>
          </tr>
          <tr>
            <td><code>POOL_MIN_BROWSERS</code></td>
            <td><code>1</code></td>
            <td>Warm pool size.</td>
          </tr>
          <tr>
            <td><code>POOL_MAX_BROWSERS</code></td>
            <td><code>2</code></td>
            <td>Max browsers.</td>
          </tr>
          <tr>
            <td><code>MAX_SESSIONS</code></td>
            <td><code>32</code></td>
            <td>Session cap.</td>
          </tr>
          <tr>
            <td><code>SESSION_TTL_MINUTES</code></td>
            <td><code>60</code></td>
            <td>Session TTL.</td>
          </tr>
          <tr>
            <td><code>MAX_REQUEST_BODY_BYTES</code></td>
            <td><code>4194304</code></td>
            <td>Maximum streamed JSON request body.</td>
          </tr>
          <tr>
            <td><code>MAX_RESPONSE_BODY_BYTES</code></td>
            <td><code>33554432</code></td>
            <td>Maximum returned HTML, JSON, XML, or text body.</td>
          </tr>
          <tr>
            <td><code>MAX_SCREENSHOT_BYTES</code></td>
            <td><code>16777216</code></td>
            <td>Maximum raw PNG size before base64 encoding.</td>
          </tr>
          <tr>
            <td><code>MAX_SOLUTION_BYTES</code></td>
            <td><code>67108864</code></td>
            <td>Maximum serialized FlareSolverr response envelope.</td>
          </tr>
          <tr>
            <td><code>MAX_TIMEOUT_MS</code></td>
            <td><code>300000</code></td>
            <td>Upper bound accepted for <code>maxTimeout</code>.</td>
          </tr>
          <tr>
            <td><code>MAX_SESSION_TTL_MINUTES</code></td>
            <td><code>1440</code></td>
            <td>Upper bound accepted for per-session TTL.</td>
          </tr>
          <tr>
            <td><code>SESSION_REAPER_INTERVAL_SECONDS</code></td>
            <td><code>30</code></td>
            <td>Expired-session cleanup interval.</td>
          </tr>
          <tr>
            <td><code>SHUTDOWN_TIMEOUT_SECONDS</code></td>
            <td><code>30</code></td>
            <td>Shared browser and session shutdown deadline.</td>
          </tr>
          <tr>
            <td><code>CLEANUP_TIMEOUT_SECONDS</code></td>
            <td><code>10</code></td>
            <td>Hard deadline for physical cleanup while logical capacity is released.</td>
          </tr>
          <tr>
            <td><code>READINESS_TIMEOUT_MS</code></td>
            <td><code>15000</code></td>
            <td>Total hard deadline for the active browser readiness probe.</td>
          </tr>
          <tr>
            <td><code>LOG_FORMAT</code></td>
            <td><code>text</code></td>
            <td>Logging format: <code>text</code> or <code>json</code>.</td>
          </tr>
          <tr>
            <td><code>PROMETHEUS_ENABLED</code></td>
            <td><code>false</code></td>
            <td>Metrics toggle.</td>
          </tr>
          <tr>
            <td><code>CHALLENGE_SOLVER</code></td>
            <td><code>none</code></td>
            <td>
              Challenge handler. <code>none</code> leaves active solving disabled;
              <code>click</code> opts in to ClickSolver-backed challenge handling.
            </td>
          </tr>
        </tbody>
      </table>

      <h2 id="compatibility">Compatibility Notes</h2>
      <ul>
        <li>
          Camouflare is a single-user, single-worker local service. Limit
          violations use the existing HTTP 500 error envelope and are never
          silently truncated.
        </li>
        <li>
          Deprecated FlareSolverr fields such as <code>download</code>,
          <code>returnRawHtml</code>, and <code>tabs_till_verify</code> are
          accepted as compatibility no-ops.
        </li>
        <li>
          Browser behavior still depends on target-site rules, IP reputation,
          proxy quality, and browser fingerprint quality.
        </li>
        <li>
          Requests to bypass a specific third-party site's access controls are
          out of scope for this project.
        </li>
        <li>
          Use <a href="/docs">Swagger UI</a> or
          <a href="/openapi.json">OpenAPI JSON</a> for generated schema details.
        </li>
      </ul>
    </section>
  </main>
</body>
</html>
""".strip()
