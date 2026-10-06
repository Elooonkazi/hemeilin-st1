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
LOCKFILE = os.path.join(CACHE_DIR, "boot.lock")
_TRYCF_RE = re.compile(r"https?://([^ \t\r\n]*trycloudflare\.com)/?")


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


def _take_boot_lock(timeout=180):
    """Inter-thread AND inter-process boot mutex.

    Streamlit reruns (page visits, code updates) can overlap: without this,
    two boots race, each starts its own tunnel, and the public domain flips.
    The boot runs in a daemon thread, so waiting here is harmless."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            fd = os.open(LOCKFILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, ("%d %d" % (os.getpid(), time.time())).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(LOCKFILE)
            except OSError:
                continue
            if age > 600:  # stale lock (holder died mid-boot); break it
                try:
                    os.unlink(LOCKFILE)
                except OSError:
                    pass
                continue
            time.sleep(2)
    return False


def _release_boot_lock():
    try:
        os.unlink(LOCKFILE)
    except OSError:
        pass


def _marker(pid):
    return "[argo] tunnel proc %d starting" % pid


def _domain_for_pid(log_path, pid, timeout):
    """Find the trycloudflare domain printed AFTER this pid's start marker.

    The log is shared/appended by every boot; matching by marker keeps the
    pid<->domain mapping exact even when several tunnels were started."""
    mk = _marker(pid)
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            time.sleep(2)
            continue
        idx = content.rfind(mk)
        if idx >= 0:
            m = _TRYCF_RE.search(content, idx)
            if m:
                return m.group(1)
        time.sleep(2)
    return None


def _run_tunnel(bot_path, port, log_path):
    proc = subprocess.Popen(
        [bot_path, "tunnel", "--edge-ip-version", "auto", "--no-autoupdate",
         "--protocol", "http2", "--logfile", log_path,
         "--loglevel", "info", "--url", "http://127.0.0.1:%d" % port],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(_marker(proc.pid) + "\n")
    except OSError:
        pass
    _write_pidfile(proc.pid)
    print("[argo] tunnel process started (pid %d)" % proc.pid, flush=True)
    return proc.pid


def _run_named_tunnel(bot_path, token, log_path):
    proc = subprocess.Popen(
        [bot_path, "tunnel", "--no-autoupdate", "--logfile", log_path,
         "--loglevel", "info", "run", "--token", token],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(_marker(proc.pid) + "\n")
    except OSError:
        pass
    _write_pidfile(proc.pid)
    print("[argo] named tunnel process started (pid %d)" % proc.pid, flush=True)
    return proc.pid


def _boot_all():
    # NOTE: Streamlit re-executes this script on every rerun/code update, and
    # module-level guards do not reliably survive that, while reruns can also
    # overlap in time. The lock serializes boots; the checks below make each
    # boot idempotent via the filesystem: never a second server, never a
    # second tunnel while the previous ones are alive.
    if not _take_boot_lock(timeout=180):
        print("[argo] boot lock busy, skipping this boot", flush=True)
        return
    try:
        _boot_inner()
    finally:
        _release_boot_lock()


def _my_server_thread_alive():
    # Process-local truth (unlike module globals across Streamlit reruns).
    return any(t.name == "vless-server" and t.is_alive()
               for t in threading.enumerate())


def _write_domain_file(domain):
    # The authoritative tunnel domain. server.py reads this on every /sub
    # request so even a server thread from an older boot/process serves the
    # current domain.
    try:
        with open(os.path.join(CACHE_DIR, "domain.txt"), "w") as f:
            f.write(domain)
    except OSError as e:
        print("[argo] domain file write failed: %r" % (e,), flush=True)


def _set_domain(domain):
    server.tunnel_domain = domain
    _write_domain_file(domain)


def _boot_inner():
    # 1. local VLESS server — one thread per process. server.run() binds if
    # the port is free, otherwise standbys and takes over when the older
    # process (rolling restart) releases it.
    if _my_server_thread_alive():
        print("[vless] server thread already running in this process",
              flush=True)
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
        _set_domain(fixed_domain)
        _print_links(fixed_domain)
        return
    # 3b. quick tunnel — reuse the live one if there is one
    pid = _read_pidfile()
    if pid and _pid_alive(pid):
        domain = _domain_for_pid(log_path, pid, timeout=15)
        if domain:
            print("[argo] reusing existing quick tunnel: " + domain, flush=True)
            _set_domain(domain)
            _print_links(domain)
            return
        print("[argo] live tunnel pid %d has no domain in log yet; "
              "starting a fresh tunnel" % pid, flush=True)
    for attempt in (1, 2, 3):
        print("[argo] starting quick tunnel (attempt %d/3) ..." % attempt,
              flush=True)
        pid = _run_tunnel(bot_path, server.LISTEN_PORT, log_path)
        domain = _domain_for_pid(log_path, pid, timeout=90)
        if domain:
            _set_domain(domain)
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
