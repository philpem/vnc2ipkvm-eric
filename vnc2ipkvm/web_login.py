"""Auto-login to the Belkin IP-KVM web interface and extract applet parameters.

The KVM's web interface serves an applet page at /title_app.asp?__port_id=N
containing Java applet parameters including the session APPLET_ID. If not
logged in, the KVM redirects to /auth.asp for username/password authentication.

This module automates the login flow and extracts all relevant applet
parameters, so the user doesn't need to manually dig out the APPLET_ID.
"""

import http.client
import logging
import re
import ssl
import urllib.parse

logger = logging.getLogger(__name__)


def fetch_applet_params(host: str, port_id: int = 0,
                        username: str = "", password: str = "",
                        use_https: bool = False, http_port: int = 80) -> dict:
    """Log in to the KVM web interface and extract applet parameters.

    Returns a dict of parameter names (uppercase) to values, e.g.:
        {"APPLET_ID": "44A2...", "PORT": "443", "PROTOCOL_VERSION": "01.11", ...}

    Raises ConnectionError or ValueError on failure.
    """
    scheme = "https" if use_https else "http"
    logger.info("Fetching applet params from %s://%s:%d/", scheme, host, http_port)

    conn = _make_connection(host, http_port, use_https)
    cookies = {}

    try:
        # Step 1: Try to fetch the applet page directly
        applet_url = f"/title_app.asp?__port_id={port_id}"
        body, resp_cookies = _http_get(conn, applet_url, cookies)
        cookies.update(resp_cookies)

        # Check if we got the applet page or were redirected to login
        if _has_applet_params(body):
            return _parse_applet_params(body)

        if not username:
            raise ConnectionError(
                "KVM requires login but no --user/--password provided. "
                "Supply credentials or use --applet-id directly.")

        # Step 2: POST login credentials
        logger.info("Login required, authenticating as '%s'...", username)
        body, resp_cookies = _http_post_login(conn, "/auth.asp",
                                               username, password, cookies)
        cookies.update(resp_cookies)

        # Step 3: Re-fetch the applet page after login
        # Need a fresh connection since HTTP/1.0 servers may close
        conn.close()
        conn = _make_connection(host, http_port, use_https)
        body, resp_cookies = _http_get(conn, applet_url, cookies)
        cookies.update(resp_cookies)

        if _has_applet_params(body):
            return _parse_applet_params(body)

        # Maybe the login response itself contains the applet redirect
        # or the login page shows an error
        if "auth.asp" in body.lower() or "login" in body.lower():
            raise ConnectionError("Login failed — check username and password")

        raise ConnectionError(
            "Could not find applet parameters after login. "
            "The KVM may require a different login flow.")

    finally:
        conn.close()


def _make_connection(host: str, port: int, use_https: bool) -> http.client.HTTPConnection:
    if use_https:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(host, port, timeout=15, context=ctx)
    return http.client.HTTPConnection(host, port, timeout=15)


def _format_cookies(cookies: dict) -> str:
    if not cookies:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _parse_set_cookies(response) -> dict:
    """Extract cookies from Set-Cookie headers."""
    cookies = {}
    for header in response.getheaders():
        if header[0].lower() == "set-cookie":
            # Parse "name=value; path=/; ..."
            cookie_str = header[1].split(";")[0].strip()
            if "=" in cookie_str:
                name, value = cookie_str.split("=", 1)
                cookies[name.strip()] = value.strip()
    return cookies


def _http_get(conn, path: str, cookies: dict) -> tuple[str, dict]:
    """Perform an HTTP GET, following one redirect if needed."""
    headers = {"User-Agent": "vnc2ipkvm/1.0"}
    cookie_str = _format_cookies(cookies)
    if cookie_str:
        headers["Cookie"] = cookie_str

    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    resp_cookies = _parse_set_cookies(resp)
    body = resp.read().decode("utf-8", errors="replace")

    logger.debug("GET %s -> %d (%d bytes)", path, resp.status, len(body))

    # Follow redirect
    if resp.status in (301, 302, 303, 307) and resp.getheader("Location"):
        location = resp.getheader("Location")
        # Parse relative or absolute URL
        parsed = urllib.parse.urlparse(location)
        redirect_path = parsed.path
        if parsed.query:
            redirect_path += "?" + parsed.query
        logger.debug("Following redirect to %s", redirect_path)
        cookies.update(resp_cookies)

        # Need fresh connection for redirect
        conn.close()
        new_conn = _make_connection(conn.host, conn.port,
                                     isinstance(conn, http.client.HTTPSConnection))
        body, more_cookies = _http_get(new_conn, redirect_path, cookies)
        resp_cookies.update(more_cookies)
        # Copy the new connection state back - caller should use the body
        # (Connection will be closed by caller)

    return body, resp_cookies


def _http_post_login(conn, path: str, username: str, password: str,
                      cookies: dict) -> tuple[str, dict]:
    """POST login credentials to auth.asp."""
    form_data = urllib.parse.urlencode({
        "login": username,
        "password": password,
        "action_login.x": "0",
        "action_login.y": "0",
    })

    headers = {
        "User-Agent": "vnc2ipkvm/1.0",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    cookie_str = _format_cookies(cookies)
    if cookie_str:
        headers["Cookie"] = cookie_str

    conn.request("POST", path, body=form_data, headers=headers)
    resp = conn.getresponse()
    resp_cookies = _parse_set_cookies(resp)
    body = resp.read().decode("utf-8", errors="replace")

    logger.debug("POST %s -> %d (%d bytes)", path, resp.status, len(body))

    # Follow redirect after login
    if resp.status in (301, 302, 303, 307) and resp.getheader("Location"):
        location = resp.getheader("Location")
        parsed = urllib.parse.urlparse(location)
        redirect_path = parsed.path
        if parsed.query:
            redirect_path += "?" + parsed.query
        logger.debug("Login redirect to %s", redirect_path)
        cookies.update(resp_cookies)
        conn.close()
        new_conn = _make_connection(conn.host, conn.port,
                                     isinstance(conn, http.client.HTTPSConnection))
        body, more_cookies = _http_get(new_conn, redirect_path, cookies)
        resp_cookies.update(more_cookies)

    return body, resp_cookies


def _has_applet_params(html: str) -> bool:
    """Check if the HTML contains applet parameters."""
    return 'name="APPLET_ID"' in html


def _parse_applet_params(html: str) -> dict:
    """Extract <param name="..." value="..."> from the applet HTML."""
    params = {}
    # Match both single and double quoted values
    pattern = re.compile(
        r'<param\s+name=["\']([^"\']+)["\']\s+value=["\']([^"\']*)["\']',
        re.IGNORECASE)
    for match in pattern.finditer(html):
        name = match.group(1).upper()
        value = match.group(2)
        params[name] = value

    if "APPLET_ID" not in params:
        raise ValueError("APPLET_ID not found in applet page")

    logger.info("Extracted %d applet parameters (APPLET_ID=%s...)",
                len(params), params["APPLET_ID"][:16])
    return params
