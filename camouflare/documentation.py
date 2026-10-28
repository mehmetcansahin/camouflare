from __future__ import annotations

V1_ENDPOINT_DESCRIPTION = """
Run a FlareSolverr-compatible command.

Supported commands:
- `sessions.create`: create or reuse a persistent browser session.
- `sessions.list`: list active persistent session ids.
- `sessions.destroy`: close and remove a persistent browser session.
- `request.get`: navigate to a URL and return the solved page payload, or with
  `transport: "http"` send a browserless HTTPS GET.
- `request.post`: submit form or raw JSON data to a URL and return the solved page payload.
- `request.head`: send a browserless HTTPS HEAD request and return its status,
  headers, and cookies.

The endpoint always returns the Camouflare envelope with `status`, `message`,
timestamps, and `version`. Browser and HTTP transport capacity timeouts return
HTTP 503. Invalid commands, missing required command fields, and session errors
return the same envelope with HTTP 500.
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
    "request.get-http": {
        "summary": "Fetch an image over the HTTP transport",
        "description": (
            "Send a browserless HTTPS GET through a SOCKS5 proxy and return the "
            "exact image bytes as base64. Headers are forwarded as given."
        ),
        "value": {
            "cmd": "request.get",
            "url": "https://cdn.example.com/logo.png",
            "transport": "http",
            "responseEncoding": "base64",
            "maxResponseBytes": 10485760,
            "headers": {"Accept": "image/*", "Referer": "https://example.com/"},
            "proxy": {
                "url": "socks5h://proxy.example:1080",
                "username": "user",
                "password": "pass",
            },
            "maxTimeout": 30000,
        },
    },
    "request.head": {
        "summary": "Inspect a redirect without a body",
        "description": (
            "Send a browserless HTTPS HEAD request. With followRedirects 0 a "
            "redirect is returned as-is, including its Location header."
        ),
        "value": {
            "cmd": "request.head",
            "url": "https://example.com/downloads/latest",
            "followRedirects": 0,
            "maxTimeout": 15000,
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
        browserless HTTP requests, persistent sessions, proxy routing, cookie
        capture, screenshots, lightweight liveness checks, browser-readiness
        checks, optional challenge handling, passive diagnostics, and optional
        Prometheus metrics.
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
      <a href="#http-transport">HTTP transport</a>
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
            Returns a passive, token-protected pool, HTTP transport, session,
            cleanup, and runtime snapshot without leasing browser capacity. A
            successful snapshot uses HTTP 200 even when <code>capacity_state</code>
            is not available. <code>capacity_state</code> describes browser capacity
            only; the <code>http</code> object reports the HTTP transport's
            <code>max_concurrency</code>, <code>active_requests</code>,
            <code>waiting_requests</code>, and <code>available_slots</code>.
          </p>
        </div>
        <div class="box">
          <p><span class="method">GET</span> <code>/metrics</code></p>
          <p>Returns Prometheus metrics when <code>PROMETHEUS_ENABLED=true</code>.</p>
        </div>
        <div class="box">
          <p><span class="method">POST</span> <code>/v1</code></p>
          <p>Runs session, browser request, and HTTP transport commands.</p>
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
            <td>
              Open a URL and return its browser-backed or origin-bounded response, or,
              with <code>transport: "http"</code>, a browserless HTTPS response.
            </td>
          </tr>
          <tr>
            <td><code>request.post</code></td>
            <td><code>url</code>, <code>postData</code></td>
            <td>Submit form or raw JSON data without following redirects.</td>
          </tr>
          <tr>
            <td><code>request.head</code></td>
            <td><code>url</code></td>
            <td>Send a browserless HTTPS HEAD request and return status, headers, and cookies.</td>
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
  "version": "2.1.0"
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
        JavaScript rendering, or browser challenge handling. Use
        <code>transport: "http"</code> to send such headers through a proxy.
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
      <p>
        With <code>transport: "http"</code>, <code>request.get</code> skips the browser
        and sends one HTTPS request, optionally through a proxy, with headers
        forwarded as given; see <a href="#http-transport">HTTP Transport</a>. This
        example fetches an image through a SOCKS5 proxy and returns its exact bytes as
        base64:
      </p>
      <pre><code>{
  "cmd": "request.get",
  "url": "https://cdn.example.com/logo.png",
  "transport": "http",
  "responseEncoding": "base64",
  "maxResponseBytes": 10485760,
  "headers": {
    "Accept": "image/*",
    "Referer": "https://example.com/"
  },
  "proxy": {
    "url": "socks5h://proxy.example:1080",
    "username": "user",
    "password": "pass"
  },
  "maxTimeout": 30000
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

      <h3 id="request-head"><code>request.head</code></h3>
      <p>
        Sends a HEAD request over the <a href="#http-transport">HTTP transport</a> and
        returns the final URL, status, headers, and cookies. <code>transport</code>
        may be omitted or <code>"http"</code>; <code>"browser"</code> is rejected with
        <code>INVALID_REQUEST</code>. The solution has <code>response: ""</code> and
        <code>bodyBytes: 0</code>. With <code>followRedirects: 0</code> a redirect is
        returned as-is, so its <code>Location</code> header can be read without
        following it.
      </p>
      <pre><code>{
  "cmd": "request.head",
  "url": "https://example.com/downloads/latest",
  "followRedirects": 0,
  "maxTimeout": 15000
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
            <td>
              Required for <code>request.get</code>, <code>request.post</code>, and
              <code>request.head</code>. The HTTP transport accepts only
              <code>https</code> URLs.
            </td>
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
            <td>
              Supports <code>url</code> or <code>server</code>, plus credentials. Used
              by browser contexts and the HTTP transport; see <a href="#proxy">Proxy</a>.
            </td>
          </tr>
          <tr>
            <td><code>cookies</code></td>
            <td>array</td>
            <td>
              Cookies to inject before navigation, or to send with an HTTP transport
              request. Each entry needs <code>name</code>
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
              are removed before cross-origin redirects. With
              <code>transport: "http"</code> they are forwarded as given, also through a
              proxy, and removed before cross-origin redirects;
              <code>transport: "browser"</code> rejects them on <code>request.get</code>.
              <code>request.post</code> uses the browser context request transport with
              redirects disabled. <code>User-Agent</code> is the compatibility exception:
              it configures browser identity for the whole context.
            </td>
          </tr>
          <tr>
            <td><code>userAgent</code></td>
            <td>string</td>
            <td>
              Browser User-Agent override for new contexts, and the
              <code>User-Agent</code> header of an HTTP transport request. Takes
              precedence over <code>User-Agent</code> in <code>headers</code>.
            </td>
          </tr>
          <tr>
            <td><code>returnOnlyCookies</code></td>
            <td>boolean</td>
            <td>
              When true, returnOnlyCookies omits response HTML, headers, and
              screenshots from <code>solution</code>. The HTTP transport then never
              reads the response body.
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
          <tr>
            <td><code>transport</code></td>
            <td>string</td>
            <td>
              <code>"http"</code> sends <code>request.get</code> as a browserless HTTPS
              request; <code>"browser"</code> always navigates in a browser context.
              When omitted, <code>request.get</code> and <code>request.post</code> keep
              automatic transport selection and <code>request.head</code> uses
              <code>"http"</code>. See <a href="#http-transport">HTTP Transport</a>.
            </td>
          </tr>
          <tr>
            <td><code>responseEncoding</code></td>
            <td>string</td>
            <td>
              HTTP transport only. <code>"text"</code> (default) decodes the body with
              its charset; <code>"base64"</code> returns the exact body bytes, after
              <code>gzip</code> or <code>deflate</code> removal, as base64.
            </td>
          </tr>
          <tr>
            <td><code>maxResponseBytes</code></td>
            <td>integer</td>
            <td>
              HTTP transport only. Response body byte limit, greater than zero and at
              most <code>MAX_RESPONSE_BODY_BYTES</code>, which is also the default. A
              larger body returns <code>RESOURCE_LIMIT_EXCEEDED</code>.
            </td>
          </tr>
          <tr>
            <td><code>followRedirects</code></td>
            <td>integer</td>
            <td>
              HTTP transport only. Redirects to follow, from 0 to 10 (default 10). With
              0 any redirect response is returned as-is; a redirect beyond the limit is
              returned as the response.
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
  "version": "2.1.0"
}</code></pre>
      <p>
        Errors use the same envelope with <code>status: "error"</code>.
        Browser pool or HTTP transport capacity exhaustion returns HTTP 503; malformed
        commands return HTTP 500.
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
        request-level or environment proxy is set, or when the request sets
        <code>transport: "browser"</code>.
      </p>
      <p>
        A GET with non-<code>User-Agent</code> target headers always uses the same
        bounded direct HTTP transport, even without <code>ajax=true</code>, and never
        falls back to browser navigation. It is rejected when a session, proxy, or
        screenshot is requested because those features cannot preserve the header
        boundary; set <code>transport: "http"</code> to send such headers through a
        proxy. A POST never follows redirects and reports its first response.
      </p>
      <p>
        Direct HTTP requests made by the browser path, meaning the preflight, the
        fallbacks, and header-bearing GETs, accept <code>http</code> and
        <code>https</code> targets including private addresses, never use a proxy, and
        follow up to ten redirects; a redirect beyond the tenth, or to a URL that cannot
        be requested, is returned as the response instead of failing. They share the
        <code>HTTP_MAX_CONCURRENCY</code> slots of the HTTP transport, but only a
        header-bearing GET waits for a free slot: the preflight and the fallbacks fall
        back to browser navigation at once when every slot is busy. A header-bearing
        GET that cannot reach the target (DNS failure, refused or reset connection, or
        failed TLS handshake) returns <code>TARGET_UNREACHABLE</code>, retryable unless
        the target certificate failed verification.
      </p>
      <p>
        Direct HTTP GET and the HTTP transport advertise <code>gzip, deflate</code> by
        default and remove those content encodings before applying the response
        charset or base64 encoding. Concatenated gzip members and up to four stacked
        encodings are supported. The response byte limit applies to the wire body and
        every decoded layer under the shared request deadline. Any other encoding, such
        as <code>br</code> or <code>zstd</code>, and malformed or truncated compression
        return <code>RESPONSE_DECODE_ERROR</code>; size violations return
        <code>RESOURCE_LIMIT_EXCEEDED</code>. Direct HTTP preserves upstream response
        headers. Cookies-only GET over direct HTTP inspects at most a 64 KiB decoded
        prefix and does not validate an unread remainder.
      </p>
      <p>
        Confirmed Cloudflare-style hard block documents return
        <code>TARGET_BLOCKED</code> without calling the challenge solver; an unresolved
        challenge still returns <code>CHALLENGE_FAILED</code>. A 403 or 503 status
        alone is not proof of a bot block: ordinary upstream errors remain
        <code>status: "ok"</code> with that status in <code>solution.status</code>,
        including a 4xx/5xx with an empty body that Firefox refuses to render (its
        <code>solution.response</code> is empty).
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

      <h2 id="http-transport">HTTP Transport</h2>
      <p>
        <code>request.head</code>, and <code>request.get</code> with
        <code>transport: "http"</code>, send one stateless HTTP request without a
        browser. They never lease a browser context; instead they use the HTTP
        transport's own <code>HTTP_MAX_CONCURRENCY</code> slots, which the browser
        path's direct HTTP requests share. A request that finds every slot busy waits
        within its own <code>maxTimeout</code>, not <code>POOL_ACQUIRE_TIMEOUT_MS</code>,
        and then returns HTTP 503 <code>POOL_UNAVAILABLE</code>. A target that does not
        respond within <code>maxTimeout</code>, less a reserve of up to one second,
        returns <code>NAVIGATION_TIMEOUT</code>.
      </p>
      <ul>
        <li>
          Only <code>https</code> URLs are accepted, for the request and for every
          redirect target; an <code>http</code> URL returns
          <code>INVALID_REQUEST</code>.
        </li>
        <li>
          Without a proxy, targets on private, loopback, link-local, cloud-metadata,
          carrier-grade NAT, multicast, or other non-public addresses return
          <code>INVALID_REQUEST</code>, including IPv4 addresses embedded in IPv6 and
          hostnames whose DNS answer contains such an address. The connection goes to
          the checked address, and TLS still verifies the certificate for the URL
          hostname against the system trust store.
        </li>
        <li>
          The request-level <code>proxy</code>, or else <code>PROXY_URL</code> /
          <code>PROXY_SERVER</code>, is used when set: HTTP proxies through
          <code>CONNECT</code>, HTTPS proxies, and SOCKS5 or SOCKS5h proxies, with
          optional credentials. Both SOCKS5 forms let the proxy resolve the target
          hostname, and the private-address check does not apply through a proxy.
          A SOCKS4 proxy returns <code>INVALID_REQUEST</code>.
        </li>
        <li>
          <code>headers</code> are sent as given, including <code>Referer</code>,
          <code>Cookie</code>, and <code>Authorization</code>. Defaults only fill in a
          missing <code>User-Agent</code>, <code>Accept</code>,
          <code>Accept-Language</code>, and <code>Accept-Encoding: gzip, deflate</code>;
          <code>userAgent</code> overrides <code>User-Agent</code>. Header values must
          be Latin-1. <code>Host</code>, <code>Content-Length</code>,
          <code>Transfer-Encoding</code>, <code>Connection</code>,
          <code>Keep-Alive</code>, <code>Upgrade</code>, <code>TE</code>,
          <code>Trailer</code>, <code>Proxy-Authorization</code>, and
          <code>Proxy-Connection</code> belong to the transport and return
          <code>INVALID_REQUEST</code>; put proxy credentials in <code>proxy</code>.
          Every caller header except <code>User-Agent</code> is removed on redirect
          hops to another origin.
        </li>
        <li>
          <code>cookies</code> are sent to matching URLs, cookies the target sets are
          kept across redirects, and <code>solution.cookies</code> returns both.
        </li>
        <li>
          <code>followRedirects</code> (0 to 10, default 10) limits how many 301, 302,
          303, 307, and 308 responses with a <code>Location</code> are followed, using
          the same method. With 0 any redirect response is returned as-is, including
          its <code>Location</code> header. A redirect beyond the limit, or one whose
          <code>Location</code> cannot be requested (for example an <code>ftp:</code>
          URL or an invalid port), is returned as the response instead of failing.
        </li>
        <li>
          Only <code>gzip</code> and <code>deflate</code> are decoded. Any other
          content encoding, for example <code>br</code> requested through a
          caller-supplied <code>Accept-Encoding</code>, returns
          <code>RESPONSE_DECODE_ERROR</code>.
        </li>
        <li>
          <code>maxResponseBytes</code> (default and maximum
          <code>MAX_RESPONSE_BODY_BYTES</code>) applies to the wire body and every
          decoded layer. A larger body, including a declared
          <code>Content-Length</code> over the limit, returns
          <code>RESOURCE_LIMIT_EXCEEDED</code>; a <code>maxResponseBytes</code> above
          <code>MAX_RESPONSE_BODY_BYTES</code> returns the same code before any request
          is sent.
        </li>
        <li>
          There is no challenge or block detection and no browser fallback: every
          upstream status, including 403 and 503, is returned with
          <code>status: "ok"</code>, and <code>fallbackUsed</code> is never set.
        </li>
        <li>
          <code>session</code>, <code>session_ttl_minutes</code>,
          <code>returnScreenshot</code>, a non-zero <code>waitInSeconds</code>,
          <code>disableMedia</code>, and <code>postData</code> return
          <code>INVALID_REQUEST</code>, as do <code>request.post</code> with
          <code>transport: "http"</code> and <code>responseEncoding</code>,
          <code>maxResponseBytes</code>, or <code>followRedirects</code> without the
          HTTP transport. <code>transport: "http"</code> and the HTTP-only fields also
          return <code>INVALID_REQUEST</code> on <code>sessions.*</code> commands.
        </li>
      </ul>
      <p>
        A completed request returns <code>status: "ok"</code> with the message
        <code>HTTP request completed.</code>; <code>solution.url</code> is the final URL
        and <code>solution.status</code> its status. <code>solution.headers</code> uses
        lower-case names and combines repeated fields, <code>Set-Cookie</code> with
        newlines and others with <code>", "</code>. Once gzip or deflate was removed,
        <code>Content-Encoding</code> and <code>Content-Length</code> are omitted
        because they describe the wire body. <code>solution.response</code> holds the
        body as text decoded with the response charset (UTF-8 by default, invalid bytes
        replaced) or, with <code>responseEncoding: "base64"</code>, the exact decoded
        bytes as base64. <code>solution.responseEncoding</code> names that encoding,
        <code>solution.bodyBytes</code> is the body's byte length, and
        <code>solution.userAgent</code> is the <code>User-Agent</code> that was sent.
        A <code>request.head</code> solution has <code>response: ""</code>,
        <code>bodyBytes: 0</code>, and the headers as sent, including
        <code>Content-Encoding</code> and <code>Content-Length</code>. With
        <code>returnOnlyCookies</code> the body is never read and the solution holds
        only <code>url</code>, <code>status</code>, <code>cookies</code>, and
        <code>userAgent</code>.
      </p>
      <pre><code>{
  "status": "ok",
  "message": "HTTP request completed.",
  "solution": {
    "url": "https://cdn.example.com/logo.png",
    "status": 200,
    "headers": {
      "content-type": "image/png",
      "content-length": "5120",
      "cache-control": "max-age=86400"
    },
    "response": "iVBORw0KGgoAAAANSUhEUgAA...",
    "cookies": [],
    "userAgent": "Mozilla/5.0 ...",
    "responseEncoding": "base64",
    "bodyBytes": 5120
  },
  "startTimestamp": 1770000000000,
  "endTimestamp": 1770000000420,
  "version": "2.1.0"
}</code></pre>
      <p>
        <code>transport: "browser"</code> always navigates in a browser context. It
        disables the <code>ajax=true</code> direct HTTP preflight and the direct HTTP
        fallbacks after a browser transport failure or navigation timeout, rejects
        non-<code>User-Agent</code> headers on <code>request.get</code> with
        <code>INVALID_REQUEST</code>, and is rejected for <code>request.head</code>.
        <code>request.post</code> keeps its browser context request transport. See the
        <a href="#error-reference">Error Reference</a> for network and proxy failures.
      </p>

      <h2 id="error-reference">Error Reference</h2>
      <p>
        Stable <code>errorCode</code> values are
        <code>INVALID_REQUEST</code>, <code>SESSION_NOT_FOUND</code>,
        <code>RESOURCE_LIMIT_EXCEEDED</code>, <code>POOL_UNAVAILABLE</code>,
        <code>REQUEST_TIMEOUT</code>, <code>NAVIGATION_TIMEOUT</code>,
        <code>BROWSER_TRANSPORT_CLOSED</code>, <code>CHALLENGE_FAILED</code>,
        <code>TARGET_BLOCKED</code>, <code>TARGET_UNREACHABLE</code>,
        <code>PROXY_CONNECTION_ERROR</code>, <code>RESPONSE_DECODE_ERROR</code>, and
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
            <td>
              A <code>transport: "http"</code> or <code>request.head</code> request finds
              every <code>HTTP_MAX_CONCURRENCY</code> slot busy until its deadline, or
              the service is shutting down.
            </td>
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
            <td>
              The browser gets no response from the target: DNS failure, connection
              refused or reset, or an interrupted transfer. Direct HTTP and the HTTP
              transport also report a failed TLS handshake this way, and the HTTP
              transport reports a proxy that cannot reach the target (SOCKS host or
              network unreachable, connection refused, or TTL expired; HTTP
              <code>CONNECT</code> 502 or 504).
            </td>
            <td>HTTP 500</td>
            <td>
              <code>TARGET_UNREACHABLE</code>; a GET or HEAD is retryable, a POST is not
              automatically retryable. A target certificate that fails verification
              over direct HTTP or the HTTP transport is not retryable.
            </td>
          </tr>
          <tr>
            <td>
              Direct HTTP or HTTP transport compression is unsupported, malformed, or
              truncated.
            </td>
            <td>HTTP 500</td>
            <td><code>RESPONSE_DECODE_ERROR</code>; not retryable.</td>
          </tr>
          <tr>
            <td>
              Through a proxy, the connection fails before the proxy reached the
              target: the proxy is unreachable, times out, is not the configured proxy
              type, closes the handshake, fails TLS verification as an HTTPS proxy, or
              refuses the request (for example <code>403</code> or rejected
              credentials).
            </td>
            <td>HTTP 500</td>
            <td>
              <code>PROXY_CONNECTION_ERROR</code>; retryable, except for rejected
              credentials, a <code>4xx</code> answer to <code>CONNECT</code>, and an
              HTTPS proxy certificate that fails verification.
            </td>
          </tr>
          <tr>
            <td>
              The HTTP transport target does not respond before the request deadline,
              including a TLS handshake that stalls after a proxy connected to it.
            </td>
            <td>HTTP 500</td>
            <td><code>NAVIGATION_TIMEOUT</code>; retryable.</td>
          </tr>
          <tr>
            <td>
              An HTTP transport request targets an <code>http</code> URL or redirect, or
              a non-public address without a proxy; uses a SOCKS4 proxy, a
              transport-managed header, a non-Latin-1 header value, or an unsupported
              field; or HTTP-only fields are sent without the HTTP transport.
            </td>
            <td>HTTP 500</td>
            <td><code>INVALID_REQUEST</code>; not retryable without correction.</td>
          </tr>
          <tr>
            <td>
              A response body exceeds the response byte limit, or
              <code>maxResponseBytes</code> exceeds <code>MAX_RESPONSE_BODY_BYTES</code>.
            </td>
            <td>HTTP 500</td>
            <td><code>RESOURCE_LIMIT_EXCEEDED</code>; not retryable.</td>
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
      <p>
        The HTTP transport (<code>transport: "http"</code> and
        <code>request.head</code>) uses the same request-level or environment proxy.
        It supports HTTP proxies through <code>CONNECT</code>, HTTPS proxies, and
        SOCKS5 or SOCKS5h proxies with optional credentials; both SOCKS5 forms let the
        proxy resolve the target hostname. SOCKS4 proxies return
        <code>INVALID_REQUEST</code> there. The browser path's own direct HTTP requests
        never use a proxy, so its <code>ajax=true</code> preflight and direct HTTP
        fallbacks do not run when a proxy is set.
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
            <td>Default proxy endpoint for browser contexts and the HTTP transport.</td>
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
            <td><code>HTTP_MAX_CONCURRENCY</code></td>
            <td><code>8</code></td>
            <td>
              Concurrent requests on the browserless HTTP transport, separate from
              browser context slots and shared by <code>transport: "http"</code>,
              <code>request.head</code>, and the browser path's direct HTTP requests.
              An HTTP transport request that finds every slot busy waits within its own
              <code>maxTimeout</code>, not <code>POOL_ACQUIRE_TIMEOUT_MS</code>, before
              HTTP 503 <code>POOL_UNAVAILABLE</code>. Must be greater than zero.
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
            <td>
              Maximum returned HTML, JSON, XML, or text body, and the default and upper
              bound for <code>maxResponseBytes</code>.
            </td>
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
            <td>Shared browser, session, and HTTP transport shutdown deadline.</td>
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
          Origin-bound GET headers, the HTTP transport, and all POST bodies use buffered
          HTTP transports instead of a browser document navigation. JavaScript
          rendering, browser challenge handling, and screenshots therefore do not apply
          to those responses.
        </li>
        <li>
          <code>transport</code>, <code>request.head</code>,
          <code>responseEncoding</code>, <code>maxResponseBytes</code>,
          <code>followRedirects</code>, and the solution's
          <code>responseEncoding</code> and <code>bodyBytes</code> are Camouflare
          extensions. Requests that omit them keep their previous behavior.
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
