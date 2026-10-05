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
        and binds to loopback. Tokenless mode accepts only loopback peers and
        <code>Host</code> values, accepts only same-origin browser requests, and
        requires a JSON content type for <code>POST /v1</code>. A non-loopback
        <code>HOST</code> requires a token. The reusable
        <code>camouflare.asgi:app</code> entry point also always requires a token;
        use the <code>camouflare</code> command for tokenless local development.
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
          <p>
            Runs the browser-readiness probe by creating a page and evaluating JS.
            When every context slot is held by a browser that is still serving,
            including one past a recycle limit that is finishing its work, it
            answers HTTP 200 with <code>capacity_state</code> <code>saturated</code>
            from the pool's own view instead of queueing a probe behind live
            requests, so HTTP 503 means the pool could not produce a working
            browser. Browsers whose process has disconnected are retired before
            that verdict, so a dead busy browser is probed, never reported as
            saturated.
          </p>
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
            <td>Open a URL and return its browser-backed or origin-bounded response.</td>
          </tr>
          <tr>
            <td><code>request.post</code></td>
            <td><code>url</code>, <code>postData</code></td>
            <td>Submit form or raw JSON data without following redirects.</td>
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
  "version": "2.0.2"
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
        When non-<code>User-Agent</code> target headers are present, the request
        instead uses stateless direct HTTP so cross-origin redirects can remove
        those headers. That mode does not support sessions, proxies, screenshots,
        JavaScript rendering, or browser challenge handling.
      </p>
      <p>
        DOM readiness and commit grace each wait at most 15 seconds within the shared
        <code>maxTimeout</code>. GET navigation keeps up to one second for collection
        (at most half the time remaining for short budgets); an already-eligible
        <code>ajax=true</code> timeout fallback keeps up to 15 seconds or half the
        remaining navigation budget. Direct HTTP uses the shortened deadline too.
        A committed document keeps browser challenge handling under the original
        request deadline. These budgets do not change transport eligibility.
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
        The POST uses the browser context's request transport with automatic
        redirects disabled; a redirect response is returned without replaying the
        request. The buffered response is not loaded into a browser page, so
        screenshots, JavaScript rendering, and browser challenge handling do not apply.
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
            <td>
              Shared command deadline in milliseconds, including setup, navigation,
              challenge handling, and collection. Default: <code>60000</code>.
            </td>
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
            <td>
              Cookies to inject before navigation. Each entry needs <code>name</code>
              and <code>value</code> plus <code>url</code> or <code>domain</code>;
              with neither, the cookie is scoped to the target <code>url</code>.
              <code>path</code>, <code>expires</code> (or Selenium's
              <code>expiry</code>), <code>httpOnly</code>, <code>secure</code>,
              <code>sameSite</code> (case-insensitive), and <code>partitionKey</code>
              are supported; other browser-export fields are ignored. Public-suffix
              domains and values a browser cannot store are rejected.
            </td>
          </tr>
          <tr>
            <td><code>headers</code></td>
            <td>object</td>
            <td>
              Origin-bound target headers. Non-<code>User-Agent</code> headers make a
              stateless, proxyless <code>request.get</code> use direct HTTP, where they
              are removed before cross-origin redirects. <code>request.post</code> uses
              the browser context request transport with redirects disabled.
              <code>User-Agent</code> is the compatibility exception: it configures
              browser identity for the whole context.
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
  "version": "2.0.2"
}</code></pre>
      <p>
        Errors use the same envelope with <code>status: "error"</code>.
        Pool saturation returns HTTP 503; malformed commands return HTTP 500.
        Optional <code>errorCode</code> and <code>retryable</code> fields let clients
        classify failures without parsing <code>message</code>.
        <code>requestOutcomeUnknown</code> is true when a failed POST may have reached
        the target. <code>fallbackUsed</code> is true only after a stateless GET actually
        transitions from browser navigation to direct HTTP. Optional fields are omitted
        when they do not apply, so treat an absent <code>errorCode</code>,
        <code>retryable</code>, <code>requestOutcomeUnknown</code>, or
        <code>fallbackUsed</code> as unset rather than as false.
      </p>
      <p>
        When no proxy is configured, a stateless GET whose query string contains
        <code>ajax=true</code> and that requests no cookies, wait time, or screenshot
        is attempted over direct HTTP before the browser. When that preflight returns a
        2xx response without challenge markers it is served as-is, and
        <code>fallbackUsed</code> is omitted because no browser navigation was
        attempted. A non-2xx status, a transport error, or a challenge-looking body
        falls through to normal browser navigation; a body over the response size
        limit ends the request with <code>RESOURCE_LIMIT_EXCEEDED</code> instead.
        Unsupported or invalid direct HTTP compression instead ends the request with
        <code>RESPONSE_DECODE_ERROR</code>, without a browser retry.
        Neither the preflight nor the transport-failure fallback runs when a
        request-level or environment proxy is set.
      </p>
      <p>
        A GET with non-<code>User-Agent</code> target headers always uses the same
        bounded direct HTTP transport, even without <code>ajax=true</code>, and never
        falls back to browser navigation. It is rejected when a session, proxy, or
        screenshot is requested because those features cannot preserve the header
        boundary. A POST never follows redirects and reports its first response.
      </p>
      <p>
        Direct HTTP GET advertises <code>gzip, deflate</code> by default and decodes
        content encodings before applying the response charset. Concatenated gzip
        members and up to four stacked encodings are supported. The response byte
        limit applies to the wire body and every decoded layer under the shared
        request deadline. Unsupported, malformed, or truncated compression returns
        <code>RESPONSE_DECODE_ERROR</code>; size violations return
        <code>RESOURCE_LIMIT_EXCEEDED</code>. Upstream response headers are preserved.
        Cookies-only GET inspects at most a 64 KiB decoded prefix and does not
        validate an unread remainder.
      </p>
      <p>
        Confirmed Cloudflare-style hard block documents return
        <code>TARGET_BLOCKED</code> without calling the challenge solver; an unresolved
        challenge still returns <code>CHALLENGE_FAILED</code>. A 403 or 503 status
        alone is not proof of a bot block: ordinary upstream errors remain
        <code>status: "ok"</code> with that status in <code>solution.status</code>.
        A cookies-only POST never reads its response body, so it cannot classify
        body-based challenges or blocks.
      </p>
      <pre><code>{
  "status": "error",
  "message": "Browser transport closed.",
  "errorCode": "BROWSER_TRANSPORT_CLOSED",
  "retryable": false,
  "requestOutcomeUnknown": true
}</code></pre>

      <h2 id="error-reference">Error Reference</h2>
      <p>
        Stable <code>errorCode</code> values are
        <code>INVALID_REQUEST</code>, <code>SESSION_NOT_FOUND</code>,
        <code>RESOURCE_LIMIT_EXCEEDED</code>, <code>POOL_UNAVAILABLE</code>,
        <code>REQUEST_TIMEOUT</code>, <code>NAVIGATION_TIMEOUT</code>,
        <code>BROWSER_TRANSPORT_CLOSED</code>, <code>CHALLENGE_FAILED</code>,
        <code>TARGET_BLOCKED</code>, <code>RESPONSE_DECODE_ERROR</code>, and
        <code>INTERNAL_ERROR</code>. New metadata is optional so existing clients can
        continue using <code>status</code> and <code>message</code> unchanged.
      </p>
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
            <td><code>POOL_UNAVAILABLE</code>; retryable.</td>
          </tr>
          <tr>
            <td>Missing <code>cmd</code>, invalid command, missing required field.</td>
            <td>HTTP 500</td>
            <td><code>INVALID_REQUEST</code>; not retryable without correction.</td>
          </tr>
          <tr>
            <td><code>sessions.destroy</code> targets an unknown session.</td>
            <td>HTTP 500</td>
            <td><code>SESSION_NOT_FOUND</code>.</td>
          </tr>
          <tr>
            <td>A session is closing, or closes while a request waits for its lock.</td>
            <td>HTTP 500</td>
            <td>
              <code>SESSION_NOT_FOUND</code>; retryable because no target request
              was sent.
            </td>
          </tr>
          <tr>
            <td>Session cleanup times out or fails during destroy or rotation.</td>
            <td>HTTP 500</td>
            <td>
              <code>REQUEST_TIMEOUT</code> or <code>INTERNAL_ERROR</code>;
              retryable after the session is removed.
            </td>
          </tr>
          <tr>
            <td>A stateless browser page cannot be opened before navigation.</td>
            <td>HTTP 500</td>
            <td>
              <code>BROWSER_TRANSPORT_CLOSED</code>; the outcome is known.
              A GET is retryable; a POST is not automatically retryable.
            </td>
          </tr>
          <tr>
            <td>
              Challenge solve or requested wait exceeds the command's
              <code>maxTimeout</code>.
            </td>
            <td>HTTP 500</td>
            <td>
              <code>CHALLENGE_FAILED</code> or <code>REQUEST_TIMEOUT</code>; may include
              a partial <code>solution</code> for debugging.
            </td>
          </tr>
          <tr>
            <td>A confirmed bot/WAF hard block document is returned.</td>
            <td>HTTP 500</td>
            <td>
              <code>TARGET_BLOCKED</code>; not retryable. The solution retains the
              upstream status, and its body is omitted for cookies-only GET.
            </td>
          </tr>
          <tr>
            <td>Direct HTTP compression is unsupported, malformed, or truncated.</td>
            <td>HTTP 500</td>
            <td><code>RESPONSE_DECODE_ERROR</code>; not retryable.</td>
          </tr>
        </tbody>
      </table>

      <h2 id="sessions">Sessions</h2>
      <p>
        Sessions keep a persistent browser context behind a named id. Requests
        that share a session are serialized with a per-session lock, so cookies
        and browser state are preserved without concurrent page races.
      </p>
      <p>
        A session holds a context slot for its whole lifetime. The number of
        sessions that can exist at once is
        <code>POOL_MAX_BROWSERS * POOL_MAX_CONTEXTS_PER_BROWSER
        - POOL_RESERVED_TRANSIENT_CONTEXTS</code>, and
        <code>MAX_SESSIONS</code> only caps the registry above that. Over the limit,
        <code>sessions.create</code> is rejected immediately with HTTP 503 and
        <code>POOL_UNAVAILABLE</code>; it does not wait for
        <code>POOL_ACQUIRE_TIMEOUT_MS</code>. Under the limit it acquires a context
        slot like any other request, so while every slot is busy it waits for the
        earlier of <code>POOL_ACQUIRE_TIMEOUT_MS</code> and its <code>maxTimeout</code>.
        The shipped Compose profile (two browsers, one context each, one reserved)
        therefore allows one concurrent session.
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
            <td><code>POOL_MAX_CONTEXTS_PER_BROWSER</code></td>
            <td><code>1</code></td>
            <td>
              Concurrent contexts per browser process. Total context slots are
              <code>POOL_MAX_BROWSERS * POOL_MAX_CONTEXTS_PER_BROWSER</code>.
            </td>
          </tr>
          <tr>
            <td><code>POOL_RESERVED_TRANSIENT_CONTEXTS</code></td>
            <td><code>1</code></td>
            <td>
              Context slots withheld from persistent sessions so that sessions cannot
              occupy every slot. Concurrent sessions are limited to total context
              slots minus this value.
            </td>
          </tr>
          <tr>
            <td><code>POOL_ACQUIRE_TIMEOUT_MS</code></td>
            <td><code>30000</code></td>
            <td>
              How long a request or readiness probe waits for a free context slot
              before HTTP 503 <code>POOL_UNAVAILABLE</code>. A request's own
              <code>maxTimeout</code> also covers this wait, so whichever deadline is
              shorter wins; when <code>maxTimeout</code> expires first the caller gets
              HTTP 500 <code>REQUEST_TIMEOUT</code> instead.
            </td>
          </tr>
          <tr>
            <td><code>BROWSER_MAX_USES</code></td>
            <td><code>200</code></td>
            <td>
              Contexts leased from one browser process before it stops being
              preferred for new leases. Spare context slots on a browser that is
              still serving remain usable as a fallback; it is closed once its
              in-flight contexts finish.
            </td>
          </tr>
          <tr>
            <td><code>BROWSER_MAX_AGE_MINUTES</code></td>
            <td><code>120</code></td>
            <td>
              Age after which a browser process, busy or not, stops being preferred
              for new leases. Spare context slots on a browser that is still
              serving remain usable as a fallback; it is closed once its in-flight
              contexts finish.
            </td>
          </tr>
          <tr>
            <td><code>MAX_SESSIONS</code></td>
            <td><code>32</code></td>
            <td>
              Upper bound on the session registry. It is not the concurrency limit:
              concurrent sessions are capped by the pool's persistent context
              capacity, which is total context slots minus
              <code>POOL_RESERVED_TRANSIENT_CONTEXTS</code>. Keep it at or above that
              capacity; a lower value is checked after a context is already leased and
              fails with HTTP 500 <code>INTERNAL_ERROR</code> rather than 503.
            </td>
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
            <td><code>POOL_MAINTENANCE_INTERVAL_SECONDS</code></td>
            <td><code>15</code></td>
            <td>
              Interval of the background pool tick that retires idle browsers past
              <code>BROWSER_MAX_AGE_MINUTES</code> and relaunches browsers up to
              <code>POOL_MIN_BROWSERS</code>. A browser retired by a request is
              replaced as soon as it closes unless a request is already waiting;
              that request launches for itself and the next tick fills any
              remaining shortfall. Background launches wait for closing browsers,
              so the tick never runs more than <code>POOL_MAX_BROWSERS</code>
              processes at once.
            </td>
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
            <td>
              Logging format: <code>text</code> or <code>json</code>. Both redact
              messages and structured fields. Text appends compact JSON as
              <code>fields={...}</code>; JSON stores it under <code>fields</code>.
              Completion records include result, HTTP status, duration, and an
              error code when applicable.
            </td>
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
          Origin-bound GET headers and all POST bodies use buffered HTTP transports
          instead of a browser document navigation. JavaScript rendering, browser
          challenge handling, and screenshots therefore do not apply to those responses.
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
