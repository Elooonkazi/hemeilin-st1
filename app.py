"""Streamlit Community Cloud entrypoint.

Public face: a boring system-metrics dashboard (this app is PUBLIC by
Streamlit's free-tier design, so nothing sensitive may ever appear in the UI).

Private work (logs only, visible in the owner's Streamlit dashboard):
  * starts the local VLESS-over-WS server (server.py) on 127.0.0.1:PORT
  * downloads cloudflared and opens an outbound Argo quick tunnel to it
  * prints the tunnel domain + vless:// client link to stdout (dashboard logs)

Secrets must NEVER be rendered with st.* calls.
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

import streamlit as st  # noqa: E402

st.set_page_config(page_title="System Metrics", page_icon="📊", layout="wide")

# ---------------------------------------------------------------- dashboard
st.title("📊 System Metrics")
st.caption("Lightweight service health overview. Auto-refreshes on visit.")

col1, col2, col3 = st.columns(3)
col1.metric("Uptime (30d)", "99.2%", "+0.3%")
col2.metric("Avg. latency", "42 ms", "-3 ms")
col3.metric("Requests / day", "18,204", "+1.1%")

st.subheader("Traffic (last 24h)")
st.line_chart(
    {"in": [12, 19, 8, 22, 31, 27, 18, 25, 33, 29, 21, 16] * 2,
     "out": [9, 14, 11, 17, 24, 20, 15, 19, 26, 22, 17, 12] * 2}
)
st.subheader("Service status")
st.success("All systems operational")
with st.expander("About this dashboard"):
    st.write(
        "Demo monitoring page rendered with Streamlit. "
        "Values are illustrative only."
    )

# ------------------------------------------------------------ node bootstrap
_boot_lock = threading.Lock()
_booted = False

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "stnode")
os.makedirs(CACHE_DIR, exist_ok=True)


def _download(url, dest):
    tmp = dest + ".download"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for _ in range(5):  # follow redirects manually
        try:
            with urllib.request.urlopen(req, timeout=90) as res:
                if res.status in (301, 302, 303, 307, 308):
                    loc = res.headers.get("Location")
                    req = urllib.request.Request(
                        urllib.parse.urljoin(url, loc),
                        headers={"User-Agent": "Mozilla/5.0"})
                    url = req.full_url
                    continue
                with open(tmp, "wb") as f:
                    shutil.copyfileobj(res, f)
                os.replace(tmp, dest)
                return True
        except Exception as e:
            print("[argo] download error: %r" % (e,), flush=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
    return False


def _fetch_cloudflared(bot_path):
    arch = "arm64" if os.uname().machine == "aarch64" else "amd64"
    sources = [
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-linux-" + arch,
        "https://" + arch + ".oooen.com/bot",
        "https://" + arch + ".ssss.nyc.mn/bot",
    ]
    for u in sources:
        print("[argo] downloading cloudflared from " + u, flush=True)
        if _download(u, bot_path):
            try:
                os.chmod(bot_path, 0o755)
            except OSError as e:
                print("[argo] chmod failed: %r" % (e,), flush=True)
                return False
            print("[argo] download ok", flush=True)
            return True
        print("[argo] download failed, trying next source", flush=True)
    return False


def _run_tunnel(bot_path, port, log_path):
    try:
        if os.path.exists(log_path):
            os.unlink(log_path)
    except OSError:
        pass
    subprocess.Popen(
        [bot_path, "tunnel", "--edge-ip-version", "auto", "--no-autoupdate",
         "--protocol", "http2", "--logfile", log_path,
         "--loglevel", "info", "--url", "http://127.0.0.1:%d" % port],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    print("[argo] tunnel process started", flush=True)


def _wait_domain(log_path, timeout=90):
    pat = re.compile(r"https?://([^ \t\r\n]*trycloudflare\.com)/?")
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                m = pat.search(f.read())
                if m:
                    return m.group(1)
        except OSError:
            pass
        time.sleep(2)
    return None


def _print_links(domain):
    link = server.argo_link(domain)
    print("", flush=True)
    print("[argo] ======================================================", flush=True)
    print("[argo] tunnel domain : " + domain, flush=True)
    print("[argo] public URL     : https://" + domain + "  (decoy page)", flush=True)
    print("[argo] subscription   : https://" + domain + server.SUB_PATH, flush=True)
    print("[argo] node link      :", flush=True)
    print(link, flush=True)
    print("[argo] NOTE: the tunnel domain changes on every restart.", flush=True)
    print("[argo]       Re-pull the subscription after each restart.", flush=True)
    print("[argo] ======================================================", flush=True)
    print("", flush=True)


PIDFILE = os.path.join(CACHE_DIR, "tunnel.pid")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pidfile():
    try:
        with open(PIDFILE, "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_pidfile(pid):
    try:
        with open(PIDFILE, "w") as f:
            f.write(str(pid))
    except OSError as e:
        print("[argo] pidfile write failed: %r" % (e,), flush=True)


def _port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _existing_tunnel_domain(log_path):
    """A tunnel process from an earlier boot in this container is still
    alive: reuse its domain instead of opening a new tunnel (which would
    change the public domain on every Streamlit script rerun)."""
    pid = _read_pidfile()
    if pid and _pid_alive(pid):
        domain = _wait_domain(log_path, timeout=10)
        if domain:
            return domain
        print("[argo] pidfile points to live pid %d but no domain in log; "
              "starting a fresh tunnel" % pid, flush=True)
    return None


def _run_tunnel(bot_path, port, log_path):
    try:
        if os.path.exists(log_path):
            os.unlink(log_path)
    except OSError:
        pass
    proc = subprocess.Popen(
        [bot_path, "tunnel", "--edge-ip-version", "auto", "--no-autoupdate",
         "--protocol", "http2", "--logfile", log_path,
         "--loglevel", "info", "--url", "http://127.0.0.1:%d" % port],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    _write_pidfile(proc.pid)
    print("[argo] tunnel process started (pid %d)" % proc.pid, flush=True)


def _run_named_tunnel(bot_path, token, log_path):
    try:
        if os.path.exists(log_path):
            os.unlink(log_path)
    except OSError:
        pass
    proc = subprocess.Popen(
        [bot_path, "tunnel", "--no-autoupdate", "--logfile", log_path,
         "--loglevel", "info", "run", "--token", token],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    _write_pidfile(proc.pid)
    print("[argo] named tunnel process started (pid %d)" % proc.pid, flush=True)


def _boot_all():
    # NOTE: Streamlit re-executes this script on every rerun/code update, and
    # module-level guards do not reliably survive that. Everything below is
    # therefore idempotent via the filesystem: never start a second server or
    # a second tunnel while the previous ones are alive.
    #
    # 1. local VLESS server — start only if the port is free
    if _port_in_use(server.LISTEN_PORT):
        print("[vless] port %d already bound, reusing existing server"
              % server.LISTEN_PORT, flush=True)
    else:
        threading.Thread(target=server.run, daemon=True,
                         name="vless-server").start()
        time.sleep(1)
    # 2. cloudflared binary (reuse if already cached and executable)
    bot_path = os.path.join(CACHE_DIR, "cfbin")
    log_path = os.path.join(CACHE_DIR, "boot.log")
    if not (os.path.exists(bot_path) and os.access(bot_path, os.X_OK)):
        if not _fetch_cloudflared(bot_path):
            print("[argo] FATAL: could not download cloudflared; "
                  "local VLESS server still running, will retry on next visit.",
                  flush=True)
            return
    # 3a. named tunnel (fixed domain) when TUNNEL_TOKEN is provided
    token = os.environ.get("TUNNEL_TOKEN", "")
    fixed_domain = os.environ.get("TUNNEL_DOMAIN", "")
    if token and fixed_domain:
        pid = _read_pidfile()
        if pid and _pid_alive(pid):
            print("[argo] reusing existing named tunnel (pid %d)" % pid,
                  flush=True)
        else:
            print("[argo] starting NAMED tunnel for " + fixed_domain,
                  flush=True)
            _run_named_tunnel(bot_path, token, log_path)
        server.tunnel_domain = fixed_domain
        _print_links(fixed_domain)
        return
    # 3b. quick tunnel — reuse the live one if there is one
    domain = _existing_tunnel_domain(log_path)
    if domain:
        print("[argo] reusing existing quick tunnel: " + domain, flush=True)
        server.tunnel_domain = domain
        _print_links(domain)
        return
    for attempt in (1, 2, 3):
        print("[argo] starting quick tunnel (attempt %d/3) ..." % attempt,
              flush=True)
        _run_tunnel(bot_path, server.LISTEN_PORT, log_path)
        domain = _wait_domain(log_path, timeout=90)
        if domain:
            server.tunnel_domain = domain
            _print_links(domain)
            return
        print("[argo] no domain appeared, retrying", flush=True)
    print("[argo] FATAL: tunnel did not come up after 3 attempts", flush=True)


def ensure_booted():
    global _booted
    with _boot_lock:
        if _booted:
            return
        _booted = True
    threading.Thread(target=_boot_all, daemon=True, name="boot").start()


ensure_booted()
